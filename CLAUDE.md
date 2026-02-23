# CLAUDE.md - pytun Project Guide

> **For AI Assistants**: This document provides comprehensive context about the pytun codebase. Subdirectory CLAUDE.md files contain component-specific details.

## Project Overview

**pytun** is a Python-based SSH reverse tunnel manager for InvGate's connector system. It creates and maintains secure tunnels from cloud servers to on-premises services (LDAP, databases, SMTP) without VPN.

**Flow**: Cloud server ← SSH reverse tunnel ← Local connector → Local service

- **Language**: Python 3.6+ (current: 3.10.11)
- **Core libs**: `paramiko==3.4.0`, `psutil==5.7.2`, `requests==2.32.4`
- **Packaging**: PyInstaller → Windows `.exe`
- **Platform**: Windows (primary), Linux (supported)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    pytun.py (Main Process)                   │
│  • Loads configs  • Spawns TunnelProcess per tunnel          │
│  • Health monitoring (30s loop)  • Auto-restart              │
│  • HTTP inspection server (port 9999)                        │
└────────┬────────────────────────────────────────────────────┘
         │ spawns via multiprocessing
         ▼              ▼              ▼
   TunnelProcess   TunnelProcess   TunnelProcess
      (SSH fwd)      (SSH fwd)      (SSH fwd)
         │ spawns threads per connection
         ▼
   Tunnel.handler() ← bidirectional relay → Local Service
```

**Process model**:
- Main process: orchestrator, monitors all tunnels
- Child processes: each tunnel in isolated `multiprocessing.Process` (no GIL, crash isolation)
- Threads: daemon thread per connection inside each child process

---

## Key Files & Components

### Root
| File | Purpose | Notes |
|------|---------|-------|
| `pytun.py` | Main orchestrator, CLI entry point | `main()`, `start_tunnels()`, `check_tunnels()`, `restart_tunnels()` |
| `device.py` | MAC address-based device authorization | RSA-PSS + SHA256; see [Known Issues](#known-issues--technical-debt) |
| `configure_logger.py` | Centralized logging | Daily rotation, 30-day retention, `LogManager` singleton |
| `utils.py` | Helpers | `get_application_path()` (PyInstaller-aware), `get_network_interfaces()` |
| `version.py` | Semantic version | **MUST update on every change** |

### Subdirectories
- `tunnel_infra/` — SSH tunnel implementation → see [tunnel_infra/CLAUDE.md](tunnel_infra/CLAUDE.md)
- `alerts/` — Email + HTTP POST alerting → see [alerts/CLAUDE.md](alerts/CLAUDE.md)
- `observation/` — HTTP inspection server + status tracking → see [observation/CLAUDE.md](observation/CLAUDE.md)
- `lib/` — `ratelimit.py`: per-argument rate limiting decorator used by email alerts

### Configuration Files

**`connector.ini`** (main config):
```ini
[pytun]
tunnel_manager_id=REQUIRED        # Unique identifier
tunnel_dirs=./configs             # Tunnel config directory
log_level=DEBUG|INFO|WARNING
log_path=./logs
inspection_port=9999
inspection_localhost_only=True    # KEEP TRUE - security critical

# Optional SMTP
smtp_hostname=  smtp_port=587  smtp_security=tls|ssl|none
smtp_login=  smtp_password=  smtp_to=   # ⚠️ plain text

# Optional HTTP POST
http_url=  http_user=  http_password=   # ⚠️ plain text
```

**`configs/*.ini`** (per-tunnel):
```ini
[tunnel]
tunnel_name=        server_host=   server_port=22
port=15000          # Port on SSH server to listen on
remote_host=        remote_port=   # Local service to forward to
username=           keyfile=       # REQUIRED: passwordless SSH key
server_key=         keep_alive_time=30
```

---

## Security Considerations

| Component | Risk | Status |
|-----------|------|--------|
| HTTP `/configs` endpoint | HIGH - exposes SSH keys + passwords | Localhost-only by default ✅ |
| Plain text passwords in config | HIGH | OS file permissions only |
| SSH authentication | LOW | Key-based, host verification, no agent |
| Device authorization | LOW | MAC spoofable - organizational control only |

**Critical rules**:
- **NEVER** set `inspection_localhost_only=False` in production — `/configs` serves SSH keys as ZIP
- SSH uses `RejectPolicy()` (strict host key check) + `look_for_keys=False` + `allow_agent=False`
- Device auth: RSA-PSS validates MAC address signature, but backward compat allows unsigned configs (`device.py:67-70`)

---

## Known Issues & Technical Debt

### High Priority (bugs/correctness)
1. **Global socket timeout** (`pytun.py:321`): `socket.setdefaulttimeout(3)` affects ALL sockets globally — should save/restore
2. **Broad exception handlers**: 25+ instances of `except Exception` — hides real errors, use specific types
3. **Windows `\\?\` path prefix**: duplicated strip logic at `pytun.py:82`, `pytun.py:118`, `observation/http_server.py:31` — needs `utils.normalize_windows_path()`
4. **`select()` no timeout** (`tunnel_infra/Tunnel.py:82`): infinite wait possible — add 60s timeout

### Medium Priority
5. **Race condition** (`alerts/pooled_alerter.py:29`): `future.exception()` called before future completes
6. **Resource leak** (`pytun.py:340`): test processes created in `test_connections()` never terminated
7. **MAC backward compat** (`device.py:67-70`): `if not self._mac_address_signature: return True` — remove in v2.0.0
8. **Outdated deps**: `psutil==5.7.2` → 6.0+, `coloredlogs==14.0` → 15.0+

### No Automated Tests
Zero unit tests exist. Manual tests only: `python pytun.py --test_all`. See [Testing](#testing).

---

## Development Guidelines

**Before every change**:
- [ ] Update `version.py` (semantic versioning: MAJOR.MINOR.PATCH)
- [ ] Test on Windows (primary platform)
- [ ] No new `except Exception` broad handlers
- [ ] No secrets/credentials in code
- [ ] Update relevant CLAUDE.md if adding components

**Code style**: Follow existing patterns. Add type hints where possible but maintain 3.6+ compatibility (`Union[str, int]` not `str | int`).

**Path resolution**: Main config paths are relative to the application directory; tunnel config paths (e.g. `keyfile`) are relative to the config file's directory.

---

## Testing

No automated tests. Manual CLI tests:
```bash
python pytun.py --test_all          # Full diagnostic
python pytun.py --test_connections  # Service connectivity
python pytun.py --test_tunnels      # SSH establishment
python pytun.py --test_smtp         # Email alerts
python pytun.py --test_http         # HTTP POST alerts
```

Pre-release checklist:
1. Build with PyInstaller: `pyinstaller pytun.spec`
2. Install on Windows, run test shortcut
3. Check `127.0.0.1:9999/` and `/status`
4. Kill tunnel PID from logs (`taskkill /F /PID <pid>`), verify auto-restart

---

## Build & Deployment

```bash
# Dev
pip install -r requirements.txt && python pytun.py

# Production (Windows .exe)
pyinstaller pytun.spec  # → dist/pytun.exe

# Windows service
shawl add --name InvGateTunnel -- pytun.exe --config_ini connector.ini
sc start InvGateTunnel
```

**PyInstaller key settings** (`pytun.spec`): `uac_admin=True`, bundles `mac_address_pub_key`, `runtime_tmpdir='./tmp'`

### mac_address_pub_key — lifecycle and staging/production split

The file `mac_address_pub_key` (RSA public key, PEM format) is bundled into the `.exe` at build time via `pytun.spec`:
```python
datas=[('mac_address_pub_key', '.')]
```
At runtime PyInstaller extracts it to `sys._MEIPASS` (`./tmp/_MEIxxx/`). In dev mode it is read from the current working directory (`get_bundle_path()` → `os.path.abspath(".")`).

> **CRITICAL — staging and production use different keys**
>
> The staging build (`ci.invgate.com/job/pytun-build-staging/`) and the production build (`ci.invgate.com/job/pytun-build/`) each embed a **different** `mac_address_pub_key`. Connector configs are signed with the private key matching their environment:
>
> | Config source | Required .exe build |
> |---|---|
> | Staging backend | Staging `.exe` |
> | Production backend | Production `.exe` |
>
> If you test a staging config with a production `.exe` (or vice versa), `is_mac_address_signature_valid()` will silently return `False` — the connector will appear unauthorized with no clear error in the logs, because all verification failures are swallowed by a bare `except` block (`device.py`, `is_mac_address_signature_valid`). **Always use the matching build when testing.**

**Jenkins**: `ci.invgate.com/job/pytun-build/` (prod), `ci.invgate.com/job/pytun-build-staging/` (staging)

**Release**: Update `version.py` → commit → Jenkins build → test installer → publish to `download.invgate.net`

---

## Monitoring

```bash
tail -f logs/main_connector.log     # Main process log
curl http://localhost:9999/status   # Tunnel status, restart counts, service health
curl http://localhost:9999/configs  # ⚠️ ZIP with SSH keys — localhost only!
curl http://localhost:9999/logs     # ZIP with all log files
```

---

**Maintainer**: InvGate - Internal Tools Team
