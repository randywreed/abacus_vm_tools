"""Behavioral tests for the signed /v1/abacus/snapshot endpoint.

These drive the real FastAPI app with a TestClient and a known shared
secret, proving the HMAC boundary works end-to-end and that failures are
generic 503s (no credential leakage). Requires the venv fastapi/httpx
stack; run with ``env -u PYTHONPATH`` to avoid the host python3.11 paths.
"""
import hashlib
import hmac
import os
import secrets
import time
import unittest
from unittest import mock

# The connector module reads this at import time; set it before importing.
os.environ.setdefault("HERMES_CLASSROOM_SHARED_SECRET", "test-shared-secret-0123456789abcdef")
os.environ.setdefault("HERMES_ATTACHMENT_DIR", "/tmp/hermes-classroom-test-attachments")

from fastapi.testclient import TestClient  # noqa: E402

import hermes_classroom_connector as connector  # noqa: E402

PATH = f"{connector.PUBLIC_PREFIX}/v1/abacus/snapshot"
SECRET = os.environ["HERMES_CLASSROOM_SHARED_SECRET"]


def _sign(method: str, path: str, body: bytes = b""):
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(24)
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join((method.upper(), path, timestamp, nonce, digest)).encode("utf-8")
    signature = hmac.new(SECRET.encode("ascii"), canonical, hashlib.sha256).hexdigest()
    return {
        "x-hermes-classroom-timestamp": timestamp,
        "x-hermes-classroom-nonce": nonce,
        "x-hermes-classroom-signature": f"v1={signature}",
    }


def _valid_snapshot():
    return {
        "source": "abacus_credits_by_user",
        "periodType": "rolling_30d",
        "period": {"type": "rolling_30d", "key": "rolling_30d", "window_start": "2026-07-06", "window_end": "2026-08-04"},
        "fetchedAt": 1785000000000,
        "users": [{"email": "alice@school.edu", "routeLlmCredits": 12.5, "totalCredits": 100.0}],
        "stats": {"rows": 1, "skipped": 0, "duplicates": 0},
    }


class SnapshotEndpointBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(connector.app)

    def setUp(self):
        # Reset the VM-local snapshot cache so each test fetches fresh.
        connector._snapshot_cache.update({"expires": 0.0, "value": None})

    def test_signed_get_returns_sanitized_snapshot(self):
        async def fake_snapshot():
            return _valid_snapshot()

        with mock.patch.object(connector, "_abacus_snapshot", side_effect=fake_snapshot):
            response = self.client.get(PATH, headers=_sign("GET", PATH))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["periodType"], "rolling_30d")
        self.assertEqual(payload["users"][0]["email"], "alice@school.edu")
        self.assertNotIn("user", payload["users"][0])

    def test_missing_signature_is_rejected(self):
        response = self.client.get(PATH)
        self.assertEqual(response.status_code, 401)

    def test_wrong_signature_is_rejected(self):
        headers = _sign("GET", PATH)
        headers["x-hermes-classroom-signature"] = "v1=" + "0" * 64
        response = self.client.get(PATH, headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_upstream_failure_is_generic_503_without_secrets(self):
        async def boom():
            raise RuntimeError("upstream exploded s2_super_secret_do_not_leak")

        with mock.patch.object(connector, "_abacus_snapshot", side_effect=boom):
            response = self.client.get(PATH, headers=_sign("GET", PATH))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("s2_super_secret_do_not_leak", response.text)
        self.assertNotIn(SECRET, response.text)


if __name__ == "__main__":
    unittest.main()
