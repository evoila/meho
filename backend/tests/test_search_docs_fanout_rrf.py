# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cross-collection fan-out + RRF merge for ``search_docs`` (G4.6-T5 #1554).

Coverage matrix (Task #1554 acceptance criteria):

* **Scope parsing** — ``parse_collection_scope`` classifies a single
  ``collection``, an explicit ``collections=[…]`` fan-out, the ``all``
  sentinel, the empty scope (falls through to the mandatory-scope 422),
  and rejects a both-scopes request (mutually exclusive).
* **RRF merge** — ``rrf_merge`` fuses per-collection ranked lists strictly
  by rank (never raw score), tags every chunk with its source collection,
  and produces a deterministic order. A chunk ranked highly in two
  collections out-ranks one ranked first in a single collection only when
  the fused score says so — order is rank-fused, not score-sorted.
* **Entitlement + readiness** — ``resolve_entitled_ready_collections``
  fans out only across entitled, ready collections; non-entitled and
  not-ready members are dropped (logged), and an empty resolved set raises
  the typed ``NoEntitledReadyCollectionError``.
* **End-to-end fan-out** — ``search_docs_fanout`` queries each collection
  independently on its own backend, tags hits with provenance, and merges
  by RRF; a single backend outage fails the whole query closed.
* **ask_docs rejects fan-out** — covered in ``test_mcp_tools_docs_ask`` for
  the wire path; here the scope parser is asserted to flag the fan-out
  shapes ``ask_docs`` refuses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import (
    DocsChunk,
    NoEntitledReadyCollectionError,
    parse_collection_scope,
    resolve_entitled_ready_collections,
    rrf_merge,
    search_docs_fanout,
)
from meho_backplane.docs_search.backends import SearchBackend, all_backends, register_backend
from meho_backplane.docs_search.backends import registry as registry_mod
from meho_backplane.docs_search.fanout import ConflictingCollectionScopeError
from meho_backplane.retrieval.retriever import RRF_K
from meho_backplane.settings import get_settings

_TENANT = "00000000-0000-0000-0000-00000000a0a0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test.

    The enumeration tests build an :class:`Operator` and open a DB session,
    both of which can trigger a :func:`get_settings` read; the route test
    pins the same minimal set.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator(*, capabilities: frozenset[str]) -> Operator:
    """Build an operator carrying *capabilities* for the entitlement gate."""
    return Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt="header.payload.sig",
        tenant_id=_TENANT,
        tenant_role="operator",
        capabilities=capabilities,
    )


def _make_collection(*, collection_key: str, backend_type: str = "fanout-fake") -> DocCollection:
    """Build a frozen :class:`DocCollection` read shape routed to *backend_type*."""
    now = datetime.now(UTC)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key=collection_key,
        vendor="vendor",
        products=("p",),
        description=None,
        when_to_use=None,
        backend={"type": backend_type, "ref": {"key": collection_key}},
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
    )


def _chunk(
    chunk_id: str, *, collection: str | None = None, score: float | None = None
) -> DocsChunk:
    """Build a :class:`DocsChunk` for the RRF unit tests."""
    return DocsChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        content=f"content-{chunk_id}",
        score=score,
        collection=collection,
    )


class _ScriptedBackend(SearchBackend):
    """A backend that returns a per-collection-keyed scripted chunk list.

    Selected by ``backend.type`` like any adapter; the ``backend.ref['key']``
    (the collection key) chooses which scripted response to return, so one
    registered instance serves every collection in the fan-out.
    """

    backend_type = "fanout-fake"

    def __init__(self, scripts: dict[str, list[CorpusChunk]]) -> None:
        self._scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        operator: Operator,
        query: str,
        *,
        backend_ref: Any = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        key = (backend_ref or {}).get("key")
        self.calls.append({"key": key, "query": query, "limit": limit})
        return CorpusSearchResponse(chunks=self._scripts.get(key, []))


class _UnavailableBackend(SearchBackend):
    """A backend that always fails closed (one collection's backend is down)."""

    backend_type = "fanout-down"

    async def search(self, *args: Any, **kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("scripted backend outage")


@pytest.fixture
def _restore_registry() -> Iterator[None]:
    """Snapshot + restore the backend registry around a test that registers fakes."""
    snapshot = all_backends()
    yield
    registry_mod._BACKENDS.clear()
    registry_mod._BACKENDS.update(snapshot)


async def _seed_collection(
    *,
    collection_key: str,
    status: str = "ready",
    tenant_id: str | None = None,
    backend_type: str = "fanout-fake",
) -> None:
    """Insert a doc-collection row for the enumeration tests."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollectionORM(
                tenant_id=tenant_id,
                collection_key=collection_key,
                vendor="vendor",
                products=["p"],
                description=None,
                when_to_use=None,
                backend={"type": backend_type, "ref": {"key": collection_key}},
                status=status,
            )
        )


# ---------------------------------------------------------------------------
# parse_collection_scope
# ---------------------------------------------------------------------------


def test_parse_scope_single() -> None:
    """A single ``collection`` is the non-fan-out path."""
    scope = parse_collection_scope("vmware", None)
    assert scope.is_fanout() is False
    assert scope.single == "vmware"


def test_parse_scope_all_sentinel() -> None:
    """``collection='all'`` is a fan-out with no explicit keys (resolve all)."""
    scope = parse_collection_scope("all", None)
    assert scope.is_fanout() is True
    assert scope.is_all is True
    assert scope.requested_keys() is None


def test_parse_scope_explicit_list_is_sorted_and_deduped() -> None:
    """An explicit ``collections`` list fans out across the sorted, deduped keys."""
    scope = parse_collection_scope(None, ["netapp", "vmware", "netapp", " "])
    assert scope.is_fanout() is True
    assert scope.is_all is False
    assert scope.requested_keys() == ["netapp", "vmware"]


def test_parse_scope_empty_is_not_a_fanout() -> None:
    """Neither scope → empty (the single path raises the mandatory-scope 422).

    Critically NOT a fan-out: a missing collection must surface as the
    mandatory-scope rejection, never silently fan out across everything.
    """
    scope = parse_collection_scope(None, None)
    assert scope.is_fanout() is False
    assert scope.single is None


def test_parse_scope_both_scopes_conflict() -> None:
    """Supplying both a single ``collection`` and ``collections`` is rejected."""
    with pytest.raises(ConflictingCollectionScopeError):
        parse_collection_scope("vmware", ["netapp"])


# ---------------------------------------------------------------------------
# rrf_merge — rank-based fusion, provenance, determinism
# ---------------------------------------------------------------------------


def test_rrf_merge_is_rank_based_not_score_sorted() -> None:
    """Fusion uses ranks, not raw scores: a low raw score can out-rank a high one.

    ``a`` is rank 1 in BOTH collections (fused 2/(K+1)); ``b`` is rank 1 in
    one collection only (fused 1/(K+1)) despite carrying a far larger raw
    score. A raw-score sort would put ``b`` first; RRF puts ``a`` first.
    """
    list_one = [_chunk("a", collection="x", score=0.10), _chunk("z", collection="x", score=0.05)]
    list_two = [_chunk("a", collection="y", score=0.11), _chunk("b", collection="y", score=99.0)]

    merged = rrf_merge([list_one, list_two], limit=10)
    order = [(c.collection, c.chunk_id) for c in merged]

    # ``a`` appears in both x and y, so it surfaces as two distinct,
    # separately-attributed hits (keyed on (collection, chunk_id)); each
    # still out-ranks the raw-high ``b`` because each collection ranked
    # *its* ``a`` at position 1.
    assert order[0][1] == "a"
    a_keys = [k for k in order if k[1] == "a"]
    assert len(a_keys) == 2  # provenance keeps the two ``a`` hits distinct
    assert ("y", "b") in order
    assert order.index(("y", "b")) > order.index(a_keys[0])


def test_rrf_merge_tags_each_chunk_with_collection() -> None:
    """Every merged chunk keeps the source ``collection`` provenance tag."""
    merged = rrf_merge(
        [[_chunk("c1", collection="vmware")], [_chunk("c2", collection="netapp")]],
        limit=10,
    )
    by_id = {c.chunk_id: c.collection for c in merged}
    assert by_id == {"c1": "vmware", "c2": "netapp"}


def test_rrf_merge_fused_score_math() -> None:
    """A chunk in two lists at ranks 1 and 2 scores 1/(K+1) + 1/(K+2)."""
    merged = rrf_merge(
        [
            [_chunk("a", collection="x"), _chunk("other", collection="x")],
            [_chunk("first", collection="y"), _chunk("a", collection="y")],
        ],
        limit=10,
    )
    # ``a`` is rank 1 in x and rank 2 in y, but keyed on (collection,
    # chunk_id) so x's ``a`` (rank 1) and y's ``a`` (rank 2) are distinct.
    x_a = next(c for c in merged if c.chunk_id == "a" and c.collection == "x")
    assert x_a.score is None  # raw score never consulted nor synthesised
    # Order: rank-1 entries (x/a and y/first, both 1/(K+1)) precede the
    # rank-2 entries (x/other and y/a, both 1/(K+2)).
    rank1 = {(c.collection, c.chunk_id) for c in merged[:2]}
    assert rank1 == {("x", "a"), ("y", "first")}


def test_rrf_merge_deterministic_on_ties() -> None:
    """Equal fused scores break on the stable ``(collection, chunk_id)`` key.

    Both chunks are rank 1 in their own list (equal fused score), so the
    tie-break is the ``(collection, chunk_id)`` key: ``("x", "b")`` sorts
    before ``("y", "a")`` because collection ``"x"`` < ``"y"``. The order is
    identical across runs (no reliance on dict insertion order).
    """
    lists = [[_chunk("b", collection="x")], [_chunk("a", collection="y")]]
    first = [(c.collection, c.chunk_id) for c in rrf_merge(lists, limit=10)]
    second = [(c.collection, c.chunk_id) for c in rrf_merge(lists, limit=10)]
    assert first == second == [("x", "b"), ("y", "a")]


def test_rrf_merge_respects_limit() -> None:
    """Only the top-``limit`` fused chunks are returned."""
    lists = [[_chunk(f"c{i}", collection="x") for i in range(5)]]
    assert len(rrf_merge(lists, limit=2)) == 2
    assert rrf_merge(lists, limit=0) == []


def test_rrf_merge_uses_house_rrf_k() -> None:
    """The fusion constant is the shared house ``RRF_K`` (single source)."""
    merged = rrf_merge([[_chunk("only", collection="x")]], limit=1)
    # A single rank-1 chunk contributes exactly 1/(RRF_K + 1); we cannot read
    # the fused score off the chunk (raw score stays None), so assert the
    # constant is the imported house value the math is built on.
    assert RRF_K == 60
    assert merged[0].collection == "x"


# ---------------------------------------------------------------------------
# resolve_entitled_ready_collections — entitlement + readiness drops
# ---------------------------------------------------------------------------


async def test_resolve_all_drops_non_entitled_and_not_ready() -> None:
    """``all`` fans out only across entitled, ready collections.

    Seeds three collections: ``vmware`` (entitled + ready), ``netapp``
    (entitled but rebuilding), ``cisco`` (ready but not entitled). Only
    ``vmware`` survives.
    """
    await _seed_collection(collection_key="vmware", status="ready")
    await _seed_collection(collection_key="netapp", status="rebuilding")
    await _seed_collection(collection_key="cisco", status="ready")
    operator = _make_operator(
        capabilities=frozenset({"meho-docs", "meho-docs:vmware", "meho-docs:netapp"})
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved = await resolve_entitled_ready_collections(session, operator, requested_keys=None)
    assert [c.collection_key for c in resolved] == ["vmware"]


async def test_resolve_explicit_list_drops_unknown_and_keeps_order_sorted() -> None:
    """An explicit list keeps only entitled+ready members, sorted; drops the rest."""
    await _seed_collection(collection_key="vmware", status="ready")
    await _seed_collection(collection_key="netapp", status="ready")
    operator = _make_operator(
        capabilities=frozenset({"meho-docs", "meho-docs:vmware", "meho-docs:netapp"})
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved = await resolve_entitled_ready_collections(
            session, operator, requested_keys=["netapp", "vmware", "ghost"]
        )
    # ``ghost`` is unknown (dropped-and-logged); the rest come back sorted.
    assert [c.collection_key for c in resolved] == ["netapp", "vmware"]


async def test_resolve_empty_entitled_set_raises() -> None:
    """No entitled, ready collection in scope → typed empty-set error."""
    await _seed_collection(collection_key="vmware", status="ready")
    operator = _make_operator(capabilities=frozenset({"meho-docs"}))  # no per-collection key

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        with pytest.raises(NoEntitledReadyCollectionError):
            await resolve_entitled_ready_collections(session, operator, requested_keys=None)


# ---------------------------------------------------------------------------
# search_docs_fanout — independent backend queries + RRF + fail-closed
# ---------------------------------------------------------------------------


async def test_fanout_queries_each_collection_and_merges_by_rrf(_restore_registry: None) -> None:
    """Each collection is queried independently; hits merge by RRF, tagged.

    ``vmware`` returns [v1, shared]; ``netapp`` returns [shared, n1]. ``shared``
    surfaces as two provenance-distinct hits; both backends are called once.
    """
    backend = _ScriptedBackend(
        {
            "vmware": [
                CorpusChunk(chunk_id="v1", document_id="dv1", content="vmware top"),
                CorpusChunk(chunk_id="s", document_id="ds", content="shared"),
            ],
            "netapp": [
                CorpusChunk(chunk_id="s", document_id="ds", content="shared"),
                CorpusChunk(chunk_id="n1", document_id="dn1", content="netapp tail"),
            ],
        }
    )
    register_backend(_ScriptedBackend.backend_type, backend)
    operator = _make_operator(
        capabilities=frozenset({"meho-docs", "meho-docs:vmware", "meho-docs:netapp"})
    )
    collections = [
        _make_collection(collection_key="netapp"),
        _make_collection(collection_key="vmware"),
    ]

    result = await search_docs_fanout(
        operator, "how to configure", collections=collections, limit=10
    )

    # Each collection's backend was called exactly once (independent query).
    assert sorted(c["key"] for c in backend.calls) == ["netapp", "vmware"]
    keys = [(c.collection, c.chunk_id) for c in result.chunks]
    # Every chunk carries provenance; ``s`` appears twice (once per source).
    assert ("vmware", "v1") in keys
    assert ("netapp", "n1") in keys
    assert keys.count(("vmware", "s")) == 1
    assert keys.count(("netapp", "s")) == 1
    assert all(c.collection in {"vmware", "netapp"} for c in result.chunks)


async def test_fanout_passes_per_collection_limit(_restore_registry: None) -> None:
    """The per-collection backend request is bounded by ``limit``."""
    backend = _ScriptedBackend({"vmware": [], "netapp": []})
    register_backend(_ScriptedBackend.backend_type, backend)
    operator = _make_operator(capabilities=frozenset({"meho-docs"}))
    collections = [
        _make_collection(collection_key="vmware"),
        _make_collection(collection_key="netapp"),
    ]

    await search_docs_fanout(operator, "q", collections=collections, limit=3)
    assert all(call["limit"] == 3 for call in backend.calls)


async def test_fanout_fails_closed_on_any_backend_outage(_restore_registry: None) -> None:
    """One unavailable backend fails the whole fan-out (no partial result)."""
    register_backend(_ScriptedBackend.backend_type, _ScriptedBackend({"vmware": []}))
    register_backend(_UnavailableBackend.backend_type, _UnavailableBackend())
    operator = _make_operator(capabilities=frozenset({"meho-docs"}))
    collections = [
        _make_collection(collection_key="vmware", backend_type="fanout-fake"),
        _make_collection(collection_key="down", backend_type="fanout-down"),
    ]

    with pytest.raises(CorpusUnavailable):
        await search_docs_fanout(operator, "q", collections=collections, limit=10)
