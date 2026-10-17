# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stage the scope repair: ask every target again, write only a manifest (#3749).

:func:`stage_scope_repair` calls ``discover_topology`` on every live
target of every tenant, the same call a refresh makes, but it never
reconciles the answer into ``graph_node`` / ``graph_edge``. It then reads
the graph, builds the plan with
:func:`~meho_backplane.topology.scope_repair.plan.build_plan` and saves
it as one ``topology_scope_manifest`` row with the graph's
``snapshot_version``. Nothing in the graph changes.

The whole run is one transaction that holds the identity-mode row
(``FOR UPDATE``) and an exclusive lock on the graph tables. Graph
writes pause until the manifest is saved: a refresh that starts while
a target is being asked cannot add a row the plan would then set aside
as unreported. The snapshot the plan is built on is the graph the
targets were asked against.

A target that fails to answer is recorded with the exception class name
only (never the message, which may carry connection details). Such a
manifest is ``incomplete`` and the cut-over refuses it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.topology.identity_mode import lock_identity_mode
from meho_backplane.topology.refresh import _invoke_discover_topology
from meho_backplane.topology.scope_repair.plan import (
    TargetDiscovery,
    build_plan,
    snapshot_version,
)
from meho_backplane.topology.scope_repair.store import (
    StoredManifest,
    load_graph,
    lock_graph_tables,
    save_manifest,
)

__all__ = ["SCOPE_REPAIR_SUB", "AlreadyScopedError", "stage_scope_repair"]

_log = structlog.get_logger(__name__)

#: ``operator.sub`` the repair discovers and writes audit rows as.
SCOPE_REPAIR_SUB = "system:topology-scope-repair"


class AlreadyScopedError(RuntimeError):
    """The cut-over has already run; there is nothing to stage."""


def system_operator(tenant_id: uuid.UUID) -> Operator:
    """The per-tenant identity the repair runs discovery as.

    Same shape as the topology scheduler's operator: no token is
    forwarded; the connector uses the target's own stored credentials.
    """
    return Operator(
        sub=SCOPE_REPAIR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _discover_one(target: Any) -> TargetDiscovery:
    """Ask one target for its topology; record a failure instead of raising."""
    observed_at = datetime.now(UTC)
    base = {
        "tenant_id": target.tenant_id,
        "target_id": target.id,
        "discovered_by": getattr(target, "product", "unknown"),
        "observed_at": observed_at,
    }
    try:
        connector = get_or_create_connector_instance(resolve_connector(target))
        hints = await _invoke_discover_topology(
            connector, target, system_operator(target.tenant_id)
        )
    except Exception as exc:
        _log.warning(
            "topology_scope_repair_target_unavailable",
            target_id=str(target.id),
            tenant_id=str(target.tenant_id),
            error_class=type(exc).__name__,
        )
        return TargetDiscovery(**base, hints=None, failure=type(exc).__name__)
    return TargetDiscovery(**base, hints=hints)


async def stage_scope_repair(*, created_by: str = SCOPE_REPAIR_SUB) -> StoredManifest:
    """Discover every live target, plan the cut-over, save the manifest.

    Graph writes wait while this runs (see the module docstring).

    Raises:
        AlreadyScopedError: the graph is already in ``scoped`` mode.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        if await lock_identity_mode(session) == "scoped":
            raise AlreadyScopedError("the topology scope cut-over has already run")
        await lock_graph_tables(session)
        targets = list(
            (
                await session.execute(
                    sa.select(Target)
                    .where(Target.deleted_at.is_(None))
                    .order_by(Target.tenant_id, Target.id)
                )
            )
            .scalars()
            .all()
        )
        discoveries = [await _discover_one(t) for t in targets]
        nodes, edges = await load_graph(session)
        plan = build_plan(nodes, edges, discoveries)
        stored = await save_manifest(
            session,
            plan=plan,
            snapshot=snapshot_version(nodes, edges),
            created_by=created_by,
        )
    _log.info(
        "topology_scope_repair_staged",
        manifest_id=str(stored.id),
        status=stored.status,
        targets=len(plan.targets),
        nodes=len(plan.nodes),
        edges=len(plan.edges),
        problems=len(plan.problems),
    )
    return stored
