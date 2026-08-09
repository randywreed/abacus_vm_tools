"""Behavioral endpoint tests for the app tunnel routes.

Drives the real FastAPI app with a TestClient and a real local upstream HTTP
server, proving:

- registration is loopback-gated (requests carrying nginx-forwarded headers
  are rejected, so the public internet cannot register tunnels);
- invalid names/ports and reserved infrastructure ports are rejected;
- the public proxy path forwards to the registered loopback port with the
  remaining path, query string, method, and body;
- unknown apps 404 and dead upstreams produce a generic 502 (no leakage).
"""
import http.server
import asyncio
import os
import socketserver
import threading
import unittest
from unittest import mock

os.environ.setdefault("HERMES_CLASSROOM_SHARED_SECRET", "test-shared-secret-0123456789abcdef")
os.environ.setdefault("HERMES_ATTACHMENT_DIR", "/tmp/hermes-classroom-test-attachments")

from fastapi.testclient import TestClient  # noqa: E402

import hermes_classroom_connector as connector  # noqa: E402
from app_tunnel import RESERVED_TUNNEL_PORTS  # noqa: E402

APPS_PATH = f"{connector.PUBLIC_PREFIX}/v1/apps"
PROXY_PATH = f"{connector.PUBLIC_PREFIX}/apps"


class EchoHandler(http.server.BaseHTTPRequestHandler):
    """Minimal upstream that echoes method, path, query, and body."""

    def _respond(self):
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        payload = (
            f"method={self.command}\n"
            f"path={self.path}\n"
            f"body={body.decode('utf-8', 'replace')}\n"
            f"x-forwarded-proto={self.headers.get('x-forwarded-proto', '')}\n"
            f"x-forwarded-host={self.headers.get('x-forwarded-host', '')}\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond
    do_DELETE = _respond
    do_PATCH = _respond

    def log_message(self, *args):
        pass


class UpstreamServer:
    def __init__(self):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), EchoHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _force_client_host(app, host: str):
    """Wrap an ASGI app so TestClient requests carry a chosen peer address.

    Starlette's TestClient always sets the ASGI ``client`` to
    ``("testclient", 50000)``.  The connector's loopback admin gate checks the
    peer address, so these tests must present a realistic loopback peer (and
    one test presents a non-loopback peer to prove the gate rejects it).
    """
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = (host, 54321)
        await app(scope, receive, send)
    return wrapped


class TunnelEndpointBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(_force_client_host(connector.app, "127.0.0.1"))

    def setUp(self):
        connector._tunnel_registry._entries.clear()

    # ── Registration gating ──────────────────────────────────────────────

    def test_register_accepts_local_request(self):
        with UpstreamServer() as upstream:
            response = self.client.post(
                APPS_PATH,
                json={"name": "game", "port": upstream.port},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertIn(f"{PROXY_PATH}/game/", data["url"])

    def test_register_rejects_nginx_forwarded_request(self):
        response = self.client.post(
            APPS_PATH,
            json={"name": "game", "port": 8767},
            headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(connector._tunnel_registry.get("game"), None)

    def test_register_rejects_forwarded_proto_alone(self):
        # nginx sets X-Forwarded-Proto; local agent curl does not.
        response = self.client.post(
            APPS_PATH,
            json={"name": "game", "port": 8767},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status_code, 403)

    def test_register_rejects_invalid_name(self):
        response = self.client.post(APPS_PATH, json={"name": "Bad Name", "port": 8767})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(connector._tunnel_registry.list(), [])

    def test_register_rejects_reserved_port(self):
        response = self.client.post(APPS_PATH, json={"name": "game", "port": 8642})
        self.assertEqual(response.status_code, 400)

    def test_register_rejects_all_reserved_infrastructure_ports(self):
        for port in sorted(RESERVED_TUNNEL_PORTS):
            with self.subTest(port=port):
                response = self.client.post(APPS_PATH, json={"name": "game", "port": port})
                self.assertEqual(response.status_code, 400)

    def test_register_rejects_non_loopback_peer_even_without_forwarded_headers(self):
        client = TestClient(_force_client_host(connector.app, "203.0.113.9"))
        response = client.post(APPS_PATH, json={"name": "game", "port": 8767})
        self.assertEqual(response.status_code, 403)

    def test_register_rejects_non_json_body(self):
        response = self.client.post(APPS_PATH, content=b"not json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)

    def test_register_rejects_missing_fields(self):
        response = self.client.post(APPS_PATH, json={"name": "game"})
        self.assertEqual(response.status_code, 400)

    # ── Listing and deletion ─────────────────────────────────────────────

    def test_list_is_loopback_gated(self):
        response = self.client.get(APPS_PATH, headers={"X-Real-IP": "203.0.113.9"})
        self.assertEqual(response.status_code, 403)

    def test_list_returns_registered_apps(self):
        connector._tunnel_registry.register("game", 8767)
        response = self.client.get(APPS_PATH)
        self.assertEqual(response.status_code, 200)
        entries = {e["name"]: e["port"] for e in response.json()["apps"]}
        self.assertEqual(entries, {"game": 8767})

    def test_delete_is_loopback_gated(self):
        response = self.client.delete(f"{APPS_PATH}/game", headers={"X-Real-IP": "203.0.113.9"})
        self.assertEqual(response.status_code, 403)

    def test_delete_removes_app(self):
        connector._tunnel_registry.register("game", 8767)
        response = self.client.delete(f"{APPS_PATH}/game")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(connector._tunnel_registry.get("game"), None)

    def test_delete_unknown_app_is_404(self):
        response = self.client.delete(f"{APPS_PATH}/nope")
        self.assertEqual(response.status_code, 404)

    # ── Public proxy path ────────────────────────────────────────────────

    def test_proxy_forwards_get_with_path_and_query(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(f"{PROXY_PATH}/game/level/1?x=2")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("method=GET", body)
        self.assertIn("path=/level/1?x=2", body)

    def test_proxy_preserves_percent_encoded_path_bytes(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            # Raw >=0x80 byte in the path must not be re-encoded as UTF-8.
            response = self.client.get(f"{PROXY_PATH}/game/x%80y")
        self.assertEqual(response.status_code, 200)
        self.assertIn("path=/x%80y", response.text)

    def test_proxy_encoded_separator_at_app_boundary_is_not_a_500(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(f"{PROXY_PATH}/game%2Fchild")
        self.assertEqual(response.status_code, 200)
        self.assertIn("path=/%2Fchild", response.text)

    def test_proxy_forwards_post_body_and_method(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.post(f"{PROXY_PATH}/game/api", json={"a": 1})
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("method=POST", body)
        self.assertIn('body={"a":1}', body)

    def test_proxy_root_path_without_trailing_slash(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(f"{PROXY_PATH}/game")
        self.assertEqual(response.status_code, 200)
        self.assertIn("path=/", response.text)

    def test_proxy_slot_is_held_until_stream_closes(self):
        async def scenario():
            from starlette.requests import Request

            original_semaphore = connector._tunnel_proxy_semaphore
            connector._tunnel_proxy_semaphore = asyncio.Semaphore(1)
            connector._tunnel_registry.register("game", 8767)

            class FakeUpstream:
                def __init__(self):
                    self.started = asyncio.Event()
                    self.release = asyncio.Event()
                    self.status_code = 200
                    self.headers = {}

                async def aiter_raw(self):
                    self.started.set()
                    yield b"ok"
                    await self.release.wait()

                async def aclose(self):
                    pass

            class FakeClient:
                instances = []

                def __init__(self, **_kwargs):
                    self.upstream = FakeUpstream()
                    self.instances.append(self)

                def build_request(self, *_args, **_kwargs):
                    return object()

                async def send(self, *_args, **_kwargs):
                    return self.upstream

                async def aclose(self):
                    pass

            def make_request():
                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                scope = {
                    "type": "http",
                    "method": "GET",
                    "scheme": "http",
                    "path": f"{PROXY_PATH}/game/",
                    "raw_path": f"{PROXY_PATH}/game/".encode(),
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"host", b"course.example.edu")],
                    "client": ("127.0.0.1", 40000),
                    "server": ("127.0.0.1", 8765),
                    "http_version": "1.1",
                }
                return Request(scope, receive)

            async def consume(response):
                return [chunk async for chunk in response.body_iterator]

            try:
                with mock.patch.object(connector.httpx, "AsyncClient", FakeClient):
                    first = await connector.tunnel_proxy(make_request(), "game")
                    first_consumer = asyncio.create_task(consume(first))
                    await FakeClient.instances[0].upstream.started.wait()

                    second_request = asyncio.create_task(
                        connector.tunnel_proxy(make_request(), "game")
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(second_request.done())

                    FakeClient.instances[0].upstream.release.set()
                    await first_consumer
                    second = await asyncio.wait_for(second_request, timeout=1)
                    second_consumer = asyncio.create_task(consume(second))
                    await FakeClient.instances[1].upstream.started.wait()
                    FakeClient.instances[1].upstream.release.set()
                    await second_consumer
            finally:
                connector._tunnel_proxy_semaphore = original_semaphore

        asyncio.run(scenario())

    def test_proxy_unknown_app_404(self):
        response = self.client.get(f"{PROXY_PATH}/ghost/")
        self.assertEqual(response.status_code, 404)

    def test_proxy_invalid_name_404(self):
        response = self.client.get(f"{PROXY_PATH}/Bad%20Name/")
        self.assertEqual(response.status_code, 404)

    def test_proxy_dead_upstream_is_generic_502(self):
        # Register a port with nothing listening on it.
        connector._tunnel_registry.register("dead", 13999)
        response = self.client.get(f"{PROXY_PATH}/dead/")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("secret", response.text.lower())

    def test_proxy_forwards_public_host_as_x_forwarded_host(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(f"{PROXY_PATH}/game/", headers={"Host": "course.example.edu"})
        self.assertIn("x-forwarded-host=course.example.edu", response.text)

    def test_proxy_overrides_spoofed_x_forwarded_host_exactly_once(self):
        # A public client can send its own x-forwarded-host (nginx passes it
        # through).  The connector must overwrite it, not append a second
        # header: a mixed-case assignment would leave both values, and
        # first-match header readers (Go/Node/http.server-style) would see
        # the attacker's value.  The upstream must see exactly one
        # x-forwarded-host, equal to the connector's public Host.
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(
                f"{PROXY_PATH}/game/",
                headers={
                    "Host": "course.example.edu",
                    "x-forwarded-host": "attacker.example",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("x-forwarded-host=attacker.example", response.text)
        self.assertEqual(response.text.count("x-forwarded-host=course.example.edu"), 1)

    def test_proxy_preserves_content_encoding_for_compressed_apps(self):
        class GzipHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                import gzip as _gzip
                payload = _gzip.compress(b"hello compressed world")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        import socketserver as _socketserver
        httpd = _socketserver.TCPServer(("127.0.0.1", 0), GzipHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connector._tunnel_registry.register("gz", port)
            response = self.client.get(f"{PROXY_PATH}/gz/")
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        # The content-encoding header now passes through, so the httpx client
        # (which auto-decompresses) receives the original plaintext.
        self.assertEqual(response.content, b"hello compressed world")

    def test_proxy_rejects_oversized_body_with_413(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.post(
                f"{PROXY_PATH}/game/upload",
                content=b"x" * (connector.TUNNEL_MAX_BODY + 1),
                headers={"Content-Type": "application/octet-stream"},
            )
        self.assertEqual(response.status_code, 413)

    def test_proxy_is_public_no_signature_required(self):
        with UpstreamServer() as upstream:
            connector._tunnel_registry.register("game", upstream.port)
            response = self.client.get(
                f"{PROXY_PATH}/game/",
                headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.9"},
            )
        # A public browser must be able to reach the app itself.
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
