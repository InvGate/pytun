"""Prometheus metrics for the tunnels, served on their own opt-in listener.

The listener only answers /metrics, so it can be exposed to the customer's
network without exposing the inspection server (/configs serves SSH keys).
The text format is also read by the OpenTelemetry Collector's prometheus
receiver.
"""
import multiprocessing
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIELDS = ("up", "connections", "connection_errors", "bytes_received", "bytes_sent")
DEFAULT_PREFIX = "connector_"

# (metric name, type, help, value key in Status.tunnel_metrics())
_TUNNEL_METRICS = (
    ("tunnel_up", "gauge", "1 when the SSH reverse tunnel is established, 0 otherwise.", "up"),
    ("tunnel_restarts_total", "counter", "Times the tunnel was restarted after going down.", "restarts"),
    ("tunnel_connections_total", "counter", "Connections forwarded to the local service.", "connections"),
    ("tunnel_connection_errors_total", "counter", "Failed connection attempts to the local service.",
     "connection_errors"),
    ("tunnel_bytes_received_total", "counter", "Bytes received from the cloud and sent to the local service.",
     "bytes_received"),
    ("tunnel_bytes_sent_total", "counter", "Bytes read from the local service and sent to the cloud.",
     "bytes_sent"),
)

_METRIC_PREFIX = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


class TunnelMetrics:
    """Per-tunnel values in shared memory, written by a TunnelProcess and read by the main process.

    The main process creates one per tunnel and hands it to every restart, so counters
    keep growing across tunnel restarts.
    """

    def __init__(self):
        self._values = multiprocessing.Array("Q", len(FIELDS))

    def add(self, field, amount=1):
        with self._values.get_lock():  # += is read-then-write; handler threads race without the lock
            self._values[FIELDS.index(field)] += amount

    def set(self, field, value):
        self._values[FIELDS.index(field)] = value

    def snapshot(self):
        return dict(zip(FIELDS, self._values[:]))


def _escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(status, prefix, tunnel_manager_id, version):
    """Prometheus text exposition format 0.0.4."""
    lines = [
        "# HELP %sinfo Connector instance information." % prefix,
        "# TYPE %sinfo gauge" % prefix,
        '%sinfo{tunnel_manager_id="%s",version="%s"} 1' % (prefix, _escape(tunnel_manager_id), _escape(version)),
    ]
    tunnels = status.tunnel_metrics()
    for name, kind, help_text, key in _TUNNEL_METRICS:
        lines.append("# HELP %s%s %s" % (prefix, name, help_text))
        lines.append("# TYPE %s%s %s" % (prefix, name, kind))
        for config_file, tunnel_name, values in tunnels:
            config = os.path.splitext(os.path.basename(config_file))[0]
            lines.append('%s%s{config="%s",tunnel="%s"} %d' % (
                prefix, name, _escape(config), _escape(tunnel_name), values[key]))
    return "\n".join(lines) + "\n"


def send_metrics(handler, text):
    """Write a Prometheus scrape response on a BaseHTTPRequestHandler."""
    body = text.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def metrics_http_server(address, render):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.partition("?")[0] != "/metrics":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            send_metrics(self, render())

        def log_message(self, format, *args):
            pass  # a scrape every few seconds would flood stderr

    return ThreadingHTTPServer(address, MetricsHandler)


def start_metrics_server(params, status, tunnel_manager_id, version, logger):
    """Serve /metrics when metrics_port is set; return the server, or None.

    Metrics are optional: a bad setting is logged and disables them, it never stops the tunnels.
    """
    port = params.get("metrics_port")
    if not port:
        return None
    prefix = params.get("metrics_prefix", DEFAULT_PREFIX)
    if not _METRIC_PREFIX.fullmatch(prefix):
        logger.error("Invalid metrics_prefix %r: metrics disabled", prefix)
        return None
    host = params.get("metrics_host", "127.0.0.1")
    try:
        server = metrics_http_server(
            (host, int(port)), lambda: render_metrics(status, prefix, tunnel_manager_id, version))
    except (OSError, ValueError) as e:
        logger.error("Couldn't start the metrics server on %s:%s, metrics disabled: %s", host, port, e)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Serving metrics on http://%s:%s/metrics", host, server.server_address[1])
    return server
