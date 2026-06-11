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

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ConnectorListItem",
    "ConnectorListResponse",
    "ConnectorState",
    "ConnectorStatusFilter",
    "EditGroupBody",
    "EditOpBody",
    "GroupingResultModel",
    "IngestJobHandle",
    "IngestJobStatusResponse",
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

    ``uri`` is the operator's spec identifier and audit label. When the
    spec is *fetched* by the backend, only an ``https://`` URL is
    accepted -- the G0.16-T8 (#95) SSRF / local-file guard rejects
    ``http``, ``file://``, and bare paths so a spec URI cannot become a
    network-topology or filesystem oracle.

    ``content`` carries the spec text inline. The ``meho`` CLI reads
    ``docs:<connector-id>/<file>`` and ``file://`` sources **CLI-side**
    and uploads the bytes here, leaving ``uri`` as the audit label, so
    no local path or non-https scheme reaches the backend. When
    ``content`` is set the backend uses it verbatim (size-capped) and
    skips the fetch; when absent, ``uri`` is fetched under the https
    guard. A bare ``docs:`` URI that reaches the backend with no
    ``content`` is rejected with :exc:`UnsupportedSpecError` (#1535).

    Wrapped in its own model so future per-spec knobs (auth headers,
    dialect pinning, content-type override) can land without
    reshaping :class:`IngestRequest`. ``extra="forbid"`` rejects
    unknown fields (per the request-schema-strictness rule) so a
    typo in a nested ``specs[*]`` entry fails 422 at the body parse
    instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(min_length=1, max_length=2048)
    # Inline spec text uploaded by the CLI for docs:/file:// sources.
    # ~20 MiB coarse char guard; the authoritative byte cap lives in
    # openapi._load_spec_bytes.
    content: str | None = Field(default=None, max_length=20 * 1024 * 1024)


class IngestRequest(BaseModel):
    """POST body for ``/api/v1/connectors/ingest``.

    Drives a full pipeline run: parse every spec in *specs*, call
    :func:`register_ingested_operations` once per spec under the same
    ``(product, version, impl_id)`` connector triple, then run
    :func:`run_llm_grouping` once for the whole connector. Multi-spec
    ingestion (vCenter's ``vcenter.yaml`` + ``vi-json.yaml``) lands as
    multiple ``SpecSource`` entries in one request.

    Two mutually-exclusive request shapes:

    * **Explicit-quadruple shape** — ``product`` + ``version`` +
      ``impl_id`` + ``specs[]`` carry the resolved triple plus the
      spec sources the caller already knows. The MCP admin tool and
      the historical CLI manual mode use this shape.
    * **Catalog-driven shape** (G0.14-T9 / #1150) — ``catalog_entry``
      carries a ``"<product>/<version>"`` reference; the route
      handler resolves the entry against the packaged catalog (see
      :mod:`meho_backplane.operations.ingest.catalog`) and fills in
      ``product`` / ``version`` / ``impl_id`` / ``specs[]`` from the
      catalog entry before dispatching through the existing ingest
      path. REST-native agent runtimes that can't shell out to the
      CLI use this shape; the CLI's ``--catalog`` flag has been
      refactored to POST this shape rather than resolving the entry
      client-side.

    The two shapes are mutually exclusive: a body that sets
    ``catalog_entry`` alongside any of ``product`` / ``version`` /
    ``impl_id`` / ``specs[]`` fails 422 ``catalog_entry_conflict`` at
    the validator below. A body that sets neither fails 422
    ``ingest_request_underspecified``. ``base_url`` and ``dry_run``
    are accepted in both shapes.

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

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    product: str | None = Field(default=None, min_length=1, max_length=64)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    impl_id: str | None = Field(default=None, min_length=1, max_length=128)
    specs: list[SpecSource] = Field(default_factory=list, max_length=16)
    catalog_entry: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, max_length=2048)
    dry_run: bool = False
    #: Background-mode opt-out. ``True`` (default) fires the
    #: pipeline off the request thread and returns
    #: :class:`IngestJobHandle` at HTTP 202; ``False`` runs the
    #: pipeline inline and returns :class:`IngestResponse` at
    #: HTTP 200 -- the legacy v0.8.x shape kept for callers with
    #: small specs that still want a blocking response (CI tests,
    #: ``dry_run=True`` validation runs, ad-hoc shell scripts).
    #: ``dry_run=True`` ignores this flag and always runs inline --
    #: the parse-only path is the fast leg per RDC #771 Finding 21
    #: and never trips the liveness-probe deadline. Aliased to
    #: ``async`` on the wire (Python reserved word) so the JSON
    #: field reads naturally, same shape :class:`AgentRunRequest`
    #: established in G11.1-T4.
    async_: bool = Field(
        default=True,
        alias="async",
        description=(
            "Run the pipeline off the request thread (202 + job handle); "
            "set to false for the legacy blocking response. Ignored when "
            "dry_run=true."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_request_shape(self) -> IngestRequest:
        """Reject bodies that mix or omit both request shapes.

        Catalog-driven shape: ``catalog_entry`` set, every quadruple
        field unset / empty. Explicit-quadruple shape: every quadruple
        field set, ``catalog_entry`` unset. Anything else is a
        caller-side bug worth a 422 rather than a half-resolved
        downstream failure (the route handler can't tell which shape
        the caller intended, and silently picking one would land an
        ingest under either the wrong triple or a half-populated
        catalog entry).

        The error messages carry the convention's diagnostic shape
        (see :doc:`docs/codebase/error-message-shape.md`): a stable
        ``snake_case`` classifier prefix (``catalog_entry_conflict``
        / ``ingest_request_underspecified``) so REST callers and the
        MCP-driving agent can branch without re-parsing the prose.
        """
        catalog_set = self.catalog_entry is not None
        quadruple_set = (
            self.product is not None
            or self.version is not None
            or self.impl_id is not None
            or len(self.specs) > 0
        )
        if catalog_set and quadruple_set:
            raise ValueError(
                "catalog_entry_conflict: 'catalog_entry' is mutually exclusive "
                "with 'product' / 'version' / 'impl_id' / 'specs[]'; "
                "supply only one request shape. "
                "See docs/codebase/error-message-shape.md.",
            )
        if not catalog_set and not quadruple_set:
            raise ValueError(
                "ingest_request_underspecified: supply either "
                "'catalog_entry' (catalog-driven shape) or "
                "'product' + 'version' + 'impl_id' + 'specs[]' "
                "(explicit-quadruple shape). "
                "See docs/codebase/error-message-shape.md.",
            )
        if not catalog_set:
            # Explicit-quadruple shape — every quadruple field must
            # be set. Partial-quadruple bodies (e.g. impl_id missing)
            # would otherwise silently default to None and surface
            # downstream as a confusing register_ingested error.
            missing = [
                name
                for name, value in (
                    ("product", self.product),
                    ("version", self.version),
                    ("impl_id", self.impl_id),
                )
                if value is None
            ]
            if not self.specs:
                missing.append("specs")
            if missing:
                raise ValueError(
                    "ingest_request_underspecified: explicit-quadruple shape "
                    f"requires {missing}. Supply the missing field(s), or use "
                    "the catalog-driven shape via 'catalog_entry'. "
                    "See docs/codebase/error-message-shape.md.",
                )
        return self


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


class EditOpWarning(BaseModel):
    """One advisory attached to an ``edit_op`` write (G0.23-T4 #1630).

    Emitted when the edit is legal and **was applied**, but the
    operator should know it leads somewhere unpleasant. The only
    producer today is the enable-time auto-shim probe:
    ``is_enabled=True`` on an op whose resolved connector is the
    unconfigured ingest auto-shim
    (:class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`)
    — dispatch is then guaranteed to fail with the
    ``connector_unsupported`` / ``cause='unreplaced_auto_shim'``
    structured error (G0.23-T1 #1627), so this warning surfaces the
    dead end at enable time instead of one dispatch later.

    ``code`` reuses the dispatch-time cause vocabulary verbatim
    (``unreplaced_auto_shim``) so an operator — or an SDK — can
    correlate the proactive warning with the reactive dispatch error
    without a translation table. Declared as a one-member ``Literal``
    so the OpenAPI schema names the vocabulary; future advisory codes
    extend the union (an additive, client-compatible change).

    ``connector_class`` carries the resolved shim class's name
    (``AutoShim_<product>_<version>_<impl_id>``) — the same key the
    dispatch error's ``extras`` payload uses. ``message`` is the
    operator-facing prose: what was applied, why dispatch will still
    fail, and the remediation imperative (register the per-product
    subclass; re-ingesting will not replace the shim).
    """

    model_config = ConfigDict(frozen=True)

    code: Literal["unreplaced_auto_shim"]
    connector_class: str
    message: str


class EditOpResponse(BaseModel):
    """Response body for ``PATCH /api/v1/connectors/{id}/operations/{op_id}``.

    G0.23-T4 (#1630) promoted the route from ``204 No Content`` to
    ``200`` with this envelope so enable-time advisories have a
    structured home on the wire. ``warnings`` is empty on the
    overwhelmingly common clean path; a non-empty list never blocks
    the write it annotates (the PATCH semantics are unchanged — the
    edit landed, audit row included, warnings or not).

    Shaped as a list (not a single nullable field) so multiple
    advisories can ride one response without another schema bump.
    Required (no default) so the OpenAPI schema marks it as
    always-present and the generated Go client gets a plain slice
    instead of a pointer — same convention as
    :class:`~meho_backplane.operations.ingest.payload.ConnectorReviewPayload.groups`.
    """

    warnings: list[EditOpWarning]


#: Lifecycle of an async ingest job. Mirrors
#: :data:`~meho_backplane.operations.ingest.jobs.IngestJobStatus` so
#: the Pydantic projection and the internal dataclass share one
#: spelling; the route layer projects ``IngestJob`` rows through the
#: response models defined below.
IngestJobStatusLiteral = Literal["running", "succeeded", "failed"]


class IngestJobHandle(BaseModel):
    """Response body for ``POST /api/v1/connectors/ingest`` (HTTP 202).

    Returned when the route fires the pipeline off the request thread
    (the default; ``async=false`` switches to the legacy blocking
    :class:`IngestResponse` at HTTP 200). Carries the freshly-minted
    ``job_id``, the current ``status`` (always ``"running"`` here),
    and a relative ``poll_url`` the operator's client can follow to
    inspect progress.

    The shape is deliberately small -- two strings + a URL -- because
    every meaningful field lives on the polling response
    (:class:`IngestJobStatusResponse`). A handle is what you get back
    immediately so the request that started the work can return
    inside the kubelet liveness-probe deadline (escape hatch must
    not crash the pod, per G0.16-T1 / RDC #771 Finding 20).
    """

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: IngestJobStatusLiteral
    poll_url: str = Field(min_length=1, max_length=2048)


class IngestJobStatusResponse(BaseModel):
    """Response body for ``GET /api/v1/connectors/ingest/jobs/{job_id}``.

    Mirrors the in-memory
    :class:`~meho_backplane.operations.ingest.jobs.IngestJob` row,
    projecting it into a Pydantic-typed shape the route returns.
    Three field clusters:

    * **Identity** -- ``job_id`` + the originator's request descriptors
      (``catalog_entry`` / ``product`` / ``version`` / ``impl_id`` /
      ``spec_uris``). Echo so the polling caller doesn't need to
      correlate against their own state.
    * **Lifecycle** -- ``status`` + ``started_at`` + optional
      ``ended_at``. Status moves ``running`` → ``succeeded`` or
      ``running`` → ``failed`` once.
    * **Result vs error** -- exactly one of ``ingestion`` (with
      optional ``grouping``) or ``error`` is populated, keyed on
      status. ``running`` leaves both ``None`` so polling clients
      branch on ``status`` rather than checking presence.

    ``error`` is the capped exception message; ``error_class`` is the
    Python exception class name -- structured enough for agents that
    want to branch (``VersionMismatchError`` vs
    ``LlmClientUnavailable``) without parsing prose. The structured
    422 envelopes the synchronous ingest path used to return (the
    error-shape convention's classifier + ``detail`` body) are NOT
    available off the request thread; the polling response is the
    new error surface for the async path.
    """

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: IngestJobStatusLiteral
    catalog_entry: str | None = None
    product: str | None = None
    version: str | None = None
    impl_id: str | None = None
    spec_uris: list[str] = Field(default_factory=list)
    started_at: float
    ended_at: float | None = None
    ingestion: IngestionResultModel | None = None
    grouping: GroupingResultModel | None = None
    error: str | None = None
    error_class: str | None = None
