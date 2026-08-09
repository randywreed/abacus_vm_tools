#!/usr/bin/env bash
#
# Hermes Classroom VM Connector — one-click installer
#
# Checks all prerequisites, installs the connector, patches Nginx, starts the
# systemd service, and securely hands off to portal registration.
#
# Run this AFTER clicking the Hermes button in the Abacus console.
#
# Test mode (non-production): `--stage-root ABSOLUTE_TEMP_DIR` stages the
# exact /opt, /etc/systemd, /etc/hermes-classroom-connector, /var/lib runtime
# layout under an empty temp root using the same layout function production
# uses, without any sudo/systemctl/nginx/network/pip operations. It requires
# the HERMES_CLASSROOM_STAGE_TEST=1 test-mode guard and never touches real
# secrets or real randomness.
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
SYSTEMD_DIR="/etc/systemd"
SERVICE_USER="ubuntu"
HERMES_ENV_FILE="/home/${SERVICE_USER}/.hermes/hermes-serve.env"
ABACUS_PYTHON="/opt/abacus-python/bin/python"
WEB_DEPLOYMENT_SKILL_SOURCE="${REPO_DIR}/skills/devops/abacus-vm-web-deployment/SKILL.md"

# ── Test-mode guard / staged root ──────────────────────────────────────────
STAGING=0
STAGE_ROOT=""
STAGE_MARKER=".hermes-classroom-stage-root"
TAIL_TEST=0

usage() { echo "usage: $0 [--stage-root ABSOLUTE_TEMP_DIR | --test-registration-tail]" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage-root)
      [[ $# -ge 2 ]] || usage
      STAGE_ROOT="$2"
      shift 2
      ;;
    --test-registration-tail)
      TAIL_TEST=1
      shift
      ;;
    *)
      usage
      ;;
  esac
done

if [[ "$TAIL_TEST" == "1" ]]; then
  if [[ "${HERMES_CLASSROOM_STAGE_TEST:-0}" != "1" ]]; then
    fail "Refusing --test-registration-tail: HERMES_CLASSROOM_STAGE_TEST=1 test-mode guard is required"
  fi
  [[ -z "$STAGE_ROOT" ]] \
    || fail "Refusing to combine --test-registration-tail with --stage-root"
fi

if [[ -n "$STAGE_ROOT" ]]; then
  if [[ "${HERMES_CLASSROOM_STAGE_TEST:-0}" != "1" ]]; then
    fail "Refusing --stage-root: HERMES_CLASSROOM_STAGE_TEST=1 test-mode guard is required"
  fi
  while [[ "$STAGE_ROOT" == */ ]]; do STAGE_ROOT="${STAGE_ROOT%/}"; done
  [[ "$STAGE_ROOT" != "/" ]] || fail "Refusing --stage-root: '/' is not a valid staging root"
  case "$STAGE_ROOT" in
    /*) ;;
    *) fail "Refusing --stage-root: path must be absolute: ${STAGE_ROOT}" ;;
  esac
  [[ ! -L "$STAGE_ROOT" ]] || fail "Refusing --stage-root: path must not be a symlink"
  if [[ -e "$STAGE_ROOT" ]]; then
    [[ -f "${STAGE_ROOT}/${STAGE_MARKER}" ]] \
      || fail "Refusing --stage-root: existing path was not created by a staging install: ${STAGE_ROOT}"
    [[ -d "$STAGE_ROOT" ]] || fail "Refusing --stage-root: existing path is not a directory"
  else
    STAGE_PARENT="$(dirname "$STAGE_ROOT")"
    [[ -d "$STAGE_PARENT" && ! -L "$STAGE_PARENT" ]] \
      || fail "Refusing --stage-root: parent directory is not a real directory"
  fi
  STAGING=1
  INSTALL_ROOT="${STAGE_ROOT}/opt/hermes-classroom-connector"
  CONFIG_ROOT="${STAGE_ROOT}/etc/hermes-classroom-connector"
  DATA_DIR="${STAGE_ROOT}/var/lib/hermes-classroom-connector"
  SYSTEMD_DIR="${STAGE_ROOT}/etc/systemd"
  SERVICE_OWNER=""
  ROOT_FLAGS=()
  USER_FLAGS=()
else
  SERVICE_OWNER="$SERVICE_USER"
  ROOT_FLAGS=(-o root -g root)
  USER_FLAGS=(-o "$SERVICE_OWNER" -g "$SERVICE_OWNER")
fi

if [[ "$STAGING" == "1" ]]; then
  WEB_DEPLOYMENT_SKILL_ROOT="${STAGE_ROOT}/home/${SERVICE_USER}/.hermes/skills/devops/abacus-vm-web-deployment"
else
  WEB_DEPLOYMENT_SKILL_ROOT="/home/${SERVICE_USER}/.hermes/skills/devops/abacus-vm-web-deployment"
fi

# ── Shared runtime layout engine (production and --stage-root) ─────────────
# Builds the complete connector runtime under the resolved prefixes using real
# install(1) operations and permissions. Production installs under real /opt,
# /etc/systemd, /etc/hermes-classroom-connector, /var/lib; staging installs the
# exact same tree under the supplied empty temp root.
layout_connector_runtime() {
  info "Installing connector files to ${INSTALL_ROOT}..."
  install -d -m 0755 "$INSTALL_ROOT" "$DATA_DIR"

  # Early-fail: verify all required source files exist before installing any.
  local -a REQUIRED_SOURCES=(
    hermes_classroom_connector.py
    app_tunnel.py
    abacus_usage.py
    telemetry.py
    idempotency.py
    session_payloads.py
    streaming_sse.py
    clarify_state.py
    attachments.py
    multipart_uploads.py
    patch_nginx_default.py
    nginx-hermes-classroom.conf
    hermes-classroom-connector.service
    hermes-classroom-serve.service
  )
  [[ -f "${REPO_DIR}/register.sh" ]] || fail "Required source file missing: ${REPO_DIR}/register.sh"
  [[ -f "$WEB_DEPLOYMENT_SKILL_SOURCE" ]] \
    || fail "Required skill file missing: ${WEB_DEPLOYMENT_SKILL_SOURCE}"
  local f
  for f in "${REQUIRED_SOURCES[@]}"; do
    if [[ ! -f "${CONNECTOR_SRC}/${f}" ]]; then
      fail "Required source file missing: ${CONNECTOR_SRC}/${f}"
    fi
  done

  # Executable entrypoints (imported modules are non-executable below).
  for f in hermes_classroom_connector.py abacus_usage.py telemetry.py \
           idempotency.py session_payloads.py \
           streaming_sse.py clarify_state.py; do
    install "${USER_FLAGS[@]}" -m 0755 \
      "${CONNECTOR_SRC}/${f}" "${INSTALL_ROOT}/${f}"
  done

  # Reviewed runtime modules: imported, never executed.
  for f in attachments.py multipart_uploads.py app_tunnel.py; do
    install "${USER_FLAGS[@]}" -m 0644 \
      "${CONNECTOR_SRC}/${f}" "${INSTALL_ROOT}/${f}"
  done

  install -d "${USER_FLAGS[@]}" -m 0755 "$WEB_DEPLOYMENT_SKILL_ROOT"
  install "${USER_FLAGS[@]}" -m 0644 \
    "$WEB_DEPLOYMENT_SKILL_SOURCE" "$WEB_DEPLOYMENT_SKILL_ROOT/SKILL.md"

  install "${ROOT_FLAGS[@]}" -m 0755 \
    "${REPO_DIR}/register.sh" "${INSTALL_ROOT}/register.sh"

  install "${ROOT_FLAGS[@]}" -m 0755 \
    "${CONNECTOR_SRC}/patch_nginx_default.py" "${INSTALL_ROOT}/patch_nginx_default.py"
  install "${ROOT_FLAGS[@]}" -m 0644 \
    "${CONNECTOR_SRC}/nginx-hermes-classroom.conf" "${INSTALL_ROOT}/nginx-hermes-classroom.conf"

  # Protected attachments directory: mode 0700 owned by the service user.
  install -d "${USER_FLAGS[@]}" -m 0700 "$DATA_DIR/attachments"

  # Hardened connector service + target-specific loopback serve unit.
  install -d -m 0755 "$SYSTEMD_DIR" "$SYSTEMD_DIR/system"
  install "${ROOT_FLAGS[@]}" -m 0644 \
    "${CONNECTOR_SRC}/hermes-classroom-serve.service" \
    "$SYSTEMD_DIR/system/hermes-classroom-serve.service"
  install "${ROOT_FLAGS[@]}" -m 0644 \
    "${CONNECTOR_SRC}/hermes-classroom-connector.service" \
    "$SYSTEMD_DIR/system/hermes-classroom-connector.service"

  # Config directory for connector.env.
  if [[ -n "$SERVICE_OWNER" ]]; then
    install -d -m 0750 -o root -g "$SERVICE_OWNER" "$CONFIG_ROOT"
  else
    install -d -m 0750 "$CONFIG_ROOT"
  fi

  ok "Connector files installed"
}

# ── Shared connector.env logic (production and --stage-root) ───────────────
# Production generates a real shared secret + dashboard token only when the
# config is absent and never regenerates present secrets/tokens or deletes
# unrelated keys. Staging never generates real randomness: it writes
# deterministic dummy placeholders, or preserves an existing file byte-for-byte.
write_connector_config() {
  local env_file="${CONFIG_ROOT}/connector.env"
  if [[ -f "$env_file" ]]; then
    ok "Reusing existing connector secret"
    if [[ "$STAGING" != "1" ]] \
      && ! grep -qE '^HERMES_DASHBOARD_SESSION_TOKEN=[0-9a-f]{64}$' "$env_file"; then
      info "Generating a local dashboard token..."
      printf 'HERMES_DASHBOARD_SESSION_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$env_file"
    fi
  elif [[ "$STAGING" == "1" ]]; then
    info "Writing deterministic test placeholders (no real secrets in staging mode)"
    cat > "$env_file" <<'EOF'
HERMES_CLASSROOM_SHARED_SECRET=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
HERMES_DASHBOARD_SESSION_TOKEN=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
HERMES_CLASSROOM_PORT=8765
HERMES_LOCAL_URL=http://127.0.0.1:8642
EOF
  else
    info "Generating connector shared secret..."
    local SECRET DASHBOARD_TOKEN
    SECRET="$(openssl rand -hex 32)"
    DASHBOARD_TOKEN="$(openssl rand -hex 32)"
    cat > "$env_file" <<EOF
HERMES_CLASSROOM_SHARED_SECRET=${SECRET}
HERMES_DASHBOARD_SESSION_TOKEN=${DASHBOARD_TOKEN}
HERMES_CLASSROOM_PORT=8765
HERMES_LOCAL_URL=http://127.0.0.1:8642
HERMES_ENV_FILE=${HERMES_ENV_FILE}
EOF
    ok "Shared secret and local dashboard token generated"
  fi
  if [[ -n "$SERVICE_OWNER" ]]; then
    chown root:"$SERVICE_OWNER" "$env_file"
  fi
  chmod 0640 "${CONFIG_ROOT}/connector.env"
}

# ── Shared config validation ───────────────────────────────────────────────
# Fails BEFORE any service restart and never prints the values.
validate_connector_config() {
  local env_file="${CONFIG_ROOT}/connector.env"
  local secret token
  if [[ ! -f "$env_file" ]]; then
    fail "Connector configuration is missing: ${env_file}"
  fi
  secret="$(grep -E '^HERMES_CLASSROOM_SHARED_SECRET=[0-9a-f]{64}$' "$env_file" | head -1 | cut -d= -f2- || true)"
  token="$(grep -E '^HERMES_DASHBOARD_SESSION_TOKEN=[0-9a-f]{64}$' "$env_file" | head -1 | cut -d= -f2- || true)"
  if [[ -z "$secret" ]]; then
    fail "Connector configuration is invalid: HERMES_CLASSROOM_SHARED_SECRET is missing or malformed (${env_file})"
  fi
  if [[ -z "$token" ]]; then
    fail "Connector configuration is invalid: HERMES_DASHBOARD_SESSION_TOKEN is missing or malformed (${env_file})"
  fi
  ok "Connector configuration validated"
}

# ── Shared production completion and registration handoff ─────────────────
completion_and_registration_handoff() {
  local register_command="${INSTALL_ROOT}/register.sh"
  local answer=""
  local retry_command="/opt/hermes-classroom-connector/register.sh https://YOUR-PORTAL-HOST"

  # The override exists only for guarded, isolated behavioral tests. It is
  # deliberately ignored by the production path, even if present in its env.
  if [[ "$TAIL_TEST" == "1" ]]; then
    register_command="${HERMES_CLASSROOM_REGISTER_TEST_EXECUTABLE:-$register_command}"
  fi

  echo
  echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
  echo -e "${GREEN}${BOLD}  ✓  Installation complete!${NC}"
  echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
  echo
  echo "Create a one-time enrollment token on the portal Workspace page."

  if [[ -t 0 && -t 1 ]]; then
    printf 'Register this VM with the course portal now? [Y/n] '
    IFS= read -r answer
    case "$answer" in
      ""|y|Y|yes|YES|Yes)
        if "$register_command"; then
          ok "Registration complete"
        else
          warn "Registration failed; the connector installation remains complete."
          echo "Retry registration with: ${retry_command}"
          return 1
        fi
        ;;
      *)
        echo "Registration skipped."
        echo "Register later with: ${retry_command}"
        ;;
    esac
  else
    echo "Interactive registration skipped because no terminal is attached."
    echo "Register later with: ${retry_command}"
  fi

  echo
  echo -e "${BOLD}Service management:${NC}"
  echo -e "  Status:   ${CYAN}systemctl status hermes-classroom-connector${NC}"
  echo -e "  Restart:  ${CYAN}sudo systemctl restart hermes-classroom-connector${NC}"
  echo -e "  Logs:     ${CYAN}journalctl -u hermes-classroom-connector -f${NC}"
  echo
}

if [[ "$TAIL_TEST" == "1" ]]; then
  completion_and_registration_handoff
  exit $?
fi

echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Hermes Classroom VM Connector — Installer${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo

if [[ "$STAGING" == "1" ]]; then
  info "Staging mode: ${STAGE_ROOT} (no root, no systemctl/nginx/network/pip)"
  layout_connector_runtime
  write_connector_config
  validate_connector_config
  printf '1\n' > "${STAGE_ROOT}/${STAGE_MARKER}"
  ok "Staged runtime layout complete at ${STAGE_ROOT}"
  exit 0
fi

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

# ── 3. Ensure Hermes serve is reachable on 127.0.0.1:8642 ────────────────
# The Hermes "button" in the Abacus console creates a service and/or env file,
# but the exact mechanism varies between Abacus images. Accept any of these:
#   - hermes-serve.service running on 8642
#   - hermes gateway run / gateway.service on 9119
#   - a running dashboard/serve process on 8642
# Start `hermes serve` on 8642 if needed.

HERMES_ENV_FILE=""
serve_running() { curl -sf --max-time 5 http://127.0.0.1:8642/api/status >/dev/null 2>&1; }
start_hermes_serve() {
  if [[ -x "$ABACUS_PYTHON" ]]; then
    setsid "$ABACUS_PYTHON" -m hermes_cli.main serve --host 127.0.0.1 --port 8642 >/tmp/hermes-classroom-serve.log 2>&1 &
    sleep 3
    if ! serve_running; then
      echo "Nothing useful on stdout, moving on."
    fi
  fi
}

if serve_running; then
  ok "Hermes serve is reachable on 127.0.0.1:8642"
else
  # Search for an env file created by the Hermes button / service
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
  if [[ -z "$HERMES_ENV_FILE" ]] && systemctl --user cat hermes-serve.service &>/dev/null; then
    HERMES_ENV_FILE="$(systemctl --user cat hermes-serve.service 2>/dev/null \
      | grep -oP 'EnvironmentFile=\K.*' | head -1 || true)"
  fi

  start_hermes_serve
fi

if ! serve_running; then
  fail "Hermes serve is not reachable on 127.0.0.1:8642.

       Click the Hermes button in the Abacus console first, then re-run:
         bash ~/abacus_vm_tools/install.sh"
fi

if [[ ! -f "${HERMES_ENV_FILE}" ]]; then
  warn "Hermes env file not found at ${HERMES_ENV_FILE:-<none>}."
  warn "This can happen on some Abacus images. Continuing anyway;"
  warn "if health checks fail later, wait a moment for Hermes setup to"
  warn "finish and then re-run:  bash ~/abacus_vm_tools/install.sh"
  HERMES_ENV_FILE=""
fi
ok "Hermes serve reachable on 127.0.0.1:8642"
if [[ -n "${HERMES_ENV_FILE}" ]]; then
  ok "Hermes serve env: ${HERMES_ENV_FILE}"
else
  warn "Hermes env file still missing; proceeding without it."
fi

HERMES_VERSION=$(curl -sf --max-time 5 http://127.0.0.1:8642/api/status 2>/dev/null \
  | "$ABACUS_PYTHON" -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null \
  || echo "unknown")
ok "Hermes version: ${HERMES_VERSION}"

# ── 5. Python dependencies ────────────────────────────────────────────────
# Explicit module -> package map. The `multipart` module ships with the
# python-multipart package; everything else maps to its same-named package.
declare -A DEP_PACKAGES=(
  [fastapi]=fastapi
  [uvicorn]=uvicorn
  [httpx]=httpx
  [websockets]=websockets
  [multipart]=python-multipart
)
DEP_MODULES=(fastapi uvicorn httpx websockets multipart)
DEPS_MISSING=()
for mod in "${DEP_MODULES[@]}"; do
  if ! "$ABACUS_PYTHON" -c "import $mod" 2>/dev/null; then
    DEPS_MISSING+=("${DEP_PACKAGES[$mod]}")
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
layout_connector_runtime

# ── 8. Generate shared secret (if first install) ───────────────────────────
write_connector_config
validate_connector_config

# ── 9. Install loopback Hermes + connector services ───────────────────────
info "Installing Hermes Classroom services..."

# Older installers started `hermes serve` as root without the connector's
# dashboard token. Replace only that exact legacy loopback process.
pkill -f -- 'hermes_cli.main serve --host 127.0.0.1 --port 8642' 2>/dev/null || true
systemctl daemon-reload
systemctl enable hermes-classroom-serve.service hermes-classroom-connector.service
systemctl restart hermes-classroom-serve.service
sleep 1
if ! systemctl is-active --quiet hermes-classroom-serve.service; then
  fail "Hermes loopback service failed to start. Check: journalctl -u hermes-classroom-serve -e"
fi
ok "Hermes loopback service is running"
ok "Connector service installed and enabled"

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

# ── 12. Completion and secure registration handoff ────────────────────────
completion_and_registration_handoff
