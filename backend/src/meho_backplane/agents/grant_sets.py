# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Starting grant sets: the first grants a person picks for a new agent.

When a person registers an agent principal
(:meth:`~meho_backplane.auth.agent_principals.AgentPrincipalService.register`)
or creates an agent definition
(:meth:`~meho_backplane.agents.service.AgentDefinitionService.create`),
they pick one of three sets (#6191):

* ``none`` -- no row. The agent runs nothing until an admin adds a grant.
* ``read-everything`` -- one row: ``op_pattern="*"``, ``target_scope="*"``,
  ``auto-execute``.
* ``read-targets`` -- one row per named target: ``op_pattern="*"``,
  ``target_scope=<target id>``, ``auto-execute``.

The risk-level ceiling (``auth/permissions.py``) keeps every operation
above ``safe`` waiting for a person's approval, so ``auto-execute`` here
means "``safe`` operations run at once". The rows are ordinary
``agent_permission`` rows: they show in ``meho agent grant list`` and an
admin removes them like any other grant.

The pick is required while ``AGENT_GRANT_REQUIRED`` is not ``off``. For a
definition it is required only when its agent has no live grant yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AgentPermission
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

__all__ = [
    "MAX_STARTING_TARGETS",
    "NOT_CHOSEN",
    "STARTING_GRANTS_JSON_SCHEMA",
    "StartingGrantSet",
    "StartingGrants",
    "StartingGrantsError",
    "audit_fields",
    "has_live_grant",
    "require_pick",
    "resolve_scopes",
    "write_starting_grants",
]

_log = structlog.get_logger(__name__)

#: The audit value when no set was picked (the switch is ``off``, or the
#: definition's agent already has a grant).
NOT_CHOSEN: Final[str] = "not-chosen"

#: Upper bound on the target names of one ``read-targets`` pick.
MAX_STARTING_TARGETS: Final[int] = 100

_OP_PATTERN: Final[str] = "*"
_VERDICT: Final[str] = "auto-execute"


class StartingGrantSet(StrEnum):
    """The three starting grant sets a person can pick."""

    NONE = "none"
    READ_EVERYTHING = "read-everything"
    READ_TARGETS = "read-targets"


class StartingGrants(BaseModel):
    """The first grants of a new agent, picked when it is registered or defined."""

    model_config = ConfigDict(extra="forbid")

    set: StartingGrantSet = Field(
        description=(
            "'none': the agent runs nothing until an admin adds a grant. "
            "'read-everything': safe operations run at once on every target; "
            "everything else waits for a person's approval. "
            "'read-targets': the same, but only on the targets in 'targets'."
        ),
    )
    targets: list[str] = Field(
        default_factory=list,
        max_length=MAX_STARTING_TARGETS,
        description="Target names. Only for 'read-targets', and required there.",
    )

    @model_validator(mode="after")
    def _targets_match_set(self) -> Self:
        if self.set is StartingGrantSet.READ_TARGETS:
            if not self.targets:
                raise ValueError("'read-targets' needs at least one target name in 'targets'")
            if any(not name.strip() for name in self.targets):
                raise ValueError("a target name in 'targets' must not be empty")
        elif self.targets:
            raise ValueError(
                f"'targets' is only used with 'read-targets', not with '{self.set.value}'"
            )
        return self


#: The ``starting_grants`` property of the admin MCP tools' input schemas.
STARTING_GRANTS_JSON_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "description": (
        "The agent's first grants. Required while AGENT_GRANT_REQUIRED is not "
        "'off' (for an agent definition: only when its agent has no grant yet). "
        "set='none': the agent runs nothing until an admin adds a grant. "
        "set='read-everything': safe operations run at once on every target; "
        "everything else waits for a person's approval. "
        "set='read-targets': the same, only on the targets named in 'targets'."
    ),
    "properties": {
        "set": {"type": "string", "enum": [member.value for member in StartingGrantSet]},
        "targets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": MAX_STARTING_TARGETS,
            "description": "Target names. Only for set='read-targets', and required there.",
        },
    },
    "required": ["set"],
    "additionalProperties": False,
}


class StartingGrantsError(ValueError):
    """The starting grant pick is missing or names a target that does not resolve.

    ``code`` is the stable error code; ``str(exc)`` is ``"<code>: <text>"``.
    The REST routes answer 422, the MCP tools ``-32602``. Nothing is written.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def require_pick(starting: StartingGrants | None) -> None:
    """Raise ``starting_grants_required`` when no set was picked and the switch is not off."""
    if starting is not None or get_settings().agent_grant_required == "off":
        return
    raise StartingGrantsError(
        "starting_grants_required",
        "pick the agent's starting grants in 'starting_grants'. "
        "Choose 'none' (the agent runs nothing until an admin adds a grant), "
        "'read-everything' (safe operations run at once on every target; "
        "everything else waits for a person's approval), or 'read-targets' "
        "with a list of target names (the same, only on those targets).",
    )


async def resolve_scopes(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    starting: StartingGrants | None,
) -> list[str]:
    """Return the ``target_scope`` of each row the set writes, in the caller's tenant.

    ``none`` (or no pick) gives no row, ``read-everything`` gives ``["*"]``,
    ``read-targets`` gives the target ids, once each. An unknown or
    ambiguous name raises :class:`StartingGrantsError`.
    """
    if starting is None or starting.set is StartingGrantSet.NONE:
        return []
    if starting.set is StartingGrantSet.READ_EVERYTHING:
        return ["*"]
    scopes: list[str] = []
    # resolve_target binds target_id / target_name for the audit row of a
    # dispatch. Here the audit row is about the agent, so drop them after.
    with structlog.contextvars.bound_contextvars(target_id=None, target_name=None):
        for name in starting.targets:
            try:
                target = await resolve_target(session, tenant_id, name)
            except TargetNotFoundError as exc:
                raise StartingGrantsError(
                    "starting_grants_target_unknown",
                    f"no target named {name!r} in this tenant. Nothing was created.",
                ) from exc
            except AmbiguousTargetError as exc:
                raise StartingGrantsError(
                    "starting_grants_target_ambiguous",
                    f"the name {name!r} matches more than one target. "
                    "Use the target's exact name. Nothing was created.",
                ) from exc
            scope = str(target.id)
            if scope not in scopes:
                scopes.append(scope)
    return scopes


async def has_live_grant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_sub: str,
) -> bool:
    """Return whether *principal_sub* has a grant row that has not expired."""
    result = await session.execute(
        select(AgentPermission.id)
        .where(
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.principal_sub == principal_sub,
            (AgentPermission.expires_at.is_(None))
            | (AgentPermission.expires_at > datetime.now(UTC)),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def write_starting_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_sub: str,
    created_by_sub: str,
    scopes: list[str],
) -> int:
    """Add the rows for *scopes* in the caller's transaction; return how many were new.

    A row the agent already has (same operation pattern and target) is
    skipped, so a pick never fails on an existing grant. The caller commits.
    """
    if not scopes:
        return 0
    result = await session.execute(
        select(AgentPermission.target_scope).where(
            AgentPermission.tenant_id == tenant_id,
            AgentPermission.principal_sub == principal_sub,
            AgentPermission.op_pattern == _OP_PATTERN,
            AgentPermission.target_scope.in_(scopes),
        )
    )
    existing = set(result.scalars().all())
    new_scopes = [scope for scope in scopes if scope not in existing]
    for scope in new_scopes:
        session.add(
            AgentPermission(
                tenant_id=tenant_id,
                principal_sub=principal_sub,
                op_pattern=_OP_PATTERN,
                target_scope=scope,
                verdict=_VERDICT,
                created_by_sub=created_by_sub,
            )
        )
    await session.flush()
    _log.info(
        "agent_starting_grants_written",
        tenant_id=str(tenant_id),
        principal_sub=principal_sub,
        rows=len(new_scopes),
        skipped=len(scopes) - len(new_scopes),
        created_by_sub=created_by_sub,
    )
    return len(new_scopes)


def audit_fields(starting: StartingGrants | None) -> dict[str, object]:
    """The ``audit_*`` values that name the picked set on the creation's audit row."""
    if starting is None:
        return {"audit_starting_grants": NOT_CHOSEN}
    fields: dict[str, object] = {"audit_starting_grants": starting.set.value}
    if starting.set is StartingGrantSet.READ_TARGETS:
        fields["audit_starting_grant_targets"] = list(starting.targets)
    return fields
