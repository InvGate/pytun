# Connector metrics (Prometheus / OpenTelemetry)

Since version 1.3.0 the connector can expose the state of its tunnels in the
[Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/).
Any tool that scrapes Prometheus endpoints can read it: Prometheus itself, the
OpenTelemetry Collector, Grafana Alloy, and most monitoring agents.

Use it to watch the tunnels from your own monitoring stack: dashboards, alerting rules and
history, instead of (or as well as) the SMTP and HTTP POST alerts. Those alerts keep working
exactly as before.

## Contents

- [Enabling metrics](#enabling-metrics)
- [Exposing metrics to another machine](#exposing-metrics-to-another-machine)
- [Metrics reference](#metrics-reference)
- [How the values behave](#how-the-values-behave)
- [Collecting with Prometheus](#collecting-with-prometheus)
- [Collecting with the OpenTelemetry Collector](#collecting-with-the-opentelemetry-collector)
- [Alerting rules](#alerting-rules)
- [Useful queries](#useful-queries)
- [Troubleshooting](#troubleshooting)

## Enabling metrics

Metrics are **off by default**. Add `metrics_port` to the `[pytun]` section of
`connector.ini` and restart the connector service:

```ini
[pytun]
tunnel_manager_id = my-connector-001
# ...

metrics_port   = 9998           # Required: turns metrics on
metrics_host   = 127.0.0.1      # Optional: address to listen on
metrics_prefix = connector_     # Optional: prefix for every metric name
```

| Setting | Default | Description |
|---------|---------|-------------|
| `metrics_port` | not set (metrics off) | TCP port that serves `/metrics`. Pick any free port. |
| `metrics_host` | `127.0.0.1` | Address to listen on. `127.0.0.1` accepts only local requests. Use `0.0.0.0` (all interfaces) or a specific interface IP when Prometheus runs on another machine. |
| `metrics_prefix` | `connector_` | Prefix added to every metric name. Must match `[a-zA-Z_:][a-zA-Z0-9_:]*`. Change it if `connector_` clashes with metrics you already collect, for example `invgate_connector_`. |

The settings are read when the connector starts. Restart the service after changing them.

Check that it works from the connector machine:

```bash
curl http://127.0.0.1:9998/metrics
```

The main log (`logs/main_connector.log`) shows `Serving metrics on http://127.0.0.1:9998/metrics`
when the listener starts.

## Exposing metrics to another machine

Metrics are served by their **own listener**, separate from the inspection server on port 9999.
It answers only `GET /metrics`; every other path returns `404`. Opening the metrics port to the
network does **not** expose `/configs`, `/logs` or `/status`. Keep `inspection_localhost_only=True`
as usual.

To let a Prometheus server on another machine scrape the connector:

1. Set `metrics_host = 0.0.0.0` (or the IP of the interface Prometheus will reach) and restart the
   connector service.
2. Allow the port in the Windows firewall. Restrict it to your Prometheus server when you can:

   ```powershell
   New-NetFirewallRule -DisplayName "InvGate Connector metrics" -Direction Inbound `
       -Protocol TCP -LocalPort 9998 -RemoteAddress 10.0.0.50 -Action Allow
   ```

   Replace `10.0.0.50` with the address of your Prometheus server.

The endpoint has no authentication and no TLS. What it reveals: tunnel names, config file names,
the `tunnel_manager_id`, the connector version and traffic counters. It contains no credentials,
keys or hostnames of your services. If that is still too much for your network, keep the default
`127.0.0.1` and run the collector on the connector machine.

## Metrics reference

The names below use the default `connector_` prefix.

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `connector_info` | gauge | `tunnel_manager_id`, `version` | Always `1`. Identifies the connector and its version. |
| `connector_tunnel_up` | gauge | `config`, `tunnel` | `1` while the SSH reverse tunnel is established, `0` otherwise. |
| `connector_tunnel_restarts_total` | counter | `config`, `tunnel` | Times the connector restarted the tunnel after it went down. |
| `connector_tunnel_connections_total` | counter | `config`, `tunnel` | Connections from the cloud forwarded successfully to the local service. |
| `connector_tunnel_connection_errors_total` | counter | `config`, `tunnel` | Connections from the cloud that could not reach the local service (`remote_host:remote_port`). |
| `connector_tunnel_bytes_received_total` | counter | `config`, `tunnel` | Bytes received from the cloud and passed to the local service. |
| `connector_tunnel_bytes_sent_total` | counter | `config`, `tunnel` | Bytes read from the local service and sent to the cloud. |

Labels:

- `config`: the tunnel's configuration file name without `.ini` (for example `ldap` for
  `configs/ldap.ini`). It is unique per tunnel, so use it to tell tunnels apart.
- `tunnel`: the `tunnel_name` from that file, for display. Two files can share a name.

Prometheus also adds `job` and `instance` to every series when it scrapes.

Example response:

```text
# HELP connector_info Connector instance information.
# TYPE connector_info gauge
connector_info{tunnel_manager_id="my-connector-001",version="1.3.0"} 1
# HELP connector_tunnel_up 1 when the SSH reverse tunnel is established, 0 otherwise.
# TYPE connector_tunnel_up gauge
connector_tunnel_up{config="ldap",tunnel="LDAP prod"} 1
# HELP connector_tunnel_restarts_total Times the tunnel was restarted after going down.
# TYPE connector_tunnel_restarts_total counter
connector_tunnel_restarts_total{config="ldap",tunnel="LDAP prod"} 1
# HELP connector_tunnel_connections_total Connections forwarded to the local service.
# TYPE connector_tunnel_connections_total counter
connector_tunnel_connections_total{config="ldap",tunnel="LDAP prod"} 42
# HELP connector_tunnel_connection_errors_total Failed connection attempts to the local service.
# TYPE connector_tunnel_connection_errors_total counter
connector_tunnel_connection_errors_total{config="ldap",tunnel="LDAP prod"} 0
# HELP connector_tunnel_bytes_received_total Bytes received from the cloud and sent to the local service.
# TYPE connector_tunnel_bytes_received_total counter
connector_tunnel_bytes_received_total{config="ldap",tunnel="LDAP prod"} 1024
# HELP connector_tunnel_bytes_sent_total Bytes read from the local service and sent to the cloud.
# TYPE connector_tunnel_bytes_sent_total counter
connector_tunnel_bytes_sent_total{config="ldap",tunnel="LDAP prod"} 8192
```

## How the values behave

**`tunnel_up`**

- It turns `1` once the SSH server accepts the port forward, and drops to `0` when the tunnel
  stops forwarding.
- A broken tunnel is detected by the keep-alive check, which runs every `keep_alive_time` seconds
  (30 by default). Expect `tunnel_up` to reach `0` up to about 40 seconds after the connection
  is lost.
- While the connector starts, or while a tunnel reconnects, `tunnel_up` is `0` for a few seconds.
  Use `for:` in alerting rules to avoid noise.

**`tunnel_restarts_total`**

- The connector checks its tunnels every 30 seconds and restarts any that went down. Each restart
  adds 1.
- If the SSH server cannot be reached, every failed reconnection attempt counts as a restart. A
  counter that keeps climbing about twice a minute means the tunnel cannot connect at all.

**Connections and bytes**

- A connection is counted when the cloud opens it through the tunnel and the connector reaches the
  local service. If the local service refuses it or times out (2 seconds), the connection counts in
  `connection_errors_total` instead. That is the same event that sends the
  "Failed to Establish connection" email alert.
- Bytes are counted as they flow, not when the connection closes. Long-lived connections, such as
  an LDAP bind that stays open, show traffic right away.

**Counter resets**

- Counters keep their values when a tunnel restarts.
- They go back to 0 when the connector service itself restarts. Prometheus' `rate()` and
  `increase()` handle that automatically; always query counters through them, not by their raw
  value.

**When the whole connector is down**

If the connector service is stopped or the machine is unreachable, there is nothing to scrape and
no `connector_*` series is updated. Alert on Prometheus' own `up` metric for the scrape job (see
[Alerting rules](#alerting-rules)).

## Collecting with Prometheus

Add a scrape job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: invgate-connector
    scrape_interval: 30s
    static_configs:
      - targets: ["connector-host:9998"]
```

Scraping more often than every 15 seconds adds nothing: `tunnel_up` changes on a 30-second cycle.

## Collecting with the OpenTelemetry Collector

The connector does not push OTLP itself. The Collector scrapes `/metrics` with its `prometheus`
receiver (included in the `otelcol-contrib` distribution) and forwards the metrics to any backend:

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: invgate-connector
          scrape_interval: 30s
          static_configs:
            - targets: ["connector-host:9998"]

exporters:
  otlp:
    endpoint: your-backend:4317

service:
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [otlp]
```

Running the Collector on the connector machine lets you keep `metrics_host = 127.0.0.1` and point
the target at `127.0.0.1:9998`.

## Alerting rules

Prometheus rule file with the alerts most installations need:

```yaml
groups:
  - name: invgate-connector
    rules:
      - alert: InvGateConnectorUnreachable
        expr: up{job="invgate-connector"} == 0
        for: 2m
        annotations:
          summary: "Connector {{ $labels.instance }} cannot be scraped (service stopped or host down)"

      - alert: InvGateTunnelDown
        expr: connector_tunnel_up == 0
        for: 2m
        annotations:
          summary: "Tunnel {{ $labels.tunnel }} ({{ $labels.config }}) is down"

      - alert: InvGateTunnelFlapping
        expr: increase(connector_tunnel_restarts_total[1h]) > 3
        annotations:
          summary: "Tunnel {{ $labels.tunnel }} restarted {{ $value }} times in the last hour"

      - alert: InvGateLocalServiceUnreachable
        expr: increase(connector_tunnel_connection_errors_total[5m]) > 0
        annotations:
          summary: "Tunnel {{ $labels.tunnel }} cannot reach its local service"
```

Tune the `for:` durations and thresholds to your environment.

## Useful queries

| Question | PromQL |
|----------|--------|
| Which tunnels are down right now? | `connector_tunnel_up == 0` |
| Restarts in the last 24 hours, per tunnel | `increase(connector_tunnel_restarts_total[24h])` |
| Connections per second, per tunnel | `rate(connector_tunnel_connections_total[5m])` |
| Share of failed connections | `rate(connector_tunnel_connection_errors_total[5m]) / (rate(connector_tunnel_connections_total[5m]) + rate(connector_tunnel_connection_errors_total[5m]))` |
| Traffic from the cloud, bits per second | `rate(connector_tunnel_bytes_received_total[5m]) * 8` |
| Traffic to the cloud, bits per second | `rate(connector_tunnel_bytes_sent_total[5m]) * 8` |
| Connector versions in the fleet | `count by (version) (connector_info)` |

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `curl` gets "connection refused" | Metrics are off or the listener did not start. Check that `metrics_port` is set in `[pytun]`, that the service was restarted, and search `main_connector.log` for `metrics`. |
| Log: `Invalid metrics_prefix ... metrics disabled` | The prefix has characters Prometheus does not allow (for example `-` or a leading digit). Use letters, digits, `_` and `:`. |
| Log: `Couldn't start the metrics server on ...` | The port is in use, the port is not a number, or `metrics_host` is not an address of this machine. Pick another port or fix the host. The tunnels keep running in the meantime. |
| Works locally, not from Prometheus | `metrics_host` is still `127.0.0.1`, or the Windows firewall blocks the port. See [Exposing metrics to another machine](#exposing-metrics-to-another-machine). |
| `404 Not Found` | Only `/metrics` is served on this port. Status, logs and configs stay on the inspection server (port 9999, local only). |
| `tunnel_up` is 0 but the email alert never arrived | Email alerts fire when the connector detects the tunnel process died; `tunnel_up` also reflects a tunnel that is reconnecting. Check `connector_tunnel_restarts_total` and the tunnel's log in `logs/`. |

A problem with the metrics settings never stops the tunnels: the error is logged and the connector
runs without metrics.
