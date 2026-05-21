# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared default-TTL resolver for the memory write surfaces.

G5.2-T2 (#624) introduced default-TTL injection on user-scope memory
writes: when the caller omits ``expires_at`` and the scope is
:attr:`MemoryScope.USER`, the surface layer injects
``now(UTC) + Settings.memory_user_default_ttl_days``. Two opt-outs
remain explicit:

* explicit ``null`` (the CLI ``--persist`` shape) → ``None``.
* explicit ISO-8601 timestamp → honoured verbatim.

G0.9.1-T3 (#775) lifted this resolver out of
:mod:`meho_backplane.api.v1.memory` so both the REST handler **and**
the MCP ``add_to_memory`` tool consume the same discrimination. Prior
to that, the MCP handler always passed ``expires_at`` explicitly to
:meth:`MemoryService.remember` -- defeating the "set vs unset" split
the REST layer modelled with :attr:`BaseModel.model_fields_set`.

Why a primitives-shape signature
================================

The resolver takes the raw triple ``(scope, expires_at_was_set,
explicit_expires_at)`` rather than a Pydantic model so the MCP entry
point -- which receives plain ``dict[str, Any]`` arguments and has no
Pydantic shell on the inbound shape -- can call it without
synthesising a fake model. Each caller decides locally what "the
field was set" means:

* The REST handler uses :attr:`RememberBody.model_fields_set` -- the
  pydantic-v2-canonical way to discriminate "field absent from JSON"
  from "field present with value null".
* The MCP handler uses ``"ttl" in arguments`` -- the dispatcher
  populates ``arguments`` only with the keys the inbound JSON-RPC
  payload carried (the JSON-Schema ``additionalProperties: false``
  guard already rejects unknown keys upstream), so membership is the
  same set-vs-unset discriminant.

The policy itself lives here -- one place to change the default
windowing, one place to widen the gate to other scopes, one place
the test suite pins.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.settings import get_settings

__all__ = ["resolve_default_expires_at"]


def resolve_default_expires_at(
    scope: MemoryScope,
    *,
    expires_at_was_set: bool,
    explicit_expires_at: datetime | None,
) -> datetime | None:
    """Return the effective ``expires_at`` for a memory write.

    Parameters
    ----------
    scope
        The memory scope being written. Only :attr:`MemoryScope.USER`
        is in scope for the default-TTL gate per #624.
    expires_at_was_set
        ``True`` when the caller supplied ``expires_at`` (REST) or
        ``ttl`` (MCP) explicitly -- including an explicit ``null`` /
        omitted-duration shape. ``False`` when the field was absent
        from the inbound payload.
    explicit_expires_at
        The caller-supplied value when ``expires_at_was_set`` is
        ``True``; ignored otherwise.

    Returns
    -------
    datetime | None
        * ``explicit_expires_at`` verbatim when the caller set the
          field (the CLI ``--persist`` opt-out path returns ``None``
          here).
        * ``None`` when the scope is not :attr:`MemoryScope.USER`
          (per #624's narrow gate).
        * ``now(UTC) + memory_user_default_ttl_days`` otherwise.
    """
    if expires_at_was_set:
        return explicit_expires_at
    if scope is not MemoryScope.USER:
        return None
    settings = get_settings()
    return datetime.now(UTC) + timedelta(days=settings.memory_user_default_ttl_days)
