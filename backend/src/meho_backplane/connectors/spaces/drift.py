# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``spaces.drift`` read: where the ownership record and the live data disagree.

Reads stored data only: spaces, current assignments, targets (with the last
probe answer) and the topology graph. It calls no vendor system, writes
nothing and repairs nothing. It reports; a person decides.

Five kinds of drift, each a list plus a count:

* ``unassigned_targets``: a live target that no space owns.
* ``missing_targets``: a current target assignment whose target is gone
  (deleted, or never existed).
* ``unreachable_targets``: an assigned target whose last probe said it is not
  reachable. A target that was never probed is not listed: unknown is not
  unreachable.
* ``missing_objects``: a current machine assignment whose vendor object id
  is in no live topology node of the tenant.
* ``state_mismatch``: a current assignment that disagrees with the live
  record: its space is disabled (``space_disabled``), or its target now has
  another name than when it was assigned (``target_renamed``).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.spaces.common import require_enabled
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode, Target
from meho_backplane.spaces.models import ResourceAssignment, Space

__all__ = ["DRIFT_KINDS", "find_drift", "spaces_drift"]

#: The five kinds of drift, in the order the answer lists them.
DRIFT_KINDS: tuple[str, ...] = (
    "unassigned_targets",
    "missing_targets",
    "unreachable_targets",
    "missing_objects",
    "state_mismatch",
)

_NAME_CHUNK = 500


def _unreachable(target: Target) -> bool:
    fingerprint = target.fingerprint
    return isinstance(fingerprint, dict) and fingerprint.get("reachable") is False


def _probed_at(target: Target) -> str | None:
    fingerprint = target.fingerprint
    probed = fingerprint.get("probed_at") if isinstance(fingerprint, dict) else None
    return probed if isinstance(probed, str) else None


async def _live_node_names(
    session: AsyncSession, tenant_id: uuid.UUID, names: set[str]
) -> set[str]:
    """Which of *names* are the name of a live topology node of the tenant."""
    found: set[str] = set()
    ordered = sorted(names)
    for start in range(0, len(ordered), _NAME_CHUNK):
        chunk = ordered[start : start + _NAME_CHUNK]
        rows = await session.scalars(
            select(GraphNode.name).where(
                GraphNode.tenant_id == tenant_id,
                GraphNode.last_seen.is_not(None),
                GraphNode.name.in_(chunk),
            )
        )
        found.update(rows)
    return found


def _where(space: Space | None, row: ResourceAssignment) -> dict[str, Any]:
    return {
        "assignment_id": str(row.id),
        "space_id": str(row.space_id),
        "space_name": space.name if space is not None else None,
    }


async def find_drift(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[str, list[dict[str, Any]]]:
    """Compare the current assignments of one tenant with its live targets and topology."""
    spaces = {
        s.id: s for s in await session.scalars(select(Space).where(Space.tenant_id == tenant_id))
    }
    assignments = list(
        await session.scalars(
            select(ResourceAssignment)
            .where(ResourceAssignment.tenant_id == tenant_id, ResourceAssignment.valid_to.is_(None))
            .order_by(ResourceAssignment.valid_from, ResourceAssignment.id)
        )
    )
    targets = {
        t.id: t
        for t in await session.scalars(
            select(Target)
            .where(Target.tenant_id == tenant_id, Target.deleted_at.is_(None))
            .order_by(Target.name, Target.id)
        )
    }
    object_ids = {a.object_id for a in assignments if a.kind == "object" and a.object_id}
    seen_objects = await _live_node_names(session, tenant_id, object_ids) if object_ids else set()

    drift: dict[str, list[dict[str, Any]]] = {kind: [] for kind in DRIFT_KINDS}
    owned: set[uuid.UUID] = set()
    reported_unreachable: set[uuid.UUID] = set()
    for row in assignments:
        space = spaces.get(row.space_id)
        if space is not None and space.state != "active":
            drift["state_mismatch"].append(
                {**_where(space, row), "reason": "space_disabled", "name": row.name_at_assignment}
            )
        if row.kind == "target":
            found = targets.get(row.target_id) if row.target_id is not None else None
            if found is None:
                drift["missing_targets"].append(
                    {
                        **_where(space, row),
                        "target_id": str(row.target_id) if row.target_id else None,
                        "name": row.name_at_assignment,
                    }
                )
                continue
            owned.add(found.id)
            if found.name != row.name_at_assignment:
                drift["state_mismatch"].append(
                    {
                        **_where(space, row),
                        "reason": "target_renamed",
                        "name": row.name_at_assignment,
                        "current_name": found.name,
                    }
                )
            if _unreachable(found) and found.id not in reported_unreachable:
                reported_unreachable.add(found.id)
                drift["unreachable_targets"].append(
                    {
                        **_where(space, row),
                        "target_id": str(found.id),
                        "name": found.name,
                        "probed_at": _probed_at(found),
                    }
                )
        elif row.kind == "object" and row.object_id and row.object_id not in seen_objects:
            drift["missing_objects"].append(
                {**_where(space, row), "object_id": row.object_id, "name": row.name_at_assignment}
            )
    for target in targets.values():
        if target.id not in owned:
            drift["unassigned_targets"].append(
                {"target_id": str(target.id), "name": target.name, "product": target.product}
            )
    return drift


async def spaces_drift(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List where the ownership record disagrees with the live targets.

    Op-id: ``spaces.drift``. Targetless and read-only. The tenant comes from
    *operator*. Answers the lists, one count for each kind and ``total_count``.
    """
    require_enabled()
    async with get_sessionmaker()() as session:
        drift = await find_drift(session, operator.tenant_id)
    counts = {kind: len(drift[kind]) for kind in DRIFT_KINDS}
    return {
        **drift,
        "counts": counts,
        "total_count": sum(counts.values()),
    }
