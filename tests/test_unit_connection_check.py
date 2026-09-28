"""Unit tests for observation/connection_check.py.

test_connection() is exercised against a real loopback TCP listener for the
success path, and against an unreachable port for the failure path (including
that a registered AlertSender gets notified, covering
observation/connection_check.py line 22).
"""
import logging
import socket

from alerts.alert_sender import AlertSender
from observation.connection_check import ConnectionCheck


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _logger():
    return logging.getLogger("test-connection-check")


class TestTestConnection:
    def test_returns_true_when_service_is_reachable(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        host, port = listener.getsockname()
        try:
            checker = ConnectionCheck(_logger())
            assert checker.test_connection("tunnel-a", host, port) is True
        finally:
            listener.close()

    def test_returns_false_and_alerts_when_service_is_unreachable(self):
        unreachable_port = _free_port()
        sent = []

        class _StubAlerter(AlertSender):
            def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
                sent.append((tunnel_name, message))

        checker = ConnectionCheck(_logger(), alert_sender=_StubAlerter())

        assert checker.test_connection("tunnel-b", "127.0.0.1", unreachable_port) is False
        assert len(sent) == 1
        assert sent[0][0] == "tunnel-b"

    def test_returns_false_without_alerting_when_no_alert_sender_registered(self):
        unreachable_port = _free_port()
        checker = ConnectionCheck(_logger())

        assert checker.test_connection("tunnel-c", "127.0.0.1", unreachable_port) is False
