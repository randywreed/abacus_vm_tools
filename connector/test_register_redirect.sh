#!/usr/bin/env bash
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/register.sh"

grep -q -- '--location' "$SCRIPT"
echo 'PASS: registration follows HTTPS redirects'
