"""Unit tests for tunnel_infra/Tunnel.py.

handler() is tested against a real loopback echo server, using a duck-typed
fake "channel" built on socket.socketpair() instead of a real paramiko
server-side reverse-forward channel. A full paramiko integration would need
the *server* side to itself accept a `request_port_forward`, bind a listening
socket, and open a `forwarded-tcpip` channel back to the client for every
inbound connection -- paramiko does not implement that server-side listener
for you (see paramiko's own forwarding demos), so building and debugging one
correctly and portably (Linux + Windows CI) was judged not worth the time
budget here versus this fallback, which paramiko's own docs describe `chan`
in `handler()` as needing only to support recv()/send()/close() -- exactly
what socket.socketpair() (portable, no AF_UNIX) plus a thin wrapper gives us.
`ssh_connect()` itself *is* covered against a real paramiko SSH server in
tests/test_unit_tunnel_process.py.
"""
import inspect
import logging
import select
import socket
import threading

import pytest

from tunnel_infra.Tunnel import Tunnel
import tunnel_infra.Tunnel as tunnel_module
from alerts.alert_sender import AlertSender


class _FakeChannel:
    """Duck-types the paramiko Channel surface Tunnel.handler() relies on."""

    def __init__(self, sock):
        self._sock = sock
        self.origin_addr = ("127.0.0.1", 12345)

    def recv(self, n):
        return self._sock.recv(n)

    def send(self, data):
        return self._sock.send(data)

    def close(self):
        self._sock.close()

    def getpeername(self):
        return ("127.0.0.1", 54321)

    def fileno(self):
        # select.select() requires every watched object to expose fileno().
        return self._sock.fileno()


class _EchoServer:
    """A tiny loopback TCP echo server, one connection at a time."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.host, self.port = self._sock.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except socket.timeout:
            return
        conn.settimeout(5)
        with conn:
            try:
                while True:
                    data = conn.recv(1024)
                    if not data:
                        return
                    conn.sendall(data)
            except OSError:
                return

    def close(self):
        self._sock.close()
        self._thread.join(timeout=5)


@pytest.fixture
def echo_server():
    server = _EchoServer()
    yield server
    server.close()


@pytest.fixture
def logger():
    return logging.getLogger("test-tunnel")


def _make_tunnel(logger, alert_senders=None):
    return Tunnel(
        "test-tunnel",
        recipient_host="127.0.0.1",
        recipient_port=0,
        client=None,
        port_to_forward=0,
        logger=logger,
        alert_senders=alert_senders,
    )


class TestHandlerRelay:
    def test_relays_bytes_in_both_directions_and_stops_on_peer_close(self, echo_server, logger):
        tunnel = _make_tunnel(logger)
        chan_sock, remote_sock = socket.socketpair()
        remote_sock.settimeout(5)
        fake_chan = _FakeChannel(chan_sock)

        thread = threading.Thread(
            target=tunnel.handler,
            args=(fake_chan, echo_server.host, echo_server.port),
            daemon=True,
        )
        thread.start()

        remote_sock.sendall(b"hello-world")
        assert remote_sock.recv(1024) == b"hello-world"

        remote_sock.sendall(b"again")
        assert remote_sock.recv(1024) == b"again"

        remote_sock.close()  # chan.recv() now returns b"" -> handler breaks
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_connect_failure_alerts_registered_senders_and_returns(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            unreachable_port = probe.getsockname()[1]

        sent = []

        class _StubAlerter(AlertSender):
            def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
                sent.append((tunnel_name, message))

        tunnel = _make_tunnel(logger, alert_senders=[_StubAlerter()])
        chan_sock, remote_sock = socket.socketpair()
        fake_chan = _FakeChannel(chan_sock)

        # No listener on unreachable_port: sock.connect() must fail quickly.
        tunnel.handler(fake_chan, "127.0.0.1", unreachable_port)

        assert len(sent) == 1
        assert sent[0][0] == "test-tunnel"
        assert "127.0.0.1" in sent[0][1]
        remote_sock.close()

    def test_connect_failure_without_alert_senders_does_not_raise(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            unreachable_port = probe.getsockname()[1]

        tunnel = _make_tunnel(logger, alert_senders=None)
        chan_sock, remote_sock = socket.socketpair()
        fake_chan = _FakeChannel(chan_sock)

        tunnel.handler(fake_chan, "127.0.0.1", unreachable_port)  # must not raise
        remote_sock.close()


class TestSelectHasNoTimeout:
    """CLAUDE.md ('select() no timeout', tunnel_infra/Tunnel.py ~line 82):
    verified true by both source inspection and intercepting the real call.
    """

    def test_source_calls_select_with_only_the_readable_list(self):
        source = inspect.getsource(Tunnel.handler)
        assert "select.select([sock, chan], [], [])" in source

    def test_real_select_call_receives_no_timeout_argument(self, echo_server, logger, monkeypatch):
        captured = {}
        real_select = select.select

        def _recording_select(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return real_select(*args, **kwargs)

        monkeypatch.setattr(tunnel_module.select, "select", _recording_select)

        tunnel = _make_tunnel(logger)
        chan_sock, remote_sock = socket.socketpair()
        remote_sock.settimeout(5)
        fake_chan = _FakeChannel(chan_sock)

        thread = threading.Thread(
            target=tunnel.handler,
            args=(fake_chan, echo_server.host, echo_server.port),
            daemon=True,
        )
        thread.start()
        remote_sock.sendall(b"ping")
        assert remote_sock.recv(1024) == b"ping"
        remote_sock.close()
        thread.join(timeout=5)

        assert captured["kwargs"] == {}
        assert len(captured["args"]) == 3  # rlist, wlist, xlist -- no timeout


class TestDelBeforeInitCompletes:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Tunnel.__del__ calls stop(), which reads self.timer without a "
            "getattr guard (tunnel_infra/Tunnel.py ~line 153). If __init__ "
            "never runs (or raises before assigning self.timer), __del__ "
            "raises AttributeError. Regression test only -- not fixed here."
        ),
    )
    def test_del_before_init_assigns_timer_raises_attributeerror(self):
        tunnel = object.__new__(Tunnel)  # __init__ deliberately skipped
        tunnel.__del__()
