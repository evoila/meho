# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.operations.ingest.llm_groups`.

Coverage matrix (G0.7-T3 / Task #404 acceptance criteria):

* :func:`run_llm_grouping` -- happy-path two-pass run produces
  :class:`OperationGroup` rows with ``review_status='staged'``,
  non-empty ``when_to_use``, non-empty ``name``, and a
  :class:`GroupingResult` with the documented counts + LLM-call shape.
* Per-op assignment -- each ingested op gets ``group_id`` set (or stays
  NULL for the ``"none"`` sentinel path).
* Pass-1 output validation -- malformed JSON / wrong shape / non-snake-case
  ``group_key`` raises :class:`LlmOutputInvalid`.
* Pass-2 batching -- 50-op corpus triggers exactly ``1 + ceil(50/50) = 2``
  LLM calls; 100 ops with batch=50 trigger ``1 + 2 = 3``.
* Idempotency -- re-running on a fully-grouped connector is a no-op
  (zero LLM calls, zero rows mutated, no audit row).
* Partial regrouping -- re-running on a partially-grouped connector
  runs Pass-2-only over the unassigned rows, reusing existing groups.
* Audit row -- one ``meho.connector.llm_grouping`` row written with
  ``{connector_id, groups_created, operations_assigned,
  operations_unassigned, llm_call_count, batch_size}`` payload.
* Prompt rendering -- snapshot-style assertions on the rendered Jinja
  templates so silent prompt drift is caught at test time.

The chassis LLM client is stubbed via a fake :class:`LlmClient`
implementation whose ``generate_json`` returns canned responses from
the corpus fixtures. A real-LLM integration test stub lives at the
end of the file, guarded by an ``ANTHROPIC_API_KEY`` env check that
keeps the unit-test gate green in sandbox.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest import (
    GroupingResult,
    GroupProposal,
    LlmOutputInvalid,
    register_ingested_operations,
    run_llm_grouping,
)
from meho_backplane.operations.ingest._internals import (
    AUDIT_METHOD,
    OP_LLM_GROUPING,
)
from meho_backplane.operations.ingest._llm_grouping_internals import (
    ASSIGN_OPS_SYSTEM_PROMPT,
    DEFAULT_GROUPING_BATCH_SIZE,
    PROPOSE_GROUPS_SYSTEM_PROMPT,
    expected_llm_call_count,
    parse_assignment_response,
    parse_proposal_response,
    render_assign_ops_prompt,
    render_propose_groups_prompt,
    strip_code_fences,
)
from meho_backplane.settings import get_settings
from tests.fixtures.llm_groups import medium_corpus, small_corpus

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    """Reset the v2 connector registry between tests."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> Any:
    """An AsyncMock standing in for EmbeddingService."""
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class StubLlmClient:
    """Deterministic :class:`LlmClient` for unit tests.

    Returns a canned response per call site. The caller seeds the
    response list in construction order; each ``generate_json`` call
    pops the next entry. Excess calls (more than seeded) raise
    :class:`AssertionError` so a test that fires the LLM more often
    than expected fails loudly rather than silently looping.

    Records every call's (system_prompt, user_prompt, max_output_tokens)
    triple on :attr:`calls` so assertions on call count, prompt shape,
    and system-prompt stability are straightforward.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses: list[str] = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_output_tokens": max_output_tokens,
            },
        )
        if not self._responses:
            raise AssertionError(
                f"StubLlmClient: ran out of canned responses (call #{len(self.calls)})",
            )
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


_OPERATOR_SUB = "op-test-1"
_OPERATOR_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ingest_small_corpus(
    embedding_service: Any,
    *,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    spec_source: str = "vcenter.yaml",
) -> None:
    """Drive T2's bulk-upsert helper on the small fixture corpus.

    Convenience wrapper so each grouping test starts from a clean
    set of ingested rows without rebuilding the connector triple
    in every test body.
    """
    await register_ingested_operations(
        product=product,
        version=version,
        impl_id=impl_id,
        spec_source=spec_source,
        operations=small_corpus.PROTOS,
        embedding_service=embedding_service,
    )


async def _ingest_medium_corpus(embedding_service: Any) -> None:
    """Drive T2 on the medium 50-op corpus."""
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=medium_corpus.PROTOS,
        embedding_service=embedding_service,
    )


# ---------------------------------------------------------------------------
# Prompt rendering -- snapshot-style assertions
# ---------------------------------------------------------------------------


def test_render_propose_groups_prompt_contains_op_count_and_bounds() -> None:
    """Pass-1 prompt embeds op count + group-count bounds verbatim."""
    rendered = render_propose_groups_prompt(
        connector_id="vmware-rest-9.0",
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        operations=small_corpus.PROTOS,
        min_groups=8,
        max_groups=15,
    )
    assert "You will see 5 operations below" in rendered
    assert "between 8 and 15 operation GROUPS" in rendered
    assert "vmware-rest-9.0" in rendered
    # Every op_id appears in the rendered prompt.
    for proto in small_corpus.PROTOS:
        assert proto.op_id in rendered
    # Output schema instruction present and unambiguous.
    assert "Output ONLY a JSON array" in rendered


def test_render_propose_groups_prompt_handles_empty_tags() -> None:
    """Ops with no tags render as ``[tags: (none)]`` rather than crashing."""
    proto = medium_corpus.PROTOS[-1]
    assert proto.op_id == medium_corpus.EXPECTED_UNASSIGNED_OP_ID
    rendered = render_propose_groups_prompt(
        connector_id="vmware-rest-9.0",
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        operations=[proto],
    )
    assert "[tags: (none)]" in rendered


def test_render_assign_ops_prompt_lists_every_group() -> None:
    """Pass-2 prompt contains each group's key + when_to_use."""
    groups = parse_proposal_response(small_corpus.STUB_PROPOSE_RESPONSE)
    rendered = render_assign_ops_prompt(
        connector_id="vmware-rest-9.0",
        product="vmware",
        version="9.0",
        groups=groups,
        operations=small_corpus.PROTOS,
    )
    for group in groups:
        assert group.group_key in rendered
        # The first ~30 chars of when_to_use are enough to verify the
        # paragraph made it through without storing the full prose
        # twice in source.
        assert group.when_to_use[:30] in rendered
    assert "Output ONLY a JSON object" in rendered


# ---------------------------------------------------------------------------
# LLM-output parsing -- failure modes
# ---------------------------------------------------------------------------


def teststrip_code_fences_removes_json_fence() -> None:
    """The fenced-code wrapper the model sometimes emits is stripped cleanly."""
    raw = '```json\n[{"group_key": "x", "name": "X", "when_to_use": "y"}]\n```'
    cleaned = strip_code_fences(raw)
    assert cleaned.startswith("[")
    assert cleaned.endswith("]")


def teststrip_code_fences_preserves_unfenced_input() -> None:
    """Bare JSON passes through unchanged (modulo whitespace)."""
    raw = '  [{"group_key": "x", "name": "X", "when_to_use": "y"}]  '
    assert strip_code_fences(raw) == raw.strip()


def testparse_proposal_response_happy_path() -> None:
    """Stub corpus produces validated GroupProposal instances."""
    groups = parse_proposal_response(small_corpus.STUB_PROPOSE_RESPONSE)
    assert len(groups) == 2
    assert {g.group_key for g in groups} == {"inventory", "vm_lifecycle"}
    for group in groups:
        assert group.name.strip()
        assert group.when_to_use.strip()


def testparse_proposal_response_rejects_malformed_json() -> None:
    """Non-JSON output raises LlmOutputInvalid pinned to pass_name='propose_groups'."""
    with pytest.raises(LlmOutputInvalid) as excinfo:
        parse_proposal_response("this is not JSON at all")
    assert excinfo.value.pass_name == "propose_groups"
    assert "this is not JSON at all" in excinfo.value.raw_output


def testparse_proposal_response_rejects_non_array() -> None:
    """A JSON object at the top level (instead of array) raises."""
    with pytest.raises(LlmOutputInvalid) as excinfo:
        parse_proposal_response('{"group_key": "x"}')
    assert "expected a JSON array" in repr(excinfo.value)


def testparse_proposal_response_rejects_invalid_group_key() -> None:
    """A non-snake-case group_key fails GroupProposal validation."""
    bad = json.dumps(
        [
            {
                "group_key": "Bad-Key",
                "name": "Bad",
                "when_to_use": "irrelevant",
            },
        ],
    )
    with pytest.raises(LlmOutputInvalid) as excinfo:
        parse_proposal_response(bad)
    # The chained ValidationError surfaces on .parse_error.
    assert excinfo.value.parse_error is not None


def testparse_proposal_response_rejects_duplicate_group_key() -> None:
    """Two entries sharing a group_key raise before any DB write."""
    bad = json.dumps(
        [
            {
                "group_key": "x",
                "name": "X",
                "when_to_use": "first",
            },
            {
                "group_key": "x",
                "name": "Y",
                "when_to_use": "second",
            },
        ],
    )
    with pytest.raises(LlmOutputInvalid) as excinfo:
        parse_proposal_response(bad)
    assert "duplicate group_key" in repr(excinfo.value)


def testparse_proposal_response_rejects_missing_when_to_use() -> None:
    """Pass-1 output must carry a non-empty ``when_to_use``."""
    bad = json.dumps(
        [
            {
                "group_key": "x",
                "name": "X",
                "when_to_use": "",
            },
        ],
    )
    with pytest.raises(LlmOutputInvalid):
        parse_proposal_response(bad)


def testparse_assignment_response_coerces_unknown_group_to_none() -> None:
    """An op assigned to an unknown group_key is silently coerced to ``"none"``.

    The parser logs a warning but the operator-facing flow handles
    unknown-group as "leave for manual review" rather than aborting.
    """
    raw = json.dumps(
        {
            "op-1": "real_group",
            "op-2": "ghost_group",
            "op-3": "none",
        },
    )
    mapping = parse_assignment_response(
        raw,
        valid_op_ids={"op-1", "op-2", "op-3"},
        valid_group_keys={"real_group"},
    )
    assert mapping == {"op-1": "real_group", "op-2": "none", "op-3": "none"}


def testparse_assignment_response_drops_unknown_op_id() -> None:
    """An op_id the parser doesn't recognise is silently dropped."""
    raw = json.dumps({"op-1": "g1", "rogue": "g1"})
    mapping = parse_assignment_response(
        raw,
        valid_op_ids={"op-1"},
        valid_group_keys={"g1"},
    )
    assert mapping == {"op-1": "g1"}


def testparse_assignment_response_rejects_non_object() -> None:
    """Top-level array (instead of object) raises."""
    with pytest.raises(LlmOutputInvalid) as excinfo:
        parse_assignment_response(
            "[]",
            valid_op_ids=set(),
            valid_group_keys=set(),
        )
    assert excinfo.value.pass_name == "assign_ops"


# ---------------------------------------------------------------------------
# expected_llm_call_count contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_count", "batch_size", "expected"),
    [
        (50, 50, 2),  # vCenter median path; AC explicit
        (100, 50, 3),
        (1000, 50, 21),
        (5, 50, 2),
        (961, 50, 21),  # full vCenter REST
        (1, 50, 2),
        (0, 50, 0),
    ],
)
def test_expected_llm_call_count(
    op_count: int,
    batch_size: int,
    expected: int,
) -> None:
    """1 + ceil(op_count / batch_size) -- the documented call-count rule."""
    assert expected_llm_call_count(op_count, batch_size) == expected


# ---------------------------------------------------------------------------
# run_llm_grouping -- end-to-end happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_llm_grouping_small_corpus_persists_groups_and_assigns_ops(
    stub_embedding_service: Any,
) -> None:
    """Happy path: Pass-1 + Pass-2 + audit row + ORM mutations."""
    await _ingest_small_corpus(stub_embedding_service)

    stub = StubLlmClient(
        responses=[
            small_corpus.STUB_PROPOSE_RESPONSE,
            small_corpus.STUB_ASSIGNMENT_RESPONSE,
        ],
    )
    result = await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )

    assert isinstance(result, GroupingResult)
    assert result.connector_id == "vmware-rest-9.0"
    assert result.groups_created == 2
    assert result.operations_assigned == 5
    assert result.operations_unassigned == 0
    assert result.llm_call_count == 2
    assert result.llm_duration_ms >= 0.0

    # Two LLM calls -- one Pass 1, one Pass 2 (5 ops fit in one batch).
    assert stub.call_count == 2
    assert "Output ONLY a JSON array" in stub.calls[0]["user_prompt"]
    assert "Output ONLY a JSON object" in stub.calls[1]["user_prompt"]

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        groups = (
            (
                await fresh.execute(
                    select(OperationGroup)
                    .where(OperationGroup.product == "vmware")
                    .order_by(OperationGroup.group_key)
                )
            )
            .scalars()
            .all()
        )
        assert len(groups) == 2
        for group in groups:
            assert group.review_status == "staged"
            assert group.when_to_use.strip()
            assert group.name.strip()
            assert group.tenant_id is None
        group_id_by_key = {g.group_key: g.id for g in groups}

        ops = (
            (
                await fresh.execute(
                    select(EndpointDescriptor)
                    .where(EndpointDescriptor.product == "vmware")
                    .order_by(EndpointDescriptor.op_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(ops) == 5
        assigned_by_op_id = {op.op_id: op.group_id for op in ops}
        for op_id, expected_key in small_corpus.EXPECTED_ASSIGNMENT_BY_OP_ID.items():
            assert assigned_by_op_id[op_id] == group_id_by_key[expected_key]


@pytest.mark.asyncio
async def test_run_llm_grouping_writes_audit_row(
    stub_embedding_service: Any,
) -> None:
    """One ``meho.connector.llm_grouping`` audit row with the documented payload."""
    await _ingest_small_corpus(stub_embedding_service)
    stub = StubLlmClient(
        responses=[
            small_corpus.STUB_PROPOSE_RESPONSE,
            small_corpus.STUB_ASSIGNMENT_RESPONSE,
        ],
    )
    await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == OP_LLM_GROUPING)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.method == AUDIT_METHOD
    assert row.operator_sub == _OPERATOR_SUB
    assert row.tenant_id == _OPERATOR_TENANT
    assert row.status_code == 200
    payload = row.payload
    assert isinstance(payload, dict)
    assert payload["connector_id"] == "vmware-rest-9.0"
    assert payload["groups_created"] == 2
    assert payload["operations_assigned"] == 5
    assert payload["operations_unassigned"] == 0
    assert payload["llm_call_count"] == 2
    assert payload["batch_size"] == DEFAULT_GROUPING_BATCH_SIZE


@pytest.mark.asyncio
async def test_run_llm_grouping_omitted_ops_counted_once(
    stub_embedding_service: Any,
) -> None:
    """Regression for CodeRabbit B1 (PR #485).

    The Pass-2 stub deliberately omits two of the five small-corpus
    op_ids from its response. The buggy ``_apply_assignments_to_rows``
    counted omitted ops twice: once via the ``NONE_GROUP_KEY`` default
    in the per-row loop and again via a trailing
    ``missing = all_op_ids - assignment_map.keys()`` block, inflating
    ``operations_unassigned`` and breaking the
    ``assigned + unassigned == len(operations)`` reconciliation
    invariant the audit row depends on.

    With the fix, omitted ops are counted exactly once. For a 5-op
    corpus with 3 assigned and 2 omitted, ``operations_unassigned``
    must equal 2 (not 4), and the audit-row payload must match.
    """
    await _ingest_small_corpus(stub_embedding_service)

    # Pass-2 response omits the two DELETE/snapshot op_ids entirely.
    # The parser keeps the three present-and-valid entries; the
    # remaining two ops should be counted as unassigned exactly once.
    partial_assignment_response = json.dumps(
        {
            "GET:/api/vcenter/cluster": "inventory",
            "GET:/api/vcenter/vm": "inventory",
            "POST:/api/vcenter/vm": "vm_lifecycle",
            # "DELETE:/api/vcenter/vm/{vm}" -- omitted
            # "POST:/api/vcenter/vm/{vm}/snapshot" -- omitted
        },
    )
    stub = StubLlmClient(
        responses=[
            small_corpus.STUB_PROPOSE_RESPONSE,
            partial_assignment_response,
        ],
    )
    result = await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )

    # Core invariant: each op is counted exactly once.
    assert result.operations_assigned + result.operations_unassigned == len(
        small_corpus.PROTOS,
    )
    assert result.operations_assigned == 3
    # Without the fix, this would be 4 (the two omitted ops are
    # counted once via the per-row loop's NONE_GROUP_KEY default and
    # again via the trailing all_op_ids/missing block).
    assert result.operations_unassigned == 2

    # Audit-row payload reflects the same (correct) total.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == OP_LLM_GROUPING)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].payload
    assert isinstance(payload, dict)
    assert payload["operations_assigned"] == 3
    assert payload["operations_unassigned"] == 2
    assert payload["operations_assigned"] + payload["operations_unassigned"] == len(
        small_corpus.PROTOS
    )

    # Per-row ORM mutations also follow the same invariant: only the
    # three assigned ops carry a group_id; the two omitted rows stay
    # NULL (unassigned).
    async with sessionmaker() as fresh:
        ops = (
            (
                await fresh.execute(
                    select(EndpointDescriptor)
                    .where(EndpointDescriptor.product == "vmware")
                    .order_by(EndpointDescriptor.op_id)
                )
            )
            .scalars()
            .all()
        )
    assigned_count = sum(1 for op in ops if op.group_id is not None)
    unassigned_count = sum(1 for op in ops if op.group_id is None)
    assert assigned_count == 3
    assert unassigned_count == 2


# ---------------------------------------------------------------------------
# Medium corpus -- AC-pinned batching + unassigned-op path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_llm_grouping_medium_corpus_two_llm_calls(
    stub_embedding_service: Any,
) -> None:
    """50 ops + batch_size=50 -> exactly 2 LLM calls (1 + ceil(50/50))."""
    await _ingest_medium_corpus(stub_embedding_service)

    stub = StubLlmClient(
        responses=[
            medium_corpus.STUB_PROPOSE_RESPONSE,
            medium_corpus.STUB_ASSIGNMENT_RESPONSE,
        ],
    )
    result = await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )
    assert result.llm_call_count == 2
    assert stub.call_count == 2
    assert result.groups_created == 8
    assert result.operations_assigned == 49
    assert result.operations_unassigned == 1

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        groups = (
            (await fresh.execute(select(OperationGroup).where(OperationGroup.product == "vmware")))
            .scalars()
            .all()
        )
        assert {g.group_key for g in groups} == set(medium_corpus.EXPECTED_GROUP_KEYS)
        unassigned = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == medium_corpus.EXPECTED_UNASSIGNED_OP_ID,
                )
            )
        ).scalar_one()
        assert unassigned.group_id is None


@pytest.mark.asyncio
async def test_run_llm_grouping_medium_corpus_with_smaller_batch(
    stub_embedding_service: Any,
) -> None:
    """batch_size=25 over 50 ops -> 1 + ceil(50/25) = 3 LLM calls.

    The two Pass-2 batches each see half the ops; the parser merges
    the two response dicts into the final assignment map. Each batch's
    response covers exactly the op_ids in that batch.
    """
    await _ingest_medium_corpus(stub_embedding_service)

    # Split the stub assignment response across two batches based on
    # op_id sort order (the production code sorts ops by op_id before
    # batching, so this matches what the real call would see).
    full_assignments: dict[str, str] = json.loads(medium_corpus.STUB_ASSIGNMENT_RESPONSE)
    ordered_op_ids = sorted(op.op_id for op in medium_corpus.PROTOS)
    batch_a_ids = set(ordered_op_ids[:25])
    batch_b_ids = set(ordered_op_ids[25:])
    batch_a = {k: v for k, v in full_assignments.items() if k in batch_a_ids}
    batch_b = {k: v for k, v in full_assignments.items() if k in batch_b_ids}

    stub = StubLlmClient(
        responses=[
            medium_corpus.STUB_PROPOSE_RESPONSE,
            json.dumps(batch_a),
            json.dumps(batch_b),
        ],
    )
    result = await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        batch_size=25,
    )
    assert result.llm_call_count == 3
    assert stub.call_count == 3
    assert result.operations_assigned == 49
    assert result.operations_unassigned == 1


# ---------------------------------------------------------------------------
# Idempotency + partial regrouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_llm_grouping_noop_on_fully_grouped_connector(
    stub_embedding_service: Any,
) -> None:
    """A second invocation with no unassigned rows is a true no-op."""
    await _ingest_small_corpus(stub_embedding_service)
    stub = StubLlmClient(
        responses=[
            small_corpus.STUB_PROPOSE_RESPONSE,
            small_corpus.STUB_ASSIGNMENT_RESPONSE,
        ],
    )
    await run_llm_grouping(
        llm_client=stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )
    assert stub.call_count == 2  # 1 + 1

    # Second invocation -- nothing to do.
    second_stub = StubLlmClient(responses=[])
    second_result = await run_llm_grouping(
        llm_client=second_stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )
    assert second_result.llm_call_count == 0
    assert second_result.groups_created == 0
    assert second_result.operations_assigned == 0
    assert second_result.operations_unassigned == 0
    assert second_stub.call_count == 0

    # No second audit row was written.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditLog).where(AuditLog.path == OP_LLM_GROUPING)))
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_run_llm_grouping_partial_regrouping_runs_pass2_only(
    stub_embedding_service: Any,
) -> None:
    """Re-running on a partially-grouped connector skips Pass 1.

    Setup: ingest the small corpus + group it. Then ingest one extra op
    (the partial-state simulation) and re-run grouping. The Pass-1 step
    must be skipped because existing groups are present; Pass-2 runs
    only on the new op.
    """
    await _ingest_small_corpus(stub_embedding_service)
    first_stub = StubLlmClient(
        responses=[
            small_corpus.STUB_PROPOSE_RESPONSE,
            small_corpus.STUB_ASSIGNMENT_RESPONSE,
        ],
    )
    await run_llm_grouping(
        llm_client=first_stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )

    # Inject one extra ingested op under the same connector, leaving
    # group_id=NULL -- this matches what T2 does for any new spec
    # operation before T3 has run.
    new_op = small_corpus.make_protos()[0].model_copy(
        update={
            "op_id": "GET:/api/vcenter/cluster-extra",
            "path": "/api/vcenter/cluster-extra",
            "summary": "List extra cluster",
        },
    )
    await register_ingested_operations(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        spec_source="vcenter.yaml",
        operations=[new_op],
        embedding_service=stub_embedding_service,
    )

    # Stub only Pass-2; Pass-1 should not fire.
    partial_stub = StubLlmClient(
        responses=[
            json.dumps({"GET:/api/vcenter/cluster-extra": "inventory"}),
        ],
    )
    result = await run_llm_grouping(
        llm_client=partial_stub,
        operator_sub=_OPERATOR_SUB,
        operator_tenant_id=_OPERATOR_TENANT,
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
    )
    assert partial_stub.call_count == 1, "Pass-1 should be skipped when existing groups are present"
    assert result.llm_call_count == 1
    assert result.groups_created == 0  # no new groups
    assert result.operations_assigned == 1
    assert result.operations_unassigned == 0

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        groups = (
            (await fresh.execute(select(OperationGroup).where(OperationGroup.product == "vmware")))
            .scalars()
            .all()
        )
        assert len(groups) == 2  # unchanged from the first run

        new_row = (
            await fresh.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "GET:/api/vcenter/cluster-extra",
                )
            )
        ).scalar_one()
        assert new_row.group_id is not None


# ---------------------------------------------------------------------------
# Validation / argument-shape failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_llm_grouping_rejects_bad_min_max_groups(
    stub_embedding_service: Any,
) -> None:
    """Bad bounds raise ValueError before any LLM call."""
    await _ingest_small_corpus(stub_embedding_service)
    stub = StubLlmClient(responses=[])
    with pytest.raises(ValueError, match="min_groups"):
        await run_llm_grouping(
            llm_client=stub,
            operator_sub=_OPERATOR_SUB,
            operator_tenant_id=_OPERATOR_TENANT,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            min_groups=0,
        )
    with pytest.raises(ValueError, match="max_groups"):
        await run_llm_grouping(
            llm_client=stub,
            operator_sub=_OPERATOR_SUB,
            operator_tenant_id=_OPERATOR_TENANT,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            min_groups=10,
            max_groups=5,
        )
    with pytest.raises(ValueError, match="batch_size"):
        await run_llm_grouping(
            llm_client=stub,
            operator_sub=_OPERATOR_SUB,
            operator_tenant_id=_OPERATOR_TENANT,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            batch_size=0,
        )
    assert stub.call_count == 0


@pytest.mark.asyncio
async def test_run_llm_grouping_surfaces_pass1_failure(
    stub_embedding_service: Any,
) -> None:
    """Pass-1 malformed output bubbles up as LlmOutputInvalid.

    The function aborts before Pass-2 fires (the test only seeds one
    response; a Pass-2 call would raise StubLlmClient's
    out-of-responses assertion, which is exactly the contract we want).
    """
    await _ingest_small_corpus(stub_embedding_service)
    stub = StubLlmClient(responses=["not json"])
    with pytest.raises(LlmOutputInvalid) as excinfo:
        await run_llm_grouping(
            llm_client=stub,
            operator_sub=_OPERATOR_SUB,
            operator_tenant_id=_OPERATOR_TENANT,
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
        )
    assert excinfo.value.pass_name == "propose_groups"
    assert stub.call_count == 1


# ---------------------------------------------------------------------------
# Frozen GroupProposal -- defence-in-depth
# ---------------------------------------------------------------------------


def test_group_proposal_is_frozen() -> None:
    """Re-assigning a GroupProposal field raises ValidationError."""
    from pydantic import ValidationError

    proposal = GroupProposal(
        group_key="x",
        name="X",
        when_to_use="something useful here",
    )
    with pytest.raises(ValidationError):
        proposal.group_key = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Real-LLM integration test (opt-in via env)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="real-LLM integration test; requires ANTHROPIC_API_KEY",
)
async def test_run_llm_grouping_with_real_claude_haiku(
    stub_embedding_service: Any,
) -> None:
    """Integration sanity: run against real Claude Haiku on the medium corpus.

    Opt-in: skipped in sandbox + CI. Operators / engineers verifying
    prompt quality run this manually with their own API key. The
    assertion is the *shape* contract -- the response validates, every
    op gets either a group_id or a recorded "none" -- not specific
    group choices, which would be model-version dependent.

    Note: this test exercises only the public surface; it does not
    pull the Anthropic SDK at import time. A concrete LlmClient
    backed by ``anthropic.AsyncAnthropic`` does **not** yet ship in
    the chassis -- ``set_llm_client_factory`` is the wire-up seam
    but FastAPI lifespan startup has no caller for it (see
    ``docs/codebase/spec-ingestion.md`` §"LLM-client wiring (build-
    time-only today)" + G0.18-T7 #1360). For v0.2 this test stays as
    a manual sanity hook.
    """
    pytest.skip(
        "real-LLM integration adapter not yet wired; "
        "no production LlmClient ships in the chassis "
        "(set_llm_client_factory has no lifespan caller). "
        "Tracked under G0.18-T7 #1360.",
    )


# ---------------------------------------------------------------------------
# Regression -- system prompts are stable strings (cacheable prefix)
# ---------------------------------------------------------------------------


def test_system_prompts_are_pure_text_no_dynamic_substitution() -> None:
    """System prompts must not contain Jinja-style template tags.

    The AI-eng-pack 'Prompt caching' rule: cache the stable prefix.
    A system prompt that interpolates per-call values breaks prompt
    caching on every call. Keep the dynamism in the user-prompt body
    (rendered by the Jinja templates); the system prompt is verbatim
    text.
    """
    template_tag = re.compile(r"\{\{|\{%")
    assert not template_tag.search(PROPOSE_GROUPS_SYSTEM_PROMPT)
    assert not template_tag.search(ASSIGN_OPS_SYSTEM_PROMPT)
    # Both system prompts must explicitly forbid prose-around-JSON.
    assert "ONLY" in PROPOSE_GROUPS_SYSTEM_PROMPT
    assert "ONLY" in ASSIGN_OPS_SYSTEM_PROMPT
