# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sandbox mode: one instance-wide switch for disposable test systems (#4622).

Decision record: ``docs/decisions/sandbox-mode.md``. In sandbox mode a
tenant admin may give a service principal a standing grant that also
covers deletes (a **delete grant**), so an automated test can build,
change and delete its own test systems with no wait for a person. Every
other instance keeps today's rules.

Three things live here:

* **The runtime flag.** :func:`sandbox_mode_active` is the one source every
  runtime reader uses (the policy gate, the grant service, the audit model
  default, the display surfaces). It is set once at boot by
  :func:`assert_sandbox_mode_consistent`, after the stored mode is checked,
  and never changes while the process runs. It is a process flag, not a
  settings read, so a code path that writes an audit row does not need the
  full :class:`~meho_backplane.settings.Settings` to be loadable.
* **The boot guards.** :func:`check_sandbox_settings` refuses to start when
  the mode is on but the confirm value is missing, is not the host of the
  MCP resource URI, or that host has no dot-separated part that is exactly
  ``sandbox``.
* **The stored mode.** :func:`store_or_check_sandbox_mode` writes the
  setting into the one-row table ``sandbox_mode_state`` with one insert
  that only one process can win, then reads the stored mode back. A stored
  mode that differs from the setting refuses the start. So the mode is
  fixed for the life of the database, and two processes that start at once
  cannot store different modes.

This module has no import-time dependency on the rest of MEHO so the
models module can import the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import structlog

if TYPE_CHECKING:
    from meho_backplane.settings import Settings

__all__ = [
    "SANDBOX_BANNER_EXPIRY_WINDOW_DAYS",
    "SANDBOX_DELETE_GRANT_MAX_DAYS",
    "SANDBOX_HOST_LABEL",
    "SandboxModeConfigError",
    "assert_sandbox_mode_consistent",
    "check_sandbox_settings",
    "sandbox_mode_active",
    "sandbox_mode_label",
    "set_sandbox_mode_active",
    "store_or_check_sandbox_mode",
]

_log = structlog.get_logger(__name__)

#: A delete grant must expire within this many days of its creation.
SANDBOX_DELETE_GRANT_MAX_DAYS: Final[int] = 30

#: The console banner counts the delete grants that expire within this window.
SANDBOX_BANNER_EXPIRY_WINDOW_DAYS: Final[int] = 7

#: The dot-separated host-name part that confirms a sandbox instance.
SANDBOX_HOST_LABEL: Final[str] = "sandbox"

#: The one row of ``sandbox_mode_state``.
_STATE_ROW_ID: Final[int] = 1

_active: bool = False


class SandboxModeConfigError(RuntimeError):
    """MEHO refuses to start: the sandbox settings or the stored mode are wrong."""


def sandbox_mode_active() -> bool:
    """Whether this process runs in sandbox mode (set once at boot)."""
    return _active


def set_sandbox_mode_active(active: bool) -> None:
    """Set the process flag. Called by the boot step; also the test seam."""
    global _active
    _active = bool(active)


def sandbox_mode_label() -> str:
    """``"on"`` or ``"off"``: the word the display surfaces show."""
    return "on" if _active else "off"


def _confirm_host(settings: Settings) -> str:
    """The host name of the MCP resource URI, lower-cased; empty when unset."""
    from meho_backplane.mcp.auth import mcp_resource_uri

    host = urlsplit(mcp_resource_uri(settings)).hostname
    return (host or "").lower()


def check_sandbox_settings(settings: Settings) -> None:
    """Refuse to start when the sandbox settings do not agree (guards 1 and 2).

    With the mode off nothing is checked. With it on:

    1. ``MEHO_SANDBOX_MODE_CONFIRM`` must be set and must equal the host of
       the MCP resource URI (the public address of the instance).
    2. One dot-separated part of that host must be exactly ``sandbox``:
       ``meho.sandbox.example`` passes, ``meho.example`` and
       ``meho-sandbox.example`` are refused.

    Raises :class:`SandboxModeConfigError` with a plain message.
    """
    if not settings.sandbox_mode:
        return
    confirm = settings.sandbox_mode_confirm.strip().lower()
    host = _confirm_host(settings)
    if not confirm:
        raise SandboxModeConfigError(
            "MEHO_SANDBOX_MODE is on but MEHO_SANDBOX_MODE_CONFIRM is not set. "
            f"Set it to the public host name of this instance ({host or 'unknown'}) "
            "to confirm that this instance holds only disposable test systems, "
            "or turn MEHO_SANDBOX_MODE off."
        )
    if confirm != host:
        raise SandboxModeConfigError(
            f"MEHO_SANDBOX_MODE_CONFIRM ({confirm!r}) is not the host name of this "
            f"instance ({host or 'unknown'!r}, the host of the MCP resource URI). "
            "These settings may have been copied from another instance. Set the "
            "confirm value to this instance's own host name, or turn "
            "MEHO_SANDBOX_MODE off."
        )
    if SANDBOX_HOST_LABEL not in host.split("."):
        raise SandboxModeConfigError(
            f"Sandbox mode needs a host name with a part that is exactly "
            f"'{SANDBOX_HOST_LABEL}' (for example meho.sandbox.example); "
            f"{host!r} has no such part. Sandbox mode is only for an instance that "
            "holds disposable test systems. Turn MEHO_SANDBOX_MODE off on every "
            "other instance."
        )


async def store_or_check_sandbox_mode(settings: Settings) -> bool:
    """Store the setting once, then check the stored mode (guard 3).

    One ``INSERT ... ON CONFLICT DO NOTHING`` on the single-row table: the
    first process to run it stores its mode, every later one changes
    nothing. Then the stored mode is read back and compared with the
    setting. A difference refuses the start: the database is fixed to the
    other mode (it was installed with the other setting, it belongs to
    another instance, or a backup from the other kind of instance was
    restored). Returns the stored mode.
    """
    from sqlalchemy import text

    from meho_backplane.db.engine import get_sessionmaker

    wanted = bool(settings.sandbox_mode)
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO sandbox_mode_state (id, sandbox_mode, stored_at) "
                "VALUES (:id, :mode, :now) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": _STATE_ROW_ID, "mode": wanted, "now": datetime.now(UTC)},
        )
        await session.commit()
        stored = (
            await session.execute(
                text("SELECT sandbox_mode FROM sandbox_mode_state WHERE id = :id"),
                {"id": _STATE_ROW_ID},
            )
        ).scalar_one()
    stored_mode = bool(stored)
    if stored_mode != wanted:
        stored_word = "on" if stored_mode else "off"
        wanted_word = "on" if wanted else "off"
        raise SandboxModeConfigError(
            f"This database was installed with sandbox mode {stored_word}, but "
            f"MEHO_SANDBOX_MODE is {wanted_word}. The mode is fixed for the life "
            "of the database. To change it, install a new instance with a new "
            "database. Check that this instance points at its own database and "
            "that no backup from an instance of the other kind was restored."
        )
    return stored_mode


async def assert_sandbox_mode_consistent(settings: Settings) -> None:
    """Run every sandbox guard and set the process flag. Boot only.

    Runs before MEHO serves a request or starts a background task. Order:
    the settings guards first (no database needed), then the stored mode.
    """
    check_sandbox_settings(settings)
    stored = await store_or_check_sandbox_mode(settings)
    set_sandbox_mode_active(stored)
    if stored:
        _log.warning(
            "sandbox_mode_on",
            confirm_host=settings.sandbox_mode_confirm,
            note="delete grants for service principals are allowed on this instance",
        )
