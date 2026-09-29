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

    def _make_bundle(self, monkeypatch, tmp_path):
        """Simulate a frozen, PyInstaller-bundled Windows app whose current
        _MEIxxx folder lives inside tmp_path, so clean_runtime_tempdir()
        scans tmp_path (not a real system temp dir).
        """
        current = tmp_path / "_MEIcurrent"
        current.mkdir()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(current), raising=False)
        monkeypatch.setattr(utils.os, "name", "nt")
        return current

    def test_deletes_old_folders_keeps_current_and_young(self, monkeypatch, tmp_path):
        current = self._make_bundle(monkeypatch, tmp_path)

        old_folder = tmp_path / "_MEIold"
        old_folder.mkdir()
        young_folder = tmp_path / "_MEIyoung"
        young_folder.mkdir()

        now = 10_000.0
        ctimes = {
            str(current): now - 5,  # young, but also "current" -> always kept
            str(old_folder): now - 3600,  # older than the 15 min threshold
            str(young_folder): now - 10,  # within the threshold
        }
        monkeypatch.setattr(utils.time, "time", lambda: now)
        monkeypatch.setattr(utils.os.path, "getctime", lambda p: ctimes[p])

        warnings = []
        logger = types.SimpleNamespace(warning=lambda *a, **k: warnings.append(a))

        utils.clean_runtime_tempdir(logger, time_threshold=15 * 60)

        assert not old_folder.exists()
        assert current.exists()
        assert young_folder.exists()
        assert warnings == []

    def test_rmtree_failure_is_logged_as_warning_and_not_raised(self, monkeypatch, tmp_path):
        current = self._make_bundle(monkeypatch, tmp_path)
        old_folder = tmp_path / "_MEIold"
        old_folder.mkdir()

        now = 10_000.0
        monkeypatch.setattr(utils.time, "time", lambda: now)
        monkeypatch.setattr(utils.os.path, "getctime", lambda p: now - 3600)

        def _raising_rmtree(path):
            raise OSError("permission denied")

        monkeypatch.setattr(utils.shutil, "rmtree", _raising_rmtree)

        warnings = []
        logger = types.SimpleNamespace(
            warning=lambda msg, **k: warnings.append(msg)
        )

        utils.clean_runtime_tempdir(logger, time_threshold=15 * 60)  # must not raise

        assert len(warnings) == 1
        assert str(old_folder) in warnings[0]

    def test_ignores_files_alongside_the_mei_folders(self, monkeypatch, tmp_path):
        current = self._make_bundle(monkeypatch, tmp_path)
        stray_file = tmp_path / "not_a_folder.txt"
        stray_file.write_text("hi")

        monkeypatch.setattr(utils.time, "time", lambda: 10_000.0)
        monkeypatch.setattr(utils.os.path, "getctime", lambda p: 10_000.0 - 3600)

        logger = types.SimpleNamespace(warning=lambda *a, **k: pytest.fail("should not warn"))

        utils.clean_runtime_tempdir(logger, time_threshold=15 * 60)  # must not raise

        assert stray_file.exists()
