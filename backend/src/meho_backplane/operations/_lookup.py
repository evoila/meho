# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector-id parsing + ``endpoint_descriptor`` lookup helpers.

The G0.6 dispatcher (T5, #396) takes a ``connector_id`` string of the
form ``"<impl_id>-<version>"`` and resolves it into the
``(product, version, impl_id)`` natural-key triple the
``endpoint_descriptor`` table is keyed on. This module owns:

* :func:`parse_connector_id` -- the parser. See its docstring for the
  encoding contract and the v1-style backward-compatible fallback.
* :func:`lookup_descriptor` -- tenant-scoped-then-global descriptor
  lookup. Returns ``None`` if no enabled descriptor matches.
* :func:`count_known_ops` -- count of enabled descriptors for a given
  ``(product, version, impl_id)``. Returned in the ``unknown_op`` error
  payload so the operator has a "did you mean…" signal without the
  full enumeration the meta-tools (T8) provide.

The split lets the dispatcher's :func:`dispatch` body keep step 2's
descriptor resolution one helper call instead of three.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup

__all__ = [
    "connector_exists",
    "count_known_ops",
    "lookup_descriptor",
    "parse_connector_id",
]


# Pattern for connector ids of the form ``"<head>-<version>"`` where
# ``head`` may itself contain hyphens (``"vmware-rest"`` etc.) and
# ``version`` is the tail segment. The version segment is pinned to
# ``[0-9][A-Za-z0-9._]*`` so it always starts with a digit -- this
# avoids ambiguous splits like ``"foo-bar"`` (no version) and keeps
# ``"vault-1.x"`` parsing into ``head="vault"`` / ``version="1.x"``.
_CONNECTOR_ID_TAIL_VERSION = re.compile(r"^(?P<head>.+)-(?P<version>[0-9][A-Za-z0-9._]*)$")


def parse_connector_id(connector_id: str) -> tuple[str, str, str]:
    """Split ``connector_id`` into ``(product, version, impl_id)``.

    The connector_id convention from the parent Initiative (#388):

    * ``"vmware-rest-9.0"`` -> product=``"vmware"``, version=``"9.0"``,
      impl_id=``"vmware-rest"``.
    * ``"vault"`` (v1-style, single-key product) -> product=``"vault"``,
      version=``""``, impl_id=``""``. Backward-compatible with the
      shipped v1 registrations.
    * ``"k8s-1.x"`` -> product=``"k8s"``, version=``"1.x"``,
      impl_id=``"k8s"``.

    The parser is forgiving: an unparseable id falls back to
    ``(connector_id, "", "")`` so the lookup just misses cleanly with
    ``unknown_op`` rather than throwing on input that the operator
    typo-ed. The natural-key index on ``endpoint_descriptor`` is what
    actually catches "no such connector" -- the parser is just a
    canonicaliser.
    """
    match = _CONNECTOR_ID_TAIL_VERSION.match(connector_id)
    if match is None:
        # No version suffix -- treat as v1-style single-product slug.
        return connector_id, "", ""
    head = match.group("head")
    version = match.group("version")
    # ``head`` is the full impl_id including a possible product prefix
    # (``"vmware-rest"``). The product is the first hyphen segment of
    # the impl_id (``"vmware"``); the rest carries the impl
    # discriminator. Single-segment heads (``"vault-1.x"``) produce
    # impl_id == product, matching how typed registrations encode the
    # single-impl case.
    product = head.split("-", 1)[0] if "-" in head else head
    return product, version, head


async def lookup_descriptor(
    *,
    tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
) -> EndpointDescriptor | None:
    """Look up an :class:`EndpointDescriptor` for *(product, version, impl_id, op_id)*.

    Tenant scoping: tenant-scoped composites (``tenant_id == <operator
    tenant>``) win when present; built-in / global rows (``tenant_id IS
    NULL``) are the fallback. Two SELECTs rather than a single ``ORDER
    BY tenant_id NULLS LAST`` -- the partial unique indexes (migration
    ``0005``) only catch duplicates within each bucket, so the
    application-layer ordering preserves the "tenant-scoped wins over
    built-in" semantics regardless of which direction PG decides NULLS
    sort in.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Tenant-scoped first.
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id == tenant_id,
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == op_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        tenant_row = result.scalar_one_or_none()
        if tenant_row is not None:
            return tenant_row
        # Built-in / global fallback.
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id == op_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        return result.scalar_one_or_none()


async def count_known_ops(
    *,
    product: str,
    version: str,
    impl_id: str,
) -> int:
    """Count enabled descriptors for *(product, version, impl_id)*.

    Returned in the ``unknown_op`` error's ``extras`` so the caller has
    a "did you mean…" signal without enumerating every op id (the
    actual enumeration belongs to the ``list_operation_groups`` /
    ``search_operations`` meta-tools shipped in T8 #399).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor.id).where(
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.is_enabled.is_(True),
            )
        )
        return len(result.all())


async def connector_exists(
    *,
    tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
) -> bool:
    """Return whether *caller-visible* operations data exists for *(product, version, impl_id)*.

    "Exists" is deliberately decoupled from ``is_enabled`` /
    ``review_status``: a connector that has registered descriptors or
    groups but has none *enabled* yet is still a *known* connector. The
    meta-tools use this to tell "unknown connector_id" (no rows at all
    for the triple — surface a 404 so a malformed/mis-shaped id fails
    loud) apart from "known connector, zero enabled groups" (rows exist
    but none enabled — a meaningful empty list, ``200 []``).

    Existence is scoped to what the calling operator can see: built-in /
    global rows (``tenant_id IS NULL``) plus this tenant's own rows
    (``tenant_id == tenant_id``). This mirrors the tenant boundary the
    data-returning queries enforce (``list_operation_groups`` /
    ``search_operations`` in ``meta_tools``). Without it the existence
    probe would be a cross-tenant presence oracle — a connector private
    to tenant B would make the gate return ``True`` for a tenant-A
    caller, yielding ``200 []`` where the caller-visible answer is
    "unknown" and the correct response is ``404``.

    The DB is the source of truth rather than the in-memory connector
    registry: every registered connector (typed, v1-compat, and
    ingested generic) writes ``endpoint_descriptor`` / ``operation_group``
    rows, and the registry is process-local while the rows are durable.
    Two cheap ``LIMIT 1`` existence probes — descriptors first (the
    common case), groups as the fallback for a connector whose groups
    were seeded ahead of its operations.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptor_hit = await session.execute(
            select(EndpointDescriptor.id)
            .where(
                (EndpointDescriptor.tenant_id.is_(None))
                | (EndpointDescriptor.tenant_id == tenant_id),
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
                (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == tenant_id),
                OperationGroup.product == product,
                OperationGroup.version == version,
                OperationGroup.impl_id == impl_id,
            )
            .limit(1)
        )
        return group_hit.first() is not None
