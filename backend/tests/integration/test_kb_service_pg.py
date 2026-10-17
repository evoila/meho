# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end :class:`KbService` tests against a real pgvector cluster.

G4.1-T1 (#415) acceptance criteria that need PG (not SQLite):

* **Idempotency at the issue's stated scale.** Ingest a 10-file
  corpus once → 10 inserts, 0 skips. Ingest the same directory a
  second time → 0 inserts, 10 skips (the body-hash short-circuit
  from G0.4-T3 fires against the real PG dialect, not just SQLite).
  Scaled down from the consumer's real 44-entry kb so each test
  runs in <1 s wall clock instead of the ~10 s a 44-file fastembed
  pass would cost; the substrate's idempotency contract is the
  same at either scale.
* **Search ranks newly-created entries.** ``create_entry`` then
  ``search_entries`` with body-derived terms must rank the new
  entry in the top-3. Exercises the full retrieve path (BM25 via
  ``to_tsvector`` + cosine via pgvector + RRF fusion).
* **Tenant boundary holds.** Two tenants ingest distinct corpora;
  each tenant's ``search_entries`` / ``list_entries`` only sees
  their own rows.

The fastembed pipeline is patched to a deterministic
bag-of-words-style stub (same pattern
``tests/integration/test_retrieval_e2e.py`` established) so each
test costs ~1 s rather than the ~10-30 s a cold ONNX load + per-doc
encode would. PG-real coverage of the actual fastembed pipeline
lives in :mod:`tests.test_retrieval_embedding`.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.kb.service import KbService
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import retrieve
from tests._kb_embedding_stub import StubPassageTokenizer

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

# Pinned tenant UUIDs match the seed rows the ``pg_engine`` conftest
# fixture inserts; the fixture seeds tenant-a and tenant-b so
# Document.tenant_id FK constraint is satisfied.
TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"


_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _make_stub_embedding_vector(text: str) -> list[float]:
    """Deterministic bag-of-words 384-dim vector keyed by token hashes.

    Same shape ``test_retrieval_e2e.py`` uses: each token contributes
    to two slots (its hash modulo 384, plus a hash*31-seeded slot).
    Unit-normalised so cosine scores fall in (0, 1). ``hashlib.blake2b``
    rather than the builtin ``hash()`` so ``PYTHONHASHSEED`` does not
    introduce per-run variation in ranking order.
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
    fake.passage_tokenizer.return_value = StubPassageTokenizer()
    fake.dimension = 384
    return fake


def _write_corpus(root: Path) -> dict[str, str]:
    """Write a 10-file kb corpus under *root*; return slug → body map.

    Mix of Kubernetes / Vault / Argo content so search queries can
    discriminate. Each body is realistic-shape for the consumer kb
    (one or two sentences with the actual product terms an operator
    would query for).
    """
    entries = {
        "k8s-ingress": (
            "Kubernetes ingress controller troubleshooting: verify Service backend "
            "selectors and NetworkPolicy egress rules."
        ),
        "k8s-rbac": (
            "Kubernetes RBAC primer: ClusterRole + RoleBinding + ServiceAccount, "
            "least-privilege defaults for operator workflows."
        ),
        "k8s-rollouts": (
            "Kubernetes rolling updates with ArgoCD: progressive delivery, "
            "readiness gates, and HPA interactions."
        ),
        "vault-jwt-auth": (
            "HashiCorp Vault JWT authentication federation via Keycloak OIDC, "
            "bound_subject discipline for issuer trust."
        ),
        "vault-kv-v2": (
            "HashiCorp Vault KV v2 secret engine: versioning, soft-delete, "
            "metadata, multi-tenant path layout."
        ),
        "argocd-sync": (
            "ArgoCD application sync waves and resource hooks for safe ordered deploys."
        ),
        "harbor-rotation": ("Harbor registry credential rotation runbook and webhook config."),
        "vcenter-9.0-snapshot": (
            "vCenter 9.0 VM snapshot revert procedure with quiesced disk behaviour notes."
        ),
        "nsx-edge-config": (
            "NSX-T edge cluster config baseline with BGP peering against upstream routers."
        ),
        "sddc-lifecycle": (
            "SDDC Manager lifecycle workflow: bundle download, prechecks, and skip-host strategies."
        ),
    }
    for slug, body in entries.items():
        (root / f"{slug}.md").write_text(body, encoding="utf-8")
    return entries


# ---------------------------------------------------------------------------
# Test 1 -- idempotent ingestion (10 inserts then 10 skips)
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_ingest_directory_idempotent_against_real_pg(
    pg_engine: None,
    tmp_path: Path,
) -> None:
    """First run = 10 inserts; second run = 10 skips (body-hash short-circuit)."""
    _write_corpus(tmp_path)
    tenant_id = uuid.UUID(TENANT_A_ID)
    service = KbService()
    fake = _make_stub_embedding_service()

    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        first = await service.ingest_directory(tmp_path, tenant_id)
        second = await service.ingest_directory(tmp_path, tenant_id)

    assert first.inserted_count == 10
    assert first.updated_count == 0
    assert first.skipped_count == 0
    assert first.error_count == 0

    assert second.inserted_count == 0
    assert second.updated_count == 0
    assert second.skipped_count == 10
    assert second.error_count == 0


# ---------------------------------------------------------------------------
# Test 2 -- search ranks freshly-created entry in top-3
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_create_entry_is_retrievable_via_search(
    pg_engine: None,
    tmp_path: Path,
) -> None:
    """Acceptance: ``create_entry`` then ``search_entries(query=<body terms>)`` ranks it top-3.

    Ingests the 10-file corpus then writes a brand-new entry whose
    body contains a deliberately distinctive phrase; the search for
    that phrase must place the new entry in the top 3 ranked hits.
    """
    _write_corpus(tmp_path)
    tenant_id = uuid.UUID(TENANT_A_ID)
    service = KbService()
    fake = _make_stub_embedding_service()

    distinctive_body = (
        "Operator memo: provisioning yodayodayoda widgets through the bespoke "
        "ratchet harness requires the green wrench, not the blue one."
    )

    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        await service.ingest_directory(tmp_path, tenant_id)
        await service.create_entry(
            tenant_id=tenant_id,
            slug="green-wrench-runbook",
            body=distinctive_body,
        )
        hits = await service.search_entries(
            tenant_id,
            "yodayodayoda ratchet harness",
            limit=5,
        )

    top_3_slugs = [hit.slug for hit in hits[:3]]
    assert "green-wrench-runbook" in top_3_slugs


# ---------------------------------------------------------------------------
# Test 3 -- tenant boundary
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_tenant_boundary_holds_for_list_and_search(
    pg_engine: None,
    tmp_path: Path,
) -> None:
    """Tenant A ingests; tenant B sees nothing on list_entries / search_entries."""
    _write_corpus(tmp_path)
    tenant_a = uuid.UUID(TENANT_A_ID)
    tenant_b = uuid.UUID(TENANT_B_ID)
    service = KbService()
    fake = _make_stub_embedding_service()

    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        await service.ingest_directory(tmp_path, tenant_a)
        a_entries = await service.list_entries(tenant_a)
        b_entries = await service.list_entries(tenant_b)
        b_search = await service.search_entries(tenant_b, "kubernetes")

    assert len(a_entries) == 10
    assert b_entries == []
    assert b_search == []


# ---------------------------------------------------------------------------
# Test 4 -- passages replace whole-body heads in search (#3761)
# ---------------------------------------------------------------------------


@_skip_no_docker
@pytest.mark.asyncio
async def test_kb_search_ranks_current_passages_without_duplicates(pg_engine: None) -> None:
    """``source="kb"`` ranks current passages; heads and old passages are not candidates.

    * An entry written through the service is found only through its
      passages: its head row is never a hit, so no entry shows twice.
    * After a body change, a term that only the old body had finds
      nothing; the new body's passages are found.
    * A legacy head without passages (written before this change) is
      still found, as itself.
    * ``source="memory"`` and ``source=None`` keep working; memory rows
      are untouched by the mapping.
    """
    tenant_id = uuid.UUID(TENANT_A_ID)
    service = KbService()
    fake = _make_stub_embedding_service()
    long_body = (
        "# Runbook\n\n"
        + "\n\n".join(f"Intro paragraph {i} about generic cluster upkeep." for i in range(40))
        + "\n\n## Restart\n\nThe zebracode restart needs the green wrench.\n"
    )

    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        entry, _ = await service.create_entry(tenant_id, "long-runbook", long_body)
        await service.create_entry(tenant_id, "swap-entry", "Old body mentions oldtermxyz.")
        await service.create_entry(tenant_id, "swap-entry", "New body mentions newtermxyz.")
        await index_document(
            tenant_id=tenant_id,
            source="kb",
            source_id="legacy-entry",
            kind="kb-entry",
            body="Legacy body mentions zebracode too.",
        )
        await index_document(
            tenant_id=tenant_id,
            source="memory",
            source_id="tenant:zebra-note",
            kind="memory-tenant",
            body="Memory note about zebracode.",
        )

        kb_hits = await retrieve(tenant_id, "zebracode", source="kb", limit=50)
        all_hits = await retrieve(tenant_id, "zebracode", limit=50)
        memory_hits = await retrieve(tenant_id, "zebracode", source="memory", limit=50)
        old_term = await retrieve(tenant_id, "oldtermxyz", source="kb", limit=50)
        kind_hits = await retrieve(tenant_id, "newtermxyz", kind="kb-entry", limit=50)
        search = await service.search_entries(tenant_id, "zebracode restart", limit=5)

    head_ids = {h.source_id for h in kb_hits if h.source == "kb"}
    assert head_ids == {"legacy-entry"}
    assert "long-runbook" not in {h.source_id for h in all_hits}
    passage_slugs = {
        h.doc_metadata["kb_passage"]["slug"] for h in kb_hits if h.kind == "kb-passage"
    }
    assert "long-runbook" in passage_slugs
    assert {h.source for h in all_hits} == {"kb", "kb-passage", "memory"}
    assert [h.source_id for h in memory_hits] == ["tenant:zebra-note"]
    assert not any("oldtermxyz" in h.body for h in old_term)
    assert [h.doc_metadata["kb_passage"]["slug"] for h in kind_hits if "newtermxyz" in h.body] == [
        "swap-entry"
    ]

    by_slug = {hit.slug: hit for hit in search}
    runbook = by_slug["long-runbook"]
    assert runbook.passages, "the long entry must come back as passages"
    best = runbook.passages[0]
    assert "zebracode" in best.text
    assert len(best.text) < len(long_body) // 4
    assert best.locator.heading_path == ["Runbook", "Restart"]
    assert long_body[best.locator.start : best.locator.end] == best.text
    assert best.locator.revision_id == entry.revision_id
    assert by_slug["legacy-entry"].passages == []


# ---------------------------------------------------------------------------
# Cheap import smoke -- always runs even without Docker
# ---------------------------------------------------------------------------


def test_module_imports_cleanly(tmp_path: Path) -> None:
    """Sanity: every test symbol resolves; corpus shape locked at 10 entries.

    Drives :func:`_write_corpus` against a real tmp_path (without DB)
    to lock the corpus-size contract Test 1 / Test 3 rely on (10
    entries). If the helper drifts to a different file count this
    smoke fails before the docker-gated tests would discover it.
    """
    assert callable(KbService)
    entries = _write_corpus(tmp_path)
    assert len(entries) == 10
    assert TENANT_A_ID and TENANT_B_ID
