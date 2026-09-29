"""Unit tests for pytun.py's orchestration logic.

Covers create_tunnels_from_config()/start_tunnels(), check_tunnels(),
restart_tunnels(), test_tunnels(), test_internet_access(), and the
--test_* argument dispatch in main(). TunnelProcess is replaced by an
in-process fake (monkeypatching pytun.TunnelProcess), so no real
multiprocessing.Process, SSH connection or socket is ever created.
"""
import logging
import socket

import pytest

import pytun


@pytest.fixture
def logger():
    log = logging.getLogger("test-orchestrator")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class FakeTunnelProcess:
    """Stand-in for tunnel_infra.TunnelProcess.TunnelProcess.

    Mimics only the attributes/methods pytun.py's orchestration functions
    touch: tunnel_name, pid, start(), is_alive(), exitcode, terminate().
    """

    instances = []

    def __init__(self, config_file, alert_senders=None, alive=True, exitcode=None):
        self.config_file = config_file
        self.alert_senders = alert_senders
        self.tunnel_name = "tunnel-%s" % config_file
        self.pid = 1234
        self._alive = alive
        self.exitcode = exitcode
        self.started = False
        self.terminated = False
        self.joined = False
        FakeTunnelProcess.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True

    def join(self):
        self.joined = True

    @staticmethod
    def from_config_file(config_file, alert_senders=None):
        return FakeTunnelProcess(config_file, alert_senders)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    FakeTunnelProcess.instances = []
    yield
    FakeTunnelProcess.instances = []


class TestCreateTunnelsFromConfig:
    def test_creates_one_process_per_config_file(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)
        files = ["a.ini", "b.ini", "c.ini"]
        processes = {}
        pytun.create_tunnels_from_config([], files, logger, processes)
        assert len(processes) == 3
        assert [p.config_file for p in processes.values()] == files

    def test_invalid_config_terminates_created_processes_and_exits(self, monkeypatch, logger):
        """Current behaviour: from_config_file() raising for ANY file aborts
        the whole batch (sys.exit(1)), not just skipping the bad file --
        every process already created is terminate()'d first.
        """

        class RaisingFactory:
            calls = 0

            @staticmethod
            def from_config_file(config_file, alert_senders=None):
                RaisingFactory.calls += 1
                if config_file == "good.ini":
                    return FakeTunnelProcess(config_file, alert_senders)
                raise Exception("bad config")

        monkeypatch.setattr(pytun, "TunnelProcess", RaisingFactory)
        files = ["good.ini", "bad.ini"]
        processes = {}
        with pytest.raises(SystemExit) as exc_info:
            pytun.create_tunnels_from_config([], files, logger, processes)
        assert exc_info.value.code == 1
        # The good process was created before the bad one blew up, and got
        # terminate()'d as part of the abort path.
        assert FakeTunnelProcess.instances[0].terminated is True

    @pytest.mark.xfail(
        strict=True,
        reason="BUG (pytun.py:create_tunnels_from_config): on a factory "
               "failure the abort path calls terminate() on every "
               "already-created process, but those processes were never "
               "start()'ed (start() only happens later, in "
               "start_tunnels()). A real multiprocessing.Process's "
               "terminate() then fails (its internal _popen is None), "
               "raising AttributeError instead of the intended clean "
               "sys.exit(1). Correct behaviour: the abort path should "
               "still exit(1) cleanly, e.g. by only terminate()'ing "
               "processes that were actually started.",
    )
    def test_invalid_config_terminate_on_unstarted_process_is_a_latent_bug(self, monkeypatch, logger):
        import multiprocessing

        class RealProcessLikeFactory:
            @staticmethod
            def from_config_file(config_file, alert_senders=None):
                if config_file == "good.ini":
                    proc = multiprocessing.Process(target=lambda: None)
                    proc.tunnel_name = "good"
                    return proc
                raise Exception("bad config")

        monkeypatch.setattr(pytun, "TunnelProcess", RealProcessLikeFactory)
        files = ["good.ini", "bad.ini"]
        processes = {}

        with pytest.raises(SystemExit):
            pytun.create_tunnels_from_config([], files, logger, processes)


class TestStartTunnels:
    def test_starts_each_created_process_and_records_status(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)
        files = ["a.ini", "b.ini"]
        processes = {}
        status = pytun.Status(mac_address="00:11:22:33:44:55")
        pytun.start_tunnels(files, logger, processes, [], status)
        assert all(p.started for p in processes.values())
        status_dict = status.to_dict()
        assert set(status_dict["status_data"].keys()) == {"a.ini", "b.ini"}
        for entry in status_dict["status_data"].values():
            assert entry["started_times"] == 1


class TestCheckTunnels:
    def test_healthy_tunnel_is_left_untouched(self, logger):
        proc = FakeTunnelProcess("a.ini", alive=True, exitcode=None)
        processes = {0: proc}
        to_restart = []
        pytun.check_tunnels(["a.ini"], list(processes.items()), logger, processes, to_restart, pool=None,
                            pooled_sender=_SpySender())
        assert to_restart == []
        assert 0 in processes
        assert proc.terminated is False

    def test_dead_tunnel_is_terminated_and_scheduled_for_restart(self, logger):
        proc = FakeTunnelProcess("a.ini", alive=False, exitcode=1)
        processes = {0: proc}
        to_restart = []
        sender = _SpySender()
        pytun.check_tunnels(["a.ini"], list(processes.items()), logger, processes, to_restart, pool=None,
                            pooled_sender=sender)
        assert to_restart == [0]
        assert 0 not in processes
        assert proc.terminated is True
        assert sender.calls == [proc.tunnel_name]

    def test_not_alive_but_exitcode_none_is_left_untouched(self, logger):
        """A process reporting not alive with exitcode still None (e.g. a
        transient race) is currently NOT restarted -- only is_alive() False
        AND exitcode is not None triggers a restart.
        """
        proc = FakeTunnelProcess("a.ini", alive=False, exitcode=None)
        processes = {0: proc}
        to_restart = []
        pytun.check_tunnels(["a.ini"], list(processes.items()), logger, processes, to_restart, pool=None,
                            pooled_sender=_SpySender())
        assert to_restart == []
        assert 0 in processes


class _SpySender:
    def __init__(self):
        self.calls = []

    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        self.calls.append(tunnel_name)


class TestRestartTunnels:
    def test_replaces_dead_process_with_a_fresh_one_from_same_config(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)
        files = ["a.ini"]
        processes = {}
        status = pytun.Status(mac_address="00:11:22:33:44:55")
        pytun.restart_tunnels(files, logger, processes, [0], [], status)
        assert 0 in processes
        new_proc = processes[0]
        assert new_proc.started is True
        assert new_proc.config_file == "a.ini"
        assert status.to_dict()["status_data"]["a.ini"]["started_times"] == 1

    def test_multiple_restarts_of_same_config_increment_started_times(self, monkeypatch, logger):
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)
        files = ["a.ini"]
        processes = {}
        status = pytun.Status(mac_address="00:11:22:33:44:55")
        pytun.restart_tunnels(files, logger, processes, [0], [], status)
        pytun.restart_tunnels(files, logger, processes, [0], [], status)
        assert status.to_dict()["status_data"]["a.ini"]["started_times"] == 2

    def test_no_backoff_or_restart_limit_currently_enforced(self, monkeypatch, logger):
        """Documents current behaviour: restart_tunnels() has no backoff and
        no restart-count limit -- it always restarts unconditionally.
        """
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)
        files = ["a.ini"]
        processes = {}
        status = pytun.Status(mac_address="00:11:22:33:44:55")
        for _ in range(5):
            pytun.restart_tunnels(files, logger, processes, [0], [], status)
        assert status.to_dict()["status_data"]["a.ini"]["started_times"] == 5


class FakeSSHClientHandle:
    def __init__(self, transport=None):
        self._transport = transport

    def get_transport(self):
        return self._transport

    def close(self):
        pass


class FakeTransport:
    def __init__(self, raise_on_forward=None):
        self.raise_on_forward = raise_on_forward
        self.closed = False

    def request_port_forward(self, address, port):
        if self.raise_on_forward:
            raise self.raise_on_forward

    def close(self):
        self.closed = True


class FakeConnectTunnelProcess:
    """Fake used for test_tunnels(): from_config_file() returns one of these
    instead of a real TunnelProcess, and ssh_connect() is overridden per test
    to simulate success/failure without touching the network.
    """

    def __init__(self, config_file, ssh_connect_result=None, ssh_connect_error=None):
        self.config_file = config_file
        self.tunnel_name = "tunnel-%s" % config_file
        self.server_host = "example.invalid"
        self.server_port = 22
        self.server_port_to_forward = 4000
        self.recipient_host = "127.0.0.1"
        self.recipient_port = 80
        self.logger = None
        self._ssh_connect_result = ssh_connect_result
        self._ssh_connect_error = ssh_connect_error

    def ssh_connect(self, exit_on_failure=True):
        if self._ssh_connect_error is not None:
            raise self._ssh_connect_error
        return self._ssh_connect_result


class TestTestTunnels:
    def test_success_branch_returns_false(self, monkeypatch, logger):
        client = FakeSSHClientHandle(transport=FakeTransport())

        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_result=client)

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["good.ini"], logger)
        assert failed is False

    def test_ssh_connect_failure_is_reported_and_counts_as_failed(self, monkeypatch, logger):
        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_error=Exception("auth failed"))

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["bad.ini"], logger)
        assert failed is True

    def test_socket_timeout_on_connect_is_reported_and_counts_as_failed(self, monkeypatch, logger):
        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_error=socket.timeout("timed out"))

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["timeout.ini"], logger)
        assert failed is True

    def test_bad_config_file_is_logged_and_skipped(self, monkeypatch, logger):
        def from_config_file(config_file, alert_senders=None):
            raise Exception("missing keyfile")

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["broken.ini"], logger)
        assert failed is True

    def test_reverse_forward_rejection_counts_as_failed(self, monkeypatch, logger):
        from paramiko import SSHException

        client = FakeSSHClientHandle(transport=FakeTransport(raise_on_forward=SSHException("rejected")))

        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_result=client)

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["good.ini"], logger, test_reverse_forward=True)
        assert failed is True

    def test_reverse_forward_skipped_when_not_requested(self, monkeypatch, logger):
        transport = FakeTransport()
        client = FakeSSHClientHandle(transport=transport)

        def from_config_file(config_file, alert_senders=None):
            return FakeConnectTunnelProcess(config_file, ssh_connect_result=client)

        monkeypatch.setattr(pytun.TunnelProcess, "from_config_file", staticmethod(from_config_file))
        failed = pytun.test_tunnels(["good.ini"], logger, test_reverse_forward=False)
        assert failed is False
        assert transport.closed is False


class TestTestInternetAccess:
    def test_success_when_socket_connects(self, monkeypatch, logger):
        class FakeSocket:
            def connect(self, addr):
                return None

        monkeypatch.setattr(pytun.socket, "socket", lambda *a, **kw: FakeSocket())
        monkeypatch.setattr(pytun.socket, "setdefaulttimeout", lambda t: None)
        assert pytun.test_internet_access(logger) is True

    def test_failure_when_socket_raises(self, monkeypatch, logger):
        class FakeSocket:
            def connect(self, addr):
                raise socket.error("network unreachable")

        monkeypatch.setattr(pytun.socket, "socket", lambda *a, **kw: FakeSocket())
        monkeypatch.setattr(pytun.socket, "setdefaulttimeout", lambda t: None)
        assert pytun.test_internet_access(logger) is False


class TestMainDispatch:
    """Verifies main()'s --test_* argument dispatch calls the right helper
    and exits with the expected code, without doing any real SSH/network I/O.
    """

    def _base_ini(self, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text("[pytun]\ntunnel_manager_id=test-manager\n")
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "t1.ini").write_text("[tunnel]\n")
        return ini_path

    def _run_main(self, monkeypatch, tmp_path, argv_extra):
        self._base_ini(tmp_path)
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"] + argv_extra)
        # Skip real device-authorization I/O (no signature configured -> already
        # authorized, but keep this explicit and independent of MAC lookups).
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)

    def test_test_connections_dispatches_and_exits(self, monkeypatch, tmp_path):
        self._run_main(monkeypatch, tmp_path, ["--test_connections"])
        calls = {}

        def fake_test_connections_and_exit(files, logger, processes):
            calls["files"] = files
            raise SystemExit(0)

        monkeypatch.setattr(pytun, "test_connections_and_exit", fake_test_connections_and_exit)
        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 0
        assert len(calls["files"]) == 1

    def test_test_connectors_dispatches_and_exits(self, monkeypatch, tmp_path):
        self._run_main(monkeypatch, tmp_path, ["--test_tunnels"])
        calls = {}

        def fake_test_tunnels_and_exit(files, logger, processes):
            calls["called"] = True
            raise SystemExit(4)

        monkeypatch.setattr(pytun, "test_tunnels_and_exit", fake_test_tunnels_and_exit)
        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 4
        assert calls["called"] is True

    def test_test_smtp_dispatches_and_exits(self, monkeypatch, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text(
            "[pytun]\ntunnel_manager_id=test-manager\n"
            "smtp_hostname=smtp.example.com\nsmtp_to=alerts@example.com\n"
            "smtp_login=alerts@example.com\n"
        )
        (tmp_path / "configs").mkdir()
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py", "--test_smtp"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)
        calls = {}

        def fake_test_mail_and_exit(logger, smtp_sender):
            calls["smtp_sender"] = smtp_sender
            raise SystemExit(0)

        monkeypatch.setattr(pytun, "test_mail_and_exit", fake_test_mail_and_exit)
        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 0
        assert calls["smtp_sender"] is not None

    def test_test_http_dispatches_and_exits(self, monkeypatch, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text(
            "[pytun]\ntunnel_manager_id=test-manager\n"
            "http_url=http://example.invalid/hook\nhttp_user=u\nhttp_password=p\n"
        )
        (tmp_path / "configs").mkdir()
        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py", "--test_http"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)
        calls = {}

        def fake_test_http_and_exit(logger, post_sender):
            calls["post_sender"] = post_sender
            raise SystemExit(0)

        monkeypatch.setattr(pytun, "test_http_and_exit", fake_test_http_and_exit)
        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 0
        assert calls["post_sender"] is not None

    def test_unauthorized_device_exits_before_running_tests(self, monkeypatch, tmp_path):
        self._run_main(monkeypatch, tmp_path, ["--test_connections"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: False)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
        with pytest.raises(SystemExit) as exc_info:
            pytun.main()
        assert exc_info.value.code == 1


class BreakMainLoop(Exception):
    """Sentinel used to stop main()'s `while True:` after one iteration."""


class TestMainOrchestrationLoop:
    def test_one_iteration_checks_and_restarts_without_real_30s_sleep(self, monkeypatch, tmp_path):
        ini_path = tmp_path / "connector.ini"
        ini_path.write_text("[pytun]\ntunnel_manager_id=test-manager\n")
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "t1.ini").write_text("[tunnel]\n")

        monkeypatch.setattr(pytun, "get_application_path", lambda: str(tmp_path))
        monkeypatch.setattr(pytun.sys, "argv", ["pytun.py"])
        monkeypatch.setattr(pytun.Device, "is_authorized", lambda self: True)
        monkeypatch.setattr(pytun, "TunnelProcess", FakeTunnelProcess)

        # First tunnel is created alive, then check_tunnels() will see it as
        # dead so restart_tunnels() replaces it -- proving one full
        # check+restart cycle runs inside the loop.
        real_check_tunnels = pytun.check_tunnels
        call_count = {"n": 0}

        def killing_check_tunnels(files, items, logger, processes, to_restart, pool, pooled_sender):
            call_count["n"] += 1
            for key, proc in items:
                proc._alive = False
                proc.exitcode = 1
            return real_check_tunnels(files, items, logger, processes, to_restart, pool, pooled_sender)

        monkeypatch.setattr(pytun, "check_tunnels", killing_check_tunnels)

        class FakeHTTPServer:
            def serve_forever(self):
                return None

        monkeypatch.setattr(
            pytun, "inspection_http_server",
            lambda *a, **kw: FakeHTTPServer(),
        )

        sleep_calls = {"n": 0}

        def fake_sleep(seconds):
            sleep_calls["n"] += 1
            assert seconds == 30
            raise BreakMainLoop()

        monkeypatch.setattr(pytun.time, "sleep", fake_sleep)

        with pytest.raises(BreakMainLoop):
            pytun.main()

        assert call_count["n"] == 1
        assert sleep_calls["n"] == 1
        # The single tunnel was killed and restarted exactly once.
        assert len(FakeTunnelProcess.instances) == 2
        assert FakeTunnelProcess.instances[0].terminated is True
        assert FakeTunnelProcess.instances[1].started is True
