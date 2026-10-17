# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit coverage for the per-principal isolation predicate at the
:func:`meho_backplane.retrieval.retriever.retrieve` boundary (#1797).

These tests pin the *wiring* of the SEV-1 cross-principal memory-leak
fix without spinning up Postgres:

* ``principal_sub`` threads from :func:`retrieve` into **both** the BM25
  and cosine candidate helpers' bind dicts.
* It threads regardless of any client-supplied ``metadata_filters`` --
  the two are independent bind params, so a client value can never strip
  the enforced predicate's bind (the SQL ANDs them; the predicate wins).
* ``principal_sub=None`` (the default) leaves the bind ``None`` so the
  ``CAST(:principal_sub AS text) IS NULL`` arm short-circuits the
  predicate for callers that opt out.
* The boundary's hardcoded user-scoped-kind / memory-source literals
  stay in lock-step with the memory scope model
  (:data:`meho_backplane.memory.schemas.USER_SCOPED`,
  :data:`meho_backplane.memory._internal.MEMORY_SOURCE`) -- a drift would
  silently reopen the leak.
* The shared predicate SQL has the exact boolean shape that keeps
  tenant-broadcast and non-memory rows visible while gating user-scoped
  memory rows on ``user_sub``.

PG-real proof of the *behaviour* (the bidirectional canary probe through
the HTTP route and the MCP resource, the non-override probe, and the
no-over-correction probe) lives in
:mod:`tests.integration.test_retrieve_principal_isolation_e2e`, which is
Docker-gated; this module is the always-on first line so the fix's
contract is exercised in every sandbox, not only CI.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from meho_backplane.memory._internal import MEMORY_SOURCE
from meho_backplane.memory.schemas import USER_SCOPED, kind_for_scope
from meho_backplane.retrieval.retriever import (
    _BM25_CANDIDATES_SQL,
    _COSINE_CANDIDATES_SQL,
    _MEMORY_ELIGIBILITY_PREDICATE_SQL,
    _MEMORY_SOURCE,
    _PRINCIPAL_PREDICATE_SQL,
    _USER_SCOPED_MEMORY_KINDS,
    MemoryEligibility,
    retrieve,
)
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


def _capture_candidate_calls() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return two dicts the patched candidate fakes populate with their args."""
    return {}, {}


@pytest.mark.asyncio
async def test_principal_sub_threads_to_both_candidate_helpers() -> None:
    """``principal_sub`` reaches both per-signal helpers identically.

    The fix is only sound if *both* candidate queries carry the
    predicate; if one signal dropped it, that signal's top-50 could
    still surface another principal's user-scoped rows. Assert the
    value arrives at BM25 and cosine byte-for-byte.
    """
    captured_bm25, captured_cosine = _capture_candidate_calls()

    async def fake_bm25(*args: Any, **kwargs: Any) -> list[Any]:
        # principal_sub is the 7th positional arg (after session,
        # tenant_id, query, source, kind, metadata_filters_json).
        captured_bm25["principal_sub"] = args[6]
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        # cosine: (session, tenant_id, embedding_literal, source, kind,
        # metadata_filters_json, principal_sub).
        captured_cosine["principal_sub"] = args[6]
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch("meho_backplane.retrieval.retriever._bm25_candidates", side_effect=fake_bm25),
        patch("meho_backplane.retrieval.retriever._cosine_candidates", side_effect=fake_cosine),
    ):
        await retrieve(
            tenant_id=uuid.uuid4(),
            query="anything",
            source="memory",
            principal_sub="principal-A-sub",
            session=MagicMock(),
        )

    assert captured_bm25["principal_sub"] == "principal-A-sub"
    assert captured_cosine["principal_sub"] == "principal-A-sub"


@pytest.mark.asyncio
async def test_principal_sub_threads_even_with_client_metadata_filters() -> None:
    """A client ``metadata_filters`` cannot strip the enforced ``principal_sub`` bind.

    This is the non-override contract at the wiring layer: the two are
    independent bind params. A client passing
    ``metadata_filters={"user_sub": "<other-sub>"}`` still results in
    ``principal_sub`` being bound to the *caller's own* sub, so the SQL
    ANDs ``metadata @> {"user_sub": <other>}`` with
    ``metadata ->> 'user_sub' = <caller>`` -- the enforced clause always
    also has to hold, so the client value can only narrow the visible
    set, never widen it to another principal's rows. PG-real proof that
    the victim's canary is absent lives in the integration probe.
    """
    captured_bm25, captured_cosine = _capture_candidate_calls()

    async def fake_bm25(*args: Any, **kwargs: Any) -> list[Any]:
        captured_bm25["metadata_filters_json"] = args[5]
        captured_bm25["principal_sub"] = args[6]
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        captured_cosine["metadata_filters_json"] = args[5]
        captured_cosine["principal_sub"] = args[6]
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch("meho_backplane.retrieval.retriever._bm25_candidates", side_effect=fake_bm25),
        patch("meho_backplane.retrieval.retriever._cosine_candidates", side_effect=fake_cosine),
    ):
        await retrieve(
            tenant_id=uuid.uuid4(),
            query="anything",
            source="memory",
            metadata_filters={"user_sub": "victim-other-sub"},
            principal_sub="caller-own-sub",
            session=MagicMock(),
        )

    # The client's adversarial user_sub is still serialised into the
    # containment filter, but the enforced principal_sub binds to the
    # caller, so the predicate cannot be widened to the victim.
    assert '"user_sub": "victim-other-sub"' in captured_bm25["metadata_filters_json"]
    assert captured_bm25["principal_sub"] == "caller-own-sub"
    assert captured_cosine["principal_sub"] == "caller-own-sub"


@pytest.mark.asyncio
async def test_principal_sub_defaults_to_none_for_opt_out_callers() -> None:
    """``principal_sub`` defaults to ``None`` so the predicate is inert.

    In-process callers that already scope by ``user_sub`` themselves
    (or query non-principal sources) get the pre-#1797 behaviour: the
    bind is ``None`` and ``CAST(:principal_sub AS text) IS NULL``
    short-circuits the predicate.
    """
    captured_bm25, _ = _capture_candidate_calls()

    async def fake_bm25(*args: Any, **kwargs: Any) -> list[Any]:
        captured_bm25["principal_sub"] = args[6]
        return []

    async def fake_cosine(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)

    with (
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        ),
        patch("meho_backplane.retrieval.retriever._bm25_candidates", side_effect=fake_bm25),
        patch("meho_backplane.retrieval.retriever._cosine_candidates", side_effect=fake_cosine),
    ):
        await retrieve(tenant_id=uuid.uuid4(), query="q", session=MagicMock())

    assert captured_bm25["principal_sub"] is None


def test_boundary_constants_track_memory_scope_model() -> None:
    """The boundary's hardcoded literals mirror the memory scope model.

    The retrieval substrate spells the user-scoped kinds + memory source
    as local literals (rather than importing the memory package) so the
    shared substrate does not invert its dependency onto a consumer. This
    test is the guard that keeps the two in lock-step: a new user-scoped
    scope added to :data:`USER_SCOPED` without updating
    :data:`_USER_SCOPED_MEMORY_KINDS` would leave that scope's rows
    ungated -- a silent reopening of the leak. Fail loud here instead.
    """
    assert {kind_for_scope(scope) for scope in USER_SCOPED} == _USER_SCOPED_MEMORY_KINDS
    assert _MEMORY_SOURCE == MEMORY_SOURCE
    # The broadcast kinds must NOT be gated -- they carry user_sub=null
    # and are tenant-visible by design (no over-correction).
    assert "memory-tenant" not in _USER_SCOPED_MEMORY_KINDS
    assert "memory-target" not in _USER_SCOPED_MEMORY_KINDS


def test_principal_predicate_sql_has_fail_open_for_non_memory_and_broadcast() -> None:
    """The shared predicate keeps non-memory + broadcast rows visible.

    The boolean shape is load-bearing:

    * ``CAST(:principal_sub AS text) IS NULL`` -> predicate inert when
      the caller opts out.
    * ``source <> 'memory'`` -> kb / operations / docs rows unaffected.
    * ``kind NOT IN (<user-scoped>)`` -> ``memory-tenant`` /
      ``memory-target`` broadcast rows unaffected.
    * ``metadata ->> 'user_sub' = :principal_sub`` -> the only arm that
      actually gates: a user-scoped memory row is visible iff the caller
      wrote it.

    Asserting the SQL text directly catches an accidental edit that, say,
    dropped the ``source <> 'memory'`` disjunct (which would wrongly
    exclude every kb row for any principal-scoped retrieve).
    """
    sql = " ".join(_PRINCIPAL_PREDICATE_SQL.split())
    assert "CAST(:principal_sub AS text) IS NULL" in sql
    assert "source <> 'memory'" in sql
    assert "kind NOT IN ('memory-user', 'memory-user-tenant', 'memory-user-target')" in sql
    assert "metadata ->> 'user_sub' = :principal_sub" in sql
    # Every spelled-out user-scoped kind in the SQL must be one the scope
    # model actually classifies as user-scoped (no typo'd kind that would
    # leave a real kind ungated).
    for kind in _USER_SCOPED_MEMORY_KINDS:
        assert kind in sql


# ---------------------------------------------------------------------------
# Memory eligibility (#3741)
# ---------------------------------------------------------------------------


def _patched_substrate(captured: list[tuple[Any, ...]]) -> Any:
    """Patch embedding + both candidate helpers; record positional args."""

    async def fake_candidates(*args: Any, **kwargs: Any) -> list[Any]:
        captured.append(args)
        return []

    fake_embedding_service = MagicMock()
    fake_embedding_service.encode_one = AsyncMock(return_value=[0.0] * 384)
    stack = ExitStack()
    stack.enter_context(
        patch(
            "meho_backplane.retrieval.retriever.get_embedding_service",
            return_value=fake_embedding_service,
        )
    )
    stack.enter_context(
        patch("meho_backplane.retrieval.retriever._bm25_candidates", side_effect=fake_candidates)
    )
    stack.enter_context(
        patch("meho_backplane.retrieval.retriever._cosine_candidates", side_effect=fake_candidates)
    )
    stack.enter_context(
        patch(
            "meho_backplane.retrieval.retriever._log_unreadable_memory_expiry",
            new=AsyncMock(),
        )
    )
    return stack


@pytest.mark.asyncio
async def test_memory_eligibility_threads_kinds_and_principal_to_both_signals() -> None:
    """Both signals get the eligibility's principal and kinds before ranking."""
    tenant = uuid.uuid4()
    eligibility = MemoryEligibility(
        tenant_id=tenant, principal_sub="principal-A", kinds=("memory-user", "memory-tenant")
    )
    captured: list[tuple[Any, ...]] = []
    with _patched_substrate(captured):
        await retrieve(
            tenant_id=tenant,
            query="anything",
            source="memory",
            memory_eligibility=eligibility,
            session=MagicMock(),
        )
    assert len(captured) == 2
    for args in captured:
        assert args[6] == "principal-A"
        assert args[7] == ["memory-user", "memory-tenant"]


@pytest.mark.asyncio
async def test_no_memory_eligibility_binds_no_kind_limit() -> None:
    """Without eligibility the kinds bind is ``None`` (expiry still applies in SQL)."""
    captured: list[tuple[Any, ...]] = []
    with _patched_substrate(captured):
        await retrieve(tenant_id=uuid.uuid4(), query="anything", session=MagicMock())
    assert [args[7] for args in captured] == [None, None]


@pytest.mark.asyncio
async def test_memory_eligibility_tenant_mismatch_fails_loudly() -> None:
    """An eligibility built for another tenant is a programming error."""
    eligibility = MemoryEligibility(
        tenant_id=uuid.uuid4(), principal_sub="principal-A", kinds=("memory-user",)
    )
    with pytest.raises(ValueError, match="tenant_id"):
        await retrieve(
            tenant_id=uuid.uuid4(),
            query="anything",
            memory_eligibility=eligibility,
            session=MagicMock(),
        )


@pytest.mark.asyncio
async def test_memory_eligibility_principal_mismatch_fails_loudly() -> None:
    """``principal_sub`` cannot disagree with the eligibility's principal."""
    tenant = uuid.uuid4()
    eligibility = MemoryEligibility(
        tenant_id=tenant, principal_sub="principal-A", kinds=("memory-user",)
    )
    with pytest.raises(ValueError, match="principal_sub"):
        await retrieve(
            tenant_id=tenant,
            query="anything",
            principal_sub="principal-B",
            memory_eligibility=eligibility,
            session=MagicMock(),
        )


def test_memory_eligibility_is_frozen_and_never_empty() -> None:
    """The projection cannot be widened after it is built, nor built empty."""
    eligibility = MemoryEligibility(
        tenant_id=uuid.uuid4(), principal_sub="principal-A", kinds=("memory-user",)
    )
    with pytest.raises(ValidationError):
        eligibility.kinds = ("memory-user", "memory-tenant")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        MemoryEligibility(tenant_id=uuid.uuid4(), principal_sub="principal-A", kinds=())


def test_eligibility_predicate_guards_expiry_before_any_cast() -> None:
    """The expiry arm checks shape and validity before it casts, in both statements."""
    sql = _MEMORY_ELIGIBILITY_PREDICATE_SQL
    assert sql.index("pg_input_is_valid") < sql.index("CAST(metadata ->> 'expires_at'")
    assert "WHEN metadata ->> 'expires_at' IS NULL THEN true" in sql
    assert "> now()" in sql
    assert "timezone('UTC', now())" in sql
    needle = " ".join(sql.split())
    for statement in (_BM25_CANDIDATES_SQL, _COSINE_CANDIDATES_SQL):
        haystack = " ".join(str(statement.text).split())
        assert needle in haystack
        # The predicate sits in WHERE, before ORDER BY / LIMIT.
        assert haystack.index(needle) < haystack.index("ORDER BY")
