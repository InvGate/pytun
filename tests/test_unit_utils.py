"""Unit tests for utils.py pure helpers.

Covers get_application_path()'s frozen/PyInstaller branch, get_bundle_path(),
and get_net_if_mac_addresses() (with psutil mocked so no real NICs are touched).
"""
import os
import sys
import types

import pytest

import utils


class TestIsAppRunningAsPyinstallerBundle:
    def test_false_in_plain_python(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert utils.is_app_running_as_pyinstaller_bundle() is False

    def test_true_when_frozen_and_meipass_set(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/some/mei/path", raising=False)
        assert utils.is_app_running_as_pyinstaller_bundle() is True

    def test_false_when_frozen_but_no_meipass(self, monkeypatch):
        # e.g. a frozen app built by something other than PyInstaller
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert utils.is_app_running_as_pyinstaller_bundle() is False


class TestGetApplicationPath:
    def test_dev_mode_returns_module_directory(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        expected = os.path.dirname(os.path.abspath(utils.__file__))
        assert utils.get_application_path() == expected

    def test_frozen_mode_returns_executable_directory(self, monkeypatch, tmp_path):
        fake_exe_dir = tmp_path / "installdir"
        fake_exe_dir.mkdir()
        fake_exe = fake_exe_dir / "pytun.exe"
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIxxx"), raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)
        assert utils.get_application_path() == str(fake_exe_dir)


class TestGetBundlePath:
    def test_returns_meipass_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert utils.get_bundle_path() == str(tmp_path)

    def test_falls_back_to_cwd_when_not_frozen(self, monkeypatch):
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert utils.get_bundle_path() == os.path.abspath(".")


class TestGetNetIfMacAddresses:
    def test_yields_only_af_link_interfaces_lowercased_with_colons(self, monkeypatch):
        af_link = utils.psutil.AF_LINK
        other_family = 999  # anything != AF_LINK

        def fake_snic(family, address):
            return types.SimpleNamespace(family=family, address=address)

        fake_addrs = {
            "eth0": [
                fake_snic(af_link, "AA-BB-CC-DD-EE-FF"),
                fake_snic(other_family, "10.0.0.1"),
            ],
            "lo": [
                fake_snic(af_link, "00-00-00-00-00-00"),
            ],
        }
        monkeypatch.setattr(utils.psutil, "net_if_addrs", lambda: fake_addrs)

        result = list(utils.get_net_if_mac_addresses())

        assert result == [
            ("eth0", "aa:bb:cc:dd:ee:ff"),
            ("lo", "00:00:00:00:00:00"),
        ]

    def test_yields_nothing_when_no_link_layer_interfaces(self, monkeypatch):
        monkeypatch.setattr(utils.psutil, "net_if_addrs", lambda: {})
        assert list(utils.get_net_if_mac_addresses()) == []


class TestCleanRuntimeTempdir:
    def test_noop_when_not_running_as_pyinstaller_bundle(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        logger = types.SimpleNamespace(warning=lambda *a, **k: pytest.fail("should not warn"))
        # Should return immediately without touching the filesystem or os.name.
        utils.clean_runtime_tempdir(logger)

    def test_noop_on_non_windows_even_if_frozen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI123"), raising=False)
        if os.name == "nt":
            pytest.skip("this branch only applies when os.name != 'nt'")
        logger = types.SimpleNamespace(warning=lambda *a, **k: pytest.fail("should not warn"))
        utils.clean_runtime_tempdir(logger)
