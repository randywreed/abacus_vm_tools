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


def patch_text(text: str) -> str:
    block_start, close = selected_block(text)
    if MARKER in text[block_start:close]:
        return text
    return f"{text[:close]}\n\n    {FRAGMENT.replace(chr(10), chr(10) + '    ')}\n{text[close:]}"


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
