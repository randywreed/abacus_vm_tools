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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final
from datetime import datetime

import httpx
import uvicorn
import websockets
from abacus_usage import abacus_api_key_from_config, balance_credit_report, daily_credit_report
from idempotency import TurnIdempotency
from session_payloads import sanitize_history
from telemetry import normalize_usage
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


PUBLIC_PREFIX: Final = "/hermes-classroom"
HERMES_BASE: Final = os.environ.get("HERMES_LOCAL_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_ENV: Final = Path(os.environ.get("HERMES_ENV_FILE", "/home/ubuntu/.hermes/hermes-serve.env"))
SHARED_SECRET: Final = os.environ["HERMES_CLASSROOM_SHARED_SECRET"].encode("ascii")
MAX_BODY: Final = 1_048_576
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
# Keyed by an opaque browser-generated ID. Only a digest of session + prompt is
# retained, never the prompt/transcript itself. Entries are short-lived so a
# network retry receives the exact completed answer without buying another turn.
# A single student VM executes one turn at a time.  Keep enough completed
# entries for normal retry windows without retaining a large batch of model
# responses in process memory.
_idempotency = TurnIdempotency(ttl_seconds=15 * 60, limit=64)


def _dashboard_token() -> str:
    """Read the local Hermes dashboard token without logging or returning it."""
    token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "").strip()
    if token:
        return token
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


async def _authenticate(method: str, path: str, headers, body: bytes) -> None:
    timestamp = headers.get("x-hermes-classroom-timestamp", "")
    nonce = headers.get("x-hermes-classroom-nonce", "")
    signature = headers.get("x-hermes-classroom-signature", "")
    if len(body) > MAX_BODY or not NONCE_RE.fullmatch(nonce):
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


async def _local_get(path: str, *, authenticated: bool = True) -> httpx.Response:
    timeout = httpx.Timeout(connect=3.0, read=25.0, write=5.0, pool=3.0)
    headers = _hermes_headers() if authenticated else {}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(f"{HERMES_BASE}{path}", headers=headers)


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


async def _rpc_chat(session_key: str | None, prompt: str) -> tuple[str, dict, str]:
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
        # Deliberately do not forward model/reasoning values. Hermes's
        # configured default is the VM owner's policy; a portal request must
        # not be able to switch it or create an accidental expensive override.
        if session_key:
            await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.resume", "params": {"session_id": session_key, "cols": 100, "source": "classroom-portal", "close_on_disconnect": True}}))
        else:
            await upstream.send(json.dumps({"jsonrpc": "2.0", "id": create_id, "method": "session.create", "params": {"cols": 100, "source": "classroom-portal", "close_on_disconnect": True}}))
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
    yield


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
        response = await _local_get("/api/status", authenticated=False)
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
            "capabilities": {"chat_completions": True, "streaming": False, "max_concurrent_turns": MAX_CONCURRENT_TURNS},
        }],
    }


@app.api_route(f"{PUBLIC_PREFIX}/v1/usage/credits", methods=["GET"])
async def usage_credits(request: Request):
    body = await request.body()
    await _authenticate(request.method, request.url.path, request.headers, body)
    try:
        return await _abacus_credits()
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return JSONResponse(status_code=503, content={"error": "Abacus credit reporting is unavailable"})


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
            return _openai_error("streaming is not supported by this connector")
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
        model = str(payload.get("model") or "").strip() or "hermes-agent"
        fingerprint = hashlib.sha256(f"{session_key or ''}\0{prompt}".encode("utf-8")).hexdigest()
        async def run_turn():
            async with _turn_semaphore:
                return await _rpc_chat(session_key, prompt)
        # Serialize by default: retrying browser/portal requests must not start
        # multiple expensive agent turns on a student's single VM.
        answer, usage, stored_session_id = await _idempotency.run(idempotency_key, fingerprint, run_turn)
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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("HERMES_CLASSROOM_PORT", "8765")), log_level="warning")
