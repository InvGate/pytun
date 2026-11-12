# observation/ - HTTP Inspection Server & Status Tracking

## Files

### http_server.py (137 lines)
`ThreadingHTTPServer` on port 9999 (configurable). Bound to `127.0.0.1` when `inspection_localhost_only=True`.

**Endpoints**:
| Endpoint | Response |
|----------|----------|
| `GET /` | Version, running status, OS (`os`), commercial OS name (`os_name`) and OS architecture (`machine`); `machine` values are listed in the README |
| `GET /status` | Tunnel status, restart counts, MAC address, service connectivity |
| `GET /configs` | **ZIP of all config files — includes SSH keys and passwords** |
| `GET /logs` | ZIP of all log files |

**Security**: `/configs` has no file filtering — it zips everything in the config directory. The only protection is `inspection_localhost_only=True` (default). Never set this to `False` in production.

**Windows path fix** (line 31): strips `\\?\` prefix from paths received via PyInstaller + Shawl. Same logic duplicated in `pytun.py:82` and `pytun.py:118` — candidate for `utils.normalize_windows_path()`.

**Known issue**: HTTP thread restart logic (`pytun.py:188-192`) creates a new thread but may not stop the old `HTTPServer` object before doing so.

### metrics.py
Opt-in Prometheus `/metrics` on its **own** `ThreadingHTTPServer` (`metrics_port`, `metrics_host` default `127.0.0.1`, `metrics_prefix` default `connector_`). It serves only `/metrics` (everything else 404), so it may face the network without exposing `/configs`. Bad settings (prefix, port, host) are logged and disable metrics — they never stop the tunnels.

User guide: [METRICS.md](../METRICS.md).

- `TunnelMetrics`: `multiprocessing.Array` of `up`, `connections`, `connection_errors`, `bytes_received`, `bytes_sent`. Created once per tunnel by `Status.start_tunnel()` in the main process and assigned to every (re)started `TunnelProcess`, so counters survive tunnel restarts. Written by `Tunnel` (handler threads, keepalive) and reset to `up=0` at the top of `TunnelProcess.run()`.
- `render_metrics()`: text format 0.0.4, labels `config` (ini file stem, unique) and `tunnel` (`tunnel_name`). Tests parse it with `prometheus_client` (dev-only dependency).

### status.py
Thread-safe tunnel status store. Uses `RLock` for concurrent reads/writes.

Tracks per tunnel: restart count, last start time, shared `TunnelMetrics`. Also stores MAC address.

Key methods: `start_tunnel(config_file, tunnel_name)` (returns the tunnel's `TunnelMetrics`; call it **before** `start()`), `tunnel_metrics()` (used by `/metrics`), `to_dict()` (used by `/status`).

### connection_check.py (33 lines)
Tests TCP connectivity to `remote_host:remote_port` with a 5-second timeout. Used by the `/status` endpoint to report live service health. Runs checks concurrently via `ThreadPoolExecutor(4)`.
