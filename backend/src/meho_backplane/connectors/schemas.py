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
    "EdgeHint",
    "EdgeKind",
    "FingerprintResult",
    "NodeHint",
    "NodeKind",
    "OperationResult",
    "ProbeResult",
    "ResultHandle",
    "TopologyHints",
]


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


class ResultHandle(BaseModel):
    """Reference to a set-shaped payload stored out-of-band (MinIO / S3 / …).

    JSONFlux contract per v0.1-spec L294-311. The dispatcher's default
    reducer
    (:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`,
    installed via ``set_default_reducer``) populates this handle for
    set-shaped responses above its threshold (>50 rows / >4 KB); smaller /
    scalar payloads pass through and the field on :class:`OperationResult`
    stays ``None``. The materialized payload is held in DuckDB (in-memory)
    for v0.2; an addressable spill backing store (MinIO/S3/Valkey) and the
    ``result_query`` / ``result_aggregate`` read-back meta-tools are a later
    Initiative.

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
    Initiative #363 (operator runs ``meho targets create`` after review).

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

    status: str  # "ok" | "error" | "denied"
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
