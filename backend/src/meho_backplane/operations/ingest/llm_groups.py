# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: orchestrator + result/config dataclasses + 5 phase helpers
# read top-to-bottom; further splitting would hide the linear two-pass flow
# the module docstring + diagram describe.

"""LLM-summarised operation grouping pass (G0.7-T3).

T3 runs *after* T2's :func:`register_ingested_operations` has bulk-
upserted parser output into ``endpoint_descriptor`` rows. The pass
groups those rows into agent-actionable buckets via two sequential LLM
calls per ingested connector:

* **Pass 1 -- group derivation.** The LLM receives every operation
  (``op_id``, ``summary``, ``tags``) and proposes 8-15 groups, each
  carrying a snake_case ``group_key``, a Title Case ``name``, and a
  paragraph-length ``when_to_use`` description. The proposal is
  validated against :class:`GroupProposal`; output that fails schema
  validation raises :class:`LlmOutputInvalid`.
* **Pass 2 -- per-op assignment.** The LLM receives the group list
  and the ops it must assign, in batches whose size is bounded by
  ``batch_size`` (default 50). Each batch returns a JSON mapping of
  ``op_id`` -> ``group_key`` (or ``"none"`` for the operator-review
  bucket). ``op_id``s the model omits or assigns to an unknown
  group stay ``group_id=NULL`` and count toward
  :attr:`GroupingResult.operations_unassigned`.

The function persists nothing until both passes complete, then writes
proposed groups (``review_status='staged'``) + per-op ``group_id``
assignments + one ``meho.connector.llm_grouping`` audit row in a
single transaction. Re-running on a fully-grouped connector is a
no-op (every op already has ``group_id`` set, no LLM call fires);
re-running on a partially-grouped connector runs only Pass 2 on the
unassigned rows against the existing :class:`OperationGroup` rows.

Per :file:`docs/codebase/spec-ingestion.md` and the T3 task body in
issue #404: this pass is non-optional for v0.2. Raw semantic+BM25
search across thousands of vendor endpoints ranks poorly; what makes
``search_operations`` agent-useful is "ask the connector for its
operation groups first, then scope a query to one". The grouping
pass + operator review (T4) is what produces that surface.

The LLM client itself is injected as a :class:`LlmClient` Protocol so
the chassis can swap in a real Anthropic Messages-API adapter without
re-plumbing T3. The production adapter
(:func:`~meho_backplane.operations.ingest.build_anthropic_ingest_llm_client`)
is wired at FastAPI lifespan startup via ``set_llm_client_factory``
(in :mod:`meho_backplane.api.v1.connectors_ingest`), reusing
``settings.anthropic_api_key`` (#1386); ``meho connector ingest``
(CLI / REST / MCP) on a deploy with the key set groups non-dry-run
ingests for real, and a deploy with no key fails closed with HTTP
503 / ``LlmClientUnavailable``. Routing through a provider-agnostic
shape (G11.5) for air-gapped deploys is the remaining follow-up.
Tests inject a deterministic stub via the same hook. There is no
module-level singleton -- every call site passes a client. See
``docs/codebase/spec-ingestion.md`` §"LLM-client wiring" for the
operator-facing framing.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest._internals import (
    OP_LLM_GROUPING,
    write_audit_row,
)
from meho_backplane.operations.ingest._llm_grouping_internals import (
    ASSIGN_OPS_SYSTEM_PROMPT,
    DEFAULT_GROUPING_BATCH_SIZE,
    DEFAULT_MAX_GROUPS,
    DEFAULT_MIN_GROUPS,
    NONE_GROUP_KEY,
    PASS1_MAX_OUTPUT_TOKENS,
    PASS2_MAX_OUTPUT_TOKENS,
    PROPOSE_GROUPS_SYSTEM_PROMPT,
    GroupProposal,
    LlmClient,
    LlmJsonResult,
    StructuredJsonLlmClient,
    build_connector_id,
    chunk_sequence,
    expected_llm_call_count,
    extract_json_object,
    load_existing_groups,
    load_unassigned_ops,
    parse_assignment_response,
    parse_proposal_response,
    render_assign_ops_prompt,
    render_propose_groups_prompt,
    strip_code_fences,
)

__all__ = [
    "DEFAULT_GROUPING_BATCH_SIZE",
    "DEFAULT_MAX_GROUPS",
    "DEFAULT_MIN_GROUPS",
    "GroupProposal",
    "GroupingConfig",
    "GroupingResult",
    "LlmClient",
    "LlmJsonResult",
    "StructuredJsonLlmClient",
    "expected_llm_call_count",
    "extract_json_object",
    "render_assign_ops_prompt",
    "render_propose_groups_prompt",
    "run_llm_grouping",
    "strip_code_fences",
]

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public result + config shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupingResult:
    """Counts + timings returned by :func:`run_llm_grouping`.

    Surfaced verbatim in the operator-facing CLI / API output (T5).

    Attributes
    ----------
    connector_id:
        Operator-facing identifier (``"<impl_id>-<version>"``) for the
        connector that was grouped.
    groups_created:
        Number of brand-new :class:`OperationGroup` rows inserted by
        this pass. Zero on a re-run that found existing groups (the
        partial-regrouping path).
    operations_assigned:
        Number of :class:`EndpointDescriptor` rows whose ``group_id``
        was set by this pass.
    operations_unassigned:
        Number of rows that remained ``group_id=NULL`` -- the LLM
        returned ``"none"`` for them, omitted them from the
        response, or assigned them to an unknown ``group_key``.
        Operator assigns these manually via T4's ``edit_op``.
    llm_call_count:
        Total LLM calls issued. ``1 + ceil(op_count / batch_size)``
        on a full grouping run (1 Pass-1 + N Pass-2 batches). Zero
        on a no-op re-run that found every op already grouped.
    llm_duration_ms:
        Wall-clock time spent inside
        ``await llm_client.generate_json(...)``, summed across both
        passes. Excludes DB time and prompt-render time so operators
        can monitor the LLM-side cost component independently.
    """

    connector_id: str
    groups_created: int
    operations_assigned: int
    operations_unassigned: int
    llm_call_count: int
    llm_duration_ms: float


@dataclass(frozen=True, slots=True)
class GroupingConfig:
    """Tunable knobs threaded through the orchestrator phases.

    Bundled into a config dataclass so individual phase helpers stay
    under a small positional / keyword-argument count without losing
    visibility on which knob each helper consumes. Public callers
    typically construct one with defaults; the
    :func:`run_llm_grouping` entry point also accepts the same knobs
    as keyword arguments and builds the :class:`GroupingConfig`
    internally.

    Attributes
    ----------
    batch_size:
        Pass-2 batch size. Default
        :data:`DEFAULT_GROUPING_BATCH_SIZE` (50).
    min_groups, max_groups:
        Bounds on the Pass-1 group count. Default 8-15.
    """

    batch_size: int = DEFAULT_GROUPING_BATCH_SIZE
    min_groups: int = DEFAULT_MIN_GROUPS
    max_groups: int = DEFAULT_MAX_GROUPS

    def validate(self) -> None:
        """Raise :class:`ValueError` if any knob is out of bounds.

        Called once at the top of :func:`run_llm_grouping` so an
        invalid configuration fails before any LLM call or DB query.
        """
        if self.min_groups < 1:
            raise ValueError(f"min_groups must be >= 1; got {self.min_groups}")
        if self.max_groups < self.min_groups:
            raise ValueError(
                "max_groups must be >= min_groups; "
                f"got max_groups={self.max_groups} min_groups={self.min_groups}",
            )
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1; got {self.batch_size}")


# ---------------------------------------------------------------------------
# Internal orchestrator state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PhaseTotals:
    """Mutable accumulator threaded through the orchestrator's phases.

    Each helper updates its slice and the orchestrator reads the final
    totals when building the :class:`GroupingResult`.
    """

    llm_call_count: int = 0
    llm_duration_ms: float = 0.0
    groups_created: int = 0
    operations_assigned: int = 0
    operations_unassigned: int = 0


@dataclass(frozen=True, slots=True)
class _ConnectorTriple:
    """Connector coordinates threaded through phase helpers.

    Keeping the three columns together as a frozen tuple lets phase
    helpers take a single argument instead of three positional
    strings -- mirrors the :class:`ConnectorScope` precedent in
    :mod:`meho_backplane.operations.ingest._internals` for the
    review-queue service.
    """

    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None

    @property
    def connector_id(self) -> str:
        return build_connector_id(self.product, self.version, self.impl_id)


# ---------------------------------------------------------------------------
# Phase 1 helpers (group derivation)
# ---------------------------------------------------------------------------


async def _propose_groups_via_llm(
    llm_client: LlmClient,
    *,
    triple: _ConnectorTriple,
    operations: Sequence[EndpointDescriptor],
    config: GroupingConfig,
    totals: _PhaseTotals,
) -> list[GroupProposal]:
    """Run Pass 1 against the LLM and return the validated proposals.

    Updates *totals* in place with the LLM-call count and duration.
    Pass-1 output that fails schema validation raises
    :class:`LlmOutputInvalid`; the orchestrator does not catch it,
    because the operator-facing CLI / API layer above us is the
    right surface to render the retry prompt.
    """
    propose_prompt = render_propose_groups_prompt(
        connector_id=triple.connector_id,
        product=triple.product,
        version=triple.version,
        impl_id=triple.impl_id,
        operations=operations,
        min_groups=config.min_groups,
        max_groups=config.max_groups,
    )
    t0 = time.monotonic()
    raw_pass1 = await llm_client.generate_json(
        system_prompt=PROPOSE_GROUPS_SYSTEM_PROMPT,
        user_prompt=propose_prompt,
        max_output_tokens=PASS1_MAX_OUTPUT_TOKENS,
    )
    totals.llm_duration_ms += (time.monotonic() - t0) * 1000.0
    totals.llm_call_count += 1
    return parse_proposal_response(raw_pass1)


def _persist_proposed_groups(
    session: AsyncSession,
    *,
    triple: _ConnectorTriple,
    proposals: Sequence[GroupProposal],
) -> list[OperationGroup]:
    """Insert one :class:`OperationGroup` per proposal in ``staged`` state.

    The caller flushes after this returns so subsequent Pass-2 lookups
    by ``group_key`` see the new rows. Commit is deferred until the
    audit row + per-op updates land, so the whole pass is atomic.
    """
    rows: list[OperationGroup] = []
    for proposal in proposals:
        row = OperationGroup(
            tenant_id=triple.tenant_id,
            product=triple.product,
            version=triple.version,
            impl_id=triple.impl_id,
            group_key=proposal.group_key,
            name=proposal.name,
            when_to_use=proposal.when_to_use,
            review_status="staged",
        )
        session.add(row)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Phase 2 helpers (per-op assignment)
# ---------------------------------------------------------------------------


async def _assign_ops_in_batches(
    llm_client: LlmClient,
    *,
    triple: _ConnectorTriple,
    operations: Sequence[EndpointDescriptor],
    groups: Sequence[GroupProposal],
    config: GroupingConfig,
    totals: _PhaseTotals,
) -> dict[str, str]:
    """Run Pass-2 in :attr:`GroupingConfig.batch_size`-sized batches.

    Each batch produces a partial ``op_id -> group_key`` mapping; the
    helper merges them into a single dict. ``totals`` accumulates the
    Pass-2 LLM-call count + duration across batches.
    """
    valid_group_keys = {g.group_key for g in groups}
    batches = list(chunk_sequence(operations, config.batch_size))
    _log.info(
        "llm_grouping_pass2_start",
        connector_id=triple.connector_id,
        batch_count=len(batches),
        batch_size=config.batch_size,
        op_count=len(operations),
    )
    assignment_map: dict[str, str] = {}
    for batch_index, batch in enumerate(batches):
        batch_assignments = await _assign_single_batch(
            llm_client,
            triple=triple,
            batch=batch,
            groups=groups,
            valid_group_keys=valid_group_keys,
            totals=totals,
        )
        assignment_map.update(batch_assignments)
        _log.info(
            "llm_grouping_pass2_batch_complete",
            connector_id=triple.connector_id,
            batch_index=batch_index,
            batch_op_count=len(batch),
            assigned_in_batch=sum(1 for v in batch_assignments.values() if v != NONE_GROUP_KEY),
        )
    return assignment_map


async def _assign_single_batch(
    llm_client: LlmClient,
    *,
    triple: _ConnectorTriple,
    batch: Sequence[EndpointDescriptor],
    groups: Sequence[GroupProposal],
    valid_group_keys: set[str],
    totals: _PhaseTotals,
) -> dict[str, str]:
    """LLM-assign one Pass-2 batch and return its ``op_id -> group_key`` map."""
    batch_op_ids = {op.op_id for op in batch}
    assign_prompt = render_assign_ops_prompt(
        connector_id=triple.connector_id,
        product=triple.product,
        version=triple.version,
        groups=groups,
        operations=batch,
    )
    t0 = time.monotonic()
    raw_pass2 = await llm_client.generate_json(
        system_prompt=ASSIGN_OPS_SYSTEM_PROMPT,
        user_prompt=assign_prompt,
        max_output_tokens=PASS2_MAX_OUTPUT_TOKENS,
    )
    totals.llm_duration_ms += (time.monotonic() - t0) * 1000.0
    totals.llm_call_count += 1
    return parse_assignment_response(
        raw_pass2,
        valid_op_ids=batch_op_ids,
        valid_group_keys=valid_group_keys,
    )


def _apply_assignments_to_rows(
    *,
    operations: Sequence[EndpointDescriptor],
    assignment_map: dict[str, str],
    group_key_to_id: dict[str, UUID],
    totals: _PhaseTotals,
) -> None:
    """Mutate each :class:`EndpointDescriptor`'s ``group_id`` per *assignment_map*.

    Updates *totals.operations_assigned* and
    *totals.operations_unassigned* in place. The orchestrator's
    surrounding transaction is what makes the mutations durable.

    Ops the LLM omitted from its Pass-2 response (``op_id`` absent from
    *assignment_map*) and ops explicitly mapped to
    :data:`NONE_GROUP_KEY` are both counted exactly once as
    ``operations_unassigned``. This preserves the
    ``operations_assigned + operations_unassigned == len(operations)``
    reconciliation invariant the audit row depends on.
    """
    for op_row in operations:
        assigned_key = assignment_map.get(op_row.op_id)
        if assigned_key is None or assigned_key == NONE_GROUP_KEY:
            totals.operations_unassigned += 1
            continue
        group_uuid = group_key_to_id.get(assigned_key)
        if group_uuid is None:  # pragma: no cover -- guarded by parser
            totals.operations_unassigned += 1
            continue
        op_row.group_id = group_uuid
        totals.operations_assigned += 1


# ---------------------------------------------------------------------------
# Group-resolution branch (full-grouping vs partial-regrouping)
# ---------------------------------------------------------------------------


async def _resolve_groups_for_pass2(
    session: AsyncSession,
    llm_client: LlmClient,
    *,
    triple: _ConnectorTriple,
    unassigned_ops: Sequence[EndpointDescriptor],
    config: GroupingConfig,
    totals: _PhaseTotals,
) -> tuple[list[GroupProposal], dict[str, UUID]]:
    """Return the groups Pass 2 will assign against, plus their UUIDs.

    Full grouping path -- no existing groups in scope: run Pass 1 to
    propose them, persist as ``staged``, flush, return.

    Partial regrouping path -- existing groups present: skip Pass 1
    entirely, project the existing rows into :class:`GroupProposal`
    instances, return.
    """
    existing_groups = await load_existing_groups(
        session,
        product=triple.product,
        version=triple.version,
        impl_id=triple.impl_id,
        tenant_id=triple.tenant_id,
    )
    if existing_groups:
        _log.info(
            "llm_grouping_partial_regrouping",
            connector_id=triple.connector_id,
            existing_group_count=len(existing_groups),
            unassigned_op_count=len(unassigned_ops),
        )
        groups = [
            GroupProposal(
                group_key=row.group_key,
                name=row.name,
                when_to_use=row.when_to_use,
            )
            for row in existing_groups
        ]
        return groups, {row.group_key: row.id for row in existing_groups}

    _log.info(
        "llm_grouping_pass1_start",
        connector_id=triple.connector_id,
        op_count=len(unassigned_ops),
        min_groups=config.min_groups,
        max_groups=config.max_groups,
    )
    groups = await _propose_groups_via_llm(
        llm_client,
        triple=triple,
        operations=unassigned_ops,
        config=config,
        totals=totals,
    )
    _log.info(
        "llm_grouping_pass1_complete",
        connector_id=triple.connector_id,
        proposed_group_count=len(groups),
    )
    persisted_rows = _persist_proposed_groups(
        session,
        triple=triple,
        proposals=groups,
    )
    await session.flush()
    totals.groups_created = len(persisted_rows)
    return groups, {row.group_key: row.id for row in persisted_rows}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _build_result(triple: _ConnectorTriple, totals: _PhaseTotals) -> GroupingResult:
    """Project the orchestrator's mutable totals into the public result shape."""
    return GroupingResult(
        connector_id=triple.connector_id,
        groups_created=totals.groups_created,
        operations_assigned=totals.operations_assigned,
        operations_unassigned=totals.operations_unassigned,
        llm_call_count=totals.llm_call_count,
        llm_duration_ms=totals.llm_duration_ms,
    )


async def _write_grouping_audit_row(
    session: AsyncSession,
    *,
    operator_sub: str,
    operator_tenant_id: UUID,
    triple: _ConnectorTriple,
    totals: _PhaseTotals,
    config: GroupingConfig,
) -> None:
    """Emit the ``meho.connector.llm_grouping`` audit row for the pass."""
    await write_audit_row(
        session,
        operator_sub=operator_sub,
        operator_tenant_id=operator_tenant_id,
        op_id=OP_LLM_GROUPING,
        payload={
            "connector_id": triple.connector_id,
            "groups_created": totals.groups_created,
            "operations_assigned": totals.operations_assigned,
            "operations_unassigned": totals.operations_unassigned,
            "llm_call_count": totals.llm_call_count,
            "batch_size": config.batch_size,
        },
    )


async def _drive_two_pass_grouping(
    session: AsyncSession,
    llm_client: LlmClient,
    *,
    triple: _ConnectorTriple,
    unassigned_ops: Sequence[EndpointDescriptor],
    config: GroupingConfig,
    totals: _PhaseTotals,
) -> None:
    """Run Pass-1 + Pass-2 + apply ORM mutations against *session*.

    Side-effects only: persists :class:`OperationGroup` rows (Pass 1),
    sets ``group_id`` on each :class:`EndpointDescriptor` (Pass 2),
    and updates *totals* in place. The orchestrator commits after this
    returns so the entire pass is atomic.
    """
    groups, group_key_to_id = await _resolve_groups_for_pass2(
        session,
        llm_client,
        triple=triple,
        unassigned_ops=unassigned_ops,
        config=config,
        totals=totals,
    )
    assignment_map = await _assign_ops_in_batches(
        llm_client,
        triple=triple,
        operations=unassigned_ops,
        groups=groups,
        config=config,
        totals=totals,
    )
    _apply_assignments_to_rows(
        operations=unassigned_ops,
        assignment_map=assignment_map,
        group_key_to_id=group_key_to_id,
        totals=totals,
    )


async def run_llm_grouping(
    *,
    llm_client: LlmClient,
    operator_sub: str,
    operator_tenant_id: UUID,
    product: str,
    version: str,
    impl_id: str,
    tenant_id: UUID | None = None,
    batch_size: int = DEFAULT_GROUPING_BATCH_SIZE,
    min_groups: int = DEFAULT_MIN_GROUPS,
    max_groups: int = DEFAULT_MAX_GROUPS,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> GroupingResult:
    """Run the two-pass LLM grouping for an ingested connector.

    See the module docstring for the full behavioural contract.
    *operator_sub* / *operator_tenant_id* attribute the audit row;
    *product* / *version* / *impl_id* / *tenant_id* scope the rows
    grouped. *batch_size* / *min_groups* / *max_groups* tune the
    grouping run (see :class:`GroupingConfig`); *sessionmaker* is a
    test seam.

    Raises :class:`LlmOutputInvalid` when either pass returns output
    that fails schema validation.
    """  # code-quality-allow: orchestrator entry point keeps every knob explicit
    config = GroupingConfig(batch_size=batch_size, min_groups=min_groups, max_groups=max_groups)
    config.validate()
    triple = _ConnectorTriple(
        product=product,
        version=version,
        impl_id=impl_id,
        tenant_id=tenant_id,
    )
    log = _log.bind(
        connector_id=triple.connector_id,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
    )
    resolved_sessionmaker = sessionmaker if sessionmaker is not None else get_sessionmaker()
    totals = _PhaseTotals()

    async with resolved_sessionmaker() as session:
        unassigned_ops = await load_unassigned_ops(
            session,
            product=product,
            version=version,
            impl_id=impl_id,
            tenant_id=tenant_id,
        )
        if not unassigned_ops:
            log.info("llm_grouping_noop", reason="all_ops_already_grouped")
            return _build_result(triple, totals)

        await _drive_two_pass_grouping(
            session,
            llm_client,
            triple=triple,
            unassigned_ops=unassigned_ops,
            config=config,
            totals=totals,
        )
        await _write_grouping_audit_row(
            session,
            operator_sub=operator_sub,
            operator_tenant_id=operator_tenant_id,
            triple=triple,
            totals=totals,
            config=config,
        )
        await session.commit()

    log.info(
        "llm_grouping_complete",
        groups_created=totals.groups_created,
        operations_assigned=totals.operations_assigned,
        operations_unassigned=totals.operations_unassigned,
        llm_call_count=totals.llm_call_count,
        llm_duration_ms=totals.llm_duration_ms,
    )
    return _build_result(triple, totals)
