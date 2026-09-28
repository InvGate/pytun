"""Unit tests filling remaining pytun.py coverage gaps left by
tests/test_unit_orchestrator.py and the CLI-behaviour test_claim_* files:
test_service_is_running(), test_tunnels_and_exit(), test_mail_and_exit(),
test_http_and_exit(), register_signal_handlers(), test_everything()'s
introspection/failure branches, and test_tunnels()'s three specific
paramiko exception branches (BadHostKeyException, AuthenticationException,
PasswordRequiredException).

Reuses test_unit_orchestrator.py's FakeConnectTunnelProcess/FakeSSHClientHandle
/FakeTransport fakes by import, so ssh_connect() never touches real sockets.
"""
import logging

import paramiko
import pytest

import pytun
from tests.test_unit_orchestrator import (
    FakeConnectTunnelProcess,
    FakeSSHClientHandle,
    FakeTransport,
)


@pytest.fixture
def logger():
    log = logging.getLogger("test-pytun-gaps")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class _TemporarilyWindows:
    """Sets os.name = "nt" only around the wrapped call, restoring it
    immediately afterwards -- pytest/pathlib internals break if os.name
    stays patched past the call (WindowsPath can't be instantiated on a
    POSIX filesystem), so this must not rely on monkeypatch's end-of-test
    teardown to restore it.
    """

    def __enter__(self):
        self._real_name = pytun.os.name
        pytun.os.name = "nt"
        return self

    def __exit__(self, *exc):
        pytun.os.name = self._real_name
        return False


class TestServiceIsRunning:
    def test_non_windows_always_returns_false(self, monkeypatch, logger):
        monkeypatch.setattr(pytun.os, "name", "posix")
        assert pytun.test_service_is_running(logger) is False

    def test_windows_running_service_returns_true(self, monkeypatch, logger):
        class _FakeService:
            def as_dict(self):
                return {"status": "running"}

        monkeypatch.setattr(pytun.psutil, "win_service_get", lambda name: _FakeService(), raising=False)
        with _TemporarilyWindows():
            result = pytun.test_service_is_running(logger, service_name="AnyService")
        assert result is True

    def test_windows_stopped_service_returns_false(self, monkeypatch, logger):
        class _FakeService:
            def as_dict(self):
                return {"status": "stopped"}

        monkeypatch.setattr(pytun.psutil, "win_service_get", lambda name: _FakeService(), raising=False)
        with _TemporarilyWindows():
            result = pytun.test_service_is_running(logger)
        assert result is False

    def test_windows_missing_service_lookup_error_returns_false(self, monkeypatch, logger):
        def _raise(name):
            raise Exception("service not found")

        monkeypatch.setattr(pytun.psutil, "win_service_get", _raise, raising=False)
        with _TemporarilyWindows():
            result = pytun.test_service_is_running(logger, service_name="Missing")
        assert result is False


class TestTestTunnelsAndExit:
    def test_success_exits_zero(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "test_tunnels", lambda files, logger: False)
        with pytest.raises(SystemExit) as exc_info:
            pytun.test_tunnels_and_exit([], logger, {})
        assert exc_info.value.code == 0

    def test_failure_exits_four(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "test_tunnels", lambda files, logger: True)
        with pytest.raises(SystemExit) as exc_info:
            pytun.test_tunnels_and_exit([], logger, {})
        assert exc_info.value.code == 4


class TestMailAndExit:
    def test_no_sender_configured_exits_two(self, logger):
        with pytest.raises(SystemExit) as exc_info:
            pytun.test_mail_and_exit(logger, None)
        assert exc_info.value.code == 2

    def test_success_exits_zero(self, logger):
        class _Sender:
            def send_alert(self, *a, **k):
                pass

        with pytest.raises(SystemExit) as exc_info:
            pytun.test_mail_and_exit(logger, _Sender())
        assert exc_info.value.code == 0

    def test_send_failure_exits_one(self, logger):
        class _Sender:
            def send_alert(self, *a, **k):
                raise RuntimeError("smtp down")

        with pytest.raises(SystemExit) as exc_info:
            pytun.test_mail_and_exit(logger, _Sender())
        assert exc_info.value.code == 1


class TestHttpAndExit:
    def test_no_sender_configured_exits_two(self, logger):
        with pytest.raises(SystemExit) as exc_info:
            pytun.test_http_and_exit(logger, None)
        assert exc_info.value.code == 2

    def test_success_exits_zero(self, logger):
        class _Sender:
            def send_alert(self, *a, **k):
                pass

        with pytest.raises(SystemExit) as exc_info:
            pytun.test_http_and_exit(logger, _Sender())
        assert exc_info.value.code == 0

    def test_send_failure_exits_one(self, logger):
        class _Sender:
            def send_alert(self, *a, **k):
                raise RuntimeError("http down")

        with pytest.raises(SystemExit) as exc_info:
            pytun.test_http_and_exit(logger, _Sender())
        assert exc_info.value.code == 1


class TestRegisterSignalHandlers:
    def test_exit_gracefully_shuts_down_pool_and_terminates_all_processes(self, monkeypatch):
        captured = {}

        def _fake_signal(sig, handler):
            captured[sig] = handler

        monkeypatch.setattr(pytun.signal, "signal", _fake_signal)

        class _FakePool:
            def __init__(self):
                self.shutdown_called = False

            def shutdown(self):
                self.shutdown_called = True

        class _FakeProc:
            def __init__(self):
                self.terminated = False
                self.joined = False

            def terminate(self):
                self.terminated = True

            def join(self):
                self.joined = True

        pool = _FakePool()
        processes = {"a": _FakeProc(), "b": _FakeProc()}

        pytun.register_signal_handlers(processes, pool)

        handler = captured[pytun.signal.SIGINT]
        assert captured[pytun.signal.SIGTERM] is handler

        with pytest.raises(SystemExit) as exc_info:
            handler()

        assert exc_info.value.code == 0
        assert pool.shutdown_called is True
        assert all(p.terminated and p.joined for p in processes.values())

    def test_exit_gracefully_tolerates_a_missing_pool(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(pytun.signal, "signal", lambda sig, handler: captured.setdefault(sig, handler))

        pytun.register_signal_handlers({}, None)

        with pytest.raises(SystemExit):
            captured[pytun.signal.SIGINT]()


class TestTestEverythingBranches:
    def test_service_down_starts_the_introspection_thread(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "test_service_is_running", lambda logger, service_name=None: False)
        monkeypatch.setattr(pytun, "test_connections", lambda files, logger, processes: False)
        monkeypatch.setattr(pytun, "test_tunnels", lambda files, logger, test_reverse_forward=True: False)

        started = {"flag": False}

        class _FakeThread:
            def start(self):
                started["flag"] = True

        pytun.test_everything([], logger, {}, introspection_thread=_FakeThread())

        assert started["flag"] is True

    def test_failed_connections_and_tunnels_are_logged_without_raising(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "test_service_is_running", lambda logger, service_name=None: True)
        monkeypatch.setattr(pytun, "test_connections", lambda files, logger, processes: True)
        monkeypatch.setattr(pytun, "test_tunnels", lambda files, logger, test_reverse_forward=True: True)

        pytun.test_everything([], logger, {})  # must not raise despite both failing


class TestTestTunnelsParamikoExceptionBranches:
    def _client_that_raises_on_connect(self, error):
        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_error=error)

        return from_config_file

    def test_bad_host_key_exception_is_reported(self, monkeypatch, logger):
        expected_key = paramiko.ECDSAKey.generate()
        got_key = paramiko.ECDSAKey.generate()
        error = paramiko.BadHostKeyException("example.invalid", got_key, expected_key)

        monkeypatch.setattr(
            pytun.TunnelProcess, "from_config_file",
            staticmethod(self._client_that_raises_on_connect(error)),
        )
        failed = pytun.test_tunnels(["bad-host-key.ini"], logger)
        assert failed is True

    def test_authentication_exception_is_reported(self, monkeypatch, logger):
        monkeypatch.setattr(
            pytun.TunnelProcess, "from_config_file",
            staticmethod(self._client_that_raises_on_connect(paramiko.AuthenticationException("rejected"))),
        )
        failed = pytun.test_tunnels(["bad-auth.ini"], logger)
        assert failed is True

    def test_password_required_exception_is_reported(self, monkeypatch, logger):
        monkeypatch.setattr(
            pytun.TunnelProcess, "from_config_file",
            staticmethod(self._client_that_raises_on_connect(paramiko.PasswordRequiredException("encrypted"))),
        )
        failed = pytun.test_tunnels(["encrypted-key.ini"], logger)
        assert failed is True

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG (pytun.py ~lines 297-308): paramiko.PasswordRequiredException "
            "is a SUBCLASS of AuthenticationException, but test_tunnels()'s "
            "`except AuthenticationException` clause is listed BEFORE its "
            "`except PasswordRequiredException` clause. Python matches the "
            "first compatible except clause, so the more specific "
            "PasswordRequiredException handler (its 'is encrypted' message, "
            "lines 304-308) is unreachable dead code -- every encrypted-key "
            "failure is misreported with the generic 'was rejected' message "
            "instead. This test proves the dead branch is unreachable; it "
            "will XPASS (and must have its marker removed) once the except "
            "clauses are reordered PasswordRequiredException-before-"
            "AuthenticationException."
        ),
    )
    def test_password_required_exception_uses_its_own_specific_message_not_authentication_exceptions(
            self, monkeypatch, logger
    ):
        logged = []

        class _RecordingLogger:
            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

            def exception(self, msg, *a, **k):
                logged.append(msg % a if a else msg)

        monkeypatch.setattr(
            pytun.TunnelProcess, "from_config_file",
            staticmethod(self._client_that_raises_on_connect(paramiko.PasswordRequiredException("encrypted"))),
        )
        pytun.test_tunnels(["encrypted-key.ini"], _RecordingLogger())

        assert len(logged) == 1
        assert "is encrypted" in logged[0]
        assert "was rejected" not in logged[0]


class TestMainSecondAuthorizationCheckAndEmptyProcesses:
    """main()'s post-config device-authorization check (line ~162) and the
    "no config files found" guard (line ~172) run after the --test_* early
    exits, so they need their own ini fixture without any --test_* flag.
    """

    def _write_ini(self, tmp_path, extra=""):
        (tmp_path / "connector.ini").write_text(
            "[pytun]\ntunnel_manager_id=test-manager\n" + extra
        )

    def test_device_unauthorized_after_config_load_exits_one(self, monkeypatch, tmp_path):
        self._write_ini(tmp_path)
        (tmp_path / "configs").mkdir()
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: False)

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 1

    def test_no_config_files_found_exits_one(self, monkeypatch, tmp_path):
        self._write_ini(tmp_path)
        (tmp_path / "configs").mkdir()  # empty: no .ini tunnel configs
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 1


class _TunnelDirsNoneParams(dict):
    """A configparser-section-like object whose 'tunnel_dirs' is explicitly
    None (unlike real configparser, which never yields None for a `.get()`
    with a default) -- the only way to reach main()'s "tunnel_path is
    invalid" dead-ish branch without crashing on `join(None, ...)` first.
    """

    def get(self, key, default=None):
        if key == "tunnel_dirs":
            return None
        return dict.get(self, key, default)

    def getboolean(self, key, default=None):
        return dict.get(self, key, default)

    def getint(self, key, default=None):
        value = dict.get(self, key, default)
        return int(value) if value is not None else default


class _FakeConfigParser:
    def __init__(self, params):
        self._params = params

    def read(self, path):
        pass

    def __contains__(self, key):
        return key == "pytun"

    def __getitem__(self, key):
        return self._params


class TestMainTestAllBranch:
    """main()'s --test_all block (~lines 130-154): the ini-not-loaded branch,
    the tunnel_path-is-None branch, the OSError-starting-the-inspection-server
    branch, and the normal success path. input(), test_everything() and
    (where relevant) inspection_http_server() are all monkeypatched so the
    test never blocks on stdin or does real SSH/HTTP/network I/O.
    """

    def _prepare(self, monkeypatch, tmp_path, config_ini_argv=None):
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        (tmp_path / "configs").mkdir(exist_ok=True)
        argv = ["pytun.py", "--test_all"]
        if config_ini_argv:
            argv += ["--config_ini", config_ini_argv]
        monkeypatch.setattr(pytun.sys, "argv", argv)
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "")

    def test_missing_ini_file_hits_params_equals_empty_branch(self, monkeypatch, tmp_path):
        # No connector.ini written at all -> params == {}.
        self._prepare(monkeypatch, tmp_path)
        called = {}
        monkeypatch.setattr(
            pytun, "test_everything",
            lambda files, logger, processes, introspection_thread=None: called.update(
                introspection_thread=introspection_thread
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()

        assert exc_info.value.code == 0
        assert called["introspection_thread"] is None

    def test_tunnel_path_none_hits_tunnel_path_invalid_branch(self, monkeypatch, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text("[pytun]\ntunnel_manager_id=test-manager\n")
        # Absolute --config_ini so main() never re-joins tunnel_path with
        # application_path (which would crash on join(None, ...) first).
        self._prepare(monkeypatch, tmp_path, config_ini_argv=str(ini_path))

        fake_params = _TunnelDirsNoneParams(tunnel_manager_id="test-manager")
        monkeypatch.setattr(
            pytun.configparser, "ConfigParser", lambda: _FakeConfigParser(fake_params)
        )
        # tunnel_path is None, so the files list-comprehension must never
        # actually call join(None, f); a listdir() returning nothing makes it
        # a no-op regardless of what tunnel_path is.
        monkeypatch.setattr(pytun, "listdir", lambda path: [])

        called = {}
        monkeypatch.setattr(
            pytun, "test_everything",
            lambda files, logger, processes, introspection_thread=None: called.update(
                introspection_thread=introspection_thread, files=files
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()

        assert exc_info.value.code == 0
        assert called["introspection_thread"] is None
        assert called["files"] == []

    def test_oserror_starting_inspection_server_is_logged_and_test_everything_still_runs(
            self, monkeypatch, tmp_path
    ):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text("[pytun]\ntunnel_manager_id=test-manager\n")
        self._prepare(monkeypatch, tmp_path)

        def _raising_inspection_http_server(*args, **kwargs):
            raise OSError("Address already in use")

        monkeypatch.setattr(pytun, "inspection_http_server", _raising_inspection_http_server)

        called = {}
        monkeypatch.setattr(
            pytun, "test_everything",
            lambda files, logger, processes, introspection_thread=None: called.update(
                introspection_thread=introspection_thread
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()

        assert exc_info.value.code == 0
        assert called["introspection_thread"] is None

    def test_success_path_builds_inspection_thread_and_runs_test_everything(self, monkeypatch, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text("[pytun]\ntunnel_manager_id=test-manager\n")
        self._prepare(monkeypatch, tmp_path)

        class _FakeHttpServer:
            def serve_forever(self):
                pytest.fail("serve_forever() must never actually run in this test")

        monkeypatch.setattr(pytun, "inspection_http_server", lambda *a, **k: _FakeHttpServer())

        called = {}
        monkeypatch.setattr(
            pytun, "test_everything",
            lambda files, logger, processes, introspection_thread=None: called.update(
                introspection_thread=introspection_thread
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()

        assert exc_info.value.code == 0
        assert called["introspection_thread"] is not None
        assert called["introspection_thread"].daemon is True


class TestMainConfigSectionAndMissingIni:
    def test_missing_ini_file_falls_back_to_empty_params(self, monkeypatch, tmp_path):
        # No connector.ini written at all -> params = {} -> tunnel_manager_id
        # defaults to '' and device auth + "no config files" still apply.
        (tmp_path / "configs").mkdir()
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 1  # no config files found

    def test_config_connector_section_is_used_when_present(self, monkeypatch, tmp_path):
        (tmp_path / "connector.ini").write_text(
            "[config-connector]\ntunnel_manager_id=from-config-connector-section\n"
        )
        (tmp_path / "configs").mkdir()
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)

        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 1  # no config files found, but section was read
