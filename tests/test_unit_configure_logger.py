"""Unit tests for configure_logger.py: LogManager.

Each test uses a unique logger `name` (via a counter) so loggers don't leak
handlers between tests through the global `logging` module registry, and
explicitly closes/removes every handler it adds so Windows can delete the
tmp_path log files afterwards (a file handler holding an open fd blocks
deletion on Windows).
"""
import glob
import itertools
import logging
import os

import pytest

from configure_logger import LogManager

_name_counter = itertools.count()


def _unique_name():
    return "pytun-test-logger-%d" % next(_name_counter)


@pytest.fixture
def cleanup_logger():
    """Yield a factory producing fresh logger names, and close/remove every
    handler added to loggers created during the test afterwards."""
    created_loggers = []

    def factory():
        name = _unique_name()
        created_loggers.append(name)
        return name

    yield factory

    for name in created_loggers:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        paramiko_logger = logging.getLogger("paramiko")
        # configure_logger also attaches to "paramiko" whenever name != "pytun";
        # only remove handlers we know we added (identity match isn't tracked
        # here, so just drop any handler pointing at our tmp files).
        for handler in list(paramiko_logger.handlers):
            base_filename = getattr(handler, "baseFilename", "")
            if "pytun-test" in base_filename or not os.path.exists(os.path.dirname(base_filename) or "."):
                handler.close()
                paramiko_logger.removeHandler(handler)


class TestConfigureLogger:
    def test_creates_file_handler_in_given_path(self, tmp_path, cleanup_logger):
        name = cleanup_logger()
        logger = LogManager.configure_logger("test.log", level=logging.DEBUG, path=str(tmp_path), name=name)

        assert logger.name == name
        log_file = tmp_path / "test.log"
        logger.info("hello")
        for handler in logger.handlers:
            handler.flush()
        assert log_file.exists()
        assert "hello" in log_file.read_text()

    def test_level_is_applied_to_logger_and_handlers(self, tmp_path, cleanup_logger):
        name = cleanup_logger()
        logger = LogManager.configure_logger("test.log", level=logging.WARNING, path=str(tmp_path), name=name)

        assert logger.level == logging.WARNING
        assert all(h.level == logging.WARNING for h in logger.handlers)

    def test_defaults_to_info_level_when_level_not_given(self, tmp_path, cleanup_logger):
        name = cleanup_logger()
        logger = LogManager.configure_logger("test.log", path=str(tmp_path), name=name)

        assert logger.level == logging.INFO

    def test_console_handler_added_only_when_requested(self, tmp_path, cleanup_logger):
        name_no_console = cleanup_logger()
        logger_no_console = LogManager.configure_logger(
            "test.log", path=str(tmp_path), log_to_console=False, name=name_no_console)
        assert not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                        for h in logger_no_console.handlers)

        name_console = cleanup_logger()
        logger_with_console = LogManager.configure_logger(
            "test2.log", path=str(tmp_path), log_to_console=True, name=name_console)
        assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                    for h in logger_with_console.handlers)

    def test_returns_same_underlying_logger_for_same_name(self, tmp_path, cleanup_logger):
        name = cleanup_logger()
        logger1 = LogManager.configure_logger("test.log", path=str(tmp_path), name=name)
        logger2 = LogManager.configure_logger("test.log", path=str(tmp_path), name=name)

        assert logger1 is logger2
        assert logger1 is logging.getLogger(name)

    def test_non_pytun_name_also_attaches_handler_to_paramiko_logger(self, tmp_path, cleanup_logger):
        name = cleanup_logger()
        LogManager.configure_logger("test.log", path=str(tmp_path), name=name)

        paramiko_logger = logging.getLogger("paramiko")
        base_filenames = [getattr(h, "baseFilename", None) for h in paramiko_logger.handlers]
        assert any(bf and os.path.dirname(bf) == str(tmp_path) for bf in base_filenames)

    def test_falls_back_to_application_path_logs_dir_when_path_does_not_exist(
            self, tmp_path, cleanup_logger, monkeypatch):
        missing_path = tmp_path / "does" / "not" / "exist"
        fallback_root = tmp_path / "app"
        fallback_root.mkdir()
        monkeypatch.setattr("configure_logger.get_application_path", lambda: str(fallback_root))

        name = cleanup_logger()
        logger = LogManager.configure_logger("test.log", path=str(missing_path), name=name)

        fallback_logs_dir = fallback_root / "logs"
        assert fallback_logs_dir.is_dir()
        assert (fallback_logs_dir / "test.log").exists()
        logger.info("fallback works")
