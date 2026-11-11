# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Turn a ``check_source`` pick into the normal event filter (#6182).

A person picks "this Sensor", "any Dashboard" and so on. MEHO stores the
ordinary event filter that the event matcher already understands:

    {"source": {"kind": "sensor", "id": "<id>"}, "worsening": true}

The id is stored, not the name, so a rename does not break the trigger.
The console, the CLI and the admin MCP tool all pass ``check_source`` on and
get the same stored filter, because the work is done once, here.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import CheckDashboard, Sensor
from meho_backplane.scheduler.schemas import CheckSource

__all__ = [
    "CheckSourceNotFoundError",
    "describe_check_source_filter",
    "list_check_source_choices",
    "resolve_check_source_filter",
]

_MODELS: Final[dict[str, type[Sensor] | type[CheckDashboard]]] = {
    "sensor": Sensor,
    "dashboard": CheckDashboard,
}


class CheckSourceNotFoundError(Exception):
    """The named Sensor or Dashboard does not exist in the trigger's tenant."""

    def __init__(self, kind: str, ref: str) -> None:
        self.kind = kind
        self.ref = ref
        super().__init__(f"{kind} {ref} not found in this tenant")


async def resolve_check_source_filter(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: CheckSource,
) -> dict[str, object]:
    """Return the event filter for *source*, after checking it in *tenant_id*."""
    model = _MODELS[source.kind]
    source_part: dict[str, object] = {"kind": source.kind}
    if source.id is not None or source.name is not None:
        stmt = select(model.id).where(model.tenant_id == tenant_id)
        stmt = stmt.where(
            model.id == source.id if source.id is not None else model.name == source.name
        )
        found = (await session.execute(stmt)).scalar_one_or_none()
        if found is None:
            raise CheckSourceNotFoundError(source.kind, str(source.id or source.name))
        source_part["id"] = str(found)
    event_filter: dict[str, object] = {"source": source_part}
    if source.only_worsening:
        event_filter["worsening"] = True
    return event_filter


async def list_check_source_choices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> dict[str, list[dict[str, str]]]:
    """Return the tenant's Sensors and Dashboards (id, name) for a picker."""
    choices: dict[str, list[dict[str, str]]] = {}
    for kind, model in _MODELS.items():
        rows = await session.execute(
            select(model.id, model.name).where(model.tenant_id == tenant_id).order_by(model.name)
        )
        choices[kind] = [{"id": str(row.id), "name": row.name} for row in rows]
    return choices


async def describe_check_source_filter(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    event_filter: dict[str, object] | None,
) -> str | None:
    """Say "Sensor web" or "any Dashboard, only when it gets worse" for a filter.

    Returns ``None`` when the filter does not have the shape ``check_source``
    makes (a custom filter), so a caller shows the raw filter instead.
    """
    if not event_filter or not set(event_filter) <= {"source", "worsening"}:
        return None
    source = event_filter.get("source")
    if not isinstance(source, dict) or set(source) - {"kind", "id"}:
        return None
    kind = source.get("kind")
    if kind not in _MODELS:
        return None
    label = "Sensor" if kind == "sensor" else "Dashboard"
    raw_id = source.get("id")
    if raw_id is None:
        text = f"any {label}"
    else:
        try:
            source_id = uuid.UUID(str(raw_id))
        except ValueError:
            return None
        model = _MODELS[kind]
        name = (
            await session.execute(
                select(model.name).where(model.id == source_id, model.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        text = f"{label} {name if name is not None else f'{source_id} (deleted)'}"
    if event_filter.get("worsening") is True:
        text += ", only when it gets worse"
    return text
