# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic-v2 request / response models for the connectors-ingest surface.

Shared by the REST router (G0.7-T6, ``api/v1/connectors_ingest.py``)
and the admin MCP tools (G0.7-T7, ``mcp/tools/connector_admin.py``).
Both surfaces hit the same service layer in :mod:`pipeline` and
:mod:`service`; defining the request / response shapes once lets the
sibling tasks consume them verbatim rather than rebuilding parallel
Pydantic trees.

The shapes are deliberately conservative:

* ``frozen=True`` on every model — responses flow read-only and any
  in-place mutation surfaces as a Pydantic error rather than a
  silently-modified payload.
* ``IngestRequest.specs`` is a list of :class:`SpecSource` objects with
  one ``uri`` field so future per-spec knobs (auth headers, dialect
  pinning) can land without breaking the wire contract.
* :class:`ConnectorListItem` projects the minimum set the operator
  needs from :class:`OperationGroup` aggregation — connector_id +
  parsed coordinates + tenant scope + per-status group counts. It
  intentionally omits per-op detail because the review payload
  (:class:`ConnectorReviewPayload`) is the right place to surface
  that.

Status filter for ``GET /api/v1/connectors``: the operator picks
``staged`` / ``enabled`` / ``disabled`` / ``all``. The semantics:

* ``staged`` — at least one group is in ``review_status='staged'``;
  the connector is awaiting review.
* ``enabled`` — every group is ``review_status='enabled'``; the
  connector is fully live.
* ``disabled`` — every group is ``review_status='disabled'``; the
  connector is fully rolled back.
* ``all`` (or ``None`` query param) — no filter, return every visible
  connector.

A connector with a mixed group state (some enabled, some staged)
shows in the ``staged`` filter — the operator-facing question is "is
there anything left to review" and the answer is yes. This is the
same conservative shape T5's CLI ``meho connector list --status
staged`` will hit.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ConnectorListItem",
    "ConnectorListResponse",
    "ConnectorStatusFilter",
    "EditGroupBody",
    "EditOpBody",
    "GroupingResultModel",
    "IngestRequest",
    "IngestResponse",
    "IngestionResultModel",
    "SpecSource",
]

#: Allowed values for the ``GET /api/v1/connectors?status=...`` filter.
#:
#: Mirrors the :class:`~meho_backplane.db.models.OperationGroup.review_status`
#: enum plus ``all`` for the no-filter case. ``None`` and ``all`` are
#: equivalent at the route layer; the explicit ``all`` value exists so
#: the CLI / MCP sibling tasks can use it as a sentinel without
#: needing to send ``status=<empty>``.
ConnectorStatusFilter = Literal["staged", "enabled", "disabled", "all"]


class SpecSource(BaseModel):
    """One spec to ingest under a connector triple.

    ``uri`` carries the operator's spec identifier. Three forms are
    accepted by the underlying :func:`parse_openapi` resolver:

    * Absolute local path — ``/abs/path/to/spec.yaml``.
    * HTTP(S) URL — ``https://api.example.com/openapi.yaml``.
    * ``docs:<connector-id>/<file>`` shorthand — resolves against the
      consumer's checked-in docs/ directory; CLI / API layers expand
      this before calling the parser.

    Wrapped in its own model so future per-spec knobs (auth headers,
    dialect pinning, content-type override) can land without
    reshaping :class:`IngestRequest`.
    """

    model_config = ConfigDict(frozen=True)

    uri: str = Field(min_length=1, max_length=2048)


class IngestRequest(BaseModel):
    """POST body for ``/api/v1/connectors/ingest``.

    Drives a full pipeline run: parse every spec in *specs*, call
    :func:`register_ingested_operations` once per spec under the same
    ``(product, version, impl_id)`` connector triple, then run
    :func:`run_llm_grouping` once for the whole connector. Multi-spec
    ingestion (vCenter's ``vcenter.yaml`` + ``vi-json.yaml``) lands as
    multiple ``SpecSource`` entries in one request.

    ``dry_run=True`` skips both the DB writes and the grouping pass:
    only :func:`parse_openapi` runs, and the response carries
    :class:`IngestionResultModel` counts derived from the parse output
    plus ``grouping=None``. Operators use the dry-run path to validate
    a spec before committing.

    ``base_url`` is the optional default base URL for the auto-
    registered :class:`GenericRestConnector` shim; ``None`` leaves
    the shim unconfigured (the connector's eventual hand-coded
    subclass at G3.x will set its own base URL).
    """

    model_config = ConfigDict(frozen=True)

    product: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    impl_id: str = Field(min_length=1, max_length=128)
    specs: list[SpecSource] = Field(min_length=1, max_length=16)
    base_url: str | None = Field(default=None, max_length=2048)
    dry_run: bool = False


class IngestionResultModel(BaseModel):
    """Pydantic projection of
    :class:`~meho_backplane.operations.ingest.register_ingested.IngestionResult`.

    The dataclass lives in :mod:`register_ingested` so the helper
    can stay framework-agnostic; this model is the wire-format twin
    the routes return. Fields mirror the dataclass one-for-one with
    an extra ``connector_id`` echo for round-trip clarity.
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    inserted_count: int
    updated_count: int
    skipped_count: int
    connector_registered: bool
    operations_grouped: bool


class GroupingResultModel(BaseModel):
    """Pydantic projection of
    :class:`~meho_backplane.operations.ingest.llm_groups.GroupingResult`.

    Mirrors the dataclass; rendered alongside :class:`IngestionResultModel`
    in the ingest response so the operator sees both counts.
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    groups_created: int
    operations_assigned: int
    operations_unassigned: int
    llm_call_count: int
    llm_duration_ms: float


class IngestResponse(BaseModel):
    """Response shape for ``POST /api/v1/connectors/ingest``.

    ``ingestion`` is always present. ``grouping`` is ``None`` for the
    dry-run path (no DB writes, no LLM call) and for the no-op
    re-run path (every op already grouped from a prior pass).

    The response carries no audit_log id explicitly — the chassis
    audit middleware writes one row per HTTP request with the route
    path bound, and the service-level helpers
    (:func:`register_ingested_operations`, :func:`run_llm_grouping`)
    write their own per-call rows under
    ``meho.connector.llm_grouping`` etc.
    """

    model_config = ConfigDict(frozen=True)

    ingestion: IngestionResultModel
    grouping: GroupingResultModel | None = None


class ConnectorListItem(BaseModel):
    """One row in the ``GET /api/v1/connectors`` response.

    Projects :class:`OperationGroup` aggregation. ``connector_id`` is
    the operator-facing identifier (``"<impl_id>-<version>"``);
    ``product`` / ``version`` / ``impl_id`` are the parsed triple.

    ``tenant_id`` is ``None`` for built-in / global connectors and a
    UUID for tenant-curated ones. ``group_count`` totals the rows
    visible to the caller; the three ``*_group_count`` fields split
    that total by ``review_status`` so operators can spot
    review-queue backlog at a glance.

    ``operation_count`` is the sum of operations across all groups
    in scope. Useful for the CLI's
    ``meho connector list --status staged`` summary view.
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None
    group_count: int
    staged_group_count: int
    enabled_group_count: int
    disabled_group_count: int
    operation_count: int


class ConnectorListResponse(BaseModel):
    """Response shape for ``GET /api/v1/connectors``.

    Wrapped in an object (rather than a bare ``list[...]``) so
    future paging / total / cursor metadata can land without a
    breaking change to the JSON shape.
    """

    model_config = ConfigDict(frozen=True)

    connectors: list[ConnectorListItem]


class EditGroupBody(BaseModel):
    """PATCH body for ``/api/v1/connectors/{id}/groups/{key}``.

    Both fields optional but at least one must be set; the route
    layer translates "neither set" into a 400. ``when_to_use`` is
    the load-bearing operator-authored prose the LLM proposed at
    grouping time; ``name`` is the Title Case display name.

    Pydantic-side ``max_length`` caps line up with the underlying
    :class:`OperationGroup.when_to_use` /
    :class:`OperationGroup.name` columns. Empty strings are
    rejected — operators that want to "clear" the field should
    re-run grouping rather than blank it.
    """

    model_config = ConfigDict(frozen=True)

    when_to_use: str | None = Field(default=None, min_length=1, max_length=2048)
    name: str | None = Field(default=None, min_length=1, max_length=128)


class EditOpBody(BaseModel):
    """PATCH body for ``/api/v1/connectors/{id}/operations/{op_id}``.

    Per-op operator overrides. At least one field must be set.

    ``safety_level`` is the bounded enum from
    :class:`~meho_backplane.db.models.EndpointDescriptor.safety_level`
    (``safe`` / ``caution`` / ``dangerous``); a value outside the set
    is rejected by Pydantic before the service layer sees it.

    ``is_enabled`` is the per-op override that wins over the group
    cascade (see :meth:`ReviewService.edit_op` for the full
    semantics). ``requires_approval`` flips the per-op flag that
    forces the dispatcher to write an audit row in
    ``status='pending'`` and wait for an operator decision before
    executing.

    ``custom_description`` is the operator's free-form override
    that the agent surfaces in place of the upstream spec's
    ``description`` field; capped at 4096 chars to keep audit-log
    payloads bounded.
    """

    model_config = ConfigDict(frozen=True)

    custom_description: str | None = Field(default=None, min_length=1, max_length=4096)
    safety_level: Literal["safe", "caution", "dangerous"] | None = None
    requires_approval: bool | None = None
    is_enabled: bool | None = None
