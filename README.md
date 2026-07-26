# Abacus VM Tools — Hermes Classroom Connector

Tools for students using the Hermes Classroom Portal with an Abacus
SuperComputer VM.

## What this does

This repo installs a secure **connector** on your Abacus VM that lets the
course portal relay authenticated chat requests to Hermes running on your
VM's loopback. The browser never sees your Hermes token, VM address, or
connector secret.

```
student browser → course portal (HTTPS) → your VM's Abacus hostname
  → Nginx → connector (127.0.0.1:8765) → Hermes (127.0.0.1:8642)
```

## Prerequisites

1. An **Abacus.ai** account with a SuperComputer VM.
2. The **Hermes button** clicked in the Abacus console (this installs Hermes
   and starts `hermes serve` on port 8642).

## Quick install (one command)

After clicking the Hermes button in the Abacus console, SSH into your VM
and run:

```bash
git clone https://github.com/randywreed/abacus_vm_tools.git ~/abacus_vm_tools
bash ~/abacus_vm_tools/install.sh
```

That's it. The installer:

- Checks that Hermes is running on `127.0.0.1:8642`
- Installs Python dependencies if missing (fastapi, uvicorn, httpx, websockets)
- Copies the connector code to `/opt/hermes-classroom-connector/`
- Generates a random 64-char shared secret in `/etc/hermes-classroom-connector/connector.env`
- Installs a hardened systemd service
- Patches Nginx to publish only `/hermes-classroom/` (preserves a timestamped backup)
- Starts the service and prints your hostname + secret

## After installation

The installer prints two values you need for portal enrollment:

1. **Your VM's hostname** — e.g. `4100ca910.abacusai.cloud`
2. **Your connector shared secret** — a 64-character hex string

Then in the course portal:

1. Open the **Workspace** page
2. Click **Create token** to get a one-time enrollment token
3. Register your connector using the hostname and secret shown by the installer
4. Click **Test connection** to verify the signed health check passes

## Re-running the installer

The installer is **idempotent** — you can run it again safely. It will:

- Reuse your existing shared secret (not generate a new one)
- Skip the Nginx patch if it's already applied
- Restart the connector service with the latest code

## Service management

```bash
# Check status
systemctl status hermes-classroom-connector

# Restart after updates
sudo systemctl restart hermes-classroom-connector

# View logs
journalctl -u hermes-classroom-connector -f
```

## Security notes

- Hermes and the connector listen on **loopback only** (127.0.0.1)
- Nginx publishes only `/hermes-classroom/` — no direct Hermes or connector access
- Every request is HMAC-signed with your per-VM secret
- Nonce replay protection (5-minute window, constant-time comparison)
- The systemd service runs with `NoNewPrivileges`, `ProtectSystem=strict`,
  `MemoryDenyWriteExecute`, and no capabilities

## Files installed

| Path | Purpose |
|---|---|
| `/opt/hermes-classroom-connector/` | Connector Python code |
| `/etc/hermes-classroom-connector/connector.env` | Shared secret + config (root-owned, 0640) |
| `/var/lib/hermes-classroom-connector/` | Runtime data directory |
| `/etc/systemd/system/hermes-classroom-connector.service` | Systemd unit |
| Nginx config (patched in place) | `/hermes-classroom/` proxy location |
