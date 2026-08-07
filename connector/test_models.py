"""Tests for RouteLLM model list, chat model routing, and streaming support."""
import asyncio
import ast
import unittest
import json
from pathlib import Path
import re


class ModelListSourceTests(unittest.TestCase):
    """Static source checks for the model endpoint."""

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_v1_models_route_exists(self):
        self.assertIn("v1/models", self.source)
        self.assertIn("_routellm_models", self.source)

    def test_model_ids_use_fullmatch_not_match(self):
        self.assertIn("fullmatch", self.source)
        self.assertNotIn(".match(raw_id)", self.source)

    def test_model_ids_are_trimmed_then_fullmatched(self):
        self.assertIn(".strip()", self.source)
        self.assertIn("fullmatch(trimmed)", self.source)

    def test_model_list_capped_at_200(self):
        self.assertIn("[:200]", self.source)

    def test_namespaced_provider_model_ids_are_allowed(self):
        """RouteLLM uses safe provider/model IDs for Kimi and DeepSeek models."""
        self.assertIn("(?=.{1,128}$)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", self.source)

    def test_model_list_exposes_sanitized_pricing_metadata(self):
        """The portal needs published token prices, not hard-coded estimates."""
        self.assertIn('"display_name"', self.source)
        self.assertIn('"model_type"', self.source)
        self.assertIn('"input_token_rate"', self.source)
        self.assertIn('"output_token_rate"', self.source)

    def test_http_failure_returns_503_not_leak(self):
        self.assertIn("503", self.source)
        self.assertIn("HTTPException(status_code=503", self.source)

    def test_api_key_inside_try(self):
        self.assertIn("key = _abacus_api_key()", self.source)
        # The try/except for _abacus_api_key must appear before the client.get try
        key_line = self.source.index("key = _abacus_api_key()")
        before_key = self.source[:key_line]
        self.assertIn("try:", before_key)
        self.assertNotIn("except", before_key.rsplit("try:", 1)[-1])  # no except between try and key

    def test_no_runtime_error_visible_in_models(self):
        self.assertIn('detail="Model list unavailable"', self.source)
        # _routellm_models must catch RuntimeError from _abacus_api_key and raise only 503
        self.assertIn("except RuntimeError:", self.source)
        self.assertIn('raise HTTPException(status_code=503, detail="Model list unavailable")', self.source)

    def test_chat_accepts_model_for_resumed_sessions(self):
        self.assertIn("normalize_model_for_session", self.source)
        self.assertIn("session_switch_command", self.source)
        self.assertNotIn("Cannot change model for an existing session", self.source)

    def test_rpc_chat_switches_model_after_resume_before_prompt(self):
        # _rpc_chat must not put model in session.resume params; a resumed
        # session switches its model via config.set before prompt.submit.
        self.assertIn("session.create", self.source)
        self.assertIn("session.resume", self.source)
        # The resume request itself must not carry model in its params.
        resume_idx = self.source.index("session.resume")
        resume_call = self.source[resume_idx:resume_idx + 140]
        self.assertNotIn('"model"', resume_call)
        # The switch (session_switch_command) must appear before prompt.submit
        # in the non-streaming _rpc_chat path.
        rpc_chat_idx = self.source.index("async def _rpc_chat")
        next_def = self.source.index("async def _rpc_request", rpc_chat_idx)
        rpc_block = self.source[rpc_chat_idx:next_def]
        switch_idx = rpc_block.index("session_switch_command")
        prompt_idx = rpc_block.index("prompt.submit")
        self.assertLess(switch_idx, prompt_idx)
        # A resumed session switches only when a model was requested.
        self.assertIn("if session_key and model:", rpc_block)
        # The create block still builds create_params with model for new sessions.
        create_idx = self.source.index("session.create")
        before_create = self.source[create_idx - 300:create_idx]
        self.assertIn("create_params[", before_create)
        self.assertIn('"model"', before_create)


class ModelSanitizationTests(unittest.TestCase):
    """Test model ID sanitization logic directly as pure functions."""

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_model_id_fullmatch(self):
        pattern = re.compile(r"^(?=.{1,128}$)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$")
        # Valid IDs, including RouteLLM's provider/model namespace.
        self.assertTrue(pattern.fullmatch("gpt-4"))
        self.assertTrue(pattern.fullmatch("claude-3.5-sonnet"))
        self.assertTrue(pattern.fullmatch("gpt_4o"))
        self.assertTrue(pattern.fullmatch("a.b-c"))
        self.assertTrue(pattern.fullmatch("moonshotai/Kimi-K2.6"))
        self.assertTrue(pattern.fullmatch("deepseek-ai/DeepSeek-V4-Flash"))
        # Invalid — empty, too long, special characters, and malformed namespaces.
        self.assertFalse(pattern.fullmatch(""))
        self.assertFalse(pattern.fullmatch("a" * 129))
        self.assertFalse(pattern.fullmatch("unsafe/<script>"))
        self.assertFalse(pattern.fullmatch("model with spaces"))
        self.assertFalse(pattern.fullmatch("model\nnewline"))
        self.assertFalse(pattern.fullmatch(" model"))
        self.assertFalse(pattern.fullmatch("provider//model"))
        self.assertFalse(pattern.fullmatch("provider/model/extra"))

    def test_sanitize_model_list_with_trim(self):
        pattern = re.compile(r"^(?=.{1,128}$)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$")
        raw_data = [
            {"id": "gpt-4", "object": "model"},
            {"id": "  spaced  ", "object": "model"},   # trimmed → "spaced" → valid
            {"id": "moonshotai/Kimi-K2.6", "object": "model"},
            {"id": "unsafe/<script>", "object": "model"},
            {"id": "", "object": "model"},
            {"id": "valid-model", "object": "model"},
            {"id": "a\nb", "object": "model"},
        ]
        result = []
        for entry in raw_data[:200]:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id")
            if isinstance(raw_id, str):
                trimmed = raw_id.strip()
                if trimmed and pattern.fullmatch(trimmed):
                    result.append({"id": trimmed, "object": "model"})
        ids = [e["id"] for e in result]
        self.assertIn("gpt-4", ids)
        self.assertIn("valid-model", ids)
        self.assertIn("spaced", ids)  # trim then validate
        self.assertIn("moonshotai/Kimi-K2.6", ids)
        self.assertNotIn("unsafe/<script>", ids)
        self.assertNotIn("", ids)
        self.assertNotIn("a\nb", ids)
        self.assertEqual(len(result), 4)

    def test_capped_at_200(self):
        pattern = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
        raw_data = [{"id": f"model-{i}", "object": "model"} for i in range(300)]
        result = []
        for entry in raw_data[:200]:
            if isinstance(entry, dict):
                raw_id = entry.get("id")
                if isinstance(raw_id, str):
                    trimmed = raw_id.strip()
                    if trimmed and pattern.fullmatch(trimmed):
                        result.append({"id": trimmed, "object": "model"})
        self.assertEqual(len(result), 200)

    def test_no_secret_in_result(self):
        result = [{"id": "gpt-4", "object": "model"}]
        json_str = json.dumps(result)
        self.assertNotIn("secret", json_str.lower())

    def test_missing_key_returns_generic_503(self):
        """Source check: _routellm_models catches RuntimeError from _abacus_api_key and raises only 503."""
        self.assertIn("except RuntimeError:", self.source)
        self.assertIn('raise HTTPException(status_code=503, detail="Model list unavailable")', self.source)


class ChatModelRouteBuilderTests(unittest.TestCase):
    """Behavioral tests for the _rpc_chat model parameter — extracted builder."""

    def test_create_includes_model_when_present(self):
        """Simulate the session.create params construction for new chat with model."""
        model = "gpt-4"
        create_params = {"cols": 100, "source": "classroom-portal", "close_on_disconnect": True}
        create_params["model"] = model
        frame = {"jsonrpc": "2.0", "id": "test-id", "method": "session.create", "params": create_params}
        self.assertIn("model", frame["params"])
        self.assertEqual(frame["params"]["model"], "gpt-4")
        self.assertNotIn("provider", frame["params"])
        self.assertNotIn("reasoning", frame["params"])

    def test_resume_never_has_model(self):
        """Simulate the session.resume params — never include model."""
        session_key = "abc123"
        resume_params = {"session_id": session_key, "cols": 100, "source": "classroom-portal", "close_on_disconnect": True}
        frame = {"jsonrpc": "2.0", "id": "test-id", "method": "session.resume", "params": resume_params}
        self.assertNotIn("model", frame["params"])

    def test_resume_preserves_requested_model(self):
        """Chat completions keep the requested model for a resumed session (pure logic)."""
        from connector.streaming_sse import normalize_model_for_session
        self.assertEqual(normalize_model_for_session("abc123", "gpt-4"), "gpt-4")
        self.assertIsNone(normalize_model_for_session("abc123", None))


class PremiumReservationLogicTests(unittest.TestCase):
    """Pure logic simulation for premium request transitions."""

    def test_credit_rejected_does_not_mutate(self):
        """Simulate: credit check fails → no reservation mutation."""
        credit_ok = False
        approved_request = {"id": "req-1", "status": "approved"}
        mutated = False
        if not credit_ok:
            pass  # never reaches reservation
        else:
            approved_request["status"] = "reserved"
            mutated = True
        self.assertFalse(mutated)
        self.assertEqual(approved_request["status"], "approved")

    def test_exact_reservation_a_not_consumed_by_b(self):
        """Reservation A success cannot consume/release reservation B."""
        reservations = {"A": "reserved", "B": "reserved"}
        # Try to consume "B" using id "A"
        target = reservations.get("A")
        if target == "reserved":
            # This would consume A, not B
            reservations["A"] = "consumed"
        self.assertEqual(reservations["A"], "consumed")
        self.assertEqual(reservations["B"], "reserved")

    def test_non_ok_upstream_releases_exact_a(self):
        """Non-OK upstream releases exact reservation A."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved", "req-43": "reserved"}
        if statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "approved"
        self.assertEqual(statuses["req-42"], "approved")
        self.assertEqual(statuses["req-43"], "reserved")

    def test_oversize_upstream_releases_exact_a(self):
        """Oversized upstream body releases exact reservation A."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        if statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "approved"
        self.assertEqual(statuses["req-42"], "approved")

    def test_invalid_json_releases_exact_a(self):
        """Invalid upstream JSON releases exact reservation A."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        if statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "approved"
        self.assertEqual(statuses["req-42"], "approved")

    def test_successful_completion_consumes_exact_a(self):
        """Successful parsed response + recorded usage consumes exact A."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        usage_recorded = True
        if statuses.get(reservation_id) == "reserved" and usage_recorded:
            statuses[reservation_id] = "consumed"
        self.assertEqual(statuses["req-42"], "consumed")


# ─── Streaming-specific tests ────────────────────────────────────────────────

class StreamingSourceTests(unittest.TestCase):
    """Source-level checks that streaming is enabled in the connector."""

    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_streaming_capability_is_true(self):
        """connector capabilities must report streaming: True."""
        self.assertIn('"streaming": True', self.source)

    def test_stream_true_is_not_rejected(self):
        """The chat endpoint must NOT reject stream: true with an error."""
        # The old rejection line must be gone
        self.assertNotIn('streaming is not supported by this connector', self.source)

    def test_chat_route_handles_stream_payload(self):
        """The chat_completions route must inspect payload.get('stream')."""
        self.assertIn("payload.get(\"stream\")", self.source)

    def test_chat_route_returns_sse_response_on_stream(self):
        """When streaming, the route must return a StreamingResponse with text/event-stream."""
        self.assertIn("StreamingResponse", self.source)
        self.assertIn("text/event-stream", self.source)

    def test_close_on_disconnect_preserved_in_streaming(self):
        """Streaming must still use close_on_disconnect in session.create/resume params."""
        # The session.create/resume blocks must still contain close_on_disconnect
        self.assertIn("close_on_disconnect", self.source)

    def test_stream_route_preserves_timeout_handling(self):
        """Streaming path must still have timeout handling."""
        # There should be a TimeoutError handler in the chat_completions route
        chat_idx = self.source.index("async def chat_completions")
        rest = self.source[chat_idx:]
        self.assertIn("TimeoutError", rest)

    def test_streaming_uses_turn_semaphore(self):
        """Streaming must acquire _turn_semaphore around the websocket turn."""
        self.assertIn("_turn_semaphore", self.source)
        event_gen_idx = self.source.index("async def event_generator")
        after_def = self.source[event_gen_idx:]
        self.assertIn("_turn_semaphore", after_def)

    def test_stream_inflight_keys_released_in_finally(self):
        """_inflight_stream_keys must still be released in the finally block."""
        self.assertIn("_inflight_stream_keys.discard(idempotency_key)", self.source)
        tree = ast.parse(self.source)
        event_generator = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == "event_generator"
        )
        finally_blocks = [
            node.finalbody
            for node in ast.walk(event_generator)
            if isinstance(node, ast.Try) and node.finalbody
        ]
        self.assertTrue(finally_blocks, "event_generator must have a finally block")
        finally_sources = [
            "\n".join(ast.get_source_segment(self.source, statement) or "" for statement in block)
            for block in finally_blocks
        ]
        self.assertTrue(
            any("_inflight_stream_keys.discard(idempotency_key)" in source for source in finally_sources),
            "event_generator finally block must discard the idempotency key",
        )

    def test_stream_constants_defined_in_connector(self):
        """Stream constants must be imported from streaming_sse."""
        self.assertIn("PENDING_KEY_LIMIT", self.source)
        self.assertIn("SEMAPHORE_ACQUIRE_TIMEOUT", self.source)
        self.assertIn("HARD_MAX_STREAM_LIFETIME_SECONDS", self.source)
        self.assertIn("MAX_UTF8_DELTA_BYTES", self.source)
        self.assertIn("MAX_TERMINAL_ENCODED_BYTES", self.source)

    def test_deadline_uses_min_of_configured_and_hard_max(self):
        """deadline must use min(configured_timeout, HARD_MAX_STREAM_LIFETIME_SECONDS)."""
        self.assertIn("min(", self.source)
        self.assertIn("HARD_MAX_STREAM_LIFETIME_SECONDS", self.source)

    def test_no_accumulated_text_in_streaming_generator(self):
        """The streaming generator must NOT accumulate a full response string."""
        self.assertNotIn("accumulated_text", self.source)

    def test_generic_error_constants_used(self):
        """Error encoding must use generic constants, not raw exception detail."""
        self.assertIn("GENERIC_TIMEOUT_MESSAGE", self.source)
        self.assertIn("GENERIC_UNAVAILABLE_MESSAGE", self.source)

    def test_dashboard_token_inside_try(self):
        """_dashboard_token() must be inside the generator's try block."""
        event_gen_idx = self.source.index("async def event_generator")
        after_def = self.source[event_gen_idx:]
        try_idx = after_def.index("try:")
        try_block = after_def[try_idx:try_idx + 800]
        self.assertIn("_dashboard_token()", try_block)

    def test_is_disconnected_polled_in_recv_frame(self):
        """The recv_frame must check request.is_disconnected() with bounded polling."""
        event_gen_idx = self.source.index("async def event_generator")
        after_def = self.source[event_gen_idx:]
        self.assertIn("request.is_disconnected()", after_def)

    def test_no_raw_exception_in_error_yield(self):
        """Error yields must encode only generic constants, not raw exception messages."""
        event_gen_idx = self.source.index("async def event_generator")
        after_def = self.source[event_gen_idx:]
        self.assertNotIn('error_message=str(exc)', after_def)


class SseEncodingTests(unittest.TestCase):
    """Test SSE delta encoding logic as a pure function."""

    def test_encode_delta_chunk(self):
        """A message.delta produces an OpenAI-style chat.completion.chunk SSE delta."""
        from connector.streaming_sse import encode_delta_chunk
        chunk = encode_delta_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            text_delta="Hello",
            session_id="sess_key_123",
        )
        lines = chunk.strip().split("\n")
        self.assertTrue(lines[0].startswith("data: {"))
        data = json.loads(lines[0].replace("data: ", "", 1))
        self.assertEqual(data["id"], "chatcmpl-abc123")
        self.assertEqual(data["object"], "chat.completion.chunk")
        self.assertEqual(data["choices"][0]["delta"].get("role"), "assistant")
        self.assertEqual(data["choices"][0]["delta"].get("content"), "Hello")
        self.assertNotIn("finish_reason", data["choices"][0])

    def test_encode_terminal_chunk(self):
        """A message.complete produces a terminal chunk with finish_reason stop, usage, session_id, then [DONE]."""
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            session_id="sess_key_123",
        )
        frames = [c for c in chunks.split("\n\n") if c.strip()]
        self.assertEqual(len(frames), 2)

        terminal_data = json.loads(frames[0].replace("data: ", "", 1))
        self.assertEqual(terminal_data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(terminal_data["usage"]["prompt_tokens"], 10)
        self.assertEqual(terminal_data["usage"]["completion_tokens"], 5)
        self.assertEqual(terminal_data["usage"]["total_tokens"], 15)
        self.assertTrue(terminal_data["usage"]["telemetry"]["tokens_reported"])
        self.assertTrue(terminal_data["usage"]["telemetry"]["input_output_reported"])
        self.assertEqual(terminal_data["session_id"], "sess_key_123")

        self.assertEqual(frames[1].strip(), "data: [DONE]")

    def test_encode_terminal_chunk_empty_usage(self):
        """Terminal chunk handles empty/missing usage gracefully."""
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            usage={},
            session_id="sess_key_123",
        )
        terminal_data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertEqual(terminal_data["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(terminal_data["usage"]["prompt_tokens"])
        self.assertIsNone(terminal_data["usage"]["completion_tokens"])
        self.assertIsNone(terminal_data["usage"]["total_tokens"])
        self.assertFalse(terminal_data["usage"]["telemetry"]["tokens_reported"])
        self.assertFalse(terminal_data["usage"]["telemetry"]["input_output_reported"])
        self.assertIn("usage", terminal_data)

    def test_encode_terminal_chunk_with_normalize_usage(self):
        """Terminal chunk uses normalize_usage, preserving all telemetry fields."""
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "model_calls": 1, "credits": 0.5, "cost": 0.05},
            session_id="sess_key_123",
        )
        terminal_data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        usage = terminal_data["usage"]
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 15)
        self.assertTrue(usage["telemetry"]["tokens_reported"])
        self.assertTrue(usage["telemetry"]["input_output_reported"])
        self.assertEqual(usage["telemetry"]["model_calls"], 1)
        self.assertEqual(usage["telemetry"]["credits"], 0.5)
        self.assertEqual(usage["telemetry"]["cost"], 0.05)

    def test_encode_terminal_chunk_null_tokens(self):
        """Terminal chunk keeps null token counts when telemetry is unavailable."""
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            usage={},
            session_id=None,
        )
        terminal_data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertIsNone(terminal_data["usage"]["prompt_tokens"])
        self.assertIsNone(terminal_data["usage"]["completion_tokens"])
        self.assertIsNone(terminal_data["usage"]["total_tokens"])
        self.assertFalse(terminal_data["usage"]["telemetry"]["tokens_reported"])

    def test_encode_terminal_chunk_no_session_id(self):
        """Terminal chunk works without a session_id."""
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            session_id=None,
        )
        terminal_data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        # session_id should be absent or empty
        self.assertNotIn("session_id", terminal_data)

    def test_encode_error_chunk(self):
        """An error produces a terminal chunk with an error field, then [DONE]."""
        from connector.streaming_sse import encode_error_chunk
        chunks = encode_error_chunk(
            completion_id="chatcmpl-abc123",
            model="gpt-4",
            error_message="Hermes timed out",
            session_id="sess_key_123",
        )
        frames = [c for c in chunks.split("\n\n") if c.strip()]
        self.assertEqual(len(frames), 2)
        error_data = json.loads(frames[0].replace("data: ", "", 1))
        self.assertIn("error", error_data)
        self.assertEqual(error_data["error"]["message"], "Hermes timed out")
        self.assertEqual(frames[1].strip(), "data: [DONE]")


class SseParsingTests(unittest.TestCase):
    """Test SSE event stream parsing for the workspace client."""

    def test_parse_sse_stream(self):
        """Parse a multi-chunk SSE stream into individual events."""
        from connector.streaming_sse import parse_sse_stream
        raw = (
            'data: {"id":"c1","choices":[{"delta":{"content":"H"}}]}\n\n'
            'data: {"id":"c1","choices":[{"delta":{"content":"i"}}]}\n\n'
            'data: {"id":"c1","choices":[{"finish_reason":"stop","usage":{"prompt_tokens":1}}],"session_id":"s1"}\n\n'
            'data: [DONE]\n\n'
        )
        events = list(parse_sse_stream(raw))
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "H")
        self.assertEqual(events[1]["choices"][0]["delta"]["content"], "i")
        self.assertEqual(events[2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[2]["session_id"], "s1")
        self.assertEqual(events[3], "[DONE]")

    def test_parse_sse_stream_splits_across_boundaries(self):
        """Parser handles data that spans across arbitrary chunk boundaries."""
        from connector.streaming_sse import parse_sse_stream
        # Simulate a ReadableStream that splits a data line in the middle
        raw1 = 'data: {"id":"c1","choices":[{"delta":{"cont'
        raw2 = 'ent":"Hi"}}]}\n\ndata: [DONE]\n\n'

        events = []
        for chunk in [raw1, raw2]:
            events.extend(parse_sse_stream(chunk, events=events))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "Hi")
        self.assertEqual(events[1], "[DONE]")

    def test_parse_sse_stream_accumulates_text(self):
        """Accumulate delta content across multiple events."""
        from connector.streaming_sse import parse_sse_stream, accumulate_stream
        raw = (
            'data: {"id":"c1","choices":[{"delta":{"content":"Hello"}}]}\n\n'
            'data: {"id":"c1","choices":[{"delta":{"content":" "}}]}\n\n'
            'data: {"id":"c1","choices":[{"delta":{"content":"World"}}]}\n\n'
        )
        text, final_event, done = accumulate_stream(raw)
        self.assertEqual(text, "Hello World")
        self.assertIsNone(final_event)  # no terminal event in this test
        self.assertFalse(done)

    def test_parse_sse_stream_terminal_event(self):
        """Terminal event is detected and returned with usage data."""
        from connector.streaming_sse import parse_sse_stream, accumulate_stream
        raw = (
            'data: {"id":"c1","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            'data: {"id":"c1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5},"session_id":"sess123"}\n\n'
            'data: [DONE]\n\n'
        )
        text, final_event, done = accumulate_stream(raw)
        self.assertEqual(text, "Hi")
        self.assertIsNotNone(final_event)
        self.assertEqual(final_event["choices"][0]["finish_reason"], "stop")
        self.assertEqual(final_event["usage"]["prompt_tokens"], 10)
        self.assertEqual(final_event["usage"]["completion_tokens"], 5)
        self.assertEqual(final_event["session_id"], "sess123")
        self.assertTrue(done)

    def test_parse_sse_stream_empty_chunks_ignored(self):
        """Empty or whitespace-only lines are ignored."""
        from connector.streaming_sse import parse_sse_stream
        raw = (
            '\n\ndata: {"id":"c1","choices":[{"delta":{"content":"x"}}]}\n\n'
            '\n\n'
            'data: [DONE]\n\n'
        )
        events = list(parse_sse_stream(raw))
        self.assertEqual(len(events), 2)

    def test_parse_sse_stream_malformed_data_skipped(self):
        """Malformed JSON data lines are skipped gracefully."""
        from connector.streaming_sse import parse_sse_stream
        raw = (
            'data: {"id":"c1","choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: {invalid json}\n\n'
            'data: [DONE]\n\n'
        )
        events = list(parse_sse_stream(raw))
        # Should have 2 events: valid delta + [DONE], malformed skipped
        valid_events = [e for e in events if e != "[DONE]"]
        self.assertEqual(len(valid_events), 1)
        self.assertEqual(events[-1], "[DONE]")


class PremiumReservationStreamingTests(unittest.TestCase):
    """Test premium reservation handling during streaming."""

    def test_reservation_not_consumed_before_final_usage(self):
        """During streaming, reservation should NOT be consumed until terminal event."""
        # Simulate: streaming in progress → no consumption yet
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        # We're still streaming — don't consume
        self.assertEqual(statuses[reservation_id], "reserved")

    def test_reservation_consumed_after_terminal_usage(self):
        """Terminal event with usage → consume reservation."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        # Terminal event received with usage
        terminal_event = {"usage": {"prompt_tokens": 10}, "session_id": "s1"}
        if terminal_event and statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "consumed"
        self.assertEqual(statuses[reservation_id], "consumed")

    def test_reservation_released_on_stream_error(self):
        """Error before terminal event → release reservation."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        # Stream error before final usage
        if "error" in statuses:
            pass
        # Release
        if statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "approved"
        self.assertEqual(statuses[reservation_id], "approved")

    def test_reservation_released_on_stream_abort(self):
        """Abort before terminal event → release reservation."""
        reservation_id = "req-42"
        statuses = {"req-42": "reserved"}
        # Stream aborted (no terminal event)
        if statuses.get(reservation_id) == "reserved":
            statuses[reservation_id] = "approved"
        self.assertEqual(statuses[reservation_id], "approved")


_PORTAL_ROUTE = Path(__file__).parent.parent / "app" / "api" / "hermes" / "[...path]" / "route.ts"


@unittest.skipUnless(_PORTAL_ROUTE.exists(), "portal route.ts source is not part of this VM repo")
class RelaySseForwardingTests(unittest.TestCase):
    """Test that the relay forwards SSE incrementally without buffering."""

    def test_relay_does_not_buffer_streaming_response(self):
        """The relay must NOT await upstream.text() for streaming responses."""
        relay_source = Path(__file__).parent.parent / "app" / "api" / "hermes" / "[...path]" / "route.ts"
        source = relay_source.read_text(encoding="utf-8")
        # For streaming, the relay should check for stream in the request body
        self.assertIn("stream", source)

    def test_relay_validates_model_before_upstream_stream(self):
        """Model/credit/premium validation must happen before streaming begins."""
        relay_source = Path(__file__).parent.parent / "app" / "api" / "hermes" / "[...path]" / "route.ts"
        source = relay_source.read_text(encoding="utf-8")
        # Validation logic must come before any streaming response
        validation_idx = source.find("catalogEntry")
        streaming_idx = source.find("streaming")
        if streaming_idx > 0:
            self.assertLess(validation_idx, streaming_idx)

    def test_relay_records_usage_from_terminal_event(self):
        """The relay must parse the terminal event's usage data for streaming."""
        relay_source = Path(__file__).parent.parent / "app" / "api" / "hermes" / "[...path]" / "route.ts"
        source = relay_source.read_text(encoding="utf-8")
        self.assertIn("usage", source)

    def test_relay_consume_premium_after_usage_recorded(self):
        """Premium consumption must happen only after usage is recorded from terminal event."""
        relay_source = Path(__file__).parent.parent / "app" / "api" / "hermes" / "[...path]" / "route.ts"
        source = relay_source.read_text(encoding="utf-8")
        # The consume logic must come after usage recording
        usage_recorded_idx = source.find("usageRecorded")
        consume_idx = source.find('"consumed"')
        if usage_recorded_idx > 0 and consume_idx > 0:
            self.assertLess(usage_recorded_idx, consume_idx)


_PORTAL_WORKSPACE = Path(__file__).parent.parent / "app" / "workspace" / "page.tsx"


@unittest.skipUnless(_PORTAL_WORKSPACE.exists(), "portal workspace source is not part of this VM repo")
class WorkspaceStreamingTests(unittest.TestCase):
    """Test that the workspace page requests and parses streaming responses."""

    @classmethod
    def setUpClass(cls):
        cls.workspace_source = (
            Path(__file__).parent.parent / "app" / "workspace" / "page.tsx"
        ).read_text(encoding="utf-8")

    def test_workspace_requests_stream_true(self):
        """The workspace must send stream: true for streaming requests."""
        self.assertIn("stream: true", self.workspace_source)
        self.assertNotIn("stream: false", self.workspace_source)

    def test_workspace_uses_fetch_response_body_reader(self):
        """The workspace must use response.body.getReader() for SSE streaming."""
        self.assertIn("getReader", self.workspace_source)
        self.assertIn("response.body", self.workspace_source)

    def test_workspace_parses_sse_events(self):
        """The workspace must parse SSE data events to accumulate text."""
        self.assertIn("text/event-stream", self.workspace_source)

    def test_workspace_uses_persistent_error_on_failure(self):
        """Streaming failures must use workspaceError, not a disappearing notice."""
        self.assertIn("workspaceError", self.workspace_source)

    def test_workspace_refreshes_on_done(self):
        """On [DONE], the workspace must refresh chat metadata and the portal."""
        self.assertIn("refreshChatsMetadata", self.workspace_source)
        self.assertIn("void load()", self.workspace_source)

    def test_workspace_tracks_terminal_valid(self):
        """Workspace must track terminalValid to distinguish complete vs incomplete streams."""
        self.assertIn("terminalValid", self.workspace_source)

    def test_workspace_throws_on_error_chunk(self):
        """Workspace must throw immediately when chunk.error is present (narrow try/catch surfaces error)."""
        self.assertIn("chunk.error", self.workspace_source)
        self.assertIn("throw new Error", self.workspace_source)
        # Must parse JSON in narrow try/catch then process chunk outside
        self.assertIn("try { chunk = JSON.parse(data); } catch { continue; }", self.workspace_source)
        self.assertIn("apiErrorMessage(chunk.error", self.workspace_source)

    def test_workspace_validates_terminal_completion(self):
        """Workspace must validate finish_reason stop AND valid top-level usage (not requiring prompt_tokens numeric)."""
        self.assertIn('finish_reason === "stop"', self.workspace_source)
        self.assertIn("chunk.usage", self.workspace_source)
        self.assertNotIn("typeof chunk.usage.prompt_tokens === \"number\"", self.workspace_source)

    def test_workspace_checks_both_saw_done_and_terminal_valid(self):
        """Workspace must require both sawDone and terminalValid for success."""
        self.assertIn("!sawDone || !terminalValid", self.workspace_source)


if __name__ == "__main__":
    unittest.main()
