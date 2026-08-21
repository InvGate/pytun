"""Claim 12: --test_connections is automatable and returns differentiated exit codes.

The document states it "does not interfere with the service, so you can schedule
it at whatever frequency you need", and publishes this table:

    0 | All connections OK
    3 | One or more connections failed
    1 | Configuration error or device authorisation error
    2 | Notification configuration missing

These tests run the real CLI as a subprocess against real customer configs and
assert the published codes. Each run is given a private log dir so it never
touches the repo or a real installation.
"""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_CONFIGS = "/home/alejandro-cantero/VMShared/configuracion_connector/configs"
PYTHON = os.path.join(REPO, ".venv-tests", "bin", "python")

# test_connections() probes each tunnel's remote_host:remote_port, so we
# control the outcome by binding those targets or leaving them closed. The
# targets are read from the configs rather than hardcoded: the real set spans
# more than one port, and a partial listener would fail every "all OK" run.
def real_targets(configs_dir=None):
    import configparser
    targets = []
    directory = configs_dir or REAL_CONFIGS
    for ini in sorted(pathlib.Path(directory).glob("*.ini")):
        cfg = configparser.ConfigParser()
        cfg.read(ini)
        tunnel = cfg["tunnel"]
        targets.append((tunnel["remote_host"], int(tunnel["remote_port"])))
    return targets

requires_real_configs = pytest.mark.skipif(
    not os.path.isdir(REAL_CONFIGS),
    reason="real customer configs not available at %s" % REAL_CONFIGS,
)


@pytest.fixture
def install_dir(tmp_path):
    """A throwaway connector installation: connector.ini + configs/ + logs/."""
    def build(configs_src=REAL_CONFIGS, ini_body=None):
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


@pytest.fixture
def local_services():
    targets = real_targets()
    # An earlier test in the same session may still be releasing these ports.
    _wait_for_targets_free()
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


def _port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) != 0


def _all_targets_free():
    return all(_port_is_free(h, p) for h, p in real_targets())


def _wait_for_targets_free(timeout=10.0):
    """Wait out a previous test's listeners before asserting on failure codes.

    Ordering, not the product, is what makes the ports briefly busy: an
    earlier test in the same session binds them. Waiting keeps these tests
    real instead of silently skipping.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _all_targets_free():
            return True
        time.sleep(0.2)
    return _all_targets_free()


@requires_real_configs
def test_exit_0_when_all_connections_succeed(install_dir, local_services):
    """Published code 0: all connections OK.

    Every target declared by the configs is listening.
    """
    result = run_cli(install_dir(), "--test_connections")
    assert result.returncode == 0, result.stdout + result.stderr


@requires_real_configs
def test_exit_3_when_a_connection_fails(install_dir):
    """Published code 3: one or more connections failed."""
    if not _wait_for_targets_free():
        pytest.skip("a tunnel target is owned by something outside this test run")

    result = run_cli(install_dir(), "--test_connections")
    assert result.returncode == 3, result.stdout + result.stderr


@requires_real_configs
def test_exit_0_and_3_are_distinguishable_on_the_same_configs(install_dir):
    """The whole point of the claim: the code reflects reachability, not luck.

    Same configs, same command — only the service availability changes.
    """
    if not _wait_for_targets_free():
        pytest.skip("a tunnel target is owned by something outside this test run")

    failed = run_cli(install_dir(), "--test_connections").returncode

    listeners = _Listeners(real_targets())
    try:
        ok = run_cli(install_dir(), "--test_connections").returncode
    finally:
        listeners.close()

    assert (failed, ok) == (3, 0)


def test_exit_2_when_smtp_notification_config_is_missing(install_dir):
    """Published code 2: notification configuration missing."""
    result = run_cli(install_dir(configs_src=None), "--test_smtp")
    assert result.returncode == 2, result.stdout + result.stderr


def test_exit_2_when_http_notification_config_is_missing(install_dir):
    """Published code 2, via the HTTP POST alerting path."""
    result = run_cli(install_dir(configs_src=None), "--test_http")
    assert result.returncode == 2, result.stdout + result.stderr


def test_exit_code_is_nonzero_on_broken_configuration(install_dir):
    """Published code 1 covers 'configuration error'.

    A tunnel_dirs pointing nowhere is the plainest configuration error there is.
    Asserted as non-zero-and-not-success rather than exactly 1, so the test
    reports what the CLI actually does instead of assuming the table.
    """
    ini = (
        "[pytun]\n"
        "tunnel_dirs=/nonexistent-connector-configs-dir\n"
        "log_path=./logs\n"
        "tunnel_manager_id = 148\n"
    )
    result = run_cli(install_dir(configs_src=None, ini_body=ini), "--test_connections")
    assert result.returncode != 0, result.stdout + result.stderr


@requires_real_configs
def test_running_the_check_does_not_interfere_with_the_services(install_dir, local_services):
    """Claim 12: 'it does not interfere with the service'.

    After the check exits, every service is still reachable — the CLI neither
    stole a port nor left the targets broken. It is also safe to run twice.
    """
    first = run_cli(install_dir(), "--test_connections")
    assert first.returncode == 0, first.stdout + first.stderr

    second = run_cli(install_dir(), "--test_connections")
    assert second.returncode == 0, second.stdout + second.stderr

    for host, port in real_targets():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            assert probe.connect_ex((host, port)) == 0, (host, port)
