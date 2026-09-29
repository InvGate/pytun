"""The ping endpoint reports the host OS, and its architecture."""
import json
import platform
import urllib.request

from observation.http_server import get_platform_info
from tests.test_claim_16_path_traversal import server  # noqa: F401 (fixture)


def test_platform_info_reports_raw_os_data(monkeypatch):
    monkeypatch.setattr(platform, "platform", lambda: "Windows-2016Server-10.0.14393-SP0")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")

    assert get_platform_info() == {
        "os": "Windows-2016Server-10.0.14393-SP0",
        "machine": "AMD64",
    }


def test_ping_includes_platform_info(server):  # noqa: F811
    base, _ = server
    with urllib.request.urlopen(base + "/") as response:
        body = json.loads(response.read())

    assert body["version"] == "9.9.9"
    assert {"os", "machine"} <= body.keys()
