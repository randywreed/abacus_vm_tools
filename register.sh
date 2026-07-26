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
  read -rp "Enter your VM's public hostname or ID (e.g. 123456789 or 123456789.abacusai.cloud): " VM_ID
fi

# Normalize manual input: accept a bare ID, hostname, or full URL.
#   https://4100ca910.abacusai.cloud/
#   4100ca910.abacusai.cloud
#   4100ca910
# all become 4100ca910.abacusai.cloud
if [[ "$VM_ID" =~ ^https?:// ]]; then
  VM_ID="${VM_ID#*://}"
fi
VM_ID="${VM_ID%%/*}"
VM_ID="${VM_ID%%:*}"
if [[ "$VM_ID" == *.abacusai.cloud ]]; then
  CONNECTOR_HOSTNAME="$VM_ID"
else
  CONNECTOR_HOSTNAME="${VM_ID}.abacusai.cloud"
fi

echo -e "${BOLD}Registering connector...${NC}"
echo -e "  Portal:     ${CYAN}${PORTAL_URL}${NC}"
echo -e "  Hostname:   ${CYAN}${CONNECTOR_HOSTNAME}${NC}"
echo -e "  Token:      ${CYAN}${ENROLLMENT_TOKEN:0:12}...${NC}"
echo

RESPONSE_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_FILE"' EXIT
HTTP_STATUS="$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' -X POST "${PORTAL_URL}/api/connector/register" \
  -H "Content-Type: application/json" \
  -d "{\"enrollmentToken\": \"${ENROLLMENT_TOKEN}\", \"connectorHostname\": \"${CONNECTOR_HOSTNAME}\", \"connectorSecret\": \"${CONNECTOR_SECRET}\"}")" || fail "Could not reach the portal at ${PORTAL_URL}."
RESPONSE="$(cat "$RESPONSE_FILE")"

if [[ "$HTTP_STATUS" =~ ^2[0-9][0-9]$ ]] && echo "$RESPONSE" | grep -q '"ok":true'; then
  ok "Connector registered successfully!"
  echo
  echo -e "${BOLD}Next step:${NC} Go back to the portal Workspace and click ${BOLD}Test connection${NC}."
else
  if [[ -n "$RESPONSE" ]]; then
    fail "Registration failed (HTTP ${HTTP_STATUS}): $RESPONSE"
  fi
  fail "Registration failed (HTTP ${HTTP_STATUS}); the portal returned no details."
fi
