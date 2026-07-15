# Safe Connection to the Network via VPN

A censorship-resistant VPN system with web-based user administration, built using **Xray-core**, **VLESS**, **REALITY**, **XTLS Vision**, **WebSocket over TLS**, **Nginx**, **PostgreSQL**, **Docker**, and **FastAPI**.

The project is designed not only to encrypt network traffic, but also to reduce recognizable VPN fingerprints under network-level inspection. It provides two complementary connection methods and a lightweight administration dashboard for managing VPN users.

> **Educational project:** This repository documents an applied network-security project focused on privacy, censorship resistance, protocol analysis, and secure system administration.

---

## Overview

Traditional VPN protocols provide strong encryption but may still expose recognizable traffic patterns. A network operator performing Deep Packet Inspection (DPI) may therefore identify and block VPN connections without breaking their encryption.

This project explores a different approach: combining encrypted transport with traffic camouflage.

The system provides two VPN connection methods:

* **VLESS + REALITY + XTLS Vision** — the primary connection method, designed to reduce protocol fingerprinting and resist active probing.
* **VLESS + WebSocket + TLS** — an alternative connection method using HTTPS-compatible transport.

Both connection methods use the same user UUIDs, allowing each authorized user to maintain a single identity across the system.

---

## System Architecture

The project is divided into three main planes:

### Data Plane

Handles actual VPN traffic through two Xray inbounds:

```text
Client
   │
   ├── VLESS + REALITY + XTLS Vision
   │        └── Xray :8443
   │
   └── VLESS + WebSocket + TLS
            └── HTTPS :443
                 │
                 └── Nginx
                      │
                      └── Xray WebSocket inbound
```

### Control Plane

Responsible for the active Xray configuration.

```text
PostgreSQL
     │
     │ User records
     ▼
FastAPI Dashboard
     │
     │ Generate configuration
     ▼
Xray config.json
     │
     │ Validate and atomically replace
     ▼
Xray Service
```

### Management Plane

A FastAPI-based web dashboard allows the administrator to:

* Add VPN users
* Revoke users
* Assign users to available inbounds
* Generate client connection links
* Regenerate the Xray configuration
* Trigger an Xray service reload
* View aggregate traffic statistics

---

## Technology Stack

| Component             | Technology            |
| --------------------- | --------------------- |
| VPN Core              | Xray-core             |
| VPN Protocol          | VLESS                 |
| Primary Transport     | REALITY + XTLS Vision |
| Alternative Transport | WebSocket over TLS    |
| Reverse Proxy         | Nginx                 |
| Backend               | FastAPI / Python      |
| Database              | PostgreSQL            |
| Containerization      | Docker                |
| Service Management    | systemd               |
| Password Hashing      | bcrypt                |
| Key Agreement         | X25519                |
| Transport Security    | TLS 1.3               |

---

## VLESS

VLESS is used as the inner transport protocol.

VLESS intentionally does not provide its own encryption layer. Instead, confidentiality and integrity are provided by the secure outer transport.

For example:

```json
{
  "protocol": "vless",
  "settings": {
    "clients": [
      {
        "id": "<USER_UUID>",
        "flow": "xtls-rprx-vision"
      }
    ],
    "decryption": "none"
  }
}
```

The `decryption: "none"` setting does **not** mean that network traffic is necessarily transmitted in plaintext. In this architecture, VLESS operates within a separately secured transport layer.

---

## REALITY + XTLS Vision

The primary tunnel uses:

```text
VLESS
   +
REALITY
   +
XTLS Vision
```

REALITY is used to make unauthorized or probing connections behave differently from authenticated VPN connections, reducing the ability of active probing to trivially identify the service.

XTLS Vision complements this by addressing traffic characteristics associated with nested encrypted protocols.

Example configuration structure:

```json
{
  "tag": "vless-reality",
  "listen": "0.0.0.0",
  "port": 8443,
  "protocol": "vless",
  "settings": {
    "clients": [
      {
        "id": "<USER_UUID>",
        "flow": "xtls-rprx-vision"
      }
    ],
    "decryption": "none"
  },
  "streamSettings": {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
      "dest": "<CAMOUFLAGE_TARGET>:443",
      "serverNames": [
        "<CAMOUFLAGE_TARGET>"
      ],
      "privateKey": "<X25519_PRIVATE_KEY>",
      "shortIds": [
        "<SHORT_ID>"
      ]
    }
  }
}
```

Sensitive values such as private keys and production identifiers should never be committed to the repository.

---

## WebSocket over TLS

The project also provides an alternative VLESS connection using WebSocket over TLS.

The architecture is approximately:

```text
Client
   │
   │ TLS / HTTPS
   ▼
Nginx :443
   │
   │ WebSocket upgrade
   ▼
Secret WebSocket Path
   │
   ▼
Xray VLESS WebSocket Inbound
```

Nginx handles TLS termination and forwards valid WebSocket upgrade requests to the local Xray service.

Example:

```nginx
server {
    listen 443 ssl;
    server_name <YOUR_DOMAIN>;

    ssl_certificate     <CERTIFICATE_PATH>;
    ssl_certificate_key <PRIVATE_KEY_PATH>;

    location <WEBSOCKET_PATH> {
        proxy_pass http://127.0.0.1:<XRAY_WS_PORT>;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }

    location / {
        proxy_pass <DECOY_WEBSITE>;
        proxy_ssl_server_name on;
    }
}
```

---

## User Management

PostgreSQL acts as the authoritative source for VPN users.

A simplified schema:

```sql
CREATE TABLE vpn_users (
    id          SERIAL PRIMARY KEY,
    uuid        UUID NOT NULL UNIQUE,
    email       TEXT NOT NULL UNIQUE,
    label       TEXT,
    inbounds    TEXT[] NOT NULL DEFAULT
                ARRAY['vless-reality', 'vless-ws'],
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ
);
```

Users can be assigned to one or multiple Xray inbounds.

For example:

```text
User UUID
    │
    ├── vless-reality
    │
    └── vless-ws
```

When the user database changes, the Xray configuration is regenerated from the current authorized user list.

---

## Adding a User

A new user can be created by generating a UUID and inserting the corresponding record into PostgreSQL.

Example:

```bash
NEW_UUID=$(xray uuid)
EMAIL="user@example.com"

echo "UUID: $NEW_UUID"

sudo docker exec -i <POSTGRES_CONTAINER> \
  psql -U <DATABASE_USER> -d <DATABASE_NAME> -c \
  "INSERT INTO vpn_users (uuid, email, label, inbounds)
   VALUES (
     '$NEW_UUID',
     '$EMAIL',
     'User',
     ARRAY['vless-reality','vless-ws']
   );"
```

A successful insertion returns:

```text
INSERT 0 1
```

The dashboard can then regenerate the Xray configuration.

After regeneration, the user's UUID should appear in each Xray inbound to which the user has been assigned.

For example, a user assigned to:

```text
vless-reality
vless-ws
```

will normally appear twice in the generated configuration: once under each inbound.

---

## Configuration Regeneration

The dashboard regenerates the Xray configuration whenever the authorized user list changes.

The process follows this sequence:

```text
User added or revoked
        │
        ▼
Update PostgreSQL
        │
        ▼
Generate new Xray configuration
        │
        ▼
Validate configuration
        │
   ┌────┴────┐
   │         │
 Invalid    Valid
   │         │
 Reject      ▼
        Backup old config
             │
             ▼
        Atomic replacement
             │
             ▼
        Signal Xray reload
```

Before replacing the active configuration, the new configuration is validated using Xray itself:

```python
result = subprocess.run(
    ["xray", "run", "-test", "-config", tmp_path],
    capture_output=True,
    timeout=10
)

if result.returncode != 0:
    os.unlink(tmp_path)
    return False, "Configuration validation failed"
```

The current configuration is backed up before replacement:

```python
shutil.copy2(
    XRAY_CONFIG_PATH,
    XRAY_CONFIG_PATH + ".prev"
)
```

The new configuration is then installed using an atomic filesystem operation:

```python
os.replace(
    tmp_path,
    XRAY_CONFIG_PATH
)
```

This prevents Xray from reading a partially written configuration.

---

## Safe Host-Service Reload

The management dashboard runs inside Docker, while Xray runs as a host-level systemd service.

Instead of giving the dashboard container access to:

```text
/var/run/docker.sock
```

or granting it unrestricted `sudo` privileges, the project uses a systemd path watcher.

The container writes to a predefined signal file:

```text
/var/run/vpn-dashboard/reload-xray
```

A systemd path unit watches the file:

```ini
[Unit]
Description=Watch for reload signal from dashboard

[Path]
PathModified=/var/run/vpn-dashboard/reload-xray
Unit=xray-reload.service

[Install]
WantedBy=multi-user.target
```

The associated service performs one specific privileged operation:

```ini
[Unit]
Description=Reload Xray when dashboard requests it
After=xray.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart xray
```

This reduces the privileges available to the dashboard container and avoids exposing the host Docker socket.

---

## Admin Authentication

The management dashboard uses HTTP Basic authentication over TLS.

The administrator password is stored as a bcrypt hash rather than plaintext.

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def verify_admin(
    credentials: HTTPBasicCredentials = Depends(security)
):
    user_ok = secrets.compare_digest(
        credentials.username,
        ADMIN_USER
    )

    pass_ok = (
        pwd_context.verify(
            credentials.password,
            ADMIN_PASS_HASH
        )
        if ADMIN_PASS_HASH
        else False
    )

    if not (user_ok and pass_ok):
        raise HTTPException(
            401,
            "Invalid credentials",
            {"WWW-Authenticate": "Basic"}
        )

    return credentials.username
```

`secrets.compare_digest()` is used for username comparison to reduce timing-related information leakage.

The password itself is verified against the stored bcrypt hash using the configured password context.

---

## Privacy and Logging

The system follows a data-minimization approach.

Xray access logging is intentionally disabled. Only operational warnings and errors are written to the error log.

Example:

```json
{
  "log": {
    "loglevel": "warning",
    "error": "/var/log/xray/error.log"
  }
}
```

The system may retain aggregate per-user traffic counters for administration purposes, but does not intentionally maintain destination-level browsing history through Xray access logs.

---

## User Revocation

User revocation uses a soft-delete model.

```

This preserves historical authorization records while removing the user's UUID from the active VPN configuration.

---

## Security Properties

The project is designed around several security principles:

* **Encrypted transport** using modern TLS mechanisms
* **Reduced protocol fingerprinting** through REALITY and XTLS Vision
* **Active-probing resistance** through controlled fallback behavior
* **Multiple connection methods** for improved availability
* **Hashed administrator credentials**
* **No plaintext password storage**
* **Minimal traffic logging**
* **Database-backed user management**
* **Soft-delete user revocation**
* **Validated configuration generation**
* **Atomic configuration replacement**
* **Privilege separation between Docker and the host**
* **No Docker socket exposure to the dashboard**

---

## Limitations

This architecture does not guarantee connectivity under every censorship regime.

Known limitations include:

* Server IP addresses may still be blocked or throttled independently of protocol identification.
* Camouflage targets may become unsuitable or unreachable.
* CDN availability varies between networks and jurisdictions.
* Traffic analysis techniques continue to evolve.
* HTTP Basic authentication is appropriate for a small single-operator dashboard but should be replaced with stronger session-based authentication for larger deployments.
* Additional rate limiting and administrative audit logging would be appropriate for a production multi-user management system.

---

## Future Improvements

Possible future work includes:

* Session-based dashboard authentication
* Multi-factor authentication
* Login rate limiting
* Administrative audit logs
* Automated server health monitoring
* Automated IP rotation
* Multiple configurable camouflage targets
* Improved traffic statistics visualization
* Automated user expiration
* Per-user quotas
* Automated configuration rollback
* Integration and deployment tests
* Automated backup and disaster recovery

---

## Disclaimer

This project is intended for **educational, research, privacy, and legitimate network-security purposes**.

Users are responsible for complying with applicable laws, regulations, hosting-provider policies, and network policies in their jurisdiction.

---

## References

1. RFC 8446 — The Transport Layer Security (TLS) Protocol Version 1.3
2. RFC 8439 — ChaCha20 and Poly1305 for IETF Protocols
3. RFC 7748 — Elliptic Curves for Security
4. RFC 5288 — AES Galois Counter Mode Cipher Suites for TLS
5. Provos & Mazières — *A Future-Adaptable Password Scheme*
6. XTLS / Xray-core documentation
7. REALITY transport documentation
8. VLESS protocol documentation
9. RFC 6455 — The WebSocket Protocol
