# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector result models and the per-target identity-model enum.

All models use ``ConfigDict(frozen=True)`` — field reassignment is blocked at
runtime. Every nested mutable field (``FingerprintResult.extras``,
``OperationResult.extras``, ``ResultHandle.schema_``, ``ResultHandle.sample_rows``)
is wrapped in :class:`types.MappingProxyType` (and tuple-of-MappingProxyType for
``sample_rows``) on construction via a ``@model_validator(mode="after")``, so
in-place mutation also raises :exc:`TypeError`. Pydantic v2's ``frozen=True`` is
documented as "faux immutability" — it blocks field reassignment but not nested
mutation, hence the wrapping. See v0.1-spec
§"Versioned connectors + targets" L267-292 (fingerprint shape) and §"Per-target
identity model" L447-454 (AuthModel) and §"JSONFlux / result handles"
L294-311 (ResultHandle).

``ResultHandle`` lives here rather than in :mod:`meho_backplane.operations.reducer`
(despite the JSONFlux contract being a reducer concern) so that
:class:`OperationResult` can carry it as a first-class field without an
``operations → connectors`` cycle: ``operations.reducer`` already needs
``OperationResult`` indirectly (its ``Reducer`` Protocol surfaces values the
dispatcher wraps into ``OperationResult``), and the dispatcher's
``connectors → operations`` direction is forbidden in this codebase.
:mod:`meho_backplane.operations.reducer` re-exports ``ResultHandle`` so the
documented public path (``from meho_backplane.operations import ResultHandle``)
keeps working.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

__all__ = [
    "AuthModel",
    "CandidateHint",
    "DrillInUnavailableReason",
    "EdgeHint",
    "EdgeKind",
    "FetchMore",
    "FetchMoreDrillIn",
    "FetchMoreNativePagination",
    "FingerprintResult",
    "NodeHint",
    "NodeKind",
    "OperationResult",
    "PaginationHint",
    "ProbeResult",
    "ResultHandle",
    "TopologyHints",
]


#: Machine-readable cause for ``FetchMoreDrillIn.available=False`` (#1629).
#: ``no_tenant_context`` -- the reduce ran without a usable
#: ``tenant_id`` / ``operator_sub`` pair in the reducer context, so the
#: spill could not be keyed to a tenant (a non-dispatch reduce; never a
#: real authenticated dispatch, where ``Operator.tenant_id`` is a
#: required field). ``result_store_unavailable`` -- tenant context was
#: present but the Valkey-backed read-back store did not persist the
#: rows (unreachable, rejected the write, or disabled by configuration).
DrillInUnavailableReason = Literal["no_tenant_context", "result_store_unavailable"]


NodeKind = Literal[
    "target",
    "vm",
    "host",
    "network",
    "datastore",
    "namespace",
    "pod",
    "service",
    "ingress",
    "node",
    "principal",
    "vault-role",
    "vault-mount",
    "volume",
]
"""Closed enum of node kinds the v0.2 graph supports.

The vocabulary is the auto-discoverable subset agreed in G9.1 (#363) —
the curated cross-system kinds (e.g. ``"workload"``, ``"deployment"``)
land in G9.2 once operator annotation flows ship.
"""


EdgeKind = Literal["runs-on", "mounts", "routes-through", "belongs-to"]
"""The four edge kinds high-confidence probes can derive.

Cross-system semantic edges (``"authenticates-via"``, ``"depends-on"``,
``"replicates-to"``, ``"backed-up-by"``) need explicit operator
assertion and land in G9.2 — auto-inferring them from probes is the
failure mode this split guards against (per Initiative #363).
"""


class AuthModel(StrEnum):
    """Per-target identity model per v0.1-spec L447-454."""

    IMPERSONATION = "impersonation"
    SHARED_SERVICE_ACCOUNT = "shared_service_account"
    PER_USER = "per_user"


class FingerprintResult(BaseModel):
    """Connector fingerprint per consumer-needs L95."""

    model_config = ConfigDict(frozen=True)

    vendor: str
    product: str
    version: str | None = None
    build: str | None = None
    edition: str | None = None
    reachable: bool
    probed_at: datetime
    probe_method: str
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_extras(self) -> "FingerprintResult":
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))
        return self

    @field_serializer("extras")
    def _serialize_extras(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class ProbeResult(BaseModel):
    """Lightweight reachability + auth-challenge result."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str | None = None
    latency_ms: float | None = None
    probed_at: datetime


class FetchMoreDrillIn(BaseModel):
    """Drill-in branch of :class:`FetchMore` -- "can the agent fetch more rows from this handle?"

    G0.15-T8 (#1219) + G0.20-T7 (#1507). The reducer mints a handle and,
    when it spills the full materialized set to the read-back store,
    flips ``available`` to ``True`` and names the ``result_query`` MCP
    tool (``mcp_tool``) plus a ready-to-adapt ``example_call`` and the
    handle's ``expires_at`` so an agent reading the envelope can page
    beyond the inline sample without a discovery dance. When the spill
    was skipped or failed (no tenant-scoped dispatch, store unreachable),
    ``available`` stays ``False`` and ``rationale`` names the
    narrower-params workaround. The contract is self-documenting per the
    consumer's recommendation in ``claude-rdc-hetzner-dc#753``: *"the
    reduced response itself should tell the agent how to fetch more"*.

    ``rationale`` is the operator/agent-facing string explaining the
    current state -- for ``available=False`` it describes the
    workaround (re-call with narrower params); for ``available=True`` it
    names the tool + the handle id to call it with. The ``mcp_tool`` /
    ``mcp_resource_uri`` / ``example_call`` / ``expires_at`` fields are
    populated only on the ``available=True`` branch.

    ``reason`` (#1629) is the machine-readable counterpart of the
    ``available=False`` rationale: one of
    :data:`DrillInUnavailableReason`, naming *which* no-spill branch
    fired (``no_tenant_context`` vs ``result_store_unavailable``) so a
    reduced-but-unspilled result is self-explanatory instead of a
    silent N-of-M sample. ``None`` on the ``available=True`` branch.
    Hardens the RDC cycle-8 ``k8s.logs tail=300`` finding where the
    operator saw a 5-of-300 sample with no way to page and no stated
    cause.
    """

    model_config = ConfigDict(frozen=True)

    available: bool
    rationale: str = Field(
        description=(
            "Free-form prose explaining the current drill-in state. "
            "For ``available=False`` this names the workaround (typically "
            "re-call with native pagination); for ``available=True`` it "
            "names the read-back tool + the handle id to call it with."
        ),
    )
    reason: DrillInUnavailableReason | None = Field(
        default=None,
        description=(
            "Machine-readable cause when ``available=False``: "
            "``no_tenant_context`` (the reduce ran outside a "
            "tenant-scoped dispatch, so the spill could not be keyed) or "
            "``result_store_unavailable`` (the read-back store did not "
            "persist the rows). ``None`` when ``available=True``."
        ),
    )
    mcp_tool: str | None = Field(
        default=None,
        description=(
            "Name of the MCP meta-tool that reads rows back from this "
            "handle (``result_query``). Populated only when "
            "``available=True``."
        ),
    )
    mcp_resource_uri: str | None = Field(
        default=None,
        description=(
            "Optional MCP resource URI addressing the handle, when the "
            "read-back surface is exposed as a resource rather than a "
            "tool. ``None`` in the current tool-only surface."
        ),
    )
    example_call: Mapping[str, Any] | None = Field(
        default=None,
        description=(
            "Curated next-call template the agent can adapt -- the "
            "``result_query`` tool name + a first-page ``{handle_id, "
            "offset, limit}`` args block. Populated only when "
            "``available=True``."
        ),
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "When the spilled handle expires (TTL from spill time). After "
            "this, ``result_query`` returns a not-found miss. Populated "
            "only when ``available=True``."
        ),
    )

    @model_validator(mode="after")
    def _freeze_example_call(self) -> "FetchMoreDrillIn":
        if self.example_call is not None:
            object.__setattr__(self, "example_call", MappingProxyType(dict(self.example_call)))
        return self

    @field_serializer("example_call")
    def _serialize_example_call(self, value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return dict(value)


class FetchMoreNativePagination(BaseModel):
    """Native-pagination branch of :class:`FetchMore`.

    Answers *"what params let the underlying op return the next slice?"*

    G0.15-T8 (#1219). When the underlying op has documented native
    pagination support (k8s server-side ``label_selector`` / ``limit`` /
    ``_continue``, REST APIs with ``offset`` / ``cursor`` knobs, etc.),
    the reducer surfaces those param names + a curated
    ``example_next_call`` so an agent can keep iterating without
    re-discovering the contract every time it sees a handle.

    The reducer reads :class:`PaginationHint` from the op's registration
    metadata (carried through the dispatcher via ``context``). When no
    hint exists, ``available=False`` with the rationale "op has no
    documented native pagination support" -- the same response shape so
    consumers parse one branch regardless of source.

    ``params`` and ``example_next_call`` are mappings so the wire format
    is stable JSON; immutability is enforced via
    :class:`types.MappingProxyType` in :meth:`_freeze_nested` per the
    sibling pattern.
    """

    model_config = ConfigDict(frozen=True)

    available: bool
    rationale: str | None = None
    params: Mapping[str, str] | None = Field(
        default=None,
        description=(
            "Mapping of pagination-param name -> short description. The "
            "agent picks the relevant subset when constructing the next "
            "call. Omitted when ``available=False``."
        ),
    )
    example_next_call: Mapping[str, Any] | None = Field(
        default=None,
        description=(
            "Curated next-call template the agent can adapt. Shape is "
            "free-form by design (each connector picks the surface it "
            "wants to teach -- MCP tool name + args, REST verb + path, "
            "CLI invocation). Omitted when ``available=False``."
        ),
    )

    @model_validator(mode="after")
    def _freeze_nested(self) -> "FetchMoreNativePagination":
        if self.params is not None:
            object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if self.example_next_call is not None:
            object.__setattr__(
                self,
                "example_next_call",
                MappingProxyType(dict(self.example_next_call)),
            )
        return self

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        return dict(value)

    @field_serializer("example_next_call")
    def _serialize_example_next_call(
        self, value: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return dict(value)


class FetchMore(BaseModel):
    """Self-documenting "how do I fetch more rows from this handle?" envelope.

    G0.15-T8 (#1219). Shipped on every :class:`ResultHandle` the reducer
    mints so an agent reading the envelope can answer
    *"how do I get the next slice"* from the response itself -- without
    a discovery dance across MCP tools / resource URIs / REST routes /
    CLI verbs. Two branches because the answer has two independent
    parts:

    * :class:`FetchMoreDrillIn` -- can the agent fetch more rows from
      the handle directly (tool name / resource URI)? ``available=True``
      with the ``result_query`` tool named when the reducer spilled the
      full set to the read-back store (G0.20-T7 #1507);
      ``available=False`` with the narrower-params workaround when the
      spill was skipped or failed.
    * :class:`FetchMoreNativePagination` -- can the agent re-call the
      original op with narrower params (k8s ``label_selector``,
      ``_continue`` token, etc.)? Populated from the op's
      :class:`PaginationHint` registration metadata; ``available=False``
      with rationale when no hint exists.

    The shape is documentation-as-data, matching the established
    precedents the consumer cited:
    :attr:`~meho_backplane.db.models.ConnectorRegistration.next_step`
    (G0.13-T3 #1153), ``/ready.features`` (G0.14-T7),
    ``/retrieve/usage.counted_surfaces`` -- every reduced response
    teaches the agent how to act on it next.
    """

    model_config = ConfigDict(frozen=True)

    drill_in: FetchMoreDrillIn
    native_pagination: FetchMoreNativePagination


class PaginationHint(BaseModel):
    """Op-registration metadata describing the underlying op's native pagination contract.

    G0.15-T8 (#1219). Connectors that ship pagination-aware ops attach
    a :class:`PaginationHint` to the op's ``llm_instructions`` payload
    under the ``pagination_hint`` key; the dispatcher forwards it to
    the reducer via the reduce ``context``, and the reducer emits the
    :attr:`FetchMore.native_pagination` block from it. Ops without a
    hint emit ``native_pagination.available=False`` with a rationale.

    Reading from ``llm_instructions`` (rather than a new column) keeps
    the contract additive: no DB migration, no
    :class:`~meho_backplane.db.models.EndpointDescriptor` schema
    change. The validation lives at the Pydantic boundary so a malformed
    hint surfaces at op-registration time, not first reduce.

    ``params`` and ``example_next_call`` mirror the wire shape of
    :class:`FetchMoreNativePagination`; the reducer copies the validated
    hint into the envelope verbatim.
    """

    model_config = ConfigDict(frozen=True)

    params: Mapping[str, str] = Field(
        description=(
            "Mapping of pagination-param name -> short description. "
            "Surfaced verbatim by the reducer's "
            ":attr:`FetchMore.native_pagination.params` envelope."
        ),
    )
    example_next_call: Mapping[str, Any] = Field(
        description=(
            "Curated next-call template the reducer surfaces verbatim "
            "via :attr:`FetchMore.native_pagination.example_next_call`. "
            "Free-form shape; connectors pick the surface (MCP tool, "
            "REST verb, CLI invocation) the most-likely consumer reads."
        ),
    )

    @model_validator(mode="after")
    def _freeze_nested(self) -> "PaginationHint":
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(
            self,
            "example_next_call",
            MappingProxyType(dict(self.example_next_call)),
        )
        return self

    @field_serializer("params")
    def _serialize_params(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @field_serializer("example_next_call")
    def _serialize_example_next_call(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class ResultHandle(BaseModel):
    """Reference to a set-shaped payload stored out-of-band (MinIO / S3 / …).

    JSONFlux contract per v0.1-spec L294-311. The dispatcher's default
    reducer
    (:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`,
    installed via ``set_default_reducer``) populates this handle for
    set-shaped responses above its threshold (>50 rows / >4 KB); smaller /
    scalar payloads pass through and the field on :class:`OperationResult`
    stays ``None``. The materialized payload is summarized from an
    in-memory DuckDB table and the full set is spilled to a Valkey-backed
    read-back store
    (:class:`~meho_backplane.connectors.result_handle_store.ResultHandleStore`,
    G0.20-T7 #1507) keyed by ``(tenant_id, handle_id)`` with the handle's
    ``ttl_seconds`` as a server-enforced expiry; the ``result_query`` MCP
    tool reads windows back out of it, surfaced on
    :attr:`FetchMore.drill_in`.

    ``schema_`` (trailing underscore) is the JSON Schema of the underlying
    payload — the trailing underscore avoids collision with Pydantic's own
    deprecated ``BaseModel.schema()`` API and is serialised as ``schema_``
    in the wire format. ``model_config = ConfigDict(frozen=True)`` plus the
    :class:`types.MappingProxyType` wrapping in :meth:`_freeze_nested` so
    callers can't mutate a handle after the reducer hands it back — neither
    at the field level nor through the nested ``schema_`` dict or
    ``sample_rows`` list (mirrors the sibling-models'
    :attr:`FingerprintResult.extras` / :attr:`OperationResult.extras`
    pattern).

    ``sample_rows`` is the **single** home of the inline preview (#134):
    the reducer carries the bounded sample here and nowhere else. The
    sibling inline ``OperationResult.result`` summary reports the preview
    *count* (``sample_rows_returned``) but does not duplicate the rows, so
    an object-heavy list op ships one bounded copy of the sample rather
    than two. The reducer sizes ``sample_rows`` to a serialized-byte budget
    (``jsonflux_sample_byte_budget``), not only a row count, so the preview
    stays inline-consumable regardless of per-row object size; the full set
    is reachable via ``result_query`` (see ``fetch_more.drill_in``).

    ``fetch_more`` is the G0.15-T8 self-documenting drill-in / pagination
    envelope -- every handle the reducer mints carries it so an agent
    reading the response can answer *"how do I get more rows"* without a
    discovery dance. ``None`` only on legacy code paths that construct
    a :class:`ResultHandle` directly without going through the reducer
    (test fixtures, future spill backends that have already enriched
    the envelope); production reduce paths always populate it.
    """

    model_config = ConfigDict(frozen=True)

    handle_id: UUID
    summary_md: str
    schema_: Mapping[str, Any] = Field(
        description="JSON Schema (Draft 2020-12) of the underlying payload shape.",
    )
    total_rows: int | None = None
    sample_rows: tuple[Mapping[str, Any], ...] | None = None
    ttl_seconds: int
    fetch_more: FetchMore | None = None

    @model_validator(mode="after")
    def _freeze_nested(self) -> "ResultHandle":
        # Pydantic v2's ``frozen=True`` is "faux immutability" — it blocks
        # field reassignment but not nested mutation. Wrap ``schema_`` in
        # :class:`MappingProxyType` and convert ``sample_rows`` to a tuple
        # of MappingProxyType-wrapped dicts so callers can't tamper with
        # the handle post-construction, matching the sibling
        # :class:`FingerprintResult` / :class:`OperationResult` pattern.
        object.__setattr__(self, "schema_", MappingProxyType(dict(self.schema_)))
        if self.sample_rows is not None:
            object.__setattr__(
                self,
                "sample_rows",
                tuple(MappingProxyType(dict(row)) for row in self.sample_rows),
            )
        return self

    @field_serializer("schema_")
    def _serialize_schema(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    @field_serializer("sample_rows")
    def _serialize_sample_rows(
        self, value: tuple[Mapping[str, Any], ...] | None
    ) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        return [dict(row) for row in value]


class NodeHint(BaseModel):
    """One node a connector's :meth:`Connector.discover_topology` returns.

    The G9.1-T3 refresh service translates a list of these (plus
    :class:`EdgeHint` entries) into ``graph_node`` rows, scoped to the
    tenant of the seed target. ``properties`` is a free-form bag for
    connector-supplied detail (e.g. a VM's power state, a pod's QoS
    class) that the refresh service persists as JSONB.
    """

    model_config = ConfigDict(frozen=True)

    kind: NodeKind
    name: str
    properties: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_properties(self) -> "NodeHint":
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))
        return self

    @field_serializer("properties")
    def _serialize_properties(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class EdgeHint(BaseModel):
    """One edge a connector's :meth:`Connector.discover_topology` returns.

    Edges are directed (``from`` → ``to``) and identified by the
    ``(kind, name)`` tuples of the endpoints, not by node IDs — the
    refresh service maps the tuples to ``graph_node.id`` rows on apply.
    The kind/name pair is unique within ``(tenant_id, kind)`` per the
    ``graph_node`` schema in G9.1-T1.
    """

    model_config = ConfigDict(frozen=True)

    from_kind: NodeKind
    from_name: str
    to_kind: NodeKind
    to_name: str
    kind: EdgeKind
    properties: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_properties(self) -> "EdgeHint":
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))
        return self

    @field_serializer("properties")
    def _serialize_properties(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class TopologyHints(BaseModel):
    """Connector-supplied topology snapshot for one target.

    Returned by :meth:`Connector.discover_topology`. The G9.1-T3 refresh
    service diffs this against existing ``graph_node`` + ``graph_edge``
    rows for the same ``(tenant_id, target_id)`` and applies inserts /
    updates / soft-deletes.

    ``nodes`` and ``edges`` are :class:`tuple` — not :class:`list` — so
    the frozen model is deeply immutable end-to-end, mirroring the
    :attr:`ResultHandle.sample_rows` pattern. A ``list`` argument at
    construction is accepted and converted by Pydantic's validation.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[NodeHint, ...] = ()
    edges: tuple[EdgeHint, ...] = ()
    discovered_at: datetime


class CandidateHint(BaseModel):
    """One candidate target a connector's :meth:`Connector.list_candidates` returns.

    Candidates are potentially-reachable targets the connector inferred
    from an existing seed (e.g. a vCenter exposing its managed ESXi
    hosts; a kubeconfig listing peer cluster contexts). The
    G9.1-T6 ``meho targets discover`` CLI verb surfaces these to the
    operator; auto-registration is intentionally out of scope per
    Initiative #363 (operator runs ``meho targets import`` after review).

    ``evidence`` is the debugging payload — whatever made the connector
    think this candidate exists (e.g. ``{"source": "kubeconfig",
    "context_name": "cluster-2"}``); ``confidence`` is the connector's
    self-assessment of probe-vs-curation reliability.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    host: str
    port: int | None = None
    evidence: Mapping[str, Any]
    confidence: Literal["high", "medium", "low"]

    @model_validator(mode="after")
    def _freeze_evidence(self) -> "CandidateHint":
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        return self

    @field_serializer("evidence")
    def _serialize_evidence(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class OperationResult(BaseModel):
    """Connector op execution result.

    ``handle`` is populated by JSONFlux-aware reducers when the raw response
    is set-shaped and large enough to spill out-of-band; v0.2's pass-through
    reducer always leaves it ``None``. Consumers should treat
    ``result + handle`` as a tagged union: ``result`` carries either the raw
    payload (no reduction) or the reduced summary (reduction happened), and
    ``handle is not None`` is the signal that the **full** payload is
    addressable via the reducer's backing store rather than inlined here.
    """

    model_config = ConfigDict(frozen=True)

    status: str  # "ok" | "pending" | "error" | "denied"
    op_id: str
    result: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    duration_ms: float
    handle: ResultHandle | None = None
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _freeze_extras(self) -> "OperationResult":
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))
        return self

    @field_serializer("extras")
    def _serialize_extras(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)
