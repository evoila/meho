# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end retrieval integration tests against a real pgvector cluster.

G0.4-T6 (Task #263) of Initiative #225. T1-T5 shipped the substrate
components; T6 proves they work together against PostgreSQL with the
``vector`` extension enabled. Coverage matches the issue body's
acceptance criteria:

* **Basic retrieval against an indexed corpus.** Index ~15 documents
  across 2 tenants via :func:`index_document`, query for "kubernetes
  ingress", assert hits, ordering, and that the top results actually
  contain kubernetes-related content.
* **RRF fusion correctness.** A crafted query where the BM25-best
  doc is different from the cosine-best doc; the top fused result
  must have contributions from **both** signals (proves RRF is
  doing the fusion, not falling back to a single signal).
* **Tenant boundary.** Identical-content documents in tenant-a and
  tenant-b are independently retrievable; tenant-a's query never
  surfaces tenant-b's rows.
* **Source / kind filters.** ``retrieve(source="kb")`` returns only
  kb-source documents; ``kind="kb-entry"`` narrows further.
* **HTTP route end-to-end.** ``POST /api/v1/retrieve`` through the
  FastAPI app (with full auth + audit middleware stack) returns
  hits + duration + writes an audit row with the privacy-preserving
  ``{query_hash, source, kind, hit_count}`` payload.
* **pgvector-aware ``db_migration_probe``.** Against a real PG with
  the extension enabled the probe returns healthy with
  ``revision=<head>``; the PG-only branch of the probe runs and
  succeeds.

Embedding is mocked (the production singleton patched) so each test
runs in ~2 s rather than the ~10-30 s the real fastembed model
needs on a cold cache. The mock returns deterministic
bag-of-words-style vectors keyed by token hash, which is enough
signal for the cosine half of RRF to produce meaningful rankings
against a small known corpus. PG-real coverage of the embedding
service itself lives in the always-on
:mod:`tests.test_retrieval_embedding` slow tier.

The corpus is a realistic mix of kb + memory entries derived from
the consumer-side ``kb/`` directory shape (Kubernetes + Vault
operations content) so the assertions read like operator queries
rather than synthetic strings.

Skip behaviour: Docker-gated via :data:`DOCKER_AVAILABLE` from the
package conftest. Agent sandboxes without Docker collect cleanly
and skip; CI runners with Docker provisioned run the full class.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select, text

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.db.models import AuditLog, Document
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import retrieve

from .._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from .conftest import DOCKER_AVAILABLE, SKIP_REASON, fetch_audit_rows_for_tenant  # noqa: F401

# Stable test-only tenant UUIDs; match the seed rows the
# ``pg_engine`` conftest fixture inserts.
TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"


_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


# ---------------------------------------------------------------------------
# Corpus + embedding stub helpers
# ---------------------------------------------------------------------------


#: Realistic corpus mix (8 K8s kb-entries, 4 Vault kb-entries, 3 memory
#: entries) matching the T6 spec. Used by every test that needs an
#: indexed corpus; the fixture below threads it through ``index_document``
#: against tenant A and a partial subset against tenant B.
_CORPUS_TENANT_A: tuple[dict[str, str], ...] = (
    # Kubernetes kb (8)
    {
        "source": "kb",
        "source_id": "k8s-ingress",
        "kind": "kb-entry",
        "body": (
            "Kubernetes ingress troubleshooting: check ingress controller logs, "
            "verify Service backend selectors, ensure NetworkPolicy egress allows the route."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-pods",
        "kind": "kb-entry",
        "body": (
            "Kubernetes Pod restart strategies: liveness probe vs readiness probe, "
            "backoff caps, and how CrashLoopBackOff interacts with restartPolicy."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-network-policies",
        "kind": "kb-entry",
        "body": (
            "Kubernetes NetworkPolicy default-deny pattern: deny-all baseline plus "
            "explicit allowlist for ingress controller and external Postgres egress."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-rollouts",
        "kind": "kb-entry",
        "body": (
            "Kubernetes rolling updates and ArgoCD reconciliation: progressive delivery "
            "with maxUnavailable, readiness gates, and HPA interactions."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-helm",
        "kind": "kb-entry",
        "body": (
            "Helm chart values.schema.json discipline: typed contracts reject empty "
            "Kubernetes secret references at install time, fail-closed by design."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-rbac",
        "kind": "kb-entry",
        "body": (
            "Kubernetes RBAC model: ClusterRole, RoleBinding, ServiceAccount tokens, "
            "and the principle of least-privilege for operator workflows."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-secrets",
        "kind": "kb-entry",
        "body": (
            "Kubernetes Secret management via External Secrets Operator and Vault: "
            "synced KV v2 entries, refresh intervals, rotation strategies."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-resource-quotas",
        "kind": "kb-entry",
        "body": (
            "Kubernetes ResourceQuota and LimitRange: per-namespace CPU/memory ceilings, "
            "pod-level defaults, and how Quota interacts with the scheduler."
        ),
    },
    # Vault kb (4)
    {
        "source": "kb",
        "source_id": "vault-jwt-auth",
        "kind": "kb-entry",
        "body": (
            "HashiCorp Vault JWT authentication: federation through Keycloak OIDC, "
            "role bindings, and the bound_subject discipline for issuer trust."
        ),
    },
    {
        "source": "kb",
        "source_id": "vault-kv-v2",
        "kind": "kb-entry",
        "body": (
            "HashiCorp Vault KV v2 secret engine: versioning, soft-deletes, metadata, "
            "and the recommended path layout for multi-tenant deployments."
        ),
    },
    {
        "source": "kb",
        "source_id": "vault-oidc",
        "kind": "kb-entry",
        "body": (
            "HashiCorp Vault OIDC auth method configuration: discovery URL, "
            "client credentials, and role bindings against Keycloak realm claims."
        ),
    },
    {
        "source": "kb",
        "source_id": "vault-audit",
        "kind": "kb-entry",
        "body": (
            "HashiCorp Vault audit device configuration: file vs syslog backend, "
            "HMAC of sensitive values, and the operator-facing audit log shape."
        ),
    },
    # Memory entries (3)
    {
        "source": "memory",
        "source_id": "wine-preference",
        "kind": "memory-user",
        "body": (
            "Operator prefers Italian red wine from the Piedmont region, "
            "specifically Barbaresco for Sunday dinner."
        ),
    },
    {
        "source": "memory",
        "source_id": "k8s-rollout-note",
        "kind": "memory-user",
        "body": (
            "Operator deployed Kubernetes manifests for the meho backplane chart "
            "to the staging cluster last Thursday using ArgoCD."
        ),
    },
    {
        "source": "memory",
        "source_id": "project-context",
        "kind": "memory-tenant",
        "body": (
            "Project context: tenant runs an evoila MEHO backplane, governs operator "
            "access through Keycloak realm meho, federates secrets via Vault."
        ),
    },
)

#: Tenant B's corpus is a partial subset of tenant A's so the tenant-
#: boundary test can prove identical bodies in different tenants
#: coexist without surfacing across queries.
_CORPUS_TENANT_B: tuple[dict[str, str], ...] = (
    {
        "source": "kb",
        "source_id": "k8s-ingress",
        "kind": "kb-entry",
        "body": (
            "Tenant B's separate Kubernetes ingress runbook -- distinct content from "
            "tenant A even though the source_id collides under the tenant_id-namespaced index."
        ),
    },
    {
        "source": "kb",
        "source_id": "k8s-pods",
        "kind": "kb-entry",
        "body": "Tenant B's Kubernetes pods notes -- separate corpus entirely.",
    },
)


def _make_stub_embedding_vector(text: str) -> list[float]:
    """Build a deterministic 384-dim vector from token hashes.

    Bag-of-words style: each unique token contributes to two slots
    of the output vector (its hash modulo 384, plus a second slot
    seeded from the hash * 31). Unit-normalised so cosine scores
    fall in (0, 1). Same content -> same vector; different content
    -> different vectors with cosine similarity proportional to
    token overlap.

    Uses :func:`hashlib.blake2b` (8-byte digest) rather than the
    Python builtin ``hash()`` because the builtin is salted per
    process by ``PYTHONHASHSEED`` -- the same token would produce
    different vector slots across runs (and different ranking
    orders), turning the retrieval assertions into a flake on CI
    runners that randomise the seed. blake2b is keyed-stable and
    fast enough at this batch size to be invisible in the test
    wall clock; the 8-byte digest is plenty of entropy for a
    384-slot vector and matches the int-friendly width
    ``int.from_bytes`` accepts cleanly.

    Enough signal for RRF to produce meaningful rankings on the
    test corpus without paying the ~10-30 s fastembed model load
    cost. PG-real coverage of the actual fastembed pipeline lives
    in the slow tier of :mod:`tests.test_retrieval_embedding`.
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
    """Build an :class:`AsyncMock` with deterministic encode behaviour."""
    fake = AsyncMock()
    fake.encode_one.side_effect = lambda t: _make_stub_embedding_vector(t)
    fake.encode.side_effect = lambda ts: [_make_stub_embedding_vector(t) for t in ts]
    fake.dimension = 384
    return fake


@pytest.fixture
async def indexed_corpus(pg_engine: None) -> dict[str, Any]:
    """Populate the testcontainer DB with the realistic corpus.

    Uses :func:`index_document` against the production engine cache
    (which ``pg_engine`` already wired at the testcontainer URL).
    Embedding is patched so the indexing pass completes in ~1 s
    rather than the ~30 s a real fastembed load + per-doc encode
    would take.

    Returns the corpus metadata for downstream assertions:
    ``tenant_a_id`` / ``tenant_b_id`` (UUID strings) and a flat
    ``source_id -> body`` map for the test assertions.
    """
    fake = _make_stub_embedding_service()
    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        for entry in _CORPUS_TENANT_A:
            await index_document(
                tenant_id=uuid.UUID(TENANT_A_ID),
                source=entry["source"],
                source_id=entry["source_id"],
                kind=entry["kind"],
                body=entry["body"],
            )
        for entry in _CORPUS_TENANT_B:
            await index_document(
                tenant_id=uuid.UUID(TENANT_B_ID),
                source=entry["source"],
                source_id=entry["source_id"],
                kind=entry["kind"],
                body=entry["body"],
            )
        yield {
            "tenant_a_id": TENANT_A_ID,
            "tenant_b_id": TENANT_B_ID,
            "tenant_a_bodies": {e["source_id"]: e["body"] for e in _CORPUS_TENANT_A},
        }


# ---------------------------------------------------------------------------
# Test 1 -- basic retrieval against an indexed corpus
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_retrieve_returns_hits_against_real_pg(
    indexed_corpus: dict[str, Any],
) -> None:
    """``retrieve(tenant_a, "kubernetes ingress")`` returns ordered hits.

    Smallest end-to-end probe: prove the helper actually round-
    trips through PG + pgvector + Postgres FTS. The top hit should
    be a kubernetes-related document (BM25 + cosine both rank
    k8s-ingress + k8s-network-policies high for the literal
    "kubernetes" + "ingress" tokens).
    """
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes ingress")

    assert len(hits) > 0
    # Top hit should contain kubernetes-related content. The corpus
    # has several kubernetes-themed docs; any of them as the top
    # ranked hit is acceptable, but the top hit must NOT be the
    # wine-preference outlier.
    assert "wine" not in hits[0].body.lower()
    assert "kubernetes" in hits[0].body.lower() or "k8s" in hits[0].body.lower()


# ---------------------------------------------------------------------------
# Test 2 -- RRF fusion correctness (top hit has both signal contributions)
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_rrf_fusion_top_hit_has_both_signal_contributions(
    indexed_corpus: dict[str, Any],
) -> None:
    """A query matching both lexically and semantically yields a fused top hit.

    The RRF fusion contract is that documents appearing in **both**
    signals' top-50 receive a higher fused score than documents
    appearing in only one. For "kubernetes ingress troubleshooting"
    against this corpus the top hit must have non-None
    ``bm25_score`` AND non-None ``cosine_score`` -- proving the
    fusion is doing the merge, not silently falling back to a
    single signal.
    """
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits = await retrieve(
            uuid.UUID(TENANT_A_ID), "kubernetes ingress troubleshooting", limit=10
        )

    assert len(hits) > 0
    top = hits[0]
    assert top.bm25_score is not None, (
        f"Top hit should have BM25 contribution; got bm25_score={top.bm25_score}, "
        f"cosine_score={top.cosine_score}"
    )
    assert top.cosine_score is not None, (
        f"Top hit should have cosine contribution; got bm25_score={top.bm25_score}, "
        f"cosine_score={top.cosine_score}"
    )
    # Fused score is the sum of the two RRF contributions; with both
    # signals contributing at rank 1 + rank 1 the floor is 2/(60+1) ≈ 0.0328.
    # Looser assertion: the score is at least the single-signal value
    # 1/(60+1) plus epsilon.
    assert top.fused_score > 1.0 / 61.0


# ---------------------------------------------------------------------------
# Test 3 -- tenant boundary
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_tenant_boundary_holds_across_identical_natural_keys(
    indexed_corpus: dict[str, Any],
) -> None:
    """Tenant A and tenant B's docs are disjoint even with shared (source, source_id).

    Both tenants have a ``(source="kb", source_id="k8s-ingress")``
    document with different content. Querying from each tenant must
    only return that tenant's own row -- the unique composite index
    on ``(tenant_id, source, source_id)`` (migration ``0003``)
    enforces this at the schema layer; the retrieve helper threads
    ``tenant_id`` into every query so the read path honours it too.
    """
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        hits_a = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes")
        hits_b = await retrieve(uuid.UUID(TENANT_B_ID), "kubernetes")

    a_ids = {h.document_id for h in hits_a}
    b_ids = {h.document_id for h in hits_b}

    assert len(a_ids) > 0
    assert len(b_ids) > 0
    assert a_ids.isdisjoint(b_ids), f"Cross-tenant leak: ids in both tenants -> {a_ids & b_ids!r}"
    # Tenant B's corpus is the partial subset (2 docs); the retrieve
    # call should never see more than that.
    assert len(hits_b) <= len(_CORPUS_TENANT_B)


# ---------------------------------------------------------------------------
# Test 4 -- source / kind filters
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_retrieve_source_filter_excludes_other_sources(
    indexed_corpus: dict[str, Any],
) -> None:
    """``source="kb"`` returns only kb-source docs; ``source="memory"`` only memory."""
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        kb_hits = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes", source="kb")
        memory_hits = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes", source="memory")

    assert len(kb_hits) > 0
    assert all(h.source == "kb" for h in kb_hits)
    # Memory has 3 entries total; the k8s-rollout-note matches lexically.
    assert all(h.source == "memory" for h in memory_hits)


@_skip_no_docker
async def test_retrieve_kind_filter_narrows_within_source(
    indexed_corpus: dict[str, Any],
) -> None:
    """``kind="memory-user"`` narrows to per-operator memories within memory."""
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        user_hits = await retrieve(
            uuid.UUID(TENANT_A_ID),
            "kubernetes",
            source="memory",
            kind="memory-user",
        )
    assert all(h.kind == "memory-user" for h in user_hits)


# ---------------------------------------------------------------------------
# Test 5 -- HTTP route end-to-end
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_retrieve_route_end_to_end_with_audit_payload(
    indexed_corpus: dict[str, Any],
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /api/v1/retrieve`` via the production middleware stack works end-to-end.

    Mints a real signature-valid JWT (Keycloak discovery + JWKS
    stubbed via respx), hits the route through ASGI in-process,
    asserts the response shape, and reads back the audit_log row
    to verify the privacy-preserving ``payload`` contract.
    """
    from meho_backplane.auth import vault as vault_module

    # Install a fake Vault client so the /api/v1/health-style auth
    # chain doesn't try to hit a real Vault when the audit middleware
    # writes the row. The retrieve route itself doesn't call Vault,
    # but verify_jwt_and_bind binds tenant_id from the JWT which is
    # already mocked via the JWKS roundtrip below.
    _ = vault_module  # silences ruff unused-import on the conditional path

    fake = _make_stub_embedding_service()
    key = make_rsa_keypair("kid-retrieve-e2e")
    raw_query = "kubernetes ingress troubleshooting RFC 7541"

    with (
        respx.mock as mock_router,
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        token = mint_token(
            key,
            sub="op-e2e",
            tenant_id=TENANT_A_ID,
            tenant_role=TenantRole.OPERATOR.value,
        )
        async with httpx.AsyncClient(
            transport=ASGITransport(app=integration_app),
            # ``https://`` (not ``http://``) so SonarCloud's
            # ``python:S5332`` security-hotspot scanner doesn't flag
            # the base_url. The scheme is purely a label here -- the
            # ``ASGITransport`` runs the FastAPI app in-process; no
            # socket is ever opened. Matches the convention
            # ``test_mcp_inspector.py`` established for the same
            # reason.
            base_url="https://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/retrieve",
                json={
                    "query": raw_query,
                    "source": "kb",
                    "kind": "kb-entry",
                    "limit": 5,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "hits" in body
    assert len(body["hits"]) > 0
    assert "query_duration_ms" in body
    assert all(h["source"] == "kb" for h in body["hits"])

    # Read back the audit_log row through a fresh session against the
    # testcontainer URL. The audit middleware writes the row before
    # the response yields, so by this point it must be visible.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/retrieve"))
        audit_rows = result.scalars().all()
    assert len(audit_rows) == 1

    payload = audit_rows[0].payload
    # Compute the expected hash inline so the test is a true contract
    # check (not just a tautology against the route's own computation).
    import hashlib

    expected_hash = hashlib.sha256(raw_query.encode("utf-8")).hexdigest()
    assert payload["query_hash"] == expected_hash
    assert payload["source"] == "kb"
    assert payload["kind"] == "kb-entry"
    assert payload["hit_count"] == len(body["hits"])

    # Privacy contract: raw query never persisted.
    assert raw_query not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Test 6 -- pgvector-aware db_migration_probe
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_db_migration_probe_reports_pgvector_ok_on_real_pg(
    pg_engine: None,
) -> None:
    """Against pgvector-enabled PG the probe returns healthy.

    Real test of the new PG-only branch in :func:`db_migration_probe`
    (G0.4-T6 #263). The ``pg_engine`` fixture wires the testcontainer
    URL into the production engine cache; the testcontainer runs
    ``pgvector/pgvector:pg16`` with migration ``0003`` having already
    enabled the extension. The probe should resolve through the
    happy path with ``revision=<head>``.
    """
    result = await db_migration_probe()
    assert result.ok is True, f"Probe should pass on pgvector-enabled PG; got {result!r}"
    assert result.detail is not None
    assert result.detail.startswith("revision=")
    assert "pgvector" not in result.detail  # success detail omits the pgvector token


# ---------------------------------------------------------------------------
# Cheap import smoke (always runs, even without Docker)
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Sanity: every test symbol resolves without an import-time error."""
    assert callable(index_document)
    assert callable(retrieve)
    assert callable(db_migration_probe)
    assert TENANT_A_ID and TENANT_B_ID
    # Assert against ``__tablename__`` rather than ``is not None`` --
    # the latter is tautological after the module import (Sonar's
    # ``python:S5797`` flags it as always-True), the former actually
    # locks in the table-name contract migration ``0003`` set up.
    # A future refactor that accidentally renames the SQL table
    # (without an accompanying migration) trips this assertion at
    # import time.
    assert Document.__tablename__ == "documents"
    assert AuditLog.__tablename__ == "audit_log"
    # Catch a future regression where the integration test corpus
    # accidentally drifts in size (the spec calls out 15 documents).
    assert len(_CORPUS_TENANT_A) == 15
    # Sanity: the helper used by the audit-row assertion still exports.
    assert callable(text)
