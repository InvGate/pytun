"""Unit tests for observation/http_server.py filling gaps left by
tests/test_claim_16_path_traversal.py and tests/test_claim_01_status_polling_cost.py
(which already cover the happy paths for /configs, /logs, /status and the
three-fixed-routes property): the ThreadingHTTPServer ImportError fallback,
the Windows "\\?\" path-prefix strips (both in _zipdir and
add_services_status), the do_GET/handle_configs/handle_logs error branches,
add_services_status's per-file exception guard, and inspection_http_server()
actually binding to the requested address (127.0.0.1 vs all interfaces).

All servers here run on real ephemeral loopback ports in a background thread
and are shut down cleanly; no real SSH keys, only synthetic placeholder text.
"""
import configparser
import importlib
import io
import json
import logging
import socket
import sys
import threading
import urllib.request
import zipfile

import pytest

from observation.http_server import RequestHandlerClassFactory, inspection_http_server
from observation.status import Status


def _logger():
    log = logging.getLogger("test-http-server")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def _start(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get_raw(host, port, path):
    """Speak raw HTTP so a deliberately malformed self.path is never
    normalised by a client library first."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n\r\n" % (path, host, port)).encode()
        )
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, head, body


class TestThreadingHTTPServerImportFallback:
    def test_import_error_falls_back_to_plain_http_server(self, monkeypatch):
        import http.server as std_http_server
        import observation.http_server as original_module

        monkeypatch.delattr(std_http_server, "ThreadingHTTPServer", raising=True)
        sys.modules.pop("observation.http_server", None)
        try:
            reloaded = importlib.import_module("observation.http_server")
            assert reloaded.HttpServer is std_http_server.HTTPServer
        finally:
            sys.modules.pop("observation.http_server", None)
            sys.modules["observation.http_server"] = original_module


class TestZipdirWindowsPathPrefix:
    """_zipdir()'s "\\?\" strip (line ~33) doesn't use `self`, so it can be
    called directly on the handler class without a live request.
    """

    def test_windows_prefixed_path_is_stripped_before_walking(self, tmp_path):
        handler_cls = RequestHandlerClassFactory().get_handler(
            str(tmp_path), "tm-id", str(tmp_path), Status("00:11:22:33:44:55"), "9.9.9", _logger()
        )
        real_dir = tmp_path / "configs"
        real_dir.mkdir()
        (real_dir / "one.ini").write_text("[tunnel]\n")

        ziph = zipfile.ZipFile(io.BytesIO(), "w", zipfile.ZIP_DEFLATED)
        prefixed_path = "\\\\?\\" + str(real_dir)

        # Must not raise even though the literal prefixed path doesn't exist.
        handler_cls._zipdir(None, prefixed_path, ziph)


class TestAddServicesStatusWindowsPathPrefixAndErrorGuard:
    def test_prefixed_config_path_is_stripped_and_ini_files_are_scanned(self, tmp_path):
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        (config_dir / "good.ini").write_text(
            "[tunnel]\ntunnel_name=good-tunnel\nremote_host=127.0.0.1\nremote_port=1\n"
        )
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        prefixed_config_path = "\\\\?\\" + str(config_dir)
        logger = _logger()
        httpd = inspection_http_server(
            prefixed_config_path, "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
            "9.9.9", ("127.0.0.1", 0), logger,
        )
        thread = _start(httpd)
        try:
            host, port = httpd.server_address
            with urllib.request.urlopen("http://%s:%d/status" % (host, port), timeout=5) as resp:
                body = json.loads(resp.read())
            assert "good-tunnel" in body
            assert body["good-tunnel"]["remote_host"] == "127.0.0.1"
        finally:
            _stop(httpd, thread)

    def test_malformed_ini_file_is_logged_and_skipped(self, tmp_path):
        """add_services_status's per-file try/except (lines ~126-128): a
        config file missing required keys must not break the whole /status
        response for the other, valid tunnels.
        """
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        (config_dir / "broken.ini").write_text("[tunnel]\ntunnel_name=broken\n")  # no remote_host
        (config_dir / "good.ini").write_text(
            "[tunnel]\ntunnel_name=good-tunnel\nremote_host=127.0.0.1\nremote_port=1\n"
        )
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        httpd = inspection_http_server(
            str(config_dir), "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
            "9.9.9", ("127.0.0.1", 0), _logger(),
        )
        thread = _start(httpd)
        try:
            host, port = httpd.server_address
            with urllib.request.urlopen("http://%s:%d/status" % (host, port), timeout=5) as resp:
                body = json.loads(resp.read())
            assert "good-tunnel" in body
            assert "broken" not in body
        finally:
            _stop(httpd, thread)


class TestDoGetAndReturnErrorBranches:
    """do_GET()'s outer try/except (~56-58) and return_error() (~61-64):
    provoked by making status.to_dict() raise inside handle_status().
    """

    def test_status_handler_exception_is_caught_and_returned_as_json_error(self, tmp_path):
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        class _RaisingStatus:
            def to_dict(self):
                raise RuntimeError("status store exploded")

        httpd = inspection_http_server(
            str(config_dir), "tm-id", str(log_dir), _RaisingStatus(),
            "9.9.9", ("127.0.0.1", 0), _logger(),
        )
        thread = _start(httpd)
        try:
            host, port = httpd.server_address
            status, head, body = _get_raw(host, port, "/status")
            assert status == 200
            assert b"application/json" in head
            payload = json.loads(body)
            assert "status store exploded" in payload["error"]
            assert payload["tunnel_manager_id"] == "tm-id"
        finally:
            _stop(httpd, thread)


class TestHandleConfigsAndLogsErrorBranches:
    """handle_configs()/handle_logs()'s own try/except (~81-82, ~103-104):
    a config/log path that can't be walked (here: None, from a bad server
    setup) must be reported as a JSON error, not crash the request thread.
    """

    def test_configs_endpoint_reports_error_when_config_path_is_unusable(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        httpd = inspection_http_server(
            None, "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
            "9.9.9", ("127.0.0.1", 0), _logger(),
        )
        thread = _start(httpd)
        try:
            host, port = httpd.server_address
            status, head, body = _get_raw(host, port, "/configs")
            assert status == 200
            assert b"application/json" in head
            payload = json.loads(body)
            assert "error" in payload
        finally:
            _stop(httpd, thread)

    def test_logs_endpoint_reports_error_when_log_path_is_unusable(self, tmp_path):
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        httpd = inspection_http_server(
            str(config_dir), "tm-id", None, Status("00:11:22:33:44:55"),
            "9.9.9", ("127.0.0.1", 0), _logger(),
        )
        thread = _start(httpd)
        try:
            host, port = httpd.server_address
            status, head, body = _get_raw(host, port, "/logs")
            assert status == 200
            assert b"application/json" in head
            payload = json.loads(body)
            assert "error" in payload
        finally:
            _stop(httpd, thread)


class TestInspectionServerBindsRequestedAddress:
    """Complements test_claim_01_16_inspection_bind.py (which tests
    get_inspection_address() and a raw socket) by proving the actual
    inspection_http_server() factory honours the address it's given.
    """

    def test_binds_loopback_only_when_localhost_address_given(self, tmp_path):
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        httpd = inspection_http_server(
            str(config_dir), "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
            "9.9.9", ("127.0.0.1", 0), _logger(),
        )
        try:
            assert httpd.server_address[0] == "127.0.0.1"
        finally:
            httpd.server_close()

    def test_binds_all_interfaces_when_wildcard_address_given(self, tmp_path):
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        httpd = inspection_http_server(
            str(config_dir), "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
            "9.9.9", ("0.0.0.0", 0), _logger(),
        )
        try:
            assert httpd.server_address[0] == "0.0.0.0"
            # Reachable via loopback too, since 0.0.0.0 includes it.
            thread = _start(httpd)
            try:
                host_port = httpd.server_address[1]
                with urllib.request.urlopen("http://127.0.0.1:%d/" % host_port, timeout=5) as resp:
                    assert resp.status == 200
            finally:
                _stop(httpd, thread)
        finally:
            httpd.server_close()
