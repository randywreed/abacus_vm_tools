"""Private, short-lived attachment storage for the classroom connector."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import stat
import time
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILES = 3
TTL_SECONDS = 30 * 60
MAX_ENTRIES = 256
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AttachmentRejected(ValueError):
    pass


class AttachmentRegistry:
    def __init__(self, root: Path, clock=time.time):
        self.root = root
        self.clock = clock
        self.entries: dict[str, tuple[str, Path, int, float]] = {}
        if self.root.is_symlink():
            raise AttachmentRejected("attachment root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._remove_startup_orphans()

    def _remove_startup_orphans(self) -> None:
        """Remove only direct, regular opaque files left by an earlier process."""
        try:
            children = list(self.root.iterdir())
        except OSError:
            return
        for path in children:
            if not OPAQUE_ID_RE.fullmatch(path.name):
                continue
            try:
                if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    continue
                path.unlink()
            except OSError:
                # Cleanup is best effort and must not reveal filesystem details.
                continue

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not value or value in {".", ".."} or "\x00" in value or any(part == ".." for part in re.split(r"[/\\]", value)):
            raise AttachmentRejected("invalid attachment name")
        safe_name = os.path.basename(value.replace("\\", "/"))
        if not NAME_RE.fullmatch(safe_name):
            raise AttachmentRejected("invalid attachment name")
        return safe_name

    def purge_expired(self) -> None:
        cutoff = self.clock() - TTL_SECONDS
        for opaque_id, (_, path, _, created) in list(self.entries.items()):
            if created <= cutoff:
                self.entries.pop(opaque_id, None)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def validate_batch(self, contents: list[bytes]) -> None:
        if len(contents) > MAX_FILES or any(not content or len(content) > MAX_FILE_BYTES for content in contents):
            raise AttachmentRejected("attachment limits exceeded")
        if sum(map(len, contents)) > MAX_TOTAL_BYTES:
            raise AttachmentRejected("attachment limits exceeded")

    def store(self, name: str, content: bytes) -> dict[str, int | str]:
        self.purge_expired()
        if len(self.entries) >= MAX_ENTRIES:
            raise AttachmentRejected("attachment registry is full")
        safe_name = self._name(name)
        self.validate_batch([content])
        opaque_id = secrets.token_urlsafe(32)
        path = self.root / opaque_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.chmod(path, 0o600)
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        self.entries[opaque_id] = (safe_name, path, len(content), self.clock())
        return {"id": opaque_id, "name": safe_name, "size": len(content)}

    def store_batch(self, items: list[tuple[str, bytes]]) -> list[dict[str, int | str]]:
        self.purge_expired()
        self.validate_batch([content for _, content in items])
        results: list[dict[str, int | str]] = []
        try:
            for name, content in items:
                results.append(self.store(name, content))
            return results
        except Exception:
            for result in results:
                opaque_id = str(result["id"])
                entry = self.entries.pop(opaque_id, None)
                if entry:
                    try:
                        entry[1].unlink()
                    except FileNotFoundError:
                        pass
            raise

    def resolve_and_consume(self, opaque_id: str, requested_name: str | None = None) -> Path:
        resolved = self.resolve_and_consume_batch([(opaque_id, requested_name)])
        return resolved[0][1]

    def resolve_and_consume_batch(
        self, items: list[tuple[str, str | None]]
    ) -> list[tuple[str, Path]]:
        """Validate every requested item, then consume the complete batch."""
        self.purge_expired()
        validated: list[tuple[str, str, Path]] = []
        seen: set[str] = set()
        for opaque_id, requested_name in items:
            if opaque_id in seen:
                raise AttachmentRejected("attachment is already requested")
            seen.add(opaque_id)
            entry = self.entries.get(opaque_id)
            if entry is None:
                raise AttachmentRejected("attachment is unknown, expired, or already consumed")
            name, path, _, _ = entry
            if requested_name is not None and requested_name != name:
                raise AttachmentRejected("attachment name does not match")
            if not path.is_file() or path.is_symlink():
                self.entries.pop(opaque_id, None)
                self.cleanup(path)
                raise AttachmentRejected("attachment is unavailable")
            validated.append((opaque_id, name, path))
        for opaque_id, _, _ in validated:
            self.entries.pop(opaque_id, None)
        return [(name, path) for _, name, path in validated]

    def cleanup(self, path: Path) -> None:
        """Delete a consumed file after its caller no longer needs the path."""
        try:
            path.relative_to(self.root)
        except ValueError:
            raise AttachmentRejected("attachment path is outside the registry") from None
        try:
            path.unlink()
        except FileNotFoundError:
            pass


async def attachment_purge_loop(registry: AttachmentRegistry, interval: float = 60.0) -> None:
    """Purge expired attachments forever, surviving individual purge failures."""
    while True:
        try:
            registry.purge_expired()
        except Exception:
            pass
        await asyncio.sleep(interval)
