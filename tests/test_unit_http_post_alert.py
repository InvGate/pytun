"""Unit tests for alerts/http_post_alert.py: HTTPPostAlertSender.

Uses a real http.server.HTTPServer on loopback in a background thread (rather
than monkeypatching requests) so the test also exercises the real HTTP
request line, headers and body requests.post() produces.
"""
import base64
import json
import socket
import threading

import pytest
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

from alerts.http_post_alert import HTTPPostAlertSender


class _RecordingHandler(BaseHTTPRequestHandler):
    # Silence default stderr access logging during tests.
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        status = self.server.response_status
        self.send_response(status)
        self.end_headers()


class _RecordingServer(HTTPServer):
    daemon_threads = True

    def __init__(self, response_status=200):
        super().__init__(("127.0.0.1", 0), _RecordingHandler)
        self.requests = []
        self.response_status = response_status


@pytest.fixture
def http_server():
    server = _RecordingServer()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture
def logger():
    import logging
    return logging.getLogger("test-http-post-alert")


def _url(server):
    host, port = server.server_address
    return "http://%s:%d/alert" % (host, port)


class TestHTTPPostAlertSenderPayload:
    def test_posts_expected_json_payload(self, http_server, logger):
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user=None,
            password=None,
            logger=logger,
        )

        sender.send_alert("tunnel-a", message="something broke")

        assert len(http_server.requests) == 1
        payload = json.loads(http_server.requests[0]["body"])
        assert payload == {
            "tunnel_name": "tunnel-a",
            "message": "something broke",
            "tunnel_manager_id": "mgr-1",
        }

    def test_default_message_when_none_given(self, http_server, logger):
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user=None,
            password=None,
            logger=logger,
        )

        sender.send_alert("tunnel-a")

        payload = json.loads(http_server.requests[0]["body"])
        assert payload["message"] == "Connector Down!"

    def test_sends_basic_auth_header_when_credentials_set(self, http_server, logger):
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user="alice",
            password="s3cret",
            logger=logger,
        )

        sender.send_alert("tunnel-a")

        auth_header = http_server.requests[0]["headers"]["Authorization"]
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
        assert decoded == "alice:s3cret"

    def test_basic_auth_header_is_sent_even_when_credentials_are_none(self, http_server, logger):
        """Not the ideal behavior, but the actual one: send_alert() always
        passes `auth=(self.user, self.password)` to requests.post(), so even
        with no configured credentials an Authorization header is sent,
        encoding the literal string "None:None". Documented here rather than
        silently assumed away.
        """
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user=None,
            password=None,
            logger=logger,
        )

        sender.send_alert("tunnel-a")

        auth_header = http_server.requests[0]["headers"]["Authorization"]
        decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
        assert decoded == "None:None"


class TestHTTPPostAlertSenderErrorHandling:
    def test_non_2xx_response_is_swallowed_by_default(self, http_server, logger):
        http_server.response_status = 500
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user=None,
            password=None,
            logger=logger,
        )

        sender.send_alert("tunnel-a")  # must not raise

        assert len(http_server.requests) == 1

    def test_non_2xx_response_reraises_when_exception_on_failure(self, http_server, logger):
        http_server.response_status = 503
        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url=_url(http_server),
            user=None,
            password=None,
            logger=logger,
        )

        with pytest.raises(requests.exceptions.HTTPError):
            sender.send_alert("tunnel-a", exception_on_failure=True)

    def test_connection_error_is_swallowed_by_default(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url="http://127.0.0.1:%d/alert" % free_port,
            user=None,
            password=None,
            logger=logger,
        )

        sender.send_alert("tunnel-a")  # nothing is listening; must not raise

    def test_connection_error_reraises_when_exception_on_failure(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        sender = HTTPPostAlertSender(
            tunnel_manager_id="mgr-1",
            post_url="http://127.0.0.1:%d/alert" % free_port,
            user=None,
            password=None,
            logger=logger,
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            sender.send_alert("tunnel-a", exception_on_failure=True)
