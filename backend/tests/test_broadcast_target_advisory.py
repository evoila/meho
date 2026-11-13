# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time target-activity advisory (#2550, #3762).

A write-class dispatch on a target where another agent holds an active
claim carries a compact ``extras["target_activity_advisory"]`` on its
success response. This is awareness after the op -- not a lock, not a
block.

Since #3762 the advisory reads only intentional claims from trusted agents,
from the announcement archive:

* an active peer claim on the target shows up; a claim on another target
  does not;
* expired claims, completed claims, steps without a TTL, legacy
  announcements without a trusted claim and claims older than the window
  never show up;
* automatic operation activity is never read (no stream read at all);
* entries carry server-stamped fields only, never agent prose;
* the caller's own claims are left out (principal + actor);
* a database error fails open (no key, op unaffected, warn-logged);
* read-class dispatches, target-less dispatches and the ``0`` window do
  no read at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import client as broadcast_client
from meho_backplane.broadcast import target_advisory
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.archive_sequence import next_announcement_seq
from meho_backplane.broadcast.publisher import _announcement_row
from meho_backplane.broadcast.target_advisory import (
    ADVISORY_EXTRAS_KEY,
    build_target_activity_advisory,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._errors import wrap_ok_result
from meho_backplane.settings import get_settings

from .broadcast_admission_helpers import seed_announcement

_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_TARGET = "cluster-x"
_ME = "user-b"
_PEER = "user-a"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_stream_read() -> Iterator[None]:
    """The advisory reads only the archive, never the Valkey stream."""
    stream_read = AssertionError("the target advisory read the stream")
    stand_in = MagicMock(
        **{f"{command}.side_effect": stream_read for command in ("xrange", "xrevrange", "xread")}
    )
    with patch.object(broadcast_client, "_CLIENT", new=stand_in):
        yield


def _operator(sub: str = _ME) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _claim(
    *, principal_sub: str = _PEER, minutes_ago: float = 1, **fields: Any
) -> AgentAnnouncementEvent:
    """Store one trusted claim step of *principal_sub* (an active start by default)."""
    values: dict[str, Any] = {
        "principal_sub": principal_sub,
        "activity": "rotating tokens -- do not touch",
        "scope": "all nodes",
        "target": _TARGET,
        "ttl_minutes": 60,
        "ts": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }
    values.update(fields)
    event, _ = await seed_announcement(_TENANT, **values)
    return event


async def _advisory(
    *, sub: str = _ME, op_class: str = "write", target_name: str | None = _TARGET
) -> dict[str, Any]:
    return await build_target_activity_advisory(
        _operator(sub), op_class=op_class, target_name=target_name
    )


# ---------------------------------------------------------------------------
# Which claims show up
# ---------------------------------------------------------------------------


async def test_active_peer_claim_surfaces() -> None:
    claim = await _claim()
    advisory = await _advisory()
    assert advisory == {
        ADVISORY_EXTRAS_KEY: [
            {
                "principal_sub": _PEER,
                "owner_sub": f"agent:{_PEER}",
                "kind": "announcement",
                "phase": "start",
                "ts": claim.ts.isoformat(),
                "expires_at": (claim.ts + timedelta(minutes=60)).isoformat(),
            }
        ]
    }


async def test_claim_naming_the_target_in_targets_surfaces() -> None:
    await _claim(target=None, targets=["other", _TARGET])
    assert len((await _advisory())[ADVISORY_EXTRAS_KEY]) == 1


async def test_claim_on_another_target_carries_no_advisory() -> None:
    await _claim(target="cluster-y")
    assert await _advisory() == {}


async def test_target_less_claim_is_not_about_this_target() -> None:
    await _claim(target=None)
    assert await _advisory() == {}


async def test_expired_claim_is_ignored() -> None:
    await _claim(minutes_ago=20, ttl_minutes=10)
    assert await _advisory() == {}


async def test_step_without_ttl_is_ignored() -> None:
    await _claim(ttl_minutes=None)
    assert await _advisory() == {}


async def test_completed_claim_is_ignored() -> None:
    claim_id = uuid.uuid4()
    await _claim(minutes_ago=5, phase="start", claim_id=claim_id)
    await _claim(minutes_ago=1, phase="completion", claim_id=claim_id)
    assert await _advisory() == {}


async def test_update_shows_the_newest_step_once() -> None:
    claim_id = uuid.uuid4()
    await _claim(minutes_ago=5, phase="start", claim_id=claim_id)
    await _claim(minutes_ago=1, phase="update", claim_id=claim_id)
    entries = (await _advisory())[ADVISORY_EXTRAS_KEY]
    assert [entry["phase"] for entry in entries] == ["update"]


async def test_update_that_moves_the_claim_away_is_ignored() -> None:
    claim_id = uuid.uuid4()
    await _claim(minutes_ago=5, phase="start", claim_id=claim_id)
    await _claim(minutes_ago=1, phase="update", claim_id=claim_id, target="cluster-y")
    assert await _advisory() == {}


async def test_claim_older_than_the_window_is_ignored() -> None:
    await _claim(minutes_ago=45, ttl_minutes=120)
    assert await _advisory() == {}


async def test_legacy_announcement_without_trusted_claim_is_ignored() -> None:
    """A row from before trusted admission is history, never a claim."""
    legacy = AgentAnnouncementEvent(
        tenant_id=_TENANT,
        principal_sub=_PEER,
        activity="legacy note",
        target=_TARGET,
        ttl_minutes=60,
        phase="start",
        ts=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with get_sessionmaker()() as session:
        seq = await next_announcement_seq(session, _TENANT)
        session.add(_announcement_row(legacy, None, seq))
        await session.commit()
    assert await _advisory() == {}


async def test_claim_of_another_tenant_is_ignored() -> None:
    await seed_announcement(uuid.uuid4(), principal_sub=_PEER, target=_TARGET, ttl_minutes=60)
    assert await _advisory() == {}


# ---------------------------------------------------------------------------
# Entry shape and self-exclusion
# ---------------------------------------------------------------------------


async def test_advisory_carries_no_prose_fields() -> None:
    await _claim(work_ref="gh:evoila/meho#1")
    (entry,) = (await _advisory())[ADVISORY_EXTRAS_KEY]
    for prose_key in ("activity", "scope", "target", "targets", "work_ref"):
        assert prose_key not in entry
    assert "rotating tokens" not in repr(entry)


async def test_excludes_caller_own_claim() -> None:
    await _claim(principal_sub=_ME)
    assert await _advisory() == {}


async def test_peer_agent_under_same_person_is_not_self() -> None:
    """Same principal, another acting agent: a peer, so it shows up."""
    await _claim(principal_sub=_ME, actor_sub="agent-x")
    with actor_delegation("agent-y"):
        advisory = await _advisory()
    assert len(advisory[ADVISORY_EXTRAS_KEY]) == 1


async def test_same_principal_and_actor_is_self() -> None:
    await _claim(principal_sub=_ME, actor_sub="agent-x")
    with actor_delegation("agent-x"):
        assert await _advisory() == {}


async def test_capped_at_five_newest_in_time_order() -> None:
    for minute in range(7, 0, -1):
        await _claim(principal_sub=f"peer-{minute}", minutes_ago=minute)
    entries = (await _advisory())[ADVISORY_EXTRAS_KEY]
    assert [entry["principal_sub"] for entry in entries] == [
        "peer-5",
        "peer-4",
        "peer-3",
        "peer-2",
        "peer-1",
    ]


# ---------------------------------------------------------------------------
# Cheap and fail-open
# ---------------------------------------------------------------------------


async def test_fail_open_on_database_error() -> None:
    def _broken() -> Any:
        raise RuntimeError("database gone")

    with (
        patch.object(target_advisory, "get_sessionmaker", new=_broken),
        patch.object(target_advisory, "_log") as mock_log,
    ):
        advisory = await _advisory()
    assert advisory == {}
    assert any(
        call.args and call.args[0] == "target_activity_advisory_failed"
        for call in mock_log.warning.call_args_list
    )


@pytest.mark.parametrize("op_class", ["read", "credential_read", "audit_query", "other"])
async def test_read_class_dispatch_performs_no_lookup(op_class: str) -> None:
    with patch.object(target_advisory, "get_sessionmaker") as sessionmaker:
        assert await _advisory(op_class=op_class) == {}
    sessionmaker.assert_not_called()


async def test_window_zero_disables_and_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    with patch.object(target_advisory, "get_sessionmaker") as sessionmaker:
        assert await _advisory() == {}
    sessionmaker.assert_not_called()


async def test_no_target_skips_lookup() -> None:
    with patch.object(target_advisory, "get_sessionmaker") as sessionmaker:
        assert await _advisory(target_name=None) == {}
    sessionmaker.assert_not_called()


# ---------------------------------------------------------------------------
# wrap_ok_result plumbing onto the frozen envelope
# ---------------------------------------------------------------------------


def test_wrap_ok_result_attaches_extras() -> None:
    fragment: dict[str, Any] = {
        ADVISORY_EXTRAS_KEY: [
            {"principal_sub": _PEER, "kind": "announcement", "phase": "start", "ts": "t"}
        ]
    }
    result = wrap_ok_result("op-1", {"ok": True}, 1.5, None, extras=fragment)
    assert result.extras[ADVISORY_EXTRAS_KEY] == fragment[ADVISORY_EXTRAS_KEY]


def test_wrap_ok_result_defaults_to_empty_extras() -> None:
    result = wrap_ok_result("op-1", {"ok": True}, 1.5, None)
    assert result.extras == {}
