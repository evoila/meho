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
* ``unreachable_targets``: an assigned target whose newest reach evidence
  says it is not reachable. The evidence is the last probe and the newest
  fresh health-check result on the target; the newer one wins. A target with
  no evidence is not listed: unknown is not unreachable.
* ``missing_objects``: a current machine assignment whose vendor object id
  is the id of no live machine node of the assigned target (of the tenant,
  when the assignment names no target).
* ``state_mismatch``: a current assignment that disagrees with the live
  record: its space is disabled (``space_disabled``), or its target now has
  another name than when it was assigned (``target_renamed``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.spaces.common import require_enabled
from meho_backplane.connectors.spaces.overview import check_is_fresh
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode, Sensor, Target
from meho_backplane.spaces.models import ResourceAssignment, Space
from meho_backplane.topology.identity_mode import NOT_LEGACY_SCOPE

__all__ = ["DRIFT_KINDS", "find_drift", "spaces_drift"]

#: The five kinds of drift, in the order the answer lists them.
DRIFT_KINDS: tuple[str, ...] = (
    "unassigned_targets",
    "missing_targets",
    "unreachable_targets",
    "missing_objects",
    "state_mismatch",
)

#: Topology node kinds that are machines. Other nodes (a network, say) may
#: carry a machine's id as a reference; that does not make them the machine.
_MACHINE_KINDS: tuple[str, ...] = ("vm", "host", "node")

#: Properties of a machine node that hold the vendor's own id for it.
#: Connectors name it differently (a Hetzner server keeps it in
#: ``server_number``). The node's name is a display name, not an id.
_OBJECT_ID_KEYS: tuple[str, ...] = ("object_id", "server_number", "uid", "uuid", "moid", "id")

#: Error codes of a call that did not reach the target. The dispatcher starts
#: its error text with the code (``connector_timeout: ConnectTimeout``).
_NOT_REACHED_CODES = frozenset({"connector_timeout"})

#: Exception classes of a ``connector_error`` where no connection was made.
_NOT_CONNECTED_EXCEPTIONS = frozenset(
    {
        "ConnectError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "gaierror",
        "NoValidConnectionsError",
    }
)

_REACHED_STATES = frozenset({"ok", "degraded", "critical"})

#: One piece of reach evidence: (when, reachable, where it comes from).
_Evidence = tuple[datetime | None, bool, dict[str, Any]]


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _probe_evidence(target: Target) -> _Evidence | None:
    fingerprint = target.fingerprint if isinstance(target.fingerprint, dict) else {}
    reachable = fingerprint.get("reachable")
    if not isinstance(reachable, bool):
        return None
    probed = fingerprint.get("probed_at")
    return (
        _parse_time(probed),
        reachable,
        {"source": "probe", "seen_at": probed if isinstance(probed, str) else None},
    )


def _call_did_not_reach(evidence: object) -> bool:
    """Whether an ``unknown`` result says the call failed to reach the target.

    Only a dispatch that ended in a connection failure counts. A refused call
    (policy), a missing connector or credential, a bad check and a timeout of
    the whole evaluation are failures on MEHO's side: they say nothing about
    the target.
    """
    if not isinstance(evidence, dict) or evidence.get("reason") != "dispatch_not_ok":
        return False
    if evidence.get("dispatch_status") != "error":
        return False
    text = evidence.get("dispatch_error")
    if not isinstance(text, str):
        return False
    code, _, detail = text.partition(": ")
    if code in _NOT_REACHED_CODES:
        return True
    return code == "connector_error" and detail.strip() in _NOT_CONNECTED_EXCEPTIONS


def _latest_state(sensor: Sensor) -> str | None:
    """The state of the newest result of *sensor*.

    With confirmation retries on, a new state waits in ``pending_state`` until
    it repeats, while ``last_evidence`` already holds the new result.
    """
    return sensor.pending_state or sensor.last_state


def _check_evidence(sensor: Sensor, now: datetime) -> _Evidence | None:
    """What one health check says about reach, or ``None`` when it says nothing.

    A stale result says nothing. ``ok``, ``degraded`` and ``critical`` mean the
    call reached the target. ``unknown`` counts only when the call failed to
    reach the target; ``skip`` and other ``unknown`` results say nothing.
    """
    if not check_is_fresh(sensor, now) or sensor.last_evaluated_at is None:
        return None
    state = _latest_state(sensor)
    if state in _REACHED_STATES:
        reachable = True
    elif state == "unknown" and _call_did_not_reach(sensor.last_evidence):
        reachable = False
    else:
        return None
    seen = sensor.last_evaluated_at
    seen = seen if seen.tzinfo is not None else seen.replace(tzinfo=UTC)
    return (
        seen,
        reachable,
        {"source": "health_check", "seen_at": seen.isoformat(), "check_id": str(sensor.id)},
    )


def _newest(evidence: Iterable[_Evidence | None]) -> _Evidence | None:
    """The newest evidence; evidence without a time is older than any with one."""
    known = [e for e in evidence if e is not None]
    if not known:
        return None
    return max(known, key=lambda e: (e[0] is not None, e[0] or datetime.min.replace(tzinfo=UTC)))


async def _sensors_by_target_name(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[str, list[Sensor]]:
    by_name: dict[str, list[Sensor]] = defaultdict(list)
    for sensor in await session.scalars(
        select(Sensor).where(Sensor.tenant_id == tenant_id, Sensor.target.is_not(None))
    ):
        name = sensor.target.get("name") if isinstance(sensor.target, dict) else None
        if isinstance(name, str):
            by_name[name].append(sensor)
    return by_name


def _node_ids(properties: object) -> set[str]:
    ids: set[str] = set()
    if isinstance(properties, dict):
        for key in _OBJECT_ID_KEYS:
            value = properties.get(key)
            if isinstance(value, str | int) and not isinstance(value, bool):
                ids.add(str(value))
    return ids


def _node_targets(target_id: uuid.UUID | None, source_scope: str | None) -> set[uuid.UUID]:
    owners = {target_id} if target_id is not None else set()
    if source_scope and source_scope.startswith("target:"):
        owners.add(uuid.UUID(source_scope.removeprefix("target:")))
    return owners


async def _live_object_ids(
    session: AsyncSession, tenant_id: uuid.UUID, machines: list[ResourceAssignment]
) -> set[tuple[uuid.UUID | None, str]]:
    """The vendor ids of live machine nodes, as ``(target, id)`` pairs.

    Each id also comes as ``(None, id)``: an assignment without a target
    matches any node of the tenant. Only the nodes of the assigned targets are
    read when every machine names its target.
    """
    stmt = select(GraphNode.target_id, GraphNode.source_scope, GraphNode.properties).where(
        GraphNode.tenant_id == tenant_id,
        GraphNode.kind.in_(_MACHINE_KINDS),
        GraphNode.last_seen.is_not(None),
        NOT_LEGACY_SCOPE,
    )
    scoped = {row.target_id for row in machines}
    if None not in scoped:
        ids = [t for t in scoped if t is not None]
        stmt = stmt.where(
            or_(
                GraphNode.target_id.in_(ids),
                GraphNode.source_scope.in_([f"target:{t}" for t in ids]),
            )
        )
    found: set[tuple[uuid.UUID | None, str]] = set()
    for target_id, source_scope, properties in await session.execute(stmt):
        owners: set[uuid.UUID | None] = {None, *_node_targets(target_id, source_scope)}
        found.update((owner, oid) for oid in _node_ids(properties) for owner in owners)
    return found


def _where(space: Space | None, row: ResourceAssignment) -> dict[str, Any]:
    return {
        "assignment_id": str(row.id),
        "space_id": str(row.space_id),
        "space_name": space.name if space is not None else None,
    }


def _unreachable(
    where: dict[str, Any], found: Target, sensors: list[Sensor], now: datetime
) -> dict[str, Any] | None:
    """The drift entry when the newest reach evidence says not reachable."""
    newest = _newest([_probe_evidence(found), *(_check_evidence(s, now) for s in sensors)])
    if newest is None or newest[1]:
        return None
    return {**where, "target_id": str(found.id), "name": found.name, **newest[2]}


def _check_target(
    drift: dict[str, list[dict[str, Any]]],
    where: dict[str, Any],
    row: ResourceAssignment,
    found: Target | None,
    owned: set[uuid.UUID],
    sensors: dict[str, list[Sensor]],
    now: datetime,
) -> None:
    """Add the drift of one current target assignment; reach is checked once per target."""
    if found is None:
        drift["missing_targets"].append(
            {
                **where,
                "target_id": str(row.target_id) if row.target_id else None,
                "name": row.name_at_assignment,
            }
        )
        return
    if found.name != row.name_at_assignment:
        drift["state_mismatch"].append(
            {
                **where,
                "reason": "target_renamed",
                "name": row.name_at_assignment,
                "current_name": found.name,
            }
        )
    if found.id not in owned:
        owned.add(found.id)
        entry = _unreachable(where, found, sensors.get(found.name, []), now)
        if entry is not None:
            drift["unreachable_targets"].append(entry)


async def find_drift(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime
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
    machines = [a for a in assignments if a.kind == "object" and a.object_id]
    seen_objects = await _live_object_ids(session, tenant_id, machines) if machines else set()
    sensors = await _sensors_by_target_name(session, tenant_id)

    drift: dict[str, list[dict[str, Any]]] = {kind: [] for kind in DRIFT_KINDS}
    owned: set[uuid.UUID] = set()
    for row in assignments:
        space = spaces.get(row.space_id)
        where = _where(space, row)
        if space is not None and space.state != "active":
            drift["state_mismatch"].append(
                {**where, "reason": "space_disabled", "name": row.name_at_assignment}
            )
        if row.kind == "target":
            found = targets.get(row.target_id) if row.target_id is not None else None
            _check_target(drift, where, row, found, owned, sensors, now)
        elif (
            row.kind == "object"
            and row.object_id
            and (row.target_id, row.object_id) not in seen_objects
        ):
            drift["missing_objects"].append(
                {
                    **where,
                    "object_id": row.object_id,
                    "target_id": str(row.target_id) if row.target_id else None,
                    "name": row.name_at_assignment,
                }
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
        drift = await find_drift(session, operator.tenant_id, datetime.now(UTC))
    counts = {kind: len(drift[kind]) for kind in DRIFT_KINDS}
    return {
        **drift,
        "counts": counts,
        "total_count": sum(counts.values()),
    }
