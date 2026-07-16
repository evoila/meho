# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Assignment validation + materialisation for the gateway checks API (#2499).

Two concerns, sharing the same per-item resolution but differing in how
they treat a failure:

* **PUT-time validation** (:func:`store_assignment`) — every authored item
  must resolve to a live target and an *enabled*, ``safety_level == 'safe'``
  endpoint descriptor. The first failure raises a typed
  :mod:`meho_backplane.gateway.errors` exception (the route maps it to a
  structured 422) and **nothing is stored** — an assignment PUT is a
  full-document replace, so a partial store is never acceptable.

* **GET-time materialisation** (:func:`materialize_assignment`) — each
  stored authored item is materialised into a wire
  :class:`~meho_backplane.runner.wire.RunnerWorkItem` against the **live**
  target row and op descriptor, so target-row drift (a rotated
  ``tls_ca_pin``, a changed ``host``) is picked up on the next poll. An
  item whose target was soft-deleted, whose connector no longer resolves,
  or whose op is no longer a runnable safe descriptor is **dropped** with a
  structured warning (the digest changes, so the runner self-heals) rather
  than failing the whole fetch.

The ``assignment_version`` is a sha256 over the canonical JSON of the fully
materialised work items — not a stored counter — so "unchanged" means the
materialisation is byte-identical, catching live target-row drift a counter
would miss.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.resolver import resolve_connector_or_label
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.gateway import repository
from meho_backplane.gateway.errors import (
    AssignmentOpNotSafeError,
    AssignmentOpUnknownError,
    AssignmentTargetUnknownError,
)
from meho_backplane.gateway.schemas import AssignmentDocument, AuthoredCheckItem
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.runner import wire
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

__all__ = [
    "compute_assignment_version",
    "descriptor_from_target",
    "materialize_assignment",
    "store_assignment",
]

_log = structlog.get_logger(__name__)

#: v1 authorizes read-only workloads only.
_SAFE_LEVEL = "safe"


def descriptor_from_target(target: TargetORM) -> wire.ResolvedTargetDescriptor:
    """Project a live ``Target`` row into the wire descriptor the runner reads.

    Carries the resolver inputs + connection-routing fields a connector
    handler duck-reads; ``secret_ref`` is the reference only, never a
    credential value.
    """
    return wire.ResolvedTargetDescriptor(
        id=target.id,
        tenant_id=target.tenant_id,
        name=target.name,
        aliases=tuple(target.aliases or ()),
        product=target.product,
        version=target.version,
        fingerprint=dict(target.fingerprint) if target.fingerprint is not None else None,
        preferred_impl_id=target.preferred_impl_id,
        host=target.host,
        port=target.port,
        fqdn=target.fqdn,
        secret_ref=target.secret_ref,
        auth_model=target.auth_model,
        verify_tls=target.verify_tls,
        tls_ca_pin=target.tls_ca_pin,
        tls_server_name=target.tls_server_name,
        extras=dict(target.extras or {}),
    )


def compute_assignment_version(items: list[wire.RunnerWorkItem]) -> str:
    """Return the sha256 hex digest over the canonical JSON of *items*.

    ``sort_keys`` canonicalises dict key order; the item order is the
    stable authored order. 64-char lowercase hex — an opaque cache key on
    the runner, digest semantics owned here.
    """
    payload = [item.model_dump(mode="json") for item in items]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolved_connector(target: TargetORM) -> type[Connector] | None:
    """Return the target's resolved connector class, or ``None`` on a miss."""
    cls, label, _message = resolve_connector_or_label(target)
    if label is not None:
        return None
    # resolver contract: label is None ⇔ cls is set.
    assert cls is not None
    return cls


async def store_assignment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    runner_name: str,
    document: AssignmentDocument,
) -> None:
    """Validate every authored item, then replace the runner's document.

    Raises a typed :mod:`meho_backplane.gateway.errors` exception on the
    first invalid item — before any write — so a rejected PUT stores
    nothing. Does not commit; the caller owns the transaction.
    """
    for item in document.items:
        await _validate_authored_item(session, tenant_id=tenant_id, item=item)
    items_json: list[dict[str, Any]] = [item.model_dump(mode="json") for item in document.items]
    await repository.upsert_assignment_row(
        session, tenant_id=tenant_id, runner_name=runner_name, items=items_json
    )


async def _validate_authored_item(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    item: AuthoredCheckItem,
) -> None:
    """Raise the appropriate typed error when an authored item is unauthorable."""
    try:
        target = await resolve_target(session, tenant_id, item.target_name)
    except (TargetNotFoundError, AmbiguousTargetError) as exc:
        raise AssignmentTargetUnknownError(
            check_ref=item.check_ref, target_name=item.target_name
        ) from exc

    cls = _resolved_connector(target)
    if cls is None:
        raise AssignmentOpUnknownError(
            check_ref=item.check_ref,
            op=item.op,
            reason=f"target {item.target_name!r} resolves no connector",
        )

    descriptor = await lookup_descriptor(
        tenant_id=tenant_id,
        product=cls.product,
        version=cls.version,
        impl_id=cls.impl_id,
        op_id=item.op,
    )
    if descriptor is None:
        raise AssignmentOpUnknownError(
            check_ref=item.check_ref,
            op=item.op,
            reason=f"no enabled descriptor for connector "
            f"({cls.product}, {cls.version}, {cls.impl_id})",
        )
    if descriptor.safety_level != _SAFE_LEVEL:
        raise AssignmentOpNotSafeError(
            check_ref=item.check_ref, op=item.op, safety_level=descriptor.safety_level
        )


async def materialize_assignment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    runner_name: str,
    principal: wire.RunnerPrincipal,
) -> wire.RunnerAssignment:
    """Materialise the runner's stored document into the versioned wire shape.

    Resolves each authored item against the live target + op descriptor;
    drops (with a warning) any item that no longer resolves to a runnable
    safe op so the runner self-heals on the next poll. The returned
    ``assignment_version`` is the content digest over the surviving items.
    """
    row = await repository.get_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
    raw_items: list[dict[str, Any]] = list(row.items) if row is not None else []

    work_items: list[wire.RunnerWorkItem] = []
    for raw in raw_items:
        authored = AuthoredCheckItem.model_validate(raw)
        work_item = await _materialize_item(
            session, tenant_id=tenant_id, authored=authored, principal=principal
        )
        if work_item is not None:
            work_items.append(work_item)

    return wire.RunnerAssignment(
        assignment_version=compute_assignment_version(work_items),
        items=work_items,
    )


async def _materialize_item(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    authored: AuthoredCheckItem,
    principal: wire.RunnerPrincipal,
) -> wire.RunnerWorkItem | None:
    """Materialise one authored item, or ``None`` (logged) if it no longer resolves."""
    try:
        target = await resolve_target(session, tenant_id, authored.target_name)
    except (TargetNotFoundError, AmbiguousTargetError):
        _log.warning(
            "assignment_item_dropped",
            reason="target_unresolved",
            check_ref=authored.check_ref,
            target_name=authored.target_name,
        )
        return None

    cls = _resolved_connector(target)
    if cls is None:
        _log.warning(
            "assignment_item_dropped",
            reason="connector_unresolved",
            check_ref=authored.check_ref,
            target_name=authored.target_name,
        )
        return None

    descriptor = await lookup_descriptor(
        tenant_id=tenant_id,
        product=cls.product,
        version=cls.version,
        impl_id=cls.impl_id,
        op_id=authored.op,
    )
    if not _is_runnable_safe(descriptor):
        _log.warning(
            "assignment_item_dropped",
            reason="op_unresolved_or_unsafe",
            check_ref=authored.check_ref,
            op=authored.op,
        )
        return None
    assert descriptor is not None and descriptor.handler_ref is not None

    return wire.RunnerWorkItem(
        check_ref=authored.check_ref,
        op_id=authored.op,
        product=cls.product,
        version=cls.version,
        impl_id=cls.impl_id,
        handler_ref=descriptor.handler_ref,
        params=dict(authored.params),
        safety_level=descriptor.safety_level,
        principal=principal,
        target_descriptor=descriptor_from_target(target),
    )


def _is_runnable_safe(descriptor: EndpointDescriptor | None) -> bool:
    """True when *descriptor* is an enabled, safe op the runner can execute.

    Requires a non-empty ``handler_ref``: the runner executes an op by
    importing that dotted handler, so a descriptor without one (an
    ingested/generic op) is not remotely runnable and is dropped.
    """
    return (
        descriptor is not None
        and descriptor.safety_level == _SAFE_LEVEL
        and bool(descriptor.handler_ref)
    )
