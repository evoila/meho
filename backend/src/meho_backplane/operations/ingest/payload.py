# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic-v2 response models for the review queue.

The three models here are returned by
:meth:`~meho_backplane.operations.ingest.service.ReviewService.get_review_payload`
and re-exported via :mod:`meho_backplane.operations.ingest`. T5
(CLI), T6 (REST routes), and T7 (admin MCP tools) all serialise
these models verbatim — the JSON contract for ``meho connector
review`` and ``GET /api/v1/connectors/{id}/review`` is the
``model_dump()`` output of :class:`ConnectorReviewPayload`.

All three are ``frozen=True``: payloads flow read-only from the
service to the operator-facing surfaces; mutating one mid-render
would be a programming bug, not a feature.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from meho_backplane.operations.ingest.api_schemas import ConnectorAuthoringKind

__all__ = [
    "ConnectorReviewGroup",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "ConnectorReviewProvenance",
]


class ConnectorReviewProvenance(BaseModel):
    """Provenance for one spec ingested under this connector (#2291).

    Surfaces the durable
    :class:`~meho_backplane.db.models.SpecProvenance` record so an
    operator reviewing a connector can tell a vendor artifact from a
    hand-mutated one before enabling reads:

    * ``uri`` — the audit label as presented at ingest
      (``spec:`` / ``https://`` / ``file:///`` / ``docs:`` form).
    * ``sha256`` — hex digest over the raw spec bytes. Two ingests of
      the *same* label with different digests mean the content changed.
    * ``origin`` — ``fetched`` (https GET) vs ``inline`` (operator
      upload) vs ``shipped`` (MEHO-authored catalog data). An inline
      upload labelled with a vendor URL is thus distinguishable from a
      genuine fetch of that URL.
    * ``operator_sub`` — who ingested it (``None`` for boot-time
      shipped ingests with no operator).
    * ``ingested_at`` — when the provenance row was last written.

    Connectors ingested before this table landed have no provenance
    rows; the surfaces render that as "unknown (pre-provenance)" rather
    than a fabricated record.
    """

    model_config = ConfigDict(frozen=True)

    uri: str
    sha256: str
    origin: str
    operator_sub: str | None
    ingested_at: datetime


class ConnectorReviewOp(BaseModel):
    """One operation within a group, as rendered in the review payload.

    Carries the operator-relevant subset of
    :class:`~meho_backplane.db.models.EndpointDescriptor`: the
    identifiers (``op_id``), the descriptive fields (``summary``,
    ``description``, ``custom_description``), the policy hooks
    (``safety_level``, ``requires_approval``), the dispatchability
    flag (``is_enabled``), and the ``tags`` array. Notably **omits**
    ``parameter_schema`` / ``response_schema`` / ``embedding`` —
    those are dispatch-time concerns, not review-time concerns, and
    sending 4 KB of schema per op would balloon the review payload
    for connectors with thousands of operations (vCenter has 961
    paths in v0.2).
    """

    model_config = ConfigDict(frozen=True)

    op_id: str
    summary: str | None
    description: str | None
    custom_description: str | None
    safety_level: str
    requires_approval: bool
    is_enabled: bool
    tags: list[str]


class ConnectorReviewGroup(BaseModel):
    """One group within the review payload.

    Carries the LLM-generated group metadata
    (``group_key``, ``name``, ``when_to_use``), the current
    ``review_status``, and the full list of child operations. The
    operator-facing review UI iterates this list to render the
    review queue; ``op_count`` is the cheap precomputed length for
    the CLI's ``meho connector review`` summary view (T5).
    """

    model_config = ConfigDict(frozen=True)

    group_key: str
    name: str
    when_to_use: str
    review_status: str
    op_count: int
    ops: list[ConnectorReviewOp]


class ConnectorReviewPayload(BaseModel):
    """Top-level review payload returned by :meth:`ReviewService.get_review_payload`.

    ``connector_id`` is the operator-facing string the request
    targeted (echoed back verbatim for round-trip clarity).
    ``product`` / ``version`` / ``impl_id`` are the parsed triple
    derived via
    :func:`~meho_backplane.operations.ingest.parser.parse_connector_id`;
    the CLI / API layers surface them in operator output so the
    convention is self-documenting. ``tenant_id`` is the scope the
    payload was queried under (``None`` for built-in).

    ``kind`` (G0.28-T6 / #1979) is the authoring-mode discriminator —
    typed / ingested-shim / profiled / profiled-but-unreviewed —
    projected from the v2 resolver's
    :data:`~meho_backplane.connectors.base.ShimKind` tier for this
    connector's ``(product, version)`` crossed with the review-gate
    state, and ``dispatchable`` is its boolean roll-up (``True`` for
    typed / profiled). They mirror the same-named fields on
    :class:`~meho_backplane.operations.ingest.api_schemas.ConnectorListItem`
    so the list and review surfaces agree on how a connector reads.
    Both default to the bare-shim reading
    (``kind="ingested-shim"``, ``dispatchable=False``) so existing
    construction call sites keep working; the service always sets them
    explicitly. The per-scheme auth detail is deliberately **not**
    surfaced here yet — deferred until #1969 freezes the
    :class:`~meho_backplane.connectors.schemas.ExecutionProfile` schema
    (the Goal flags secret-handling sensitivity). See
    :data:`~meho_backplane.operations.ingest.api_schemas.ConnectorAuthoringKind`.
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None
    groups: list[ConnectorReviewGroup]
    total_op_count: int
    # ``total_op_count`` sums only ops in the *rendered* groups above. #125:
    # ``ungrouped_op_count`` is every other descriptor row for this connector —
    # ops with a null ``group_id`` or in an unrendered group — so
    # ``total_op_count + ungrouped_op_count`` reconciles exactly to the
    # ``operation_count`` the ``GET /api/v1/connectors`` listing reports for
    # the same connector (both count the same descriptor universe). ``0`` when
    # every op is grouped (the common case). Additive; defaults to ``0`` so
    # existing construction call sites keep working.
    ungrouped_op_count: int = 0
    kind: ConnectorAuthoringKind = "ingested-shim"
    dispatchable: bool = False
    # #2291: one entry per spec ingested under this connector scope,
    # ordered by ``uri``. Empty for connectors ingested before the
    # provenance table landed (surfaces render "unknown (pre-provenance)").
    # Additive with a default so existing construction call sites keep
    # working; the service always sets it from the persisted rows.
    provenance: list[ConnectorReviewProvenance] = []
