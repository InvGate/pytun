# pytun - SSH Reverse Tunnel Manager

[![Python 3.6+](https://img.shields.io/badge/python-3.6+-blue.svg)](https://www.python.org/downloads/)

**pytun** is a production-grade SSH reverse tunnel manager that allows on-premises services to be securely accessed from cloud instances without VPN. It provides automatic health monitoring, alerting, and self-healing capabilities.

## 🎯 Use Case

Connect cloud applications to local network services (LDAP, databases, SMTP) that don't have public network access:

```
Cloud Application → SSH Server → pytun Connector → Local Service
   (SaaS)          (Bastion)    (Your Network)    (LDAP/DB/etc)
```

**Example**: Your InvGate cloud instance needs to query your on-premises Active Directory server without exposing AD to the internet.

## ✨ Features

- 🔄 **Automatic Restart** - Monitors tunnels every 30 seconds, auto-restarts on failure
- 🔐 **SSH Security** - Key-based authentication with host verification (no passwords)
- 📊 **HTTP Monitoring** - Built-in status API on port 9999
- 📧 **Alerting** - Email (SMTP) and HTTP POST alerts with rate limiting
- 🔍 **Multi-Tunnel** - Manage multiple tunnels from single connector instance
- 💾 **Logging** - Daily rotating logs with 30-day retention
- 🔒 **Device Authorization** - Optional MAC address + cryptographic signature
- 🪟 **Windows Service** - Runs as Windows service (via PyInstaller + Shawl)

## 🚀 Quick Start

### Prerequisites

- Python 3.6+ (recommended: 3.10+)
- SSH access to a bastion/jump host
- Passwordless SSH key pair

### Installation

```bash
# 1. Clone repository
git clone https://github.com/InvGate/pytun.git
cd pytun

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure connector (see Configuration section)
cp connector.ini.example connector.ini
# Edit connector.ini with your settings

# 4. Create tunnel configuration
mkdir -p configs
# Create configs/my-tunnel.ini (see example below)

# 5. Run connector
python pytun.py
```

## ⚙️ Configuration

### Main Configuration: `connector.ini`

```ini
[pytun]
# Required
tunnel_manager_id=my-connector-001    # Unique identifier for this connector

# Tunnel Configuration
tunnel_dirs=./configs                 # Directory containing tunnel .ini files

# Logging
log_level=DEBUG                       # DEBUG | INFO | WARNING | ERROR
log_to_console=True                   # Output to console
log_path=./logs                       # Log directory

# HTTP Inspection Server
inspection_port=9999                  # Status API port
inspection_localhost_only=True        # Security: localhost only (recommended)

# Optional: Email Alerts (SMTP)
smtp_hostname=smtp.example.com        # SMTP server
smtp_port=587                         # SMTP port
smtp_security=tls                     # none | tls | ssl
smtp_login=alerts@example.com         # SMTP username
smtp_password=your-password           # SMTP password (plain text)
smtp_from=connector@example.com       # From address
smtp_to=admin@example.com             # Alert recipient

# Optional: HTTP POST Alerts
http_url=https://api.example.com/webhook
http_user=webhook-user
http_password=webhook-password
```

### Tunnel Configuration: `configs/my-tunnel.ini`

```ini
[tunnel]
# Tunnel Name (optional, for logging/alerts)
tunnel_name=LDAP Service Tunnel

# SSH Server (Cloud Bastion/Jump Host)
server_host=bastion.example.com       # Required: SSH server hostname/IP
server_port=22                        # SSH port (default: 22)
server_key=~/.ssh/known_hosts         # Server's public key file

# Port Forwarding
port=10389                            # Port to listen on SSH server (bastion)

# Local Service (On-Premises)
remote_host=ldap.local.network        # Required: Local service hostname/IP
remote_port=389                       # Required: Local service port

# SSH Authentication
username=tunnel-user                  # Required: SSH username
keyfile=~/.ssh/pytun_key              # Required: Path to private SSH key (passwordless!)

# Health Monitoring
keep_alive_time=30                    # Seconds between keepalive checks

# Logging (optional, inherits from main config if not set)
log_level=DEBUG
log_to_console=False
```

### How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│ Example: Expose local LDAP (389) on cloud server port 10389     │
└──────────────────────────────────────────────────────────────────┘

Cloud App connects to: bastion.example.com:10389
           ↓
   [SSH Server / Bastion]
           ↓ (reverse SSH tunnel)
      [pytun Connector]
           ↓ (local network)
   [LDAP Server: ldap.local.network:389]

Data flows bidirectionally through encrypted SSH tunnel
```

## 🔧 Usage

### Running the Connector

```bash
# Standard run (uses ./connector.ini)
python pytun.py

# Custom configuration file
python pytun.py --config_ini /path/to/connector.ini

# Show version
python pytun.py --version
```

### Testing Before Deployment

```bash
# Test service connectivity (checks if local services are reachable)
python pytun.py --test_connections

# Test SSH tunnel establishment (verifies SSH connection to bastion)
python pytun.py --test_tunnels

# Test SMTP email alerts
python pytun.py --test_smtp

# Test HTTP POST alerts
python pytun.py --test_http

# Run all tests (comprehensive diagnostic)
python pytun.py --test_all
```

### Monitoring

```bash
# View logs (real-time)
tail -f logs/main_connector.log       # Main connector log
tail -f logs/my-tunnel-name.log       # Per-tunnel logs

# HTTP Status API (while connector is running)
curl http://localhost:9999/                # Health check
curl http://localhost:9999/status          # Tunnel status + service health
curl http://localhost:9999/configs -o configs.zip   # Download configs
curl http://localhost:9999/logs -o logs.zip         # Download logs
```

**Status Response Example**:
```json
{
  "created_at": 1612345678.123,
  "mac_address": "00:11:22:33:44:55",
  "status_data": {
    "my-tunnel": {
      "started_times": 2,
      "last_start": 1612345678.123
    }
  },
  "my-tunnel": {
    "remote_host": "ldap.local.network",
    "remote_port": 389,
    "status": true
  }
}
```

## 🔐 SSH Key Setup

**Important**: pytun requires **passwordless** SSH keys.

### Generate SSH Key Pair

```bash
# Generate 4096-bit RSA key (no passphrase)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/pytun_key -N ""

# Two files created:
#   ~/.ssh/pytun_key      (private key - keep secure!)
#   ~/.ssh/pytun_key.pub  (public key)
```

### Deploy Public Key to Bastion

```bash
# Copy public key to SSH server's authorized_keys
ssh-copy-id -i ~/.ssh/pytun_key.pub tunnel-user@bastion.example.com

# Or manually:
cat ~/.ssh/pytun_key.pub | ssh tunnel-user@bastion.example.com \
  "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

### Test SSH Connection

```bash
# Verify passwordless authentication works
ssh -i ~/.ssh/pytun_key tunnel-user@bastion.example.com

# If prompted for password, key setup failed
```

### Get Server's Public Key

```bash
# Add server to known_hosts
ssh-keyscan bastion.example.com >> ~/.ssh/known_hosts

# Use this file as server_key in tunnel config
```

## 📦 Building Windows Executable

```bash
# Install PyInstaller (if not already installed)
pip install pyinstaller

# Build executable
pyinstaller pytun.spec

# Output: dist/pytun.exe
```

### Windows Service Installation

```bash
# Using Shawl (Windows service wrapper)
# Download: https://github.com/mtkennerly/shawl

shawl add --name InvGateTunnel -- \
  "C:\path\to\pytun.exe" --config_ini "C:\path\to\connector.ini"

# Start service
sc start InvGateTunnel

# Stop service
sc stop InvGateTunnel

# Remove service
shawl remove --name InvGateTunnel
```

## 🏗️ Architecture

### Process Model

```
pytun.py (Main Process)
  ├─ Loads configurations
  ├─ Spawns TunnelProcess for each .ini file (multiprocessing)
  ├─ Monitors health every 30 seconds
  ├─ Auto-restarts failed processes
  └─ HTTP inspection server (port 9999)

TunnelProcess (Per-Tunnel Child Process)
  ├─ Establishes SSH connection
  ├─ Requests reverse port forwarding
  ├─ Accepts incoming connections
  └─ Spawns handler thread per connection (bidirectional relay)
```

### Health Monitoring

Each tunnel performs three health checks every 30 seconds:
1. **SSH Keepalive** - `send_ignore()` at transport level
2. **Transport Active** - Verify SSH connection state
3. **Session Test** - Open/close a test SSH session

If any check fails → tunnel marked as failed → process exits → parent restarts

### Alerting

- **Email**: SMTP with TLS/SSL, **rate limited** to 1 email per 10 minutes per tunnel
- **HTTP POST**: Webhook to external monitoring system, **no rate limit**
- **Non-blocking**: Alerts sent via `ThreadPoolExecutor`, doesn't block tunnel operations

## 🛡️ Security Considerations

### ✅ Strong Security Features

- **SSH Key-Based Auth**: No passwords, only SSH keys
- **Host Key Verification**: Rejects unknown SSH servers (prevents MITM)
- **SSH Agent Disabled**: No automatic key discovery
- **Localhost-Only API**: Inspection server only on 127.0.0.1 by default

### ⚠️ Security Warnings

1. **Config Files Contain Credentials**
   - SMTP passwords stored in plain text
   - Restrict file permissions: `chmod 600 connector.ini`
   - Consider encrypting config files at rest

2. **HTTP `/configs` Endpoint Exposes SSH Keys**
   - Returns ZIP with ALL config files including private keys
   - **Mitigation**: `inspection_localhost_only=True` by default
   - **NEVER** set `inspection_localhost_only=False` in production
   - Use SSH tunnel for remote access: `ssh -L 9999:localhost:9999 user@connector-host`

3. **SSH Keys Must Be Passwordless**
   - Required for unattended operation
   - Secure with file permissions: `chmod 400 ~/.ssh/pytun_key`

4. **Log Files May Contain Sensitive Data**
   - Set `log_level=INFO` in production (not DEBUG)
   - Secure log directory permissions

## 🐛 Troubleshooting

### Tunnel Won't Start

**Symptoms**: Process exits immediately, no tunnel established

**Common Causes**:
```bash
# Check logs for specific error
tail -f logs/main_connector.log

# Common errors and solutions:
PasswordRequiredException        → SSH key has password (must be passwordless)
BadHostKeyException             → Server key mismatch (update known_hosts)
AuthenticationException         → SSH key not authorized (check authorized_keys)
FileNotFoundError: keyfile      → Path to SSH key incorrect
Missing keyfile argument        → keyfile not set in tunnel config
```

**Debug Steps**:
```bash
# 1. Test SSH connection manually
ssh -i /path/to/keyfile username@server_host -p server_port

# 2. Test tunnel establishment
python pytun.py --test_tunnels

# 3. Test service connectivity
python pytun.py --test_connections
```

### Tunnel Keeps Restarting

**Symptoms**: Logs show constant restart loop

**Check**:
1. Local service is running and reachable
2. `remote_host` and `remote_port` are correct
3. Firewall allows connection to local service

```bash
# Test service connectivity from connector host
nc -zv remote_host remote_port

# Or use telnet
telnet remote_host remote_port

# Check connector logs for pattern
tail -f logs/tunnel-name.log | grep -E "error|exception|down"
```

### Alerts Not Sending

**SMTP Alerts**:
```bash
# Test SMTP configuration
python pytun.py --test_smtp

# Check rate limiting (10-minute window)
# If alert sent recently, may be suppressed

# Verify SMTP settings in connector.ini
```

**HTTP Alerts**:
```bash
# Test HTTP POST configuration
python pytun.py --test_http

# Test webhook endpoint manually
curl -X POST -u http_user:http_password \
  -H "Content-Type: application/json" \
  -d '{"test": "alert"}' \
  http_url
```

### Permission Denied

**Linux**:
```bash
# Fix config file permissions
chmod 600 connector.ini
chmod 600 configs/*.ini
chmod 400 ~/.ssh/pytun_key
chmod 755 logs/

# Check ownership
ls -la connector.ini
```

**Windows**: Run as Administrator (required by `uac_admin=True` in PyInstaller spec)

## 📚 Documentation

- **[CLAUDE.md](CLAUDE.md)** - Comprehensive technical documentation for developers/AI assistants
- **[Customer Docs](https://docs.google.com/document/d/1bhlSKnXat4NMa48K0-OH7UHLwC2aXXoiSRTljt6L7pY)** - End-user documentation

## 🔗 Related Projects

- **TMT (Tunnel Manager Tool)** - Flask API for managing connectors and generating installers
- **Tunnel Windows Installer** - NSIS script to build Windows installer packages

## 📋 Requirements

### Python Dependencies

```
coloredlogs==14.0              # Colored logging output
deckar01-ratelimit==3.0.2      # Rate limiting for alerts
email-validator==1.1.1         # Email address validation
paramiko==3.4.0                # SSH protocol implementation
psutil==5.7.2                  # System and process utilities
requests==2.32.4               # HTTP requests for alerts
```

### Platform Support

- **Primary**: Windows (via PyInstaller executable)
- **Supported**: Linux (run from source)
- **Tested On**: Windows, Windows Server, Ubuntu

## 📊 Technical Details

### Key Technologies

- **SSH Implementation**: Paramiko (Python SSH library)
- **Process Isolation**: `multiprocessing.Process` (one process per tunnel)
- **Concurrency**: Daemon threads for connection handling
- **I/O Multiplexing**: `select.select()` for bidirectional relay
- **Logging**: `TimedRotatingFileHandler` (daily rotation, 30-day retention)

### Port Forwarding Flow

```python
# Simplified flow (see tunnel_infra/Tunnel.py for details)

1. SSH client connects to bastion server
2. Request reverse port forward: server.port → client
3. Server accepts connections on specified port
4. For each connection:
   - SSH server forwards to pytun via SSH tunnel
   - pytun spawns handler thread
   - Handler connects to local service
   - select() relays data bidirectionally
5. Connection closes, thread exits
6. Keepalive checks every 30 seconds
```

---

**Version**: 1.1.17
**Maintainer**: InvGate - Internal Tools Team
**Repository**: Private (Internal Use Only)
**Last Updated**: 2025
