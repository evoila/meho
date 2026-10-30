# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The caller's active-claim lookup reads the announcement archive (#4091).

``announce_claim_gap`` decides whether the caller holds an active announce
claim that covers a write-class op, and if not, why. The reflex advisory
and the opt-in announce gate both use it.

Before #4091 the lookup read the newest 100 entries of the Valkey stream.
On a busy tenant the caller's claim fell out of those 100 entries while
``meho_broadcast_recent`` still listed it as active, so the advisory told
the agent to announce again. These tests store claims in the archive only
and fail if anything reads the stream.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import announce_claim_lookup
from meho_backplane.broadcast import client as broadcast_client
from meho_backplane.broadcast.announce_claim_lookup import (
    announce_claim_gap,
    caller_has_active_announce_claim,
)
from meho_backplane.broadcast.announce_gate import (
    announce_gate_blocks,
    reset_announce_gate_cache_for_testing,
)
from meho_backplane.broadcast.reflex import (
    _ANNOUNCE_NUDGES,
    REFLEX_ADVISORY_EXTRAS_KEY,
    build_reflex_advisory,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.settings import get_settings

from ._request_usage_helpers import seed_usage_event
from .broadcast_admission_helpers import seed_announcement

_TENANT = UUID("00000000-0000-0000-0000-000000004091")
_SESSION = UUID("40914091-4091-4091-4091-409140914091")
_ME = "agent-op"
_VC = "vc-lab-1"
#: A generic vCenter op the issue saw the wrong advisory on; ``write`` class.
_OP = "POST:/content/library/item"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    reset_announce_gate_cache_for_testing()
    yield
    reset_announce_gate_cache_for_testing()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_stream_read() -> Iterator[None]:
    """The lookup must read only the archive, never the Valkey stream.

    Every module reaches Valkey through the one shared client, so a stand-in
    client that fails on a stream read catches it wherever the read happens.
    """
    stream_read = AssertionError("the claim lookup read the stream")
    stand_in = MagicMock(
        **{f"{command}.side_effect": stream_read for command in ("xrange", "xrevrange", "xread")}
    )
    with patch.object(broadcast_client, "_CLIENT", new=stand_in):
        yield


def _operator(sub: str = _ME, *, tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _claim(*, minutes_ago: float = 1, **fields: Any) -> None:
    """Store one announcement of the caller (by default an active write claim)."""
    values: dict[str, Any] = {
        "principal_sub": _ME,
        "planned_op_class": "write",
        "ttl_minutes": 60,
        "ts": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }
    values.update(fields)
    await seed_announcement(_TENANT, **values)


async def _gap(target_name: str | None = _VC, *, op_id: str = _OP) -> str | None:
    return await announce_claim_gap(_operator(), op_id=op_id, target_name=target_name)


# ---------------------------------------------------------------------------
# The case from the issue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rows", ["start", "update", "both"])
async def test_issue_claims_cover_the_target(rows: str) -> None:
    """A multi-target start and a single-target update each cover the op.

    Same shape as the issue: a ``start`` naming six targets with a 600
    minute TTL, then an ``update`` of the same claim naming one target
    with a 240 minute TTL.
    """
    claim_id = uuid.uuid4()
    if rows in ("start", "both"):
        await _claim(
            minutes_ago=24,
            phase="start",
            claim_id=claim_id,
            targets=[_VC, "nsx-1", "sddc-1", "esx-1", "esx-2", "esx-3"],
            ttl_minutes=600,
            work_ref="gh:evoila/meho#1",
        )
    if rows in ("update", "both"):
        await _claim(
            minutes_ago=0,
            phase="update",
            claim_id=claim_id,
            target=_VC,
            ttl_minutes=240,
            work_ref="gh:evoila/meho#1",
        )
    assert await _gap() is None
    assert await caller_has_active_announce_claim(_operator(), op_id=_OP, target_name=_VC)


async def test_peer_announcements_do_not_hide_the_callers_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newer announcements of other principals never push the claim out.

    The old stream read kept the newest entries of the whole tenant. The
    archive lookup keeps only the caller's rows, so even with a tiny cap the
    claim is found behind more peer rows than the cap.
    """
    monkeypatch.setattr(announce_claim_lookup, "_CLAIM_SCAN_LIMIT", 3)
    await _claim(minutes_ago=10, target=_VC)
    for i in range(5):
        await _claim(minutes_ago=5 - i, principal_sub=f"peer-{i}", target=_VC)
    assert await _gap() is None


# ---------------------------------------------------------------------------
# Why no claim covers the op
# ---------------------------------------------------------------------------


async def test_no_claim_at_all() -> None:
    assert await _gap() == "no_claim"
    assert not await caller_has_active_announce_claim(_operator(), op_id=_OP, target_name=_VC)


async def test_claims_of_other_principals_and_tenants_do_not_count() -> None:
    await _claim(principal_sub="someone-else", target=_VC)
    await seed_announcement(
        uuid.uuid4(), principal_sub=_ME, target=_VC, planned_op_class="write", ttl_minutes=60
    )
    assert await _gap() == "no_claim"


async def test_expired_claim() -> None:
    await _claim(minutes_ago=30, target=_VC, ttl_minutes=10)
    assert await _gap() == "expired"


async def test_active_claim_for_another_target() -> None:
    await _claim(targets=["vc-other", "nsx-1"])
    assert await _gap() == "other_target"


async def test_active_claim_with_another_op_class() -> None:
    await _claim(target=_VC, planned_op_class="read")
    assert await _gap() == "other_op_class"


async def test_expired_claim_for_another_target_is_no_claim() -> None:
    await _claim(minutes_ago=30, target="vc-other", ttl_minutes=10)
    assert await _gap() == "no_claim"


async def test_claim_older_than_a_day_is_not_read() -> None:
    await _claim(minutes_ago=25 * 60, target=_VC, ttl_minutes=None)
    assert await _gap() == "no_claim"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        (
            [{"minutes_ago": 30, "target": _VC, "ttl_minutes": 10}, {"target": "vc-other"}],
            "expired",
        ),
        ([{"target": _VC, "planned_op_class": "read"}, {"target": "vc-other"}], "other_op_class"),
    ],
)
async def test_the_closest_reason_wins(claims: list[dict[str, Any]], expected: str) -> None:
    for fields in claims:
        await _claim(**fields)
    assert await _gap() == expected


async def test_cap_keeps_the_newest_own_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap an older covering claim is not seen (documented bound)."""
    monkeypatch.setattr(announce_claim_lookup, "_CLAIM_SCAN_LIMIT", 1)
    await _claim(minutes_ago=10, target=_VC)
    await _claim(minutes_ago=1, target="vc-other")
    assert await _gap() == "other_target"


# ---------------------------------------------------------------------------
# Claims that cover any op
# ---------------------------------------------------------------------------


async def test_claim_without_target_or_class_covers_the_op() -> None:
    await _claim(planned_op_class=None)
    assert await _gap() is None


async def test_op_without_target_is_covered_by_any_active_claim() -> None:
    await _claim(target="vc-other")
    assert await _gap(None) is None


# ---------------------------------------------------------------------------
# The two users of the lookup
# ---------------------------------------------------------------------------


@contextmanager
def _agent_session() -> Iterator[MagicMock]:
    """Bind an agent session and fake the advisory's dedupe ``SET NX``."""
    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    token = agent_session_id_var.set(_SESSION)
    try:
        with patch("meho_backplane.broadcast.reflex.get_broadcast_client", return_value=client):
            yield client
    finally:
        agent_session_id_var.reset(token)


async def _read_the_feed() -> None:
    """Record a ``meho_broadcast_recent`` call, so only announce can fire."""
    await seed_usage_event(
        tenant_id=_TENANT,
        route="tool:meho_broadcast_recent",
        occurred_at=datetime.now(UTC),
        session_id=_SESSION,
    )


async def test_reflex_advisory_is_silent_when_the_archive_holds_the_claim() -> None:
    await _read_the_feed()
    await _claim(minutes_ago=24, targets=[_VC, "nsx-1"], ttl_minutes=600)
    with _agent_session() as client, patch("meho_backplane.broadcast.reflex._log") as log:
        advisory = await build_reflex_advisory(_operator(), op_id=_OP, target_name=_VC)
    assert advisory == {}
    client.set.assert_not_called()
    # Silent because the claim was found, not because the lookup failed open.
    log.warning.assert_not_called()


async def test_reflex_advisory_says_the_claim_expired() -> None:
    await _read_the_feed()
    await _claim(minutes_ago=30, target=_VC, ttl_minutes=10)
    with _agent_session():
        advisory = await build_reflex_advisory(_operator(), op_id=_OP, target_name=_VC)
    assert advisory == {REFLEX_ADVISORY_EXTRAS_KEY: _ANNOUNCE_NUDGES["expired"]}


async def _enable_gate() -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="t-4091", name="Tenant", announce_gate_enabled=True))
        await session.commit()


async def test_announce_gate_passes_with_an_archived_claim() -> None:
    await _enable_gate()
    assert (
        await announce_gate_blocks(_operator(), op_id=_OP, safety_level="caution", target_name=_VC)
        is not None
    )
    await _claim(target=_VC)
    assert (
        await announce_gate_blocks(_operator(), op_id=_OP, safety_level="caution", target_name=_VC)
        is None
    )
