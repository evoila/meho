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

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.operations.ingest.api_schemas import ConnectorAuthoringKind

__all__ = [
    "ConnectorReviewGroup",
    "ConnectorReviewGroupAdvisory",
    "ConnectorReviewOp",
    "ConnectorReviewPayload",
    "ConnectorReviewProvenance",
    "GroupSafetyDistribution",
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
    # #3102: the stored ``parameter_schema`` cannot self-resolve (a
    # pre-#3095 ingest left a dangling ``$ref``), so dispatch refuses
    # every call with ``invalid_op_schema`` until the connector's spec
    # is re-ingested. Additive with a default so existing construction
    # call sites keep working; the service sets it from the row.
    needs_reingest: bool = False
    # #5029: the reading tenant's own on/off setting for a built-in op.
    # ``None`` means the tenant uses the install default (``is_enabled``),
    # or the payload is not for a built-in connector. The effective value
    # is this field when set, else ``is_enabled``.
    tenant_enabled: bool | None = None


class ConnectorReviewDeleteOp(BaseModel):
    """One stored ingested delete that sits below the ``destructive`` risk level (#3827).

    The review report for stored deletes. Rows persisted by an earlier
    ingest keep their old ``safety_level`` / ``requires_approval`` until the
    connector is ingested again, so a guard test on the spec cannot show an
    operator which stored rows still run without approval. Each row carries
    what is stored next to what a fresh ingest of the same ``(method, path)``
    would assign today (parser default, then the connector floor, then the
    catalog overlay). ``needs_correction`` is ``True`` when the stored row is
    weaker than that; ``reason`` says why in plain words.
    """

    model_config = ConfigDict(frozen=True)

    op_id: str
    method: str
    safety_level: str
    requires_approval: bool
    is_enabled: bool
    expected_safety_level: str
    expected_requires_approval: bool
    needs_correction: bool
    reason: str


class GroupSafetyDistribution(BaseModel):
    """Risk levels of the enabled ops in one group (#3671).

    Counts only ops with ``is_enabled=True``. One count per value of the
    closed ``safety_level`` set, plus how many of those ops need approval
    and how many ops are enabled in total. Disabled ops are not counted.
    """

    model_config = ConfigDict(frozen=True)

    enabled_op_count: int = 0
    safe: int = 0
    caution: int = 0
    dangerous: int = 0
    destructive: int = 0
    requires_approval_count: int = 0


class ConnectorReviewGroupAdvisory(BaseModel):
    """Advice for the operator who reviews one group (#3671).

    ``distribution`` is what the group's enabled ops really do.
    ``flags`` lists short, plain notes where the group's ``when_to_use``
    text does not match those ops (for example the text says
    "read-only" but some ops need approval). Empty when nothing looks
    wrong.

    This is advice only. It never blocks a dispatch, an ingest, or the
    move from ``staged`` to ``enabled``. Dispatch keeps using each op's
    own ``safety_level``, ``requires_approval``, and policy; the group
    label and its text are never part of that decision.
    """

    model_config = ConfigDict(frozen=True)

    distribution: GroupSafetyDistribution = Field(default_factory=GroupSafetyDistribution)
    flags: list[str] = Field(default_factory=list)


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
    # #3671: advice that compares ``when_to_use`` with the enabled ops.
    # Additive with a default so existing construction call sites keep
    # working; the service always sets it.
    advisory: ConnectorReviewGroupAdvisory = Field(default_factory=ConnectorReviewGroupAdvisory)


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
    # #3870: the ops counted by ``ungrouped_op_count``, one entry each,
    # sorted by ``op_id``, with the same per-op fields as a group's
    # ``ops`` (enabled state, safety level, approval flag). Dispatch
    # serves an enabled ungrouped op like any other, so the review lists
    # them too: ``groups`` plus ``ungrouped_ops`` is the full op set.
    # Additive with a default so existing construction call sites keep
    # working; the service always sets it.
    ungrouped_ops: list[ConnectorReviewOp] = []
    kind: ConnectorAuthoringKind = "ingested-shim"
    dispatchable: bool = False
    # #2291: one entry per spec ingested under this connector scope,
    # ordered by ``uri``. Empty for connectors ingested before the
    # provenance table landed (surfaces render "unknown (pre-provenance)").
    # Additive with a default so existing construction call sites keep
    # working; the service always sets it from the persisted rows.
    provenance: list[ConnectorReviewProvenance] = []
    # #3102: how many descriptor rows in this connector's scope carry
    # ``needs_reingest`` — counted over the same full descriptor
    # universe as ``total_op_count + ungrouped_op_count`` (group or
    # not), so a flagged op outside any rendered group still surfaces.
    # ``> 0`` means the stored schemas predate an ingest-parser fix and
    # a same-spec re-ingest repairs them in place (enablement, groups,
    # and operator curation are preserved by the upsert). Additive with
    # a default so existing construction call sites keep working.
    needs_reingest_op_count: int = 0
    # #3827: every stored ingested delete-shaped op (``DELETE`` verb, or a
    # write the connector floor treats as a removal) whose ``safety_level``
    # is below ``destructive``, with the posture a fresh ingest would give.
    # Counted over the full descriptor universe (group or not). Additive
    # with a default so existing construction call sites keep working.
    delete_ops_below_destructive: list[ConnectorReviewDeleteOp] = []
    # How many of those rows are weaker than a fresh ingest would make them
    # (``needs_correction``). ``> 0`` means an operator must ingest the
    # connector again or correct the rows with ``edit-op``.
    delete_ops_needing_correction_count: int = 0
