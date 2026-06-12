# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :class:`MemoryService`.

G5.1-T1 (#421) acceptance criteria coverage:

* ``remember`` + ``recall`` + ``forget`` round-trip for every scope.
* RBAC enforcement at the write boundary (operator -> TENANT denied).
* Tenant boundary: tenant A's rows invisible to tenant B's operator.
* User boundary: operator A's user-scoped row invisible to operator B
  in the same tenant.
* Expired-entry filter (without ``include_expired``).
* ``include_expired=True`` returns expired rows.
* ``target_name`` required for target-scoped writes (ValueError).
* ``search_memories`` reuses retrieval substrate with kind+RBAC
  post-filtering.

The tests run against the autouse :func:`tests.conftest._default_database_url`
fixture's SQLite-backed engine; embeddings are mocked so the indexer
does not pull fastembed on every test. The retriever's PG-only SQL
(``@@`` / ``<=>``) is exercised via :func:`patch` on
:func:`meho_backplane.retrieval.retriever.retrieve` -- the PG-real
contract lives in :mod:`tests.integration.test_retrieval_e2e`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory.rbac import PermissionDeniedError
from meho_backplane.memory.schemas import MemoryEntryCreate, MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: Deterministic 384-dim placeholder embedding. The indexer's only
#: requirement of the embedding service is "return a list[float] of
#: length 384"; mocking with this fixed vector keeps the round-trip
#: cheap (no fastembed model load) without affecting any of the
#: service-level assertions, which only inspect the document body and
#: metadata.
_FAKE_EMBEDDING: list[float] = [0.01] * 384


@pytest.fixture
def _fake_embedding_service() -> Iterator[None]:
    """Patch the embedding singleton imported by the indexer.

    Both ``indexer`` and ``retriever`` modules import
    :func:`~meho_backplane.retrieval.embedding.get_embedding_service`
    at module scope, so the patch needs to target the imported name
    on each side. Service-level tests only exercise the indexer path
    (retriever is patched per-test via :func:`patch` where needed),
    so we only patch the indexer's bound name.
    """
    fake = AsyncMock()
    fake.encode_one.return_value = _FAKE_EMBEDDING
    fake.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        yield


def _op(
    *,
    sub: str = "op-42",
    tenant_id: uuid.UUID | None = None,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    """Build an :class:`Operator` for service tests.

    The default :class:`TenantRole.OPERATOR` matches the most common
    test case (a user writing their own memory); tests that need
    ``tenant_admin`` / ``read_only`` pass the role explicitly.
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=tenant_id or uuid.UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=role,
    )


# ---------------------------------------------------------------------------
# Round-trip per scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope, target_name",
    [
        (MemoryScope.USER, None),
        (MemoryScope.USER_TENANT, None),
        (MemoryScope.USER_TARGET, "infra-1"),
        (MemoryScope.TENANT, None),  # tenant_admin op below
        (MemoryScope.TARGET, "infra-1"),
    ],
)
@pytest.mark.asyncio
async def test_remember_recall_round_trip_per_scope(
    _fake_embedding_service: None,
    scope: MemoryScope,
    target_name: str | None,
) -> None:
    """Every scope persists + reads back with body + metadata intact.

    The tenant_admin role is used for ``TENANT`` writes (operator
    role is denied by the RBAC matrix); operator role is used
    everywhere else. The slug is auto-generated; we read it back from
    the returned entry to drive ``recall``.
    """
    role = TenantRole.TENANT_ADMIN if scope is MemoryScope.TENANT else TenantRole.OPERATOR
    operator = _op(role=role)
    service = MemoryService()
    stored = await service.remember(
        operator=operator,
        scope=scope,
        body="memory body — round-trip test",
        target_name=target_name,
    )
    assert stored.scope is scope
    assert stored.body == "memory body — round-trip test"
    assert stored.user_sub == (
        operator.sub
        if scope in {MemoryScope.USER, MemoryScope.USER_TENANT, MemoryScope.USER_TARGET}
        else None
    )
    assert stored.target_name == target_name

    recalled = await service.recall(
        operator=operator,
        scope=scope,
        slug=stored.slug,
        target_name=target_name,
    )
    assert recalled is not None
    assert recalled.id == stored.id
    assert recalled.body == stored.body
    assert recalled.metadata.get("scope") == scope.value


# ---------------------------------------------------------------------------
# RBAC enforcement at write boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_role_cannot_write_tenant_scope(_fake_embedding_service: None) -> None:
    """``operator`` writing ``TENANT`` -> :class:`PermissionDeniedError`.

    The most load-bearing matrix cell: tenant-shared memory is
    privileged; the promotion path (G5.2 #374) is the only intended
    way an operator gets content into ``TENANT`` scope.
    """
    operator = _op(role=TenantRole.OPERATOR)
    service = MemoryService()
    with pytest.raises(PermissionDeniedError) as excinfo:
        await service.remember(
            operator=operator,
            scope=MemoryScope.TENANT,
            body="should not commit",
        )
    assert excinfo.value.scope is MemoryScope.TENANT


@pytest.mark.asyncio
async def test_read_only_role_cannot_write_anything(_fake_embedding_service: None) -> None:
    """``read_only`` role denied on every scope (sampled via USER)."""
    operator = _op(role=TenantRole.READ_ONLY)
    service = MemoryService()
    with pytest.raises(PermissionDeniedError):
        await service.remember(
            operator=operator,
            scope=MemoryScope.USER,
            body="read-only cannot write",
        )


# ---------------------------------------------------------------------------
# Tenant + user boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_none_for_other_operator_user_scope(
    _fake_embedding_service: None,
) -> None:
    """Operator B cannot recall operator A's user-scoped row in the same tenant.

    The natural-key encoding already hides the row (different
    user_sub -> different source_id), so B's recall lookup returns
    None at the SQL layer before RBAC even runs. This locks in that
    behaviour: a regression to a flat ``source_id="<slug>"`` would
    silently break per-user isolation.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    alice = _op(sub="alice", tenant_id=tenant)
    bob = _op(sub="bob", tenant_id=tenant)
    service = MemoryService()
    stored = await service.remember(
        operator=alice,
        scope=MemoryScope.USER,
        body="alice's private note",
        slug="shared-slug-name",
    )
    # Bob cannot recall Alice's memory even though he knows the slug.
    bob_recall = await service.recall(
        operator=bob,
        scope=MemoryScope.USER,
        slug="shared-slug-name",
    )
    assert bob_recall is None
    # Alice's own recall still works.
    alice_recall = await service.recall(
        operator=alice,
        scope=MemoryScope.USER,
        slug="shared-slug-name",
    )
    assert alice_recall is not None
    assert alice_recall.id == stored.id


@pytest.mark.asyncio
async def test_tenant_boundary_holds_for_recall(_fake_embedding_service: None) -> None:
    """Tenant B's operator cannot recall tenant A's tenant-scoped row.

    Tenant-scoped memory is shared *within* a tenant; cross-tenant
    visibility is explicitly disallowed (consumer-needs.md §G5 "Out
    of scope"). The substrate enforces via ``documents.tenant_id``;
    this test pins the recall-layer behaviour.
    """
    tenant_a = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    tenant_b = uuid.UUID("11111111-1111-1111-1111-111111111111")
    admin_a = _op(sub="admin-a", tenant_id=tenant_a, role=TenantRole.TENANT_ADMIN)
    admin_b = _op(sub="admin-b", tenant_id=tenant_b, role=TenantRole.TENANT_ADMIN)
    service = MemoryService()
    stored_a = await service.remember(
        operator=admin_a,
        scope=MemoryScope.TENANT,
        body="tenant A guardrail",
        slug="t-guard",
    )
    # admin B in tenant B cannot reach tenant A's slug.
    cross = await service.recall(
        operator=admin_b,
        scope=MemoryScope.TENANT,
        slug=stored_a.slug,
    )
    assert cross is None
    # admin A's own recall round-trips cleanly.
    own = await service.recall(
        operator=admin_a,
        scope=MemoryScope.TENANT,
        slug=stored_a.slug,
    )
    assert own is not None


# ---------------------------------------------------------------------------
# Expiry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_entry_filtered_from_recall(_fake_embedding_service: None) -> None:
    """A row whose stored ``expires_at`` is in the past is None on recall.

    G5.2 #374's executor deletes expired rows on a daily cadence;
    G5.1's contract is the read-side filter so an operator never
    sees a stale memory between the moment it expires and the
    moment the executor reaps it.
    """
    operator = _op()
    service = MemoryService()
    past = datetime.now(UTC) - timedelta(hours=1)
    stored = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="should be filtered",
        expires_at=past,
    )
    recalled = await service.recall(
        operator=operator,
        scope=MemoryScope.USER,
        slug=stored.slug,
    )
    assert recalled is None


@pytest.mark.asyncio
async def test_list_filters_expired_by_default_and_surfaces_with_opt_in(
    _fake_embedding_service: None,
) -> None:
    """``list_memories`` hides expired by default; ``include_expired=True`` reveals.

    The opt-in is the diagnostic surface for G5.2's cleanup task
    (audit replay, operator probing "what would be reaped tomorrow")
    -- otherwise the filter is on by default and operators see only
    live entries.
    """
    operator = _op()
    service = MemoryService()
    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    live = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="still live",
        expires_at=future,
    )
    expired = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="already expired",
        expires_at=past,
    )

    default_list = await service.list_memories(operator=operator)
    default_ids = {entry.id for entry in default_list}
    assert live.id in default_ids
    assert expired.id not in default_ids

    full_list = await service.list_memories(operator=operator, include_expired=True)
    full_ids = {entry.id for entry in full_list}
    assert {live.id, expired.id}.issubset(full_ids)


# ---------------------------------------------------------------------------
# target_name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [MemoryScope.USER_TARGET, MemoryScope.TARGET])
@pytest.mark.asyncio
async def test_remember_requires_target_name_for_target_scopes(
    _fake_embedding_service: None,
    scope: MemoryScope,
) -> None:
    """Target-scoped writes without ``target_name`` -> ValueError.

    Surfaces *before* RBAC so a request body validation error is
    not masked as "permission denied". The API layer (T2 #422) maps
    this to 422 Unprocessable Entity; the matrix mismatch is 403.
    """
    operator = _op(role=TenantRole.TENANT_ADMIN)
    service = MemoryService()
    with pytest.raises(ValueError, match="target_name"):
        await service.remember(
            operator=operator,
            scope=scope,
            body="missing target",
        )


# ---------------------------------------------------------------------------
# list_memories filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_memories_filters_by_scope(_fake_embedding_service: None) -> None:
    """``scope`` arg narrows the candidate kinds at the SQL layer."""
    operator = _op()
    service = MemoryService()
    await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="user-scoped",
    )
    await service.remember(
        operator=operator,
        scope=MemoryScope.USER_TENANT,
        body="user-tenant-scoped",
    )
    user_only = await service.list_memories(operator=operator, scope=MemoryScope.USER)
    assert all(entry.scope is MemoryScope.USER for entry in user_only)
    assert len(user_only) == 1


@pytest.mark.asyncio
async def test_list_memories_filters_by_slug_pattern(_fake_embedding_service: None) -> None:
    """``slug_pattern`` filters in-process via substring match.

    Pure substring -- no regex / glob -- so operator-typed values are
    predictable.
    """
    operator = _op()
    service = MemoryService()
    await service.remember(
        operator=operator, scope=MemoryScope.USER, body="x", slug="wine-preference"
    )
    await service.remember(operator=operator, scope=MemoryScope.USER, body="y", slug="k8s-rollout")
    wine_match = await service.list_memories(operator=operator, slug_pattern="wine")
    assert len(wine_match) == 1
    assert wine_match[0].slug == "wine-preference"


@pytest.mark.asyncio
async def test_list_memories_filters_by_tag(_fake_embedding_service: None) -> None:
    """``tag`` matches membership in ``metadata.tags``.

    The list is the caller-provided tag array; missing/malformed
    arrays fail to no-match rather than raise.
    """
    operator = _op()
    service = MemoryService()
    await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="x",
        metadata={"tags": ["k8s", "ops"]},
    )
    await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="y",
        metadata={"tags": ["wine"]},
    )
    k8s_tagged = await service.list_memories(operator=operator, tag="k8s")
    assert len(k8s_tagged) == 1
    assert "k8s" in k8s_tagged[0].metadata.get("tags", [])


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_deletes_row_and_returns_true(_fake_embedding_service: None) -> None:
    """``forget`` removes the row; subsequent recall returns None."""
    operator = _op()
    service = MemoryService()
    stored = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="ephemeral",
    )
    deleted = await service.forget(
        operator=operator,
        scope=MemoryScope.USER,
        slug=stored.slug,
    )
    assert deleted is True
    assert (
        await service.recall(
            operator=operator,
            scope=MemoryScope.USER,
            slug=stored.slug,
        )
        is None
    )


@pytest.mark.asyncio
async def test_forget_returns_false_for_unknown_slug(_fake_embedding_service: None) -> None:
    """``forget`` of a slug that doesn't exist returns False, not raises.

    Idempotent delete: the caller can re-issue a forget without
    worrying about state drift.
    """
    operator = _op()
    service = MemoryService()
    deleted = await service.forget(
        operator=operator,
        scope=MemoryScope.USER,
        slug="never-existed",
    )
    assert deleted is False


@pytest.mark.asyncio
async def test_forget_denied_for_operator_role_on_tenant_scope(
    _fake_embedding_service: None,
) -> None:
    """RBAC mirrors write for delete -- operator cannot reap tenant-shared rows."""
    operator = _op(role=TenantRole.OPERATOR)
    service = MemoryService()
    with pytest.raises(PermissionDeniedError):
        await service.forget(
            operator=operator,
            scope=MemoryScope.TENANT,
            slug="any",
        )


# ---------------------------------------------------------------------------
# search_memories (retrieval substrate mocked)
# ---------------------------------------------------------------------------


def _build_hit(
    *,
    kind: str,
    tenant_id: uuid.UUID,
    user_sub: str | None,
    target_name: str | None,
    slug: str,
    body: str = "stub body",
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RetrievalHit:
    """Build a :class:`RetrievalHit` shaped like one the substrate would emit.

    Helper for the search tests: lets each test pin the metadata
    fields ``MemoryService.search_memories`` post-filters on without
    going through the SQL path. ``created_at`` / ``updated_at``
    default to a deterministic non-epoch instant so a passthrough
    regression (the G0.9.1-T4 #776 fix) surfaces in any test that
    asserts on the result-side timestamps.
    """
    metadata: dict[str, Any] = {
        "scope": kind.removeprefix("memory-"),
        "user_sub": user_sub,
        "target_name": target_name,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }
    default_ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="memory",
        source_id=f"stub:{slug}",
        kind=kind,
        body=body,
        doc_metadata=metadata,
        created_at=created_at if created_at is not None else default_ts,
        updated_at=updated_at if updated_at is not None else default_ts,
        fused_score=0.5,
        bm25_score=0.4,
        cosine_score=0.6,
        bm25_rank=1,
        cosine_rank=1,
    )


@pytest.mark.asyncio
async def test_search_memories_pushes_user_sub_to_substrate_for_user_scopes() -> None:
    """G4.4-T2 (#1179): user-scoped recalls push ``user_sub`` to the substrate.

    Pre-migration the service post-filtered the retriever's hits in
    Python on ``user_sub`` (an operator's search could rank against
    another operator's user-scoped row and drop it after the fact,
    burning the substrate's candidate budget on RBAC-invisible rows).
    Post-migration the predicate rides into the substrate's
    ``metadata_filters`` so the SQL layer eliminates those rows
    upstream.

    Cross-scope (``scope=None``) search fans out per visible kind:
    one ``retrieve`` call per :class:`MemoryScope` member, each with
    its kind-appropriate ``metadata_filters`` dict (``{"user_sub":
    op.sub}`` for user-flavoured kinds, ``None`` for tenant /
    target-flavoured kinds).
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    bob = _op(sub="bob", tenant_id=tenant)
    # Substrate is push-down-correct: simulate it returning only
    # bob-owned rows (alice's user-scoped rows were filtered out at
    # the SQL boundary by ``doc_metadata @> {"user_sub": "bob"}``).
    bob_hit = _build_hit(
        kind="memory-user",
        tenant_id=tenant,
        user_sub="bob",
        target_name=None,
        slug="bob-note",
    )

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(return_value=[bob_hit]),
    ) as retrieve_mock:
        service = MemoryService()
        results = await service.search_memories(operator=bob, query="any")

    # Returned hits carry only operator-visible rows; the substrate
    # is the one enforcing that, not the service post-filter.
    assert len(results) >= 1
    assert all(r.entry.user_sub in (None, "bob") for r in results)

    # Cross-scope (scope=None) fans out one retrieve per visible
    # kind. Inspect the per-kind metadata_filters: user-flavoured
    # kinds push user_sub, tenant/target kinds push None.
    expected_kinds_with_filters = {
        "memory-user": {"user_sub": "bob"},
        "memory-user-tenant": {"user_sub": "bob"},
        "memory-user-target": {"user_sub": "bob"},
        "memory-tenant": None,
        "memory-target": None,
    }
    observed = {}
    for call in retrieve_mock.await_args_list:
        kwargs = call.kwargs
        assert kwargs["source"] == "memory"
        observed[kwargs["kind"]] = kwargs.get("metadata_filters")
    assert observed == expected_kinds_with_filters


@pytest.mark.asyncio
async def test_search_memories_scope_given_user_pushes_single_filter() -> None:
    """``scope=USER`` issues exactly one retrieve with ``{"user_sub": op.sub}``.

    Scope-given recalls take the single-retrieve fast path -- the
    cross-scope fan-out only fires when ``scope=None``. This test
    pins both ends: one call, with the right ``kind`` and
    ``metadata_filters``.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(sub="alice", tenant_id=tenant)
    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(return_value=[]),
    ) as retrieve_mock:
        service = MemoryService()
        await service.search_memories(
            operator=operator,
            query="any",
            scope=MemoryScope.USER,
        )
    retrieve_mock.assert_awaited_once()
    kwargs = retrieve_mock.await_args.kwargs
    assert kwargs["source"] == "memory"
    assert kwargs["kind"] == "memory-user"
    assert kwargs["metadata_filters"] == {"user_sub": "alice"}


@pytest.mark.asyncio
async def test_search_memories_scope_given_tenant_pushes_no_metadata_filter() -> None:
    """``scope=TENANT`` issues one retrieve with ``metadata_filters=None``.

    Within-tenant RBAC for ``TENANT`` / ``TARGET`` scopes is "any
    operator" -- there's no per-row predicate to push down. The
    substrate's ``tenant_id`` filter already enforces the tenant
    boundary upstream, so ``metadata_filters`` is ``None`` here.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(tenant_id=tenant, role=TenantRole.TENANT_ADMIN)
    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(return_value=[]),
    ) as retrieve_mock:
        service = MemoryService()
        await service.search_memories(
            operator=operator,
            query="any",
            scope=MemoryScope.TENANT,
        )
    retrieve_mock.assert_awaited_once()
    kwargs = retrieve_mock.await_args.kwargs
    assert kwargs["kind"] == "memory-tenant"
    assert kwargs["metadata_filters"] is None


@pytest.mark.asyncio
async def test_search_memories_rbac_invisible_rows_no_longer_burn_budget() -> None:
    """G4.4-T2 (#1179) regression: many invisible rows + 1 visible -> 1 hit.

    Pre-migration scenario: tenant has N>50 user-scoped memories
    owned by *alice*, plus 1 user-scoped memory owned by *bob* that
    matches *bob*'s query. The substrate's 50-candidate-per-signal
    budget gets burned on alice's rows (they rank highly on lexical
    match) and bob's matching row falls off the end -- the in-process
    post-filter drops alice's rows and returns ``[]`` to bob.

    Post-migration: the substrate's ``metadata_filters={"user_sub":
    "bob"}`` predicate eliminates alice's rows before candidate
    selection. Bob's row is the only candidate, ranks 1, returns.

    This test mocks the substrate to *behave* as the push-down
    promises -- when called with ``metadata_filters={"user_sub":
    "bob"}`` it returns only bob's row, regardless of what alice's
    rows look like in the simulated corpus. The teeth of the test is
    that the service no longer drops anything in post-retrieval
    Python: every returned hit shows up because the substrate
    pre-filtered, not because the service did.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    bob = _op(sub="bob", tenant_id=tenant)

    async def push_down_aware_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        # Simulate substrate: only return rows matching the
        # metadata_filters dict the caller passed. The kind dispatch
        # is per-scope; bob's match lives in ``memory-user``.
        if kwargs["kind"] != "memory-user":
            return []
        filters = kwargs.get("metadata_filters")
        if filters != {"user_sub": "bob"}:
            # Pre-migration shape (no filter) would have returned 50
            # alice-owned rows + 1 bob row; if the service ever stops
            # passing the filter this branch surfaces it.
            return [
                _build_hit(
                    kind="memory-user",
                    tenant_id=tenant,
                    user_sub="alice",
                    target_name=None,
                    slug=f"alice-note-{i}",
                )
                for i in range(50)
            ] + [
                _build_hit(
                    kind="memory-user",
                    tenant_id=tenant,
                    user_sub="bob",
                    target_name=None,
                    slug="bob-match",
                )
            ]
        return [
            _build_hit(
                kind="memory-user",
                tenant_id=tenant,
                user_sub="bob",
                target_name=None,
                slug="bob-match",
            )
        ]

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(side_effect=push_down_aware_retrieve),
    ):
        service = MemoryService()
        results = await service.search_memories(operator=bob, query="any")

    # Bob sees his row. The push-down is the only reason -- if the
    # service still post-filtered on user_sub the test would still
    # pass, so the real teeth is the metadata_filters assertion above
    # in ``test_search_memories_pushes_user_sub_to_substrate_for_user_scopes``.
    assert len(results) == 1
    assert results[0].entry.slug == "bob-match"
    assert results[0].entry.user_sub == "bob"


@pytest.mark.asyncio
async def test_search_memories_multi_scope_intersection_via_per_kind_fanout() -> None:
    """Cross-scope recall returns hits from multiple kinds, ranked by fused_score.

    Verifies the per-kind fan-out merge: when scope=None and matches
    exist in both a user-flavoured kind (with user_sub push-down) and
    a tenant-flavoured kind (no push-down), both surface, ordered
    by ``fused_score`` desc. The merge is rank-based and
    scale-invariant (RRF), so cross-kind comparison is total-order
    on a comparable scalar.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(sub="alice", tenant_id=tenant)

    user_hit = _build_hit(
        kind="memory-user",
        tenant_id=tenant,
        user_sub="alice",
        target_name=None,
        slug="user-match",
    )
    # Mutate fused_score so we can pin the merge ordering.
    user_hit = user_hit.model_copy(update={"fused_score": 0.7})

    tenant_hit = _build_hit(
        kind="memory-tenant",
        tenant_id=tenant,
        user_sub=None,
        target_name=None,
        slug="tenant-match",
    )
    tenant_hit = tenant_hit.model_copy(update={"fused_score": 0.5})

    async def fanout_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        if kwargs["kind"] == "memory-user":
            assert kwargs["metadata_filters"] == {"user_sub": "alice"}
            return [user_hit]
        if kwargs["kind"] == "memory-tenant":
            assert kwargs.get("metadata_filters") is None
            return [tenant_hit]
        return []

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(side_effect=fanout_retrieve),
    ):
        service = MemoryService()
        results = await service.search_memories(operator=operator, query="any")

    # Both kinds surface, ordered by fused_score desc.
    assert len(results) == 2
    assert results[0].entry.slug == "user-match"
    assert results[0].entry.scope == MemoryScope.USER
    assert results[1].entry.slug == "tenant-match"
    assert results[1].entry.scope == MemoryScope.TENANT


@pytest.mark.asyncio
async def test_search_memories_filters_expired_hits() -> None:
    """Expired hits are dropped from the search result, same as ``list_memories``.

    G4.4-T2 (#1179) note: ``expires_at`` is a *range* predicate
    (``> now()``) which the T1 substrate's containment-only
    ``metadata_filters`` cannot express, so expired rows stay as a
    post-retrieval Python filter in
    :meth:`MemoryService._hit_to_search_result`. RBAC ``user_sub``
    push-down covers the much-larger budget-burn surface (every
    operator's archived memories across the tenant); the expired-row
    pool stays small in practice via the daily reaper.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(tenant_id=tenant)
    past = datetime.now(UTC) - timedelta(hours=1)
    live_hit = _build_hit(
        kind="memory-user",
        tenant_id=tenant,
        user_sub=operator.sub,
        target_name=None,
        slug="live",
    )
    expired_hit = _build_hit(
        kind="memory-user",
        tenant_id=tenant,
        user_sub=operator.sub,
        target_name=None,
        slug="expired",
        expires_at=past,
    )

    async def per_kind_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        # Cross-scope fan-out asks for each kind in turn; the
        # expired/live pair lives under memory-user. The substrate
        # would receive ``metadata_filters={"user_sub": op.sub}`` for
        # user-flavoured kinds, but the mock doesn't need to inspect
        # that here -- the push-down assertions live in the dedicated
        # ``_pushes_user_sub_to_substrate`` test above.
        if kwargs["kind"] == "memory-user":
            return [expired_hit, live_hit]
        return []

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(side_effect=per_kind_retrieve),
    ):
        service = MemoryService()
        results = await service.search_memories(operator=operator, query="any")
    assert len(results) == 1
    assert results[0].entry.slug == "live"


@pytest.mark.asyncio
async def test_search_memories_with_scope_narrows_kind_filter() -> None:
    """``scope`` arg pins ``kind`` on the underlying retrieve call."""
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(tenant_id=tenant, role=TenantRole.TENANT_ADMIN)
    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(return_value=[]),
    ) as retrieve_mock:
        service = MemoryService()
        await service.search_memories(
            operator=operator,
            query="anything",
            scope=MemoryScope.TENANT,
        )
    call_kwargs = retrieve_mock.await_args.kwargs
    assert call_kwargs["kind"] == "memory-tenant"


@pytest.mark.asyncio
async def test_search_memories_passes_through_hit_timestamps() -> None:
    """``created_at`` / ``updated_at`` on the search result match the substrate hit.

    G0.9.1-T4 (#776) regression gate. Before the fix the service
    substituted ``EPOCH`` for both fields because :class:`RetrievalHit`
    didn't carry them; the consumer dogfood (Signal #11) observed
    ``1970-01-01T00:00:00Z`` strings on every search response. This
    test pins the passthrough: a hit with distinct, non-epoch
    ``created_at`` / ``updated_at`` surfaces unchanged on
    ``result.entry``. The default ``_build_hit`` instant already
    differs from ``EPOCH``; this test makes the two timestamps
    distinct so an accidental ``updated_at = created_at`` swap
    surfaces too.
    """
    tenant = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
    operator = _op(tenant_id=tenant)
    created = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    updated = datetime(2026, 5, 21, 10, 20, 33, tzinfo=UTC)
    hit = _build_hit(
        kind="memory-user",
        tenant_id=tenant,
        user_sub=operator.sub,
        target_name=None,
        slug="ts-passthrough",
        created_at=created,
        updated_at=updated,
    )

    async def per_kind_retrieve(**kwargs: Any) -> list[RetrievalHit]:
        # Cross-scope fan-out (``scope=None``) issues one retrieve per
        # visible kind. Return the timestamp-bearing hit only for the
        # ``memory-user`` kind so the test exercises a single result
        # row through ``_hit_to_search_result``.
        if kwargs["kind"] == "memory-user":
            return [hit]
        return []

    with patch(
        "meho_backplane.memory.service.retrieve",
        new=AsyncMock(side_effect=per_kind_retrieve),
    ):
        service = MemoryService()
        results = await service.search_memories(operator=operator, query="any")

    assert len(results) == 1
    assert results[0].entry.created_at == created
    assert results[0].entry.updated_at == updated
    # Defence in depth: explicitly disallow the old EPOCH placeholder
    # so a regression to "swap in a sentinel that looks real" is
    # caught here, not just by the equality assertions above.
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert results[0].entry.created_at != epoch
    assert results[0].entry.updated_at != epoch


# ---------------------------------------------------------------------------
# Slug validation (B1 regression — round-trip asymmetry on colon-bearing slugs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_slug", ["foo:bar", "k8s:prod", "a:b:c", "with space", "slash/path"])
@pytest.mark.asyncio
async def test_remember_rejects_slugs_with_colons_or_unsafe_chars(
    _fake_embedding_service: None, bad_slug: str
) -> None:
    """Slugs outside the safe set raise before the indexer is reached.

    The ``source_id`` encoding scheme joins on ``:`` and reverses with
    ``rsplit(':', 1)`` -- a slug containing ``:`` would round-trip
    asymmetrically (stored ``source_id='user:<sub>:foo:bar'`` decodes
    back to ``slug='bar'``). The service-layer ``validate_slug`` is
    the gate for direct-import callers; ``MemoryEntryCreate``'s
    pydantic ``Field(pattern=...)`` guards request bodies through the
    API surface (T2 #422). Both paths must reject.
    """
    operator = _op()
    service = MemoryService()
    with pytest.raises(ValueError, match="slug"):
        await service.remember(
            operator=operator,
            scope=MemoryScope.USER,
            body="x",
            slug=bad_slug,
        )


def test_memory_entry_create_rejects_slug_with_colon() -> None:
    """Pydantic request-body path rejects unsafe slugs at construction.

    Mirrors the service-layer guard in
    :func:`test_remember_rejects_slugs_with_colons_or_unsafe_chars`
    so the API surface (T2 #422) inherits the same constraint via
    :class:`MemoryEntryCreate` without re-validating in route code.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MemoryEntryCreate(scope=MemoryScope.USER, body="x", slug="foo:bar")


@pytest.mark.parametrize("good_slug", ["wine-preference", "k8s.rollout-note", "abc123", "a_b_c"])
@pytest.mark.asyncio
async def test_remember_accepts_safe_slugs(_fake_embedding_service: None, good_slug: str) -> None:
    """Slugs in the safe-URL alphabet (letters, digits, ``-_.``) round-trip cleanly.

    Pins that the safe set is wide enough for the operator-friendly
    identifiers consumer-needs.md §G5 names (``wine-preference``,
    ``k8s-rollout``, ``project-context``) plus the
    :func:`auto_slug` hex-prefix shape.
    """
    operator = _op()
    service = MemoryService()
    stored = await service.remember(
        operator=operator,
        scope=MemoryScope.USER,
        body="x",
        slug=good_slug,
    )
    assert stored.slug == good_slug
    recalled = await service.recall(operator=operator, scope=MemoryScope.USER, slug=good_slug)
    assert recalled is not None
    assert recalled.slug == good_slug


# ---------------------------------------------------------------------------
# encode_source_id defensive raise (m1 — replace assert with explicit raise)
# ---------------------------------------------------------------------------


def test_encode_source_id_raises_when_target_name_missing() -> None:
    """Direct-import callers get a clear error, not a silent ``None`` interpolation.

    The service layer's :func:`_require_target_name` guards before
    reaching the helper, but a test / future adapter calling
    :func:`encode_source_id` directly with ``target_name=None`` on a
    target-scoped value would have silently produced
    ``"user-target:<sub>:None:<slug>"`` under ``python -O`` (which
    strips ``assert``). Explicit raise locks the invariant.
    """
    from meho_backplane.memory._internal import encode_source_id

    for scope in (MemoryScope.USER_TARGET, MemoryScope.TARGET):
        with pytest.raises(ValueError, match="target_name required"):
            encode_source_id(
                scope=scope,
                user_sub="op-42",
                target_name=None,
                slug="any",
            )


# ---------------------------------------------------------------------------
# Document-table contract — the rows look right
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_writes_source_memory_and_expected_kind(
    _fake_embedding_service: None,
) -> None:
    """The row on disk has ``source='memory'`` and ``kind='memory-<scope>'``.

    Locks the substrate contract the indexer accepts: a regression
    that changed the source name or the kind prefix would silently
    skew every downstream filter (retrieval source filter, G5.2
    cleanup-by-kind, audit aggregations).
    """
    operator = _op()
    service = MemoryService()
    stored = await service.remember(
        operator=operator,
        scope=MemoryScope.USER_TENANT,
        body="probe",
        slug="probe-slug",
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(Document).where(Document.id == stored.id))
        row = result.scalar_one()
    assert row.source == "memory"
    assert row.kind == "memory-user-tenant"
    assert row.source_id == f"user-tenant:{operator.sub}:probe-slug"
    assert row.doc_metadata.get("user_sub") == operator.sub
    assert row.doc_metadata.get("scope") == "user-tenant"
