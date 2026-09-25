import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True, scope="session")
def _keep_repo_ini_intact():
    """Undo pytun.py's legacy pytun.ini -> connector.ini migration.

    The CLI tests run pytun.py from the repo, so its application path is the
    repo itself and the migration would rename the tracked pytun.ini.
    """
    legacy = os.path.join(REPO, "pytun.ini")
    migrated = os.path.join(REPO, "connector.ini")
    had_legacy = os.path.isfile(legacy)
    had_migrated = os.path.isfile(migrated)
    yield
    if had_legacy and not os.path.isfile(legacy) and not had_migrated and os.path.isfile(migrated):
        os.rename(migrated, legacy)


def _free_port():
    """Ask the OS for a free TCP port on loopback, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_synthetic_tunnel(configs_dir, index):
    """Build one synthetic, unsigned tunnel config with its own throwaway keys.

    Mirrors exactly what TunnelProcess.from_config_file() requires: a
    passwordless ECDSA private key referenced by a relative `keyfile`, and a
    known_hosts-format `server_key` pinning [server_host]:server_port with a
    freshly generated host key. Nothing here is a real credential.
    """
    server_host = "127.0.0.1"
    server_port = _free_port()
    listen_port = _free_port()
    remote_port = _free_port()
    username = "tunneluser"

    key_name = "tunnel%d.key" % index
    server_key_name = "tunnel%d.server_key" % index
    key_path = configs_dir / key_name
    server_key_path = configs_dir / server_key_name

    tunnel_key = paramiko.ECDSAKey.generate()
    tunnel_key.write_private_key_file(str(key_path))

    host_key = paramiko.ECDSAKey.generate()
    server_key_path.write_text(
        "[%s]:%s %s %s\n" % (server_host, server_port, host_key.get_name(), host_key.get_base64())
    )

    ini_body = (
        "[tunnel]\n"
        "tunnel_name=synthetic-tunnel-%d\n"
        "server_host=%s\n"
        "server_port=%s\n"
        "port=%s\n"
        "remote_host=127.0.0.1\n"
        "remote_port=%s\n"
        "username=%s\n"
        "keyfile=%s\n"
        "server_key=%s\n"
        "keep_alive_time=30\n"
    ) % (index, server_host, server_port, listen_port, remote_port, username, key_name, server_key_name)
    ini_path = configs_dir / ("tunnel%d.ini" % index)
    ini_path.write_text(ini_body)

    return {
        "ini": ini_path,
        "keyfile": key_path,
        "server_key": server_key_path,
        "server_host": server_host,
        "server_port": server_port,
        "port": listen_port,
        "remote_host": "127.0.0.1",
        "remote_port": remote_port,
        "username": username,
    }


@pytest.fixture
def synthetic_configs(tmp_path):
    """A directory of synthetic, unsigned tunnel configs with generated keys.

    Replaces PYTUN_REAL_CONFIGS for tests that only need *some* valid tunnel
    configs, not facts about real customer key material. Builds two tunnels
    on distinct, OS-assigned free ports so tests can tell them apart.
    """
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    tunnels = [_write_synthetic_tunnel(configs_dir, i) for i in range(2)]
    return {"dir": configs_dir, "tunnels": tunnels}
