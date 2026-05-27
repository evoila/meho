# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.broadcast.history` (G6.4-T4 #1103).

Acceptance-criteria coverage:

* :func:`list_recent_events_fail_soft` catches
  :class:`redis.exceptions.RedisError` and returns
  ``{"events": [], "next_cursor": None}`` (the empty result -- the UI
  history fragment renders its empty state on this shape).
* :func:`list_recent_events_strict` propagates the same
  :class:`RedisError` to its caller (the MCP ``broadcast.recent``
  dispatcher maps it to ``-32603`` Internal Error upstream).
* :class:`InvalidSinceError` (a programmer-error on the caller's part,
  not a Valkey teardown) is NOT swallowed by the fail-soft wrapper --
  same propagation contract both wrappers share.
* Happy-path shape: both wrappers return the same dict shape
  ``{"events": [...], "next_cursor": ...}`` from a successful XRANGE.
* Tenant scoping is structural -- the stream key is always
  ``meho:feed:{operator.tenant_id}``, asserted via the mocked
  ``xrange``'s call args.

The MCP wire-level glue (``-32603`` / ``-32602`` JSON-RPC error codes)
is exercised through :mod:`tests.test_mcp_tool_broadcast_recent`. This
module pins the helper-level contract that wire layer depends on.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    InvalidSinceError,
    get_broadcast_client,
    list_recent_events_fail_soft,
    list_recent_events_strict,
    reset_broadcast_client_for_testing,
)
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_TENANT = UUID("aaaa0000-0000-0000-0000-000000000099")
_AUDIT_ID = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub URL + clear the cached client around every test.

    The construction path needs ``BROADCAST_REDIS_URL`` set; per-test
    patches replace ``xrange`` so no socket ever opens. Other env vars
    the :class:`Settings` constructor requires (Keycloak / Vault /
    backplane URL) come from the autouse ``required_settings_env``
    fixture imported from :mod:`tests.mcp_test_fixtures`.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-test",
        raw_jwt="x",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _make_event(*, op_id: str = "vsphere.vm.list") -> BroadcastEvent:
    """Build a :class:`BroadcastEvent` for round-tripping through XRANGE."""
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        tenant_id=_TENANT,
        principal_sub="op-test",
        target_name=None,
        op_id=op_id,
        op_class="read",
        result_status="ok",
        audit_id=_AUDIT_ID,
        payload={"op_class": "read", "params": {}, "result_status": "ok"},
    )


def _xrange_entry(event: BroadcastEvent, entry_id: str) -> tuple[str, dict[str, str]]:
    return entry_id, {"event": event.model_dump_json()}


# ---------------------------------------------------------------------------
# Happy-path shape (both wrappers agree)
# ---------------------------------------------------------------------------


async def test_fail_soft_returns_dict_shape_on_success() -> None:
    """A successful read returns ``{"events": [...], "next_cursor": ...}``.

    The fail-soft variant degrades to the empty shape on Valkey errors,
    but the empty shape MUST match the success-shape so the UI's
    template doesn't have to special-case the failure path. The dict
    keys are the contract; downstream code reads ``result["events"]``
    and ``result["next_cursor"]`` without checking the variant.
    """
    event = _make_event(op_id="vsphere.vm.list")
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(return_value=[_xrange_entry(event, "1747800000000-0")]),
    ):
        result = await list_recent_events_fail_soft(_operator())
    assert set(result.keys()) == {"events", "next_cursor"}
    assert len(result["events"]) == 1
    assert result["events"][0]["op_id"] == "vsphere.vm.list"
    assert result["events"][0]["id"] == "1747800000000-0"


async def test_strict_returns_dict_shape_on_success() -> None:
    """The strict variant returns the same dict shape as fail-soft."""
    event = _make_event(op_id="vsphere.vm.create")
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(return_value=[_xrange_entry(event, "1747800000001-0")]),
    ):
        result = await list_recent_events_strict(_operator())
    assert set(result.keys()) == {"events", "next_cursor"}
    assert result["events"][0]["op_id"] == "vsphere.vm.create"


# ---------------------------------------------------------------------------
# Fail-soft contract: RedisError -> empty result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exception_cls",
    [RedisError, RedisConnectionError, RedisTimeoutError],
    ids=["RedisError", "ConnectionError", "TimeoutError"],
)
async def test_fail_soft_swallows_redis_error_returns_empty(
    exception_cls: type[RedisError],
) -> None:
    """A Valkey teardown surfaces as ``{"events": [], "next_cursor": None}``.

    Acceptance criterion: the fail-soft caller catches
    :class:`redis.exceptions.RedisError` and returns the empty
    sentinel. The dashboard's history fragment renders its empty state
    on this shape rather than 500-ing.

    Parametrised across the three concrete teardown classes the
    redis-py 7.4 hierarchy exposes (the base ``RedisError`` and the
    two specific subclasses the UI actually expects on a broadcast-
    subchart blip). All three must surface as the empty result; a
    catch-clause that only handled ``ConnectionError`` would still
    500 on a ``TimeoutError``, defeating the contract.
    """
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(side_effect=exception_cls("simulated valkey teardown")),
    ):
        result = await list_recent_events_fail_soft(_operator())
    assert result == {"events": [], "next_cursor": None}


# ---------------------------------------------------------------------------
# Fail-loud contract: RedisError re-raised
# ---------------------------------------------------------------------------


async def test_strict_re_raises_redis_error() -> None:
    """A Valkey teardown propagates to the caller of the strict wrapper.

    Acceptance criterion: the fail-loud caller re-raises the same
    :class:`RedisError`. The MCP dispatcher's generic exception
    handler then maps it to JSON-RPC ``-32603`` Internal Error so the
    agent sees the failure rather than a silent empty result.
    """
    bc = get_broadcast_client()
    with (
        patch.object(
            bc,
            "xrange",
            new=AsyncMock(side_effect=RedisConnectionError("simulated teardown")),
        ),
        pytest.raises(RedisConnectionError, match="simulated teardown"),
    ):
        await list_recent_events_strict(_operator())


# ---------------------------------------------------------------------------
# Programmer errors propagate (both wrappers)
# ---------------------------------------------------------------------------


async def test_fail_soft_does_not_swallow_invalid_since() -> None:
    """A malformed ``since`` is a programmer error, NOT a Valkey teardown.

    Fail-soft swallows the "data store unavailable" path, but a caller
    passing garbage to the helper is a bug the caller should surface.
    :class:`InvalidSinceError` must propagate through the fail-soft
    wrapper unchanged so a typo in the call site doesn't degrade
    silently to an empty pane.
    """
    with pytest.raises(InvalidSinceError, match="not a valid ISO-8601"):
        await list_recent_events_fail_soft(_operator(), since="garbled-input")


async def test_strict_propagates_invalid_since() -> None:
    """The strict variant also propagates :class:`InvalidSinceError`."""
    with pytest.raises(InvalidSinceError, match="not a valid ISO-8601"):
        await list_recent_events_strict(_operator(), since="garbled-input")


# ---------------------------------------------------------------------------
# Tenant scoping is structural (verified at the helper layer)
# ---------------------------------------------------------------------------


async def test_stream_key_derived_from_operator_tenant_id_strict() -> None:
    """``list_recent_events_strict`` reads ``meho:feed:{operator.tenant_id}``.

    The helper API has no ``tenant_id`` argument; the stream key comes
    exclusively from :attr:`Operator.tenant_id`. Asserts the structural
    tenant boundary holds at the helper layer (the MCP wire layer adds
    a separate structural check on the input schema).
    """
    op = _operator()
    bc = get_broadcast_client()
    mock = AsyncMock(return_value=[])
    with patch.object(bc, "xrange", new=mock):
        await list_recent_events_strict(op)
    assert mock.await_args.args[0] == f"meho:feed:{_TENANT}"


async def test_stream_key_derived_from_operator_tenant_id_fail_soft() -> None:
    """``list_recent_events_fail_soft`` reads the same structural key."""
    op = _operator()
    bc = get_broadcast_client()
    mock = AsyncMock(return_value=[])
    with patch.object(bc, "xrange", new=mock):
        await list_recent_events_fail_soft(op)
    assert mock.await_args.args[0] == f"meho:feed:{_TENANT}"
