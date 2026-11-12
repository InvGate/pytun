"""Tests for observation/metrics.py: shared tunnel counters, the Prometheus
text rendering and the opt-in /metrics listener.

The rendering is checked with prometheus_client's own parser (dev-only
dependency), so the contract under test is "Prometheus can read it", not the
exact wording of each line.
"""
import configparser
import logging
import multiprocessing
import os
import urllib.error
import urllib.request

import pytest
from prometheus_client.parser import text_string_to_metric_families

from observation.metrics import TunnelMetrics, render_metrics, start_metrics_server
from observation.status import Status


@pytest.fixture
def logger():
    log = logging.getLogger("test-metrics")
    log.propagate = True
    return log


def _params(body=""):
    config = configparser.ConfigParser()
    config.read_string("[pytun]\n" + body)
    return config["pytun"]


def _samples(text):
    """{(sample name, tunnel label): value} plus the family types, via the real parser."""
    families = list(text_string_to_metric_families(text))
    samples = {(s.name, s.labels.get("tunnel")): s for f in families for s in f.samples}
    types = {f.name: f.type for f in families}
    return samples, types


def _child_counts_one_connection(metrics):
    metrics.add("connections")
    metrics.add("bytes_sent", 10)


class TestTunnelMetrics:
    # pytest's own threads trigger the fork() warning on Linux/3.12; the child only touches the array.
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
    def test_counts_from_successive_tunnel_processes_accumulate(self):
        """A tunnel process and its restart write into the same shared counters."""
        metrics = TunnelMetrics()
        for _ in range(2):
            child = multiprocessing.Process(target=_child_counts_one_connection, args=(metrics,))
            child.start()
            child.join(timeout=60)
            assert child.exitcode == 0

        snapshot = metrics.snapshot()
        assert snapshot["connections"] == 2
        assert snapshot["bytes_sent"] == 20


class TestRenderMetrics:
    @pytest.fixture
    def status(self):
        status = Status(mac_address="00:11:22:33:44:55")
        ldap_file = os.path.join("configs", "ldap.ini")
        ldap = status.start_tunnel(ldap_file, 'LDAP "prod" C:\\x')
        status.start_tunnel(ldap_file, 'LDAP "prod" C:\\x')  # one restart
        ldap.set("up", 1)
        ldap.add("connections", 3)
        ldap.add("connection_errors", 2)
        ldap.add("bytes_received", 100)
        ldap.add("bytes_sent", 250)
        status.start_tunnel(os.path.join("configs", "db.ini"), "db-tunnel")
        return status

    def test_reports_each_tunnel_with_its_values(self, status):
        samples, _ = _samples(render_metrics(status, "connector_", "tm-1", "9.9.9"))

        ldap = 'LDAP "prod" C:\\x'  # quotes and backslash survive escaping
        assert samples[("connector_tunnel_up", ldap)].value == 1
        assert samples[("connector_tunnel_restarts_total", ldap)].value == 1
        assert samples[("connector_tunnel_connections_total", ldap)].value == 3
        assert samples[("connector_tunnel_connection_errors_total", ldap)].value == 2
        assert samples[("connector_tunnel_bytes_received_total", ldap)].value == 100
        assert samples[("connector_tunnel_bytes_sent_total", ldap)].value == 250
        assert samples[("connector_tunnel_up", ldap)].labels["config"] == "ldap"

        assert samples[("connector_tunnel_up", "db-tunnel")].value == 0
        assert samples[("connector_tunnel_restarts_total", "db-tunnel")].value == 0

    def test_info_metric_identifies_the_connector(self, status):
        samples, _ = _samples(render_metrics(status, "connector_", "tm-1", "9.9.9"))

        info = samples[("connector_info", None)]
        assert info.value == 1
        assert info.labels == {"tunnel_manager_id": "tm-1", "version": "9.9.9"}

    def test_counters_and_gauges_are_typed(self, status):
        _, types = _samples(render_metrics(status, "connector_", "tm-1", "9.9.9"))

        assert types["connector_tunnel_up"] == "gauge"
        # The parser drops the _total suffix from counter family names.
        assert types["connector_tunnel_restarts"] == "counter"
        assert types["connector_tunnel_bytes_sent"] == "counter"

    def test_prefix_is_applied_to_every_metric(self, status):
        samples, _ = _samples(render_metrics(status, "invgate_", "tm-1", "9.9.9"))

        assert samples
        assert all(name.startswith("invgate_") for name, _ in samples)


class TestStartMetricsServer:
    def test_disabled_unless_metrics_port_is_set(self, logger):
        assert start_metrics_server(_params(), Status("mac"), "tm-1", "9.9.9", logger) is None

    def test_serves_metrics_on_loopback_by_default(self, logger):
        server = start_metrics_server(_params("metrics_port=0\n"), Status("mac"), "tm-1", "9.9.9", logger)
        try:
            host, port = server.server_address
            assert host == "127.0.0.1"
            with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port, timeout=5) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith("text/plain")
                samples, _ = _samples(response.read().decode())
            assert samples[("connector_info", None)].labels["tunnel_manager_id"] == "tm-1"
        finally:
            server.shutdown()
            server.server_close()

    def test_uses_configured_prefix(self, logger):
        server = start_metrics_server(_params("metrics_port=0\nmetrics_prefix=invgate_\n"), Status("mac"),
                                      "tm-1", "9.9.9", logger)
        try:
            port = server.server_address[1]
            with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % port, timeout=5) as response:
                samples, _ = _samples(response.read().decode())
            assert ("invgate_info", None) in samples
        finally:
            server.shutdown()
            server.server_close()

    def test_binds_the_configured_host(self, logger):
        server = start_metrics_server(_params("metrics_port=0\nmetrics_host=0.0.0.0\n"), Status("mac"),
                                      "tm-1", "9.9.9", logger)
        try:
            assert server.server_address[0] == "0.0.0.0"
        finally:
            server.shutdown()
            server.server_close()

    @pytest.mark.parametrize("path", ["/", "/configs", "/logs", "/status", "/metrics/../configs"])
    def test_only_metrics_is_served(self, logger, path):
        """This listener may face the network: it must never serve the inspection routes."""
        server = start_metrics_server(_params("metrics_port=0\n"), Status("mac"), "tm-1", "9.9.9", logger)
        try:
            port = server.server_address[1]
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5)
            assert exc_info.value.code == 404
        finally:
            server.shutdown()
            server.server_close()

    @pytest.mark.parametrize("body", [
        "metrics_port=0\nmetrics_prefix=9bad-prefix\n",
        "metrics_port=not-a-port\n",
        "metrics_port=0\nmetrics_host=256.256.256.256\n",
    ])
    def test_bad_config_disables_metrics_without_raising(self, logger, caplog, body):
        """Metrics are optional: a mistake there must not take the tunnels down."""
        with caplog.at_level(logging.ERROR, logger="test-metrics"):
            assert start_metrics_server(_params(body), Status("mac"), "tm-1", "9.9.9", logger) is None
        assert any("metrics" in record.getMessage().lower() for record in caplog.records)
