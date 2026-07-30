"""Pure SSE encoding / parsing helpers for the Hermes Classroom streaming path.

These functions are intentionally framework-free so they can be unit-tested
without FastAPI / websockets / HTTPX dependencies.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

try:  # Package import during tests; top-level import when installed as connector scripts.
    from .telemetry import normalize_usage
except ImportError:  # pragma: no cover - deployment layout
    from telemetry import normalize_usage


PENDING_KEY_LIMIT: int = 64
SEMAPHORE_ACQUIRE_TIMEOUT: float = 60.0
HARD_MAX_STREAM_LIFETIME_SECONDS: float = 600.0
MAX_UTF8_DELTA_BYTES: int = 512_000
MAX_TERMINAL_ENCODED_BYTES: int = 1_048_576

GENERIC_TIMEOUT_MESSAGE = "Hermes timed out"
GENERIC_UNAVAILABLE_MESSAGE = "Hermes is unavailable"
GENERIC_INCOMPLETE_MESSAGE = "Hermes response was incomplete"

ALLOWED_ERROR_MESSAGES: frozenset = frozenset({
    GENERIC_TIMEOUT_MESSAGE,
    GENERIC_UNAVAILABLE_MESSAGE,
    GENERIC_INCOMPLETE_MESSAGE,
})


def normalize_model_for_session(session_key: str | None, model: str | None) -> str | None:
    return None if session_key else model


def completion_text_fallback(payload: dict, *, saw_text_delta: bool) -> str:
    if saw_text_delta:
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def encode_delta_chunk(
    *,
    completion_id: str,
    model: str | None,
    text_delta: str,
    session_id: str | None,
    index: int = 0,
) -> str:
    delta: dict[str, Any] = {"role": "assistant", "content": text_delta}
    choice: dict[str, Any] = {"index": index, "delta": delta}
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
    }
    if session_id:
        chunk["session_id"] = session_id
    chunk["choices"] = [choice]
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def encode_terminal_chunk(
    *,
    completion_id: str,
    model: str | None,
    usage: dict | None,
    session_id: str | None,
    index: int = 0,
) -> str:
    norm: dict[str, Any] = normalize_usage(usage) if isinstance(usage, dict) else {}
    choice: dict[str, Any] = {
        "index": index,
        "message": {"role": "assistant", "content": ""},
        "finish_reason": "stop",
    }
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [choice],
        "usage": norm,
    }
    if session_id:
        chunk["session_id"] = session_id
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def encode_clarify_chunk(
    *,
    completion_id: str,
    model: str | None,
    token: str,
    question: str,
    choices: list[str],
    multi_select: bool = False,
    session_id: str | None,
) -> str:
    """Encode a clarify.request into an SSE chunk.

    The chunk follows the same outer shape as delta/error chunks so the
    workspace SSE parser can handle it uniformly.  Only the safe fields
    (*question*, *choices*, *multi_select*, *token*) appear in the nested
    ``clarify.payload`` dict — no gateway ``request_id``, prompt, history,
    or internal state is exposed.
    """
    payload: dict[str, object] = {
        "type": "clarify",
        "payload": {
            "token": token,
            "question": question,
            "choices": choices,
            "multi_select": multi_select,
        },
    }
    chunk: dict[str, object] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}}
        ],
        "clarify": payload,
    }
    if session_id:
        chunk["session_id"] = session_id
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def encode_error_chunk(
    *,
    completion_id: str,
    model: str | None,
    error_message: str,
    session_id: str | None,
) -> str:
    if error_message not in ALLOWED_ERROR_MESSAGES:
        error_message = GENERIC_UNAVAILABLE_MESSAGE
    chunk: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
        "error": {"message": error_message, "type": "invalid_request_error"},
    }
    if session_id:
        chunk["session_id"] = session_id
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def parse_sse_stream(raw: str, events: list | None = None) -> list:
    if events is None:
        events = []
    pending = ""
    if events and isinstance(events[-1], str):
        pending = events.pop()
    buffer = pending + raw
    frames = buffer.split("\n\n")
    last = frames.pop() if frames else buffer
    result: list = []
    for frame in frames:
        frame = frame.strip()
        if not frame:
            continue
        if not frame.startswith("data: "):
            continue
        payload = frame[6:]
        if payload == "[DONE]":
            result.append("[DONE]")
            continue
        try:
            result.append(json.loads(payload))
        except (json.JSONDecodeError, ValueError):
            pass
    if last.strip():
        events.append(last)
    return result


def accumulate_stream(raw: str) -> tuple[str, dict | None, bool]:
    events = parse_sse_stream(raw)
    text_parts: list[str] = []
    terminal_event: dict | None = None
    for event in events:
        if event == "[DONE]":
            return "".join(text_parts), terminal_event, True
        if not isinstance(event, dict):
            continue
        choices = event.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            finish_reason = choice.get("finish_reason")
            if finish_reason == "stop":
                terminal_event = event
    return "".join(text_parts), terminal_event, False


def validate_completion_fallback_text(
    text: str | None,
    max_bytes: int,
    saw_text_delta: bool = False,
) -> str:
    """Validate completion-only fallback text against a UTF-8 byte cap.

    When *saw_text_delta* is true no fallback text is needed, so an empty
    string is returned immediately (preserving the no-duplicate behaviour).

    Otherwise the text is returned as-is when its UTF-8 encoding is within
    *max_bytes*.  Text that exceeds the cap raises ``ValueError`` — silent
    truncation is not permitted.
    """
    if saw_text_delta:
        return ""
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(
            f"Completion-only fallback text is {len(encoded)} bytes, "
            f"exceeds the {max_bytes}-byte UTF-8 cap"
        )
    return text
