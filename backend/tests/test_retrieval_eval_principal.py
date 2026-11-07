# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The retrieval eval runs with the caller's identity.

* The eval answer for person A holds none of person B's user memories:
  the runner passes A's ``sub`` to every ``retrieve`` call, and the
  per-person rule keeps B's user memories out of the hit lists.
* The retire checklist passes the caller's ``sub`` to the eval.
* :func:`retrieve` refuses a memory search that names no principal,
  unless the caller sets ``all_principals=True`` on purpose.

The substrate is stubbed. The SQL rule itself is tested against a real
PostgreSQL in ``tests/integration/test_retrieve_principal_isolation_e2e.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.retrieval.eval.runner import eval_all, eval_surface
from meho_backplane.retrieval.retire import compute_retire_checklist
from meho_backplane.retrieval.retriever import RetrievalHit, retrieve
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SUB_A = "person-a"
_SUB_B = "person-b"
_TS = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` needs."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _memory_hit(owner: str, slug: str) -> RetrievalHit:
    """A user-scope memory hit; ``source_id`` is ``user:<owner>:<slug>``."""
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=_TENANT,
        source="memory",
        source_id=f"user:{owner}:{slug}",
        kind="memory-user",
        body=f"private note of {owner}",
        doc_metadata={"user_sub": owner},
        created_at=_TS,
        updated_at=_TS,
        fused_score=0.5,
        bm25_score=0.5,
        cosine_score=0.5,
        bm25_rank=1,
        cosine_rank=1,
    )


def _substrate_with_two_people() -> tuple[Any, list[str | None]]:
    """A ``retrieve`` stand-in that holds one user memory of A and one of B.

    It applies the per-person rule the way the substrate does: with a
    ``principal_sub`` it returns only that person's user memories; without
    one it returns both. It records the ``principal_sub`` of each call.
    """
    seen: list[str | None] = []
    rows = [_memory_hit(_SUB_A, "a-note"), _memory_hit(_SUB_B, "b-note")]

    async def fake_retrieve(
        *,
        tenant_id: uuid.UUID,
        query: str,
        source: str | None = None,
        kind: str | None = None,
        limit: int = 10,
        principal_sub: str | None = None,
    ) -> list[RetrievalHit]:
        seen.append(principal_sub)
        if source != "memory":
            return []
        return [
            r for r in rows if principal_sub is None or r.doc_metadata["user_sub"] == principal_sub
        ]

    return fake_retrieve, seen


@pytest.mark.asyncio
async def test_eval_answer_for_a_holds_no_user_memory_of_b() -> None:
    fake_retrieve, seen = _substrate_with_two_people()

    result = await eval_surface(
        "memory", tenant_id=_TENANT, principal_sub=_SUB_A, retrieve_fn=fake_retrieve
    )

    assert result.query_count > 0
    assert seen and set(seen) == {_SUB_A}
    hit_lists = [q.meho_hits for q in result.queries]
    assert all("user/a-note" in hits for hits in hit_lists)
    assert not any("user/b-note" in hits for hits in hit_lists)


@pytest.mark.asyncio
async def test_eval_all_passes_the_principal_to_every_surface() -> None:
    fake_retrieve, seen = _substrate_with_two_people()

    result = await eval_all(tenant_id=_TENANT, principal_sub=_SUB_A, retrieve_fn=fake_retrieve)

    assert {s.surface for s in result.surfaces} == {"kb", "memory", "operations"}
    assert seen and set(seen) == {_SUB_A}
    memory = next(s for s in result.surfaces if s.surface == "memory")
    assert not any("user/b-note" in q.meho_hits for q in memory.queries)


@pytest.mark.asyncio
async def test_eval_memory_surface_without_principal_is_refused_by_the_real_retriever() -> None:
    with pytest.raises(ValueError, match="principal_sub"):
        await eval_surface("memory", tenant_id=_TENANT)


@pytest.mark.asyncio
async def test_retire_checklist_passes_the_principal_to_the_eval() -> None:
    fake_retrieve, seen = _substrate_with_two_people()

    async with get_sessionmaker()() as session:
        await compute_retire_checklist(
            session=session,
            surfaces=["memory"],
            tenant_id=_TENANT,
            principal_sub=_SUB_A,
            retrieve_fn=fake_retrieve,
        )

    assert seen and set(seen) == {_SUB_A}


# ---------------------------------------------------------------------------
# retrieve(): a memory search names a principal, or says it wants all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "memory"])
async def test_retrieve_refuses_memory_search_without_principal(source: str | None) -> None:
    with pytest.raises(ValueError, match="all_principals"):
        await retrieve(tenant_id=_TENANT, query="q", source=source, session=MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "memory"])
async def test_retrieve_refuses_even_with_limit_zero(source: str | None) -> None:
    with pytest.raises(ValueError, match="principal_sub"):
        await retrieve(tenant_id=_TENANT, query="q", source=source, limit=0)


def _patched_substrate() -> Any:
    fake_embedding = MagicMock()
    fake_embedding.encode_one = AsyncMock(return_value=[0.0] * 384)
    return (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding,
        ),
        patch(
            "meho_backplane.retrieval.retriever._bm25_candidates", new=AsyncMock(return_value=[])
        ),
        patch(
            "meho_backplane.retrieval.retriever._cosine_candidates", new=AsyncMock(return_value=[])
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "kb"},
        {"source": "operations"},
        {"source": "memory", "principal_sub": _SUB_A},
        {"source": None, "principal_sub": _SUB_A},
        {"source": "memory", "all_principals": True},
        {"source": None, "all_principals": True},
    ],
)
async def test_retrieve_allows_search_with_principal_or_explicit_all(
    kwargs: dict[str, Any],
) -> None:
    embedding, bm25, cosine = _patched_substrate()
    with embedding, bm25, cosine:
        hits = await retrieve(tenant_id=_TENANT, query="q", session=MagicMock(), **kwargs)
    assert hits == []
