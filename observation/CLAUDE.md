# observation/ - HTTP Inspection Server & Status Tracking

## Files

### http_server.py (137 lines)
`ThreadingHTTPServer` on port 9999 (configurable). Bound to `127.0.0.1` when `inspection_localhost_only=True`.

**Endpoints**:
| Endpoint | Response |
|----------|----------|
| `GET /` | Version + running status |
| `GET /status` | Tunnel status, restart counts, MAC address, service connectivity |
| `GET /configs` | **ZIP of all config files — includes SSH keys and passwords** |
| `GET /logs` | ZIP of all log files |

**Security**: `/configs` has no file filtering — it zips everything in the config directory. The only protection is `inspection_localhost_only=True` (default). Never set this to `False` in production.

**Windows path fix** (line 31): strips `\\?\` prefix from paths received via PyInstaller + Shawl. Same logic duplicated in `pytun.py:82` and `pytun.py:118` — candidate for `utils.normalize_windows_path()`.

**Known issue**: HTTP thread restart logic (`pytun.py:188-192`) creates a new thread but may not stop the old `HTTPServer` object before doing so.

### status.py (39 lines)
Thread-safe tunnel status store. Uses `RLock` for concurrent reads/writes.

Tracks per tunnel: restart count, last start time. Also stores MAC address.

Key methods: `start_tunnel(name)`, `to_dict()` (used by `/status` endpoint).

### connection_check.py (33 lines)
Tests TCP connectivity to `remote_host:remote_port` with a 5-second timeout. Used by the `/status` endpoint to report live service health. Runs checks concurrently via `ThreadPoolExecutor(4)`.
