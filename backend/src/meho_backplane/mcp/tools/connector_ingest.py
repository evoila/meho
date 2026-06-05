# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingest-pipeline MCP tools for the connector admin surface.

Two tools under the ``meho.connector.*`` namespace that drive the
spec-ingestion pipeline + its async job offload:

* ``meho.connector.ingest`` — run the ingest pipeline. **tenant_admin**.
* ``meho.connector.ingest_status`` — poll an async ingest job. **operator**.

The review / edit / state-machine tools (``list`` / ``review`` /
``edit_group`` / ``edit_op`` / ``enable`` / ``disable``) live in the
sibling :mod:`meho_backplane.mcp.tools.connector_admin` module; the two
files split the seven-plus-one admin tools by responsibility (pipeline
vs. review) so neither grows past the code-quality file-size budget.
Both import the shared schema snippets + coercion helpers from
:mod:`meho_backplane.mcp.tools._connector_shared`.

Async ingest offload (#1531)
============================

A real (``dry_run=false``) ingest of a real-world vendor OpenAPI spec
(SDDC Manager 9.0, ~375 ops; vmware/9.0, 1275 ops) runs the parser +
register + LLM-grouping pipeline for tens of seconds. Run inline on the
MCP request, that blocks past the agent's tool-call deadline and the
call times out with no handle to recover the result — the register +
grouping phases still commit in their own sessions (no work is lost),
but the agent can't confirm the connector populated.

This tool carries the #1303 REST async-202 offload to the MCP surface,
reusing the same in-memory
:class:`~meho_backplane.operations.ingest.IngestJobRegistry` +
:func:`~meho_backplane.operations.ingest.run_ingest_job` the REST route
drives. With ``async=true`` (and ``dry_run=false``) the handler creates
a job row, fires the pipeline off the request via
:func:`asyncio.create_task`, and returns an
:class:`~meho_backplane.operations.ingest.IngestJobHandle` immediately;
the agent polls ``meho.connector.ingest_status`` to completion. A run
started over MCP is poll-able over the REST
``GET /api/v1/connectors/ingest/jobs/{job_id}`` endpoint and vice versa
— the shared registry is the single source of truth, the same
cross-surface property :mod:`meho_backplane.mcp.tools.agent_runs` has
for agent runs (#811).

``dry_run=true`` and ``async=false`` keep the existing **inline** shape
— the pipeline runs on the request and the full
:class:`~meho_backplane.operations.ingest.IngestResponse` returns
synchronously. ``dry_run`` is the parse-only fast path (no DB / LLM
hops); small-spec callers and CI fixtures stay synchronous by leaving
``async`` unset. ``async`` is ignored on the dry-run path, mirroring
the REST route.

Error mapping
=============

``VersionMismatchError`` / ``UncoveredVersionLabel`` are caller-input
validation errors that surface as JSON-RPC ``-32602`` with the shared
structured detail on ``error.data`` (G0.9.1-T5 #777). These are only
catchable on the **inline** path — the async path has already returned
a handle by the time the pipeline raises, so a failure there flips the
job to ``failed`` and the diagnostic surfaces via ``error`` /
``error_class`` on the ``meho.connector.ingest_status`` response
(same trade-off the REST async path makes). A missing / cross-tenant
job id on the poll tool surfaces as ``-32602`` ``ingest_job_not_found``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final
from uuid import UUID

import structlog

from meho_backplane.api.v1.connectors_ingest import get_llm_client_factory
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._connector_shared import (
    _OP_CLASS_READ,
    _OP_CLASS_WRITE,
    _TENANT_ID_PROPERTY,
    _coerce_tenant_id,
    _model_dump_json_safe,
)
from meho_backplane.operations.ingest import (
    IngestionPipelineResult,
    IngestionPipelineService,
    IngestJob,
    IngestJobHandle,
    IngestJobNotFoundError,
    IngestJobStatusResponse,
    IngestRequest,
    SpecSource,
    UncoveredVersionLabel,
    VersionMismatchError,
    build_uncovered_version_label_detail,
    build_version_mismatch_detail,
    get_job_registry,
    run_ingest_job,
)

__all__: list[str] = []

_log = structlog.get_logger(__name__)


#: Strong references to in-flight background tasks. Python's
#: :func:`asyncio.create_task` does not retain its tasks; without an
#: external reference the garbage collector can drop the task
#: mid-execution. Cleared by each task's completion callback so a
#: steady stream of async ingests doesn't grow the set unbounded.
#: Mirrors the REST route's ``_background_tasks`` map
#: (:mod:`meho_backplane.api.v1.connectors_ingest`); the MCP surface
#: keys by the task itself (a ``set``) rather than by job id because
#: the MCP path has no cancel-by-id verb yet.
_background_tasks: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# meho.connector.ingest
# ---------------------------------------------------------------------------


def _build_ingest_request(arguments: dict[str, Any]) -> IngestRequest:
    """Translate the JSON-Schema-validated MCP arguments into an IngestRequest.

    The MCP path always constructs the explicit-quadruple shape (the
    JSON-Schema layer doesn't expose ``catalog_entry`` — that lives
    only on the REST surface today), so product / version / impl_id are
    guaranteed non-None after validation. The asserts pin the invariant
    for mypy after the schema's optional typing widened to support the
    REST ``catalog_entry`` shape (G0.14-T9 / #1150).
    """
    request = IngestRequest(
        product=arguments["product"],
        version=arguments["version"],
        impl_id=arguments["impl_id"],
        specs=[SpecSource(uri=spec["uri"]) for spec in arguments["specs"]],
        base_url=arguments.get("base_url"),
        dry_run=bool(arguments.get("dry_run", False)),
    )
    assert request.product is not None
    assert request.version is not None
    assert request.impl_id is not None
    return request


async def _ingest_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run the full T1 + T2 + T3 ingest pipeline (inline or async).

    Translates the MCP arguments into the canonical
    :class:`IngestRequest` and delegates to
    :meth:`IngestionPipelineService.ingest`. The handler reads the
    **active** LLM-client factory via
    :func:`meho_backplane.api.v1.connectors_ingest.get_llm_client_factory`,
    so it shares the production factory FastAPI lifespan startup installs
    rather than pinning the fail-closed default (#1386).

    ``async=true`` (and ``dry_run=false``) returns an
    :class:`IngestJobHandle` immediately and runs the pipeline off the
    request (#1531). ``dry_run=true`` / ``async=false`` keep the inline
    shape and return the canonical :class:`IngestResponse`.
    """
    request = _build_ingest_request(arguments)
    tenant_id = _coerce_tenant_id(arguments.get("tenant_id"))
    service = IngestionPipelineService(
        operator,
        llm_client_factory=get_llm_client_factory(),
    )
    # Async offload only when the operator opted in *and* this is a real
    # write — ``dry_run`` is the parse-only fast path that never blocks
    # long enough to matter, so it stays inline and ``async`` is ignored
    # there (mirrors the REST route).
    if bool(arguments.get("async", False)) and not request.dry_run:
        return await _spawn_async_ingest(
            service=service,
            request=request,
            operator=operator,
            tenant_id=tenant_id,
        )
    return await _run_inline_ingest(
        service=service,
        request=request,
        tenant_id=tenant_id,
    )


async def _run_inline_ingest(
    *,
    service: IngestionPipelineService,
    request: IngestRequest,
    tenant_id: UUID | None,
) -> dict[str, Any]:
    """Run the pipeline on the request and return the canonical IngestResponse.

    The inline path is the pre-#1531 behaviour, preserved verbatim for
    ``dry_run=true`` and ``async=false`` callers (no regression). The
    typed caller-input exceptions map to JSON-RPC ``-32602`` with the
    shared structured detail on ``error.data`` (G0.9.1-T5 #777); the
    shared builders in :mod:`operations/ingest/error_envelopes` are the
    single source of truth so the REST 422 envelope and the MCP
    ``error.data`` member can't drift.
    """
    assert request.product is not None
    assert request.version is not None
    assert request.impl_id is not None
    try:
        result = await service.ingest(
            product=request.product,
            version=request.version,
            impl_id=request.impl_id,
            specs=request.specs,
            base_url=request.base_url,
            tenant_id=tenant_id,
            dry_run=request.dry_run,
        )
    except VersionMismatchError as exc:
        raise McpInvalidParamsError(
            str(exc),
            data=build_version_mismatch_detail(exc),
        ) from exc
    except UncoveredVersionLabel as exc:
        raise McpInvalidParamsError(
            str(exc),
            data=build_uncovered_version_label_detail(exc),
        ) from exc
    ingestion_model, grouping_model = result.to_api_models()
    # Build the canonical IngestResponse shape manually — the explicit
    # dict here is the same wire contract the REST router emits.
    return {
        "ingestion": ingestion_model.model_dump(mode="json"),
        "grouping": (
            grouping_model.model_dump(mode="json") if grouping_model is not None else None
        ),
    }


async def _spawn_async_ingest(
    *,
    service: IngestionPipelineService,
    request: IngestRequest,
    operator: Operator,
    tenant_id: UUID | None,
) -> dict[str, Any]:
    """Create a job row, fire the pipeline off the request, return a handle.

    Carries the #1303 REST async-202 offload onto the MCP surface
    (#1531). Reuses the shared :class:`IngestJobRegistry` +
    :func:`run_ingest_job` the REST route drives, so a run started over
    MCP is poll-able over REST and vice versa. The handle returns
    immediately — well inside the agent's tool-call deadline — and the
    long-running parse + register + LLM-grouping pass runs in a
    background :func:`asyncio.create_task` (not the request).

    The job is scoped to the operator's tenant; the poll tool applies
    the same cross-tenant 404 conflation. A typed caller-input failure
    (version mismatch / uncovered label) raised by the pipeline now
    flips the job to ``failed`` and surfaces via the poll response's
    ``error`` / ``error_class`` rather than as an inline ``-32602`` —
    the same trade-off the REST async path documents.

    ``request`` is already in the explicit-quadruple shape — its
    product / version / impl_id are non-None (proven at construction by
    :func:`_build_ingest_request`). :meth:`IngestJobRegistry.create`
    takes ``str | None`` for those descriptors, so no re-assert is
    needed here; the closure's stricter :meth:`ingest` call carries the
    ``# type: ignore`` that pins the runtime guarantee for mypy.
    """
    registry = get_job_registry()
    job = await registry.create(
        operator_sub=operator.sub,
        tenant_id=tenant_id,
        catalog_entry=None,  # the MCP path is always explicit-quadruple
        product=request.product,
        version=request.version,
        impl_id=request.impl_id,
        spec_uris=[spec.uri for spec in request.specs],
    )

    async def _pipeline_call() -> IngestionPipelineResult:
        return await service.ingest(
            product=request.product,  # type: ignore[arg-type]
            version=request.version,  # type: ignore[arg-type]
            impl_id=request.impl_id,  # type: ignore[arg-type]
            specs=request.specs,
            base_url=request.base_url,
            tenant_id=tenant_id,
            dry_run=False,
        )

    # ``asyncio.create_task`` (not the request itself) so the work
    # survives the tool call returning the handle. Parallels the REST
    # route's ``_spawn_async_ingest`` and every other background-worker
    # spawn in the backplane.
    task = asyncio.create_task(
        run_ingest_job(job.job_id, pipeline_call=_pipeline_call),
        name=f"mcp-ingest-job-{job.job_id}",
    )
    _track_background_task(task)

    handle = IngestJobHandle(
        job_id=job.job_id,
        status=job.status,
        poll_url=f"/api/v1/connectors/ingest/jobs/{job.job_id}",
    )
    return _model_dump_json_safe(handle)


def _track_background_task(task: asyncio.Task[None]) -> None:
    """Hold a strong reference to *task* until it completes.

    See :data:`_background_tasks` for the rationale — a bare
    :func:`asyncio.create_task` task can be GC'd mid-execution if no
    code retains a reference. The completion callback removes the
    reference; double-removal is tolerated (``set.discard``).
    """
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# meho.connector.ingest_status
# ---------------------------------------------------------------------------


async def _ingest_status_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Poll an async ingest job's durable status by handle.

    Companion to the async path of :func:`_ingest_handler`. Reads the
    in-memory :class:`IngestJob` row from the shared registry, applies
    the tenant-isolation gate (a non-admin operator probing a built-in
    job, or any operator probing another tenant's job, sees the same
    ``ingest_job_not_found`` a missing id returns), and projects the
    row into the :class:`IngestJobStatusResponse` wire shape the REST
    poll endpoint also returns.

    An unknown / cross-tenant / malformed handle surfaces as JSON-RPC
    ``-32602`` (``handle must be a valid job id (UUID)`` /
    ``ingest_job_not_found``) — the closest spec-blessed shape for
    "bad input" / "not found", matching the connector-ingest error
    convention.
    """
    raw = arguments["job_id"]
    try:
        job_id = UUID(raw)
    except (ValueError, TypeError, AttributeError) as exc:
        raise McpInvalidParamsError("handle must be a valid job id (UUID)") from exc
    registry = get_job_registry()
    try:
        job = await registry.get(
            job_id,
            tenant_id=operator.tenant_id,
            is_tenant_admin=operator.tenant_role is TenantRole.TENANT_ADMIN,
        )
    except IngestJobNotFoundError as exc:
        raise McpInvalidParamsError("ingest_job_not_found") from exc
    return _model_dump_json_safe(_job_to_response(job))


def _job_to_response(job: IngestJob) -> IngestJobStatusResponse:
    """Project an :class:`IngestJob` into its Pydantic response shape.

    Mirrors the REST route's ``_job_to_response`` projection so the MCP
    poll response and the REST poll response carry an identical wire
    shape. A terminal success populates ``ingestion`` (+ optional
    ``grouping``); a terminal failure populates ``error`` +
    ``error_class``; ``running`` leaves both clusters ``None`` so
    clients branch on ``status`` rather than presence-checking.
    """
    ingestion_model = None
    grouping_model = None
    if job.status == "succeeded" and job.result is not None:
        ingestion_model, grouping_model = job.result.to_api_models()
    return IngestJobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        catalog_entry=job.catalog_entry,
        product=job.product,
        version=job.version,
        impl_id=job.impl_id,
        spec_uris=list(job.spec_uris),
        started_at=job.started_at,
        ended_at=job.ended_at,
        ingestion=ingestion_model,
        grouping=grouping_model,
        error=job.error,
        error_class=job.error_class,
    )


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


_INGEST_DESCRIPTION: Final[str] = (
    "Ingest one or more OpenAPI specs into a MEHO connector "
    "(tenant_admin only). Parses each spec, registers the operations "
    "into the endpoint_descriptor table, and runs the LLM-summarised "
    "grouping pass. The connector lands in 'staged' state — operators "
    "must review groups + per-op flags and then call "
    "meho.connector.enable before the connector's operations become "
    "dispatchable. "
    "Use when adding a new vendor surface (product=vmware version=9.0 "
    "impl_id=vmware-rest specs=[...]); supports merging multiple specs "
    "under one connector (vSphere ingests vcenter.yaml + vi-json.yaml). "
    "For a real-world vendor spec set async=true to get a job handle "
    "back immediately (the parse+register+grouping pass blocks past the "
    "tool-call timeout otherwise); then poll meho.connector.ingest_status "
    "with the returned job_id until status is 'succeeded' or 'failed'. "
    "dry_run=true validates specs without writing and always returns "
    "inline (async is ignored). "
    "Do NOT use for typed connectors (Vault, K8s, bind9) — those "
    "register via register_typed_operation() at connector init and "
    "never need this tool."
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.ingest",
        description=_INGEST_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "product": {"type": "string", "minLength": 1, "maxLength": 64},
                "version": {"type": "string", "minLength": 1, "maxLength": 64},
                "impl_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "specs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "uri": {"type": "string", "minLength": 1, "maxLength": 2048},
                        },
                        "required": ["uri"],
                        "additionalProperties": False,
                    },
                },
                "base_url": {"type": ["string", "null"], "maxLength": 2048},
                "dry_run": {"type": "boolean", "default": False},
                "async": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Return a job handle immediately and run the "
                        "pipeline off the request, instead of blocking "
                        "for the full parse + register + grouping pass. "
                        "Required for real-world vendor specs, which "
                        "block past the agent's tool-call deadline when "
                        "run inline. Poll meho.connector.ingest_status "
                        "with the returned job_id. Ignored when "
                        "dry_run=true (the parse-only path stays inline)."
                    ),
                },
                "tenant_id": _TENANT_ID_PROPERTY,
            },
            "required": ["product", "version", "impl_id", "specs"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_ingest_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.connector.ingest_status",
        description=(
            "Poll the durable status of an async connector-ingest job "
            "by handle (operator-level). Use after a "
            "meho.connector.ingest call made with async=true returned a "
            "job_id — call this repeatedly until status is 'succeeded' "
            "(carries the final ingestion + grouping counts so you can "
            "confirm the connector populated) or 'failed' (carries "
            "error_class + error). While the pipeline runs the status is "
            "'running' and the result clusters are null; branch on "
            "status rather than presence-checking the fields. Reads the "
            "same in-memory job registry the REST "
            "GET /api/v1/connectors/ingest/jobs/{job_id} endpoint uses, "
            "so a run started over either surface is pollable from the "
            "other. Do NOT use for a sync (inline) ingest — those return "
            "the full result directly and mint no job_id. An unknown / "
            "cross-tenant handle returns 'ingest_job_not_found'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The job handle (a UUID) returned by an async meho.connector.ingest call."
                    ),
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_ingest_status_handler,
)
