#!/usr/bin/env python3
"""Black-box integration tests for register.sh."""

import json
import os
import pty
import select
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "register.sh"
USAGE = "Usage: register.sh [PORTAL_HTTPS_ORIGIN]"
TOKEN = "dummy-token_1234567890"
SECRET = "a" * 64
REAL_CONFIG = f"""HERMES_CLASSROOM_SHARED_SECRET={SECRET}
HERMES_DASHBOARD_SESSION_TOKEN={'b' * 64}
HERMES_CLASSROOM_PORT=8765
HERMES_LOCAL_URL=http://127.0.0.1:8642
HERMES_ENV_FILE=/home/ubuntu/.hermes/hermes-serve.env
"""


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "connector.env"
        self.config.write_text(REAL_CONFIG, encoding="ascii")
        self.calls = self.dir / "curl.calls"
        self.payload = self.dir / "payload.json"
        self.response = self.dir / "response.json"
        self.response.write_text('{"ok":true}\n', encoding="ascii")
        self.status = self.dir / "status"
        self.status.write_text("201\n", encoding="ascii")
        self.curl = self.dir / "curl"
        self.curl.write_text(
            """#!/bin/sh
set -eu
printf '%s\\n' "$@" >>"$REGISTER_CALLS"
printf '%s\\n' -- END >>"$REGISTER_CALLS"
out=
payload=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output|-o) out=$2; shift 2 ;;
    --write-out|-w) shift 2 ;;
    --data-binary) payload=${2#@}; shift 2 ;;
    *) shift ;;
  esac
done
cp "$payload" "$REGISTER_PAYLOAD"
stat -c '%a' "$payload" >"$REGISTER_PAYLOAD.mode"
cp "$REGISTER_RESPONSE" "$out"
tr -d '\\n' <"$REGISTER_STATUS"
""",
            encoding="ascii",
        )
        self.curl.chmod(0o755)

    def env(self, guarded=True):
        env = os.environ.copy()
        env.update(
            {
                "HERMES_CLASSROOM_REGISTER_CONFIG": str(self.config),
                "HERMES_CLASSROOM_REGISTER_HOSTNAME": "Class-7.ABACUSAI.CLOUD",
                "HERMES_CLASSROOM_REGISTER_CURL": str(self.curl),
                "REGISTER_CALLS": str(self.calls),
                "REGISTER_PAYLOAD": str(self.payload),
                "REGISTER_RESPONSE": str(self.response),
                "REGISTER_STATUS": str(self.status),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if guarded:
            env["HERMES_CLASSROOM_REGISTER_TEST"] = "1"
        else:
            env.pop("HERMES_CLASSROOM_REGISTER_TEST", None)
        return env

    def run_script(self, args=(), data=b"", env=None, timeout=4):
        return subprocess.run(
            [str(SCRIPT), *args],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env(),
            timeout=timeout,
            check=False,
        )

    def call_args(self):
        lines = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines.count("END"), 1, lines)
        return [line for line in lines if line != "END"]

    def test_usage_and_token_is_never_an_argument(self):
        cp = self.run_script(["https://portal.example", "forbidden-token"])
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual(cp.stderr.decode().strip(), USAGE)
        self.assertNotIn(b"forbidden-token", cp.stdout + cp.stderr)

    def test_missing_portal_on_non_tty_fails_promptly(self):
        cp = self.run_script(data=TOKEN.encode() + b"\n")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn(USAGE.encode(), cp.stderr)

    def test_portal_validation(self):
        bad = [
            "http://portal.example", "https://portal.example/path",
            "https://portal.example:443", "https://127.0.0.1",
            "https://user@portal.example", "https://*.example",
            "https://portal.example?q=1", " https://portal.example",
            "https://-bad.example", "https://bad_.example",
        ]
        for value in bad:
            with self.subTest(value=value):
                cp = self.run_script([value], TOKEN.encode() + b"\n")
                self.assertNotEqual(cp.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_non_tty_success_and_safe_single_curl_call(self):
        cp = self.run_script(["https://PORTAL.Example/"], TOKEN.encode() + b"\n")
        self.assertEqual(cp.returncode, 0, cp.stderr.decode())
        transcript = cp.stdout + cp.stderr
        self.assertNotIn(TOKEN.encode(), transcript)
        self.assertNotIn(SECRET.encode(), transcript)
        args = self.call_args()
        self.assertIn("--proto", args)
        self.assertIn("=https", args)
        self.assertNotIn("--location", args)
        self.assertNotIn("--proto-redir", args)
        self.assertIn("https://portal.example/api/connector/register", args)
        self.assertTrue(any(a.startswith("@") for a in args), args)
        payload = json.loads(self.payload.read_text(encoding="utf-8"))
        self.assertEqual(
            payload,
            {
                "enrollmentToken": TOKEN,
                "connectorHostname": "class-7.abacusai.cloud",
                "connectorSecret": SECRET,
            },
        )
        self.assertEqual(
            self.payload.with_suffix(".json.mode").read_text(encoding="ascii").strip(),
            "600",
        )

    def test_token_validation_and_exactly_one_line(self):
        bad = [b"\n", b"short\n", b"bad token-that-is-long\n", b"x" * 4097 + b"\n",
               TOKEN.encode() + b"\nsecond\n", b"control-abcdefghijkl\x01\n"]
        for value in bad:
            with self.subTest(size=len(value)):
                cp = self.run_script(["https://portal.example"], value)
                self.assertNotEqual(cp.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_host_normalization_and_rejection(self):
        for host in ["Class-7", "HTTPS://Class-7.ABACUSAI.CLOUD/", "Class-7.Example"]:
            with self.subTest(host=host):
                env = self.env()
                env["HERMES_CLASSROOM_REGISTER_HOSTNAME"] = host
                cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n", env)
                self.assertEqual(cp.returncode, 0, cp.stderr.decode())
                self.calls.unlink()
        for host in ["node.example:443", "node.example/path", "a@node.example",
                     "node.example?q", "*.example", "10.0.0.1", "node.example.bad suffix"]:
            with self.subTest(host=host):
                env = self.env()
                env["HERMES_CLASSROOM_REGISTER_HOSTNAME"] = host
                cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n", env)
                self.assertNotEqual(cp.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_real_installer_config_allows_unrelated_lines(self):
        self.config.write_text(
            "COMMENT=this-is-not-a-secret\nHERMES_CLASSROOM_SHARED_SECRET_BACKUP=unused\n"
            + REAL_CONFIG + "TRAILING_SETTING=yes\n",
            encoding="ascii",
        )
        cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n")
        self.assertEqual(cp.returncode, 0, cp.stderr.decode())

    def test_config_requires_one_valid_shared_secret_assignment(self):
        invalid = [
            REAL_CONFIG.replace(f"HERMES_CLASSROOM_SHARED_SECRET={SECRET}\n", ""),
            REAL_CONFIG.replace(SECRET, "xyz", 1),
            REAL_CONFIG.replace(SECRET, SECRET.upper(), 1),
            REAL_CONFIG + f"HERMES_CLASSROOM_SHARED_SECRET={'c' * 64}\n",
            f" HERMES_CLASSROOM_SHARED_SECRET={SECRET}\n" + REAL_CONFIG,
            f"HERMES_CLASSROOM_SHARED_SECRET={SECRET}x\n",
            "X" * 16385 + "\n" + REAL_CONFIG,
        ]
        for content in invalid:
            with self.subTest(content=content[:20]):
                self.config.write_text(content, encoding="ascii")
                cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n")
                self.assertNotEqual(cp.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_link_local_instance_id_precedes_system_hostname(self):
        metadata_calls = self.dir / "metadata.calls"
        metadata_bin = self.dir / "metadata-bin"
        metadata_bin.mkdir()
        metadata_curl = metadata_bin / "curl"
        metadata_curl.write_text(
            """#!/bin/sh
set -eu
printf '%s\\n' "$@" >>"$METADATA_CALLS"
case " $* " in
  *' http://169.254.169.254/latest/meta-data/instance-id '*)
    printf '%s' 'i-0AbC123'
    exit 0
    ;;
esac
exit 1
""",
            encoding="ascii",
        )
        metadata_curl.chmod(0o755)
        env = self.env()
        env.pop("HERMES_CLASSROOM_REGISTER_HOSTNAME")
        env["PATH"] = str(metadata_bin) + os.pathsep + env["PATH"]
        env["METADATA_CALLS"] = str(metadata_calls)
        cp = self.run_script(
            ["https://portal.example"], TOKEN.encode() + b"\n", env
        )
        self.assertEqual(cp.returncode, 0, cp.stderr.decode())
        payload = json.loads(self.payload.read_text(encoding="utf-8"))
        self.assertEqual(payload["connectorHostname"], "i-0abc123.abacusai.cloud")
        calls = metadata_calls.read_text(encoding="utf-8")
        self.assertIn("--connect-timeout\n1\n", calls)
        self.assertIn("--max-time\n2\n", calls)
        self.assertIn(
            "http://169.254.169.254/latest/meta-data/instance-id", calls
        )
        self.assertNotIn(TOKEN, calls)
        self.assertNotIn(SECRET, calls)

    def test_json_helper_does_not_receive_secrets_in_argv_or_environment(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("REGISTER_TOKEN=$token", source)
        self.assertNotIn("REGISTER_SECRET=$connector_secret", source)
        self.assertNotIn('python3 - "$token"', source)
        self.assertNotIn('python3 - "$connector_secret"', source)

    def test_overrides_are_not_honored_without_guard(self):
        cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n", self.env(False))
        self.assertNotEqual(cp.returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_http_or_json_failure_never_dumps_body_or_secrets(self):
        marker = "RAW-BODY-MUST-NOT-PRINT"
        self.response.write_text(marker + TOKEN + SECRET, encoding="ascii")
        self.status.write_text("500\n", encoding="ascii")
        cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n")
        output = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn(marker.encode(), output)
        self.assertNotIn(TOKEN.encode(), output)
        self.assertNotIn(SECRET.encode(), output)
        self.assertLess(len(output), 1024)

        self.response.write_text('{"ok":1}', encoding="ascii")
        self.status.write_text("200\n", encoding="ascii")
        self.calls.unlink(missing_ok=True)
        cp = self.run_script(["https://portal.example"], TOKEN.encode() + b"\n")
        self.assertNotEqual(cp.returncode, 0)

    def test_tty_token_is_hidden_and_interrupt_is_clean(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [str(SCRIPT), "https://portal.example"],
            stdin=slave, stdout=slave, stderr=slave,
            env=self.env(), start_new_session=True,
        )
        os.close(slave)
        transcript = bytearray()
        deadline = time.monotonic() + 4
        try:
            while b"Registration token: " not in transcript and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        transcript.extend(os.read(master, 4096))
                    except OSError:
                        break
            self.assertIn(b"Registration token: ", transcript)
            os.write(master, TOKEN.encode())
            time.sleep(0.1)
            os.write(master, b"\x03")
            proc.wait(timeout=3)
            while True:
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    break
                try:
                    transcript.extend(os.read(master, 4096))
                except OSError:
                    break
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            os.close(master)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(TOKEN.encode(), transcript)
        self.assertNotIn(b"Traceback", transcript)
        self.assertLess(len(transcript), 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
