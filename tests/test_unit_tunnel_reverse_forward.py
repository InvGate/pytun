"""Tests for tunnel_infra/Tunnel.py's reverse_forward_tunnel(), validate_tunnel_up()
and stop(), which test_unit_tunnel.py deliberately left to this file (see its
module docstring).

One end-to-end test drives a real paramiko SSH server (tests/ssh_server.py)
through the full reverse-forward path: request_port_forward -> accept() ->
handler() relay -> validate_tunnel_up() keepalive. The remaining branches
(chan is None, failed flag exit, forwarding exception, stop() idempotence,
validate_tunnel_up's three distinct failure modes) are exercised against a
lightweight fake transport, since a real server can't deterministically and
quickly reach those specific error paths without flaky timing.
"""
import logging
import socket
import threading
import time

import paramiko
import pytest

from tunnel_infra.Tunnel import Tunnel
from tests.ssh_server import ReverseForwardSSHServer


@pytest.fixture
def logger():
    return logging.getLogger("test-tunnel-reverse-forward")


class _EchoServer:
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


class TestReverseForwardTunnelEndToEnd:
    def test_full_round_trip_through_a_real_ssh_server(self, echo_server, logger, tmp_path):
        client_key = paramiko.ECDSAKey.generate()
        server = ReverseForwardSSHServer(authorized_key=client_key)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                server.host,
                server.port,
                username="tunneluser",
                pkey=client_key,
                look_for_keys=False,
                allow_agent=False,
                timeout=5,
            )

            tunnel = Tunnel(
                "e2e-tunnel",
                recipient_host=echo_server.host,
                recipient_port=echo_server.port,
                client=client,
                port_to_forward=0,
                logger=logger,
                keep_alive_time=30,
            )

            tunnel_thread = threading.Thread(target=tunnel.reverse_forward_tunnel, daemon=True)
            tunnel_thread.start()

            _, _, bound_port = server.wait_for_forward_request()

            with socket.create_connection((server.host, bound_port), timeout=5) as probe:
                probe.settimeout(5)
                probe.sendall(b"round-trip")
                assert probe.recv(1024) == b"round-trip"

            # validate_tunnel_up() success path: send_ignore + is_active +
            # open_session all succeed on the still-live transport, and it
            # reschedules its own Timer.
            tunnel.validate_tunnel_up()
            assert tunnel.failed is False
            assert tunnel.timer is not None
        finally:
            tunnel.stop()
            assert tunnel.timer is None
            assert tunnel.transport is None
            client.close()
            server.close()
            tunnel_thread.join(timeout=5)


class _FakeChannel:
    def close(self):
        pass


class _FakeTransport:
    def __init__(self):
        self.forwarded = []
        self.cancelled = []
        self.closed = False
        self._accept_queue = []

    def request_port_forward(self, address, port):
        self.forwarded.append((address, port))

    def cancel_port_forward(self, address, port):
        self.cancelled.append((address, port))

    def accept(self, timeout=None):
        if self._accept_queue:
            return self._accept_queue.pop(0)
        return None

    def close(self):
        self.closed = True

    def send_ignore(self):
        pass

    def is_active(self):
        return True

    def open_session(self, timeout=None):
        return _FakeChannel()


class _FakeClient:
    def __init__(self, transport):
        self._transport = transport

    def get_transport(self):
        return self._transport


class TestReverseForwardTunnelBranches:
    def test_none_channel_is_skipped_then_failed_flag_stops_the_loop(self, logger, monkeypatch):
        transport = _FakeTransport()
        transport._accept_queue = [None]

        tunnel = Tunnel(
            "fake-tunnel",
            recipient_host="127.0.0.1",
            recipient_port=0,
            client=_FakeClient(transport),
            port_to_forward=1234,
            logger=logger,
        )

        # Suppress the real 30s keepalive Timer that reverse_forward_tunnel starts,
        # replacing it with a no-op that never fires during the test.
        import tunnel_infra.Tunnel as tunnel_module

        real_timer = threading.Timer
        monkeypatch.setattr(
            tunnel_module.threading, "Timer", lambda *a, **k: real_timer(9999, lambda: None)
        )

        call_count = {"n": 0}

        def _accept(timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # chan is None -> continue
            tunnel.failed = True
            return None  # loop re-checks self.failed and returns

        transport.accept = _accept

        tunnel.reverse_forward_tunnel()

        assert call_count["n"] == 2
        assert transport.forwarded == [("", 1234)]
        tunnel.timer.cancel()

    def test_exception_during_forwarding_is_logged_and_swallowed(self, logger):
        class _RaisingTransport(_FakeTransport):
            def request_port_forward(self, address, port):
                raise RuntimeError("boom")

        tunnel = Tunnel(
            "fake-tunnel",
            recipient_host="127.0.0.1",
            recipient_port=0,
            client=_FakeClient(_RaisingTransport()),
            port_to_forward=1234,
            logger=logger,
        )

        tunnel.reverse_forward_tunnel()  # must not raise

        assert tunnel.timer is None


class TestValidateTunnelUpBranches:
    def test_send_ignore_failure_marks_failed(self, logger):
        transport = _FakeTransport()
        transport.send_ignore = lambda: (_ for _ in ()).throw(OSError("dead"))
        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=1, logger=logger,
        )
        tunnel.transport = transport

        tunnel.validate_tunnel_up()

        assert tunnel.failed is True
        assert tunnel.timer is None

    def test_inactive_transport_marks_failed(self, logger):
        transport = _FakeTransport()
        transport.is_active = lambda: False
        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=1, logger=logger,
        )
        tunnel.transport = transport

        tunnel.validate_tunnel_up()

        assert tunnel.failed is True
        assert tunnel.timer is None

    def test_open_session_failure_marks_failed(self, logger):
        transport = _FakeTransport()
        transport.open_session = lambda timeout=None: (_ for _ in ()).throw(paramiko.SSHException("no session"))
        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=1, logger=logger,
        )
        tunnel.transport = transport

        tunnel.validate_tunnel_up()

        assert tunnel.failed is True
        assert tunnel.timer is None

    def test_success_reschedules_timer(self, logger):
        transport = _FakeTransport()
        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=1, logger=logger, keep_alive_time=9999,
        )
        tunnel.transport = transport

        tunnel.validate_tunnel_up()

        assert tunnel.failed is False
        assert tunnel.timer is not None
        tunnel.timer.cancel()


class TestHandlerRemainingBranches:
    """Covers the handler() lines test_unit_tunnel.py's echo-based tests don't
    reach: the sock-side clean close, the alert-sender exception path, and the
    ConnectionResetError / generic Exception guards around the relay loop.
    """

    def test_sock_side_close_breaks_the_relay_loop(self, logger):
        class _ImmediatelyClosingServer:
            def __init__(self):
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.bind(("127.0.0.1", 0))
                self._sock.listen(1)
                self._sock.settimeout(5)
                self.host, self.port = self._sock.getsockname()
                self._thread = threading.Thread(target=self._serve, daemon=True)
                self._thread.start()

            def _serve(self):
                conn, _ = self._sock.accept()
                conn.close()  # sock.recv() in handler() returns b"" -> break

            def close(self):
                self._sock.close()
                self._thread.join(timeout=5)

        server = _ImmediatelyClosingServer()
        try:
            tunnel = Tunnel(
                "fake-tunnel", recipient_host="127.0.0.1", recipient_port=0,
                client=None, port_to_forward=0, logger=logger,
            )
            chan_sock, remote_sock = socket.socketpair()
            remote_sock.settimeout(5)

            class _FakeChan:
                origin_addr = ("127.0.0.1", 1)

                def __init__(self, sock):
                    self._sock = sock

                def recv(self, n):
                    return self._sock.recv(n)

                def send(self, data):
                    return self._sock.send(data)

                def close(self):
                    self._sock.close()

                def getpeername(self):
                    return ("127.0.0.1", 2)

                def fileno(self):
                    return self._sock.fileno()

            fake_chan = _FakeChan(chan_sock)
            tunnel.handler(fake_chan, server.host, server.port)  # must return, not hang
            remote_sock.close()
        finally:
            server.close()

    def test_alert_sender_raising_is_caught_and_logged(self, logger):
        from alerts.alert_sender import AlertSender

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            unreachable_port = probe.getsockname()[1]

        class _RaisingAlerter(AlertSender):
            def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
                raise RuntimeError("alerting backend down")

        tunnel = Tunnel(
            "fake-tunnel", recipient_host="127.0.0.1", recipient_port=0,
            client=None, port_to_forward=0, logger=logger, alert_senders=[_RaisingAlerter()],
        )
        chan_sock, remote_sock = socket.socketpair()

        class _FakeChan:
            origin_addr = ("127.0.0.1", 1)

            def __init__(self, sock):
                self._sock = sock

        # Must not raise even though the only registered alert sender blows up.
        tunnel.handler(_FakeChan(chan_sock), "127.0.0.1", unreachable_port)
        remote_sock.close()

    def test_connection_reset_during_relay_is_logged_and_swallowed(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            host, port = srv.getsockname()

            def _accept_and_hold():
                conn, _ = srv.accept()
                time.sleep(5)
                conn.close()

            t = threading.Thread(target=_accept_and_hold, daemon=True)
            t.start()

            tunnel = Tunnel(
                "fake-tunnel", recipient_host="127.0.0.1", recipient_port=0,
                client=None, port_to_forward=0, logger=logger,
            )

            class _RaisingChan:
                origin_addr = ("127.0.0.1", 1)

                def getpeername(self):
                    return ("127.0.0.1", 3)

                def fileno(self):
                    raise ConnectionResetError("simulated reset")

            tunnel.handler(_RaisingChan(), host, port)  # must not raise

    def test_generic_exception_during_relay_is_logged_and_swallowed(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            host, port = srv.getsockname()

            def _accept_and_hold():
                conn, _ = srv.accept()
                time.sleep(5)
                conn.close()

            t = threading.Thread(target=_accept_and_hold, daemon=True)
            t.start()

            tunnel = Tunnel(
                "fake-tunnel", recipient_host="127.0.0.1", recipient_port=0,
                client=None, port_to_forward=0, logger=logger,
            )

            class _RaisingChan:
                origin_addr = ("127.0.0.1", 1)

                def getpeername(self):
                    return ("127.0.0.1", 3)

                def fileno(self):
                    raise ValueError("simulated generic failure")

            tunnel.handler(_RaisingChan(), host, port)  # must not raise


class TestStop:
    def test_stop_cancels_timer_and_transport_and_is_idempotent(self, logger):
        transport = _FakeTransport()
        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=42, logger=logger,
        )
        tunnel.transport = transport
        tunnel.timer = threading.Timer(9999, lambda: None)
        tunnel.timer.start()

        tunnel.stop()

        assert tunnel.timer is None
        assert tunnel.transport is None
        assert transport.cancelled == [("", 42)]
        assert transport.closed is True

        tunnel.stop()  # calling twice must not raise

    def test_stop_swallows_transport_errors(self, logger):
        class _RaisingTransport(_FakeTransport):
            def cancel_port_forward(self, address, port):
                raise RuntimeError("already gone")

        tunnel = Tunnel(
            "fake-tunnel", recipient_host="h", recipient_port=1, client=None,
            port_to_forward=42, logger=logger,
        )
        tunnel.transport = _RaisingTransport()

        tunnel.stop()  # must not raise despite cancel_port_forward blowing up
