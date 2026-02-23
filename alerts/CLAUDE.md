# alerts/ - Alert System

## Files

### alert_sender.py (10 lines)
Abstract base class. All alert implementations must implement:
```python
send_alert(tunnel_name, message=None, exception_on_failure=False)
```

### email_alert.py (83 lines)
SMTP alerting. Supports `smtp_security`: `"none"`, `"tls"`, `"ssl"`.

**Rate limiting**: `@ratelimit_by_args(calls=1, period=600)` — each unique `tunnel_name` gets its own independent 10-minute window (from `lib/ratelimit.py`). If 10 tunnels fail simultaneously, 10 separate emails are sent — this is intentional per-tunnel behavior.

Passwords are stored in plain text in `connector.ini`. No encryption at rest.

### http_post_alert.py (32 lines)
HTTP POST webhook. Payload: `{tunnel_name, message, tunnel_manager_id}`. Basic auth.

**No rate limiting** (unlike email alerts). High-frequency tunnel failures can flood the endpoint.

### pooled_alerter.py (37 lines)
Dispatches alerts via `ThreadPoolExecutor` (default 1 worker). Non-blocking — submits and returns immediately.

**Known bug** (line 29): `future.exception()` is called immediately after `pool.submit()` without checking `future.done()` first. The future may not be complete yet, causing the exception check to block or return prematurely.

---

## lib/ratelimit.py (45 lines)
Per-argument rate limiting decorator. Creates a separate `RateLimiter` instance per unique combination of decorated function arguments, enabling independent windows per tunnel.

---

## Pitfall: Alert configuration is optional but silently absent
If `smtp_hostname` / `http_url` are missing from `connector.ini`, alerts are simply not configured — no warning is logged. Tunnel failures will go unnotified without any indication in the startup logs.
