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
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import event, select, text

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.db.models import AuditLog, Document
from meho_backplane.retrieval import retriever as _retriever_module
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve
from meho_backplane.settings import get_settings

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
        hits = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes ingress", all_principals=True)

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
            uuid.UUID(TENANT_A_ID),
            "kubernetes ingress troubleshooting",
            limit=10,
            all_principals=True,
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
        hits_a = await retrieve(uuid.UUID(TENANT_A_ID), "kubernetes", all_principals=True)
        hits_b = await retrieve(uuid.UUID(TENANT_B_ID), "kubernetes", all_principals=True)

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
        memory_hits = await retrieve(
            uuid.UUID(TENANT_A_ID), "kubernetes", source="memory", all_principals=True
        )

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
            all_principals=True,
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


# ---------------------------------------------------------------------------
# KB_SEARCH_PLAIN_SENTENCES (#6784): the any-word fallback of the knowledge search
#
# A knowledge search (source="kb") whose every-word search finds nothing runs
# the word search again, and a passage needs any word. Switch off: as before.
# Memory search and a search with no source do not change. The made-up
# embedding gives the meaning half a fixed order, so only the word half is
# under test.
# ---------------------------------------------------------------------------

_KBPS_TENANT_A = uuid.UUID(TENANT_A_ID)
_KBPS_TENANT_B = uuid.UUID(TENANT_B_ID)

#: (source, source_id, body). Made-up texts.
_KBPS_CORPUS = (
    (
        "kb",
        "restore",
        "Restore the demo share from the snapshot when the nightly backup job fails.",
    ),
    ("kb", "mounts", "The demo share is mounted by the backup job."),
    ("kb", "certs", "Rotate the certificate of the gateway every ninety days."),
    ("kb", "holidays", "The office calendar lists every public holiday."),
    ("memory", "note", "The nightly backup job of the demo share failed on the gateway."),
)
#: A long question. No passage holds every word ("ticket" is in none).
_KBPS_LONG = (
    "ticket: how do I restore the demo share after the nightly backup job failed on the gateway"
)
_KBPS_SHORT = "backup job"
_KBPS_STOP_ONLY = "the of and it is to"


@pytest.fixture
def _kbps_clear_settings_cache() -> Iterator[None]:
    # Only the tests of this section ask for it (through kbps_corpus), so no
    # other test of the module sees a cleared settings cache.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _kbps_switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("KB_SEARCH_PLAIN_SENTENCES", "true" if on else "false")
    get_settings.cache_clear()


def _kbps_probe_every_list(dbapi_connection: Any, *_: Any) -> None:
    # The vector index (IVFFlat) was built on an empty table, so probing one
    # list can miss rows of this small kbps_corpus. Probing every list makes the
    # meaning search exact.
    cursor = dbapi_connection.cursor()
    cursor.execute("SET ivfflat.probes = 100")
    cursor.close()


def _kbps_reset_probes(dbapi_connection: Any, *_: Any) -> None:
    # A connection goes back to the pool with the setting of the server again,
    # so a test of another module never gets the exact meaning search.
    cursor = dbapi_connection.cursor()
    cursor.execute("RESET ivfflat.probes")
    cursor.close()


@pytest.fixture
async def kbps_corpus(pg_engine: None, _kbps_clear_settings_cache: None) -> AsyncIterator[None]:
    engine = get_engine().sync_engine
    event.listen(engine, "checkout", _kbps_probe_every_list)
    event.listen(engine, "checkin", _kbps_reset_probes)
    fake = _make_stub_embedding_service()
    try:
        with (
            patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
            patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
        ):
            for source, source_id, body in _KBPS_CORPUS:
                await index_document(
                    tenant_id=_KBPS_TENANT_A,
                    source=source,
                    source_id=source_id,
                    kind="kb-entry" if source == "kb" else "memory-tenant",
                    body=body,
                )
            # Tenant B holds a text with many question words. Tenant A must never see it.
            await index_document(
                tenant_id=_KBPS_TENANT_B,
                source="kb",
                source_id="other-tenant",
                kind="kb-entry",
                body="Restore the demo share after the nightly backup job failed on the gateway.",
            )
            yield
    finally:
        # Runs also when the set-up above fails.
        event.remove(engine, "checkout", _kbps_probe_every_list)
        event.remove(engine, "checkin", _kbps_reset_probes)


# The word-search statement of main, before this Task (every word, no any_word
# bind). The tests below run it as the reference for the switch off.
_KBPS_MAIN_BM25_SQL = text(
    r"""
    SELECT id, ts_rank_cd(
        to_tsvector('english', body),
        plainto_tsquery('english', :query)
    ) AS score
    FROM documents
    WHERE tenant_id = :tenant_id
      AND (
        CAST(:source AS text) IS NULL
        OR source = :source
        OR (CAST(:source AS text) = 'kb' AND source = 'kb-passage')
      )
      AND (
        CAST(:kind AS text) IS NULL
        OR kind = :kind
        OR (CAST(:kind AS text) = 'kb-entry' AND kind = 'kb-passage')
      )
      AND NOT (
        source = 'kb'
        AND EXISTS (
          SELECT 1 FROM documents AS passage
          WHERE passage.tenant_id = documents.tenant_id
            AND passage.source = 'kb-passage'
            AND passage.metadata @> jsonb_build_object(
              'kb_passage', jsonb_build_object('entry_id', CAST(documents.id AS text))
            )
        )
      )
      AND (CAST(:metadata_filters AS text) IS NULL
           OR metadata @> CAST(:metadata_filters AS jsonb))
      AND (
        CAST(:updated_after AS timestamptz) IS NULL
        OR updated_at >= CAST(:updated_after AS timestamptz)
      )
      AND (
        CAST(:updated_before AS timestamptz) IS NULL
        OR updated_at <= CAST(:updated_before AS timestamptz)
      )
      AND (
        (CAST(:confirmed_after AS timestamptz) IS NULL
         AND CAST(:confirmed_before AS timestamptz) IS NULL)
        OR CASE
          WHEN metadata ->> 'confirmed_at' IS NULL THEN NULL
          WHEN metadata ->> 'confirmed_at'
               !~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
            THEN NULL
          WHEN NOT pg_input_is_valid(metadata ->> 'confirmed_at', 'timestamptz') THEN NULL
          WHEN metadata ->> 'confirmed_at' ~ '(Z|[+-]\d\d(:?\d\d)?)$'
            THEN CAST(metadata ->> 'confirmed_at' AS timestamptz)
          ELSE CAST(metadata ->> 'confirmed_at' AS timestamp) AT TIME ZONE 'UTC'
        END BETWEEN COALESCE(CAST(:confirmed_after AS timestamptz), '-infinity')
                AND COALESCE(CAST(:confirmed_before AS timestamptz), 'infinity')
      )
      AND (
        CAST(:principal_sub AS text) IS NULL
        OR source <> 'memory'
        OR kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')
        OR metadata ->> 'user_sub' = :principal_sub
      )
      AND (
        source <> 'memory'
        OR (
          (CAST(:memory_kinds AS text[]) IS NULL
           OR kind = ANY(CAST(:memory_kinds AS text[])))
          AND CASE
            WHEN metadata ->> 'expires_at' IS NULL THEN true
            WHEN metadata ->> 'expires_at'
                 !~ '^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d([.]\d{1,6})?)?(Z|[+-]\d\d(:?\d\d)?)?$'
              THEN false
            WHEN NOT pg_input_is_valid(metadata ->> 'expires_at', 'timestamptz') THEN false
            WHEN metadata ->> 'expires_at' ~ '(Z|[+-]\d\d(:?\d\d)?)$'
              THEN CAST(metadata ->> 'expires_at' AS timestamptz) > now()
            ELSE CAST(metadata ->> 'expires_at' AS timestamp) > timezone('UTC', now())
          END
        )
      )
      AND to_tsvector('english', body) @@ plainto_tsquery('english', :query)
    ORDER BY score DESC
    LIMIT :limit
    """
)


_KBPS_MAIN_BM25_CALLS: list[str] = []


async def _kbps_main_bm25_candidates(
    session: Any,
    tenant_id: uuid.UUID,
    query: str,
    source: str | None,
    kind: str | None,
    metadata_filters_json: str | None,
    principal_sub: str | None,
    memory_kinds: list[str] | None = None,
    *,
    time_window: Any = None,
    **_unused: Any,
) -> Any:
    """Main's ``_bm25_candidates``: the statement above with the binds main sent."""
    _KBPS_MAIN_BM25_CALLS.append(query)
    result = await session.execute(
        _KBPS_MAIN_BM25_SQL,
        {
            "query": query,
            "tenant_id": str(tenant_id),
            "source": source,
            "kind": kind,
            "metadata_filters": metadata_filters_json,
            "principal_sub": principal_sub,
            "memory_kinds": memory_kinds,
            "limit": _retriever_module.CANDIDATE_LIMIT,
            **_retriever_module._time_window_binds(time_window),
        },
    )
    return result.all()


async def _kbps_search(query: str, source: str | None = "kb") -> list[RetrievalHit]:
    return await retrieve(
        _KBPS_TENANT_A, query, source=source, limit=10, all_principals=source != "kb"
    )


def _kbps_shape(hits: list[RetrievalHit]) -> list[tuple[str, float, float | None, int | None]]:
    return [(h.source_id, h.fused_score, h.bm25_score, h.bm25_rank) for h in hits]


def _kbps_words(hits: list[RetrievalHit]) -> dict[str, float | None]:
    return {h.source_id: h.bm25_score for h in hits}


async def test_switch_off_long_question_gets_no_word_score(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    hits = await _kbps_search(_KBPS_LONG)
    assert hits
    assert all(h.bm25_score is None for h in hits)
    assert all(h.bm25_rank is None for h in hits)


async def test_switch_off_short_question_gets_word_scores(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    scores = _kbps_words(await _kbps_search(_KBPS_SHORT))
    assert scores["restore"] is not None
    assert scores["mounts"] is not None
    assert scores["certs"] is None


async def test_switch_on_long_question_ranks_by_question_words(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=True)
    hits = await _kbps_search(_KBPS_LONG)
    by_id = {h.source_id: h for h in hits}
    # The passages that hold question words get a word score; others do not.
    assert by_id["restore"].bm25_score is not None
    assert by_id["mounts"].bm25_score is not None
    assert by_id["certs"].bm25_score is not None
    assert by_id["holidays"].bm25_score is None
    # The passage with the most question words ranks first among them.
    assert by_id["restore"].bm25_rank == 1
    assert by_id["restore"].bm25_score > by_id["mounts"].bm25_score
    # Passages of memory and of another tenant never show up in a kb search.
    assert set(by_id) <= {"restore", "mounts", "certs", "holidays"}


@pytest.mark.parametrize("query", [_KBPS_SHORT, "backup job demo share"])
async def test_switch_on_every_word_match_is_unchanged(
    query: str, kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    off = _kbps_shape(await _kbps_search(query))
    _kbps_switch(monkeypatch, on=True)
    on = _kbps_shape(await _kbps_search(query))
    assert on == off
    assert any(row[2] is not None for row in on)


# (query, ids that get a word score with the switch on, same as off?)
_KBPS_ODD = [
    # No word survives: no word hit, and the same as off.
    (_KBPS_STOP_ONLY, set(), True),
    ("", set(), True),
    ("   ", set(), True),
    # The operators are dropped and every word is found: no fallback.
    ("backup & job | share", {"restore", "mounts"}, True),
    # No passage holds "quoted" or "text": nothing to find, also with any word.
    ("it's \"quoted\" 'text' (a:b)", set(), True),
    # No passage holds the hyphen word: the any-word search finds the two
    # passages that hold "demo" and "share".
    ("demo-share", {"restore", "mounts"}, False),
]


@pytest.mark.parametrize(("query", "scored", "same_as_off"), _KBPS_ODD)
async def test_switch_on_odd_questions_give_the_expected_hits(
    query: str,
    scored: set[str],
    same_as_off: bool,
    kbps_corpus: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kbps_switch(monkeypatch, on=False)
    off = _kbps_shape(await _kbps_search(query))
    _kbps_switch(monkeypatch, on=True)
    on_hits = await _kbps_search(query)
    assert {h.source_id for h in on_hits if h.bm25_score is not None} == scored
    assert (_kbps_shape(on_hits) == off) is same_as_off


async def test_switch_on_hyphen_word_finds_its_passage(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=True)
    hits = await _kbps_search("demo-share certificate zebra")
    assert _kbps_words(hits)["certs"] is not None


@pytest.mark.parametrize("source", ["memory", None])
async def test_switch_on_other_sources_are_unchanged(
    source: str | None, kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    off = _kbps_shape(await _kbps_search(_KBPS_LONG, source=source))
    _kbps_switch(monkeypatch, on=True)
    on = _kbps_shape(await _kbps_search(_KBPS_LONG, source=source))
    assert on == off
    assert all(row[2] is None for row in on)


async def test_second_search_keeps_the_tenant_and_the_filters(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=True)
    hits: list[Any] = await retrieve(
        _KBPS_TENANT_B, _KBPS_LONG, source="kb", limit=10, metadata_filters={"nothing": "matches"}
    )
    # A metadata filter that no row holds leaves no hit, and no word hit either.
    assert hits == []
    own = await _kbps_search(_KBPS_LONG)
    assert "other-tenant" not in {h.source_id for h in own}
    other = await retrieve(_KBPS_TENANT_B, _KBPS_LONG, source="kb", limit=10)
    assert {h.source_id for h in other} == {"other-tenant"}


# Switch off: the same answer as main, tested against main's own word-search statement.
_KBPS_OFF_CASES = [
    pytest.param(_KBPS_SHORT, "kb", id="kb-short"),
    pytest.param(_KBPS_LONG, "kb", id="kb-long"),
    pytest.param(_KBPS_SHORT, "memory", id="memory-short"),
    pytest.param(_KBPS_LONG, "memory", id="memory-long"),
    pytest.param(_KBPS_SHORT, None, id="all-short"),
    pytest.param(_KBPS_LONG, None, id="all-long"),
]


def _kbps_full(hits: list[RetrievalHit]) -> list[dict[str, Any]]:
    return [h.model_dump(mode="json") for h in hits]


@pytest.mark.parametrize(("query", "source"), _KBPS_OFF_CASES)
async def test_switch_off_hit_list_is_the_same_as_main(
    query: str, source: str | None, kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    current = await _kbps_search(query, source=source)
    _KBPS_MAIN_BM25_CALLS.clear()
    with patch.object(_retriever_module, "_bm25_candidates", _kbps_main_bm25_candidates):
        reference = await _kbps_search(query, source=source)
    assert [query] == _KBPS_MAIN_BM25_CALLS  # the reference ran
    assert current  # the meaning half always finds rows, so the lists are not empty
    # Ids, order, fused score, word score and rank, cosine score and rank.
    assert _kbps_full(current) == _kbps_full(reference)


@pytest.mark.parametrize(("query", "source"), _KBPS_OFF_CASES[:2])
async def test_switch_off_rest_search_answer_is_the_same_as_main(
    query: str,
    source: str,
    kbps_corpus: None,
    integration_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kbps_switch(monkeypatch, on=False)
    key = make_rsa_keypair("kid-kbps-rest")

    async def _post() -> list[dict[str, Any]]:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            token = mint_token(
                key,
                sub="op-kbps",
                tenant_id=TENANT_A_ID,
                tenant_role=TenantRole.OPERATOR.value,
            )
            async with httpx.AsyncClient(
                transport=ASGITransport(app=integration_app), base_url="https://testserver"
            ) as client:
                response = await client.post(
                    "/api/v1/retrieve",
                    json={"query": query, "source": source, "limit": 10},
                    headers={"Authorization": f"Bearer {token}"},
                )
        assert response.status_code == 200, response.text
        return list(response.json()["hits"])

    current = await _post()
    with patch.object(_retriever_module, "_bm25_candidates", _kbps_main_bm25_candidates):
        reference = await _post()
    assert current
    assert current == reference


# ---------------------------------------------------------------------------
# KB_SEARCH_PLAIN_SENTENCES (#6784): one decision for the whole knowledge search
#
# The caller's own passages and the shared passages of an owner tenant are
# searched together. The any-word search runs only when NO searched tenant has
# a passage with every word. An every-word hit that is dropped later (the
# caller has an entry with the same slug) still counts.
# ---------------------------------------------------------------------------

#: Own passages hold only some of these words; the shared "shared-all" holds all.
_KBPS_Q_SHARED_ALL = "nightly rotate certificate"
#: Own passages "restore" and "mounts" hold every word; "shared-part" holds two.
_KBPS_Q_OWN_ALL = "backup job demo share"
#: No passage anywhere holds "ticket".
_KBPS_Q_NONE = "ticket backup gateway"

_KBPS_SHARED_DOCS = (
    ("shared-all", "Rotate the certificate every nightly cycle."),
    ("shared-part", "The backup job runs weekly."),
)


async def _kbps_index_shared(slug: str, body: str, tenant_id: uuid.UUID) -> None:
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        await index_document(
            tenant_id=tenant_id,
            source="kb",
            source_id=slug,
            kind="kb-entry",
            body=body,
            metadata={"shared": True},
        )


@pytest.fixture
async def kbps_shared(kbps_corpus: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant B owns two shared entries; tenant A is the caller."""
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(_KBPS_TENANT_B))
    get_settings.cache_clear()
    for slug, body in _KBPS_SHARED_DOCS:
        await _kbps_index_shared(slug, body, _KBPS_TENANT_B)


async def _kbps_entries(query: str, scope: Any, filters: dict[str, Any] | None = None) -> Any:
    from meho_backplane.kb.service import KbService

    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        return await KbService().search_entries(
            _KBPS_TENANT_A, query, filters=filters, limit=10, scope=scope
        )


def _kbps_entry_shape(hits: Any) -> list[tuple[str, float, float | None, int | None]]:
    return [(h.slug, h.fused_score, h.bm25_score, h.bm25_rank) for h in hits]


def _kbps_scored(hits: Any) -> set[str]:
    return {h.slug for h in hits if h.bm25_score is not None}


# (scope, query, slugs with a word score with the switch on, same as off?)
_KBPS_JOINED = [
    # Own passages hold only some words, a shared passage holds all: no any-word search.
    pytest.param("all", _KBPS_Q_SHARED_ALL, {"shared-all"}, True, id="own-partial-shared-all"),
    # Own passages hold every word, a shared passage holds some: no any-word search.
    pytest.param("all", _KBPS_Q_OWN_ALL, {"restore", "mounts"}, True, id="own-all-shared-partial"),
    # No passage anywhere holds every word: any word, in both tenants.
    pytest.param(
        "all",
        _KBPS_Q_NONE,
        {"restore", "mounts", "certs", "shared-part"},
        False,
        id="none-anywhere",
    ),
    # Scope "tenant" never looks at the shared entries: the own tenant decides alone.
    pytest.param("tenant", _KBPS_Q_SHARED_ALL, {"restore", "certs"}, False, id="tenant-scope"),
    # Scope "shared" looks at the owner tenant only.
    pytest.param("shared", _KBPS_Q_SHARED_ALL, {"shared-all"}, True, id="shared-scope-all"),
    pytest.param("shared", _KBPS_Q_NONE, {"shared-part"}, False, id="shared-scope-none"),
]


@pytest.mark.parametrize(("scope", "query", "scored", "same_as_off"), _KBPS_JOINED)
async def test_one_decision_for_the_whole_knowledge_search(
    scope: str,
    query: str,
    scored: set[str],
    same_as_off: bool,
    kbps_shared: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _kbps_switch(monkeypatch, on=False)
    off = await _kbps_entries(query, scope)
    _kbps_switch(monkeypatch, on=True)
    on = await _kbps_entries(query, scope)
    assert _kbps_scored(on) == scored
    assert (_kbps_entry_shape(on) == _kbps_entry_shape(off)) is same_as_off


async def test_dropped_every_word_shared_hit_still_counts(
    kbps_shared: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller has an entry with the slug of the shared every-word entry. The
    # shared hit is dropped (the own entry wins), but the question keeps the
    # every-word search, as it has today: the own partial passages get no score.
    await _kbps_index_shared("shared-all", "The nightly backup job.", _KBPS_TENANT_A)
    _kbps_switch(monkeypatch, on=False)
    off = await _kbps_entries(_KBPS_Q_SHARED_ALL, "all")
    _kbps_switch(monkeypatch, on=True)
    on = await _kbps_entries(_KBPS_Q_SHARED_ALL, "all")
    assert _kbps_scored(on) == _kbps_scored(off) == set()
    assert _kbps_entry_shape(on) == _kbps_entry_shape(off)


async def test_shared_scope_left_out_by_a_filter_leaves_the_own_tenant_alone(
    kbps_shared: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "shared": false leaves every shared entry out, so only the own tenant is
    # searched and it decides alone: no passage there holds every word.
    fake = _make_stub_embedding_service()
    with patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake):
        await index_document(
            tenant_id=_KBPS_TENANT_A,
            source="kb",
            source_id="private",
            kind="kb-entry",
            body="The nightly cycle.",
            metadata={"shared": False},
        )
    _kbps_switch(monkeypatch, on=True)
    hits = await _kbps_entries(_KBPS_Q_SHARED_ALL, "all", {"shared": False})
    assert _kbps_scored(hits) == {"private"}


async def test_switch_off_makes_no_extra_word_query(
    kbps_shared: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=False)
    _KBPS_MAIN_BM25_CALLS.clear()
    with patch.object(_retriever_module, "_bm25_candidates", _kbps_main_bm25_candidates):
        await _kbps_entries(_KBPS_Q_NONE, "all")
    # One word search for the own tenant and one for the owner tenant, as on main.
    assert _KBPS_MAIN_BM25_CALLS == [_KBPS_Q_NONE, _KBPS_Q_NONE]


@pytest.mark.parametrize(("query", "scored"), [(_KBPS_Q_SHARED_ALL, {"shared-all"})])
async def test_rest_search_makes_one_decision_for_own_and_shared(
    query: str,
    scored: set[str],
    kbps_shared: None,
    integration_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = make_rsa_keypair("kid-kbps-rest-joined")

    async def _post() -> list[dict[str, Any]]:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            token = mint_token(
                key,
                sub="op-kbps",
                tenant_id=TENANT_A_ID,
                tenant_role=TenantRole.OPERATOR.value,
            )
            async with httpx.AsyncClient(
                transport=ASGITransport(app=integration_app), base_url="https://testserver"
            ) as client:
                response = await client.post(
                    "/api/v1/retrieve",
                    json={"query": query, "source": "kb", "limit": 10},
                    headers={"Authorization": f"Bearer {token}"},
                )
        assert response.status_code == 200, response.text
        return list(response.json()["hits"])

    _kbps_switch(monkeypatch, on=False)
    off = await _post()
    _kbps_switch(monkeypatch, on=True)
    on = await _post()
    assert {h["source_id"] for h in on if h["bm25_score"] is not None} == scored
    assert on == off


# ---------------------------------------------------------------------------
# KB_SEARCH_PLAIN_SENTENCES (#6784): the any-word search uses at most
# ANY_WORD_MAX_WORDS different words, so a long question stays fast.
# ---------------------------------------------------------------------------

#: Made-up words that no passage holds, all different.
_KBPS_FILLER = [f"zq{a}{b}x" for a in "bcdfg" for b in "bcdfghjk"]


def _kbps_word_scores(hits: list[RetrievalHit]) -> dict[str, tuple[float | None, int | None]]:
    return {h.source_id: (h.bm25_score, h.bm25_rank) for h in hits if h.bm25_score is not None}


async def test_any_word_search_uses_only_the_first_words(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _retriever_module.ANY_WORD_MAX_WORDS == 32
    first = ["backup", *_KBPS_FILLER[: _retriever_module.ANY_WORD_MAX_WORDS - 1]]
    rest = _KBPS_FILLER[_retriever_module.ANY_WORD_MAX_WORDS - 1 :]
    _kbps_switch(monkeypatch, on=True)
    # "certificate" comes after the first 32 different words: it does not count.
    long_hits = await _kbps_search(" ".join([*first, *rest, "certificate"]))
    first_hits = await _kbps_search(" ".join(first))
    assert set(_kbps_word_scores(long_hits)) == {"restore", "mounts"}
    assert _kbps_word_scores(long_hits) == _kbps_word_scores(first_hits)
    # Inside the first 32 different words, it counts.
    inside = await _kbps_search(" ".join(["certificate", *first, *rest]))
    assert "certs" in _kbps_word_scores(inside)


async def test_any_word_search_counts_a_repeated_word_once(
    kbps_corpus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _kbps_switch(monkeypatch, on=True)
    # 60 words, but only "backup" and the 30 made-up words are different.
    query = " ".join(["backup"] * 30 + _KBPS_FILLER[:30] + ["certificate"])
    assert set(_kbps_word_scores(await _kbps_search(query))) == {"restore", "mounts", "certs"}
