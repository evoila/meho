# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector DELETE engine — row removal + auto-shim deregistration.

G0.25-T2 (#1700). The ingest pipeline auto-registers a
:class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`
shim on the first ingest of a ``(product, version, impl_id)`` triple
(`register_ingested.py`), and an aborted ingest — a spec that parses
to zero operations, a multi-spec batch that fails mid-way — leaves
that shim registered forever with no surface to remove it. This
module is the engine behind the two removal surfaces
(``DELETE /api/v1/connectors/{connector_id}`` and the
``meho.connector.delete`` MCP tool, both delegating through
:meth:`~meho_backplane.operations.ingest.service.ReviewService.delete_connector`).

Mechanism note (recorded on #1700): the task body sketched
``review_status='deleted'`` soft-deletion, but
``ck_operation_group_review_status`` (migration ``0005``) pins the
column to ``('staged', 'enabled', 'disabled')`` and the wave assigns
Alembic migrations to the sibling backfill task (#1701). The shipped
mechanism is therefore **row removal**: the scoped
``endpoint_descriptor`` + ``operation_group`` rows are deleted in one
transaction (DML only, no schema change; the single inbound FK is
``endpoint_descriptor.group_id → operation_group.id ON DELETE SET
NULL``), one ``meho.connector.delete`` audit row preserves the
forensic trail, and re-ingest revives the connector from scratch —
the exact revival path the task scoped in ("once deleted, the
operator re-ingests to bring the connector back").

Registry policy
---------------

Only :class:`GenericRestConnector` subclasses (the ``AutoShim_*``
classes the ingest pipeline synthesises) are ever deregistered, and
only when **no** rows remain for the triple under *any* tenant scope
— the v2 registry is process-global, so popping a class while
another tenant still has rows would break that tenant's dispatch
resolution. Hand-coded classes (``VaultConnector``,
``VmwareRestConnector``, …) are never deregistered: they re-register
at every process start, so removal here would only manufacture a
restart-inconsistent half-state. A connector whose hand-coded class
survives the row delete simply reverts to the truthful
``state="registered"`` listing row ("known class, awaiting ingest").

The registry walk matches by the *parsed* natural key (the same
round-trip ``list_connectors`` uses), so the VCF-family long↔short
product splits resolve correctly: rows persist under the
dispatch-canonical product (``vrli``) while the shim may be
registered under the supplied product (``vcf-logs``), and both spell
the same ``connector_id``.

Transaction shape
-----------------

:func:`stage_connector_delete` runs inside the caller's session and
performs the row deletes + decides the shim keys; the caller
(:class:`ReviewService`) writes the audit row, commits, and only then
applies :func:`deregister_staged_auto_shims`. Ordering is deliberate:
a failed commit must leave the process-local registry untouched, and
the pop itself is idempotent (a replay against a freshly-restarted
pod, where no shim was ever re-registered, is a logged no-op).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import CursorResult, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import all_connectors_v2, deregister_connector_v2
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest._internals import ConnectorScope
from meho_backplane.operations.ingest._llm_grouping_internals import build_connector_id
from meho_backplane.operations.ingest.connector_registration import GenericRestConnector
from meho_backplane.operations.ingest.exceptions import ConnectorNotFoundError
from meho_backplane.operations.ingest.parser import parse_connector_id

__all__ = [
    "DeleteConnectorResult",
    "DeleteConnectorWarning",
    "StagedConnectorDelete",
    "deregister_staged_auto_shims",
    "stage_connector_delete",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeleteConnectorWarning:
    """One advisory attached to a completed connector delete.

    Mirrors the :class:`~meho_backplane.operations.ingest.api_schemas.EditOpWarning`
    discipline (G0.23-T4 #1630): the delete **was applied**; the
    warning tells the operator it had a sharp edge. The only producer
    today is the enabled-operations probe — the connector still had
    dispatchable ops when it was deleted. The REST route stays
    ``204 No Content`` per the task contract, so the structured wire
    home for this advisory is the MCP tool response; the REST path
    surfaces it via the ``connector_delete_enabled_ops`` log event
    and the audit payload's ``enabled_operations_deleted`` count.
    """

    code: str
    enabled_op_count: int
    message: str


@dataclass(frozen=True, slots=True)
class DeleteConnectorResult:
    """Outcome of one connector delete, as returned by the service layer.

    ``registry_only`` distinguishes the zero-op-stub path (no DB rows
    existed anywhere; only the auto-shim class was removed — the
    primary #1700 consumer scenario) from the row-bearing path.
    ``class_deregistered`` reports whether the delete removed the
    triple's auto-shim from the process-local v2 registry; it stays
    ``False`` when rows survive under another tenant scope or when
    the resolved class is hand-coded.
    """

    connector_id: str
    groups_deleted: int
    operations_deleted: int
    enabled_operations_deleted: int
    class_deregistered: bool
    registry_only: bool
    warnings: tuple[DeleteConnectorWarning, ...]


@dataclass(frozen=True, slots=True)
class StagedConnectorDelete:
    """A delete staged inside an open session, awaiting commit.

    ``shim_keys`` carries the v2-registry keys (registry spelling,
    not the parsed one) to pop **after** the caller's commit
    succeeds; ``audit_payload`` is the JSON-safe dict for the
    ``meho.connector.delete`` audit row the caller writes into the
    same transaction.
    """

    result: DeleteConnectorResult
    audit_payload: dict[str, Any]
    shim_keys: tuple[tuple[str, str, str], ...]


def _auto_shim_keys_for_triple(
    product: str,
    version: str,
    impl_id: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return v2-registry keys whose auto-shim resolves to the parsed triple.

    Walks :func:`all_connectors_v2` and keeps the keys that (a) are
    not v1-compat padding rows (empty ``version`` / ``impl_id``),
    (b) round-trip through :func:`parse_connector_id` to exactly
    *(product, version, impl_id)* — the same lossless-round-trip rule
    ``list_connectors._resolve_class_only_natural_key`` applies, which
    is what makes the VCF long↔short product split match — and
    (c) hold a :class:`GenericRestConnector` subclass. Hand-coded
    classes are intentionally excluded; see the module docstring's
    registry policy.
    """
    keys: list[tuple[str, str, str]] = []
    for (reg_product, reg_version, reg_impl_id), cls in sorted(all_connectors_v2().items()):
        if not reg_version or not reg_impl_id:
            continue
        connector_id = build_connector_id(reg_product, reg_version, reg_impl_id)
        if parse_connector_id(connector_id) != (product, version, impl_id):
            continue
        if not issubclass(cls, GenericRestConnector):
            continue
        keys.append((reg_product, reg_version, reg_impl_id))
    return tuple(keys)


async def _rows_exist_any_scope(
    session: AsyncSession,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return whether any row exists for the triple under **any** tenant scope.

    Internal-only, deliberately unscoped — the decision it feeds
    ("may the process-global auto-shim be deregistered?") is about
    the registry, which has no tenant axis. The probe never widens
    what the caller's *response* exposes: the 204/404 outcome is
    fully determined by the caller-scoped rows before this runs, so
    no cross-tenant information leaks through the wire contract.
    Two ``LIMIT 1`` probes, descriptors first — same shape as
    :func:`~meho_backplane.operations._lookup.connector_exists`.
    """
    descriptor_hit = await session.execute(
        select(EndpointDescriptor.id)
        .where(
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
        )
        .limit(1)
    )
    if descriptor_hit.first() is not None:
        return True
    group_hit = await session.execute(
        select(OperationGroup.id)
        .where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
        )
        .limit(1)
    )
    return group_hit.first() is not None


async def _scoped_groups(
    session: AsyncSession,
    scope: ConnectorScope,
) -> list[OperationGroup]:
    """Return the scope's group rows sorted by ``group_key`` (empty list OK).

    Unlike :func:`~meho_backplane.operations.ingest._internals.load_groups`
    this does **not** raise on an empty result — the zero-op-stub
    path (no rows anywhere, only a registered shim) is a legitimate
    delete target here, and the not-found decision needs more
    context than "no groups" (stray ungrouped descriptors must be
    found too).
    """
    stmt = select(OperationGroup).where(
        OperationGroup.product == scope.product,
        OperationGroup.version == scope.version,
        OperationGroup.impl_id == scope.impl_id,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        stmt = stmt.where(OperationGroup.tenant_id == scope.tenant_id)
    result = await session.execute(stmt.order_by(OperationGroup.group_key))
    return list(result.scalars().all())


async def _scoped_descriptor_counts(
    session: AsyncSession,
    scope: ConnectorScope,
) -> tuple[int, int]:
    """Return ``(total, enabled)`` descriptor counts for the scope.

    Counts by the natural-key triple + tenant scope rather than by
    ``group_id`` so descriptors the T2 upsert landed but the T3
    grouping pass never assigned (``group_id IS NULL`` — the
    mid-pipeline-abort shape this task exists for) are counted and
    later deleted too. Portable ``CASE WHEN`` sum, same dialect
    rationale as ``list_connectors._operation_count_by_connector``.
    """
    enabled_case = func.sum(
        case((EndpointDescriptor.is_enabled.is_(True), 1), else_=0),
    )
    stmt = select(func.count(EndpointDescriptor.id), enabled_case).where(
        EndpointDescriptor.product == scope.product,
        EndpointDescriptor.version == scope.version,
        EndpointDescriptor.impl_id == scope.impl_id,
    )
    if scope.tenant_id is None:
        stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        stmt = stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
    total_raw, enabled_raw = (await session.execute(stmt)).one()
    return int(total_raw or 0), int(enabled_raw or 0)


async def _delete_scoped_rows(
    session: AsyncSession,
    scope: ConnectorScope,
) -> tuple[int, int]:
    """Bulk-delete the scope's descriptor + group rows; return rowcounts.

    Descriptors first: their ``group_id`` FK is ``ON DELETE SET
    NULL`` so the reverse order would also be referentially safe,
    but deleting children first avoids a pointless wave of NULL
    re-writes. Both statements filter by the natural-key triple +
    tenant scope (never by ``group_id``), so ungrouped stray
    descriptors are removed with the rest.
    """
    descriptor_stmt = delete(EndpointDescriptor).where(
        EndpointDescriptor.product == scope.product,
        EndpointDescriptor.version == scope.version,
        EndpointDescriptor.impl_id == scope.impl_id,
    )
    group_stmt = delete(OperationGroup).where(
        OperationGroup.product == scope.product,
        OperationGroup.version == scope.version,
        OperationGroup.impl_id == scope.impl_id,
    )
    if scope.tenant_id is None:
        descriptor_stmt = descriptor_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        descriptor_stmt = descriptor_stmt.where(EndpointDescriptor.tenant_id == scope.tenant_id)
        group_stmt = group_stmt.where(OperationGroup.tenant_id == scope.tenant_id)
    # Cast rationale matches _internals.cascade_is_enabled: DML
    # statements produce a CursorResult whose rowcount mypy cannot
    # see through AsyncSession.execute's Result annotation.
    descriptor_result: CursorResult[Any] = await session.execute(descriptor_stmt)  # type: ignore[assignment]
    group_result: CursorResult[Any] = await session.execute(group_stmt)  # type: ignore[assignment]
    return (
        max(descriptor_result.rowcount or 0, 0),
        max(group_result.rowcount or 0, 0),
    )


def _build_warnings(
    connector_id: str,
    enabled_count: int,
) -> tuple[DeleteConnectorWarning, ...]:
    """Return the enabled-operations advisory when *enabled_count* > 0."""
    if enabled_count <= 0:
        return ()
    message = (
        f"connector {connector_id!r} still had {enabled_count} enabled "
        f"operation(s) at delete time; the delete completed and those "
        f"operations are no longer dispatchable. Re-ingest the spec and "
        f"re-enable the connector to restore them."
    )
    return (
        DeleteConnectorWarning(
            code="enabled_operations_deleted",
            enabled_op_count=enabled_count,
            message=message,
        ),
    )


def _registry_only_stage(
    scope: ConnectorScope,
    connector_id: str,
) -> StagedConnectorDelete:
    """Stage the zero-op-stub delete (no rows anywhere; pop the shim only).

    Raises :class:`ConnectorNotFoundError` when no auto-shim matches
    the triple either — nothing exists to delete, and the unified
    404 keeps the probe surface identical to every other connector
    route. A registered *hand-coded* class without rows also 404s
    here: its registration is a deploy artefact, not deletable
    operator state.
    """
    shim_keys = _auto_shim_keys_for_triple(scope.product, scope.version, scope.impl_id)
    if not shim_keys:
        raise ConnectorNotFoundError(connector_id=connector_id, tenant_id=scope.tenant_id)
    result = DeleteConnectorResult(
        connector_id=connector_id,
        groups_deleted=0,
        operations_deleted=0,
        enabled_operations_deleted=0,
        class_deregistered=True,
        registry_only=True,
        warnings=(),
    )
    return StagedConnectorDelete(
        result=result,
        audit_payload=_audit_payload(scope, result, deleted_group_keys=[]),
        shim_keys=shim_keys,
    )


def _audit_payload(
    scope: ConnectorScope,
    result: DeleteConnectorResult,
    *,
    deleted_group_keys: list[str],
) -> dict[str, Any]:
    """Build the JSON-safe ``meho.connector.delete`` audit payload."""
    return {
        "connector_id": result.connector_id,
        "tenant_scope": str(scope.tenant_id) if scope.tenant_id is not None else None,
        "deleted_group_keys": sorted(deleted_group_keys),
        "groups_deleted": result.groups_deleted,
        "operations_deleted": result.operations_deleted,
        "enabled_operations_deleted": result.enabled_operations_deleted,
        "class_deregistered": result.class_deregistered,
        "registry_only": result.registry_only,
    }


async def stage_connector_delete(
    session: AsyncSession,
    scope: ConnectorScope,
    connector_id: str,
) -> StagedConnectorDelete:
    """Delete the scope's rows in *session* and stage the registry follow-up.

    Two paths:

    * **Zero rows under the caller's scope.** If rows exist for the
      triple under *another* scope, raise
      :class:`ConnectorNotFoundError` — the caller cannot delete rows
      their scope cannot see (the same 404 conflation every connector
      route uses; a tenant-scoped REST call must not be able to nuke
      a built-in connector, and vice versa). If no rows exist
      anywhere, this is the zero-op stub: stage the auto-shim pop, or
      404 when no auto-shim matches either.
    * **Rows present.** Count + bulk-delete the scoped descriptor and
      group rows, then re-probe the triple unscoped *inside the same
      transaction* (the session sees its own deletes): only when
      nothing remains anywhere is the auto-shim staged for
      deregistration.

    The caller writes the audit row from ``audit_payload``, commits,
    and then applies :func:`deregister_staged_auto_shims`.
    """
    groups = await _scoped_groups(session, scope)
    op_total, op_enabled = await _scoped_descriptor_counts(session, scope)

    if not groups and op_total == 0:
        if await _rows_exist_any_scope(session, scope.product, scope.version, scope.impl_id):
            raise ConnectorNotFoundError(connector_id=connector_id, tenant_id=scope.tenant_id)
        return _registry_only_stage(scope, connector_id)

    group_keys = [group.group_key for group in groups]
    ops_deleted, groups_deleted = await _delete_scoped_rows(session, scope)
    residual = await _rows_exist_any_scope(session, scope.product, scope.version, scope.impl_id)
    shim_keys: tuple[tuple[str, str, str], ...] = ()
    if not residual:
        shim_keys = _auto_shim_keys_for_triple(scope.product, scope.version, scope.impl_id)

    result = DeleteConnectorResult(
        connector_id=connector_id,
        groups_deleted=groups_deleted,
        operations_deleted=ops_deleted,
        enabled_operations_deleted=op_enabled,
        class_deregistered=bool(shim_keys),
        registry_only=False,
        warnings=_build_warnings(connector_id, op_enabled),
    )
    return StagedConnectorDelete(
        result=result,
        audit_payload=_audit_payload(scope, result, deleted_group_keys=group_keys),
        shim_keys=shim_keys,
    )


def deregister_staged_auto_shims(
    staged: StagedConnectorDelete,
) -> None:
    """Pop every staged auto-shim key from the v2 registry (post-commit).

    Process-local and idempotent — a key already absent (another
    worker raced, or the pod restarted between commit and pop on a
    retry) logs ``connector_deregister_v2_missed`` inside
    :func:`deregister_connector_v2` and moves on. Runs strictly after
    the row-delete transaction commits so a failed commit never
    leaves the registry ahead of the database.
    """
    for product, version, impl_id in staged.shim_keys:
        deregister_connector_v2(product=product, version=version, impl_id=impl_id)
