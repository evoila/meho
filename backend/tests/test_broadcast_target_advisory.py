# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-time target-activity advisory (T7 #2550).

A write-class dispatch on a target with recent PEER activity carries a
compact ``extras["target_activity_advisory"]`` on its success response so
the caller learns another principal is active there at the moment it
matters. This is post-op awareness -- not a lock, not a block. Pre-op
checking stays the discipline's ``meho.broadcast.recent`` read step.

Coverage mirrors the acceptance criteria on the issue:

* peer operation AND active announcement claim on the same target both
  surface; a dispatch on a different target carries no advisory;
* advisory entries carry structured fields ONLY -- zero agent prose
  (``activity`` / ``scope`` / ``target`` / ``targets``);
* the caller's own activity is excluded (principal + actor);
* a Valkey teardown fails open (no key, op unaffected, warn-logged),
  the same mold as the ``publish_event`` fail-open path;
* read-class dispatch performs no stream read (call-count assertion);
* the ``0`` window knob disables the feature entirely;
* :func:`wrap_ok_result` plumbs the advisory onto the frozen
  :class:`OperationResult.extras`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.agent_events import AgentAnnouncementEvent
from meho_backplane.broadcast.history import (
    ADVISORY_EXTRAS_KEY,
    build_target_activity_advisory,
)
from meho_backplane.operations._errors import wrap_ok_result
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_TARGET = "cluster-x"
_AUDIT_ID = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL + a 30-min advisory window per test.

    Mirrors ``test_mcp_tool_broadcast_recent``: per-test patches replace
    ``xrange`` so no socket ever opens, and the settings cache is cleared
    so the env pins take effect.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "30")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


def _operator(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _op_event(
    *,
    principal_sub: str,
    op_id: str = "vsphere.vm.create",
    op_class: str = "write",
    target_name: str | None = _TARGET,
    actor_sub: str | None = None,
) -> BroadcastEvent:
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime.now(UTC),
        tenant_id=_TENANT,
        principal_sub=principal_sub,
        target_name=target_name,
        op_id=op_id,
        op_class=op_class,
        result_status="ok",
        audit_id=_AUDIT_ID,
        actor_sub=actor_sub,
        payload={"op_class": op_class, "params": {}, "result_status": "ok"},
    )


def _announcement(
    *,
    principal_sub: str,
    target: str | None = _TARGET,
    phase: str = "start",
) -> AgentAnnouncementEvent:
    return AgentAnnouncementEvent(
        tenant_id=_TENANT,
        principal_sub=principal_sub,
        activity="rotating tokens -- do not touch",
        scope="all nodes",
        target=target,
        phase=phase,
        ttl_minutes=60,
        ts=datetime.now(UTC),
    )


def _entry(
    event: BroadcastEvent | AgentAnnouncementEvent, entry_id: str
) -> tuple[str, dict[str, str]]:
    return entry_id, {"event": event.model_dump_json()}


def _seed_xrange(entries: list[tuple[str, dict[str, str]]]) -> AsyncMock:
    """Patch the broadcast client's ``xrange`` to return *entries*."""
    bc = get_broadcast_client()
    mock = AsyncMock(return_value=entries)
    return patch.object(bc, "xrange", new=mock)


# ---------------------------------------------------------------------------
# Peer activity surfaces; different-target carries nothing
# ---------------------------------------------------------------------------


async def test_peer_operation_and_announcement_surface() -> None:
    """A's op + A's active claim on X both appear on B's write to X."""
    entries = [
        _entry(_announcement(principal_sub="user-a"), "1747800000000-0"),
        _entry(_op_event(principal_sub="user-a"), "1747800001000-0"),
    ]
    with _seed_xrange(entries):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    peers = advisory[ADVISORY_EXTRAS_KEY]
    kinds = {p["kind"] for p in peers}
    assert kinds == {"operation", "announcement"}
    assert all(p["principal_sub"] == "user-a" for p in peers)
    op_entry = next(p for p in peers if p["kind"] == "operation")
    assert op_entry["op_id"] == "vsphere.vm.create"
    ann_entry = next(p for p in peers if p["kind"] == "announcement")
    assert ann_entry["phase"] == "start"


async def test_different_target_carries_no_advisory() -> None:
    """B's write on a target with no peer activity gets no key."""
    entries = [_entry(_op_event(principal_sub="user-a"), "1747800001000-0")]
    with _seed_xrange(entries):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name="cluster-y",
        )
    assert advisory == {}


# ---------------------------------------------------------------------------
# No prose; self-exclusion
# ---------------------------------------------------------------------------


async def test_advisory_carries_no_prose_fields() -> None:
    """Advisory entries expose structured fields only -- no agent prose."""
    entries = [_entry(_announcement(principal_sub="user-a"), "1747800000000-0")]
    with _seed_xrange(entries):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    for entry in advisory[ADVISORY_EXTRAS_KEY]:
        assert not ({"activity", "scope", "target", "targets"} & set(entry))
        assert set(entry) <= {"principal_sub", "actor_sub", "kind", "op_id", "phase", "ts"}


async def test_excludes_caller_own_principal_and_actor() -> None:
    """The caller's own op (same principal + actor) is filtered out."""
    entries = [
        _entry(_op_event(principal_sub="user-b"), "1747800000000-0"),
        _entry(_op_event(principal_sub="user-a"), "1747800001000-0"),
    ]
    with _seed_xrange(entries):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    peers = advisory[ADVISORY_EXTRAS_KEY]
    assert [p["principal_sub"] for p in peers] == ["user-a"]


async def test_peer_agent_under_same_human_is_not_self() -> None:
    """A different delegated agent (same human) is a peer, not self."""
    entries = [
        # caller's own delegated op: principal user-b, actor agent:me
        _entry(
            _op_event(principal_sub="user-b", actor_sub="agent:me"),
            "1747800000000-0",
        ),
        # a sibling agent under the same human: distinct actor -> peer
        _entry(
            _op_event(principal_sub="user-b", actor_sub="agent:other"),
            "1747800001000-0",
        ),
    ]
    with _seed_xrange(entries), actor_delegation("agent:me"):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    peers = advisory[ADVISORY_EXTRAS_KEY]
    assert [p["actor_sub"] for p in peers] == ["agent:other"]


async def test_advisory_capped_at_five_most_recent() -> None:
    """At most five peer entries, newest last."""
    entries = [
        _entry(_op_event(principal_sub=f"user-{i}"), f"17478000{i:02d}000-0") for i in range(8)
    ]
    with _seed_xrange(entries):
        advisory = await build_target_activity_advisory(
            _operator("caller"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    peers = advisory[ADVISORY_EXTRAS_KEY]
    assert len(peers) == 5
    assert [p["principal_sub"] for p in peers] == [f"user-{i}" for i in range(3, 8)]


# ---------------------------------------------------------------------------
# Fail-open, gating, disable knob
# ---------------------------------------------------------------------------


async def test_fail_open_on_valkey_teardown() -> None:
    """A Valkey teardown yields no advisory and never raises."""
    import structlog
    from redis import exceptions as redis_exceptions

    bc = get_broadcast_client()
    with (
        patch.object(
            bc,
            "xrange",
            new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    assert advisory == {}
    assert any(entry["event"] == "broadcast_history_fetch_failed" for entry in logs)


@pytest.mark.parametrize(
    "op_id",
    ["vsphere.vm.list", "vault.kv.read", "audit.query"],
)
async def test_read_class_dispatch_performs_no_lookup(op_id: str) -> None:
    """Read-class ops short-circuit before any stream read."""
    bc = get_broadcast_client()
    xr = AsyncMock(return_value=[])
    with patch.object(bc, "xrange", new=xr):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id=op_id,
            target_name=_TARGET,
        )
    assert advisory == {}
    xr.assert_not_called()


async def test_window_zero_disables_and_skips_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0`` window disables the feature -- no stream read, no key."""
    monkeypatch.setenv("DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    bc = get_broadcast_client()
    xr = AsyncMock(return_value=[])
    with patch.object(bc, "xrange", new=xr):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=_TARGET,
        )
    assert advisory == {}
    xr.assert_not_called()


async def test_no_target_skips_lookup() -> None:
    """A target-less dispatch performs no stream read."""
    bc = get_broadcast_client()
    xr = AsyncMock(return_value=[])
    with patch.object(bc, "xrange", new=xr):
        advisory = await build_target_activity_advisory(
            _operator("user-b"),
            op_id="vsphere.vm.create",
            target_name=None,
        )
    assert advisory == {}
    xr.assert_not_called()


# ---------------------------------------------------------------------------
# wrap_ok_result plumbing onto the frozen envelope
# ---------------------------------------------------------------------------


def test_wrap_ok_result_attaches_extras() -> None:
    """The advisory fragment lands on ``OperationResult.extras``."""
    fragment: dict[str, Any] = {
        ADVISORY_EXTRAS_KEY: [
            {"principal_sub": "user-a", "kind": "operation", "op_id": "x", "ts": "t"}
        ]
    }
    result = wrap_ok_result("op-1", {"ok": True}, 1.5, None, extras=fragment)
    assert result.extras[ADVISORY_EXTRAS_KEY] == fragment[ADVISORY_EXTRAS_KEY]


def test_wrap_ok_result_defaults_to_empty_extras() -> None:
    """Omitting extras leaves the frozen model's empty default intact."""
    result = wrap_ok_result("op-1", {"ok": True}, 1.5, None)
    assert result.extras == {}
