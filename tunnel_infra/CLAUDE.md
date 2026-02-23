# tunnel_infra/ - SSH Tunnel Infrastructure

## Files

### TunnelProcess.py (201 lines)
Multiprocessing wrapper — extends `multiprocessing.Process`. One process per tunnel config.

| Method | Lines | Purpose |
|--------|-------|---------|
| `exit_gracefully()` | 95-100 | SIGTERM/SIGINT handler |
| `run()` | 102-135 | Process entry point |
| `ssh_connect()` | 137-166 | Establishes SSH connection |
| `from_config_file()` | 168-201 | Factory: creates instance from `.ini` file |

**SSH connection** (security-critical):
```python
client.set_missing_host_key_policy(paramiko.RejectPolicy())  # Strict - prevents MITM
client.connect(key_filename=keyfile, look_for_keys=False, allow_agent=False)
```

**Config parsing**: `keyfile` and `server_key` paths are resolved relative to the config file's directory (not the app directory). Missing `keyfile` raises at tunnel start, not at config parse time.

### Tunnel.py (164 lines)
SSH reverse port forwarding implementation. Runs inside a `TunnelProcess`.

| Method | Lines | Purpose |
|--------|-------|---------|
| `handler()` | 49-98 | Bidirectional data relay (spawns daemon thread per connection) |
| `validate_tunnel_up()` | 100-125 | Three-pronged keepalive check |
| `reverse_forward_tunnel()` | 127-150 | Main forwarding loop — calls `transport.request_port_forward()` |
| `stop()` | 152-164 | Graceful shutdown |

**Known issue**: `select()` at line 82 has no timeout — can block indefinitely if both ends go silent without closing:
```python
r, w, x = select.select([sock, chan], [], [])  # TODO: add timeout=60
```

### pathtype.py (20 lines)
Custom `argparse` type for CLI path validation. Validates file/directory existence before the app starts.

---

## Pitfalls

**Passwordless SSH keys required**: Paramiko raises `PasswordRequiredException` if the key has a passphrase. Always generate with `-N ""`.

**Process vs thread model**:
- `pytun.py` spawns `TunnelProcess` (multiprocessing) — isolated memory, survives individual crashes
- `Tunnel.py` spawns handler threads (threading) — shared memory within the process, daemon threads die with the process

**Config validation is late**: Required fields (`server_host`, `remote_host`, `username`, `keyfile`) are validated when the tunnel starts, not when configs are parsed. The main process can start successfully and only fail when individual tunnels attempt to connect.

**Common SSH errors**:
```
PasswordRequiredException  → key has passphrase
BadHostKeyException        → server_key mismatch, update known_hosts
AuthenticationException    → SSH key not in authorized_keys
ConnectionRefusedError     → server unreachable
```
