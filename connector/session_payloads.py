"""Sanitizers for the small, browser-safe Hermes session payload surface."""


def sanitize_history(messages: object, max_body_bytes: int) -> list[dict]:
    """Expose only ordinary user/assistant transcript text.

    Hermes 0.18.2's ``session.history`` gateway response uses ``text`` rather
    than OpenAI's ``content`` key.  Accept ``content`` as a compatibility
    fallback, but never return reasoning, system, tool, or provider fields.
    """
    if not isinstance(messages, list):
        return []
    result: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = message.get("text")
        if not isinstance(text, str):
            text = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(text, str):
            continue
        if len(text.encode("utf-8")) > max_body_bytes:
            continue
        result.append({"role": role, "content": text})
    return result
