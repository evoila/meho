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
* every refusal that writes an audit row answers with that row's id as
  ``audit_id``, and with ``audit_id: null`` only when the row did not commit
  (#4669)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meho_backplane.operations.dispatcher as dispatcher
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_RECEIPT = uuid.UUID("00000000-0000-0000-0000-00000000a669")


def _operator() -> Operator:
    return Operator(
        sub="limit-principal",
        raw_jwt="x",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings the params hash reads its server key from."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_dispatch_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Stub the dispatcher seams reached before + at the #3500 limit block."""
    # Match the descriptor identity the dispatcher receives from storage.
    # This unadvertised typed row must pass directly to the limit block;
    # catalog-owner guards are opt-in and ingested-only.
    descriptor = SimpleNamespace(
        product="svc",
        version="1.0",
        impl_id="svc",
        source_kind="typed",
        parameter_schema={},
        op_id="svc.read",
        safety_level="caution",
    )
    monkeypatch.setattr(dispatcher, "lookup_descriptor", AsyncMock(return_value=descriptor))
    monkeypatch.setattr(dispatcher, "validate_params", lambda schema, params: [])
    monkeypatch.setattr(
        dispatcher,
        "get_settings",
        lambda: SimpleNamespace(dispatch_concurrency_slot_ttl_seconds=3600),
    )
    # Both writers answer with the id of the row they committed (#4669).
    audit = AsyncMock(return_value=_RECEIPT)
    monkeypatch.setattr(dispatcher, "audit_safe", audit)
    reject = AsyncMock(return_value=_RECEIPT)
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
    assert result.audit_id == _RECEIPT
    # Audited as a rejection row with result_status='rate_limited' ...
    stub_dispatch_seams["reject"].assert_awaited_once()
    assert stub_dispatch_seams["reject"].await_args.kwargs["result_status"] == "rate_limited"
    stub_dispatch_seams["audit"].assert_not_awaited()  # only the rejection row is written
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
    assert result.audit_id == _RECEIPT
    stub_dispatch_seams["reject"].assert_awaited_once()
    assert stub_dispatch_seams["reject"].await_args.kwargs["result_status"] == "rate_limited"
    stub_dispatch_seams["audit"].assert_not_awaited()  # only the rejection row is written
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
    assert result.audit_id == _RECEIPT
    stub_dispatch_seams["gate"].assert_awaited_once()
    stub_dispatch_seams["release"].assert_awaited_once_with("slot-key-1")


def _no_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "resolve_dispatch_rate_limit", lambda s, t: 0)
    monkeypatch.setattr(dispatcher, "check_dispatch_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(dispatcher, "resolve_dispatch_concurrency_cap", lambda s, t: 0)
    monkeypatch.setattr(dispatcher, "acquire_dispatch_slot", AsyncMock(return_value=(None, None)))


@pytest.mark.asyncio
async def test_unexpected_verdict_denial_returns_its_audit_id(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed deny for a verdict the gate should never return (#4669)."""
    _no_limits(monkeypatch)
    stub_dispatch_seams["gate"].return_value = (SimpleNamespace(value="weird"), None)

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == "denied"
    assert "unexpected policy verdict" in (result.error or "")
    assert result.audit_id == _RECEIPT
    assert stub_dispatch_seams["audit"].await_args.kwargs["result_status"] == "denied"


@pytest.mark.asyncio
async def test_announce_gate_refusal_returns_its_audit_id(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call refused by the announce gate carries the id of its ``denied`` row (#4669)."""
    _no_limits(monkeypatch)
    stub_dispatch_seams["gate"].return_value = (PermissionVerdict.AUTO_EXECUTE, None)
    monkeypatch.setattr(
        dispatcher, "announce_gate_blocks", AsyncMock(return_value="announce first")
    )

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "announce_required"
    assert result.audit_id == _RECEIPT
    assert stub_dispatch_seams["audit"].await_args.kwargs["result_status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["audit", "reject"])
async def test_refusal_keeps_audit_id_null_when_the_row_did_not_commit(
    stub_dispatch_seams: dict[str, AsyncMock], monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """A failed audit write leaves ``audit_id: null``; the answer is otherwise unchanged (#4669)."""
    stub_dispatch_seams[writer].return_value = None
    if writer == "reject":
        monkeypatch.setattr(dispatcher, "resolve_dispatch_rate_limit", lambda s, t: 5)
        monkeypatch.setattr(dispatcher, "check_dispatch_rate_limit", AsyncMock(return_value=30))
        expected_status, expected_code = "rate_limited", "rate_limited"
    else:
        _no_limits(monkeypatch)
        stub_dispatch_seams["gate"].return_value = (PermissionVerdict.DENY, "policy denied")
        expected_status, expected_code = "denied", "denied"

    result = await dispatch(
        operator=_operator(),
        connector_id="svc-1.0",
        op_id="svc.read",
        target=None,
        params={},
    )

    assert result.status == expected_status
    assert result.extras["error_code"] == expected_code
    assert result.audit_id is None
    stub_dispatch_seams[writer].assert_awaited_once()
