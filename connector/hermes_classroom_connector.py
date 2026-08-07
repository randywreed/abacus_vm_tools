#!/usr/bin/env python3
"""Hermes Classroom Connector.

This service deliberately has a small, server-to-server surface.  It is not a
browser API and it never exposes the Hermes dashboard token.  Nginx publishes
only ``/hermes-classroom/`` over the Abacus HTTPS hostname and forwards it to
this loopback-only service.

The central course portal signs every request with a per-VM HMAC key.  The
signature is over the public method/path/body digest plus a short-lived
timestamp and one-time nonce, preventing URL substitution and replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Final
from datetime import datetime, timezone

import httpx
import uvicorn
import websockets
from abacus_usage import abacus_api_key_from_config, balance_credit_report, daily_credit_report, build_by_user_snapshot
from idempotency import TurnIdempotency
from session_payloads import sanitize_history
from telemetry import normalize_usage
try:
    from .attachments import (
        AttachmentRegistry,
        AttachmentRejected,
        MAX_FILES,
        MAX_FILE_BYTES,
        MAX_TOTAL_BYTES,
        attachment_purge_loop,
    )
except ImportError:  # pragma: no cover - deployment layout
    from attachments import (  # type: ignore[assignment]
        AttachmentRegistry,
        AttachmentRejected,
        MAX_FILES,
        MAX_FILE_BYTES,
        MAX_TOTAL_BYTES,
        attachment_purge_loop,
    )
try:  # Package import in the repository; top-level import on installed VMs.
    from .clarify_state import (
        ClarifyState,
        CLARIFY_TTL_SECONDS,
        CLARIFY_TOKEN_RE,
        DeferredQueue,
        validate_question,
        validate_choices,
    )
    from .streaming_sse import (
        completion_text_fallback,
        encode_clarify_chunk,
        encode_delta_chunk,
        encode_error_chunk,
        encode_terminal_chunk,
        normalize_model_for_session,
        session_switch_command,
        validate_config_set_result,
        PENDING_KEY_LIMIT,
        SEMAPHORE_ACQUIRE_TIMEOUT,
        HARD_MAX_STREAM_LIFETIME_SECONDS,
        MAX_UTF8_DELTA_BYTES,
        MAX_TERMINAL_ENCODED_BYTES,
        GENERIC_TIMEOUT_MESSAGE,
        GENERIC_UNAVAILABLE_MESSAGE,
        validate_completion_fallback_text,
    )
except ImportError:  # pragma: no cover - deployment layout
    from clarify_state import (  # type: ignore[assignment]
        ClarifyState,
        CLARIFY_TTL_SECONDS,
        CLARIFY_TOKEN_RE,
        DeferredQueue,
        validate_question,
        validate_choices,
    )
    from streaming_sse import (  # type: ignore[assignment]
        completion_text_fallback,
        encode_clarify_chunk,
        encode_delta_chunk,
        encode_error_chunk,
        encode_terminal_chunk,
        normalize_model_for_session,
        session_switch_command,
        validate_config_set_result,
        PENDING_KEY_LIMIT,
        SEMAPHORE_ACQUIRE_TIMEOUT,
        HARD_MAX_STREAM_LIFETIME_SECONDS,
        MAX_UTF8_DELTA_BYTES,
        MAX_TERMINAL_ENCODED_BYTES,
        GENERIC_TIMEOUT_MESSAGE,
        GENERIC_UNAVAILABLE_MESSAGE,
        validate_completion_fallback_text,
    )
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
try:
    from .multipart_uploads import select_uploads
except ImportError:  # pragma: no cover - deployment layout
    from multipart_uploads import select_uploads  # type: ignore[assignment]


PUBLIC_PREFIX: Final = "/hermes-classroom"
HERMES_BASE: Final = os.environ.get("HERMES_LOCAL_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_ENV: Final = Path(os.environ.get("HERMES_ENV_FILE", "/home/ubuntu/.hermes/hermes-serve.env"))
SHARED_SECRET: Final = os.environ["HERMES_CLASSROOM_SHARED_SECRET"].encode("ascii")
MAX_BODY: Final = 1_048_576
# Bounded multipart framing allowance above the 10 MiB attachment content
# policy (attachments.MAX_TOTAL_BYTES) so multipart boundaries/headers never
# consume content budget. This stays far below the framework transport
# ceiling so vinext/next does not reject uploads before this service.
MULTIPART_FRAMING_ALLOWANCE: Final = 64 * 1024
MAX_FILE_REQUEST_BODY: Final = MAX_TOTAL_BYTES + MULTIPART_FRAMING_ALLOWANCE
MAX_CLOCK_SKEW: Final = 60
NONCE_TTL: Final = 300
NONCE_LIMIT: Final = 10_000
NONCE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
try:
    MAX_CONCURRENT_TURNS: Final = max(1, int(os.environ.get("HERMES_CLASSROOM_MAX_CONCURRENT_TURNS", "1")))
except ValueError:
    MAX_CONCURRENT_TURNS = 1

# Exact backend access is intentional: this connector must not become a
# general-purpose remote shell, filesystem, settings, or plugin proxy.
_used_nonces: OrderedDict[str, float] = OrderedDict()
_nonce_lock = asyncio.Lock()
_turn_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
_usage_cache: dict[str, object] = {"expires": 0.0, "value": None}
_usage_lock = asyncio.Lock()
# In-memory clarify state for the resumable clarification bridge
_clarify_state = ClarifyState()

# Keyed by an opaque browser-generated ID. Only a digest of session + prompt is
# retained, never the prompt/transcript itself. Entries are short-lived so a
# network retry receives the exact completed answer without buying another turn.
# A single student VM executes one turn at a time.  Keep enough completed
# entries for normal retry windows without retaining a large batch of model
# responses in process memory.
_idempotency = TurnIdempotency(ttl_seconds=15 * 60, limit=64)
# In-flight streaming key tracking: prevents duplicate agent turns for streaming requests
_inflight_stream_keys: set[str] = set()
_inflight_stream_lock = asyncio.Lock()
_attachment_registry = AttachmentRegistry(Path(os.environ.get("HERMES_ATTACHMENT_DIR", "/var/lib/hermes-classroom-connector/attachments")))


def _dashboard_token() -> str:
    """Read the local Hermes dashboard token without logging or returning it.

    Prefer an explicit systemd EnvironmentFile value.  This supports Abacus
    images that provision the connector token directly while retaining the
    original Hermes environment-file fallback.
    """
    configured = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "").strip()
    if configured:
        return configured
    try:
        for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("HERMES_DASHBOARD_SESSION_TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token:
                    return token
    except OSError as exc:
        raise RuntimeError("Hermes session-token file is unavailable") from exc
    raise RuntimeError("Hermes session token is not configured")


def _canonical(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), path, timestamp, nonce, digest)).encode("utf-8")


async def _consume_nonce(nonce: str) -> bool:
    now = time.monotonic()
    async with _nonce_lock:
        while _used_nonces and next(iter(_used_nonces.values())) < now - NONCE_TTL:
            _used_nonces.popitem(last=False)
        if nonce in _used_nonces:
            return False
        _used_nonces[nonce] = now
        while len(_used_nonces) > NONCE_LIMIT:
            _used_nonces.popitem(last=False)
        return True


async def _authenticate(method: str, path: str, headers, body: bytes, max_body: int = MAX_BODY) -> None:
    timestamp = headers.get("x-hermes-classroom-timestamp", "")
    nonce = headers.get("x-hermes-classroom-nonce", "")
    signature = headers.get("x-hermes-classroom-signature", "")
    if len(body) > max_body or not NONCE_RE.fullmatch(nonce):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        issued = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    if abs(time.time() - issued) > MAX_CLOCK_SKEW:
        raise HTTPException(status_code=401, detail="Unauthorized")
    expected = hmac.new(SHARED_SECRET, _canonical(method, path, timestamp, nonce, body), hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("v1=")
    if not hmac.compare_digest(expected, supplied) or not await _consume_nonce(nonce):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _hermes_headers() -> dict[str, str]:
    return {"X-Hermes-Session-Token": _dashboard_token()}


def _abacus_api_key() -> str:
    """Find the VM's existing Abacus key without ever returning or logging it."""
    direct = os.environ.get("ABACUS_API_KEY", "").strip()
    if direct:
        return direct
    config = Path(os.environ.get("HERMES_CONFIG_FILE", "/home/ubuntu/.hermes/config.yaml"))
    try:
        selected = abacus_api_key_from_config(config.read_text(encoding="utf-8"))
        if selected:
            return selected
    except OSError:
        pass
    raise RuntimeError("Abacus API credentials are unavailable")


async def _abacus_credits() -> dict:
    """Return sanitized, aggregate Abacus credit data with a tiny VM-local cache."""
    async with _usage_lock:
        cached = _usage_cache.get("value")
        if isinstance(cached, dict) and time.monotonic() < float(_usage_cache["expires"]):
            return cached
        headers = {"Authorization": f"Bearer {_abacus_api_key()}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            balance_response = await client.post("https://api.abacus.ai/api/v0/_getUserInfo", headers=headers, json={})
            balance_response.raise_for_status()
            result = balance_credit_report(balance_response.json())
            try:
                log_response = await client.post("https://api.abacus.ai/api/v0/_getOrganizationComputePointLog", headers=headers, json={"byLlm": False, "byUser": False})
                log_response.raise_for_status()
                today = datetime.now().date().isoformat()
                result["daily"] = daily_credit_report(log_response.json(), today)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                # The balance is still useful if Abacus's daily-log service lags.
                pass
        _usage_cache.update({"expires": time.monotonic() + 30, "value": result})
        return result


_snapshot_cache: dict[str, object] = {"expires": 0.0, "value": None}
_snapshot_lock = asyncio.Lock()


async def _abacus_snapshot() -> dict:
    """Return a bounded, sanitized per-user Abacus credit snapshot.

    The byUser:true log has no date field; the Abacus org compute-point log
    is a rolling ~30-day window by contract, so the snapshot period is
    ``rolling_30d`` (never calendar-month). Usernames and non-RouteLLM
    buckets are dropped; only normalized emails plus RouteLLM/total credits
    leave the VM, along with a fetch timestamp for freshness.
    """
    async with _snapshot_lock:
        cached = _snapshot_cache.get("value")
        if isinstance(cached, dict) and time.monotonic() < float(_snapshot_cache["expires"]):  # type: ignore[arg-type]
            return cached
        headers = {"Authorization": f"Bearer {_abacus_api_key()}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            by_user_response = await client.post("https://api.abacus.ai/api/v0/_getOrganizationComputePointLog", headers=headers, json={"byLlm": False, "byUser": True})
            by_user_response.raise_for_status()
            daily_response = await client.post("https://api.abacus.ai/api/v0/_getOrganizationComputePointLog", headers=headers, json={"byLlm": False, "byUser": False})
            daily_response.raise_for_status()
        payload = build_by_user_snapshot(
            by_user_response.json(),
            daily_response.json(),
            generated_at_ms=int(time.time() * 1000),
            today=datetime.now(timezone.utc).date().isoformat(),
        )
        _snapshot_cache.update({"expires": time.monotonic() + 60, "value": payload})
        return payload


MODEL_ID_RE: Final = re.compile(r"^(?=.{1,128}$)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$")
MODEL_TYPE_RE: Final = re.compile(r"^[a-z_]{1,64}$")
RATE_RE: Final = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,18})?$")


def _metadata_text(value: object, limit: int) -> str:
    """Return a bounded, display-safe provider metadata field."""
    return value.strip()[:limit] if isinstance(value, str) else ""


def _token_rate(value: object) -> str | None:
    """Preserve a provider token rate exactly, without floating-point rounding."""
    if isinstance(value, bool):
        return None
    candidate = str(value).strip() if isinstance(value, (str, int, float)) else ""
    return candidate if RATE_RE.fullmatch(candidate) else None


async def _routellm_models() -> list[dict]:
    """Fetch model list from RouteLLM over HTTPS. Returns sanitized {id} entries, max 200."""
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        try:
            key = _abacus_api_key()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="Model list unavailable")
        try:
            response = await client.get(
                "https://routellm.abacus.ai/v1/models",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            raise HTTPException(status_code=503, detail="Model list unavailable")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise HTTPException(status_code=503, detail="Invalid model list response")
    result: list[dict] = []
    for entry in data["data"][:200]:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if isinstance(raw_id, str):
            trimmed = raw_id.strip()
            if trimmed and MODEL_ID_RE.fullmatch(trimmed):
                display_name = _metadata_text(entry.get("display_name"), 160) or trimmed
                model_type = _metadata_text(entry.get("model_type"), 64)
                item: dict[str, str] = {"id": trimmed, "object": "model", "display_name": display_name}
                if model_type and MODEL_TYPE_RE.fullmatch(model_type):
                    item["model_type"] = model_type
                input_token_rate = _token_rate(entry.get("input_token_rate"))
                output_token_rate = _token_rate(entry.get("output_token_rate"))
                if input_token_rate is not None:
                    item["input_token_rate"] = input_token_rate
                if output_token_rate is not None:
                    item["output_token_rate"] = output_token_rate
                result.append(item)
    return result


async def _local_get(path: str) -> httpx.Response:
    timeout = httpx.Timeout(connect=3.0, read=25.0, write=5.0, pool=3.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(f"{HERMES_BASE}{path}", headers=_hermes_headers())


def _openai_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "invalid_request_error"}})


def _openai_messages(value: object) -> tuple[list[dict], str]:
    if not isinstance(value, list) or not value:
        raise ValueError("messages must be a non-empty array")
    messages: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each message must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("only string system, user, and assistant messages are supported")
        if len(content.encode("utf-8")) > MAX_BODY:
            raise ValueError("a message is too large")
        messages.append({"role": role, "content": content})
    final = messages.pop()
    if final["role"] != "user" or not final["content"].strip():
        raise ValueError("the final message must be a non-empty user message")
    return messages, final["content"]


# Hermes 0.18.2 durable keys are ``YYYYMMDD_HHMMSS_abcdef``.  Keep the
# accepted alphabet slightly broader for valid legacy keys while rejecting
# separators/path syntax before an ID reaches JSON-RPC.
SESSION_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TITLE_MAX_CHARS: Final = 180


def _safe_session_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid session id")
    return session_id


def _safe_title(value: object) -> str:
    title = str(value or "").strip()
    if not title or len(title) > TITLE_MAX_CHARS or "\x00" in title:
        raise ValueError("title must be between 1 and 180 characters")
    return title


def _safe_history(messages: object) -> list[dict]:
    """Return only display-safe human/assistant text from Hermes history."""
    return sanitize_history(messages, MAX_BODY)


def _attachment_prompt(payload: dict) -> tuple[str, list[Path]]:
    raw = payload.get("attachments", [])
    if raw is None:
        return "", []
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        raise ValueError("attachments must contain at most three files")
    try:
        requests: list[tuple[str, str | None]] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("invalid attachment")
            requests.append((item["id"], item.get("name")))
        paths = _attachment_registry.resolve_and_consume_batch(requests)
    except (AttachmentRejected, ValueError) as exc:
        raise ValueError("attachment is unknown, expired, or already consumed") from exc
    if not paths:
        return "", []
    manifest = ["\n\n--- ATTACHMENTS AVAILABLE ON THIS VM ---"]
    manifest.extend(f"[{index}] {item_name}: {path}" for index, (item_name, path) in enumerate(paths, 1))
    manifest.append("--- END ATTACHMENTS ---")
    return "".join(manifest), [path for _, path in paths]


def _safe_session_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    sanitized: list[dict] = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        try:
            session_id = _safe_session_id(row.get("id"))
        except ValueError:
            continue
        title = str(row.get("title") or "").strip()[:TITLE_MAX_CHARS]
        preview = str(row.get("preview") or "").strip()[:280]
        try:
            started_at = float(row.get("started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0
        try:
            message_count = max(0, int(row.get("message_count") or 0))
        except (TypeError, ValueError):
            message_count = 0
        sanitized.append({"id": session_id, "title": title, "preview": preview, "started_at": started_at, "message_count": message_count})
    return sanitized


async def _rpc_send_wait(upstream, recv_frame, frame: dict, *, error_message: str = "Hermes rejected the session request") -> dict:
    """Send one JSON-RPC request and wait for its matching response frame.

    Unrelated frames are skipped so this is safe mid-stream, and an RPC error
    (or a non-dict result) raises rather than silently continuing.
    """
    await upstream.send(json.dumps(frame))
    while True:
        response = await recv_frame()
        if response.get("id") != frame["id"]:
            continue
        if "error" in response:
            raise RuntimeError(error_message)
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Hermes returned an invalid session response")
        return result


async def _rpc_chat(session_key: str | None, prompt: str, model: str | None = None) -> tuple[str, dict, str]:
    """Execute one local Hermes turn and wait for its terminal event.

    Hermes 0.18.x has no OpenAI REST endpoint.  Its supported dashboard
    transport is JSON-RPC over the loopback-only ``/api/ws`` endpoint.  This
    adapter uses Hermes's stored session key as the source of truth.  Each HTTP
    request temporarily opens/resumes the gateway session, submits only the
    new prompt, and closes the live handle after the terminal event.  It sets
    ``close_on_disconnect`` so an HTTP disconnect/timeout tears down an
    in-flight agent rather than allowing unattended spend; Hermes persists the
    durable session history separately in its state DB.
    """
    token = _dashboard_token()
    timeout = float(os.environ.get("HERMES_CLASSROOM_CHAT_TIMEOUT_SECONDS", "300"))
    request_id = uuid.uuid4().hex
    create_id = f"create-{request_id}"
    prompt_id = f"prompt-{request_id}"
    async with websockets.connect(
        f"ws://127.0.0.1:8642/api/ws?token={token}",
        open_timeout=8,
        close_timeout=5,
        max_size=MAX_BODY,
    ) as upstream:
        deadline = time.monotonic() + timeout

        async def recv_frame() -> dict:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes did not finish before the connector timeout")
            raw = await asyncio.wait_for(upstream.recv(), timeout=remaining)
            if not isinstance(raw, str):
                raise RuntimeError("Hermes returned an unexpected binary frame")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise RuntimeError("Hermes returned an invalid JSON-RPC frame")
            return data

        # Hermes emits gateway.ready before accepting JSON-RPC requests.
        while True:
            frame = await recv_frame()
            if frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "gateway.ready":
                break
        if session_key:
            await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.resume", "params": {"session_id": session_key, "cols": 100, "source": "classroom-portal", "close_on_disconnect": True}}))
        else:
            create_params: dict = {"cols": 100, "source": "classroom-portal", "close_on_disconnect": True}
            if model:
                create_params["model"] = model
            await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.create", "params": create_params}))
        session_id = ""
        stored_session_id = ""
        while not session_id or not stored_session_id:
            frame = await recv_frame()
            if frame.get("id") != create_id:
                continue
            if "error" in frame:
                raise RuntimeError("Hermes could not create a session")
            result = frame.get("result") or {}
            session_id = str(result.get("session_id") or "")
            stored_session_id = str(result.get("session_key") or result.get("stored_session_id") or session_key or "")
            if not session_id or not stored_session_id:
                raise RuntimeError("Hermes returned an invalid session response")
        if session_key and model:
            switch_frame = session_switch_command(session_id, model, f"model-{request_id}")
            if switch_frame is not None:
                switch_result = await _rpc_send_wait(upstream, recv_frame, switch_frame, error_message="Hermes could not switch session model")
                validate_config_set_result(switch_result, requested_model=model)
        await upstream.send(json.dumps({"jsonrpc": "2.0", "id": prompt_id, "method": "prompt.submit", "params": {"session_id": session_id, "text": prompt}}))
        submitted = False
        while True:
            frame = await recv_frame()
            if frame.get("id") == prompt_id:
                if "error" in frame:
                    raise RuntimeError("Hermes rejected the prompt")
                submitted = True
                continue
            if frame.get("method") != "event":
                continue
            params = frame.get("params") or {}
            if params.get("session_id") != session_id:
                continue
            event_type = params.get("type")
            payload = params.get("payload") or {}
            if event_type == "error":
                raise RuntimeError(str(payload.get("message") or "Hermes failed to complete the prompt"))
            if event_type == "message.complete":
                if not submitted:
                    raise RuntimeError("Hermes completed before accepting the prompt")
                text = payload.get("text")
                if not isinstance(text, str):
                    raise RuntimeError("Hermes completed without text")
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                # A completed turn has already been persisted by Hermes. Close
                # the live gateway handle so one web request cannot pin a VM
                # session slot; failures here do not invalidate the response.
                try:
                    await upstream.send(json.dumps({"jsonrpc": "2.0", "id": f"close-{request_id}", "method": "session.close", "params": {"session_id": session_id}}))
                except Exception:
                    pass
                return text, usage, _safe_session_id(stored_session_id)


async def _rpc_request(method: str, params: dict) -> dict:
    """Perform one narrow, supported Hermes JSON-RPC request."""
    token = _dashboard_token()
    request_id = uuid.uuid4().hex
    timeout = float(os.environ.get("HERMES_CLASSROOM_RPC_TIMEOUT_SECONDS", "30"))
    async with websockets.connect(f"ws://127.0.0.1:8642/api/ws?token={token}", open_timeout=8, close_timeout=5, max_size=MAX_BODY) as upstream:
        deadline = time.monotonic() + timeout
        async def recv_frame() -> dict:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes did not respond before the connector timeout")
            raw = await asyncio.wait_for(upstream.recv(), timeout=remaining)
            if not isinstance(raw, str):
                raise RuntimeError("Hermes returned an unexpected binary frame")
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                raise RuntimeError("Hermes returned an invalid JSON-RPC frame")
            return frame
        while True:
            frame = await recv_frame()
            if frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "gateway.ready":
                break
        await upstream.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        while True:
            frame = await recv_frame()
            if frame.get("id") != request_id:
                continue
            if "error" in frame:
                raise RuntimeError("Hermes rejected the session request")
            result = frame.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Hermes returned an invalid session response")
            return result


async def _with_resumed_session(session_key: str, action: str, params: dict) -> dict:
    """Resume persisted session, make one operation, then close its live handle."""
    token = _dashboard_token()
    timeout = float(os.environ.get("HERMES_CLASSROOM_RPC_TIMEOUT_SECONDS", "30"))
    async with websockets.connect(f"ws://127.0.0.1:8642/api/ws?token={token}", open_timeout=8, close_timeout=5, max_size=MAX_BODY) as upstream:
        deadline = time.monotonic() + timeout
        async def call(method: str, call_params: dict) -> dict:
            request_id = uuid.uuid4().hex
            await upstream.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": call_params}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Hermes did not respond before the connector timeout")
                raw = await asyncio.wait_for(upstream.recv(), timeout=remaining)
                if not isinstance(raw, str):
                    raise RuntimeError("Hermes returned an unexpected binary frame")
                frame = json.loads(raw)
                if not isinstance(frame, dict) or frame.get("id") != request_id:
                    continue
                if "error" in frame:
                    raise RuntimeError("Hermes rejected the session request")
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Hermes returned an invalid session response")
                return result
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes did not respond before the connector timeout")
            raw = await asyncio.wait_for(upstream.recv(), timeout=remaining)
            if isinstance(raw, str):
                frame = json.loads(raw)
                if isinstance(frame, dict) and frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "gateway.ready":
                    break
        # Even read-only session operations get a short-lived live gateway
        # handle. Tear it down immediately on disconnect rather than relying on
        # the grace reaper to release the VM's active-session slot.
        resumed = await call("session.resume", {"session_id": session_key, "cols": 100, "source": "classroom-portal", "close_on_disconnect": True})
        live_id = str(resumed.get("session_id") or "")
        if not live_id:
            raise RuntimeError("Hermes returned an invalid session response")
        try:
            return await call(action, {**params, "session_id": live_id})
        finally:
            try:
                await call("session.close", {"session_id": live_id})
            except Exception:
                pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    purge_task = asyncio.create_task(attachment_purge_loop(_attachment_registry))
    try:
        yield
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    # Never reveal authentication implementation details to the public host.
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.api_route(f"{PUBLIC_PREFIX}/health", methods=["GET"])
async def health(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        response = await _local_get("/api/status")
    except (httpx.HTTPError, RuntimeError):
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if response.status_code != 200:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.api_route(f"{PUBLIC_PREFIX}/v1/capabilities", methods=["GET"])
async def capabilities(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    return {
        "object": "list",
        "data": [{
            "id": "hermes-agent",
            "object": "model",
            "owned_by": "local-hermes",
            "capabilities": {"chat_completions": True, "streaming": True, "max_concurrent_turns": MAX_CONCURRENT_TURNS},
        }],
    }


@app.post(f"{PUBLIC_PREFIX}/v1/files")
async def upload_files(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body, MAX_FILE_REQUEST_BODY)
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        return JSONResponse(status_code=415, content={"error": "multipart/form-data is required"})
    try:
        form = await request.form()
        uploads = select_uploads(value for _, value in form.multi_items())
        if not uploads or len(uploads) > MAX_FILES:
            raise AttachmentRejected("up to three non-empty files are required")
        contents: list[tuple[str, bytes]] = []
        total = 0
        for upload in uploads:
            data = await upload.read(MAX_FILE_BYTES + 1)
            if not data or len(data) > MAX_FILE_BYTES:
                raise AttachmentRejected("attachment limits exceeded")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise AttachmentRejected("attachment limits exceeded")
            contents.append((upload.filename or "", data))
        results = _attachment_registry.store_batch(contents)
        return {"attachments": results}
    except AttachmentRejected as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except (RuntimeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "Invalid multipart upload"})
    finally:
        for upload in locals().get("uploads", []):
            await upload.close()


@app.api_route(f"{PUBLIC_PREFIX}/v1/models", methods=["GET"])
async def list_models(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    return {"object": "list", "data": await _routellm_models()}


@app.api_route(f"{PUBLIC_PREFIX}/v1/usage/credits", methods=["GET"])
async def usage_credits(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        return await _abacus_credits()
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Abacus credit reporting is unavailable"})


@app.api_route(f"{PUBLIC_PREFIX}/v1/abacus/snapshot", methods=["GET"])
async def abacus_snapshot(request: Request):
    """Portal-only per-user usage snapshot; never relayed to student browsers."""
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        return await _abacus_snapshot()
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Abacus usage snapshot is unavailable"})


@app.get(f"{PUBLIC_PREFIX}/v1/sessions")
async def list_sessions(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        result = await _rpc_request("session.list", {"limit": 100})
        return {"object": "list", "data": _safe_session_rows(result.get("sessions"))}
    except TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Hermes timed out"})
    except (OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Hermes session list is unavailable"})


@app.get(f"{PUBLIC_PREFIX}/v1/sessions/{{session_id}}")
async def get_session(session_id: str, request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        stored_id = _safe_session_id(session_id)
        result = await _with_resumed_session(stored_id, "session.history", {})
        return {"id": stored_id, "messages": _safe_history(result.get("messages"))}
    except ValueError as exc:
        return _openai_error(str(exc), 400)
    except TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Hermes timed out"})
    except (OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
        return JSONResponse(status_code=404, content={"error": "Hermes session is unavailable"})


@app.patch(f"{PUBLIC_PREFIX}/v1/sessions/{{session_id}}")
async def rename_session(session_id: str, request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        stored_id = _safe_session_id(session_id)
        title = _safe_title(data.get("title"))
        result = await _with_resumed_session(stored_id, "session.title", {"title": title})
        return {"id": stored_id, "title": str(result.get("title") or title)[:TITLE_MAX_CHARS]}
    except ValueError as exc:
        return _openai_error(str(exc), 400)
    except TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Hermes timed out"})
    except (OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Hermes could not rename this session"})


@app.delete(f"{PUBLIC_PREFIX}/v1/sessions/{{session_id}}")
async def delete_session(session_id: str, request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        stored_id = _safe_session_id(session_id)
        result = await _rpc_request("session.delete", {"session_id": stored_id})
        return {"id": stored_id, "deleted": result.get("deleted") == stored_id}
    except ValueError as exc:
        return _openai_error(str(exc), 400)
    except TimeoutError:
        return JSONResponse(status_code=504, content={"error": "Hermes timed out"})
    except (OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Hermes could not delete this session"})


async def _chat_completions_streaming(payload: dict, request: Request) -> StreamingResponse:
    """Stream chat completions via SSE, bridging Hermes message.delta / message.complete events."""
    history, prompt = _openai_messages(payload.get("messages"))
    if history:
        raise ValueError("persistent Hermes chats accept only the new user message")
    requested_session = payload.get("session_id")
    session_key = _safe_session_id(requested_session) if requested_session is not None else None
    model_raw = str(payload.get("model") or "").strip()
    model = normalize_model_for_session(session_key, model_raw if model_raw else None)
    idempotency_key = str(payload.get("idempotency_key") or "").strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise ValueError("a valid idempotency key is required")
    attachment_prompt, attachment_paths = _attachment_prompt(payload)
    prompt += attachment_prompt
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    async def event_generator():
        request_id = uuid.uuid4().hex
        create_id = f"create-{request_id}"
        prompt_id = f"prompt-{request_id}"
        try:
            async with _inflight_stream_lock:
                if idempotency_key in _inflight_stream_keys:
                    raise ValueError("This request is already in progress")
                if len(_inflight_stream_keys) >= PENDING_KEY_LIMIT:
                    raise ValueError("Too many pending streaming requests")
                _inflight_stream_keys.add(idempotency_key)

            token = _dashboard_token()
            timeout = float(os.environ.get("HERMES_CLASSROOM_CHAT_TIMEOUT_SECONDS", "300"))
            deadline = time.monotonic() + min(timeout, HARD_MAX_STREAM_LIFETIME_SECONDS)

            try:
                await asyncio.wait_for(_turn_semaphore.acquire(), timeout=SEMAPHORE_ACQUIRE_TIMEOUT)
            except asyncio.TimeoutError:
                raise TimeoutError("Could not acquire turn semaphore")
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:8642/api/ws?token={token}",
                    open_timeout=8,
                    close_timeout=5,
                    max_size=MAX_BODY,
                ) as upstream:
                    # Bounded deferred inbox for event frames that arrive
                    # before their corresponding JSON-RPC ack completes.
                    # Prevents message.delta / message.complete frames from
                    # being silently dropped during clarify.respond ack-wait.
                    _deferred_q: DeferredQueue = DeferredQueue(maxsize=128)

                    async def recv_frame():
                        while True:
                            # Consume any deferred frames before reading
                            # from the upstream WebSocket.  This ensures
                            # event frames queued during ack-wait are
                            # delivered to the normal event loop in order.
                            frame = _deferred_q.get_nowait()
                            if frame is not None:
                                return frame

                            if await request.is_disconnected():
                                try:
                                    await upstream.close()
                                except Exception:
                                    pass
                                raise asyncio.CancelledError()
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Hermes did not finish before the connector timeout")
                            try:
                                raw = await asyncio.wait_for(upstream.recv(), timeout=min(remaining, 1.0))
                                break
                            except asyncio.TimeoutError:
                                continue
                        if not isinstance(raw, str):
                            raise RuntimeError("Hermes returned an unexpected binary frame")
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise RuntimeError("Hermes returned an invalid JSON-RPC frame")
                        return data

                    while True:
                        frame = await recv_frame()
                        if frame.get("method") == "event" and (frame.get("params") or {}).get("type") == "gateway.ready":
                            break

                    if session_key:
                        await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.resume", "params": {"session_id": session_key, "cols": 100, "source": "classroom-portal", "close_on_disconnect": True}}))
                    else:
                        create_params: dict = {"cols": 100, "source": "classroom-portal", "close_on_disconnect": True}
                        if model:
                            create_params["model"] = model
                        await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.create", "params": create_params}))

                    session_id_val = ""
                    stored_session_id = ""
                    while not session_id_val or not stored_session_id:
                        frame = await recv_frame()
                        if frame.get("id") != create_id:
                            continue
                        if "error" in frame:
                            raise RuntimeError("Hermes could not create a session")
                        result = frame.get("result") or {}
                        session_id_val = str(result.get("session_id") or "")
                        stored_session_id = str(result.get("session_key") or result.get("stored_session_id") or session_key or "")
                        if not session_id_val or not stored_session_id:
                            raise RuntimeError("Hermes returned an invalid session response")

                    if session_key and model:
                        switch_frame = session_switch_command(session_id_val, model, f"model-{request_id}")
                        if switch_frame is not None:
                            switch_result = await _rpc_send_wait(upstream, recv_frame, switch_frame, error_message="Hermes could not switch session model")
                            validate_config_set_result(switch_result, requested_model=model)

                    await upstream.send(json.dumps({"jsonrpc": "2.0", "id": prompt_id, "method": "prompt.submit", "params": {"session_id": session_id_val, "text": prompt}}))
                    submitted = False
                    utf8_delta_total = 0
                    saw_text_delta = False

                    while True:
                        frame = await recv_frame()
                        if frame.get("id") == prompt_id:
                            if "error" in frame:
                                raise RuntimeError("Hermes rejected the prompt")
                            submitted = True
                            continue
                        if frame.get("method") != "event":
                            continue
                        params = frame.get("params") or {}
                        if params.get("session_id") != session_id_val:
                            continue
                        event_type = params.get("type")
                        event_payload = params.get("payload") or {}
                        if event_type == "error":
                            raise RuntimeError(str(event_payload.get("message") or "Hermes failed to complete the prompt"))
                        if event_type == "clarify.request":
                            if not submitted:
                                raise RuntimeError("Hermes requested clarification before accepting the prompt")
                            gateway_request_id = str(event_payload.get("request_id", ""))
                            if not gateway_request_id:
                                raise RuntimeError("Hermes clarify.request missing request_id")
                            question_raw = str(event_payload.get("question", ""))
                            raw_choices = event_payload.get("choices")
                            if not isinstance(raw_choices, list) or len(raw_choices) < 2:
                                raise RuntimeError("Hermes clarify.request missing valid choices")
                            multi_select = bool(event_payload.get("multi_select", False))
                            try:
                                capped_q = validate_question(question_raw)
                                capped_choices = validate_choices(raw_choices)
                            except ValueError as exc:
                                raise RuntimeError(f"Invalid clarify.request payload: {exc}") from exc
                            ctoken = await _clarify_state.create_pending(
                                gateway_request_id, capped_q, capped_choices, multi_select
                            )
                            yield encode_clarify_chunk(
                                completion_id=completion_id,
                                model=model,
                                token=ctoken,
                                question=capped_q,
                                choices=capped_choices,
                                multi_select=multi_select,
                                session_id=stored_session_id,
                            )
                            try:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    raise TimeoutError("Clarify response was not received in time")
                                answer = await _clarify_state.await_answer(
                                    ctoken, timeout=min(remaining, CLARIFY_TTL_SECONDS)
                                )
                            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                                await _clarify_state.cleanup_token(ctoken)
                                raise
                            clarify_id = f"clarify-{request_id}-{uuid.uuid4().hex}"
                            await upstream.send(json.dumps({
                                "jsonrpc": "2.0",
                                "id": clarify_id,
                                "method": "clarify.respond",
                                "params": {"request_id": gateway_request_id, "answer": answer},
                            }))
                            # Wait for matching JSON-RPC ack before continuing.
                            # Unrelated frames (non-matching id) are safely ignored
                            # since the agent is blocked waiting for the clarify
                            # response — no meaningful events arrive until ack'd.
                            # ACK must carry a ``result`` dict with
                            # ``status: "ok"`` matching the gateway's
                            # ``_respond`` contract.  A missing ``result`` or
                            # ``result: {status: 'expired'}`` (which the gateway
                            # returns when the clarify timed out server-side)
                            # both fail closed.
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
                                        upstream.recv(), timeout=min(remaining, 1.0)
                                    )
                                except asyncio.TimeoutError:
                                    continue
                                if not isinstance(ack_raw, str):
                                    continue
                                ack_data = json.loads(ack_raw)
                                if not isinstance(ack_data, dict):
                                    continue
                                if ack_data.get("id") != clarify_id:
                                    # Queue nonmatching frames instead of
                                    # dropping them.  Event frames that
                                    # arrive before their ack are preserved
                                    # in order for the normal event loop.
                                    try:
                                        _deferred_q.put(ack_data)
                                    except RuntimeError:
                                        raise  # fail-closed on overflow
                                    continue
                                # Require a valid positive JSON-RPC result.
                                result = ack_data.get("result")
                                if not isinstance(result, dict) or result.get("status") != "ok":
                                    raise RuntimeError("Hermes rejected the clarify response")
                                break
                            continue
                        if event_type == "message.delta":
                            delta_text = event_payload.get("text", "")
                            if isinstance(delta_text, str) and delta_text:
                                utf8_delta_total += len(delta_text.encode("utf-8"))
                                if utf8_delta_total > MAX_UTF8_DELTA_BYTES:
                                    raise RuntimeError("Response exceeded maximum delta size")
                                saw_text_delta = True
                                yield encode_delta_chunk(
                                    completion_id=completion_id,
                                    model=model,
                                    text_delta=delta_text,
                                    session_id=stored_session_id,
                                )
                        if event_type == "message.complete":
                            if not submitted:
                                raise RuntimeError("Hermes completed before accepting the prompt")
                            usage = event_payload.get("usage") if isinstance(event_payload.get("usage"), dict) else {}
                            fallback_text = completion_text_fallback(event_payload, saw_text_delta=saw_text_delta)
                            if fallback_text:
                                fallback_text = validate_completion_fallback_text(fallback_text, MAX_UTF8_DELTA_BYTES, saw_text_delta=saw_text_delta)
                            try:
                                await upstream.send(json.dumps({"jsonrpc": "2.0", "id": f"close-{request_id}", "method": "session.close", "params": {"session_id": session_id_val}}))
                            except Exception:
                                pass
                            if fallback_text:
                                yield encode_delta_chunk(
                                    completion_id=completion_id,
                                    model=model,
                                    text_delta=fallback_text,
                                    session_id=stored_session_id,
                                )
                            terminal = encode_terminal_chunk(
                                completion_id=completion_id,
                                model=model,
                                usage=usage,
                                session_id=stored_session_id,
                            )
                            if len(terminal.encode("utf-8")) > MAX_TERMINAL_ENCODED_BYTES:
                                raise RuntimeError("Terminal event exceeds maximum size")
                            yield terminal
                            return
            finally:
                _turn_semaphore.release()

        except asyncio.CancelledError:
            raise
        except TimeoutError:
            yield encode_error_chunk(
                completion_id=completion_id,
                model=model,
                error_message=GENERIC_TIMEOUT_MESSAGE,
                session_id=None,
            )
        except (ValueError, OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
            yield encode_error_chunk(
                completion_id=completion_id,
                model=model,
                error_message=GENERIC_UNAVAILABLE_MESSAGE,
                session_id=None,
            )
        finally:
            async with _inflight_stream_lock:
                _inflight_stream_keys.discard(idempotency_key)
            for path in attachment_paths:
                _attachment_registry.cleanup(path)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(f"{PUBLIC_PREFIX}/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    try:
        await _authenticate(request.method, request.url.path, request.headers, body)
    except HTTPException:
        raise
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        if payload.get("stream"):
            return await _chat_completions_streaming(payload, request)
        history, prompt = _openai_messages(payload.get("messages"))
        # Persistent classroom chats submit only the final user message.  The
        # legacy messages array is accepted only when it contains that one
        # message, preventing an untrusted browser from rewriting Hermes's
        # authoritative stored transcript.
        if history:
            raise ValueError("persistent Hermes chats accept only the new user message")
        requested_session = payload.get("session_id")
        session_key = _safe_session_id(requested_session) if requested_session is not None else None
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise ValueError("a valid idempotency key is required")
        model_raw = str(payload.get("model") or "").strip()
        model = normalize_model_for_session(session_key, model_raw if model_raw else None)
        attachment_prompt, attachment_paths = _attachment_prompt(payload)
        prompt += attachment_prompt
        fingerprint = hashlib.sha256(f"{session_key or ''}\0{prompt}".encode("utf-8")).hexdigest()
        async def run_turn():
            async with _turn_semaphore:
                return await _rpc_chat(session_key, prompt, model=model)
        # Serialize by default: retrying browser/portal requests must not start
        # multiple expensive agent turns on a student's single VM.
        try:
            answer, usage, stored_session_id = await _idempotency.run(idempotency_key, fingerprint, run_turn)
        finally:
            for path in attachment_paths:
                _attachment_registry.cleanup(path)
    except ValueError as exc:
        return _openai_error(str(exc))
    except TimeoutError:
        return _openai_error("Hermes timed out", 504)
    except (OSError, RuntimeError, websockets.WebSocketException, json.JSONDecodeError):
        return _openai_error("Hermes is unavailable", 503)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    normalized_usage = normalize_usage(usage)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "session_id": stored_session_id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": normalized_usage,
    }


@app.post(f"{PUBLIC_PREFIX}/v1/clarify")
async def clarify_answer(request: Request):
    """Resolve a pending clarification with the student's answer.

    Body: ``{"token": "...", "answer": <str|list[str]>}``
    - The *token* is an opaque 64-hex-char one-time portal token from a
      ``clarify`` SSE event.
    - For single-select clarification, *answer* must be a string matching an
      offered choice exactly.
    - For multi-select clarification, *answer* must be a non-empty list of
      unique offered choices.

    This endpoint ONLY resolves the pending future — it does not create, close,
    or modify the Hermes session or execute arbitrary RPC.  Unknown, expired,
    or replayed tokens return 404 and do not affect other pending turns.
    """
    body = await request.body()
    try:
        await _authenticate(request.method, request.url.path, request.headers, body)
    except HTTPException:
        raise
    # Strict size limit — clarify answers are tiny
    if len(body) > 4096:
        raise HTTPException(status_code=400, detail="Body too large")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        token = str(data.get("token", ""))
        if not CLARIFY_TOKEN_RE.fullmatch(token):
            raise ValueError("invalid token format")
        answer = data.get("answer")
        if answer is None:
            raise ValueError("answer is required")
    except (json.JSONDecodeError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid clarify request")
    success = await _clarify_state.resolve_pending(token, answer)
    if not success:
        raise HTTPException(status_code=404, detail="Clarify token not found or expired")
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("HERMES_CLASSROOM_PORT", "8765")), log_level="warning")
