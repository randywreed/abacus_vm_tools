"""Focused tests for the clarify bridge token lifecycle and validation.

All tests are behavioral — they exercise the pure ``ClarifyState`` store and
validation functions, never the source code shape.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unittest
import uuid

from connector.clarify_state import (
    CLARIFY_TOKEN_RE,
    ClarifyState,
    DeferredQueue,
    validate_answer,
    validate_choices,
    validate_question,
)


class ValidateQuestionTests(unittest.TestCase):
    """``validate_question`` safety and bounds."""

    def test_valid_question_passes(self) -> None:
        self.assertEqual(validate_question("Which model?"), "Which model?")

    def test_empty_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_question("")

    def test_whitespace_only_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_question("   ")

    def test_non_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_question(42)

    def test_long_question_capped(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_QUESTION_CHARS

        long_q = "x" * (CLARIFY_MAX_QUESTION_CHARS + 100)
        result = validate_question(long_q)
        self.assertEqual(len(result), CLARIFY_MAX_QUESTION_CHARS)


class ValidateChoicesTests(unittest.TestCase):
    """``validate_choices`` safety and bounds."""

    def test_valid_choices_passes(self) -> None:
        self.assertEqual(validate_choices(["A", "B"]), ["A", "B"])

    def test_single_choice_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_choices(["Only one"])

    def test_too_many_choices_raises(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_CHOICES

        with self.assertRaises(ValueError):
            validate_choices(["x"] * (CLARIFY_MAX_CHOICES + 1))

    def test_duplicate_choices_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_choices(["A", "A"])

    def test_non_string_choice_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_choices(["A", 42])

    def test_empty_choice_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_choices(["A", ""])

    def test_long_choice_capped(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_CHOICE_CHARS

        long_c = "x" * (CLARIFY_MAX_CHOICE_CHARS + 50)
        result = validate_choices(["Short", long_c])
        self.assertEqual(len(result[1]), CLARIFY_MAX_CHOICE_CHARS)


class ValidateAnswerTests(unittest.TestCase):
    """``validate_answer`` safety and invariants."""

    CHOICES = ["GPT-4", "Claude", "Gemini"]

    def test_single_select_valid(self) -> None:
        self.assertEqual(validate_answer(self.CHOICES, False, "Claude"), "Claude")

    def test_single_select_missing_choice_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, False, "LLaMA")

    def test_single_select_empty_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, False, "")

    def test_single_select_non_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, False, 42)

    def test_multi_select_valid(self) -> None:
        result = validate_answer(self.CHOICES, True, ["GPT-4", "Gemini"])
        self.assertEqual(result, ["GPT-4", "Gemini"])

    def test_multi_select_not_a_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, True, "GPT-4")

    def test_multi_select_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, True, [])

    def test_multi_select_duplicates_raises(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_ANSWER_CHOICES_MULTI

        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, True, ["GPT-4", "GPT-4"])

    def test_multi_select_unknown_choice_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, True, ["GPT-4", "LLaMA"])

    def test_multi_select_too_many_raises(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_ANSWER_CHOICES_MULTI

        many = self.CHOICES * (CLARIFY_MAX_ANSWER_CHOICES_MULTI + 1)
        with self.assertRaises(ValueError):
            validate_answer(self.CHOICES, True, many[: CLARIFY_MAX_ANSWER_CHOICES_MULTI + 1])

    def test_multi_select_bounded_subset_accepted(self) -> None:
        result = validate_answer(self.CHOICES, True, self.CHOICES[:2])
        self.assertEqual(result, self.CHOICES[:2])


class ClarifyStateTokenLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Token creation, resolution, replay, expiry, and cleanup."""

    async def asyncSetUp(self) -> None:
        self.state = ClarifyState(ttl=300.0)

    async def test_create_pending_returns_valid_token(self) -> None:
        token = await self.state.create_pending(
            "req-1", "Which model?", ["A", "B", "C"]
        )
        self.assertIsInstance(token, str)
        self.assertTrue(CLARIFY_TOKEN_RE.fullmatch(token))
        count = await self.state.pending_count()
        self.assertEqual(count, 1)

    async def test_resolve_pending_valid_answer_returns_true(self) -> None:
        token = await self.state.create_pending("req-1", "Pick one", ["X", "Y"])
        result = await self.state.resolve_pending(token, "X")
        self.assertTrue(result)

    async def test_resolve_pending_returns_answer(self) -> None:
        token = await self.state.create_pending("req-1", "Pick one", ["X", "Y"])

        async def _resolve() -> None:
            await asyncio.sleep(0.01)
            await self.state.resolve_pending(token, "X")

        task = asyncio.create_task(_resolve())
        result = await self.state.await_answer(token, timeout=5.0)
        await task
        self.assertEqual(result, "X")

    async def test_replay_rejected(self) -> None:
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B"])
        self.assertTrue(await self.state.resolve_pending(token, "A"))
        # Second resolve returns False (entry still in _entries but future already done)
        self.assertFalse(await self.state.resolve_pending(token, "B"))

    async def test_await_answer_after_resolve_returns_answer(self) -> None:
        """Regression: resolve BEFORE await_answer must still deliver the answer.

        When the browser POSTs a valid answer before the streaming coroutine
        calls await_answer (the pre-valid-answer-race scenario), await_answer
        must capture the entry and await its already-fulfilled future, returning
        the answer instead of raising KeyError.
        """
        token = await self.state.create_pending(
            "req-1", "Pick one", ["X", "Y"]
        )
        # Resolve first (simulates browser POST arriving before await_answer)
        self.assertTrue(await self.state.resolve_pending(token, "X"))
        # Now await — must get the answer, not KeyError
        result = await self.state.await_answer(token, timeout=5.0)
        self.assertEqual(result, "X")
        # Entry must be cleaned up after await_answer consumes it
        self.assertNotIn(token, self.state.entries)
        # Replay must still be rejected
        self.assertFalse(await self.state.resolve_pending(token, "Y"))

    async def test_expired_token_rejected(self) -> None:
        state = ClarifyState(ttl=0.0)  # Immediate expiry
        token = await state.create_pending("req-1", "Pick?", ["A", "B"])
        await asyncio.sleep(0.01)  # Let time advance
        self.assertFalse(await state.resolve_pending(token, "A"))

    async def test_unknown_token_rejected(self) -> None:
        self.assertFalse(await self.state.resolve_pending("nonexistent00000000000000000000", "A"))

    async def test_cleanup_token_removes_entry(self) -> None:
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B"])
        await self.state.cleanup_token(token)
        self.assertFalse(await self.state.resolve_pending(token, "A"))

    async def test_cleanup_all_removes_all_entries(self) -> None:
        t1 = await self.state.create_pending("req-1", "Q?", ["A", "B"])
        t2 = await self.state.create_pending("req-2", "Q?", ["C", "D"])
        self.assertEqual(await self.state.pending_count(), 2)
        removed = await self.state.cleanup_all()
        self.assertEqual(removed, 2)
        self.assertEqual(await self.state.pending_count(), 0)

    async def test_answer_validation_fails_closed(self) -> None:
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B"])
        # Answer not in choices
        self.assertFalse(await self.state.resolve_pending(token, "C"))

    async def test_invalid_answer_does_not_consume_token(self) -> None:
        """Regression: invalid answer must NOT consume the pending entry."""
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B", "C"])
        # Invalid answer returns False
        self.assertFalse(await self.state.resolve_pending(token, "D"))
        # Token must still be pending
        self.assertIn(token, self.state.entries)

    async def test_invalid_then_valid_answer_resolves(self) -> None:
        """Regression: after invalid answer, a valid answer must resolve the future."""
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B"])

        async def _resolve_valid() -> None:
            await asyncio.sleep(0.02)
            self.assertFalse(
                await self.state.resolve_pending(token, "X"),
                "invalid answer must return False",
            )
            self.assertIn(token, self.state.entries, "token must survive invalid answer")
            self.assertTrue(
                await self.state.resolve_pending(token, "A"),
                "valid answer must return True after invalid",
            )

        task = asyncio.create_task(_resolve_valid())
        result = await self.state.await_answer(token, timeout=5.0)
        await task
        self.assertEqual(result, "A")

    async def test_await_answer_times_out(self) -> None:
        token = await self.state.create_pending("req-1", "Pick?", ["A", "B"])
        with self.assertRaises(asyncio.TimeoutError):
            await self.state.await_answer(token, timeout=0.01)

    async def test_await_answer_unknown_token_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            await self.state.await_answer("nonexistent00000000000000000000", timeout=1.0)


class ClarifyStateMultiSelectTests(unittest.IsolatedAsyncioTestCase):
    """Multi-select create/resolve/validation."""

    async def test_multi_select_create_and_resolve(self) -> None:
        state = ClarifyState()
        token = await state.create_pending("req-1", "Which?", ["A", "B", "C"], multi_select=True)
        self.assertTrue(CLARIFY_TOKEN_RE.fullmatch(token))
        result = await state.resolve_pending(token, ["A", "C"])
        self.assertTrue(result)

    async def test_multi_select_answer_exceeds_bounded_limit_fails_closed(self) -> None:
        from connector.clarify_state import CLARIFY_MAX_ANSWER_CHOICES_MULTI

        state = ClarifyState()
        choices = [str(i) for i in range(CLARIFY_MAX_ANSWER_CHOICES_MULTI + 5)]
        token = await state.create_pending("req-1", "Which?", choices, multi_select=True)
        too_many = [str(i) for i in range(CLARIFY_MAX_ANSWER_CHOICES_MULTI + 1)]
        self.assertFalse(await state.resolve_pending(token, too_many))

    async def test_multi_select_invalid_does_not_consume_token(self) -> None:
        """Regression: invalid multi-select answer must NOT consume the pending entry."""
        state = ClarifyState()
        choices = ["Red", "Green", "Blue"]
        token = await state.create_pending("req-1", "Which?", choices, multi_select=True)
        # Invalid choice returns False
        self.assertFalse(await state.resolve_pending(token, ["Red", "Yellow"]))
        # Token must still be pending
        self.assertIn(token, state.entries)

    async def test_multi_select_invalid_then_valid_resolves(self) -> None:
        """Regression: after invalid multi-select, a valid answer must resolve."""
        state = ClarifyState()
        token = await state.create_pending("req-1", "Colors?", ["A", "B", "C"], multi_select=True)

        async def _resolve_valid() -> None:
            await asyncio.sleep(0.02)
            self.assertFalse(
                await state.resolve_pending(token, ["X"]),
                "invalid answer must return False",
            )
            self.assertIn(token, state.entries, "token must survive invalid answer")
            self.assertTrue(
                await state.resolve_pending(token, ["A", "C"]),
                "valid answer must return True after invalid",
            )

        task = asyncio.create_task(_resolve_valid())
        result = await state.await_answer(token, timeout=5.0)
        await task
        self.assertEqual(result, ["A", "C"])


class ClarifyRespondAckBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """Behavioral tests for the clarify.respond send+ack-wait pattern.

    Uses asyncio queues to simulate the upstream WebSocket, proving the
    connector sends with a unique ``id`` and waits for a matching positive
    acknowledgement before continuing.  No source-code regex inspection.
    """

    async def _simulate(
        self,
        respond_fn=None,
        disconnect: bool = False,
        deadline_delay: float = 10.0,
    ) -> tuple[str | None, str | None]:
        """Simulate the clarify.respond send+ack-wait loop.

        *respond_fn* is called with the generated clarify_id and should
        return a list of JSON-RPC frames to feed as upstream responses.

        Returns (clarify_id, error_msg) where clarify_id is the sent id
        (None if no send occurred) and error_msg is any error raised
        (None on success).
        """
        recv_q: asyncio.Queue[str | None] = asyncio.Queue()
        send_q: asyncio.Queue[str] = asyncio.Queue()

        class FakeRequest:
            async def is_disconnected(self) -> bool:
                return disconnect

        request = FakeRequest()
        gateway_request_id = "gw-req-1"
        answer = "Claude"

        request_id = "test-sim"
        clarify_id = f"clarify-{request_id}-sim-{uuid.uuid4().hex}"

        # Generate response frames using the actual clarify_id
        if respond_fn is not None:
            for frame in respond_fn(clarify_id):
                recv_q.put_nowait(json.dumps(frame))

        async def _upstream_recv() -> str:
            data = await asyncio.wait_for(recv_q.get(), timeout=2.0)
            if data is None:
                raise RuntimeError("upstream closed")
            return data

        async def _upstream_send(data: str) -> None:
            send_q.put_nowait(data)

        deadline = time.monotonic() + deadline_delay
        sent_id: str | None = None
        error: str | None = None

        try:
            clarify_id_use = clarify_id
            await _upstream_send(json.dumps({
                "jsonrpc": "2.0",
                "id": clarify_id_use,
                "method": "clarify.respond",
                "params": {"request_id": gateway_request_id, "answer": answer},
            }))
            sent_id = clarify_id_use
            # Wait for ack loop (same logic as event_generator)
            while True:
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Clarify response was not acknowledged by Hermes"
                    )
                try:
                    ack_raw = await asyncio.wait_for(
                        _upstream_recv(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    continue
                ack_data = json.loads(ack_raw)
                if not isinstance(ack_data, dict):
                    continue
                if ack_data.get("id") != clarify_id_use:
                    continue
                # Require a valid positive JSON-RPC result (same contract as
                # the live connector's ack loop).
                result = ack_data.get("result")
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise RuntimeError("Hermes rejected the clarify response")
                break
        except (TimeoutError, RuntimeError, asyncio.CancelledError) as exc:
            error = str(exc)

        # Check what was sent
        sent_raw = None
        try:
            sent_raw = await asyncio.wait_for(send_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        return sent_id, error

    async def test_sends_with_unique_id(self) -> None:
        """clarify.respond must be sent with a unique JSON-RPC id."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNotNone(sent_id)
        self.assertIsNone(error)
        if sent_id:
            self.assertTrue(sent_id.startswith("clarify-"))

    async def test_waits_for_matching_ack(self) -> None:
        """Must wait until a frame with matching id arrives."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": "wrong-id-1", "result": {"status": "ok"}},
                {"jsonrpc": "2.0", "id": "wrong-id-2", "result": {"status": "ok"}},
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error)

    async def test_ignores_unrelated_frames(self) -> None:
        """Non-matching frames must be ignored, not treated as ack."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"method": "event", "params": {"type": "some.event"}},
                {"jsonrpc": "2.0", "id": "wrong-id", "result": {"valid": "but wrong"}},
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error)

    async def test_rejects_error_response(self) -> None:
        """An error response for the matching id must raise."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid,
                 "error": {"code": -32601, "message": "Method not found"}},
            ],
        )
        self.assertIsNotNone(error)
        self.assertIn("rejected", str(error).lower())

    async def test_disconnect_raises(self) -> None:
        """HTTP disconnect must cancel the wait."""
        sent_id, error = await self._simulate(
            respond_fn=None,  # no frames
            disconnect=True,
        )
        self.assertIsNotNone(error)

    async def test_timeout_raises(self) -> None:
        """Deadline expiry without matching ack must raise TimeoutError."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": "wrong-only", "result": {"status": "ok"}},
            ],
            deadline_delay=0.05,
        )
        self.assertIsNotNone(error)
        self.assertIn("acknowledged", str(error).lower())

    async def test_rejects_missing_result(self) -> None:
        """A matching-id frame without a ``result`` key must fail closed."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "noresult": True},
            ],
        )
        self.assertIsNotNone(error)
        self.assertIn("rejected", str(error).lower())

    async def test_rejects_expired_status(self) -> None:
        """``result: {status: 'expired'}`` must fail closed."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "expired"}},
            ],
        )
        self.assertIsNotNone(error)
        self.assertIn("rejected", str(error).lower())

    async def test_rejects_non_ok_status(self) -> None:
        """Any status other than ``'ok'`` must fail closed."""
        sent_id, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "something_else"}},
            ],
        )
        self.assertIsNotNone(error)
        self.assertIn("rejected", str(error).lower())


class EncodeClarifyChunkTests(unittest.TestCase):
    """The clarify SSE chunk must expose no gateway internals."""

    def test_safe_shape_no_secrets(self) -> None:
        from connector.streaming_sse import encode_clarify_chunk

        raw = encode_clarify_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            token="abc123" + "0" * 58,
            question="Which color?",
            choices=["Red", "Blue"],
            multi_select=False,
            session_id="sess_1",
        )
        frames = [f for f in raw.split("\n\n") if f.strip()]
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith("data: "))
        chunk = json.loads(frames[0][6:])
        clarify = chunk.get("clarify")
        self.assertIsNotNone(clarify)
        payload = clarify.get("payload", {})
        # Safe fields present
        self.assertIn("token", payload)
        self.assertIn("question", payload)
        self.assertIn("choices", payload)
        self.assertIn("multi_select", payload)
        self.assertEqual(payload["question"], "Which color?")
        self.assertEqual(payload["choices"], ["Red", "Blue"])
        self.assertFalse(payload["multi_select"])
        # Gateway internals must NOT be exposed
        self.assertNotIn("request_id", payload)
        self.assertNotIn("request_id", chunk)
        self.assertNotIn("session_id", payload)
        self.assertNotIn("gateway", str(payload).lower())

    def test_choices_list_capped_by_schema(self) -> None:
        from connector.streaming_sse import encode_clarify_chunk

        raw = encode_clarify_chunk(
            completion_id="chatcmpl-test",
            model=None,
            token="t" + "0" * 63,
            question="Q?",
            choices=["A", "B", "C"],
            multi_select=True,
            session_id=None,
        )
        chunk = json.loads(raw.split("\n\n")[0][6:])
        payload = chunk["clarify"]["payload"]
        self.assertEqual(payload["choices"], ["A", "B", "C"])
        self.assertTrue(payload["multi_select"])
        # No session_id in the outer chunk either when None provided
        self.assertNotIn("session_id", chunk)


class ClarifySSEDoesNotWeakenTerminalValidation(unittest.TestCase):
    """Clarify events in the SSE stream must not confuse the existing terminal
    / [DONE] parser or the workspace's ``sawDone`` / ``terminalValid`` guards."""

    def test_clarify_does_not_have_terminal_fields(self) -> None:
        from connector.streaming_sse import encode_clarify_chunk

        raw = encode_clarify_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            token="t" + "0" * 63,
            question="Q?",
            choices=["A", "B"],
            multi_select=False,
            session_id="sess_1",
        )
        chunk = json.loads(raw.split("\n\n")[0][6:])
        # Must not look like a terminal event
        self.assertNotIn("finish_reason", chunk.get("choices", [{}])[0])
        self.assertIsNone(chunk.get("usage"))
        self.assertNotIn("error", chunk)


class DeferredQueueBasicTests(unittest.TestCase):
    """``DeferredQueue`` put/get ordering, overflow, and empty behavior."""

    def test_put_get_ordered(self) -> None:
        q = DeferredQueue(maxsize=4)
        q.put({"id": 1})
        q.put({"id": 2})
        self.assertEqual(q.get_nowait(), {"id": 1})
        self.assertEqual(q.get_nowait(), {"id": 2})

    def test_empty_returns_none(self) -> None:
        q = DeferredQueue(maxsize=4)
        self.assertIsNone(q.get_nowait())

    def test_overflow_raises_runtime_error(self) -> None:
        q = DeferredQueue(maxsize=2)
        q.put({"method": "event", "params": {"type": "a"}})
        q.put({"method": "event", "params": {"type": "b"}})
        with self.assertRaises(RuntimeError):
            q.put({"method": "event", "params": {"type": "c"}})

    def test_put_get_after_overflow_no_corruption(self) -> None:
        """After a failed put, the queue must still deliver valid entries."""
        q = DeferredQueue(maxsize=2)
        q.put({"n": 1})
        q.put({"n": 2})
        with self.assertRaises(RuntimeError):
            q.put({"n": 3})
        self.assertEqual(q.get_nowait(), {"n": 1})
        self.assertEqual(q.get_nowait(), {"n": 2})
        self.assertIsNone(q.get_nowait())


class ClarifyAckDeferredTests(unittest.IsolatedAsyncioTestCase):
    """Integration: event frames arriving before the clarify.respond ack are
    preserved in order and delivered after ack succeeds."""

    async def _simulate(
        self,
        respond_fn,
        disconnect: bool = False,
        deadline_delay: float = 10.0,
        maxsize: int = 8,
    ) -> tuple[list[dict], str | None]:
        """Simulate the event_generator's ack-wait loop using DeferredQueue.

        *respond_fn* is called with the generated clarify_id and should
        return a list of JSON-RPC frames to feed as upstream responses.

        Returns (deferred_frames, error) where *deferred_frames* is the
        list of nonmatching frames collected during the ack wait (empty
        when none were queued, or when the ack failed) and *error* is
        any error message raised (None on success).
        """
        recv_q: asyncio.Queue[str | None] = asyncio.Queue()
        deferred_q: DeferredQueue = DeferredQueue(maxsize=maxsize)
        clarify_id = f"clarify-test-{uuid.uuid4().hex}"

        if respond_fn is not None:
            for frame in respond_fn(clarify_id):
                recv_q.put_nowait(json.dumps(frame))

        async def _upstream_recv() -> str:
            data = await asyncio.wait_for(recv_q.get(), timeout=2.0)
            if data is None:
                raise RuntimeError("upstream closed")
            return data

        deadline = time.monotonic() + deadline_delay
        error: str | None = None

        try:
            # Same ack-wait loop as the production event_generator
            while True:
                if disconnect:
                    raise asyncio.CancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Clarify response was not acknowledged by Hermes"
                    )
                try:
                    ack_raw = await asyncio.wait_for(
                        _upstream_recv(), timeout=min(remaining, 1.0)
                    )
                except asyncio.TimeoutError:
                    continue
                if not isinstance(ack_raw, str):
                    continue
                ack_data = json.loads(ack_raw)
                if not isinstance(ack_data, dict):
                    continue
                if ack_data.get("id") != clarify_id:
                    # Queue nonmatching frames instead of dropping
                    try:
                        deferred_q.put(ack_data)
                    except RuntimeError:
                        raise  # fail-closed on overflow
                    continue
                # Require a valid positive JSON-RPC result.
                result = ack_data.get("result")
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise RuntimeError("Hermes rejected the clarify response")
                break
        except (TimeoutError, RuntimeError, asyncio.CancelledError) as exc:
            error = str(exc)

        # Collect all deferred frames in order
        collected: list[dict] = []
        while True:
            f = deferred_q.get_nowait()
            if f is None:
                break
            collected.append(f)
        return collected, error

    async def test_events_before_ack_are_preserved_in_order(self) -> None:
        """Feed [message.delta, message.complete, matching ack] during ack wait.
        The two event frames must be available from the deferred queue in
        original order after the ack succeeds."""
        collected, error = await self._simulate(
            respond_fn=lambda cid: [
                {"method": "event", "params": {"type": "message.delta",
                 "session_id": "sess1", "payload": {"text": "Hello "}}},
                {"method": "event", "params": {"type": "message.complete",
                 "session_id": "sess1", "payload": {"text": "Hello world"}}},
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error, msg=f"ack should succeed, got: {error}")
        self.assertEqual(len(collected), 2)
        self.assertEqual(
            collected[0].get("params", {}).get("type"),
            "message.delta",
        )
        self.assertEqual(
            collected[1].get("params", {}).get("type"),
            "message.complete",
        )

    async def test_no_events_nothing_queued(self) -> None:
        """When no event frames arrive before the ack, the deferred queue
        remains empty."""
        collected, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error)
        self.assertEqual(collected, [])

    async def test_deferred_overflow_fails_closed(self) -> None:
        """Feeding more nonmatching frames than the queue maxsize causes a
        RuntimeError (fail-closed), not silent data loss."""
        # 9 nonmatching event frames (maxsize=8) + 1 matching ack at end
        frames = [
            {"method": "event", "params": {"type": f"evt{i}", "session_id": "s1"}}
            for i in range(9)
        ]
        frames.append(
            {"jsonrpc": "2.0", "id": "this-wont-match", "result": {"status": "ok"}}
        )
        collected, error = await self._simulate(
            respond_fn=lambda cid: frames,
            maxsize=8,
        )
        self.assertIsNotNone(error)
        self.assertIn("overflow", str(error).lower())

    async def test_mixed_ordering_preserved(self) -> None:
        """Interleaved events and nonmatching responses are collected in the
        exact order they arrive."""
        collected, error = await self._simulate(
            respond_fn=lambda cid: [
                {"method": "event", "params": {"type": "msg.delta.1",
                 "session_id": "s1"}},
                {"jsonrpc": "2.0", "id": "other-rpc", "result": {"n": 1}},
                {"method": "event", "params": {"type": "msg.delta.2",
                 "session_id": "s1"}},
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error)
        self.assertEqual(len(collected), 3)
        self.assertEqual(collected[0].get("params", {}).get("type"), "msg.delta.1")
        self.assertEqual(collected[1].get("id"), "other-rpc")
        self.assertEqual(collected[2].get("params", {}).get("type"), "msg.delta.2")

    async def test_normal_ack_still_works(self) -> None:
        """The ack-wait must still correctly identify matching ack when there
        are no intervening events (regression: existing behavior preserved)."""
        collected, error = await self._simulate(
            respond_fn=lambda cid: [
                {"jsonrpc": "2.0", "id": cid, "result": {"status": "ok"}},
            ],
        )
        self.assertIsNone(error)
        self.assertEqual(collected, [])


if __name__ == "__main__":
    unittest.main()
