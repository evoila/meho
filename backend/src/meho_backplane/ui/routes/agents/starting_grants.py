# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The starting grant pick in the console forms (#6192).

The register form (``principals_forms.py``) and the agent definition
create form (``forms.py``) both ask the person for the agent's first
grants. The three choices come from
:class:`~meho_backplane.agents.grant_sets.StartingGrantSet`. No choice is
selected at first. While ``AGENT_GRANT_REQUIRED`` is not ``off`` the
choice is required in the form; the service checks it again and its
error text shows next to the field.

After a pick that writes grants, the console opens the agent grants
page filtered to the agent, so the person sees the grants that were
written.
"""

from __future__ import annotations

import uuid
from typing import Final
from urllib.parse import quote

from fastapi import status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select

from meho_backplane.agents.grant_sets import (
    StartingGrants,
    StartingGrantsError,
    StartingGrantSet,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.settings import get_settings

__all__ = [
    "FIELD",
    "GRANT_SET_MAX",
    "TARGETS_FIELD",
    "error_text",
    "grants_redirect",
    "parse_pick",
    "pick_context",
    "pick_values",
]

#: The form field names. Errors for the pick are keyed on :data:`FIELD`.
FIELD: Final[str] = "starting_grants"
TARGETS_FIELD: Final[str] = "starting_grant_targets"

#: Form-body cap for the set value (the longest value is 15 characters).
GRANT_SET_MAX: Final[int] = 32

_CHOICES: Final[tuple[dict[str, str], ...]] = (
    {
        "value": StartingGrantSet.NONE.value,
        "label": "None",
        "help": "The agent runs nothing until an admin adds a grant.",
    },
    {
        "value": StartingGrantSet.READ_EVERYTHING.value,
        "label": "Read everything",
        "help": (
            "Safe operations run at once on every target. "
            "Everything else waits for a person's approval."
        ),
    },
    {
        "value": StartingGrantSet.READ_TARGETS.value,
        "label": "Read some targets",
        "help": "The same, but only on the targets you pick below.",
    },
)


async def pick_context(tenant_id: uuid.UUID) -> dict[str, object]:
    """Return the template values the pick field needs, for *tenant_id*."""
    return {
        "grant_set_choices": _CHOICES,
        "grant_target_names": await _target_names(tenant_id),
        "grants_required": get_settings().agent_grant_required != "off",
    }


async def _target_names(tenant_id: uuid.UUID) -> list[str]:
    """Return the names of every live target of the tenant, sorted."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(Target.name)
            .where(Target.tenant_id == tenant_id, Target.deleted_at.is_(None))
            .order_by(Target.name)
        )
        return list(result.scalars().all())


def pick_values(grant_set: str, targets: list[str]) -> dict[str, object]:
    """Return the submitted pick, to show it again when the form has an error."""
    return {FIELD: grant_set.strip(), TARGETS_FIELD: list(targets)}


def parse_pick(grant_set: str, targets: list[str]) -> StartingGrants | None:
    """Return the picked set, or ``None`` when no option was picked.

    ``None`` lets the service decide whether a pick is needed. A wrong
    pick (unknown set, targets without "Read some targets", no target
    with it) raises :class:`ValueError` with a plain message.
    """
    picked = grant_set.strip()
    names = [name for name in (t.strip() for t in targets) if name]
    if not picked:
        if names:
            raise ValueError("Pick 'Read some targets' to use the targets you selected.")
        return None
    try:
        return StartingGrants.model_validate({"set": picked, "targets": names})
    except ValidationError as exc:
        first = exc.errors()[0]
        message = str(first.get("msg", "invalid starting grants"))
        raise ValueError(message.removeprefix("Value error, ")) from exc


def error_text(exc: StartingGrantsError) -> str:
    """Return the service's message without its error code."""
    return str(exc).removeprefix(f"{exc.code}: ")


def grants_redirect(principal_sub: str) -> HTMLResponse:
    """Return a 204 + ``HX-Redirect`` to the grants page of *principal_sub*."""
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": f"/ui/agents/grants?principal_sub={quote(principal_sub, safe='')}"},
    )
