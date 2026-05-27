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
* ``extra="forbid"`` on every **request** body (G0.9-T2 / #729):
  :class:`IngestRequest`, :class:`SpecSource`, :class:`EditGroupBody`,
  :class:`EditOpBody`. Unknown fields fail 422 ``extra_forbidden`` at
  the framework boundary instead of being silently dropped, so a v0.2.1
  client mis-spelling ``impl_id`` as ``implementation_id`` (or the
  RDC-team-style ``q`` / ``top_k`` carryover) hears about it
  immediately rather than seeing a confusing required-field error or
  silent default. Response models keep the pydantic default
  (``extra="ignore"``) — they aren't validated against client input.
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
    "ConnectorState",
    "ConnectorStatusFilter",
    "EditGroupBody",
    "EditOpBody",
    "GroupingResultModel",
    "IngestRequest",
    "IngestResponse",
    "IngestionResultModel",
    "NextStep",
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
    reshaping :class:`IngestRequest`. ``extra="forbid"`` rejects
    unknown fields (per the request-schema-strictness rule) so a
    typo in a nested ``specs[*]`` entry fails 422 at the body parse
    instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    ``extra="forbid"`` rejects unknown body fields with 422
    ``extra_forbidden`` (G0.9-T2 / #729) — silent-drop on a typo
    would otherwise mean the pipeline runs with the wrong impl_id
    and the operator only finds out at review-time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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


#: Lifecycle state of a row in the ``GET /api/v1/connectors`` response.
#:
#: G0.9.1-T1 (#773). Splits the two operator-meaningful states the listing
#: surfaces:
#:
#: * ``"ingested"`` — the connector has at least one row in
#:   ``operation_group`` / ``endpoint_descriptor`` and the dispatcher can
#:   resolve it via :func:`~meho_backplane.operations._lookup.connector_exists`.
#:   ``call_operation`` / ``list_operation_groups`` / ``search_operations``
#:   will return data (or a documented empty) for this ``connector_id``.
#: * ``"registered"`` — the connector class is registered in the v2 registry
#:   (via :func:`~meho_backplane.connectors.registry.register_connector_v2`)
#:   but no descriptor / group rows exist yet. The dispatcher cannot
#:   resolve it; ``list_operation_groups`` / ``search_operations`` /
#:   ``call_operation`` return ``UnknownConnectorError`` (REST → 404).
#:   The state is *visible-but-not-dispatchable*: operators see the
#:   connector exists, but know not to call it until the ingest /
#:   typed-op registration lands rows under it.
#:
#: Rows whose ``connector_id`` round-trips through
#: :func:`parse_connector_id` to a triple the dispatcher cannot resolve
#: (stale-rename DB rows, class-side-only entries whose v2-registry
#: product disagrees with the parser's derived product) are dropped
#: before the listing returns — they have no operator-meaningful state.
ConnectorState = Literal["ingested", "registered"]


class NextStep(BaseModel):
    """Self-describing in-product hint pointing at the verb that closes the workflow.

    Surfaced on :class:`ConnectorListItem` rows whose :attr:`~ConnectorListItem.state`
    is ``"registered"`` (G0.13-T3 / #1133). An ``"ingested"`` row sets
    :attr:`ConnectorListItem.next_step` to ``None`` because the dispatcher
    already resolves operations against it -- there is nothing left for the
    operator to do.

    Two ``verb`` shapes ship:

    * ``meho connector ingest --catalog <product>/<version>`` -- when the
      connector-spec catalog (G0.7-T8 / #743) carries an entry for the
      registry's ``(product, version)``. The operator copies the verb,
      ``meho connector ingest`` looks up the upstream spec URL, and
      dispatchability follows after operator review.
    * ``meho connector ingest --product <p> --version <v> --impl <i> --spec <uri>``
      -- when the catalog has no entry. The ``rationale`` makes the
      missing-catalog branch explicit so the operator knows they need
      to source the OpenAPI spec themselves.

    Frozen for the same reason every wire shape in this module is frozen:
    responses are read-only and any in-place mutation should surface as a
    Pydantic error rather than a silently-modified payload.
    """

    model_config = ConfigDict(frozen=True)

    verb: str = Field(min_length=1, max_length=512)
    rationale: str = Field(min_length=1, max_length=512)


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

    ``state`` (G0.9.1-T1 / #773) distinguishes *dispatchable* rows
    (``"ingested"`` — DB-backed, resolves through the dispatcher) from
    *registered-but-empty* rows (``"registered"`` — v2-registry entries
    without descriptor rows yet). Rows that wouldn't round-trip
    through the resolver at all (stale-impl_id DB rows, v2 entries
    whose registry product disagrees with the parser's derived
    product) are filtered out before the response is built;
    ``connector_id``s the listing emits are guaranteed to resolve
    through :func:`~meho_backplane.operations._lookup.connector_exists`
    when ``state == "ingested"``, and explicitly labelled
    not-yet-dispatchable when ``state == "registered"``. Defaults to
    ``"ingested"`` so existing call sites (tests, MCP fakes)
    construct rows without breakage.

    ``next_step`` (G0.13-T3 / #1133) is the self-describing hint that
    closes the workflow gap the v0.6.0 RDC dogfood surfaced (signal 11:
    half-registered connectors fail lookup with no in-product hint
    about what verb closes the workflow). It is a :class:`NextStep`
    object on ``state="registered"`` rows and ``None`` on
    ``state="ingested"`` rows (no operator action remains for an
    ingested connector). The hint is computed against the curated
    connector-spec catalog (#743): when the catalog carries an entry
    for the registry's ``(product, version)`` the verb points at
    ``meho connector ingest --catalog ...``; otherwise it points at
    the manual-mode flags. Defaults to ``None`` so existing
    construction call sites (tests, MCP fakes) continue to compile
    without explicit assignment.
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
    state: ConnectorState = "ingested"
    next_step: NextStep | None = None


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

    ``extra="forbid"`` (G0.9-T2 / #729) means an unknown field —
    e.g. a client mis-spelling ``when_to_use`` as ``whentouse`` —
    fails 422 instead of being silently dropped (which would
    surface as a confusing 400 "at least one field must be set"
    from the route layer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    ``extra="forbid"`` (G0.9-T2 / #729) rejects unknown fields with
    422 ``extra_forbidden`` — same fail-loud posture as every other
    public v1 request schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    custom_description: str | None = Field(default=None, min_length=1, max_length=4096)
    safety_level: Literal["safe", "caution", "dangerous"] | None = None
    requires_approval: bool | None = None
    is_enabled: bool | None = None
