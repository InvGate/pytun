"""Unit tests for tunnel_infra/TunnelProcess.py.

ssh_connect() is exercised against a real paramiko SSH server bound to
loopback (auth success, wrong-key auth failure, and host-key mismatch via
RejectPolicy), not just source inspection. from_config_file() is tested for
its validation branches and for resolving keyfile/server_key relative to the
config file's directory.
"""
import configparser
import os
import socket
import threading

import paramiko
import pytest

from tunnel_infra.TunnelProcess import TunnelProcess


class _AuthorizedKeyServerInterface(paramiko.ServerInterface):
    def __init__(self, authorized_key):
        self.authorized_key = authorized_key

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        if key.get_base64() == self.authorized_key.get_base64():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class _FakeSSHServer:
    """A real, minimal paramiko SSH server on loopback for one connection."""

    def __init__(self, authorized_key):
        self.host_key = paramiko.ECDSAKey.generate()
        self.authorized_key = authorized_key
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.host, self.port = self._sock.getsockname()
        self._transport = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except socket.timeout:
            return
        transport = paramiko.Transport(conn)
        self._transport = transport
        transport.add_server_key(self.host_key)
        server_iface = _AuthorizedKeyServerInterface(self.authorized_key)
        try:
            transport.start_server(server=server_iface)
        except (paramiko.SSHException, EOFError, OSError):
            return

    def close(self):
        self._sock.close()
        if self._transport is not None:
            self._transport.close()
        self._thread.join(timeout=5)

    def server_key_line(self):
        return "[%s]:%s %s %s\n" % (
            self.host,
            self.port,
            self.host_key.get_name(),
            self.host_key.get_base64(),
        )


@pytest.fixture
def logger():
    import logging
    return logging.getLogger("test-tunnel-process")


def _make_process(tmp_path, server, keyfile, server_key_path, username="tunneluser"):
    return TunnelProcess(
        tunnel_name="test-tunnel",
        server_host=server.host,
        server_port=server.port,
        server_port_to_forward=0,
        server_key=str(server_key_path),
        user_to_login=username,
        key_file=str(keyfile),
        recipient_host="127.0.0.1",
        recipient_port=0,
        keep_alive_time=30,
        log_level="DEBUG",
        log_to_console=False,
        log_path=str(tmp_path),
    )


class TestSSHConnectAgainstARealServer:
    def test_connects_successfully_with_the_authorized_key(self, tmp_path):
        client_key = paramiko.ECDSAKey.generate()
        server = _FakeSSHServer(authorized_key=client_key)
        try:
            keyfile = tmp_path / "client.key"
            client_key.write_private_key_file(str(keyfile))
            server_key_path = tmp_path / "known_hosts"
            server_key_path.write_text(server.server_key_line())

            process = _make_process(tmp_path, server, keyfile, server_key_path)
            client = process.ssh_connect(exit_on_failure=False)
            try:
                assert client.get_transport().is_active()
            finally:
                client.close()
        finally:
            server.close()

    def test_wrong_key_auth_failure_exits_when_exit_on_failure(self, tmp_path):
        client_key = paramiko.ECDSAKey.generate()
        wrong_key = paramiko.ECDSAKey.generate()
        server = _FakeSSHServer(authorized_key=client_key)
        try:
            keyfile = tmp_path / "wrong.key"
            wrong_key.write_private_key_file(str(keyfile))
            server_key_path = tmp_path / "known_hosts"
            server_key_path.write_text(server.server_key_line())

            process = _make_process(tmp_path, server, keyfile, server_key_path)
            with pytest.raises(SystemExit):
                process.ssh_connect(exit_on_failure=True)
        finally:
            server.close()

    def test_wrong_key_auth_failure_raises_when_not_exit_on_failure(self, tmp_path):
        client_key = paramiko.ECDSAKey.generate()
        wrong_key = paramiko.ECDSAKey.generate()
        server = _FakeSSHServer(authorized_key=client_key)
        try:
            keyfile = tmp_path / "wrong.key"
            wrong_key.write_private_key_file(str(keyfile))
            server_key_path = tmp_path / "known_hosts"
            server_key_path.write_text(server.server_key_line())

            process = _make_process(tmp_path, server, keyfile, server_key_path)
            with pytest.raises(paramiko.SSHException):
                process.ssh_connect(exit_on_failure=False)
        finally:
            server.close()

    def test_host_key_mismatch_is_rejected(self, tmp_path):
        """RejectPolicy: a known_hosts entry for a *different* host key must
        make the connection fail, proving impostor servers are refused.
        """
        client_key = paramiko.ECDSAKey.generate()
        server = _FakeSSHServer(authorized_key=client_key)
        try:
            keyfile = tmp_path / "client.key"
            client_key.write_private_key_file(str(keyfile))

            imposter_key = paramiko.ECDSAKey.generate()
            server_key_path = tmp_path / "known_hosts"
            server_key_path.write_text(
                "[%s]:%s %s %s\n" % (
                    server.host, server.port, imposter_key.get_name(), imposter_key.get_base64()
                )
            )

            process = _make_process(tmp_path, server, keyfile, server_key_path)
            with pytest.raises(paramiko.SSHException):
                process.ssh_connect(exit_on_failure=False)
        finally:
            server.close()

    def test_connection_refused_raises_when_not_exit_on_failure(self, tmp_path):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        client_key = paramiko.ECDSAKey.generate()
        keyfile = tmp_path / "client.key"
        client_key.write_private_key_file(str(keyfile))

        class _FakeServerAddr:
            host = "127.0.0.1"
            port = free_port

        process = _make_process(tmp_path, _FakeServerAddr(), keyfile, tmp_path / "missing_known_hosts")
        # No server_key file exists and nothing listens on free_port.
        with pytest.raises(Exception):
            process.ssh_connect(exit_on_failure=False)


class TestFromConfigFileValidation:
    def _write_ini(self, path, body):
        path.write_text(body)
        return path

    def test_missing_keyfile_raises(self, tmp_path):
        ini = self._write_ini(tmp_path / "tunnel.ini", (
            "[tunnel]\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
        ))
        with pytest.raises(Exception, match="Missing keyfile"):
            TunnelProcess.from_config_file(str(ini))

    def test_missing_required_field_raises_keyerror(self, tmp_path):
        (tmp_path / "the.key").write_text("not a real key, just needs to exist as a path value")
        ini = self._write_ini(tmp_path / "tunnel.ini", (
            "[tunnel]\n"
            "keyfile=the.key\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
            # server_host is required and missing
        ))
        with pytest.raises(KeyError):
            TunnelProcess.from_config_file(str(ini))

    def test_keyfile_and_server_key_resolved_relative_to_config_directory(self, tmp_path):
        subdir = tmp_path / "configs"
        subdir.mkdir()
        (subdir / "the.key").write_text("private key placeholder")
        (subdir / "hosts").write_text("known hosts placeholder")
        ini = self._write_ini(subdir / "tunnel.ini", (
            "[tunnel]\n"
            "keyfile=the.key\n"
            "server_key=hosts\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
        ))

        process = TunnelProcess.from_config_file(str(ini))

        assert process.key_file == str(subdir / "the.key")
        assert process.server_key == str(subdir / "hosts")

    def test_absolute_keyfile_path_is_used_as_is(self, tmp_path):
        subdir = tmp_path / "configs"
        subdir.mkdir()
        absolute_key = tmp_path / "elsewhere.key"
        absolute_key.write_text("private key placeholder")
        ini = self._write_ini(subdir / "tunnel.ini", (
            "[tunnel]\n"
            "keyfile=%s\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
        ) % absolute_key)

        process = TunnelProcess.from_config_file(str(ini))

        assert process.key_file == str(absolute_key)

    def test_server_key_defaults_to_none_when_absent(self, tmp_path):
        subdir = tmp_path / "configs"
        subdir.mkdir()
        (subdir / "the.key").write_text("private key placeholder")
        ini = self._write_ini(subdir / "tunnel.ini", (
            "[tunnel]\n"
            "keyfile=the.key\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
        ))

        process = TunnelProcess.from_config_file(str(ini))

        assert process.server_key is None

    def test_default_ports_and_keep_alive_time_are_applied(self, tmp_path):
        subdir = tmp_path / "configs"
        subdir.mkdir()
        (subdir / "the.key").write_text("private key placeholder")
        ini = self._write_ini(subdir / "tunnel.ini", (
            "[tunnel]\n"
            "keyfile=the.key\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
        ))

        process = TunnelProcess.from_config_file(str(ini))

        assert process.server_port == 22
        assert process.server_port_to_forward == 4000
        assert process.recipient_port == 22
        assert process.keep_alive_time == 30

    def test_connector_section_is_used_when_no_tunnel_section(self, tmp_path):
        subdir = tmp_path / "configs"
        subdir.mkdir()
        (subdir / "the.key").write_text("private key placeholder")
        ini = self._write_ini(subdir / "tunnel.ini", (
            "[connector]\n"
            "keyfile=the.key\n"
            "server_host=127.0.0.1\n"
            "username=user\n"
            "remote_host=127.0.0.1\n"
            "connector_name=named-from-connector-section\n"
        ))

        process = TunnelProcess.from_config_file(str(ini))

        assert process.tunnel_name == "named-from-connector-section"
