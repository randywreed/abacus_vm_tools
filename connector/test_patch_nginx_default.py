"""Focused regression tests for safe Nginx connector insertion."""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path


FRAGMENT_PATH = Path(__file__).with_name("nginx-hermes-classroom.conf")
FRAMEWORK_TRANSPORT_ENVELOPE = 11 * 1024 * 1024
NGINX_SIZE_RE = re.compile(r"^([0-9]+)([kKmMgG]?)$")
NGINX_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}


def client_body_size(fragment: str) -> tuple[str, int]:
    match = re.search(r"client_max_body_size\s+([^\s;]+)\s*;", fragment)
    if not match:
        raise AssertionError("classroom proxy must set an explicit client_max_body_size")
    token = match.group(1)
    parsed = NGINX_SIZE_RE.fullmatch(token)
    if not parsed:
        raise AssertionError(f"unparseable client_max_body_size {token!r}")
    unit = parsed.group(2).lower()
    return token, int(parsed.group(1)) * NGINX_SIZE_UNITS[unit]


MODULE_PATH = Path(__file__).with_name("patch_nginx_default.py")
SPEC = importlib.util.spec_from_file_location("patch_nginx_default", MODULE_PATH)
assert SPEC and SPEC.loader
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


FRAGMENT = "location ^~ /hermes-classroom/ { return 401; }"

REAL_FRAGMENT = PATCHER.FRAGMENT


def _indent_for_embedding(content: str) -> str:
    return "\n".join(("    " + line if line.strip() else line) for line in content.split("\n"))


def legacy_pair() -> str:
    stale = REAL_FRAGMENT.replace("client_max_body_size 11m;", "client_max_body_size 1m;")
    return _indent_for_embedding(stale)


def current_pair() -> str:
    return _indent_for_embedding(REAL_FRAGMENT)


def legacy_server() -> str:
    return (
        "server {\n"
        "    listen 80 default_server;\n"
        "    server_name _;\n"
        "    root /var/www/html;\n"
        "    gzip on;\n"
        "\n"
        "    # untouched unrelated comment\n"
        + legacy_pair()
        + "    # trailing comment preserved\n"
        "}\n"
    )


class ClassroomUpgradePatchTests(unittest.TestCase):
    """Upgrade a legacy already-deployed classroom pair in the selected server."""

    def setUp(self) -> None:
        self.original_fragment = PATCHER.FRAGMENT
        PATCHER.FRAGMENT = REAL_FRAGMENT

    def tearDown(self) -> None:
        PATCHER.FRAGMENT = self.original_fragment

    def test_legacy_one_megabyte_pair_is_upgraded_to_current_fragment(self) -> None:
        patched = PATCHER.patch_text(legacy_server())
        self.assertIn("client_max_body_size 11m;", patched)
        self.assertNotIn("client_max_body_size 1m;", patched)
        self.assertEqual(patched.count("location ^~ /hermes-classroom/ {"), 1)
        self.assertEqual(patched.count("location = /hermes-classroom {"), 1)

    def test_legacy_upgrade_preserves_unrelated_directives_and_comments(self) -> None:
        patched = PATCHER.patch_text(legacy_server())
        for kept in (
            "root /var/www/html;",
            "gzip on;",
            "server_name _;",
            "# untouched unrelated comment",
            "# trailing comment preserved",
            "return 404;",
        ):
            self.assertIn(kept, patched)

    def test_legacy_upgrade_is_byte_idempotent(self) -> None:
        patched = PATCHER.patch_text(legacy_server())
        self.assertEqual(PATCHER.patch_text(patched), patched)

    def test_fresh_insert_and_reread_is_byte_idempotent(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    server_name _;\n"
            "    root /var/www/html;\n"
            "}\n"
        )
        once = PATCHER.patch_text(source)
        self.assertEqual(PATCHER.patch_text(once), once)

    def test_config_already_at_exact_current_fragment_is_byte_unchanged(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    server_name _;\n"
            "    root /var/www/html;\n"
            + current_pair()
            + "}\n"
        )
        self.assertEqual(PATCHER.patch_text(source), source)

    def test_non_selected_server_classroom_blocks_are_not_modified(self) -> None:
        source = (
            "server {\n"
            "    listen 80;\n"
            "    server_name localhost;\n"
            "    root /usr/share/nginx/html;\n"
            + legacy_pair()
            + "}\n"
            "server {\n"
            "    listen 80 default_server;\n"
            "    server_name _;\n"
            "    root /var/www/html;\n"
            "}\n"
        )
        patched = PATCHER.patch_text(source)
        self.assertIn("client_max_body_size 1m;", patched)
        self.assertIn("client_max_body_size 11m;", patched)
        selected_start, selected_end = PATCHER.selected_block(patched)
        self.assertIn("client_max_body_size 11m;", patched[selected_start:selected_end])
        self.assertNotIn("client_max_body_size 1m;", patched[selected_start:selected_end])

    def test_duplicate_prefix_locations_fail_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 1m;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 11m;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            PATCHER.patch_text(source)

    def test_duplicate_exact_locations_fail_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 1m;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            PATCHER.patch_text(source)

    def test_partial_prefix_only_classroom_block_fails_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 1m;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "partial"):
            PATCHER.patch_text(source)

    def test_partial_exact_only_classroom_block_fails_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "partial"):
            PATCHER.patch_text(source)

    def test_incompatible_prefix_proxy_target_fails_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 1m;\n"
            "        proxy_pass http://127.0.0.1:9999;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            PATCHER.patch_text(source)

    def test_incompatible_exact_return_fails_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 403;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        client_max_body_size 1m;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            PATCHER.patch_text(source)

    def test_incompatible_prefix_missing_body_limit_fails_closed(self) -> None:
        source = (
            "server {\n"
            "    listen 80 default_server;\n"
            "    location = /hermes-classroom {\n"
            "        return 404;\n"
            "    }\n"
            "    location ^~ /hermes-classroom/ {\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_pass http://127.0.0.1:8765;\n"
            "    }\n"
            "}\n"
        )
        with self.assertRaisesRegex(ValueError, "incompatible"):
            PATCHER.patch_text(source)


class NginxPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_fragment = PATCHER.FRAGMENT
        PATCHER.FRAGMENT = FRAGMENT

    def tearDown(self) -> None:
        PATCHER.FRAGMENT = self.original_fragment

    def test_nginx_conf_default_server(self) -> None:
        source = """http {
    server {
        listen 80 default_server;
        root /var/www/html;
    }
}
"""
        patched = PATCHER.patch_text(source)
        self.assertIn("root /var/www/html;", patched)
        self.assertEqual(patched.count(PATCHER.MARKER), 1)

    def test_default_conf_localhost_fallback(self) -> None:
        source = """server {
    listen 80;
    server_name localhost;
    location / { root /usr/share/nginx/html; }
}
"""
        patched = PATCHER.patch_text(source)
        self.assertIn(PATCHER.MARKER, patched)
        self.assertIn("root /usr/share/nginx/html", patched)

    def test_idempotent_in_selected_block(self) -> None:
        source = """server {
    listen 80 default_server;
    location ^~ /hermes-classroom/ { return 401; }
}
"""
        self.assertEqual(PATCHER.patch_text(source), source)

    def test_marker_in_wrong_server_does_not_skip_selected_server(self) -> None:
        source = """server {
    listen 80;
    server_name other;
    location ^~ /hermes-classroom/ { return 401; }
}
server {
    listen 80 default_server;
    server_name _;
}
"""
        patched = PATCHER.patch_text(source)
        self.assertEqual(patched.count(PATCHER.MARKER), 2)
        selected_start, selected_end = PATCHER.selected_block(patched)
        self.assertIn(PATCHER.MARKER, patched[selected_start:selected_end])

    def test_multiple_defaults_are_rejected(self) -> None:
        source = """server {
    listen 80 default_server;
}
server {
    listen 80 default_server;
}
"""
        with self.assertRaisesRegex(ValueError, "at most one"):
            PATCHER.patch_text(source)

    def test_unbalanced_selected_block_is_rejected(self) -> None:
        source = """server {
    listen 80 default_server;
"""
        with self.assertRaisesRegex(ValueError, "balanced"):
            PATCHER.patch_text(source)


class ClassroomProxyBodyLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fragment = FRAGMENT_PATH.read_text(encoding="utf-8")

    def test_proxy_permits_the_bounded_framework_transport_envelope(self) -> None:
        _, limit_bytes = client_body_size(self.fragment)
        self.assertEqual(
            limit_bytes,
            FRAMEWORK_TRANSPORT_ENVELOPE,
            "classroom proxy must permit the same bounded 11 MiB framework "
            "transport envelope used by the portal",
        )

    def test_proxy_body_limit_is_not_one_megabyte_unbounded_or_off(self) -> None:
        token, _ = client_body_size(self.fragment)
        self.assertNotIn(token.lower(), {"1m", "unbounded", "off", "0"})

    def test_classroom_denial_timeouts_buffering_and_headers_stay_unchanged(self) -> None:
        self.assertIn("location = /hermes-classroom {", self.fragment)
        self.assertIn("return 404;", self.fragment)
        self.assertIn("proxy_http_version 1.1;", self.fragment)
        self.assertIn("proxy_connect_timeout 5s;", self.fragment)
        self.assertIn("proxy_send_timeout 30s;", self.fragment)
        self.assertIn("proxy_read_timeout 330s;", self.fragment)
        self.assertIn("proxy_buffering off;", self.fragment)
        for header in (
            "Host $host",
            "X-Real-IP $remote_addr",
            "X-Forwarded-For $proxy_add_x_forwarded_for",
            "X-Forwarded-Proto $scheme",
        ):
            self.assertIn(f"proxy_set_header {header};", self.fragment)


if __name__ == "__main__":
    unittest.main()
