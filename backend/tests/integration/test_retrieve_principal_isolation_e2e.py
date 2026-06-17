# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end proof of per-principal memory isolation on ``retrieve()`` (#1797).

SEV-1 regression suite for the within-tenant cross-principal memory leak:
``retrieve(source="memory")`` used to return another principal's
``user`` / ``user-tenant`` / ``user-target`` rows (full ``body``) to any
caller in the same tenant, on the two raw callers that skipped the
per-principal ``user_sub`` push-down -- ``POST /api/v1/retrieve`` and the
``meho://retrieve/{query}`` MCP resource.

These tests run against a **real pgvector cluster** (testcontainers,
Docker-gated like the rest of ``tests/integration/``) because the leak is
a property of the actual ``documents.metadata ->> 'user_sub'`` SQL
predicate, not the in-process wiring (that is pinned in the always-on
:mod:`tests.test_retrieve_isolation`). Two distinct principals (A and B)
are minted in the **same tenant**; each writes a private canary in every
user-scoped scope, A also writes a tenant-broadcast canary, and the
probes assert:

* **Bidirectional isolation** -- A's user-scoped canaries never surface
  to B and vice-versa, through the substrate, the HTTP route, and the MCP
  resource.
* **MCP resource isolation** -- the resource (which retrieves across
  *every* source with no ``metadata_filters``) does not leak memory rows.
* **Non-overridable** -- B passing ``metadata_filters={"user_sub":
  "<A's sub>"}`` still gets none of A's rows (the enforced predicate
  wins).
* **No over-correction** -- A's ``tenant``-broadcast canary IS visible to
  B (broadcast scopes are tenant-wide by design).

Embedding is mocked (deterministic bag-of-words vectors) so the suite
runs in ~2 s rather than paying the fastembed cold-load cost; the canary
bodies share enough tokens with the probe query that both RRF signals
return them, so an absence in the result set is a real isolation pass,
not a ranking miss.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

# Same tenant for both principals -- the leak is *within* a tenant.
# Matches the seed row ``pg_engine`` inserts.
TENANT_ID: str = "11111111-1111-1111-1111-111111111111"

# Two distinct Keycloak-shaped principal subs in that one tenant.
PRINCIPAL_A_SUB: str = "00000000-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRINCIPAL_B_SUB: str = "00000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

# A probe query whose tokens appear in every canary body so both BM25 and
# cosine return the canaries -- absence in the result is then a genuine
# isolation pass rather than a retrieval miss.
PROBE_QUERY: str = "kubernetes vault rotation runbook canary"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _operator(sub: str, *, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    """Build an :class:`Operator` for *sub* in the shared test tenant."""
    return Operator(
        sub=sub,
        name=f"principal-{sub[:4]}",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid.UUID(TENANT_ID),
        tenant_role=role,
    )


def _stub_vector(text: str) -> list[float]:
    """Deterministic 384-dim bag-of-words vector (process-stable).

    Mirrors :func:`tests.integration.test_retrieval_e2e._make_stub_embedding_vector`
    so the canaries rank meaningfully against :data:`PROBE_QUERY` without
    the fastembed cold-load cost. ``blake2b`` (not the salted builtin
    ``hash``) keeps slot assignment stable across CI runs.
    """
    v = [0.0] * 384
    for token in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        v[h % 384] += 1.0
        v[(h * 31) % 384] += 0.5
    magnitude = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / magnitude for x in v]


def _stub_embedding_service() -> AsyncMock:
    fake = AsyncMock()
    fake.encode_one.side_effect = lambda t: _stub_vector(t)
    fake.encode.side_effect = lambda ts: [_stub_vector(t) for t in ts]
    fake.dimension = 384
    return fake


# Canary bodies share the probe tokens; the trailing marker makes each
# row identifiable in an assertion failure.
def _canary_body(owner: str, scope: MemoryScope) -> str:
    return (
        f"kubernetes vault rotation runbook canary owned by {owner} "
        f"in scope {scope.value} -- secret body that must stay private"
    )


@pytest.fixture
async def seeded_memories(pg_engine: None) -> AsyncIterator[dict[str, Any]]:
    """Write per-principal canaries for A and B plus a broadcast canary by A.

    Uses the real :meth:`MemoryService.remember` write path so the
    rows carry a correctly-stamped ``user_sub`` in ``doc_metadata`` --
    the exact field the boundary predicate gates on. Embedding is patched
    on both the indexer and retriever import sites so the write + read
    legs share the same deterministic vectors.
    """
    fake = _stub_embedding_service()
    service = MemoryService()
    op_a = _operator(PRINCIPAL_A_SUB)
    op_b = _operator(PRINCIPAL_B_SUB)

    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        # A's private canaries, one per user-scoped scope.
        await service.remember(op_a, MemoryScope.USER, _canary_body("A", MemoryScope.USER))
        await service.remember(
            op_a, MemoryScope.USER_TENANT, _canary_body("A", MemoryScope.USER_TENANT)
        )
        await service.remember(
            op_a,
            MemoryScope.USER_TARGET,
            _canary_body("A", MemoryScope.USER_TARGET),
            target_name="target-x",
        )
        # B's private canaries, mirror set.
        await service.remember(op_b, MemoryScope.USER, _canary_body("B", MemoryScope.USER))
        await service.remember(
            op_b, MemoryScope.USER_TENANT, _canary_body("B", MemoryScope.USER_TENANT)
        )
        await service.remember(
            op_b,
            MemoryScope.USER_TARGET,
            _canary_body("B", MemoryScope.USER_TARGET),
            target_name="target-x",
        )
        # A's tenant-broadcast canary (no over-correction probe).
        await service.remember(op_a, MemoryScope.TENANT, _canary_body("A", MemoryScope.TENANT))

    yield {"op_a": op_a, "op_b": op_b, "fake": fake}


def _bodies(hits: list[RetrievalHit]) -> list[str]:
    return [h.body for h in hits]


def _owned_by(hits: list[RetrievalHit], owner: str) -> list[RetrievalHit]:
    """Return the user-scoped memory hits whose canary body names *owner*."""
    marker = f"owned by {owner} in scope"
    return [h for h in hits if marker in h.body and h.kind != "memory-tenant"]


# ---------------------------------------------------------------------------
# Substrate-level bidirectional isolation (the core leak)
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_substrate_user_scoped_isolation_is_bidirectional(
    seeded_memories: dict[str, Any],
) -> None:
    """A broad ``retrieve(source="memory")`` returns only the caller's user rows.

    The substrate is the shared boundary both leaking surfaces flow
    through; proving it here proves the fix is caller-agnostic. Each
    principal must see their own three user-scoped canaries and **none**
    of the other principal's -- in both directions.
    """
    fake = seeded_memories["fake"]

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits_a = await retrieve(
            uuid.UUID(TENANT_ID),
            PROBE_QUERY,
            source="memory",
            limit=50,
            principal_sub=PRINCIPAL_A_SUB,
        )
        hits_b = await retrieve(
            uuid.UUID(TENANT_ID),
            PROBE_QUERY,
            source="memory",
            limit=50,
            principal_sub=PRINCIPAL_B_SUB,
        )

    # A sees A's three user-scoped canaries, never B's.
    assert len(_owned_by(hits_a, "A")) == 3, _bodies(hits_a)
    assert _owned_by(hits_a, "B") == [], f"LEAK: B's rows visible to A -> {_bodies(hits_a)}"
    # Symmetric.
    assert len(_owned_by(hits_b, "B")) == 3, _bodies(hits_b)
    assert _owned_by(hits_b, "A") == [], f"LEAK: A's rows visible to B -> {_bodies(hits_b)}"


@_skip_no_docker
async def test_substrate_leaks_without_principal_sub_baseline(
    seeded_memories: dict[str, Any],
) -> None:
    """Sanity: omitting ``principal_sub`` is the pre-fix (leaking) behaviour.

    Proves the canaries genuinely co-reside and are retrievable -- so the
    isolation asserted above is the predicate working, not the corpus
    being empty or the probe query missing. This is the exact call shape
    the two raw callers used *before* #1797 (no per-principal scoping).
    """
    fake = seeded_memories["fake"]
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits = await retrieve(uuid.UUID(TENANT_ID), PROBE_QUERY, source="memory", limit=50)

    # Without the predicate, BOTH principals' user-scoped canaries surface.
    assert len(_owned_by(hits, "A")) == 3, _bodies(hits)
    assert len(_owned_by(hits, "B")) == 3, _bodies(hits)


# ---------------------------------------------------------------------------
# Non-overridable: a client metadata_filters cannot widen the predicate
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_client_metadata_filters_cannot_override_principal_predicate(
    seeded_memories: dict[str, Any],
) -> None:
    """B targeting A's ``user_sub`` via ``metadata_filters`` still gets none of A's rows.

    The enforced ``metadata ->> 'user_sub' = :principal_sub`` clause is
    ANDed in unconditionally, so the adversarial client filter can only
    narrow the set -- it can never widen it to A's private rows.
    """
    fake = seeded_memories["fake"]
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits = await retrieve(
            uuid.UUID(TENANT_ID),
            PROBE_QUERY,
            source="memory",
            limit=50,
            metadata_filters={"user_sub": PRINCIPAL_A_SUB},
            principal_sub=PRINCIPAL_B_SUB,
        )

    assert _owned_by(hits, "A") == [], f"LEAK: client filter widened to A's rows -> {_bodies(hits)}"


# ---------------------------------------------------------------------------
# No over-correction: tenant-broadcast canary stays visible cross-principal
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_tenant_broadcast_canary_visible_cross_principal(
    seeded_memories: dict[str, Any],
) -> None:
    """A's ``tenant``-scoped canary IS returned to B (broadcast unaffected).

    The fix must not over-correct: ``memory-tenant`` / ``memory-target``
    rows carry ``user_sub = null`` and are tenant-wide by design. B must
    see A's tenant-broadcast canary even though B cannot see any of A's
    user-scoped rows.
    """
    fake = seeded_memories["fake"]
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits = await retrieve(
            uuid.UUID(TENANT_ID),
            PROBE_QUERY,
            source="memory",
            limit=50,
            principal_sub=PRINCIPAL_B_SUB,
        )

    tenant_canaries = [h for h in hits if h.kind == "memory-tenant" and "owned by A" in h.body]
    assert len(tenant_canaries) == 1, (
        f"OVER-CORRECTION: A's tenant-broadcast canary not visible to B -> {_bodies(hits)}"
    )


# ---------------------------------------------------------------------------
# MCP retrieve resource is isolated by the same boundary
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_mcp_retrieve_resource_does_not_leak_memory_rows(
    seeded_memories: dict[str, Any],
) -> None:
    """The ``meho://retrieve/{query}`` resource is isolated for both principals.

    The resource retrieves across *every* source with no
    ``metadata_filters`` -- the second leaking surface the issue names.
    Driving the handler directly (it threads ``operator.sub`` into
    ``retrieve``) proves the boundary fix closes it: B's read sees B's
    user-scoped canaries and none of A's, and symmetrically.
    """
    from meho_backplane.mcp.resources.retrieve import _retrieve_handler

    op_a = seeded_memories["op_a"]
    op_b = seeded_memories["op_b"]
    fake = seeded_memories["fake"]

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        result_b = await _retrieve_handler(op_b, {"query": PROBE_QUERY})
        result_a = await _retrieve_handler(op_a, {"query": PROBE_QUERY})

    b_bodies = [hit["body"] for hit in result_b["hits"]]
    a_bodies = [hit["body"] for hit in result_a["hits"]]

    # B's resource read: B's user-scoped canaries present, A's absent.
    assert any("owned by B in scope" in b for b in b_bodies), b_bodies
    a_user_rows_in_b = [
        b for b in b_bodies if "owned by A in scope" in b and "scope tenant" not in b
    ]
    assert a_user_rows_in_b == [], f"LEAK via MCP resource: A's rows visible to B -> {b_bodies}"

    # Symmetric: A's read carries none of B's user-scoped canaries.
    b_user_rows_in_a = [
        b for b in a_bodies if "owned by B in scope" in b and "scope tenant" not in b
    ]
    assert b_user_rows_in_a == [], f"LEAK via MCP resource: B's rows visible to A -> {a_bodies}"
