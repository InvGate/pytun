"""Claim 16: the CVE-2024-2318 class of path traversal does not apply.

The document asserts three structural properties of the introspection server:
  a) exactly three fixed routes, matched by literal comparison;
  b) no request parameter is processed at all;
  c) the packaged directories are fixed at startup from the config file and
     cannot be altered by an HTTP request.

These tests run a real introspection server and attack it.
"""
import io
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from observation.http_server import inspection_http_server
from observation.status import Status

SECRET_OUTSIDE = "ssh-key-that-must-never-be-served"


@pytest.fixture
def server(tmp_path):
    """A live introspection server bound to loopback, as shipped by default."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "tunnel1.ini").write_text(
        "[tunnel]\ntunnel_name=t1\nremote_host=127.0.0.1\nremote_port=1\n"
    )
    (config_dir / "keyfile1").write_text("PRIVATE-KEY-INSIDE-CONFIGS")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "main_connector.log").write_text("log line")

    # A secret that lives OUTSIDE both configured directories. Property (c)
    # means no request may ever reach it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text(SECRET_OUTSIDE)

    logger = logging.getLogger("test-introspection")
    logger.addHandler(logging.NullHandler())

    httpd = inspection_http_server(
        str(config_dir), "tm-id", str(log_dir), Status("00:11:22:33:44:55"),
        "9.9.9", ("127.0.0.1", 0), logger,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        yield base, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get(base, path):
    """Raw request, bypassing urllib's path normalisation.

    urllib would collapse '../' client-side, which would make the traversal
    test prove nothing. We speak HTTP directly so the server sees the
    traversal verbatim.
    """
    host_port = base[len("http://"):]
    host, port = host_port.split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(
            ("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host_port)).encode()
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


TRAVERSALS = [
    "/../outside/id_rsa",
    "/../../etc/passwd",
    "/configs/../../outside/id_rsa",
    "/logs/../outside/id_rsa",
    "/%2e%2e/outside/id_rsa",
    "/..%2f..%2fetc%2fpasswd",
    "/....//outside/id_rsa",
    "/status/../../outside/id_rsa",
    "/etc/passwd",
    "/..\\..\\outside\\id_rsa",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_never_returns_file_contents(server, path):
    """Property (a)+(c): anything that is not a literal known route is a ping."""
    base, _ = server
    status, _, body = get(base, path)

    assert SECRET_OUTSIDE.encode() not in body
    assert b"root:" not in body  # /etc/passwd marker
    # Non-matching paths fall through to handle_ping, which answers JSON.
    assert status == 200
    assert b'"status": "ok"' in body
    assert b'"version": "9.9.9"' in body


@pytest.mark.parametrize("path", [
    "/configs?path=../../etc/passwd",
    "/logs?fileName=../../outside/id_rsa",
    "/status?dir=/etc",
])
def test_query_parameters_are_not_routes_and_are_ignored(server, path):
    """Property (b): no request parameter is processed.

    A query string makes self.path differ from the literal route, so these do
    not even reach the zip handlers — they fall through to ping.
    """
    base, _ = server
    status, _, body = get(base, path)
    assert status == 200
    assert b'"status": "ok"' in body
    assert SECRET_OUTSIDE.encode() not in body
    assert b"root:" not in body


def test_only_three_routes_are_special_cased(server):
    """Property (a): /status, /logs, /configs behave specially; others ping."""
    base, _ = server

    _, _, status_body = get(base, "/status")
    assert b'"mac_address"' in status_body

    for route in ("/logs", "/configs"):
        _, head, body = get(base, route)
        assert b"application/zip" in head, route
        assert body[:2] == b"PK", route  # a real zip

    for route in ("/", "/admin", "/Status", "/CONFIGS", "/status/"):
        _, _, body = get(base, route)
        assert b'"status": "ok"' in body, route


def test_configs_zip_is_confined_to_the_configured_directory(server):
    """Property (c): the zip contains only the configured dir, nothing outside."""
    base, _ = server
    _, _, body = get(base, "/configs")

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        contents = b"".join(zf.read(n) for n in names)

    assert names, "configs zip was empty"
    assert SECRET_OUTSIDE.encode() not in contents
    assert not any("outside" in n for n in names)


def test_configs_endpoint_serves_the_private_key_without_authentication(server):
    """The document's own admission, stated plainly and proven.

    Claim 16 concedes /configs ships the tunnel private key with no auth. This
    test asserts that concession is accurate — it is the reason the port must
    never be exposed.
    """
    base, _ = server
    status, head, body = get(base, "/configs")

    assert status == 200
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        contents = b"".join(zf.read(n) for n in zf.namelist())

    assert b"PRIVATE-KEY-INSIDE-CONFIGS" in contents
    assert b"WWW-Authenticate" not in head
