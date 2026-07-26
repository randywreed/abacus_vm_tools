#!/usr/bin/env bash
#
# Hermes Classroom VM Connector — one-click installer
#
# Clones to ~/abacus_vm_tools, checks all prerequisites, installs the
# connector, patches Nginx, starts the systemd service, and prints the
# hostname + generated secret the student needs for portal enrollment.
#
# Run this AFTER clicking the Hermes button in the Abacus console.
#
set -euo pipefail

# ── Colors ─────────────────────────────────────────────────────────────────
BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}●${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1" >&2; exit 1; }

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONNECTOR_SRC="${REPO_DIR}/connector"
INSTALL_ROOT="/opt/hermes-classroom-connector"
CONFIG_ROOT="/etc/hermes-classroom-connector"
DATA_DIR="/var/lib/hermes-classroom-connector"
SERVICE_USER="ubuntu"
HERMES_ENV_FILE="/home/${SERVICE_USER}/.hermes/hermes-serve.env"
ABACUS_PYTHON="/opt/abacus-python/bin/python"

echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Hermes Classroom VM Connector — Installer${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo

# ── 1. Root check ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  warn "This installer needs root. Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

info "Checking prerequisites..."

# ── 2. Abacus Python ───────────────────────────────────────────────────────
if [[ ! -x "$ABACUS_PYTHON" ]]; then
  fail "Abacus Python not found at ${ABACUS_PYTHON}. Is this an Abacus SuperComputer VM?"
fi
ok "Abacus Python: $($ABACUS_PYTHON --version 2>&1)"

# ── 3. Find Hermes serve env file ──────────────────────────────────────────
# The Hermes "button" in the Abacus console creates this file, but its location
# varies between Abacus images. Search common locations before giving up.
HERMES_ENV_FILE=""
for candidate in \
  "/home/${SERVICE_USER}/.hermes/hermes-serve.env" \
  "/home/${SERVICE_USER}/.hermes/.env" \
  "/opt/abacus-python/etc/hermes-serve.env"; do
  if [[ -f "$candidate" ]]; then
    HERMES_ENV_FILE="$candidate"
    break
  fi
done

# Also check the hermes-serve.service unit for an EnvironmentFile directive
if [[ -z "$HERMES_ENV_FILE" ]] && systemctl --user cat hermes-serve.service &>/dev/null; then
  HERMES_ENV_FILE="$(systemctl --user cat hermes-serve.service 2>/dev/null \
    | grep -oP 'EnvironmentFile=\K.*' | head -1 || true)"
fi

if [[ -z "$HERMES_ENV_FILE" ]] || [[ ! -f "$HERMES_ENV_FILE" ]]; then
  fail "Hermes serve env file not found.

       Click the Hermes button in the Abacus console first, then re-run:
         bash ~/abacus_vm_tools/install.sh"
fi
ok "Hermes serve env: ${HERMES_ENV_FILE}"

# ── 4. Hermes serve running on 8642 ──────────────────────────────────────
if ! curl -sf --max-time 5 http://127.0.0.1:8642/api/status >/dev/null 2>&1; then
  warn "Hermes serve is not responding on 127.0.0.1:8642."
  warn "If you just clicked the Hermes button, wait a moment and re-run."
  warn "You can check with:  curl http://127.0.0.1:8642/api/status"
  fail "Hermes serve must be running before installing the connector."
fi
HERMES_VERSION=$(curl -sf http://127.0.0.1:8642/api/status 2>/dev/null \
  | "$ABACUS_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null \
  || echo "unknown")
ok "Hermes serve running (v${HERMES_VERSION}) on 127.0.0.1:8642"

# ── 5. Python dependencies ────────────────────────────────────────────────
DEPS_MISSING=()
for mod in fastapi uvicorn httpx websockets; do
  if ! "$ABACUS_PYTHON" -c "import $mod" 2>/dev/null; then
    DEPS_MISSING+=("$mod")
  fi
done
if [[ ${#DEPS_MISSING[@]} -gt 0 ]]; then
  info "Installing Python dependencies: ${DEPS_MISSING[*]}"
  "$ABACUS_PYTHON" -m pip install --quiet "${DEPS_MISSING[@]}" \
    || fail "Failed to install Python dependencies."
fi
ok "Python dependencies OK"

# ── 6. Nginx ─────────────────────────────────────────────────────────────
if ! command -v nginx >/dev/null 2>&1; then
  fail "Nginx is not installed. Abacus VMs should have it — check your image."
fi
if ! systemctl is-active --quiet nginx; then
  warn "Nginx is not running. Starting it..."
  systemctl start nginx || fail "Could not start Nginx."
fi
ok "Nginx is active"

# ── 7. Install connector files ───────────────────────────────────────────
info "Installing connector to ${INSTALL_ROOT}..."
install -d -m 0755 "$INSTALL_ROOT" "$DATA_DIR"

for f in hermes_classroom_connector.py abacus_usage.py telemetry.py \
         idempotency.py session_payloads.py; do
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
    "${CONNECTOR_SRC}/${f}" "${INSTALL_ROOT}/${f}"
done

install -o root -g root -m 0755 \
  "${CONNECTOR_SRC}/patch_nginx_default.py" "${INSTALL_ROOT}/patch_nginx_default.py"
install -o root -g root -m 0644 \
  "${CONNECTOR_SRC}/nginx-hermes-classroom.conf" "${INSTALL_ROOT}/nginx-hermes-classroom.conf"

ok "Connector files installed"

# ── 8. Generate shared secret (if first install) ───────────────────────────
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_ROOT"

if [[ ! -f "${CONFIG_ROOT}/connector.env" ]]; then
  info "Generating connector shared secret..."
  SECRET="$(openssl rand -hex 32)"
  cat > "${CONFIG_ROOT}/connector.env" <<EOF
HERMES_CLASSROOM_SHARED_SECRET=${SECRET}
HERMES_CLASSROOM_PORT=8765
HERMES_LOCAL_URL=http://127.0.0.1:8642
HERMES_ENV_FILE=${HERMES_ENV_FILE}
EOF
  ok "Shared secret generated"
else
  ok "Reusing existing connector secret"
fi
chown root:"$SERVICE_USER" "${CONFIG_ROOT}/connector.env"
chmod 0640 "${CONFIG_ROOT}/connector.env"

# ── 9. Install systemd service ────────────────────────────────────────────
info "Installing systemd service..."
install -o root -g root -m 0644 \
  "${CONNECTOR_SRC}/hermes-classroom-connector.service" \
  /etc/systemd/system/hermes-classroom-connector.service

systemctl daemon-reload
systemctl enable hermes-classroom-connector.service
ok "Service installed and enabled"

# ── 10. Patch Nginx ───────────────────────────────────────────────────────
info "Patching Nginx configuration..."

# Detect which server block Abacus selects
NGINX_TARGET=""
if grep -Eq 'listen[[:space:]]+80[[:space:]]+default_server[[:space:]]*;' /etc/nginx/nginx.conf; then
  NGINX_TARGET=/etc/nginx/nginx.conf
elif grep -Fq 'server_name  localhost;' /etc/nginx/conf.d/default.conf 2>/dev/null; then
  NGINX_TARGET=/etc/nginx/conf.d/default.conf
else
  fail "Could not identify the Abacus-selected Nginx server block."
fi

# Patch it (idempotent — skips if already patched)
PATCHED_DEFAULT="$(mktemp)"
trap 'rm -f "$PATCHED_DEFAULT"' EXIT
"$ABACUS_PYTHON" "${INSTALL_ROOT}/patch_nginx_default.py" "$NGINX_TARGET" "$PATCHED_DEFAULT"

if diff -q "$NGINX_TARGET" "$PATCHED_DEFAULT" >/dev/null 2>&1; then
  ok "Nginx already patched — skipping"
else
  BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  NGINX_BACKUP="${NGINX_TARGET}.hermes-classroom-backup-${BACKUP_STAMP}"
  cp -a "$NGINX_TARGET" "$NGINX_BACKUP"
  chown root:root "$NGINX_BACKUP"
  chmod 0644 "$NGINX_BACKUP"
  install -o root -g root -m 0644 "$PATCHED_DEFAULT" "$NGINX_TARGET"

  if ! nginx -t 2>&1; then
    warn "Nginx validation failed — restoring backup..."
    install -o root -g root -m 0644 "$NGINX_BACKUP" "$NGINX_TARGET"
    nginx -t || true
    fail "Nginx validation failed. Original config restored from ${NGINX_BACKUP}"
  fi
  ok "Nginx patched and validated (backup: ${NGINX_BACKUP})"
fi

# ── 11. (Re)start the connector service ───────────────────────────────────
info "Starting connector service..."
systemctl restart hermes-classroom-connector.service
sleep 1
if systemctl is-active --quiet hermes-classroom-connector.service; then
  ok "Connector service is running"
else
  fail "Connector service failed to start. Check: journalctl -u hermes-classroom-connector -e"
fi

systemctl reload nginx 2>/dev/null || true
ok "Nginx reloaded"

# ── 12. Determine the public hostname ─────────────────────────────────────
# Abacus VMs have a public HTTPS hostname of the form <id>.abacusai.cloud
# The VM itself doesn't know its own public ID — it's assigned by Abacus.
# We check a few sources, then fall back to prompting the student.
VM_ID="${ABACUS_PUBLIC_VM_ID:-}"

# Try extracting from the SSH known_hosts on the VM (if the student has
# connected to themselves or Abacus recorded it)
if [[ -z "$VM_ID" ]]; then
  VM_ID="$(grep -oP '\K\d{6,}(?=\.ssh[24]?\.abacusai\.cloud)' ~/.ssh/known_hosts 2>/dev/null | head -1 || true)"
fi
if [[ -z "$VM_ID" ]]; then
  # Last resort: ask the user
  warn "Could not auto-detect your VM's public hostname."
  warn "Find it in the Abacus console — it looks like: https://123456789.abacusai.cloud"
  warn "Or check your SSH connection string: ssh ubuntu@<id>.ssh4.abacusai.cloud"
  HOSTNAME_MSG="Look in the Abacus console for your VM's public URL (e.g. https://123456789.abacusai.cloud)"
else
  HOSTNAME_MSG="${VM_ID}.abacusai.cloud"
fi

# ── 13. Read the shared secret for display ────────────────────────────────
DISPLAY_SECRET="$(grep HERMES_CLASSROOM_SHARED_SECRET= "${CONFIG_ROOT}/connector.env" | cut -d= -f2)"

# ── 14. Success message ───────────────────────────────────────────────────
echo
echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✓  Installation complete!${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo
echo -e "${BOLD}Your VM connector hostname:${NC}"
echo -e "  ${CYAN}${HOSTNAME_MSG}${NC}"
echo
echo -e "${BOLD}Your connector shared secret (shown once):${NC}"
echo -e "  ${CYAN}${DISPLAY_SECRET}${NC}"
echo
echo -e "${BOLD}Next steps:${NC}"
echo -e "  1. Open the course portal in your browser"
echo -e "  2. Go to the Workspace page"
echo -e "  3. Click ${BOLD}Create token${NC} to get a one-time enrollment token"
echo -e "  4. Register your connector using the hostname and secret above"
echo -e "  5. Click ${BOLD}Test connection${NC} to verify"
echo
echo -e "${BOLD}Service management:${NC}"
echo -e "  Status:   ${CYAN}systemctl status hermes-classroom-connector${NC}"
echo -e "  Restart:  ${CYAN}sudo systemctl restart hermes-classroom-connector${NC}"
echo -e "  Logs:     ${CYAN}journalctl -u hermes-classroom-connector -f${NC}"
echo
