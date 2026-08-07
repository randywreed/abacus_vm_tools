#!/usr/bin/env bash

set -u
umask 077

usage() {
    printf '%s\n' 'Usage: register.sh [PORTAL_HTTPS_ORIGIN]' >&2
}

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

interrupted() {
    printf '\n%s\n' 'Registration interrupted.' >&2
    exit 130
}
trap interrupted INT TERM HUP

if (( $# > 1 )); then
    usage
    exit 1
fi

portal=${1-}
if [[ -z $portal ]]; then
    if [[ ! -t 0 ]]; then
        usage
        exit 1
    fi
    printf '%s' 'Portal HTTPS origin: ' >&2
    if ! IFS= read -r portal; then
        fail 'Portal origin was not provided.'
    fi
fi

if ! portal=$(python3 - "$portal" <<'PY'
import ipaddress
import re
import sys

value = sys.argv[1]
match = re.fullmatch(r"https://([^/]+)/?", value, re.IGNORECASE | re.ASCII)
if not match:
    raise SystemExit(1)
host = match.group(1).lower()
label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", host, re.ASCII):
    raise SystemExit(1)
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    raise SystemExit(1)
print("https://" + host)
PY
); then
    fail 'Invalid portal HTTPS origin.'
fi

test_mode=0
if [[ ${HERMES_CLASSROOM_REGISTER_TEST-} == 1 ]]; then
    test_mode=1
fi

config_path=/etc/hermes-classroom-connector/connector.env
curl_bin=curl
host_candidate=
if (( test_mode )); then
    config_path=${HERMES_CLASSROOM_REGISTER_CONFIG:-$config_path}
    curl_bin=${HERMES_CLASSROOM_REGISTER_CURL:-$curl_bin}
    host_candidate=${HERMES_CLASSROOM_REGISTER_HOSTNAME-}
fi

[[ -f $config_path ]] || fail 'Connector configuration is unavailable.'
if ! connector_secret=$(python3 - "$config_path" <<'PY'
import re
import sys

try:
    with open(sys.argv[1], "rb") as stream:
        data = stream.read(16385)
except OSError:
    raise SystemExit(1)
if len(data) > 16384:
    raise SystemExit(1)
key = b"HERMES_CLASSROOM_SHARED_SECRET"
candidates = []
for line in data.splitlines():
    stripped = line.lstrip()
    if (stripped == key or stripped.startswith(key + b"=") or
            stripped.startswith(key + b" ") or stripped.startswith(key + b"\t")):
        candidates.append(line)
if len(candidates) != 1:
    raise SystemExit(1)
match = re.fullmatch(rb"HERMES_CLASSROOM_SHARED_SECRET=([0-9a-f]{64})", candidates[0])
if match is None:
    raise SystemExit(1)
sys.stdout.buffer.write(match.group(1))
PY
); then
    fail 'Connector configuration is invalid.'
fi

normalize_host() {
    python3 - "$1" <<'PY'
import ipaddress
import re
import sys

value = sys.argv[1]
url = re.fullmatch(r"https://([^/]+)/?", value, re.IGNORECASE | re.ASCII)
if url:
    host = url.group(1).lower()
elif re.fullmatch(r"[^/]+", value, re.ASCII):
    host = value.lower()
else:
    raise SystemExit(1)
label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if "." not in host:
    if not re.fullmatch(label, host, re.ASCII):
        raise SystemExit(1)
    host += ".abacusai.cloud"
if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", host, re.ASCII):
    raise SystemExit(1)
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    raise SystemExit(1)
print(host)
PY
}

connector_hostname=
if [[ -n $host_candidate ]]; then
    connector_hostname=$(normalize_host "$host_candidate") || fail 'Invalid connector hostname.'
else
    metadata_host=
    if command -v curl >/dev/null 2>&1; then
        metadata_host=$(curl --silent --fail --connect-timeout 1 --max-time 2 \
            --noproxy '*' \
            'http://169.254.169.254/latest/meta-data/instance-id' 2>/dev/null) || metadata_host=
    fi
    if [[ -n $metadata_host ]]; then
        connector_hostname=$(normalize_host "$metadata_host") || connector_hostname=
    fi
    if [[ -z $connector_hostname ]] && command -v curl >/dev/null 2>&1; then
        metadata_host=$(curl --silent --fail --connect-timeout 1 --max-time 2 \
            --noproxy '*' \
            --header 'Metadata-Flavor: Google' \
            'http://metadata.google.internal/computeMetadata/v1/instance/hostname' 2>/dev/null) || metadata_host=
        if [[ -n $metadata_host ]]; then
            connector_hostname=$(normalize_host "$metadata_host") || connector_hostname=
        fi
    fi
    if [[ -z $connector_hostname ]]; then
        system_host=$(hostname -f 2>/dev/null) || system_host=$(hostname 2>/dev/null) || system_host=
        if [[ -n $system_host ]]; then
            connector_hostname=$(normalize_host "$system_host") || connector_hostname=
        fi
    fi
    if [[ -z $connector_hostname && -t 0 ]]; then
        printf '%s' 'Connector hostname: ' >&2
        if IFS= read -r host_candidate; then
            connector_hostname=$(normalize_host "$host_candidate") || connector_hostname=
        fi
    fi
    [[ -n $connector_hostname ]] || fail 'A valid connector hostname is required.'
fi

token=
if [[ -t 0 ]]; then
    printf '%s' 'Registration token: ' >&2
    if ! IFS= read -r -s token; then
        printf '\n' >&2
        fail 'Registration token was not provided.'
    fi
    printf '\n' >&2
else
    if ! token=$(python3 -c 'import re
import sys

line = sys.stdin.buffer.readline(4098)
if len(line) > 4097 or not line:
    raise SystemExit(1)
if line.endswith(b"\n"):
    line = line[:-1]
if line.endswith(b"\r") or sys.stdin.buffer.read(1):
    raise SystemExit(1)
if not 16 <= len(line) <= 4096:
    raise SystemExit(1)
if not re.fullmatch(rb"[A-Za-z0-9._~-]+", line):
    raise SystemExit(1)
sys.stdout.buffer.write(line)'
    ); then
        fail 'Invalid registration token.'
    fi
fi

if (( ${#token} < 16 || ${#token} > 4096 )) || [[ ! $token =~ ^[A-Za-z0-9._~-]+$ ]]; then
    token=
    fail 'Invalid registration token.'
fi

if [[ $curl_bin == */* ]]; then
    [[ -x $curl_bin ]] || fail 'Registration client is unavailable.'
else
    curl_bin=$(command -v "$curl_bin") || fail 'Registration client is unavailable.'
fi

temp_dir=$(mktemp -d) || fail 'Unable to prepare registration request.'
cleanup() {
    token=
    connector_secret=
    rm -f -- "$temp_dir/request.json" "$temp_dir/response.json"
    rmdir -- "$temp_dir" 2>/dev/null || true
}
trap cleanup EXIT
request_file=$temp_dir/request.json
response_file=$temp_dir/response.json

if ! {
    printf '%s\n' "$token"
    printf '%s\n' "$connector_hostname"
    printf '%s\n' "$connector_secret"
} | python3 -c '
import json
import sys

values = []
for _ in range(3):
    line = sys.stdin.buffer.readline(4098)
    if not line.endswith(b"\n"):
        raise SystemExit(1)
    try:
        values.append(line[:-1].decode("ascii"))
    except UnicodeDecodeError:
        raise SystemExit(1)
if sys.stdin.buffer.read(1):
    raise SystemExit(1)
token, hostname, secret = values
json.dump({
    "enrollmentToken": token,
    "connectorHostname": hostname,
    "connectorSecret": secret,
}, sys.stdout, separators=(",", ":"))
' >"$request_file"; then
    fail 'Unable to prepare registration request.'
fi

http_status=$(
    "$curl_bin" \
        --silent \
        --request POST \
        --proto '=https' \
        --connect-timeout 10 \
        --max-time 30 \
        --max-filesize 65536 \
        --header 'Content-Type: application/json' \
        --data-binary "@$request_file" \
        --output "$response_file" \
        --write-out '%{http_code}' \
        "$portal/api/connector/register" 2>/dev/null
) || fail 'Registration request failed.'

[[ $http_status =~ ^[0-9]{3}$ ]] || fail 'Registration request failed.'
[[ $http_status =~ ^2[0-9][0-9]$ ]] || fail "Registration failed (HTTP $http_status)."
[[ -f $response_file ]] || fail 'Registration response was invalid.'
response_size=$(wc -c <"$response_file") || fail 'Registration response was invalid.'
(( response_size <= 65536 )) || fail 'Registration response was too large.'

if ! python3 - "$response_file" <<'PY' >/dev/null 2>&1
import json
import sys

try:
    with open(sys.argv[1], "rb") as stream:
        result = json.load(stream)
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(1)
if type(result) is not dict or result.get("ok") is not True:
    raise SystemExit(1)
PY
then
    fail 'Registration response was invalid.'
fi

printf 'Connector registered successfully for %s.\n' "$connector_hostname"
