# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgreSQL 16 proof that Memory eligibility runs before ranking (#3741).

The eligibility and expiry rules live only in the candidate SQL in
:mod:`meho_backplane.retrieval.retriever`, so they need a real pgvector
cluster (``pgvector/pgvector:pg16``, Docker-gated like the rest of
``tests/integration/``). The suite proves:

* expired rows that would rank first cannot take a candidate slot from a
  lower-ranked eligible row (more expired rows than ``CANDIDATE_LIMIT``);
* the guarded expiry cast: canonical UTC, ``Z``, non-UTC offsets, values
  without an offset (read as UTC), missing / ``null`` (never expires), and
  malformed, ``now``, ``epoch``, ``infinity`` values (excluded, without a
  query error);
* a capped diagnostic log for unreadable expiry values that holds no raw
  value;
* tenant / principal / scope / target eligibility through the Memory
  service, ``retrieve(source="memory")`` and all-source ``retrieve``;
* non-Memory rows are unaffected.

Embedding is a deterministic bag-of-words stub so the suite needs no model
download.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.memory.rbac import MemoryRbacResolver
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
from meho_backplane.retrieval.indexer import index_document
from meho_backplane.retrieval.retriever import CANDIDATE_LIMIT, retrieve

from .conftest import DOCKER_AVAILABLE, SKIP_REASON

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
SUB_A = "00000000-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUB_B = "00000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
QUERY = "vault rotation runbook canary"
_MISSING = object()


def _vector(text: str) -> list[float]:
    """Deterministic 384-dim bag-of-words vector (process-stable)."""
    v = [0.0] * 384
    for token in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        v[h % 384] += 1.0
        v[(h * 31) % 384] += 0.5
    magnitude = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / magnitude for x in v]


@pytest.fixture
def stub_embedding() -> Iterator[None]:
    """Patch the embedding service on the indexer and retriever import sites."""
    fake = AsyncMock()
    fake.encode_one.side_effect = _vector
    fake.dimension = 384
    with (
        patch("meho_backplane.retrieval.indexer.get_embedding_service", return_value=fake),
        patch("meho_backplane.retrieval.retriever.get_embedding_service", return_value=fake),
    ):
        yield


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _stage(
    slug: str,
    *,
    scope: MemoryScope = MemoryScope.TENANT,
    owner: str | None = None,
    target_name: str | None = None,
    expires: Any = _MISSING,
    body: str = QUERY,
    tenant_id: uuid.UUID = TENANT,
) -> None:
    """Write one memory row with exact metadata (bypassing write-side checks)."""
    metadata: dict[str, Any] = {
        "scope": scope.value,
        "user_sub": owner,
        "target_name": target_name,
    }
    if expires is not _MISSING:
        metadata["expires_at"] = expires
    await index_document(
        tenant_id=tenant_id,
        source="memory",
        source_id=f"{scope.value}:{owner or '-'}:{target_name or '-'}:{slug}",
        kind=f"memory-{scope.value}",
        body=f"{body} {slug}",
        metadata=metadata,
    )


def _slugs(hits: list[Any]) -> set[str]:
    """Slug of each hit: a Memory search hit's ``slug``, else the last body word."""
    out: set[str] = set()
    for hit in hits:
        out.add(hit.slug if hasattr(hit, "slug") else hit.body.rsplit(" ", 1)[-1])
    return out


# ---------------------------------------------------------------------------
# Expired rows cannot take candidate slots
# ---------------------------------------------------------------------------


async def test_expired_top_ranked_rows_do_not_crowd_out_eligible_hit(
    pg_engine: None, stub_embedding: None
) -> None:
    """More expired exact matches than ``CANDIDATE_LIMIT`` still leave room for a live row."""
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    for i in range(CANDIDATE_LIMIT + 10):
        await _stage(f"expired{i}", owner=SUB_A, scope=MemoryScope.USER, expires=past)
    # Shares fewer query words, so it ranks below every expired row.
    await _stage("liveweak", owner=SUB_A, scope=MemoryScope.USER, body="vault rotation notes")

    results = await MemoryService().search_memories(_operator(SUB_A), QUERY, limit=5)

    assert _slugs(results) == {"liveweak"}


# ---------------------------------------------------------------------------
# Guarded expiry cast
# ---------------------------------------------------------------------------


def _expiry_cases() -> dict[str, tuple[Any, bool]]:
    """slug -> (stored expires_at value, expected visible)."""
    now = datetime.now(UTC)
    plus_0530 = timezone(timedelta(hours=5, minutes=30))
    return {
        "missingkey": (_MISSING, True),
        "jsonnull": (None, True),
        "futureutc": ((now + timedelta(hours=2)).isoformat(), True),
        "futurez": ((now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), True),
        "pastutc": ((now - timedelta(hours=2)).isoformat(), False),
        "futureoffset": ((now + timedelta(hours=2)).astimezone(plus_0530).isoformat(), True),
        # The wall clock reads ~4h ahead of UTC, but the instant is in the past.
        "pastoffset": ((now - timedelta(hours=1)).astimezone(plus_0530).isoformat(), False),
        "naivefuture": ((now + timedelta(hours=2)).replace(tzinfo=None).isoformat(), True),
        "naivepast": ((now - timedelta(hours=2)).replace(tzinfo=None).isoformat(), False),
        "garbage": ("not-a-date", False),
        "badmonth": ("2026-13-45T00:00:00+00:00", False),
        "nowword": ("now", False),
        "infinity": ("infinity", False),
        "neginfinity": ("-infinity", False),
        "epochword": ("epoch", False),
        "number": (4102444800, False),
    }


_UNREADABLE = {"garbage", "badmonth", "nowword", "infinity", "neginfinity", "epochword", "number"}


async def _stage_expiry_cases() -> set[str]:
    cases = _expiry_cases()
    for slug, (value, _visible) in cases.items():
        await _stage(slug, expires=value)
    return {slug for slug, (_value, visible) in cases.items() if visible}


async def test_expiry_cases_through_memory_search(pg_engine: None, stub_embedding: None) -> None:
    """The service sees exactly the unexpired, readable rows -- and the query never errors."""
    expected = await _stage_expiry_cases()
    with capture_logs() as logs:
        results = await MemoryService().search_memories(_operator(SUB_A), QUERY, limit=50)

    assert _slugs(results) == expected
    events = [e for e in logs if e["event"] == "memory_expiry_unreadable_excluded"]
    assert len(events) == 1
    assert events[0]["excluded_count"] == len(_UNREADABLE)
    assert events[0]["count_capped"] is False
    serialised = json.dumps(events[0], default=str)
    for raw in ("not-a-date", "2026-13-45", "infinity", "epoch", "4102444800"):
        assert raw not in serialised


async def test_expiry_applies_to_all_source_retrieve_without_eligibility(
    pg_engine: None, stub_embedding: None
) -> None:
    """Even a caller with no eligibility value cannot rank an expired memory.

    The session runs in a non-UTC time zone (UTC+05:30). If the SQL cast
    read offset-less values in the session zone instead of UTC,
    ``naivefuture`` (now + 2h) would land 3.5h in the past and drop out.
    """
    expected = await _stage_expiry_cases()
    async with get_sessionmaker()() as session:
        await session.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
        assert (await session.execute(text("SHOW TimeZone"))).scalar_one() == "Asia/Kolkata"
        hits = await retrieve(TENANT, QUERY, limit=50, session=session)
    assert _slugs(hits) == expected


# ---------------------------------------------------------------------------
# Tenant / principal / scope / target eligibility
# ---------------------------------------------------------------------------


async def _stage_matrix() -> None:
    for owner, tag in ((SUB_A, "a"), (SUB_B, "b")):
        await _stage(f"user{tag}", scope=MemoryScope.USER, owner=owner)
        await _stage(f"usertenant{tag}", scope=MemoryScope.USER_TENANT, owner=owner)
        await _stage(
            f"usertarget{tag}", scope=MemoryScope.USER_TARGET, owner=owner, target_name="target-x"
        )
    await _stage("shared", scope=MemoryScope.TENANT)
    await _stage("sharedtarget", scope=MemoryScope.TARGET, target_name="target-x")
    await _stage("othertenant", scope=MemoryScope.TENANT, tenant_id=OTHER_TENANT)
    await index_document(
        tenant_id=TENANT,
        source="kb",
        source_id="kb-entry-1",
        kind="kb-entry",
        body=f"{QUERY} kbentry",
        metadata={},
    )


_B_VISIBLE = {"userb", "usertenantb", "usertargetb", "shared", "sharedtarget"}


async def test_cross_scope_search_sees_only_eligible_rows(
    pg_engine: None, stub_embedding: None
) -> None:
    """B sees B's own rows plus shared tenant/target rows; nothing of A's or another tenant's."""
    await _stage_matrix()
    results = await MemoryService().search_memories(_operator(SUB_B), QUERY, limit=50)
    assert _slugs(results) == _B_VISIBLE


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (MemoryScope.USER, {"userb"}),
        (MemoryScope.USER_TENANT, {"usertenantb"}),
        (MemoryScope.USER_TARGET, {"usertargetb"}),
        (MemoryScope.TENANT, {"shared"}),
        (MemoryScope.TARGET, {"sharedtarget"}),
    ],
)
async def test_scoped_search_sees_only_that_scope(
    pg_engine: None, stub_embedding: None, scope: MemoryScope, expected: set[str]
) -> None:
    """Each scope narrows to its own kind; user scopes stay owner-only."""
    await _stage_matrix()
    results = await MemoryService().search_memories(_operator(SUB_B), QUERY, scope=scope)
    assert _slugs(results) == expected


async def test_foreign_rows_look_like_no_match(pg_engine: None, stub_embedding: None) -> None:
    """Only A's user rows match the query: B gets the same empty list as a miss."""
    await _stage("privatea", scope=MemoryScope.USER, owner=SUB_A, body="zebra quartz")
    service = MemoryService()
    foreign = await service.search_memories(
        _operator(SUB_B), "zebra quartz", scope=MemoryScope.USER
    )
    miss = await service.search_memories(
        _operator(SUB_B), "nothing matches", scope=MemoryScope.USER
    )
    assert foreign == miss == []


async def test_generic_retrieve_memory_and_all_sources_share_eligibility(
    pg_engine: None, stub_embedding: None
) -> None:
    """``source="memory"`` and omitted source use the same eligibility; KB stays compatible."""
    await _stage_matrix()
    eligibility = MemoryRbacResolver().eligibility(_operator(SUB_B))

    memory_only = await retrieve(
        TENANT, QUERY, source="memory", limit=50, memory_eligibility=eligibility
    )
    all_sources = await retrieve(TENANT, QUERY, limit=50, memory_eligibility=eligibility)
    kb_only = await retrieve(TENANT, QUERY, source="kb", limit=50, principal_sub=SUB_B)

    assert _slugs(memory_only) == _B_VISIBLE
    assert _slugs(all_sources) == _B_VISIBLE | {"kbentry"}
    assert _slugs(kb_only) == {"kbentry"}
