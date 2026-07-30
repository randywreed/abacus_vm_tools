"""In-memory pending clarification token store for the Hermes Classroom connector.

This module exists as a focused, testable helper so token lifecycle, replay
protection, answer validation, and cleanup can be verified without spawning the
full FastAPI / WebSocket connector stack.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

CLARIFY_TOKEN_BYTES: int = 32  # 64 hex chars output by secrets.token_hex(32)
CLARIFY_TTL_SECONDS: float = 300.0  # 5 minutes
CLARIFY_MAX_QUESTION_CHARS: int = 2000
CLARIFY_MAX_CHOICES: int = 20
CLARIFY_MAX_CHOICE_CHARS: int = 500
CLARIFY_MAX_ANSWER_CHOICES_MULTI: int = 10
CLARIFY_TOKEN_RE: re.Pattern = re.compile(r"^[0-9a-f]{64}$")
CLARIFY_PENDING_LIMIT: int = 64
# Maximum wait for a clarify.respond JSON-RPC ack from the Hermes gateway
CLARIFY_RESPOND_ACK_TIMEOUT: float = 300.0


@dataclass
class _Entry:
    """Internal entry for a pending clarification."""

    token: str
    gateway_request_id: str
    choices: list[str]
    multi_select: bool
    expires_at: float
    future: asyncio.Future[Any] = field(
        repr=False, default_factory=lambda: asyncio.get_running_loop().create_future()
    )


def validate_question(question: object) -> str:
    """Validate and cap question length.

    Returns the capped, stripped question string.
    Raises ``ValueError`` if the question is not a non-empty string.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    return question.strip()[:CLARIFY_MAX_QUESTION_CHARS]


def validate_choices(choices: object) -> list[str]:
    """Validate and cap choices list.

    Returns a list of capped, unique choice strings.
    Raises ``ValueError`` if the value is not a list of 2-``CLARIFY_MAX_CHOICES``
    non-empty strings.
    """
    if not isinstance(choices, list) or len(choices) < 2 or len(choices) > CLARIFY_MAX_CHOICES:
        raise ValueError(
            f"choices must be a list of 2-{CLARIFY_MAX_CHOICES} strings"
        )
    result: list[str] = []
    for c in choices:
        if not isinstance(c, str) or not c.strip():
            raise ValueError("each choice must be a non-empty string")
        result.append(c.strip()[:CLARIFY_MAX_CHOICE_CHARS])
    if len(set(result)) != len(result):
        raise ValueError("choices must be unique")
    return result


def validate_answer(choices: list[str], multi_select: bool, answer: object) -> str | list[str]:
    """Validate an answer against the given choices and multi_select flag.

    Single-select: *answer* must be exactly one of *choices* (string, case-sensitive).
    Multi-select: *answer* must be a non-empty list of unique, bounded items from
    *choices*.

    Returns the validated answer in the format the gateway expects
    (``str`` for single-select, ``list[str]`` for multi-select).

    Raises ``ValueError`` on invalid input.
    """
    if multi_select:
        if not isinstance(answer, list):
            raise ValueError("answer must be a list for multi-select")
        if not answer:
            raise ValueError("answer list must not be empty")
        if len(answer) > CLARIFY_MAX_ANSWER_CHOICES_MULTI:
            raise ValueError(
                f"answer must contain at most {CLARIFY_MAX_ANSWER_CHOICES_MULTI} choices"
            )
        if len(set(answer)) != len(answer):
            raise ValueError("answer must not contain duplicate choices")
        sanitized: list[str] = []
        for a in answer:
            if not isinstance(a, str) or not a.strip():
                raise ValueError("each answer choice must be a non-empty string")
            stripped = a.strip()
            if stripped not in choices:
                raise ValueError(f"answer choice '{stripped}' is not an offered choice")
            sanitized.append(stripped)
        return sanitized
    else:
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("answer must be a non-empty string")
        stripped = answer.strip()
        if stripped not in choices:
            raise ValueError(f"answer '{stripped}' is not an offered choice")
        return stripped


class ClarifyState:
    """In-memory store for pending clarification tokens.

    Thread-safe via ``asyncio.Lock``.  Expired entries are lazily evicted on
    every mutation.  The store is bounded by *limit*.
    """

    def __init__(self, ttl: float = CLARIFY_TTL_SECONDS, limit: int = CLARIFY_PENDING_LIMIT) -> None:
        self.ttl = ttl
        self.limit = limit
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create_pending(
        self,
        gateway_request_id: str,
        question: str,
        choices: list[str],
        multi_select: bool = False,
    ) -> str:
        """Create a pending clarification and return its opaque token.

        *question* and *choices* MUST already be validated (by
        ``validate_question`` and ``validate_choices`` respectively) before
        calling this method.
        """
        async with self._lock:
            self._evict_expired()
            token = secrets.token_hex(CLARIFY_TOKEN_BYTES)
            entry = _Entry(
                token=token,
                gateway_request_id=gateway_request_id,
                choices=choices,
                multi_select=multi_select,
                expires_at=time.monotonic() + self.ttl,
            )
            self._entries[token] = entry
            while len(self._entries) > self.limit:
                stale = self._entries.popitem(last=False)
                if not stale[1].future.done():
                    stale[1].future.cancel()
            return token

    async def resolve_pending(self, token: str, answer: object) -> bool:
        """Resolve a pending clarification with a validated *answer*.

        Returns ``True`` if the token was found, unexpired, and the answer was
        accepted (the future is resolved).  Returns ``False`` (without raising)
        for unknown, expired, or already-resolved tokens so the HTTP endpoint
        can return 4xx without risk of dangling futures.

        IMPORTANT: validation happens BEFORE consumption.  An invalid answer
        returns ``False`` without touching the pending entry, so the caller can
        retry with a valid answer using the same token.

        A valid answer makes the token replay-proof immediately (the future is
        resolved and any subsequent call returns ``False``), BUT the entry
        remains in the store so that an already-running streaming coroutine
        (``await_answer``) can still retrieve the fulfilled answer.  The entry
        is removed by ``await_answer`` after it consumes the result, or by
        expiry eviction.
        """
        async with self._lock:
            self._evict_expired()
            entry = self._entries.get(token, None)
            if entry is None:
                return False
            if time.monotonic() > entry.expires_at:
                return False
            # Reject replay if the future is already resolved.
            if entry.future.done():
                return False
            # Validate answer inside the lock — synchronous, no await points.
            try:
                validated = validate_answer(entry.choices, entry.multi_select, answer)
            except ValueError:
                return False
            # Set result on the future.  The entry stays in _entries so the
            # streaming coroutine's await_answer can still find it.
            entry.future.set_result(validated)
            return True

    async def await_answer(self, token: str, timeout: float | None = None) -> str | list[str]:
        """Wait for an answer on a pending token.

        Raises ``KeyError`` if the token is unknown (expired or cleaned up).
        Raises ``asyncio.TimeoutError`` if the timeout elapses before the
        answer arrives.

        Works correctly even when ``resolve_pending`` already resolved the
        answer before this call is made (the pre-valid-answer-race fix): the
        entry is captured under the lock, and if its future is already
        fulfilled, ``asyncio.wait_for`` returns the result immediately.
        """
        async with self._lock:
            self._evict_expired()
            entry = self._entries.get(token)
            if entry is None:
                raise KeyError("clarify token not found")
            captured_future = entry.future

        try:
            return await asyncio.wait_for(
                asyncio.shield(captured_future), timeout=timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self.cleanup_token(token)
            raise
        finally:
            # Clean up the consumed entry on all paths.  Idempotent: if
            # cleanup_token was already called in the except handler or by the
            # caller, the entry is already gone and the second call is a no-op.
            await self.cleanup_token(token)

    async def cleanup_token(self, token: str) -> None:
        """Remove a token entry and cancel its future (if pending)."""
        async with self._lock:
            entry = self._entries.pop(token, None)
            if entry is not None and not entry.future.done():
                entry.future.cancel()

    async def cleanup_all(self) -> int:
        """Remove all pending entries and cancel their futures.

        Returns the count of entries removed.
        """
        async with self._lock:
            count = len(self._entries)
            for entry in self._entries.values():
                if not entry.future.done():
                    entry.future.cancel()
            self._entries.clear()
            return count

    async def pending_count(self) -> int:
        """Return the number of pending (non-expired) tokens."""
        async with self._lock:
            self._evict_expired()
            return len(self._entries)

    @property
    def entries(self) -> OrderedDict[str, _Entry]:
        """Expose entries for test introspection.  Not for production use."""
        return self._entries

    def _evict_expired(self) -> None:
        now = time.monotonic()
        while self._entries and next(iter(self._entries.values())).expires_at < now:
            stale = self._entries.popitem(last=False)
            if not stale[1].future.done():
                stale[1].future.cancel()


class DeferredQueue:
    """Bounded FIFO inbox for deferred JSON-RPC frames during ack-wait.

    Used to preserve event frames that arrive during a clarify.respond
    ack wait, so they can be consumed by the normal event loop after
    the ack completes rather than being silently dropped.
    """

    def __init__(self, maxsize: int = 64) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)

    def put(self, frame: dict) -> None:
        """Add a frame to the deferred queue.

        Raises ``RuntimeError`` if the queue is at capacity (fail-closed
        on overflow).
        """
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            raise RuntimeError(
                "Deferred frame queue overflow"
            ) from None

    def get_nowait(self) -> dict | None:
        """Dequeue a frame, or return ``None`` if empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
