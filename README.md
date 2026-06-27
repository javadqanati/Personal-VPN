# Safe Connection to the Network via VPN

A censorship-resistant VPN system with a web-based administration dashboard,
built as a project for the System Security course at the University of Messina
under Prof. Massimo Villari.

## Overview

The system implements a VPN that resists deep packet inspection (DPI) and
active probing by a state-level network adversary, while remaining
cryptographically secure end-to-end. It is delivered as a working deployment
with a simple web-based admin dashboard rather than a manual configuration
file workflow.

## Architecture

The deployment consists of two coexisting VPN tunnels and a control plane:

### Tunnel 1 — VLESS over WebSocket via Cloudflare (port 443)

- TLS 1.3 with a Let's Encrypt certificate
- VLESS protocol carried inside a WebSocket on path `/tunnel2026`
- Nginx terminates TLS and forwards `/tunnel2026` to Xray on `127.0.0.1:10000`
- Any unauthenticated request to `/` is transparently proxied to a real
  decoy site (Wikipedia) as defence against active probing
- Cloudflare proxies the connection, so the origin IP is never exposed

### Tunnel 2 — VLESS + REALITY + XTLS Vision (port 8443)

- TCP-level handshake that mimics a real, externally-resolvable website
  (`www.google.com`) using REALITY's handshake camouflage
- XTLS Vision flow control to flatten the packet-length fingerprint that
  identifies TLS-in-TLS proxies
- AEAD ciphers (AES-256-GCM / ChaCha20-Poly1305) at the outer TLS layer
- This is the tunnel that survives Iran-grade DPI; the WebSocket tunnel is
  retained as a CDN-friendly fallback

### Web administration dashboard

- FastAPI backend in Docker
- PostgreSQL stores the authoritative user list and time-series traffic
  snapshots
- Xray's gRPC stats API (`127.0.0.1:10085`) provides live traffic counters
- Admin authentication via HTTP Basic with a bcrypt-hashed password
- A systemd path-watcher allows the dashboard container to trigger an
  Xray reload without granting it Docker-socket or sudo access

## Security properties

- **Confidentiality and integrity**: TLS 1.3 with forward secrecy (ECDHE)
  and AEAD ciphers, end-to-end between client and server.
- **Camouflage against DPI**: outer traffic on tunnel 1 is indistinguishable
  from ordinary HTTPS to a Cloudflare-fronted domain; tunnel 2 mimics a
  TLS 1.3 handshake to a real, legitimate website.
- **Active-probe resistance**: connections that do not present a valid VLESS
  handshake on port 443 are proxied to a decoy site, denying the censor a
  deterministic fingerprint.
- **No destination logging**: Xray access logging is disabled by design;
  only aggregate per-user byte counters are recorded. The server cannot
  produce a list of sites that any user has visited.
- **Auditable user lifecycle**: every user provisioning or revocation is
  logged in PostgreSQL with timestamps and the Xray config is atomically
  regenerated. A failed validation step rolls back the change.

## Repository layout

- `app/` — FastAPI backend (`main.py`) and the static dashboard UI
- `deploy/nginx/` — Nginx site configuration
- `deploy/systemd/` — `xray-reload.service` and `xray-reload.path` units
- `deploy/xray/config.example.json` — sanitised Xray configuration template
- `Dockerfile`, `docker-compose.yml`, `requirements.txt` — dashboard container
- `.env` (not committed) — admin credentials, database DSN, REALITY public key

## Deployment summary

1. Provision an Ubuntu 24.04 VPS with a public IP.
2. Install Xray-core, Nginx, certbot, Docker, and PostgreSQL.
3. Point a domain at the server through Cloudflare (orange-cloud proxy for
   the WebSocket inbound; DNS-only for the REALITY inbound).
4. Generate a Let's Encrypt certificate for the domain.
5. Generate REALITY key material with `xray x25519` and `openssl rand -hex 8`.
6. Copy `deploy/xray/config.example.json` to `/usr/local/etc/xray/config.json`
   and replace the placeholders.
7. Install the systemd units from `deploy/systemd/` and enable
   `xray-reload.path` so the dashboard can trigger reloads.
8. Create a `.env` file with admin credentials, the database DSN, and the
   REALITY public key.
9. Run `docker compose up -d` from this directory.
10. Open `https://your-domain/dashboard/` and authenticate.

## Course context

University of Messina, System Security course (Prof. Massimo Villari),
Academic Year 2025–2026. Project title: *Safe Connection to the Network via VPN*.

## Acknowledgements

This project would not have been possible without the open-source work of
the XTLS / Xray-core community, the Let's Encrypt project, and the
maintainers of FastAPI, PostgreSQL, and Chart.js.
