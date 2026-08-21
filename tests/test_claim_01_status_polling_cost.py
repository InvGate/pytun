"""Claim 1: cost of polling /status from an observability platform.

The answer tells the customer they can integrate /status with Datadog,
Dynatrace, Zabbix, Prometheus or Splunk. It does not state what each call
costs. These tests pin down the cost so a polling interval can be recommended
safely, and so a regression that makes /status heavier is caught.

Measured behaviour (this machine, loopback services):
  * / (ping)                        ~3-5 ms, no side effects
  * /status, services reachable     ~3 ms (1 tunnel) to ~33 ms (50 tunnels)
  * /status, port closed            ~4-13 ms (connection refused is immediate)
  * /status, service black-holed    ~5 s per batch of 4 tunnels (socket timeout)

The last line is the operational risk: latency is bounded by the socket
timeout times ceil(tunnels / pool_size), not by the number of tunnels.
"""
import logging
import os
import socket
import threading
import time
import urllib.request

import pytest

from observation.connection_check import ConnectionCheck
from observation.http_server import inspection_http_server
from observation.status import Status

# The connect timeout each service check uses, and the fixed worker count.
EXPECTED_SOCKET_TIMEOUT = 5
EXPECTED_POOL_SIZE = 4

UNROUTABLE_HOST = "192.0.2.1"  # TEST-NET-1: black-holes, forcing the timeout


def quiet_logger(name):
    logger = logging.getLogger(name)
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    return logger


def write_configs(directory, count, host, port):
    os.makedirs(directory, exist_ok=True)
    for i in range(count):
        with open(os.path.join(directory, "t%d.ini" % i), "w") as handle:
            handle.write(
                "[tunnel]\ntunnel_name=t%d\nremote_host=%s\nremote_port=%d\n"
                % (i, host, port)
            )


class RunningServer:
    def __init__(self, config_dir, log_dir):
        self.httpd = inspection_http_server(
            config_dir, "id", log_dir, Status("00:11:22:33:44:55"),
            "1.0", ("127.0.0.1", 0), quiet_logger("status-cost"),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def get(self, path, timeout=180):
        started = time.perf_counter()
        urllib.request.urlopen(self.url + path, timeout=timeout).read()
        return time.perf_counter() - started

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def live_service():
    """A listener standing in for a reachable forwarded service."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    stop = False

    def accept_loop():
        while not stop:
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                return

    threading.Thread(target=accept_loop, daemon=True).start()
    yield server.getsockname()[1]
    stop = True
    server.close()


# ------------------------------------------------------- what each call does

def test_ping_endpoint_performs_no_service_checks(tmp_path, live_service):
    """Any path other than the three known ones is a cheap ping.

    Useful to tell the customer: a liveness probe should hit /, not /status.
    """
    config_dir = tmp_path / "configs"
    write_configs(str(config_dir), 8, "127.0.0.1", live_service)
    (tmp_path / "logs").mkdir()

    server = RunningServer(str(config_dir), str(tmp_path / "logs"))
    try:
        elapsed = server.get("/")
    finally:
        server.close()

    # No sockets opened per tunnel, so this must stay in the milliseconds.
    assert elapsed < 1.0, "ping took %.3fs; it should do no service checks" % elapsed


def test_status_opens_one_tcp_connection_per_configured_tunnel(tmp_path, live_service):
    """/status is not a passive read: it probes every declared service."""
    config_dir = tmp_path / "configs"
    write_configs(str(config_dir), 5, "127.0.0.1", live_service)
    (tmp_path / "logs").mkdir()

    connects = []
    real_connect = socket.socket.connect

    def recording_connect(self, address):
        connects.append(address)
        return real_connect(self, address)

    socket.socket.connect = recording_connect
    try:
        server = RunningServer(str(config_dir), str(tmp_path / "logs"))
        try:
            server.get("/status")
        finally:
            server.close()
    finally:
        socket.socket.connect = real_connect

    probes = [a for a in connects if a == ("127.0.0.1", live_service)]
    assert len(probes) == 5, connects


def test_service_check_timeout_and_pool_size_are_what_latency_depends_on():
    """Pin the two constants that determine worst-case /status latency.

    Worst case ~= timeout * ceil(tunnels / pool). If either changes, the
    polling-interval guidance given to customers changes with it.
    """
    import inspect

    check_source = inspect.getsource(ConnectionCheck.test_connection)
    assert "sock.settimeout(%d)" % EXPECTED_SOCKET_TIMEOUT in check_source

    from observation.http_server import RequestHandlerClassFactory

    handler_source = inspect.getsource(RequestHandlerClassFactory.get_handler)
    assert "ThreadPoolExecutor(%d)" % EXPECTED_POOL_SIZE in handler_source


# ------------------------------------------------------- the operational risk

def test_status_latency_is_bounded_by_timeout_times_batches(tmp_path):
    """The finding worth telling the customer about.

    With services that black-hole traffic, /status blocks for the socket
    timeout, once per batch of pool_size tunnels. Five unreachable tunnels
    take two batches, not one.
    """
    (tmp_path / "logs").mkdir()

    def latency(count):
        directory = tmp_path / ("cfg%d" % count)
        write_configs(str(directory), count, UNROUTABLE_HOST, 9999)
        server = RunningServer(str(directory), str(tmp_path / "logs"))
        try:
            return server.get("/status")
        finally:
            server.close()

    one_batch = latency(EXPECTED_POOL_SIZE)          # 4 tunnels  -> 1 batch
    two_batches = latency(EXPECTED_POOL_SIZE + 1)    # 5 tunnels  -> 2 batches

    assert one_batch >= EXPECTED_SOCKET_TIMEOUT * 0.9, one_batch
    assert one_batch < EXPECTED_SOCKET_TIMEOUT * 1.8, one_batch
    # Crossing the pool boundary doubles the wait.
    assert two_batches >= EXPECTED_SOCKET_TIMEOUT * 1.9, two_batches


def test_reachable_services_keep_status_fast(tmp_path, live_service):
    """Healthy case: polling is cheap, so a short interval is fine."""
    config_dir = tmp_path / "configs"
    write_configs(str(config_dir), 20, "127.0.0.1", live_service)
    (tmp_path / "logs").mkdir()

    server = RunningServer(str(config_dir), str(tmp_path / "logs"))
    try:
        elapsed = server.get("/status")
    finally:
        server.close()

    assert elapsed < 2.0, "20 reachable tunnels took %.3fs" % elapsed


def test_concurrent_polls_are_served_in_parallel_and_leave_no_threads(tmp_path):
    """Overlapping polls must not queue up or leak threads.

    Answers the real question behind 'how often can it be called': if the
    platform polls faster than the response time, calls overlap. They are
    served concurrently and the threads are reclaimed.
    """
    (tmp_path / "logs").mkdir()
    config_dir = tmp_path / "configs"
    # Each call blocks ~2 batches on unreachable services.
    write_configs(str(config_dir), EXPECTED_POOL_SIZE + 1, UNROUTABLE_HOST, 9999)

    server = RunningServer(str(config_dir), str(tmp_path / "logs"))
    before = threading.active_count()
    durations = []

    def poll():
        durations.append(server.get("/status"))

    try:
        threads = [threading.Thread(target=poll) for _ in range(4)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.perf_counter() - started
    finally:
        server.close()

    assert len(durations) == 4
    slowest = max(durations)
    # Parallel, not serialised: four overlapping calls finish in about the
    # time of the slowest one, not four times that.
    assert wall < slowest * 2, (wall, durations)

    for _ in range(20):
        if threading.active_count() <= before + 1:
            break
        time.sleep(0.2)
    assert threading.active_count() <= before + 1, "handler threads leaked"
