"""Pure app-tunnel validation and registry for the Hermes Classroom connector.

Students' Hermes agents can publish loopback web apps at a public path under
``/hermes-classroom/apps/<name>/``. The FastAPI routes live in
``hermes_classroom_connector.py``; this module is framework-free so the
validation and registry logic can be unit-tested without FastAPI.

Security posture:

- App names are strictly lowercase ``[a-z0-9-]`` segments (no dots, slashes,
  or separators) so they are safe URL path segments and cannot traverse
  directories or collide with the connector's own routes.
- Tunnel ports are restricted to the unprivileged high range and must not be
  reserved infrastructure ports.
- The proxy target is always ``127.0.0.1:<port>``; there is no host input, so
  the tunnel cannot be pointed at arbitrary internet hosts (no SSRF surface).
- The registry is in-memory and dies with the VM, which is the intended
  lifecycle for student sandboxes.
"""
from __future__ import annotations

import re
from typing import Final

APP_NAME_RE: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Infrastructure loopback ports that must never be tunneled to the public.
# 8642 = Hermes dashboard/gateway; 8765 = this connector; 2375/2376 = Docker;
# 3306 = MySQL; 5432 = Postgres; 6379 = Redis; 9229 = Node inspector;
# 11211 = memcached; 11434 = Ollama.
RESERVED_TUNNEL_PORTS: Final = frozenset({
    2375, 2376, 3306, 5432, 6379, 8642, 8765, 9229, 11211, 11434,
})

MAX_TUNNEL_APPS: Final = 8


def validate_app_name(name: str) -> str:
    """Return the name if it is a safe tunnel app name, else raise ValueError."""
    if not isinstance(name, str) or len(name) < 1 or len(name) > 63:
        raise ValueError("app name must be 1-63 characters")
    if APP_NAME_RE.fullmatch(name) is None:
        raise ValueError("app name must be lowercase letters, digits, and hyphens only")
    return name


def validate_tunnel_port(port: int) -> int:
    """Return the port if it is a safe tunnel target port, else raise ValueError."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if port < 1024 or port > 65535:
        raise ValueError("port must be in the unprivileged range 1024-65535")
    if port in RESERVED_TUNNEL_PORTS:
        raise ValueError("port is reserved for infrastructure services")
    return port


class AppTunnelRegistry:
    """Bounded in-memory map of app name -> loopback port."""

    def __init__(self, max_apps: int = MAX_TUNNEL_APPS) -> None:
        if max_apps < 1:
            raise ValueError("max_apps must be at least 1")
        self.max_apps = max_apps
        self._entries: dict[str, int] = {}

    def register(self, name: str, port: int) -> None:
        valid_name = validate_app_name(name)
        valid_port = validate_tunnel_port(port)
        if valid_name not in self._entries and len(self._entries) >= self.max_apps:
            raise ValueError(f"app tunnel limit of {self.max_apps} reached")
        self._entries[valid_name] = valid_port

    def unregister(self, name: str) -> bool:
        valid_name = validate_app_name(name)
        return self._entries.pop(valid_name, None) is not None

    def get(self, name: str) -> int | None:
        valid_name = validate_app_name(name)
        return self._entries.get(valid_name)

    def list(self) -> list[tuple[str, int]]:
        return list(self._entries.items())
