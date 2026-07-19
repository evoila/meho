# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end proof of the service-account ``search_memory`` round-trip (#2484).

Regression suite closing the #2494 re-probe of a v0.21.0 field finding:
under the backplane service-account JWT (a ``client_credentials``
principal, ``sub = 6a695b29-a691-4ed9-a1f9-e4f58a4d86a3``), every
``search_memory`` call reportedly returned **0 hits** even though the row
was freshly written under the same JWT and visible via the REST list
verb.

The finding was triaged **refuted**: no principal-type filter exists on
the retrieval path. The only per-principal predicate is
``documents.metadata ->> 'user_sub' = :principal_sub`` string equality
(``retriever.py`` :data:`~meho_backplane.retrieval.retriever._PRINCIPAL_PREDICATE_SQL`),
mirroring the REST list rule
(``MemoryRbacResolver.can_read``, ``rbac.py`` L165-168). The write path
stamps ``user_sub = operator.sub`` (``service.remember`` →
``build_metadata``); the search path filters on the same ``operator.sub``.
``principal_kind`` (``user`` / ``service`` / ``agent`` / ``runner``) is
**never** consulted in the memory or retrieval modules, so a
``client_credentials`` service account is scoped by exactly the same
``sub`` equality as an interactive human operator.

This suite closes the re-probe by reproducing the finding's *exact* path
against a **real pgvector cluster** (testcontainers, Docker-gated like
the rest of ``tests/integration/``) under a single, consistent
``client_credentials`` principal:

1. Mint a ``client_credentials`` service-account operator
   (:attr:`~meho_backplane.auth.operator.PrincipalKind.SERVICE`), reusing
   the field finding's ``sub``.
2. ``add_to_memory scope=user`` over the **MCP tool surface**
   (:func:`~meho_backplane.mcp.tools.memory._add_to_memory_handler`) with
   a unique token in the body.
3. ``search_memory`` over the MCP tool surface
   (:func:`~meho_backplane.mcp.tools.memory._search_memory_handler`) for
   that token — both scope-omitted (the finding's Round 1/2 shape) and
   ``scope=user`` (the narrowed shape).
4. Assert the service account sees its own write.
5. Assert the REST **list** rule (``MemoryService.list_memories`` — the
   exact function ``GET /api/v1/memory`` calls) returns the *same* row,
   so search and list agree for the same principal on the same scope.

Result: the round-trip is **clean** — the service account retrieves its
own user-scoped write. The finding does **not** reproduce for a single,
consistent ``client_credentials`` principal; the reported 0-hits was a
surface/principal-mixing artefact of the probe (a search ``sub`` that
differed from the write ``sub``), exactly as the #2494 triage predicted.
The proof-of-round-trip test is the deliverable, per the #2494 DoD
("re-probed clean before closing").

Embedding is mocked (deterministic bag-of-words vectors, same shape as
:mod:`tests.integration.test_retrieve_principal_isolation_e2e`) so the
suite runs in ~2 s. The body shares tokens with the probe query so both
RRF signals return it — a hit is then a genuine round-trip pass, not a
ranking fluke — and, crucially, a *stopword-only* probe (``"the"``) is
also asserted to hit so a NULL/absent embedding on the service-written
row could not masquerade as the round-trip working.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.mcp.tools.memory import (
    _add_to_memory_handler,
    _search_memory_handler,
)
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

# The shared test tenant seeded by ``pg_engine``.
TENANT_ID: str = "11111111-1111-1111-1111-111111111111"

# The field finding's backplane service-account ``sub`` (verbatim from the
# #2484 reproduction). One principal writes AND searches — the whole point
# is that these are the *same* ``sub``.
SERVICE_ACCOUNT_SUB: str = "6a695b29-a691-4ed9-a1f9-e4f58a4d86a3"

# A unique token embedded in the written body; the probe searches for it.
UNIQUE_TOKEN: str = "zylophane-4f58a4d86a3-canary"

# Probe query whose tokens appear in the body so both BM25 and cosine
# return the row — absence would then be a genuine round-trip failure.
PROBE_QUERY: str = f"kubernetes vault rotation {UNIQUE_TOKEN}"

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _service_account_operator() -> Operator:
    """Build the ``client_credentials`` service-account operator.

    ``principal_kind=service`` is the discriminator a real Keycloak
    ``client_credentials`` token carries. It is asserted here to make the
    test's intent explicit, but note the memory/retrieval path never
    branches on it — the round-trip works because ``sub`` is the only
    per-principal key.
    """
    return Operator(
        sub=SERVICE_ACCOUNT_SUB,
        name="backplane-service-account",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid.UUID(TENANT_ID),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


def _stub_vector(text: str) -> list[float]:
    """Deterministic, process-stable 384-dim bag-of-words vector.

    Mirrors :func:`tests.integration.test_retrieve_principal_isolation_e2e._stub_vector`
    so the written row ranks meaningfully against :data:`PROBE_QUERY`
    without the fastembed cold-load cost.
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


@pytest.fixture
async def written_memory(pg_engine: None) -> AsyncIterator[dict[str, Any]]:
    """``add_to_memory scope=user`` over the MCP tool as the service account.

    Drives the real MCP write handler (not the service directly) so the
    reproduction covers the exact surface the finding named. Embedding is
    patched on the indexer *and* retriever import sites so the write and
    read legs share the same deterministic vectors.
    """
    fake = _stub_embedding_service()
    operator = _service_account_operator()
    body = (
        f"kubernetes vault rotation runbook note {UNIQUE_TOKEN}: the service "
        "account learned this preference and must be able to recall it"
    )

    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        entry = await _add_to_memory_handler(
            operator,
            {
                "scope": MemoryScope.USER.value,
                "body": body,
                "slug": "sa-roundtrip-note",
                "ttl": "PT1M",
            },
        )

    # The write must land under the service account's own ``sub`` — this is
    # the field the retrieval predicate gates on.
    assert entry["user_sub"] == SERVICE_ACCOUNT_SUB, entry
    assert entry["scope"] == MemoryScope.USER.value, entry

    yield {"operator": operator, "fake": fake, "entry": entry}


def _hit_bodies(result: dict[str, Any]) -> list[str]:
    return [hit["entry"]["body"] for hit in result["hits"]]


# ---------------------------------------------------------------------------
# The core round-trip: the service account retrieves its own write.
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_service_account_search_finds_own_user_scoped_write(
    written_memory: dict[str, Any],
) -> None:
    """MCP ``search_memory`` (scope omitted) returns the service account's own row.

    This is the finding's Round 1/2 shape: a fresh ``scope=user`` write
    followed by an unscoped ``search_memory`` for a token in the body,
    all under one ``client_credentials`` principal. The finding reported
    0 hits; the clean round-trip returns the row.
    """
    operator = written_memory["operator"]
    fake = written_memory["fake"]

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        result = await _search_memory_handler(operator, {"query": PROBE_QUERY})

    bodies = _hit_bodies(result)
    assert any(UNIQUE_TOKEN in b for b in bodies), (
        f"0 HITS regression: service account cannot see its own user-scoped "
        f"write via search_memory -> {bodies}"
    )


@_skip_no_docker
async def test_service_account_search_scope_user_finds_own_write(
    written_memory: dict[str, Any],
) -> None:
    """MCP ``search_memory scope=user`` returns the row (the narrowed shape).

    The single-scope path pushes ``metadata_filters={"user_sub": sub}``
    *and* ``principal_sub=sub`` into ``retrieve``; both resolve to the
    service account's own ``sub``, so the write is visible.
    """
    operator = written_memory["operator"]
    fake = written_memory["fake"]

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        result = await _search_memory_handler(
            operator, {"query": PROBE_QUERY, "scope": MemoryScope.USER.value}
        )

    bodies = _hit_bodies(result)
    assert any(UNIQUE_TOKEN in b for b in bodies), (
        f"service account's scope=user search returned no own row -> {bodies}"
    )


@_skip_no_docker
async def test_service_account_stopword_query_still_hits_via_cosine(
    written_memory: dict[str, Any],
) -> None:
    """A stopword-only probe still returns the row (finding Round 3 shape).

    ``plainto_tsquery('english', 'the')`` is empty, so BM25 contributes
    nothing; the row must arrive purely on the cosine signal. This pins
    the reframed AC's "missing embedding on the service-written row"
    concern: if the ``add_to_memory`` write had left ``embedding`` NULL,
    the cosine candidate set would not rank the row and this assertion
    would fail even though the token-overlap probes above passed.
    """
    operator = written_memory["operator"]
    fake = written_memory["fake"]

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        result = await _search_memory_handler(operator, {"query": "the"})

    bodies = _hit_bodies(result)
    assert any(UNIQUE_TOKEN in b for b in bodies), (
        f"cosine-only probe missed the service-written row (NULL embedding?) -> {bodies}"
    )


# ---------------------------------------------------------------------------
# REST list and MCP search agree for the same principal on the same scope.
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_rest_list_and_mcp_search_return_the_same_row(
    written_memory: dict[str, Any],
) -> None:
    """The REST list rule and MCP search surface the identical row (AC #3).

    ``MemoryService.list_memories`` is the exact function ``GET
    /api/v1/memory`` calls; it applies ``can_read`` (``rbac.py`` L165-168)
    — the same ``sub`` equality the search predicate enforces. Both must
    return the service account's own write, so search never diverges from
    list for one principal on one scope. The finding's Round 4 cross-check
    (row visible via REST, absent via search) is exactly this divergence,
    and it must not occur.
    """
    operator = written_memory["operator"]
    fake = written_memory["fake"]
    written_id = written_memory["entry"]["id"]

    service = MemoryService()
    listed = await service.list_memories(operator, scope=MemoryScope.USER)

    with patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake):
        searched = await _search_memory_handler(operator, {"query": PROBE_QUERY})

    listed_ids = {str(entry.id) for entry in listed}
    searched_ids = {hit["entry"]["id"] for hit in searched["hits"]}

    assert written_id in listed_ids, (
        f"REST list did not return the service account's own write -> {listed_ids}"
    )
    assert written_id in searched_ids, (
        f"MCP search did not return the service account's own write -> {searched_ids}"
    )
    # The row visible via list must also be visible via search — no
    # surface can see a row the other cannot for the same principal/scope.
    assert written_id in (listed_ids & searched_ids), (
        f"list/search divergence for one principal: list={listed_ids} search={searched_ids}"
    )
