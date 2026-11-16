# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save an agent definition from the console forms; map known failures to field errors.

:func:`save_create` and :func:`save_edit` call
:class:`~meho_backplane.agents.service.AgentDefinitionService` and turn
the errors a person can fix (a duplicate name, an unknown
``identity_ref``, a missing or wrong starting grant pick) into the
per-field messages the create and edit modals show
(:mod:`~meho_backplane.ui.routes.agents.forms`).

The callers in ``forms.py`` bind the ``audit_*`` values before they
read the pick, the same values as ``POST`` / ``PATCH /api/v1/agents``.
"""

from __future__ import annotations

from fastapi import HTTPException

from meho_backplane.agents.grant_sets import StartingGrantsError
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentDefinitionUpdate
from meho_backplane.agents.service import (
    AgentDefinitionExistsError,
    AgentDefinitionService,
    AgentIdentityRefInvalidError,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.routes.agents import starting_grants

__all__ = ["save_create", "save_edit"]

_IDENTITY_REF_ERROR = (
    "identity_ref does not resolve to a registered, non-revoked agent principal in this tenant"
)


async def save_create(
    session_ctx: UISessionContext,
    operator: Operator,
    body: AgentDefinitionCreate,
) -> dict[str, str] | None:
    """Create the definition; return the field errors of a known failure."""
    try:
        await AgentDefinitionService().create(
            tenant_id=session_ctx.tenant_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except StartingGrantsError as exc:
        return {starting_grants.FIELD: starting_grants.error_text(exc)}
    except AgentDefinitionExistsError:
        return {"name": "an agent with this name already exists"}
    except AgentIdentityRefInvalidError:
        return {"identity_ref": _IDENTITY_REF_ERROR}
    return None


async def save_edit(
    session_ctx: UISessionContext,
    operator: Operator,
    name: str,
    body: AgentDefinitionUpdate,
) -> dict[str, str] | None:
    """Save the edit; return the field errors of a known failure (404 when absent)."""
    try:
        entry = await AgentDefinitionService().update(
            session_ctx.tenant_id, name, body, updated_by_sub=operator.sub
        )
    except StartingGrantsError as exc:
        return {starting_grants.FIELD: starting_grants.error_text(exc)}
    except AgentIdentityRefInvalidError:
        return {"identity_ref": _IDENTITY_REF_ERROR}
    if entry is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return None
