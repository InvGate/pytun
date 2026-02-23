# CLAUDE.md - pytun Project Guide

> **For AI Assistants**: This document provides comprehensive context about the pytun codebase to help you work effectively on this project.

## Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Key Files & Components](#key-files--components)
4. [Setup & Development](#setup--development)
5. [Testing Strategy](#testing-strategy)
6. [Common Pitfalls](#common-pitfalls)
7. [Technical Debt](#technical-debt)
8. [Security Considerations](#security-considerations)
9. [Build & Deployment](#build--deployment)
10. [Troubleshooting](#troubleshooting)

---

## Project Overview

**pytun** is a Python-based SSH reverse tunnel manager that creates and maintains secure tunnels from cloud servers to local network services. It's designed for InvGate's connector system to allow on-premises services (LDAP, databases, SMTP) to be accessed from cloud instances.

### What Problem Does It Solve?

Allows cloud services to access on-premises resources without VPN by using **SSH reverse port forwarding**:
- Cloud server ← SSH tunnel ← Local connector → Local service
- Automatic health monitoring and restart
- Multi-tunnel support with alerting

### Tech Stack

- **Language**: Python 3.6+ (current: 3.10.11)
- **Core Libraries**:
  - `paramiko==3.4.0` - SSH implementation
  - `psutil==5.7.2` - Process management
  - `requests==2.32.4` - HTTP alerts
- **Packaging**: PyInstaller (Windows .exe)
- **Platform**: Windows (primary), Linux (supported)

---

## Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    pytun.py (Main Process)                   │
│  • Loads configs                                             │
│  • Spawns TunnelProcess for each tunnel                      │
│  • Health monitoring (30-second loop)                        │
│  • Auto-restart failed tunnels                               │
│  • HTTP inspection server (port 9999)                        │
└────────┬────────────────────────────────────────────────────┘
         │ spawns via multiprocessing
         ├──────────────┬──────────────┬──────────────┐
         ▼              ▼              ▼              ▼
   ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
   │TunnelP 1│    │TunnelP 2│    │TunnelP 3│    │TunnelP N│
   └────┬────┘    └────┬────┘    └────┬────┘    └────┬────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
   Tunnel obj    Tunnel obj    Tunnel obj    Tunnel obj
   (SSH fwd)     (SSH fwd)     (SSH fwd)     (SSH fwd)
```

### Data Flow (Per Connection)

```
External Client
    ↓
SSH Server (cloud) - port_to_forward
    ↓ (SSH reverse tunnel)
Tunnel.accept() - paramiko transport
    ↓ (spawn handler thread)
Tunnel.handler() - bidirectional relay
    ↓ (socket.connect)
Local Service (e.g., LDAP on 192.168.1.100:389)
    ↓ (select() multiplexing)
Data flows both directions
```

### Process Model

- **Main Process**: `pytun.py` - orchestrator, monitors all tunnels
- **Child Processes**: Each tunnel runs in isolated `multiprocessing.Process`
  - Prevents one tunnel crash from affecting others
  - True parallelism (no GIL issues)
  - Clean resource boundaries
- **Threads**: Per-connection handler threads (daemon threads)

### Key Design Patterns

1. **Process Isolation**: `TunnelProcess` extends `multiprocessing.Process`
2. **Thread-per-Connection**: Each client connection gets a daemon handler thread
3. **Keepalive Monitoring**: Three-pronged health check every 30 seconds
4. **Automatic Restart**: Parent detects dead processes and respawns
5. **Pooled Alerting**: Non-blocking alerts via `ThreadPoolExecutor`

---

## Key Files & Components

### Core Entry Point

**`pytun.py`** (503 lines)
- **Purpose**: Main orchestrator and CLI entry point
- **Key Functions**:
  - `main()` - Initialization, config loading, main event loop
  - `start_tunnels()` - Spawns all tunnel processes
  - `check_tunnels()` - Detects dead processes (every 30s)
  - `restart_tunnels()` - Respawns failed tunnels
  - `test_*()` - CLI test modes (--test_all, --test_smtp, etc.)
- **Responsibilities**: Process lifecycle, signal handling, HTTP server management
- **⚠️ Pitfall**: Broad exception handlers at lines 258, 310, 440 - hide real errors

### Tunnel Infrastructure (`tunnel_infra/`)

**`TunnelProcess.py`** (201 lines)
- **Purpose**: Multiprocessing wrapper for tunnels
- **Extends**: `multiprocessing.Process`
- **Key Methods**:
  - `run()` - Process entry point (lines 102-135)
  - `ssh_connect()` - Establishes SSH connection (lines 137-166)
  - `from_config_file()` - Factory method to create from .ini (lines 168-201)
  - `exit_gracefully()` - SIGTERM/SIGINT handler (lines 95-100)
- **⚠️ Critical**: Uses `RejectPolicy()` for host key verification - prevents MITM

**`Tunnel.py`** (164 lines)
- **Purpose**: SSH reverse port forwarding implementation
- **Key Methods**:
  - `reverse_forward_tunnel()` - Main forwarding loop (lines 127-150)
  - `handler()` - Bidirectional data relay (lines 49-98)
  - `validate_tunnel_up()` - Three-pronged keepalive (lines 100-125)
  - `stop()` - Graceful shutdown (lines 152-164)
- **⚠️ Critical**: `select()` at line 82 has NO timeout - infinite wait possible
- **Threading Model**: Spawns daemon thread per connection

**`pathtype.py`** (20 lines)
- **Purpose**: Custom argparse type for path validation
- **Use**: Validates file/directory existence in CLI args

### Alert System (`alerts/`)

**`alert_sender.py`** (10 lines)
- **Purpose**: Abstract base class for all alert implementations
- **Interface**: `send_alert(tunnel_name, message=None, exception_on_failure=False)`

**`email_alert.py`** (83 lines)
- **Purpose**: SMTP email alerting with TLS/SSL support
- **Rate Limiting**: 600 seconds (10 minutes) per tunnel - see line 9
- **Decorator**: `@ratelimit_by_args` ensures per-tunnel rate limiting
- **Security Types**: "none", "tls", "ssl"
- **⚠️ Note**: Plain text passwords in config

**`http_post_alert.py`** (32 lines)
- **Purpose**: HTTP POST webhook alerts
- **Auth**: Basic authentication
- **Payload**: JSON with `{tunnel_name, message, tunnel_manager_id}`
- **⚠️ Note**: No rate limiting (unlike email alerts)

**`pooled_alerter.py`** (37 lines)
- **Purpose**: Multi-threaded alert dispatcher
- **Pool**: `ThreadPoolExecutor` (default 1 worker)
- **Behavior**: Non-blocking - submits and returns immediately
- **⚠️ Bug**: Race condition at line 29 - `future.exception()` may not be ready

### Monitoring System (`observation/`)

**`http_server.py`** (137 lines)
- **Purpose**: HTTP inspection/status server (default port 9999)
- **Endpoints**:
  - `GET /` - Health check, returns version and status
  - `GET /status` - Tunnel status, MAC, restart counts, service connectivity
  - `GET /configs` - ZIP download of all config files (⚠️ includes SSH keys!)
  - `GET /logs` - ZIP download of all log files
- **⚠️ Security Risk**: `/configs` exposes SSH keys and credentials - localhost-only by default
- **Threading**: `ThreadingHTTPServer` for concurrent requests

**`status.py`** (39 lines)
- **Purpose**: Thread-safe tunnel status tracking
- **Thread Safety**: Uses `RLock` for concurrent access
- **Tracks**: Restart counts, last start time, MAC address
- **Methods**: `start_tunnel()`, `to_dict()`

**`connection_check.py`** (33 lines)
- **Purpose**: Service connectivity testing
- **Method**: Socket connection with 5-second timeout
- **Integration**: Used by `/status` endpoint to check service health
- **Parallelism**: `ThreadPoolExecutor(4)` for concurrent checks

### Device Authorization

**`device.py`** (106 lines)
- **Purpose**: MAC address-based device authorization
- **Crypto**: RSA-PSS signature validation with SHA256
- **Public Key**: `mac_address_pub_key` file in root directory
- **⚠️ TO BE REMOVED**: Lines 67-70 - backward compat for unsigned configs
- **⚠️ Limitation**: MAC addresses can be spoofed (acknowledged in comments)
- **Process**:
  1. Extract MAC from config (base64-encoded JSON)
  2. Verify MAC exists on network interface
  3. Validate RSA-PSS signature

### Utilities

**`configure_logger.py`** (40 lines)
- **Purpose**: Centralized logging configuration
- **Rotation**: Daily at midnight, 30-day retention
- **Handler**: `TimedRotatingFileHandler`
- **Class**: `LogManager` - singleton pattern

**`utils.py`** (60 lines)
- **Purpose**: Helper functions
- **Key Functions**:
  - `get_application_path()` - PyInstaller-aware path resolution
  - `cleanup_pyinstaller_temp()` - Windows temp cleanup (lines 38-40)
  - `get_network_interfaces()` - MAC address enumeration via psutil

**`version.py`** (3 lines)
- **Purpose**: Semantic versioning
- **Current**: `__version__ = "1.1.17"`
- **⚠️ Important**: MUST update on every change (semantic versioning)

### Library (`lib/`)

**`ratelimit.py`** (45 lines)
- **Purpose**: Per-argument rate limiting decorator
- **Pattern**: Creates separate rate limiter per unique argument combination
- **Why**: Allows each tunnel to have independent rate limit window
- **Used By**: `email_alert.py`

### Configuration Files

**`connector.ini`** (main config)
```ini
[pytun]
tunnel_manager_id=REQUIRED        # Unique identifier
tunnel_dirs=./configs             # Tunnel config directory
log_level=DEBUG|INFO|WARNING      # Logging verbosity
log_path=./logs                   # Log directory
inspection_port=9999              # HTTP server port
inspection_localhost_only=True    # Security: localhost only

# Optional: SMTP Alerts
smtp_hostname=smtp.example.com
smtp_port=587
smtp_security=tls                 # none|tls|ssl
smtp_login=user@example.com
smtp_password=plain_text_password # ⚠️ Plain text!
smtp_to=admin@example.com

# Optional: HTTP Alerts
http_url=https://api.example.com/alert
http_user=username
http_password=plain_text_password # ⚠️ Plain text!
```

**`configs/*.ini`** (per-tunnel configs)
```ini
[tunnel]
tunnel_name=My Service Tunnel
server_host=cloud-jump.example.com  # REQUIRED: SSH bastion
server_port=22
port=15000                          # Port on SSH server to listen
remote_host=192.168.1.100           # REQUIRED: Local service
remote_port=389
username=ssh_user                   # REQUIRED
keyfile=/path/to/private/key        # REQUIRED: SSH private key
server_key=known_hosts              # Server public key
keep_alive_time=30                  # Seconds between health checks
```

**`pytun.spec`** (PyInstaller config)
- **Purpose**: Build configuration for Windows executable
- **Key Settings**:
  - `uac_admin=True` - Requires admin elevation
  - `console=True` - Console application
  - `datas=[('mac_address_pub_key', '.')]` - Bundle public key
  - `upx=True` - UPX compression

---

## Setup & Development

### Prerequisites

- Python 3.6+ (recommended: 3.10+)
- pip
- SSH access to a jump host/bastion server
- Passwordless SSH key pair

### Quick Start

```bash
# 1. Clone repository
git clone https://github.com/InvGate/pytun.git
cd pytun

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure (see Configuration section below)
cp connector.ini.example connector.ini
# Edit connector.ini with your settings

# 5. Create tunnel configs
mkdir -p configs
# Create configs/tunnel.ini (see template below)

# 6. Run
python pytun.py
```

### Configuration Setup

**Step 1: Main Configuration**
```bash
# Create connector.ini
cat > connector.ini << 'EOF'
[pytun]
tunnel_manager_id=dev-connector-001
tunnel_dirs=./configs
log_level=DEBUG
log_to_console=True
log_path=./logs
inspection_port=9999
inspection_localhost_only=True
EOF
```

**Step 2: Tunnel Configuration**
```bash
# Create configs/example-tunnel.ini
cat > configs/example-tunnel.ini << 'EOF'
[tunnel]
tunnel_name=Example Tunnel
server_host=your-jump-host.example.com
server_port=22
port=15000
remote_host=localhost
remote_port=8080
username=tunnel-user
keyfile=~/.ssh/pytun_key
server_key=~/.ssh/known_hosts
keep_alive_time=30
log_level=DEBUG
log_to_console=True
EOF
```

**Step 3: SSH Keys**
```bash
# Generate passwordless SSH key
ssh-keygen -t rsa -b 4096 -f ~/.ssh/pytun_key -N ""

# Copy public key to jump host
ssh-copy-id -i ~/.ssh/pytun_key.pub tunnel-user@your-jump-host.example.com

# Extract server's public key (for server_key config)
ssh-keyscan your-jump-host.example.com >> ~/.ssh/known_hosts
```

**Step 4: Device Authorization (Optional)**
```bash
# If you have a mac_address_pub_key file, place it in root
# Otherwise, the connector will run without MAC validation
# (backward compatibility - see device.py:67-70)
```

### Running Locally

```bash
# Standard run
python pytun.py

# With custom config
python pytun.py --config_ini /path/to/connector.ini

# Test connectivity
python pytun.py --test_connections

# Test SSH tunnels
python pytun.py --test_tunnels

# Full diagnostic
python pytun.py --test_all

# Check version
python pytun.py --version
```

### Monitoring

```bash
# View logs (while running)
tail -f logs/main_connector.log
tail -f logs/your-tunnel-name.log

# Check status via HTTP
curl http://localhost:9999/
curl http://localhost:9999/status

# Download configs
curl http://localhost:9999/configs -o configs.zip

# Download logs
curl http://localhost:9999/logs -o logs.zip
```

---

## Testing Strategy

### ⚠️ CRITICAL: No Automated Tests

**Current State**: Zero unit tests, no pytest/unittest framework

**What Exists**: CLI-based manual tests only
```bash
python pytun.py --test_smtp          # Test email alerts
python pytun.py --test_http          # Test HTTP POST alerts
python pytun.py --test_connections   # Test service connectivity
python pytun.py --test_tunnels       # Test SSH connection
python pytun.py --test_all           # Run all tests
```

### Manual Testing Process (from docs)

Before releasing a new version:
1. Install connector on Windows machine
2. Run test script (shortcut created by installer)
3. Check HTTP endpoints: `127.0.0.1:9999/info` and `/status`
4. Test auto-restart: Find tunnel PIDs in `main_connector.log`, kill them with `taskkill /F /PID <pid>`, verify restart

### Test Alert System Manually

```bash
# Test HTTP alerts
# 1. Start local server
python -m http.server 8000

# 2. Configure connector.ini
[pytun]
signature=invalid_to_trigger_failure
http_url=http://127.0.0.1:8000
http_user=test
http_password=test

# 3. Run connector - should POST to localhost:8000
```

### Coverage Gaps (NEEDS TESTS)

| Component | Coverage | Priority |
|-----------|----------|----------|
| SSH reverse port forwarding | 0% | CRITICAL |
| Bidirectional data relay | 0% | CRITICAL |
| Keepalive validation | 0% | HIGH |
| MAC signature validation | 0% | HIGH |
| Alert rate limiting | 0% | HIGH |
| Thread safety (Status, timers) | 0% | HIGH |
| Config parsing edge cases | 0% | MEDIUM |
| HTTP endpoint responses | 0% | MEDIUM |

### Recommended Test Structure (TODO)

```
tests/
├── unit/
│   ├── test_device_authorization.py
│   ├── test_config_parsing.py
│   ├── test_alert_rate_limiting.py
│   └── test_path_normalization.py
├── integration/
│   ├── test_ssh_tunnel.py
│   ├── test_data_forwarding.py
│   └── test_keepalive.py
└── functional/
    ├── test_tunnel_restart.py
    └── test_http_endpoints.py
```

---

## Common Pitfalls

### 1. Broad Exception Handlers Hide Errors

**Problem**: Code catches generic `Exception` everywhere
```python
# pytun.py:258 - DON'T DO THIS
try:
    tunnel_process = TunnelProcess.from_config_file(config_file, [])
except Exception as e:  # Too broad!
    logger.exception(...)
    continue
```

**Impact**: Loses critical diagnostic information when tunnels fail

**Solution**: Catch specific exceptions
```python
try:
    tunnel_process = TunnelProcess.from_config_file(config_file, [])
except (FileNotFoundError, KeyError, ValueError) as e:
    logger.exception("Config parse failed: %s", e)
    continue
except paramiko.SSHException as e:
    logger.exception("SSH setup failed: %s", e)
    continue
```

### 2. Global Socket Timeout Side Effect

**Problem**: `pytun.py:321` sets global socket timeout
```python
socket.setdefaulttimeout(3)  # Affects ALL sockets globally!
```

**Impact**: All future socket operations timeout at 3 seconds

**Solution**: Use context manager or restore original
```python
original_timeout = socket.getdefaulttimeout()
try:
    socket.setdefaulttimeout(3)
    # ... test code ...
finally:
    socket.setdefaulttimeout(original_timeout)
```

### 3. Passwordless SSH Keys Required

**Problem**: Paramiko doesn't support password-protected keys easily
```python
# TunnelProcess.py:158 - key_filename
client.connect(key_filename=self.key_file, ...)
# If key has password: PasswordRequiredException
```

**Solution**: Always use passwordless keys
```bash
ssh-keygen -t rsa -b 4096 -f pytun_key -N ""  # -N "" = no passphrase
```

### 4. Relative Paths Resolved Differently

**Problem**: Main config vs tunnel config path resolution
```python
# Main config: relative to application directory
log_path = params.get("log_path", './logs')

# Tunnel config: relative to config file directory
if not isabs(key_file):
    key_file = join(directory, key_file)  # directory = dirname(ini_file)
```

**Solution**: Always verify which directory paths are relative to

### 5. Windows Path Issues with PyInstaller

**Problem**: Windows adds `\\?\` prefix when using PyInstaller + Shawl
```python
# Appears in 3 places - duplicated code!
if log_path.startswith("\\\\?\\"):
    log_path = log_path.replace("\\\\?\\", "")
```

**Solution**: Use centralized path normalization utility (TODO: create this)

### 6. HTTP Inspection Server Exposes Credentials

**Problem**: `/configs` endpoint returns ALL config files including SSH keys
```python
# observation/http_server.py:67-82
def handle_configs(self):
    zipf = zipfile.ZipFile(...)
    self._zipdir(config_path, zipf)  # No filtering!
```

**Mitigation**: `inspection_localhost_only=True` by default (only accessible from 127.0.0.1)

**⚠️ Warning**: Don't set `inspection_localhost_only=False` in production

### 7. Process vs Thread Confusion

**Problem**: Tunnels run in processes, handlers in threads
```python
# pytun.py creates processes
tunnel_process.start()  # multiprocessing.Process

# Tunnel.py creates threads
thr = threading.Thread(target=self.handler, daemon=True)
thr.start()
```

**Impact**: Different isolation guarantees, different debugging approaches

**Remember**:
- Processes = isolated memory, survives crashes, GIL-free
- Threads = shared memory, daemon threads die with parent

### 8. Configuration Validation Happens Late

**Problem**: Missing required fields discovered at runtime
```python
# TunnelProcess.py:175
key_file = defaults.get('keyfile')  # May be None!
if key_file is None:
    raise Exception("Missing keyfile argument")  # Fails at tunnel start
```

**Impact**: Connector starts, then fails when trying to establish tunnels

**Solution**: Validate all configs before starting any tunnels (TODO)

### 9. Rate Limiting is Per-Sender-Per-Tunnel

**Behavior**: Each `(tunnel_name, sender)` pair has separate 10-minute window
```python
# alerts/email_alert.py:9
@ratelimit_by_args(calls=1, period=600)  # 600s = 10 min
def send_alert(self, tunnel_name, message=None):
    # Each tunnel_name gets its own rate limit
```

**Impact**: If 10 tunnels fail, you'll get 10 emails (one per tunnel)

**Expectation**: This is correct behavior - per-tunnel alerting

### 10. Logs Rotate Daily, 30-Day Retention

**Configuration**: `configure_logger.py:25`
```python
backupCount=30  # Keep 30 files
when="midnight"  # Rotate at midnight
```

**Impact**: Logs older than 30 days are deleted automatically

**Action**: Archive logs externally if needed for compliance

---

## Technical Debt

### High Priority

#### 1. Remove MAC Address Backward Compatibility
**File**: `device.py:67-70`
```python
# TO BE REMOVED:
# Authorize connectors without the MAC address config key
if not self._mac_address_signature:
    return True  # Allows running without MAC validation
```
**Action**: Remove in next major version (2.0.0)

#### 2. Replace Broad Exception Handlers
**Locations**: 25+ instances across codebase
```python
# Common pattern:
except Exception as e:
    logger.exception(...)
```
**Action**: Replace with specific exception types
**Priority**: HIGH - impacts debugging and error recovery

#### 3. Extract Windows Path Normalization
**Duplication**: 3 instances
```python
# pytun.py:82, pytun.py:118, observation/http_server.py:31
if path.startswith("\\\\?\\"):
    path = path.replace("\\\\?\\", "")
```
**Action**: Create `utils.normalize_windows_path(path)` utility
**Priority**: HIGH - reduces duplication, centralized fix

#### 4. Fix Global Socket Timeout
**File**: `pytun.py:321`
```python
socket.setdefaulttimeout(3)  # GLOBAL - affects all sockets!
```
**Action**: Use context manager or restore original timeout
**Priority**: HIGH - side effect bug

#### 5. Add Timeout to select()
**File**: `tunnel_infra/Tunnel.py:82`
```python
r, w, x = select.select([sock, chan], [], [])  # No timeout!
```
**Action**: Add timeout parameter (e.g., 60 seconds)
**Priority**: MEDIUM - could cause infinite wait

### Medium Priority

#### 6. Upgrade Outdated Dependencies
```
psutil==5.7.2       → 6.0.0+ (latest)
coloredlogs==14.0   → 15.0+ (latest)
```
**Action**: Test compatibility and upgrade
**Priority**: MEDIUM - security and bug fixes

#### 7. Fix Race Condition in Pooled Alerter
**File**: `alerts/pooled_alerter.py:29`
```python
future = self.pool.submit(...)
error = future.exception()  # May not be ready!
```
**Action**: Check `future.done()` before accessing `exception()`
**Priority**: MEDIUM - rare edge case

#### 8. Resource Leak in test_connections()
**File**: `pytun.py:340`
```python
create_tunnels_from_config([], files, logger, processes)
# Processes created but never terminated!
```
**Action**: Add cleanup/termination of test processes
**Priority**: MEDIUM - only affects test mode

#### 9. HTTP Thread Restart Logic
**File**: `pytun.py:188-192`
```python
if not http_inspection_thread.is_alive():
    http_inspection_thread.join()
    http_inspection_thread = threading.Thread(...)
    # Old http_inspection object still running?
```
**Action**: Verify old server is stopped before restarting
**Priority**: LOW - rare scenario

### Low Priority

#### 10. Improve Error Messages
**Pattern**: Generic messages without context
```python
logger.exception("Error processing HTTP Request %s: %s" % (self.path, e))
```
**Action**: Add contextual information to error messages
**Priority**: LOW - improves debugging experience

#### 11. Type Hints Compatibility
**Issue**: Uses Python 3.10+ union syntax (`str | int`)
```python
log_level: str | int  # Requires Python 3.10+
```
**Status**: README claims Python 3.6+ support
**Action**: Either require 3.10+ or use `Union[str, int]`
**Priority**: LOW - documentation vs implementation mismatch

#### 12. Add Configuration Validation
**Issue**: Required fields validated at tunnel start, not at parse time
**Action**: Add upfront validation in `pytun.py:main()`
**Priority**: LOW - fail-fast improvement

---

## Security Considerations

### Authentication & Authorization

#### SSH Security (GOOD ✅)
```python
# TunnelProcess.py:145
client.set_missing_host_key_policy(paramiko.RejectPolicy())  # Strict!
client.connect(
    key_filename=keyfile,      # Key-based only
    look_for_keys=False,       # No auto-discovery
    allow_agent=False,         # No SSH agent
)
```
**Strengths**:
- Key-based authentication only (no passwords)
- Host key verification (prevents MITM)
- SSH agent disabled (controlled authentication)

#### Device Authorization (MEDIUM ⚠️)
```python
# device.py - RSA-PSS signature validation
pubkey.verify(
    signature,
    mac_address.encode("utf8"),
    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=MAX),
    hashes.SHA256()
)
```
**Strengths**:
- Proper cryptography (RSA-PSS with SHA256)
- Maximum salt length

**Limitations** (acknowledged in code):
- MAC addresses can be spoofed
- Backward compatibility allows unsigned configs (device.py:67-70)
- Purpose: Organizational control, not anti-malicious

### Credential Storage (BAD ⚠️)

**Plain Text in Config Files**:
```ini
smtp_password=plain_text_password  # No encryption!
http_password=plain_text_password  # No encryption!
```

**SSH Keys**:
```ini
keyfile=/path/to/private/key  # Path only, not embedded
```

**Recommendations**:
1. Encrypt config files at rest
2. Use OS keychain/credential manager
3. Support environment variables for sensitive values
4. Restrict config file permissions (chmod 600)

### Exposed Services

#### HTTP Inspection Server (CRITICAL ⚠️)

**Default Configuration** (SAFE):
```ini
inspection_localhost_only=True  # 127.0.0.1 only
inspection_port=9999
```

**⚠️ DANGER - If Set to False**:
```python
# observation/http_server.py:67-82
GET /configs  # Returns ZIP with SSH keys, passwords, ALL configs!
GET /logs     # Returns all log files (may contain sensitive data)
```

**Security Rules**:
1. **NEVER** set `inspection_localhost_only=False` in production
2. If remote access needed, use SSH tunnel to localhost:9999
3. Add authentication to HTTP endpoints (TODO)
4. Filter `/configs` to exclude sensitive files (TODO)

### Attack Surface Summary

| Component | Risk | Mitigation |
|-----------|------|-----------|
| HTTP `/configs` endpoint | **HIGH** | Localhost-only by default |
| Plain text passwords in config | **HIGH** | File permissions (OS-level) |
| SSH tunnel authentication | LOW | Key-based, host verification |
| Device authorization | LOW | Acknowledged limitation |
| Log file exposure | MEDIUM | Localhost-only HTTP server |

### Security Best Practices

1. **Config File Permissions**:
   ```bash
   chmod 600 connector.ini
   chmod 600 configs/*.ini
   chmod 400 ~/.ssh/pytun_key
   ```

2. **SSH Key Management**:
   - Use passwordless keys (required)
   - Rotate keys periodically
   - One key per connector (don't reuse)

3. **HTTP Inspection Server**:
   - Keep `inspection_localhost_only=True`
   - Use SSH tunnel for remote access: `ssh -L 9999:localhost:9999 user@connector-host`

4. **Logging**:
   - Review `log_level` - DEBUG may log sensitive data
   - Secure log file permissions
   - Rotate and archive logs securely

5. **Alert Configuration**:
   - Use TLS/SSL for SMTP (`smtp_security=tls`)
   - Use HTTPS for HTTP POST alerts

---

## Build & Deployment

### Development Build

```bash
# Install dependencies
pip install -r requirements.txt

# Run from source
python pytun.py --config_ini connector.ini
```

### Production Build (Windows Executable)

```bash
# Build with PyInstaller
pyinstaller pytun.spec

# Output: dist/pytun.exe
```

### PyInstaller Configuration

**File**: `pytun.spec`
```python
a = Analysis(
    ['pytun.py'],
    datas=[('mac_address_pub_key', '.')],  # Bundle public key
    hiddenimports=['ssl', '_ssl'],         # Force SSL imports
    excludes=['urllib3'],                  # Remove unused deps
)

exe = EXE(
    a,
    name='pytun',
    console=True,           # Console application
    uac_admin=True,         # Request admin elevation
    icon='invgate.ico',     # Windows icon
    upx=True,               # UPX compression
    runtime_tmpdir='./tmp'  # Temp directory
)
```

### Windows Service Installation

```bash
# Using shawl (Windows service wrapper)
shawl add --name InvGateTunnel -- pytun.exe --config_ini connector.ini

# Start service
sc start InvGateTunnel

# Stop service
sc stop InvGateTunnel
```

### CI/CD Pipeline

**GitHub Actions** (`.github/workflows/codeql-analysis.yml`):
- Runs on: Push to master, PRs, weekly schedule
- Only performs: CodeQL static analysis
- Does NOT run: Tests (because there are none)

**Jenkins Jobs** (from compiled.md):
- Production build: https://ci.invgate.com/job/pytun-build/
- Staging build: https://ci.invgate.com/job/pytun-build-staging/
- Publish installer: https://ci.invgate.com/job/publish-connector-installer/

### Release Process

1. Update `version.py` (semantic versioning)
2. Commit changes
3. Run Jenkins build job
4. Test installer on Windows machine:
   - Install connector
   - Run test script
   - Check HTTP endpoints
   - Test tunnel restart (kill PIDs)
5. Publish installer to download.invgate.net

### Version Management

**File**: `version.py`
```python
__version__ = "1.1.17"
```

**⚠️ IMPORTANT**: MUST update on every change using semantic versioning:
- MAJOR: Breaking changes
- MINOR: New features (backward compatible)
- PATCH: Bug fixes

---

## Troubleshooting

### Tunnel Won't Start

**Symptom**: Process starts then immediately exits

**Check**:
1. Config file exists and is readable
2. Required fields present: `server_host`, `remote_host`, `username`, `keyfile`
3. SSH key file exists and is readable
4. SSH key is passwordless

**Debug**:
```bash
# Test SSH connection manually
ssh -i /path/to/keyfile username@server_host -p server_port

# Test tunnel establishment
python pytun.py --test_tunnels

# Check logs
tail -f logs/main_connector.log
tail -f logs/tunnel-name.log
```

**Common Errors**:
```
PasswordRequiredException    → SSH key has password, use passwordless key
BadHostKeyException          → server_key mismatch, update known_hosts
AuthenticationException      → SSH key not authorized, check authorized_keys
ConnectionRefusedError       → server_host unreachable, check network/firewall
```

### Tunnel Keeps Restarting

**Symptom**: Logs show constant restart loop

**Check**:
1. `remote_host` and `remote_port` are correct and reachable
2. Service is running on `remote_host:remote_port`
3. Keepalive check succeeding (look for "Connector down!" in logs)

**Debug**:
```bash
# Test service connectivity
python pytun.py --test_connections

# Check if service is running
# (from connector host)
nc -zv remote_host remote_port

# Watch logs for error pattern
tail -f logs/tunnel-name.log | grep -E "error|exception|down"
```

### Alerts Not Sending

**Symptom**: No emails/HTTP POSTs when tunnels fail

**Check SMTP**:
```bash
# Test SMTP configuration
python pytun.py --test_smtp

# Check rate limiting - may be suppressed if sent recently
# Email alerts: 10-minute rate limit per tunnel
```

**Check HTTP**:
```bash
# Test HTTP POST configuration
python pytun.py --test_http

# Check HTTP endpoint accessibility
curl -X POST -u http_user:http_password http_url
```

**Common Issues**:
- Missing required config fields (`smtp_hostname`, `smtp_to`, etc.)
- Firewall blocking SMTP port (25/587/465)
- Rate limit exceeded (check last alert time)
- Invalid credentials

### HTTP Inspection Server Not Responding

**Symptom**: `curl http://localhost:9999/` fails

**Check**:
1. Connector is running (`ps aux | grep pytun` or Task Manager)
2. Port 9999 not in use by another process
3. `inspection_localhost_only=True` (use localhost, not IP)

**Debug**:
```bash
# Check if port is listening
netstat -an | grep 9999          # Linux
netstat -an | findstr 9999       # Windows

# Check main logs
tail -f logs/main_connector.log | grep inspection

# Try different address
curl http://127.0.0.1:9999/
curl http://localhost:9999/
```

### High Memory Usage

**Symptom**: Process memory grows over time

**Possible Causes**:
1. Many concurrent connections (each spawns a thread)
2. Log accumulation (30-day retention)
3. Connection leak (threads not terminating)

**Debug**:
```bash
# Check thread count
ps -eLf | grep pytun | wc -l     # Linux
# Windows: Task Manager → Details → pytun.exe → Threads

# Check open connections
lsof -p <pid>                     # Linux
netstat -ano | findstr <pid>      # Windows

# Monitor memory over time
while true; do ps -p <pid> -o rss=; sleep 60; done
```

**Mitigation**:
- Review `keep_alive_time` (default 30s)
- Check for connection leaks in handler threads
- Verify threads are daemon threads (auto-cleanup)

### Windows Path Issues

**Symptom**: "File not found" errors on Windows with PyInstaller

**Cause**: Windows adds `\\?\` prefix with PyInstaller + Shawl

**Check Logs**:
```
ERROR: Can't find config file: \\?\C:\Program Files\pytun\connector.ini
```

**Fix**: Already handled in code at 3 locations (pytun.py:82, 118; http_server.py:31)

**If Still Failing**: File may have actual path issue, verify file exists

### Permission Denied Errors

**Symptom**: Can't read config, can't write logs, can't access SSH key

**Linux**:
```bash
# Check permissions
ls -la connector.ini
ls -la ~/.ssh/pytun_key
ls -la logs/

# Fix permissions
chmod 644 connector.ini
chmod 400 ~/.ssh/pytun_key
chmod 755 logs/
```

**Windows**:
- Run as Administrator (required by `uac_admin=True`)
- Check file/directory permissions in Properties → Security

### Device Not Authorized

**Symptom**: "Can't start connector, this device is not authorized"

**Cause**: MAC address signature validation failed

**Check**:
1. `mac_address_pub_key` file exists in root directory
2. Signature in `connector.ini` is valid base64 JSON
3. MAC address in signature matches network interface

**Debug**:
```python
# Test MAC address detection
python -c "import psutil; print(psutil.net_if_addrs())"

# Check signature format
import base64, json
sig = "eyJwYXlsb2FkIjoiYWE6YmI6Y2M6ZGQ6ZWU6ZmYiLCAic2lnIjoiYmFzZTY0c2lnIn0="
print(json.loads(base64.b64decode(sig)))
# Should output: {"payload": "aa:bb:cc:dd:ee:ff", "sig": "base64sig"}
```

**Workaround** (temporary):
```ini
# Remove signature line from connector.ini
# Backward compatibility (device.py:67-70) will allow it
[pytun]
# signature=...  # Comment out or remove
```

---

## Additional Resources

### Documentation

- **Customer Docs**: https://docs.google.com/document/d/1bhlSKnXat4NMa48K0-OH7UHLwC2aXXoiSRTljt6L7pY
- **Internal Docs**: `docs/compiled.md`
- **Architecture Diagram**: `docs/connector_flow.excalidraw.png`

### Related Projects

- **TMT** (Tunnel Manager Tool): http://mercurial.invgate.com/Internal/tmt
  - Flask API for managing connectors
  - MySQL/SQLite database
  - Creates connector installers
- **Tunnel Windows Installer**: http://mercurial.invgate.com/Internal/tunnel-windows-installer
  - NSIS script to build Windows installer
  - Requires `pytun.exe` from PyInstaller

### External Dependencies

- **Paramiko Docs**: https://docs.paramiko.org/
- **PyInstaller Docs**: https://pyinstaller.org/
- **Python multiprocessing**: https://docs.python.org/3/library/multiprocessing.html

---

## Development Guidelines (Internal Use)

**Note**: This is a private repository maintained by InvGate's Internal Tools team. External contributions are not accepted.

### Before Making Changes

1. **Read this entire CLAUDE.md file**
2. **Check Technical Debt section** - don't add to known issues
3. **Update `version.py`** - use semantic versioning
4. **Test on Windows** - primary deployment platform

### Code Style

- Follow existing patterns (even if not ideal)
- Avoid adding more broad exception handlers
- Add type hints where possible (but maintain 3.6+ compatibility)
- Document complex logic with comments
- Update this CLAUDE.md if adding new components

### Testing (TODO: Needs Improvement)

**Current**: Only manual CLI tests exist
```bash
python pytun.py --test_all
```

**Future**: Add pytest tests for new features
```bash
pytest tests/unit/test_new_feature.py
```

### Change Checklist

- [ ] Version updated in `version.py`
- [ ] Changes tested on Windows
- [ ] Logs reviewed for errors/warnings
- [ ] No new broad exception handlers added
- [ ] CLAUDE.md updated if needed
- [ ] No secrets/credentials in code

---

## Quick Reference Card

### Start/Stop

```bash
# Development
python pytun.py

# Windows Service
sc start InvGateTunnel
sc stop InvGateTunnel
```

### Testing

```bash
python pytun.py --test_all          # Full diagnostic
python pytun.py --test_connections  # Service connectivity
python pytun.py --test_tunnels      # SSH establishment
```

### Monitoring

```bash
# Logs
tail -f logs/main_connector.log

# HTTP Status
curl http://localhost:9999/status
```

### Common Files

```
connector.ini          # Main config (REQUIRED)
configs/*.ini          # Tunnel configs
logs/                  # Log files (auto-created)
mac_address_pub_key    # Public key (optional)
```

### Environment

```bash
# Python 3.6+
python --version

# Dependencies
pip install -r requirements.txt

# Build
pyinstaller pytun.spec
```

---

**Maintainer**: InvGate - Internal Tools Team