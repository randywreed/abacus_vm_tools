#!/usr/bin/env bash
#
# Hermes Classroom — Connector Registration
#
# Sends your VM's hostname and connector secret to the course portal
# using a one-time enrollment token. The portal encrypts your secret
# and never returns it.
#
# Usage:
#   ./register.sh <portal-url> <enrollment-token>
#
# Example:
#   ./register.sh http://localhost:3002 abc123def456...
#
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1" >&2; exit 1; }

PORTAL_URL="${1:?Usage: register.sh <portal-url> <enrollment-token>}"
ENROLLMENT_TOKEN="${2:?Usage: register.sh <portal-url> <enrollment-token>}"

CONFIG_FILE="/etc/hermes-classroom-connector/connector.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
  fail "Connector config not found. Run install.sh first."
fi

CONNECTOR_SECRET="$(grep HERMES_CLASSROOM_SHARED_SECRET= "$CONFIG_FILE" | cut -d= -f2)"
if [[ -z "$CONNECTOR_SECRET" ]]; then
  fail "Could not read connector secret from $CONFIG_FILE"
fi

# Auto-detect hostname from Abacus metadata or derive from instance
VM_ID="$(curl -sf --max-time 5 http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)"
if [[ -z "$VM_ID" ]]; then
  VM_ID="$(hostname -f 2>/dev/null | grep -oP '^\d+' || true)"
fi
if [[ -z "$VM_ID" ]]; then
  read -rp "Enter your VM's public hostname (e.g. 123456789.abacusai.cloud): " VM_ID
fi
CONNECTOR_HOSTNAME="${VM_ID}.abacusai.cloud"

echo -e "${BOLD}Registering connector...${NC}"
echo -e "  Portal:     ${CYAN}${PORTAL_URL}${NC}"
echo -e "  Hostname:   ${CYAN}${CONNECTOR_HOSTNAME}${NC}"
echo -e "  Token:      ${CYAN}${ENROLLMENT_TOKEN:0:12}...${NC}"
echo

RESPONSE=$(curl -sf -X POST "${PORTAL_URL}/api/connector/register" \
  -H "Content-Type: application/json" \
  -d "{\"enrollmentToken\": \"${ENROLLMENT_TOKEN}\", \"connectorHostname\": \"${CONNECTOR_HOSTNAME}\", \"connectorSecret\": \"${CONNECTOR_SECRET}\"}" \
  2>&1) || fail "Registration request failed."

if echo "$RESPONSE" | grep -q '"ok":true'; then
  ok "Connector registered successfully!"
  echo
  echo -e "${BOLD}Next step:${NC} Go back to the portal Workspace and click ${BOLD}Test connection${NC}."
else
  fail "Registration failed: $RESPONSE"
fi
