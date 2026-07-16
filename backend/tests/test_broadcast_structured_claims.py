# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured intent claims on ``meho.broadcast.announce`` (T1, #2544).

Covers the keystone contract of Broadcast v2 (Initiative #2543):

* ``AgentAnnouncementEvent`` gains optional typed fields -- ``targets``,
  ``planned_op_class``, ``ttl_minutes``, ``work_ref``, ``run_id`` -- and
  a derived ``expires_at``.
* Trust split: the validated structured fields (``planned_op_class`` /
  ``ttl_minutes`` / ``run_id`` / ``phase`` / ``ts`` / ``expires_at``)
  serialise UNWRAPPED; the free-text fields (``activity`` / ``scope`` /
  ``target`` / ``targets[]`` / ``work_ref``) stay behind the
  untrusted-content envelope. Filtering runs pre-wrap on the model.
* ``meho.broadcast.recent`` gains ``work_ref`` + ``active_only`` filters
  and the ``target`` filter now matches an announcement's ``targets``
  list; invalid claims reject at the boundary with ``-32602``.
* Back-compat: single-``target`` announcements and pre-v2 stream entries
  (no claim fields) still parse and render.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    PLANNED_OP_CLASS_VALUES,
    AgentAnnouncementEvent,
    BroadcastEvent,
    get_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.history import dump_event_wire, event_matches
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.tools.broadcast import _handler_recent
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_AUDIT_ID: UUID = UUID("44444444-4444-4444-4444-444444444444")
_RUN_ID: UUID = UUID("99999999-9999-9999-9999-999999999999")


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL + reset the cached client around each test.

    Also neutralises the per-principal announce rate limit (G6.5-T6
    #2546) so these wire tests -- which mock ``xadd`` and never open a
    socket -- don't trip the limiter's real ``INCR``/``EXPIRE``. The
    ``0`` env knob documents intent, but the load-bearing guard is the
    ``enforce_announce_rate_limit`` patch: the env->``get_settings``
    route alone is fragile under the app fixture, which can repopulate
    the ``lru_cache`` with the default limit after this fixture clears
    it (passed single-process locally, tripped a real socket ->
    ``-32603 ConnectionError`` under xdist ``loadscope`` in CI). Patching
    the function bypasses settings caching entirely.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("BROADCAST_ANNOUNCE_RATE_PER_MINUTE", "0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    with patch(
        "meho_backplane.mcp.tools.broadcast.enforce_announce_rate_limit",
        new=AsyncMock(return_value=None),
    ):
        yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _announcement(
    *,
    activity: str = "rotating tokens",
    target: str | None = None,
    targets: list[str] | None = None,
    scope: str | None = None,
    planned_op_class: str | None = None,
    ttl_minutes: int | None = None,
    work_ref: str | None = None,
    run_id: UUID | None = None,
    ts: datetime | None = None,
) -> AgentAnnouncementEvent:
    """Build an :class:`AgentAnnouncementEvent` with sensible defaults."""
    return AgentAnnouncementEvent(
        tenant_id=OPERATOR_TENANT_ID,
        principal_sub="op-test",
        activity=activity,
        target=target,
        targets=targets or [],
        scope=scope,
        planned_op_class=planned_op_class,  # type: ignore[arg-type]
        ttl_minutes=ttl_minutes,
        work_ref=work_ref,
        run_id=run_id,
        ts=ts or datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
    )


def _broadcast_event(*, target_name: str | None = None) -> BroadcastEvent:
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        tenant_id=OPERATOR_TENANT_ID,
        principal_sub="op-test",
        target_name=target_name,
        op_id="vsphere.vm.list",
        op_class="read",
        result_status="ok",
        audit_id=_AUDIT_ID,
        payload={"op_class": "read"},
    )


def _entry(
    event: AgentAnnouncementEvent | BroadcastEvent,
    entry_id: str,
) -> tuple[str, dict[str, str]]:
    return entry_id, {"event": event.model_dump_json()}


def _result_dict(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


def _tools_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# ---------------------------------------------------------------------------
# Model: new fields + derived expires_at + defense-in-depth validation
# ---------------------------------------------------------------------------


def test_model_accepts_structured_claims_and_derives_expires_at() -> None:
    """The model carries the typed claims; ``expires_at`` = ``ts + ttl``."""
    event = _announcement(
        targets=["cluster-x", "cluster-y"],
        planned_op_class="write",
        ttl_minutes=30,
        work_ref="gh:evoila/meho#123",
        run_id=_RUN_ID,
    )
    assert event.targets == ["cluster-x", "cluster-y"]
    assert event.planned_op_class == "write"
    assert event.ttl_minutes == 30
    assert event.work_ref == "gh:evoila/meho#123"
    assert event.run_id == _RUN_ID
    assert event.expires_at == event.ts + timedelta(minutes=30)


def test_model_expires_at_none_without_ttl() -> None:
    """No ``ttl_minutes`` → the announcement is not a time-bounded claim."""
    assert _announcement().expires_at is None


def test_model_defaults_preserve_pre_v2_shape() -> None:
    """A bare announcement has empty ``targets`` and None claim fields."""
    event = _announcement()
    assert event.targets == []
    assert event.planned_op_class is None
    assert event.ttl_minutes is None
    assert event.work_ref is None
    assert event.run_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"targets": [f"t{i}" for i in range(11)]},  # 11 > MAX_TARGETS
        {"targets": ["x" * 257]},  # element over TARGET_MAX_CHARS
        {"ttl_minutes": 0},  # below TTL_MIN_MINUTES
        {"ttl_minutes": 1441},  # above TTL_MAX_MINUTES
        {"work_ref": "x" * 257},  # over WORK_REF_MAX_CHARS
        {"planned_op_class": "not-a-class"},  # outside the enum
    ],
)
def test_model_rejects_out_of_bounds_claims(kwargs: dict[str, Any]) -> None:
    """pydantic enforces the same bounds the handler does (defense in depth)."""
    with pytest.raises(ValidationError):
        _announcement(**kwargs)


def test_planned_op_class_enum_matches_classify_op_taxonomy() -> None:
    """The declaration enum spans the full classify_op output taxonomy."""
    assert set(PLANNED_OP_CLASS_VALUES) == {
        "read",
        "write",
        "credential_read",
        "credential_write",
        "credential_mint",
        "audit_query",
        "approval",
        "other",
    }


# ---------------------------------------------------------------------------
# dump_event_wire: structured trusted (unwrapped) vs prose (enveloped)
# ---------------------------------------------------------------------------


def test_dump_wraps_prose_and_leaves_structure_unwrapped() -> None:
    """Free text is enveloped; enums/int/UUID/timestamps are served raw."""
    event = _announcement(
        activity="rotating tokens",
        target="cluster-x",
        targets=["cluster-x", "cluster-y"],
        scope="token rotation",
        planned_op_class="credential_write",
        ttl_minutes=30,
        work_ref="gh:evoila/meho#123",
        run_id=_RUN_ID,
    )
    data = dump_event_wire(event)

    # Prose enveloped.
    assert data["activity"] == wrap_untrusted_text("rotating tokens")
    assert data["scope"] == wrap_untrusted_text("token rotation")
    assert data["target"] == wrap_untrusted_text("cluster-x")
    assert data["work_ref"] == wrap_untrusted_text("gh:evoila/meho#123")
    assert data["targets"] == [
        wrap_untrusted_text("cluster-x"),
        wrap_untrusted_text("cluster-y"),
    ]

    # Structure unwrapped -- trustworthy coordination data.
    assert data["planned_op_class"] == "credential_write"
    assert data["ttl_minutes"] == 30
    assert data["run_id"] == str(_RUN_ID)
    assert data["phase"] == "update"
    assert data["expires_at"].startswith("2026-05-25T10:30:00")


def test_dump_leaves_empty_targets_and_none_claims_alone() -> None:
    """A bare announcement dumps an empty list + null structured fields."""
    data = dump_event_wire(_announcement())
    assert data["targets"] == []
    assert data["planned_op_class"] is None
    assert data["ttl_minutes"] is None
    assert data["run_id"] is None
    assert data["expires_at"] is None


# ---------------------------------------------------------------------------
# event_matches: target-vs-targets, work_ref, active_only
# ---------------------------------------------------------------------------


def test_target_filter_matches_targets_list_member() -> None:
    """A ``target`` filter matches when the value is in ``targets``."""
    event = _announcement(targets=["cluster-x", "cluster-y"])
    assert event_matches(event, op_class=None, principal=None, target="cluster-y")
    assert not event_matches(event, op_class=None, principal=None, target="cluster-z")


def test_target_filter_still_matches_single_target() -> None:
    """Back-compat: the single ``target`` field still satisfies the filter."""
    event = _announcement(target="prod-vc-1")
    assert event_matches(event, op_class=None, principal=None, target="prod-vc-1")


def test_work_ref_filter_matches_announcement_only() -> None:
    """``work_ref`` matches an announcement; a BroadcastEvent never does."""
    ann = _announcement(work_ref="gh:evoila/meho#123")
    assert event_matches(
        ann, op_class=None, principal=None, target=None, work_ref="gh:evoila/meho#123"
    )
    assert not event_matches(ann, op_class=None, principal=None, target=None, work_ref="gh:other#9")
    bev = _broadcast_event(target_name="cluster-x")
    assert not event_matches(
        bev, op_class=None, principal=None, target=None, work_ref="gh:evoila/meho#123"
    )


def test_active_only_drops_expired_claim_keeps_others() -> None:
    """``active_only`` drops an elapsed TTL claim; non-TTL events survive."""
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    expired = _announcement(
        ttl_minutes=30,
        ts=now - timedelta(minutes=60),  # expires_at = now - 30m (elapsed)
    )
    active = _announcement(ttl_minutes=30, ts=now - timedelta(minutes=10))
    no_ttl = _announcement()  # not a claim -- always active
    bev = _broadcast_event()  # audit event -- always active

    assert not event_matches(
        expired, op_class=None, principal=None, target=None, active_only=True, now=now
    )
    assert event_matches(
        active, op_class=None, principal=None, target=None, active_only=True, now=now
    )
    assert event_matches(
        no_ttl, op_class=None, principal=None, target=None, active_only=True, now=now
    )
    assert event_matches(bev, op_class=None, principal=None, target=None, active_only=True, now=now)


# ---------------------------------------------------------------------------
# Announce inputSchema exposes the new claim fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_announce_schema_exposes_claim_fields(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The wire schema advertises the structured-claim properties + bounds."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    announce = next(
        t for t in resp.json()["result"]["tools"] if t["name"] == "meho.broadcast.announce"
    )
    props = announce["inputSchema"]["properties"]
    assert props["targets"]["type"] == "array"
    assert props["targets"]["maxItems"] == 10
    assert props["targets"]["items"]["maxLength"] == 256
    assert set(props["planned_op_class"]["enum"]) == set(PLANNED_OP_CLASS_VALUES)
    assert props["ttl_minutes"]["minimum"] == 1
    assert props["ttl_minutes"]["maximum"] == 1440
    assert props["work_ref"]["maxLength"] == 256
    assert props["run_id"]["format"] == "uuid"
    # Structured claims are optional -- only 'activity' stays required.
    assert announce["inputSchema"]["required"] == ["activity"]


# ---------------------------------------------------------------------------
# Announce validation: invalid claims reject with -32602
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator,bad_args",
    [
        (TenantRole.OPERATOR, {"targets": [f"t{i}" for i in range(11)]}),
        (TenantRole.OPERATOR, {"ttl_minutes": 0}),
        (TenantRole.OPERATOR, {"work_ref": "x" * 257}),
        (TenantRole.OPERATOR, {"run_id": "not-a-uuid"}),
        (TenantRole.OPERATOR, {"planned_op_class": "nope"}),
        (TenantRole.OPERATOR, {"targets": "cluster-x"}),  # not a list
    ],
    indirect=["client_with_operator"],
)
def test_announce_invalid_claim_rejects_with_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    bad_args: dict[str, Any],
) -> None:
    """Each malformed claim surfaces as JSON-RPC -32602 at the boundary."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call("meho.broadcast.announce", {"activity": "x", **bad_args}),
    )
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Announce echo + published wire shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_announce_echoes_declared_claims_and_publishes_them(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The ack echoes declared claims; the XADD'd JSON carries them typed."""
    client, op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")) as xa:
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.announce",
                {
                    "activity": "rotating tokens on cluster X",
                    "targets": ["cluster-x"],
                    "planned_op_class": "write",
                    "ttl_minutes": 30,
                    "work_ref": "gh:evoila/meho#123",
                    "run_id": str(_RUN_ID),
                    "phase": "start",
                },
            ),
        )
    result = _result_dict(resp)
    assert result["cursor"] == "1747800000000-0"
    assert result["event_id"] == "1747800000000-0"
    assert result["targets"] == ["cluster-x"]
    assert result["planned_op_class"] == "write"
    assert result["ttl_minutes"] == 30
    assert result["work_ref"] == "gh:evoila/meho#123"
    assert result["run_id"] == str(_RUN_ID)

    announce_payloads = [
        json.loads(call.args[1]["event"])
        for call in xa.await_args_list
        if "event" in call.args[1]
        and json.loads(call.args[1]["event"]).get("event_kind") == "agent_announcement"
    ]
    assert len(announce_payloads) == 1
    decoded = announce_payloads[0]
    assert decoded["targets"] == ["cluster-x"]
    assert decoded["planned_op_class"] == "write"
    assert decoded["ttl_minutes"] == 30
    assert decoded["work_ref"] == "gh:evoila/meho#123"
    assert decoded["run_id"] == str(_RUN_ID)
    assert decoded["tenant_id"] == str(op.tenant_id)


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_bare_announce_keeps_pre_v2_ack_shape(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """No claims → the ack stays the pre-v2 ``{event_id, cursor}`` shape."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-7")):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )
    assert _result_dict(resp) == {"event_id": "1747800000000-7", "cursor": "1747800000000-7"}


# ---------------------------------------------------------------------------
# recent round-trip: target / work_ref / active_only each surface the claim
# ---------------------------------------------------------------------------


async def test_recent_target_and_work_ref_filters_surface_the_claim() -> None:
    """A claim on ``cluster-x`` is found by target and by work_ref filters."""
    op = build_operator(TenantRole.OPERATOR)
    claim = _announcement(
        activity="rotating tokens",
        targets=["cluster-x"],
        planned_op_class="write",
        ttl_minutes=30,
        work_ref="gh:evoila/meho#123",
        ts=datetime.now(UTC),
    )
    other = _announcement(activity="unrelated", targets=["cluster-z"])
    entries = [_entry(claim, "1-0"), _entry(other, "2-0")]
    bc = get_broadcast_client()

    with patch.object(bc, "xrange", new=AsyncMock(return_value=entries)):
        by_target = await _handler_recent(op, {"filter": {"target": "cluster-x"}})
        by_work_ref = await _handler_recent(op, {"filter": {"work_ref": "gh:evoila/meho#123"}})

    assert [e["targets"] for e in by_target["events"]] == [[wrap_untrusted_text("cluster-x")]]
    assert by_target["events"][0]["planned_op_class"] == "write"
    assert len(by_work_ref["events"]) == 1
    assert by_work_ref["events"][0]["work_ref"] == wrap_untrusted_text("gh:evoila/meho#123")


async def test_recent_active_only_excludes_expired_claim() -> None:
    """Before TTL elapses ``active_only`` surfaces the claim; after, it drops."""
    op = build_operator(TenantRole.OPERATOR)
    fresh = _announcement(targets=["cluster-x"], ttl_minutes=30, ts=datetime.now(UTC))
    stale = _announcement(
        targets=["cluster-x"],
        ttl_minutes=30,
        ts=datetime.now(UTC) - timedelta(minutes=90),
    )
    bc = get_broadcast_client()

    with patch.object(bc, "xrange", new=AsyncMock(return_value=[_entry(fresh, "1-0")])):
        active = await _handler_recent(op, {"filter": {"active_only": True}})
    assert len(active["events"]) == 1

    with patch.object(bc, "xrange", new=AsyncMock(return_value=[_entry(stale, "1-0")])):
        expired = await _handler_recent(op, {"filter": {"active_only": True}})
    assert expired["events"] == []


# ---------------------------------------------------------------------------
# Back-compat: pre-v2 stream entries (no claim fields) parse + render
# ---------------------------------------------------------------------------


async def test_pre_v2_entry_without_claim_fields_still_parses() -> None:
    """An entry written before the claim fields existed round-trips cleanly.

    The wire JSON omits ``targets`` / ``planned_op_class`` / ``ttl_minutes``
    / ``work_ref`` / ``run_id`` entirely (the v0.8.0 announcement shape);
    the model fills defaults and the reader serves it beside a v2 claim on
    the same mixed stream.
    """
    op = build_operator(TenantRole.OPERATOR)
    legacy_json = json.dumps(
        {
            "kind": "agent_announcement",
            "event_kind": "agent_announcement",
            "tenant_id": str(OPERATOR_TENANT_ID),
            "principal_sub": "op-test",
            "activity": "legacy announce",
            "target": "prod-vc-1",
            "scope": None,
            "phase": "start",
            "ts": "2026-05-25T09:00:00Z",
        }
    )
    v2 = _announcement(activity="v2 announce", targets=["cluster-x"], ttl_minutes=30)
    entries = [("1-0", {"event": legacy_json}), _entry(v2, "2-0")]
    bc = get_broadcast_client()

    with patch.object(bc, "xrange", new=AsyncMock(return_value=entries)):
        result = await _handler_recent(op, {})

    activities = [e["activity"] for e in result["events"]]
    assert activities == [
        wrap_untrusted_text("legacy announce"),
        wrap_untrusted_text("v2 announce"),
    ]
    # Legacy entry renders with the claim defaults (empty list, nulls).
    legacy = result["events"][0]
    assert legacy["targets"] == []
    assert legacy["planned_op_class"] is None
    assert legacy["expires_at"] is None
