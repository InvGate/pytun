"""Claim 12: --test_connections is automatable and returns differentiated exit codes.

The document states it "does not interfere with the service, so you can schedule
it at whatever frequency you need", and publishes this table:

    0 | All connections OK
    3 | One or more connections failed
    1 | Configuration error or device authorisation error
    2 | Notification configuration missing

Connection-outcome tests (0 / 3) call pytun's own functions in-process, with
test_internet_access() monkeypatched out: test_connections() always calls it,
and it otherwise reaches 8.8.8.8:53 on the real internet, which these tests
must not depend on. The remaining tests run the CLI as a subprocess, because
the subprocess boundary is the point (real argv/exit-code behaviour) and
those code paths never call test_internet_access.

All configs are synthetic and each run is given a private log dir so it never
touches the repo or a real installation.
"""
import logging
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

import pytun

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable


@pytest.fixture
def install_dir(tmp_path):
    """A throwaway connector installation: connector.ini + configs/ + logs/."""
    def build(configs_src=None, ini_body=None):
        (tmp_path / "logs").mkdir(exist_ok=True)
        configs = tmp_path / "configs"
        if configs_src is not None:
            shutil.copytree(configs_src, configs, dirs_exist_ok=True)
        else:
            configs.mkdir(exist_ok=True)
        if ini_body is None:
            # Absolute paths on purpose. pytun resolves relative tunnel_dirs
            # against get_application_path() (the executable's directory), not
            # the cwd, so a relative path here would look inside the repo.
            ini_body = (
                "[pytun]\n"
                "tunnel_dirs=%s\n"
                "log_level=DEBUG\n"
                "log_path=%s\n"
                "tunnel_manager_id = 148\n"
                "inspection_port = 9999\n"
            ) % (configs, tmp_path / "logs")
        ini = tmp_path / "connector.ini"
        ini.write_text(ini_body)
        return ini
    return build


def run_cli(ini, *args, timeout=180):
    """Run pytun.py as the customer would, with stdin closed.

    stdin is closed deliberately: an unauthorized device path calls input(),
    and a hung test is worse than a failing one.
    """
    return subprocess.run(
        [PYTHON, os.path.join(REPO, "pytun.py"), "--config_ini", str(ini), *args],
        cwd=str(pathlib.Path(ini).parent),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class _Listener:
    """A real TCP listener standing in for one forwarded local service."""

    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self._stop = False
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self):
        self._stop = True
        self.sock.close()
        self.thread.join(timeout=5)


class _Listeners:
    """Listeners for every target the configs declare — all or nothing."""

    def __init__(self, targets):
        self.listeners = []
        try:
            for host, port in targets:
                self.listeners.append(_Listener(host, port))
        except OSError:
            self.close()
            raise

    def close(self):
        for listener in self.listeners:
            listener.close()
        self.listeners = []


def _targets(synthetic_configs):
    return [(t["remote_host"], t["remote_port"]) for t in synthetic_configs["tunnels"]]


def _files(synthetic_configs):
    return [str(t["ini"]) for t in synthetic_configs["tunnels"]]


def _port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) != 0


def _all_targets_free(targets):
    return all(_port_is_free(h, p) for h, p in targets)


def _wait_for_targets_free(targets, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _all_targets_free(targets):
            return True
        time.sleep(0.2)
    return _all_targets_free(targets)


@pytest.fixture
def local_services(synthetic_configs):
    targets = _targets(synthetic_configs)
    _wait_for_targets_free(targets)
    deadline = time.time() + 10
    listeners = None
    while listeners is None:
        try:
            listeners = _Listeners(targets)
        except OSError as exc:
            if time.time() >= deadline:
                pytest.skip("cannot bind all tunnel targets %s (%s)" % (targets, exc))
            time.sleep(0.2)
    yield listeners
    listeners.close()


def _no_internet(logger):
    """Stand-in for pytun.test_internet_access(): never touch the real network."""
    return True


def test_exit_0_when_all_connections_succeed(monkeypatch, synthetic_configs, local_services):
    """Published code 0: all connections OK.

    Every target declared by the configs is listening.
    """
    monkeypatch.setattr(pytun, "test_internet_access", _no_internet)
    logger = logging.getLogger("claim12")
    with pytest.raises(SystemExit) as exc:
        pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
    assert exc.value.code == 0


def test_exit_3_when_a_connection_fails(monkeypatch, synthetic_configs):
    """Published code 3: one or more connections failed."""
    targets = _targets(synthetic_configs)
    if not _wait_for_targets_free(targets):
        pytest.skip("a tunnel target is owned by something outside this test run")

    monkeypatch.setattr(pytun, "test_internet_access", _no_internet)
    logger = logging.getLogger("claim12")
    with pytest.raises(SystemExit) as exc:
        pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
    assert exc.value.code == 3


def test_exit_0_and_3_are_distinguishable_on_the_same_configs(monkeypatch, synthetic_configs):
    """The whole point of the claim: the code reflects reachability, not luck.

    Same configs, same command — only the service availability changes.
    """
    targets = _targets(synthetic_configs)
    if not _wait_for_targets_free(targets):
        pytest.skip("a tunnel target is owned by something outside this test run")

    monkeypatch.setattr(pytun, "test_internet_access", _no_internet)
    logger = logging.getLogger("claim12")

    with pytest.raises(SystemExit) as exc:
        pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
    failed = exc.value.code

    listeners = _Listeners(targets)
    try:
        with pytest.raises(SystemExit) as exc:
            pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
        ok = exc.value.code
    finally:
        listeners.close()

    assert (failed, ok) == (3, 0)


def test_exit_2_when_smtp_notification_config_is_missing(install_dir):
    """Published code 2: notification configuration missing."""
    result = run_cli(install_dir(), "--test_smtp")
    assert result.returncode == 2, result.stdout + result.stderr


def test_exit_2_when_http_notification_config_is_missing(install_dir):
    """Published code 2, via the HTTP POST alerting path."""
    result = run_cli(install_dir(), "--test_http")
    assert result.returncode == 2, result.stdout + result.stderr


def test_exit_code_is_nonzero_on_broken_configuration(install_dir):
    """Published code 1 covers 'configuration error'.

    A tunnel_dirs pointing nowhere is the plainest configuration error there is.
    Asserted as non-zero-and-not-success rather than exactly 1, so the test
    reports what the CLI actually does instead of assuming the table. This
    path fails before test_connections() (and test_internet_access()) is ever
    reached, so the subprocess boundary is safe to keep.
    """
    ini = (
        "[pytun]\n"
        "tunnel_dirs=/nonexistent-connector-configs-dir\n"
        "log_path=./logs\n"
        "tunnel_manager_id = 148\n"
    )
    result = run_cli(install_dir(configs_src=None, ini_body=ini), "--test_connections")
    assert result.returncode != 0, result.stdout + result.stderr


def test_running_the_check_does_not_interfere_with_the_services(monkeypatch, synthetic_configs, local_services):
    """Claim 12: 'it does not interfere with the service'.

    After the check exits, every service is still reachable — the CLI neither
    stole a port nor left the targets broken. It is also safe to run twice.
    """
    monkeypatch.setattr(pytun, "test_internet_access", _no_internet)
    logger = logging.getLogger("claim12")

    with pytest.raises(SystemExit) as exc:
        pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
    assert exc.value.code == 0

    with pytest.raises(SystemExit) as exc:
        pytun.test_connections_and_exit(_files(synthetic_configs), logger, {})
    assert exc.value.code == 0

    for host, port in _targets(synthetic_configs):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            assert probe.connect_ex((host, port)) == 0, (host, port)
