# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Versioned wire models for the satellite-runner poll/report protocol.

One codebase, one schema. These models are the single source of truth
for both sides of the runner wire:

* the **runner** (this package) serialises results and deserialises
  assignments against them, and
* the central **ingest + assignment API** (#2499) MUST import these same
  classes for its serialiser rather than declaring a parallel twin.

#2499 is expected to **widen** these models in place — most notably
:class:`ResolvedTargetDescriptor`, which is seeded here with the
resolver-read attributes a handler duck-reads and grows the
connection-routing set (host/port/secret_ref/TLS) on the central side.
Widening one class both sides import keeps the two ends of the wire in
lockstep by construction; forking a second copy would let the shapes
drift silently. Do not fork.

``SCHEMA_VERSION`` is an explicit protocol marker so a future
incompatible change is a visible bump rather than an implicit break.
The per-assignment :attr:`RunnerAssignment.assignment_version` is a
separate concern: an **opaque content digest** whose contract (a sha256
over the canonical materialised payload) is owned by #2499. The runner
treats it purely as a cache key — it never parses it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.operator import PrincipalKind, TenantRole

__all__ = [
    "SCHEMA_VERSION",
    "ResolvedTargetDescriptor",
    "RunnerAssignment",
    "RunnerPrincipal",
    "RunnerResult",
    "RunnerResultBatch",
    "RunnerWorkItem",
]

#: Wire protocol version. Bump on any incompatible change to the models
#: below so a runner talking to a mismatched central fails loudly rather
#: than misinterpreting fields.
SCHEMA_VERSION = "1"


class ResolvedTargetDescriptor(BaseModel):
    """Centrally-resolved target attributes a connector handler reads.

    The runner has no local target table, so the central resolver
    (:mod:`meho_backplane.connectors.resolver`, DB-bound) materialises
    the fields a handler duck-reads off a ``Target`` row and ships them
    on the assignment. The v1 seed carries exactly the resolver-read
    attributes (product / fingerprint / version / preferred_impl_id) plus
    the human-facing ``name`` and the ``extras`` escape hatch.

    #2499 widens this class with the connection-routing set
    (host, port, secret_ref, TLS flags) moulded on the central
    ``TargetSummary``. The runner tolerates the wider shape because a
    handler only reads the attributes it needs.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    product: str
    version: str | None = None
    fingerprint: dict[str, Any] | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    preferred_impl_id: str | None = None


class RunnerPrincipal(BaseModel):
    """Principal context an executed op runs under, carried on each item.

    Enough to reconstruct an :class:`~meho_backplane.auth.operator.Operator`
    on the runner without a JWT: the runner never sees a bearer token for
    the acting principal (the op was already authorized centrally), so the
    reconstructed operator's ``raw_jwt`` is empty.
    """

    model_config = ConfigDict(frozen=True)

    sub: str
    tenant_id: UUID
    tenant_role: TenantRole
    principal_kind: PrincipalKind = PrincipalKind.SERVICE


class RunnerWorkItem(BaseModel):
    """One centrally-authorized operation for the runner to execute.

    Carries the fields the runner needs to resolve and invoke a handler
    entirely locally: the dotted ``handler_ref`` (resolved via
    :func:`~meho_backplane.operations._handler_resolve.import_handler`),
    the ``(product, version, impl_id)`` registry key used to rebind a
    bound-method handler against its connector instance, the validated
    ``params``, the ``safety_level`` the runner re-checks (defence in
    depth), the principal context, and the resolved target descriptor
    (``None`` for targetless synthetic ops such as ``net.*``).
    """

    model_config = ConfigDict(frozen=True)

    check_ref: str
    op_id: str
    product: str
    version: str = ""
    impl_id: str = ""
    handler_ref: str
    params: dict[str, Any] = Field(default_factory=dict)
    safety_level: str
    principal: RunnerPrincipal
    target_descriptor: ResolvedTargetDescriptor | None = None


class RunnerAssignment(BaseModel):
    """The runner's current work assignment, fetched each tick.

    ``assignment_version`` is an opaque digest the runner echoes back as
    ``known_version`` so central can answer an unchanged assignment with a
    ``304`` (see :meth:`RunnerClient.fetch_assignment`).
    """

    model_config = ConfigDict(frozen=True)

    assignment_version: str
    items: list[RunnerWorkItem] = Field(default_factory=list)


class RunnerResult(BaseModel):
    """The outcome of executing one :class:`RunnerWorkItem`.

    ``result_uid`` is generated on the runner (a uuid4 hex) so central
    ingest can deduplicate spool re-posts idempotently — a batch written
    to the retry spool carries the same uids when it is re-posted.

    ``status`` is a runner-level tri-state, distinct from any status
    inside ``result``:

    * ``ok`` — the handler ran and returned its structured payload
      (which may itself report a failed probe; a failed check is a
      result, not a runner error).
    * ``refused`` — the runner declined to execute (unsafe safety_level,
      or a handler_ref outside the connector tree).
    * ``error`` — the handler raised; the exception is summarised in
      ``error`` and never re-raised into the tick loop.
    """

    model_config = ConfigDict(frozen=True)

    result_uid: str
    check_ref: str
    op_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


class RunnerResultBatch(BaseModel):
    """A batch of results the runner reports (or spools) in one POST."""

    model_config = ConfigDict(frozen=True)

    runner_id: str
    results: list[RunnerResult] = Field(default_factory=list)
