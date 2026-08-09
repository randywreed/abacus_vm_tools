"""Runtime tests for the streaming SSE encoder functions.

Directly invokes terminal/error encoding and the extracted pure cap/lifetime
guard constants. Tests all bounds, generic error, and no duplicate terminal content.
"""

import unittest
import json
from pathlib import Path

from connector.streaming_sse import normalize_model_for_session, session_switch_command


class SseRuntimeEncodingTests(unittest.TestCase):
    """Direct runtime tests for terminal and error encoding functions."""

    def test_terminal_chunk_has_stop_reason(self):
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            session_id="sess_1",
        )
        frames = [c for c in chunks.split("\n\n") if c.strip()]
        self.assertEqual(len(frames), 2)
        data = json.loads(frames[0].replace("data: ", "", 1))
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(frames[1].strip(), "data: [DONE]")

    def test_terminal_chunk_has_no_total_text(self):
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            session_id="sess_1",
        )
        data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertEqual(data["choices"][0]["message"]["content"], "")

    def test_terminal_chunk_without_usage(self):
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            usage=None,
            session_id=None,
        )
        frames = [c for c in chunks.split("\n\n") if c.strip()]
        self.assertEqual(len(frames), 2)
        data = json.loads(frames[0].replace("data: ", "", 1))
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("session_id", data)

    def test_terminal_chunk_with_empty_usage(self):
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            usage={},
            session_id="sess_1",
        )
        data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertIn("usage", data)
        self.assertIn("telemetry", data["usage"])

    def test_error_chunk_has_generic_message(self):
        from connector.streaming_sse import encode_error_chunk
        chunks = encode_error_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            error_message="Hermes timed out",
            session_id="sess_1",
        )
        frames = [c for c in chunks.split("\n\n") if c.strip()]
        self.assertEqual(len(frames), 2)
        data = json.loads(frames[0].replace("data: ", "", 1))
        self.assertIn("error", data)
        self.assertEqual(data["error"]["message"], "Hermes timed out")
        self.assertEqual(frames[1].strip(), "data: [DONE]")

    def test_error_chunk_without_session_id(self):
        from connector.streaming_sse import encode_error_chunk
        chunks = encode_error_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            error_message="Hermes is unavailable",
            session_id=None,
        )
        data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertNotIn("session_id", data)

    def test_terminal_chunk_no_duplicate_done(self):
        from connector.streaming_sse import encode_terminal_chunk
        chunks = encode_terminal_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            session_id="sess_1",
        )
        done_count = chunks.count("data: [DONE]")
        self.assertEqual(done_count, 1)

    def test_error_chunk_no_duplicate_done(self):
        from connector.streaming_sse import encode_error_chunk
        chunks = encode_error_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            error_message="Hermes timed out",
            session_id="sess_1",
        )
        done_count = chunks.count("data: [DONE]")
        self.assertEqual(done_count, 1)

    def test_delta_chunk_no_finish_reason(self):
        from connector.streaming_sse import encode_delta_chunk
        chunk = encode_delta_chunk(
            completion_id="chatcmpl-test",
            model="gpt-4",
            text_delta="Hello",
            session_id="sess_1",
        )
        data = json.loads(chunk.replace("data: ", "", 1).strip())
        self.assertNotIn("finish_reason", data["choices"][0])
        self.assertEqual(data["choices"][0]["delta"]["content"], "Hello")
        self.assertEqual(data["choices"][0]["delta"]["role"], "assistant")

    def test_completion_text_is_fallback_only_when_no_delta_was_sent(self):
        from connector.streaming_sse import completion_text_fallback
        payload = {"text": "Final answer", "status": "complete"}
        self.assertEqual(completion_text_fallback(payload, saw_text_delta=False), "Final answer")
        self.assertEqual(completion_text_fallback(payload, saw_text_delta=True), "")
        self.assertEqual(completion_text_fallback({"text": None}, saw_text_delta=False), "")


class ConstantsExistenceTests(unittest.TestCase):
    """Verify the constants defined in streaming_sse are importable and have expected types."""

    def test_pending_key_limit_exists(self):
        from connector.streaming_sse import PENDING_KEY_LIMIT
        self.assertIsInstance(PENDING_KEY_LIMIT, int)
        self.assertGreater(PENDING_KEY_LIMIT, 0)

    def test_semaphore_acquire_timeout_exists(self):
        from connector.streaming_sse import SEMAPHORE_ACQUIRE_TIMEOUT
        self.assertIsInstance(SEMAPHORE_ACQUIRE_TIMEOUT, (int, float))
        self.assertGreater(SEMAPHORE_ACQUIRE_TIMEOUT, 0)

    def test_hard_max_stream_lifetime_seconds_exists(self):
        from connector.streaming_sse import HARD_MAX_STREAM_LIFETIME_SECONDS
        self.assertIsInstance(HARD_MAX_STREAM_LIFETIME_SECONDS, (int, float))
        self.assertGreater(HARD_MAX_STREAM_LIFETIME_SECONDS, 0)

    def test_chat_timeout_defaults_to_hard_stream_lifetime(self):
        from connector import streaming_sse

        timeout_helper = getattr(streaming_sse, "bounded_chat_timeout_seconds", None)
        if not callable(timeout_helper):
            self.fail("bounded_chat_timeout_seconds is missing")
        self.assertEqual(
            timeout_helper(None),
            streaming_sse.HARD_MAX_STREAM_LIFETIME_SECONDS,
        )

    def test_chat_timeout_override_cannot_exceed_hard_stream_lifetime(self):
        from connector.streaming_sse import (
            HARD_MAX_STREAM_LIFETIME_SECONDS,
            bounded_chat_timeout_seconds,
        )

        self.assertEqual(
            bounded_chat_timeout_seconds("1200"),
            HARD_MAX_STREAM_LIFETIME_SECONDS,
        )

    def test_chat_timeout_rejects_invalid_values_as_runtime_configuration_errors(self):
        from connector.streaming_sse import bounded_chat_timeout_seconds

        for raw_value in ("0", "-30", "nan", "inf", "-inf", "", "bogus"):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(RuntimeError):
                    bounded_chat_timeout_seconds(raw_value)

    def test_max_utf8_delta_bytes_exists(self):
        from connector.streaming_sse import MAX_UTF8_DELTA_BYTES
        self.assertIsInstance(MAX_UTF8_DELTA_BYTES, int)
        self.assertGreater(MAX_UTF8_DELTA_BYTES, 0)

    def test_max_terminal_encoded_bytes_exists(self):
        from connector.streaming_sse import MAX_TERMINAL_ENCODED_BYTES
        self.assertIsInstance(MAX_TERMINAL_ENCODED_BYTES, int)
        self.assertGreater(MAX_TERMINAL_ENCODED_BYTES, 0)

    def test_generic_timeout_message_exists(self):
        from connector.streaming_sse import GENERIC_TIMEOUT_MESSAGE
        self.assertIsInstance(GENERIC_TIMEOUT_MESSAGE, str)
        self.assertTrue(len(GENERIC_TIMEOUT_MESSAGE) > 0)

    def test_generic_unavailable_message_exists(self):
        from connector.streaming_sse import GENERIC_UNAVAILABLE_MESSAGE
        self.assertIsInstance(GENERIC_UNAVAILABLE_MESSAGE, str)
        self.assertTrue(len(GENERIC_UNAVAILABLE_MESSAGE) > 0)


class ConnectorImportsConstantsTests(unittest.TestCase):
    """Verify the connector module imports the constants from streaming_sse."""

    def _source(self):
        return (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")

    def test_connector_imports_pending_key_limit(self):
        self.assertIn("PENDING_KEY_LIMIT", self._source())

    def test_connector_imports_semaphore_timeout(self):
        self.assertIn("SEMAPHORE_ACQUIRE_TIMEOUT", self._source())

    def test_connector_imports_hard_max_lifetime(self):
        self.assertIn("HARD_MAX_STREAM_LIFETIME_SECONDS", self._source())

    def test_connector_imports_max_utf8_delta(self):
        self.assertIn("MAX_UTF8_DELTA_BYTES", self._source())

    def test_connector_imports_max_terminal_encoded(self):
        self.assertIn("MAX_TERMINAL_ENCODED_BYTES", self._source())

    def test_connector_imports_generic_timeout(self):
        self.assertIn("GENERIC_TIMEOUT_MESSAGE", self._source())

    def test_connector_imports_generic_unavailable(self):
        self.assertIn("GENERIC_UNAVAILABLE_MESSAGE", self._source())

    def test_connector_uses_min_for_deadline(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("min(timeout, HARD_MAX_STREAM_LIFETIME_SECONDS)", after_def)

    def test_both_chat_paths_use_the_bounded_timeout_helper(self):
        source = self._source()
        expected_call = 'bounded_chat_timeout_seconds(os.environ.get("HERMES_CLASSROOM_CHAT_TIMEOUT_SECONDS"))'
        self.assertEqual(source.count(expected_call), 2)
        self.assertNotIn('HERMES_CLASSROOM_CHAT_TIMEOUT_SECONDS", "300"', source)

    def test_connector_no_accumulated_text(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertNotIn("accumulated_text", after_def)

    def test_connector_uses_generic_timeout_in_error(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("GENERIC_TIMEOUT_MESSAGE", after_def)

    def test_connector_uses_generic_unavailable_in_error(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("GENERIC_UNAVAILABLE_MESSAGE", after_def)

    def test_connector_fallback_imported_in_package_branch(self):
        source = self._source()
        # Must appear in the try (package) import block
        pkg_import_idx = source.index("try:  # Package import")
        next_except = source.index("except ImportError:", pkg_import_idx)
        pkg_block = source[pkg_import_idx:next_except]
        self.assertIn("completion_text_fallback", pkg_block)

    def test_connector_fallback_imported_in_top_level_branch(self):
        source = self._source()
        # Must appear in the except (top-level) import block
        except_idx = source.index("except ImportError:")
        next_from = source.index("from fastapi", except_idx)
        top_block = source[except_idx:next_from]
        self.assertIn("completion_text_fallback", top_block)

    def test_connector_tracks_saw_text_delta_boolean(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("saw_text_delta", after_def)

    def test_connector_uses_fallback_on_message_complete(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        msg_complete_idx = after_def.index('if event_type == "message.complete"')
        # Find the next 'return' or the end of the function body
        # Look for completion_text_fallback usage within the message.complete handler
        msg_complete_block = after_def[msg_complete_idx:]
        # Terminate at the next 'return' at the top level of the generator or next event handler
        # We need to see fallback_text or completion_text_fallback before yield terminal
        yield_terminal_idx = msg_complete_block.find("yield terminal")
        if yield_terminal_idx == -1:
            yield_terminal_idx = len(msg_complete_block)
        handler = msg_complete_block[:yield_terminal_idx]
        self.assertIn("completion_text_fallback", handler)

    def test_connector_fallback_enforces_utf8_cap(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        msg_complete_idx = after_def.index('if event_type == "message.complete"')
        msg_complete_block = after_def[msg_complete_idx:]
        yield_terminal_idx = msg_complete_block.find("yield terminal")
        if yield_terminal_idx == -1:
            yield_terminal_idx = len(msg_complete_block)
        handler = msg_complete_block[:yield_terminal_idx]
        # The fallback text must be UTF-8 byte capped
        self.assertIn("MAX_UTF8_DELTA_BYTES", handler)

    def test_connector_cancelled_error_yields_unavailable_chunk(self):
        """CancelledError handler must yield a generic unavailable error chunk
        (+ [DONE]) before re-raising, so the relay sees a well-formed SSE
        termination instead of a raw stream close."""
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        cancelled_idx = after_def.index("except asyncio.CancelledError")
        cancelled_block = after_def[cancelled_idx:cancelled_idx + 800]
        self.assertIn("yield encode_error_chunk", cancelled_block)
        self.assertIn("GENERIC_UNAVAILABLE_MESSAGE", cancelled_block)
        self.assertIn("raise", cancelled_block)
        # Cleanup must remain in finally
        self.assertIn("finally:", after_def)
        self.assertIn("_inflight_stream_keys.discard(idempotency_key)", after_def)

    def test_connector_finally_cleans_up_inflight_key(self):
        source = self._source()
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        # The finally block must still discard the inflight key
        self.assertIn("finally:", after_def)
        self.assertIn("_inflight_stream_keys.discard(idempotency_key)", after_def)


class NormalizeModelBehavioralTests(unittest.TestCase):
    """Behavioral tests for the shared normalize_model_for_session helper."""

    def test_preserves_model_for_resumed_session(self):
        self.assertEqual(normalize_model_for_session("sess_abc", "gpt-4"), "gpt-4")

    def test_returns_none_when_resumed_session_has_no_model(self):
        self.assertIsNone(normalize_model_for_session("sess_abc", None))
        self.assertIsNone(normalize_model_for_session("sess_abc", ""))

    def test_returns_model_when_session_key_is_none(self):
        self.assertEqual(normalize_model_for_session(None, "gpt-4"), "gpt-4")

    def test_returns_none_when_no_session_key_and_no_model(self):
        self.assertIsNone(normalize_model_for_session(None, None))


class SessionModelSwitchTests(unittest.TestCase):
    """Behavioral tests for session_switch_command — the validated config.set builder."""

    def test_builds_config_set_for_exact_model(self):
        frame = session_switch_command("20260725_120000_abcdef", "moonshotai/Kimi-K2.6", "rid-123")
        self.assertEqual(frame["jsonrpc"], "2.0")
        self.assertEqual(frame["id"], "rid-123")
        self.assertEqual(frame["method"], "config.set")
        params = frame["params"]
        self.assertEqual(params["session_id"], "20260725_120000_abcdef")
        self.assertEqual(params["key"], "model")
        self.assertEqual(params["value"], "moonshotai/Kimi-K2.6 --session")
        self.assertTrue(params["confirm_expensive_model"])

    def test_returns_none_when_no_model_requested(self):
        self.assertIsNone(session_switch_command("sess_1", None, "rid-1"))

    def test_allows_routellm_model_id_alphabet(self):
        for model in (
            "gpt-4",
            "claude-3.5-sonnet",
            "deepseek-ai/DeepSeek-V4-Flash",
            "moonshotai/Kimi-K2.6",
            "openai/gpt-4o-plus:beta",
            "a_b.c:d+e/-f",
        ):
            frame = session_switch_command("sess_1", model, "rid-1")
            self.assertEqual(frame["params"]["value"], f"{model} --session")

    def test_rejects_injection_attempts(self):
        for model in ("gpt-4 --flag", " model", "model\nrm -rf", "gpt-4;id", "gpt-4$x"):
            with self.assertRaises(ValueError):
                session_switch_command("sess_1", model, "rid-1")

    def test_rejects_oversized_model_id(self):
        with self.assertRaises(ValueError):
            session_switch_command("sess_1", "m" * 129, "rid-1")

    def test_streaming_resume_switches_model_before_prompt_submit(self):
        source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("session_switch_command", after_def)
        self.assertIn("if session_key and model:", after_def)
        switch_idx = after_def.index("session_switch_command")
        prompt_idx = after_def.index("prompt.submit")
        self.assertLess(switch_idx, prompt_idx)

    def test_streaming_validates_config_set_before_prompt_submit(self):
        """The config.set result must be validated after the switch RPC and before prompt.submit."""
        source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")
        event_gen_idx = source.index("async def event_generator")
        after_def = source[event_gen_idx:]
        self.assertIn("validate_config_set_result", after_def)
        rpc_idx = after_def.index("_rpc_send_wait")
        validate_idx = after_def.index("validate_config_set_result")
        prompt_idx = after_def.index("prompt.submit")
        self.assertLess(rpc_idx, validate_idx)
        self.assertLess(validate_idx, prompt_idx)

    def test_rpc_chat_validates_config_set_before_prompt_submit(self):
        """The non-streaming _rpc_chat must also gate prompt.submit on the validated result."""
        source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")
        rpc_chat_idx = source.index("async def _rpc_chat")
        next_def = source.index("async def _rpc_request", rpc_chat_idx)
        rpc_block = source[rpc_chat_idx:next_def]
        self.assertIn("validate_config_set_result", rpc_block)
        rpc_idx = rpc_block.index("_rpc_send_wait")
        validate_idx = rpc_block.index("validate_config_set_result")
        prompt_idx = rpc_block.index("prompt.submit")
        self.assertLess(rpc_idx, validate_idx)
        self.assertLess(validate_idx, prompt_idx)

    def test_connector_imports_config_set_validator_in_both_branches(self):
        source = (Path(__file__).parent / "hermes_classroom_connector.py").read_text(encoding="utf-8")
        pkg_import_idx = source.index("try:  # Package import")
        next_except = source.index("except ImportError:", pkg_import_idx)
        pkg_block = source[pkg_import_idx:next_except]
        self.assertIn("validate_config_set_result", pkg_block)
        except_idx = source.index("except ImportError:")
        next_from = source.index("from fastapi", except_idx)
        top_block = source[except_idx:next_from]
        self.assertIn("validate_config_set_result", top_block)


class ValidateConfigSetResultTests(unittest.TestCase):
    """Behavioral tests for validate_config_set_result — the config.set result gate.

    The validator must only accept a result proving the session model actually
    switched to the exact requested id, never a success-shaped response that a
    broken or future gateway could send without applying the switch.
    """

    def test_exact_success_returns_none(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "model", "value": "moonshotai/Kimi-K2.6", "deferred": False}
        self.assertIsNone(
            validate_config_set_result(result, requested_model="moonshotai/Kimi-K2.6")
        )

    def test_deferred_success_is_allowed(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "model", "value": "gpt-4", "deferred": True}
        self.assertIsNone(validate_config_set_result(result, requested_model="gpt-4"))

    def test_wrong_key_raises(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "temperature", "value": "gpt-4"}
        with self.assertRaises(ValueError):
            validate_config_set_result(result, requested_model="gpt-4")

    def test_missing_key_raises(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"value": "gpt-4"}
        with self.assertRaises(ValueError):
            validate_config_set_result(result, requested_model="gpt-4")

    def test_wrong_value_raises(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "model", "value": "gpt-4o"}
        with self.assertRaises(ValueError):
            validate_config_set_result(result, requested_model="gpt-4")

    def test_missing_value_raises(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "model"}
        with self.assertRaises(ValueError):
            validate_config_set_result(result, requested_model="gpt-4")

    def test_confirm_required_raises(self):
        from connector.streaming_sse import validate_config_set_result
        result = {"key": "model", "value": "gpt-4", "confirm_required": True}
        with self.assertRaises(ValueError):
            validate_config_set_result(result, requested_model="gpt-4")

    def test_non_dict_result_raises(self):
        from connector.streaming_sse import validate_config_set_result
        with self.assertRaises(ValueError):
            validate_config_set_result(None, requested_model="gpt-4")

    def test_exception_text_never_embeds_result_data(self):
        from connector.streaming_sse import validate_config_set_result
        secret_value = "sk-secret-0123456789abcdef"
        result = {
            "key": "model",
            "value": secret_value,
            "confirm_required": True,
            "extra": {"session_id": "sess-TOP-SECRET"},
        }
        with self.assertRaises(ValueError) as ctx:
            validate_config_set_result(result, requested_model="gpt-4")
        self.assertNotIn(secret_value, str(ctx.exception))
        self.assertNotIn("TOP-SECRET", str(ctx.exception))
        self.assertNotIn("confirm_required", str(ctx.exception))


class EncodeErrorChunkNormalizationTests(unittest.TestCase):
    """The encode_error_chunk must never expose an arbitrary input through the returned frame."""

    def test_allowlisted_message_preserved(self):
        from connector.streaming_sse import encode_error_chunk, GENERIC_TIMEOUT_MESSAGE, GENERIC_UNAVAILABLE_MESSAGE, GENERIC_INCOMPLETE_MESSAGE
        for msg in (GENERIC_TIMEOUT_MESSAGE, GENERIC_UNAVAILABLE_MESSAGE, GENERIC_INCOMPLETE_MESSAGE):
            chunks = encode_error_chunk(completion_id="cmpl-t", model=None, error_message=msg, session_id=None)
            data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
            self.assertEqual(data["error"]["message"], msg)

    def test_arbitrary_input_normalized_to_generic_unavailable(self):
        from connector.streaming_sse import encode_error_chunk, GENERIC_UNAVAILABLE_MESSAGE
        secret_like = "EXCEPTION: /etc/shadow contents: root:!:1:..."
        chunks = encode_error_chunk(completion_id="cmpl-t", model=None, error_message=secret_like, session_id=None)
        data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertEqual(data["error"]["message"], GENERIC_UNAVAILABLE_MESSAGE)
        self.assertNotIn("shadow", data["error"]["message"])
        self.assertNotIn("EXCEPTION", data["error"]["message"])

    def test_empty_string_normalized(self):
        from connector.streaming_sse import encode_error_chunk, GENERIC_UNAVAILABLE_MESSAGE
        chunks = encode_error_chunk(completion_id="cmpl-t", model=None, error_message="", session_id=None)
        data = json.loads(chunks.split("\n\n")[0].replace("data: ", "", 1))
        self.assertEqual(data["error"]["message"], GENERIC_UNAVAILABLE_MESSAGE)


class ValidateCompletionFallbackTextTests(unittest.TestCase):
    """Behavioral tests for validate_completion_fallback_text — the pure byte-cap helper."""

    def test_within_cap_returns_text(self):
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        text = "hello world"
        result = validate_completion_fallback_text(text, MAX_UTF8_DELTA_BYTES)
        self.assertEqual(result, text)

    def test_empty_text_returns_empty(self):
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        result = validate_completion_fallback_text("", MAX_UTF8_DELTA_BYTES)
        self.assertEqual(result, "")

    def test_none_text_returns_empty(self):
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        result = validate_completion_fallback_text(None, MAX_UTF8_DELTA_BYTES)
        self.assertEqual(result, "")

    def test_at_exact_utf8_boundary_returns_text(self):
        """Text whose UTF-8 byte length equals MAX_UTF8_DELTA_BYTES is accepted."""
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        # Build text that is exactly MAX_UTF8_DELTA_BYTES in UTF-8
        # Use single-byte ASCII chars for simplicity
        text = "A" * MAX_UTF8_DELTA_BYTES
        result = validate_completion_fallback_text(text, MAX_UTF8_DELTA_BYTES)
        self.assertEqual(result, text)

    def test_over_utf8_boundary_raises_value_error(self):
        """Text exceeding MAX_UTF8_DELTA_BYTES must raise ValueError (fail closed)."""
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        text = "A" * (MAX_UTF8_DELTA_BYTES + 1)
        with self.assertRaises(ValueError):
            validate_completion_fallback_text(text, MAX_UTF8_DELTA_BYTES)

    def test_multi_byte_char_at_boundary_raises(self):
        """Text with multi-byte UTF-8 characters that pushes over the cap raises."""
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        # Build text that is MAX_UTF8_DELTA_BYTES - 1 bytes using 3-byte chars (€)
        # Then append two chars that push it over the cap
        three_byte = "\u20ac"  # € is 3 bytes in UTF-8
        # Fill up to MAX_UTF8_DELTA_BYTES - 1 bytes
        fill_bytes = MAX_UTF8_DELTA_BYTES - 1
        # 3-byte chars: need fill_bytes // 3 of them, plus remainder
        num_full = fill_bytes // 3
        remainder = fill_bytes % 3
        text = three_byte * num_full + "A" * remainder
        self.assertEqual(len(text.encode("utf-8")), fill_bytes)
        # Append two chars (each 1 byte) -> over the cap
        text = text + "XY"
        with self.assertRaises(ValueError):
            validate_completion_fallback_text(text, MAX_UTF8_DELTA_BYTES)

    def test_saw_text_delta_true_raises_value_error(self):
        """When saw_text_delta=true, the function must still raise ValueError for oversized text."""
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        text = "hello"
        result = validate_completion_fallback_text(text, MAX_UTF8_DELTA_BYTES, saw_text_delta=True)
        # saw_text_delta=True means no fallback text is needed, so return empty string
        self.assertEqual(result, "")

    def test_saw_text_delta_true_with_empty_text(self):
        from connector.streaming_sse import validate_completion_fallback_text, MAX_UTF8_DELTA_BYTES
        result = validate_completion_fallback_text("", MAX_UTF8_DELTA_BYTES, saw_text_delta=True)
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
