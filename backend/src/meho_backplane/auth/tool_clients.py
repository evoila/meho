# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The login clients of the AI tools, and the check that they exist.

With the ``MCP_CLIENT_PER_TOOL_KIND`` switch on, ``GET /api/v1/auth-config``
lists one login client per AI tool kind (``mcp_clients``). The Claude Code
plugin and the Claude Desktop extension then sign in with that client. A
client that is missing in Keycloak would stop that tool from signing in.

So before MEHO lists a client, it asks Keycloak whether the client exists,
with the admin client (``KEYCLOAK_ADMIN_*``). MEHO lists only the clients
it found. A tool whose client is not listed keeps the shared ``meho-mcp``
client, which works as before.

* When the check cannot run (the admin client is not set up, or Keycloak
  does not answer), MEHO lists no client, and ``/ready`` says ``unknown``
  with the reason. A failed check never reads as "all clients exist".
* The answer is kept for :data:`CHECK_TTL_S` seconds. ``/auth-config``
  needs no login, so a caller must not be able to make MEHO call Keycloak
  on every request. One check runs at a time.
* ``/ready`` never waits for Keycloak. It shows the last answer and starts
  a new check in the background when the last one is too old.

With the switch off, nothing here runs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient, KeycloakAdminError
from meho_backplane.settings import Settings

__all__ = [
    "CHECK_TTL_S",
    "MCP_TOOL_CLIENTS",
    "ToolClientCheck",
    "check_tool_clients",
    "listed_tool_clients",
    "reset_tool_client_check",
    "tool_clients_report",
]

#: The login client of each AI tool kind. The realm file of the built-in
#: Keycloak and ``meho admin keycloak bootstrap-clients`` create these
#: clients with the same settings as ``meho-mcp``. The keys are the values
#: the tools send in the ``Meho-Tool`` header.
MCP_TOOL_CLIENTS: Final[dict[str, str]] = {
    "claude-code": "meho-mcp-code",
    "claude-desktop": "meho-mcp-desktop",
}

#: How long one check answer is used.
CHECK_TTL_S: Final[float] = 60.0

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ToolClientCheck:
    """One answer of the check.

    ``present`` maps each tool kind whose client exists to that client.
    ``missing`` names the clients Keycloak does not have. ``error`` is set
    when the check could not run; then ``present`` and ``missing`` are
    empty, because nothing is known.
    """

    present: dict[str, str] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    error: str | None = None
    checked_at: float = 0.0

    @property
    def state(self) -> Literal["ok", "missing", "unknown"]:
        if self.error is not None:
            return "unknown"
        return "missing" if self.missing else "ok"

    def report(self) -> dict[str, Any]:
        """The ``/ready`` shape: state, the missing clients, and the reason."""
        return {"state": self.state, "missing": list(self.missing), "detail": self.error}


_last: ToolClientCheck | None = None
_lock = asyncio.Lock()
_refresh: asyncio.Task[ToolClientCheck] | None = None


def _is_fresh(check: ToolClientCheck | None) -> bool:
    return check is not None and time.monotonic() - check.checked_at < CHECK_TTL_S


async def _run_check(settings: Settings) -> ToolClientCheck:
    try:
        async with KeycloakAdminClient.from_settings(settings) as admin:
            found = {
                kind: client_id
                for kind, client_id in MCP_TOOL_CLIENTS.items()
                if await admin.client_exists(client_id)
            }
    except KeycloakAdminError as exc:
        _log.warning("mcp_tool_client_check_failed", error=str(exc))
        return ToolClientCheck(error=str(exc), checked_at=time.monotonic())
    missing = tuple(c for k, c in MCP_TOOL_CLIENTS.items() if k not in found)
    if missing:
        _log.warning("mcp_tool_clients_missing", missing=list(missing))
    return ToolClientCheck(present=found, missing=missing, checked_at=time.monotonic())


async def check_tool_clients(settings: Settings) -> ToolClientCheck:
    """Return the last answer while it is fresh; else ask Keycloak again."""
    global _last
    async with _lock:
        if _last is None or not _is_fresh(_last):
            _last = await _run_check(settings)
        return _last


async def listed_tool_clients(settings: Settings) -> dict[str, str]:
    """The ``mcp_clients`` of ``/auth-config``: the clients that exist.

    Empty with the switch off, and empty when the check could not run.
    """
    if not settings.mcp_client_per_tool_kind:
        return {}
    return dict((await check_tool_clients(settings)).present)


def tool_clients_report(settings: Settings) -> dict[str, Any] | None:
    """The ``tool_clients`` entry of ``/ready``, or ``None`` with the switch off.

    Does not wait for Keycloak: it shows the last answer and, when that is
    missing or too old, starts a check in the background.
    """
    global _refresh
    if not settings.mcp_client_per_tool_kind:
        return None
    last = _last
    if not _is_fresh(last) and (_refresh is None or _refresh.done()):
        _refresh = asyncio.get_running_loop().create_task(check_tool_clients(settings))
    if last is None:
        return {"state": "unknown", "missing": [], "detail": "not checked yet"}
    return last.report()


def reset_tool_client_check() -> None:
    """Forget the last answer. For tests."""
    global _last, _refresh
    _last = None
    _refresh = None
