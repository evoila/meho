# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end CI exercise for the ``examples/kb_writeback`` sample.

The sample's contract (#1081) is the closed-loop pattern: an
investigation agent produces a structured finding, the harness
persists it to the tenant kb, and a follow-up retrieval finds it.
This module proves that loop closes end to end against a real
``pgvector/pgvector:pg16`` container -- the same testcontainer the
sibling :mod:`tests.integration.test_kb_service_pg` runs the kb
primitives against -- so the sample stays runnable as MEHO evolves.

Why this test lives under ``backend/tests/integration/``
=========================================================

The ``examples/kb_writeback`` package ships outside ``backend/``'s
wheel layout (the consumer-visible repo-root ``examples/`` tree
is a documentation surface, not a Python distribution). The
integration suite already runs from ``backend/`` against the
``pg_engine`` fixture; pulling the sample into this directory's
test run costs nothing beyond an ``sys.path`` extension at module
load time and avoids a second ``pytest`` invocation in CI.

Wire-up shape
=============

The test imports the sample at module load by walking up to the
repo root and adding ``examples/`` to ``sys.path``. ``importlib``
is the standard recipe; ``sys.path.insert`` after locating the
parent keeps the example tree usable from the bare ``python
-m examples.kb_writeback.workflow`` shape as well.

The agent runtime is a :class:`PydanticAgentRun` wired to a
:class:`~pydantic_ai.models.function.FunctionModel` whose callback
emits the sample's `Finding` directly via the framework's
structured-output tool. No real LLM is called; the run is
deterministic and offline. (The sample's *production* path runs
through the same seam against a real Anthropic / OpenAI / VCF PAIF
backend; the test stubs the model factory, not the seam.)

The embedding service is stubbed with the same per-token vector
helper :mod:`tests.integration.test_kb_service_pg` uses so the
retrieval call ranks by deterministic terms; cold ONNX loads
would add ~10 s of wall clock to a sub-second test for no useful
coverage (the real embedding pipeline has its own dedicated
suite, :mod:`tests.test_retrieval_embedding`).
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent import PydanticAgentRun
from meho_backplane.auth.operator import Operator, TenantRole

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

# Locate the repo root so the example package is importable. The
# integration test is in ``backend/tests/integration/`` so the repo
# root is three parents up. Inserting at position 0 wins against a
# stale install on the path (rare in CI, common in dev).
_REPO_ROOT: Path = Path(__file__).resolve().parents[3]
_EXAMPLES_ROOT: Path = _REPO_ROOT / "examples"
if str(_EXAMPLES_ROOT) not in sys.path:  # idempotent across module reloads
    sys.path.insert(0, str(_EXAMPLES_ROOT))


# Import the sample modules. Placed after the path manipulation so
# the import is unambiguous; ruff E402 is the cost.
from kb_writeback.agent_definitions import (  # noqa: E402
    INVESTIGATION_AGENT,
    Finding,
)
from kb_writeback.workflow import (  # noqa: E402
    PROVENANCE_METADATA_KEY,
    PROVENANCE_METADATA_VALUE,
    run_closed_loop,
)

# Pinned tenant UUIDs the ``pg_engine`` fixture seeds. Mirror
# :mod:`tests.integration.test_kb_service_pg` so the two suites can
# share interpretation of the seed rows.
_TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
_TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _make_operator(tenant_id: str) -> Operator:
    """Build a minimal :class:`Operator` for the given tenant.

    The sample's run is driven by an operator with ``operator`` role;
    no other RBAC discipline is exercised because the kb create flow
    is the only mutation the loop performs and ``KbService`` itself
    does not enforce role (the route layer would, but this sample
    calls the service directly).
    """
    return Operator(
        sub=f"op-kb-writeback-{tenant_id}",
        name="KB Writeback Sample",
        email=None,
        raw_jwt="<integration-test-raw-jwt>",
        tenant_id=uuid.UUID(tenant_id),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_stub_embedding_vector(text: str) -> list[float]:
    """Deterministic bag-of-words 384-dim vector keyed by token hashes.

    Same shape :mod:`tests.integration.test_kb_service_pg` uses --
    each token contributes to two slots (its hash modulo 384, plus
    a hash*31-seeded slot). Unit-normalised so cosine scores fall
    in (0, 1). ``hashlib.blake2b`` rather than the builtin ``hash``
    so ``PYTHONHASHSEED`` does not introduce per-run variation in
    ranking order.
    """
    v = [0.0] * 384
    for token in text.lower().split():
        h = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(),
            "big",
        )
        v[h % 384] += 1.0
        v[(h * 31) % 384] += 0.5
    magnitude = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / magnitude for x in v]


def _make_stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` whose encode methods return per-token vectors."""
    fake = AsyncMock()
    fake.encode_one.side_effect = lambda t: _make_stub_embedding_vector(t)
    fake.encode.side_effect = lambda ts: [_make_stub_embedding_vector(t) for t in ts]
    fake.dimension = 384
    return fake


# A canonical sample symptom + expected finding. The Finding shape is
# what the FunctionModel "produces" -- distinctive evidence tokens
# (e.g. "yodayodayoda-quiesce") so the follow-up retrieval can be
# scored against a query that no real corpus entry would match.
_SYMPTOM: str = (
    "vCenter 9.0 snapshot revert failing with quiesced-disk timeout on tenant-a Aria signals."
)
_EXPECTED_FINDING: Finding = Finding(
    subject="vCenter 9.0 snapshot revert quiesce timeout",
    summary=(
        "The 9.0 release tightened the VMware Tools quiesce handshake; "
        "VMs whose guest agent is slow to acknowledge the freeze land in "
        "a yodayodayoda-quiesce loop until the operator bypasses quiesce "
        "or restarts the guest tools service."
    ),
    evidence=[
        "yodayodayoda-quiesce-loop",
        "VMware Tools 12.5.0 handshake",
        "snapshot.create.failed event",
    ],
    slug="vcenter-9.0-snapshot-quiesce",
)


def _function_model_emits_finding(finding: Finding) -> FunctionModel:
    """Build a deterministic model that emits *finding* on the first turn.

    The framework's structured-output contract is implemented by the
    model calling the final-result tool with arguments that match the
    output schema; ``info.output_tools[0].name`` is the
    framework-assigned tool name for the schema (typically
    ``final_result``). This callback returns that tool call straight
    away -- the example's investigation agent budget is 3 turns, so
    a one-turn answer comfortably fits.
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages  # unused -- always answer on turn 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    finding.model_dump(mode="json"),
                )
            ]
        )

    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# Test 1 -- the loop closes end to end
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_closed_loop_writes_and_retrieves_finding(
    pg_engine: None,
) -> None:
    """The full investigation -> kb write -> retrieval pattern closes.

    Asserts each of the three load-bearing properties the sample is
    documenting:

    * The investigation agent produces the structured :class:`Finding`.
    * :func:`persist_finding_to_kb` lands the finding in the kb under
      the operator's tenant with the right slug, body shape, and
      provenance metadata.
    * :func:`retrieve_as_context` returns the just-written finding as
      a top-ranked hit when queried with terms from its evidence.
    """
    operator = _make_operator(_TENANT_A_ID)
    runtime = PydanticAgentRun(
        model_factory=lambda: _function_model_emits_finding(_EXPECTED_FINDING),
    )
    fake_embedding = _make_stub_embedding_service()

    with (
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embedding,
        ),
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding,
        ),
    ):
        result = await run_closed_loop(
            operator=operator,
            symptom=_SYMPTOM,
            follow_up_query="yodayodayoda-quiesce handshake snapshot",
            runtime=runtime,
        )

    # Phase 1 -- the finding came through unchanged.
    assert result.finding == _EXPECTED_FINDING

    # Phase 2 -- the entry landed under the right tenant + slug, and
    # the provenance marker is on its metadata for later sweeps.
    assert result.entry.tenant_id == uuid.UUID(_TENANT_A_ID)
    assert result.entry.slug == _EXPECTED_FINDING.slug
    assert result.entry.metadata.get(PROVENANCE_METADATA_KEY) == PROVENANCE_METADATA_VALUE
    # The body contains the subject + the summary + the evidence
    # tokens; we don't assert verbatim shape (that's the renderer's
    # contract) but every part should be present.
    body = result.entry.body
    assert _EXPECTED_FINDING.subject in body
    for evidence_line in _EXPECTED_FINDING.evidence:
        assert evidence_line in body

    # Phase 3 -- retrieval ranks the freshly-written entry in the top
    # 3 against a query that quotes evidence tokens. The default
    # follow_up_query overlaps with the evidence on purpose so the
    # ranking is deterministic; a query with no overlap would test
    # the empty-result path, not the closed-loop path.
    top_3_slugs = [hit.slug for hit in result.retrieval_hits[:3]]
    assert _EXPECTED_FINDING.slug in top_3_slugs


# ---------------------------------------------------------------------------
# Test 2 -- tenant boundary holds (write under A, read under B)
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_retrieval_does_not_cross_tenant_boundary(
    pg_engine: None,
) -> None:
    """A finding written under tenant A is invisible to tenant B's retrieval.

    The kb substrate enforces tenant scoping at SQL level via the
    ``documents.tenant_id`` filter; this test proves the sample's
    closed-loop helpers respect the boundary. The two seeded tenants
    (``11111111-...`` and ``22222222-...``) come from the
    ``pg_engine`` fixture's seed step.
    """
    operator_a = _make_operator(_TENANT_A_ID)
    operator_b = _make_operator(_TENANT_B_ID)
    runtime = PydanticAgentRun(
        model_factory=lambda: _function_model_emits_finding(_EXPECTED_FINDING),
    )
    fake_embedding = _make_stub_embedding_service()

    with (
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embedding,
        ),
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding,
        ),
    ):
        # Tenant A runs the closed loop; the finding lands in tenant
        # A's kb. We do not need the structured result here -- the
        # write side-effect is what the next call probes.
        await run_closed_loop(
            operator=operator_a,
            symptom=_SYMPTOM,
            follow_up_query="yodayodayoda-quiesce handshake snapshot",
            runtime=runtime,
        )

        # Reuse the helper for the read-only retrieval; tenant B's
        # search returns no hits because the entry was written under
        # tenant A.
        from kb_writeback.workflow import retrieve_as_context

        b_hits = await retrieve_as_context(
            operator=operator_b,
            query="yodayodayoda-quiesce handshake snapshot",
        )

    assert b_hits == []


# ---------------------------------------------------------------------------
# Test 3 -- module import smoke (runs without Docker)
# ---------------------------------------------------------------------------


def test_example_modules_import_cleanly() -> None:
    """Sanity: every symbol the sample documents resolves and types out.

    Runs even without Docker so a smoke-only CI lane catches the case
    of the example tree being moved / renamed without the integration
    cluster spinning up.
    """
    # Every imported symbol exists and is the right kind of value.
    assert INVESTIGATION_AGENT.name == "kb-writeback-investigator"
    assert INVESTIGATION_AGENT.output_type is Finding
    assert callable(run_closed_loop)
    assert PROVENANCE_METADATA_KEY == "source"
    assert PROVENANCE_METADATA_VALUE == "kb-writeback-sample"
