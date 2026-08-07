"""Safety-focused tests for the persistent Hermes session adapter."""
import unittest
import asyncio
from pathlib import Path

class SessionAdapterSourceTests(unittest.TestCase):
    """Keep the connector's public session surface narrow and durable."""
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_supported_session_methods_are_used(self):
        for method in ("session.create", "session.list", "session.resume", "session.history", "session.title", "session.delete", "session.close", "prompt.submit"):
            self.assertIn(method, self.source)

    def test_history_sanitization_excludes_system_and_tool_messages(self):
        sanitizer = (Path(__file__).parent / "session_payloads.py").read_text(encoding="utf-8")
        self.assertIn('if role not in {"user", "assistant"}', sanitizer)
        self.assertIn('"role": role, "content": text', sanitizer)

    def test_session_ids_are_validated_and_no_full_history_is_sent_to_prompt(self):
        self.assertIn('SESSION_ID_RE', self.source)
        self.assertIn('A-Za-z0-9_-', self.source)
        self.assertIn('persistent Hermes chats accept only the new user message', self.source)
        self.assertIn('"source": "classroom-portal"', self.source)
        self.assertIn('"close_on_disconnect": True', self.source)
        self.assertIn('a valid idempotency key is required', self.source)
        self.assertIn('limit=64', self.source)

    def test_session_routes_and_ui_controls_are_narrow(self):
        route_source_path = Path(__file__).parents[1] / "app/api/hermes/[...path]/route.ts"
        page_source_path = Path(__file__).parents[1] / "app/workspace/page.tsx"
        if not route_source_path.exists() or not page_source_path.exists():
            self.skipTest("portal source is not part of this VM repo")
        route_source = route_source_path.read_text(encoding="utf-8")
        page_source = page_source_path.read_text(encoding="utf-8")
        self.assertIn('"v1/sessions"', route_source)
        self.assertIn('sessionPath.test(path)', route_source)
        self.assertIn('export async function PATCH', route_source)
        self.assertIn('export async function DELETE', route_source)
        self.assertIn('New chat', page_source)
        self.assertIn('window.confirm', page_source)
        self.assertIn('session_id: activeChatId ?? undefined', page_source)
        self.assertIn('idempotency_key: requestId', page_source)
        self.assertIn('mobile-chat-menu', page_source)


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_calls_coalesce_and_reuse_completed_result(self):
        from idempotency import TurnIdempotency
        cache = TurnIdempotency(limit=64)
        self.assertEqual(cache.limit, 64)
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()
        async def work():
            nonlocal calls
            calls += 1; started.set(); await release.wait()
            return ("answer", {"tokens": 1}, "20260725_120000_abcdef")
        first = asyncio.create_task(cache.run("request-id-000000000000", "digest", work))
        await started.wait()
        second = asyncio.create_task(cache.run("request-id-000000000000", "digest", work))
        release.set()
        self.assertEqual(await first, await second)
        self.assertEqual(calls, 1)
        self.assertEqual(await cache.run("request-id-000000000000", "digest", work), ("answer", {"tokens": 1}, "20260725_120000_abcdef"))
        self.assertEqual(calls, 1)

    async def test_idempotency_key_cannot_be_reused_for_another_prompt(self):
        from idempotency import TurnIdempotency
        cache = TurnIdempotency(limit=64)
        async def work(): return "ok"
        await cache.run("request-id-000000000001", "first", work)
        with self.assertRaises(ValueError):
            await cache.run("request-id-000000000001", "different", work)


class ObservedHermesHistoryFixtureTests(unittest.TestCase):
    def test_0182_session_history_text_shape_is_renderable_and_safe(self):
        from session_payloads import sanitize_history
        # Exact field shape observed from the installed 0.18.2 gateway: the
        # `messages` array holds role + `text`, not OpenAI `content`.
        observed_response = {"count": 2, "messages": [
            {"role": "user", "text": "Help me plan my project", "internal": "not exposed"},
            {"role": "assistant", "text": "## A plan", "reasoning": "not exposed"},
        ]}
        self.assertEqual(sanitize_history(observed_response["messages"], 1024), [
            {"role": "user", "content": "Help me plan my project"},
            {"role": "assistant", "content": "## A plan"},
        ])

    def test_system_and_tool_history_entries_remain_hidden(self):
        from session_payloads import sanitize_history
        self.assertEqual(sanitize_history([
            {"role": "system", "text": "secret"},
            {"role": "tool", "text": "tool secret"},
            {"role": "assistant", "text": "visible"},
        ], 1024), [{"role": "assistant", "content": "visible"}])


if __name__ == "__main__":
    unittest.main()
