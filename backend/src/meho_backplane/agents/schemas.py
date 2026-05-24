# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 models for the agent-definition CRUD surface.

G11.1-T2 (#809) under Initiative #802. Three write/read shapes plus the
logical model-tier enum:

* :class:`AgentDefinitionCreate` -- the POST body
  (:meth:`~meho_backplane.agents.service.AgentDefinitionService.create`).
* :class:`AgentDefinitionUpdate` -- the PATCH body (every field optional;
  only supplied fields change).
* :class:`AgentDefinitionRead` -- the row representation every accessor
  returns.

All three set ``extra="forbid"`` so an unknown / mistyped field is a
422 at the boundary rather than a silently-dropped no-op -- the same
strictness :class:`~meho_backplane.api.v1.broadcast_overrides.BroadcastOverrideCreate`
applies.

The bounded :class:`AgentModelTier` enum is enforced as a Pydantic
``Literal``-equivalent (a typed enum field), not a DB ``CHECK``, so a
future tier (G11.5's multi-provider resolver may add one) lands without
a migration -- the forward-compat argument
:class:`~meho_backplane.db.models.BroadcastOverride.scope_field` makes.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "NAME_PATTERN",
    "AgentDefinitionCreate",
    "AgentDefinitionRead",
    "AgentDefinitionUpdate",
    "AgentModelTier",
    "validate_name",
]


#: Regex anchoring the agent-name character set the surface accepts.
#: An agent name is the operator-facing handle *and* a URL path segment
#: (``GET /api/v1/agents/{name}``), so it is constrained to the
#: safe-URL alphabet: letters, digits, hyphen, underscore, dot. This
#: mirrors :data:`meho_backplane.memory.schemas.SLUG_PATTERN` -- the
#: same human-and-machine-friendly identifier shape (``incident-triage``,
#: ``vm.inventory-bot``) while excluding slash, colon, and whitespace
#: that would break path routing or shell quoting.
NAME_PATTERN: str = r"^[A-Za-z0-9_\-\.]+$"

#: Compiled form of :data:`NAME_PATTERN`. Module-level so the
#: service-layer validator at write boundaries does not pay a per-call
#: compile cost.
_NAME_RE: re.Pattern[str] = re.compile(NAME_PATTERN)


def validate_name(name: str) -> str:
    """Validate *name* against :data:`NAME_PATTERN`; return it on success.

    Raises :class:`ValueError` when *name* contains characters outside
    the safe set. The service-layer write boundaries call this so direct
    (non-Pydantic) callers cannot smuggle a slash- or colon-containing
    name past the :class:`AgentDefinitionCreate` field pattern -- such a
    name would break the ``/api/v1/agents/{name}`` route's path segment.
    Pure function; returns the input unchanged on success so call sites
    can write ``name = validate_name(name)``.
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"agent name {name!r} contains characters outside the safe set "
            f"(allowed: letters, digits, hyphen, underscore, dot)"
        )
    return name


class AgentModelTier(StrEnum):
    """Logical model tier an agent definition runs against.

    The tier is *logical* -- G11.5's multi-provider resolver maps it to
    a concrete model backend at run time. T2 stores the tier verbatim;
    it does not resolve it. ``StrEnum`` (PEP 663, stdlib 3.11+) gives
    the members ``str`` semantics so ``f"tier={AgentModelTier.STANDARD}"``
    renders as ``"tier=standard"``, matching the
    :class:`~meho_backplane.auth.operator.TenantRole` /
    :class:`~meho_backplane.memory.schemas.MemoryScope` convention.

    Three tiers in v0.2:

    * ``standard`` -- the default general-purpose tier.
    * ``fast`` -- a cheaper / lower-latency tier for simple loops.
    * ``deep`` -- a more capable tier for harder reasoning.
    """

    STANDARD = "standard"
    FAST = "fast"
    DEEP = "deep"


#: Inclusive upper bound on :attr:`AgentDefinitionCreate.turn_budget`.
#: A turn is one model round-trip; the runtime stops the loop once the
#: budget is spent (Pydantic AI ``UsageLimits(request_limit=...)`` in
#: T1). The cap is a safety ceiling against a misconfigured definition
#: that would loop indefinitely / burn an unbounded provider bill;
#: 1000 is far above any realistic ops loop while still bounded.
_MAX_TURN_BUDGET: int = 1000


class AgentDefinitionCreate(BaseModel):
    """POST body -- the inputs ``create`` consumes. Pydantic v2 strict.

    ``extra="forbid"`` rejects unknown fields with 422 (catches a client
    typo like ``"system-prompt"`` before it lands as a silent no-op).
    ``name`` is constrained to :data:`NAME_PATTERN` at construction time;
    the service-layer :func:`validate_name` is the parallel gate for
    non-Pydantic call paths.

    ``toolset`` and ``output_schema`` are free-shaped JSON objects --
    T3 (#810) owns the toolset-resolution contract and the runtime
    (T1 #808) owns the output-schema contract; T2 only stores them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=NAME_PATTERN)
    identity_ref: str = Field(min_length=1, max_length=256)
    model_tier: AgentModelTier
    system_prompt: str = Field(min_length=1)
    toolset: dict[str, Any] = Field(default_factory=dict)
    turn_budget: int = Field(ge=1, le=_MAX_TURN_BUDGET)
    output_schema: dict[str, Any] | None = None
    enabled: bool = True


class AgentDefinitionUpdate(BaseModel):
    """PATCH body -- every field optional; only supplied fields change.

    ``extra="forbid"`` for the same reason as
    :class:`AgentDefinitionCreate`. A field left ``None`` (its default)
    is *not* applied -- the service distinguishes "field omitted" from
    "field set to a value" via :meth:`pydantic.BaseModel.model_dump`
    with ``exclude_unset=True``, so a PATCH can change a single field
    without clobbering the rest.

    ``name`` is *not* updatable here: it is the per-tenant natural key
    (the URL path segment + unique index). Renaming an agent is a
    delete + recreate, which keeps the natural-key invariant simple and
    avoids a name-collision race inside the update path.

    ``output_schema`` cannot be cleared through this shape (``None`` is
    indistinguishable from "omitted" under ``exclude_unset``); clearing
    a structured-output schema is a delete + recreate in v0.2. This is
    an accepted v0.2 limitation, not a silent bug -- documented so a
    future shape (a sentinel / ``JsonValue`` discriminator) can lift it.
    """

    model_config = ConfigDict(extra="forbid")

    identity_ref: str | None = Field(default=None, min_length=1, max_length=256)
    model_tier: AgentModelTier | None = None
    system_prompt: str | None = Field(default=None, min_length=1)
    toolset: dict[str, Any] | None = None
    turn_budget: int | None = Field(default=None, ge=1, le=_MAX_TURN_BUDGET)
    output_schema: dict[str, Any] | None = None
    enabled: bool | None = None


class AgentDefinitionRead(BaseModel):
    """Row representation every accessor returns.

    ``from_attributes=True`` lets the route / service hand back the
    SQLAlchemy ORM row directly; Pydantic serialises via this model
    rather than the ORM's ``__dict__``. The model is the single source
    of truth for which columns the REST + MCP surfaces expose, so a
    future column addition is opt-in (it must be declared here to leak).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    identity_ref: str
    model_tier: str
    system_prompt: str
    toolset: dict[str, Any]
    turn_budget: int
    output_schema: dict[str, Any] | None
    enabled: bool
    created_by_sub: str
    created_at: datetime
    updated_at: datetime
