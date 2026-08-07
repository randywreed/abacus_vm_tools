#!/usr/bin/env python3
"""Safely insert the connector fragment into Abacus's selected Nginx server."""
from __future__ import annotations

import re
import sys
from pathlib import Path


FRAGMENT = Path(__file__).with_name("nginx-hermes-classroom.conf").read_text(encoding="utf-8").strip()
MARKER = "location ^~ /hermes-classroom/"
DEFAULT_LISTEN_RE = re.compile(r"(?m)^\s*listen\s+80\s+default_server\s*;")
LOCALHOST_RE = re.compile(r"(?m)^\s*server_name\s+localhost\s*;")
SERVER_OPEN_RE = re.compile(r"(?m)^\s*server\s*\{")
LOCATION_RE = re.compile(r"^\s*location\s+(?:(=|\^~|~\*|~)\s+)?(\S+)\s*$")
CLIENT_MAX_BODY_RE = re.compile(r"client_max_body_size\s+([^;\s]+)\s*;?")
NGINX_SIZE_RE = re.compile(r"^[0-9]+[kKmMgG]?$")


def server_close(text: str, start: int) -> int:
    depth = 0
    opened = False
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return index
    raise ValueError("selected Nginx server block is not balanced")


def server_blocks(text: str) -> list[tuple[int, int]]:
    return [(match.start(), server_close(text, match.start())) for match in SERVER_OPEN_RE.finditer(text)]


def selected_block(text: str) -> tuple[int, int]:
    blocks = server_blocks(text)
    defaults = [block for block in blocks if DEFAULT_LISTEN_RE.search(text, block[0], block[1])]
    if len(defaults) == 1:
        return defaults[0]
    if len(defaults) > 1:
        raise ValueError("expected at most one Nginx listen 80 default_server block")
    localhost = [block for block in blocks if LOCALHOST_RE.search(text, block[0], block[1])]
    if len(localhost) != 1:
        raise ValueError("could not identify exactly one Abacus localhost server block")
    return localhost[0]


def matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    for index in range(open_idx, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("selected Nginx server block is not balanced")


def server_children(text: str, block_start: int, close: int) -> list[tuple[int, int, int]]:
    """Return (line_start, open_idx, close_idx) for each top-level { ... } block
    directly inside the selected server block text[block_start:close]."""
    open_brace = text.find("{", block_start, close)
    if open_brace == -1 or open_brace >= close:
        return []
    blocks: list[tuple[int, int, int]] = []
    i = open_brace + 1
    while i < close:
        if text[i] == "{":
            close_idx = matching_brace(text, i)
            newline = text.rfind("\n", block_start, i)
            line_start = newline + 1 if newline != -1 else block_start
            blocks.append((line_start, i, close_idx))
            i = close_idx + 1
            continue
        i += 1
    return blocks


def location_signature(header: str) -> tuple[str | None, str] | None:
    match = LOCATION_RE.fullmatch(header)
    if match is None:
        return None
    return (match.group(1), match.group(2))


def _describe(signature: tuple[str | None, str]) -> str:
    modifier, argument = signature
    return f"location {modifier + ' ' if modifier else ''}{argument}"


def normalize_body(body: str) -> list[str]:
    return [re.sub(r"\s+", " ", line.strip()) for line in body.splitlines() if line.strip()]


def _client_max_body_parts(lines: list[str]) -> tuple[list[str], str | None] | None:
    others: list[str] = []
    size: str | None = None
    for line in lines:
        match = CLIENT_MAX_BODY_RE.fullmatch(line)
        if match:
            if size is not None:
                return None
            size = match.group(1)
        else:
            others.append(line)
    return others, size


def bodies_compatible(existing: list[str], fragment: list[str]) -> bool:
    if existing == fragment:
        return True
    existing_parts = _client_max_body_parts(existing)
    fragment_parts = _client_max_body_parts(fragment)
    if existing_parts is None or fragment_parts is None:
        return False
    existing_others, existing_size = existing_parts
    fragment_others, fragment_size = fragment_parts
    if existing_size is None or fragment_size is None:
        return False
    if existing_others != fragment_others:
        return False
    return NGINX_SIZE_RE.fullmatch(existing_size) is not None


def fragment_blocks() -> list[tuple[tuple[str | None, str], str, list[str]]]:
    wrapped = "server {\n" + FRAGMENT + "\n}\n"
    blocks: list[tuple[tuple[str | None, str], str, list[str]]] = []
    for line_start, open_idx, close_idx in server_children(wrapped, 0, len(wrapped)):
        signature = location_signature(wrapped[line_start:open_idx])
        if signature is None:
            continue
        raw = wrapped[line_start:close_idx + 1]
        body = normalize_body(wrapped[open_idx + 1:close_idx])
        blocks.append((signature, raw, body))
    return blocks


def render_text(content: str, indent: str) -> str:
    return "\n".join((indent + line if line.strip() else line) for line in content.split("\n"))


def _leading_indent(text: str, line_start: int, open_idx: int) -> str:
    prefix = text[line_start:open_idx]
    return prefix[: len(prefix) - len(prefix.lstrip())]


def _insert_fragment(text: str, close: int) -> str:
    return f"{text[:close]}\n\n{render_text(FRAGMENT, '    ')}\n{text[close:]}"


def patch_text(text: str) -> str:
    block_start, close = selected_block(text)

    expected: dict[tuple[str | None, str], tuple[str, list[str]]] = {}
    for signature, raw, body in fragment_blocks():
        expected[signature] = (raw, body)

    matched: dict[tuple[str | None, str], tuple[int, int, int, list[str]]] = {}
    for line_start, open_idx, close_idx in server_children(text, block_start, close):
        signature = location_signature(text[line_start:open_idx])
        if signature is None or signature not in expected:
            continue
        if signature in matched:
            raise ValueError(f"duplicate {_describe(signature)} in selected Nginx server block")
        matched[signature] = (line_start, open_idx, close_idx, normalize_body(text[open_idx + 1:close_idx]))

    if not matched:
        return _insert_fragment(text, close)

    missing = [signature for signature in expected if signature not in matched]
    if missing:
        raise ValueError("selected server has a partial classroom block; refusing to guess")

    replacements: list[tuple[int, int, str]] = []
    for signature, (raw, expected_body) in expected.items():
        line_start, open_idx, close_idx, existing_body = matched[signature]
        if not bodies_compatible(existing_body, expected_body):
            raise ValueError(f"incompatible classroom block in selected Nginx server block")
        if existing_body != expected_body:
            replacements.append(
                (line_start, close_idx + 1, render_text(raw, _leading_indent(text, line_start, open_idx)))
            )

    if not replacements:
        return text

    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: patch_nginx_default.py INPUT OUTPUT", file=sys.stderr)
        return 2
    source, destination = map(Path, sys.argv[1:])
    patched = patch_text(source.read_text(encoding="utf-8"))
    destination.write_text(patched, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
