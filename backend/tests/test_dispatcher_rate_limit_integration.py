# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatcher wiring for the #3500 dispatch limits.

Proves the ``dispatch()`` integration contract without a live connector or
vendor round-trip, by stubbing the dispatcher's seams (descriptor lookup,
param validation, the limit primitives, the audit writer, the policy gate):

* an over-rate-limit dispatch returns a ``rate_limited`` envelope, writes an
  audit row with ``result_status='rate_limited'``, and never reaches the
  policy gate / execution (no vendor traffic) — acceptance criterion "over-
  limit events are audited"
* an over-concurrency-cap dispatch behaves the same with ``kind='concurrency'``
* a dispatch that passes both limits acquires a slot and releases it in the
  ``finally`` even when a later step (here: a policy denial) returns early
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meho_backplane.operations.dispatcher as dispatcher
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.operations import dispatch

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


def _operator() -> Operator:
    return Operator(
        sub="limit-principal",
        raw_jwt="x",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
def stub_dispatch_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Stub the dispatcher seams reached before + at the #3500 limit block."""
    descriptor = SimpleNamespace(source_kind="typed", parameter_schema={}, op_id="svc.read")
    monkeypatch.setattr(dispatcher, "lookup_descriptor", AsyncMock(return_value=descriptor))
    monkeypatch.setattr(dispatcher, "validate_params", lambda schema, params: [])
    monkeypatch.setattr(
        dispatcher,
        "get_settings",
        lambda: SimpleNamespace(dispatch_concurrency_slot_ttl_seconds=3600),
    )
    audit = AsyncMock()
    monkeypatch.setattr(dispatcher, "audit_and_broadcast_safe", audit)
    reject = AsyncMock()
    monkeypatch.setattr(dispatcher, "audit_rejection_safe", reject)
    gate = AsyncMock()
    monkeypatch.setattr(dispatcher, "policy_gate", gate)
    release = AsyncMock()
    monkeypatch.setattr(dispatcher, "release_dispatch_slot", release)
    return {"audit": audit, "reject": reject, "gate": gate, "release": release}


@pytest.mark.asyncio
async def test_rate_limit_rejection_is_audited_and_skips_execution(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "resolve_dispatch_rate_limit", lambda s, t: 5)
    monkeypatch.setattr(dispatcher, "check_dispatch_rate_limit", AsyncMock(return_value=30))

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == "rate_limited"
    assert result.extras["kind"] == "rate"
    assert result.extras["retry_after_seconds"] == 30
    # Audited (row only, no broadcast) with result_status='rate_limited' ...
    stub_dispatch_seams["reject"].assert_awaited_once()
    assert stub_dispatch_seams["reject"].await_args.kwargs["result_status"] == "rate_limited"
    stub_dispatch_seams["audit"].assert_not_awaited()  # no broadcast amplification
    # ... and the policy gate / execution were never reached (no vendor traffic).
    stub_dispatch_seams["gate"].assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrency_rejection_is_audited_and_skips_execution(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "resolve_dispatch_rate_limit", lambda s, t: 0)
    monkeypatch.setattr(dispatcher, "check_dispatch_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatcher, "resolve_dispatch_concurrency_cap", lambda s, t: 2)
    monkeypatch.setattr(dispatcher, "acquire_dispatch_slot", AsyncMock(return_value=(1, None)))

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == "rate_limited"
    assert result.extras["kind"] == "concurrency"
    stub_dispatch_seams["reject"].assert_awaited_once()
    assert stub_dispatch_seams["reject"].await_args.kwargs["result_status"] == "rate_limited"
    stub_dispatch_seams["audit"].assert_not_awaited()  # no broadcast amplification
    stub_dispatch_seams["gate"].assert_not_awaited()


@pytest.mark.asyncio
async def test_acquired_slot_released_even_on_early_return(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both limits pass; a slot is acquired, then the policy gate denies and
    # dispatch returns early from inside the try — the finally must release.
    monkeypatch.setattr(dispatcher, "resolve_dispatch_rate_limit", lambda s, t: 0)
    monkeypatch.setattr(dispatcher, "check_dispatch_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatcher, "resolve_dispatch_concurrency_cap", lambda s, t: 2)
    monkeypatch.setattr(
        dispatcher, "acquire_dispatch_slot", AsyncMock(return_value=(None, "slot-key-1"))
    )
    stub_dispatch_seams["gate"].return_value = (PermissionVerdict.DENY, "policy denied")

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == "denied"
    stub_dispatch_seams["gate"].assert_awaited_once()
    stub_dispatch_seams["release"].assert_awaited_once_with("slot-key-1")
