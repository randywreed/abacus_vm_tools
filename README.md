# Abacus VM Tools — Hermes Classroom Connector

This GitHub repository is the canonical source for the Hermes Classroom VM
connector. Clone it on every new student Abacus SuperComputer VM, then install
from that clone.

## What this does

The installer places a secure connector between the course portal and Hermes
running on the VM's loopback interface. The browser never receives the Hermes
token, VM address, or connector shared secret.

```
student browser → course portal (HTTPS) → your VM's Abacus hostname
  → Nginx → connector (127.0.0.1:8765) → Hermes (127.0.0.1:8642)
```

The connector supports:

- Health and capability discovery
- Available models
- Usage credits and a rolling-30-day Abacus usage snapshot
- Files and attachments
- Streaming chat
- Sessions
- Clarification requests and responses

## Prerequisites

1. An Abacus.ai account with a SuperComputer VM.
2. The Hermes button clicked in the Abacus console so Hermes is installed and
   available on port 8642.
3. Git and Nginx on the VM.

## Install and enroll a new VM

Open the VM terminal and clone this canonical repository:

```bash
git clone https://github.com/randywreed/abacus_vm_tools.git ~/abacus_vm_tools
bash ~/abacus_vm_tools/install.sh
```

Then complete enrollment:

1. In the course portal, open Workspace and create a one-time enrollment
   token.
2. At the installer's `Register this VM with the course portal now? [Y/n]`
   prompt, press Enter to accept the secure handoff.
3. Enter the portal's HTTPS origin when prompted.
4. Paste the one-time enrollment token at the hidden token prompt. The token
   is not echoed.
5. Return to Workspace and click Test connection.

The installer invokes the protected registration command with zero arguments,
so it securely prompts for both the portal origin and token. There is no VM
hostname or shared-secret copying step.

If you decline the handoff, run the safe retry command later:

```bash
/opt/hermes-classroom-connector/register.sh https://YOUR-PORTAL-HOST
```

The optional argument is the non-secret portal HTTPS origin. The enrollment
token is deliberately absent from the command line, examples, and shell
history; `register.sh` prompts for it without echoing.

For managed non-interactive automation, pipe the token from a protected secret
source rather than placing a literal token on the command line:

```bash
protected-secret-source | /opt/hermes-classroom-connector/register.sh https://YOUR-PORTAL-HOST
```

Restrict access to the source and avoid commands that echo the token into logs.

If registration fails during the installer handoff, the connector installation
remains complete. The installer reports the registration failure, prints the
safe retry command, and exits nonzero so automation can detect the incomplete
enrollment.

## What the installer changes

The installer:

- Verifies Hermes on `127.0.0.1:8642`
- Installs missing Python dependencies: `fastapi`, `uvicorn`, `httpx`,
  `websockets`, and `python-multipart`
- Installs reviewed runtime files and the registration command under
  `/opt/hermes-classroom-connector/`
- Creates the connector configuration and random per-VM secrets when absent
- Installs and starts hardened systemd services
- Patches Nginx to publish only `/hermes-classroom/`, preserving a timestamped
  backup
- Offers the secure portal-registration handoff when attached to an interactive
  terminal

When stdin or stdout is not a TTY, installation never waits for input and does
not launch registration. It prints the safe retry command instead.

## Re-running the installer

The installer is idempotent. Reinstalling updates runtime code, preserves
existing connector configuration values (including the shared secret), skips
an already applied Nginx patch, and restarts the services. A legacy config
missing the required local dashboard token receives that new value without
replacing the shared secret or unrelated settings.

## Security model

- Hermes and the connector listen only on `127.0.0.1`.
- Nginx exposes only `/hermes-classroom/`; it does not directly expose Hermes.
- Requests are HMAC-signed with a random per-VM shared secret and protected
  against nonce replay.
- The connector configuration is root-owned with mode `0640`.
- The portal encrypts the connector shared secret at rest.
- Shared secrets and enrollment tokens must never be copied into Git, command
  arguments, shell history, or logs.
- Registration reads the installed protected configuration internally. It does
  not require displaying or manually copying the VM hostname or shared secret.
- Attachments are stored in a protected mode-`0700` directory.
- The systemd service uses hardening including `NoNewPrivileges`,
  `ProtectSystem=strict`, `MemoryDenyWriteExecute`, and no capabilities.

## Service management

```bash
systemctl status hermes-classroom-connector
sudo systemctl restart hermes-classroom-connector
journalctl -u hermes-classroom-connector -f
```

## Files installed

| Path | Purpose |
|---|---|
| `/opt/hermes-classroom-connector/` | Reviewed connector runtime |
| `/opt/hermes-classroom-connector/register.sh` | Secure portal registration command, mode `0755` |
| `/etc/hermes-classroom-connector/connector.env` | Shared secret + config (root-owned, 0640) |
| `/var/lib/hermes-classroom-connector/attachments/` | Protected attachments, mode `0700` |
| `/etc/systemd/system/hermes-classroom-serve.service` | Loopback Hermes service unit |
| `/etc/systemd/system/hermes-classroom-connector.service` | Systemd unit |
| Nginx config (patched in place) | `/hermes-classroom/` proxy location |

## Non-production staging mode

`HERMES_CLASSROOM_STAGE_TEST=1` with `--stage-root` or
`--test-registration-tail` exists only for isolated automated tests. It stages
a deterministic layout or exercises the completion handoff without root,
system, service, network, package, or real-config access. Do not use either
mode to install or register a production VM.
