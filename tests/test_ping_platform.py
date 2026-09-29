"""The ping endpoint reports the host OS, its commercial name and its architecture."""
import json
import platform
import urllib.request

import pytest

from observation import http_server
from observation.http_server import get_os_name, get_platform_info
from tests.test_claim_16_path_traversal import server  # noqa: F401 (fixture)


def _registry(product_name, build):
    return lambda: (product_name, build)


def test_platform_info_reports_raw_os_data(monkeypatch):
    monkeypatch.setattr(platform, "platform", lambda: "Windows-10-10.0.14393-SP0")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(http_server, "get_os_name", lambda: "Windows Server 2016 Standard")

    assert get_platform_info() == {
        "os": "Windows-10-10.0.14393-SP0",
        "os_name": "Windows Server 2016 Standard",
        "machine": "AMD64",
    }


@pytest.mark.parametrize("product_name, build, expected", [
    ("Windows Server 2016 Standard", "14393", "Windows Server 2016 Standard"),
    ("Windows Server 2022 Datacenter", "20348", "Windows Server 2022 Datacenter"),
    ("Windows 10 Pro", "19045", "Windows 10 Pro"),
    # Windows 11 still reports "Windows 10" in ProductName; build 22000+ is 11.
    ("Windows 10 Pro", "22631", "Windows 11 Pro"),
    ("Windows 10 Enterprise", "22000", "Windows 11 Enterprise"),
])
def test_os_name_from_registry(monkeypatch, product_name, build, expected):
    monkeypatch.setattr(http_server, "_read_windows_product", _registry(product_name, build))

    assert get_os_name() == expected


def test_os_name_is_empty_without_registry(monkeypatch):
    monkeypatch.setattr(http_server, "_read_windows_product", lambda: None)

    assert get_os_name() == ""


def test_os_name_tolerates_non_numeric_build(monkeypatch):
    monkeypatch.setattr(http_server, "_read_windows_product", _registry("Windows 10 Pro", "abc"))

    assert get_os_name() == "Windows 10 Pro"


def test_ping_includes_platform_info(server):  # noqa: F811
    base, _ = server
    with urllib.request.urlopen(base + "/") as response:
        body = json.loads(response.read())

    assert body["version"] == "9.9.9"
    assert {"os", "os_name", "machine"} <= body.keys()


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(values, open_error=None):
    import types

    def open_key(root, path):
        if open_error:
            raise open_error
        assert path == r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        return _FakeKey()

    return types.SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        OpenKey=open_key,
        QueryValueEx=lambda key, name: (values[name], 1),
    )


def test_read_windows_product_from_registry(monkeypatch):
    fake = _fake_winreg({"ProductName": "Windows Server 2016 Standard", "CurrentBuildNumber": "14393"})
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)

    assert http_server._read_windows_product() == ("Windows Server 2016 Standard", "14393")


def test_read_windows_product_registry_error(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "winreg", _fake_winreg({}, open_error=OSError("denied")))

    assert http_server._read_windows_product() is None


def test_read_windows_product_without_winreg(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "winreg", None)

    assert http_server._read_windows_product() is None
