# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opt-in dispatch-time announce gate (#3133, Initiative #3128).

The enforcing companion to the reflex advisory: off by default and
opt-in per tenant, it rejects a caution-or-higher write-class op before
execution when the caller holds no active announce claim covering it, with
a structured ``announce_required`` denial naming ``meho_broadcast_announce``.
Fail-open throughout -- a disabled or unreadable policy, or an unreachable
claim scan, never blocks a dispatch.

Coverage mirrors the acceptance criteria on the issue, by name:

* enablement is a structured per-tenant flag, default OFF, read through a
  cache-aware fail-open resolver;
* the gate blocks only for enabled-tenant + write-class + ``safety_level
  >= caution`` + no covering claim; a covering claim, a read-class op, a
  ``safe`` op, and a disabled tenant each pass;
* the gate composes on the shared dispatch path -- an enabled-tenant
  dispatch is rejected identically with and without an agent session
  (CLI/MCP parity), and a disabled-tenant dispatch executes;
* the gate is not a NEEDS_APPROVAL path -- the rejection is a plain
  ``denied`` envelope.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import update

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.announce_gate import (
    announce_gate_blocks,
    announce_gate_enabled,
    reset_announce_gate_cache_for_testing,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-000000003133")
_SESSION = UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    # Silence the reflex advisory (sibling feature) so it never touches the
    # stubbed Valkey in these gate-focused dispatch tests.
    monkeypatch.setenv("REFLEX_ADVISORY_WINDOW_MINUTES", "0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    yield
    reset_broadcast_client_for_testing()
    reset_announce_gate_cache_for_testing()
    get_settings.cache_clear()


def _operator(sub: str = "user-b", *, tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@contextmanager
def _bound_session(session_id: UUID | None) -> Iterator[None]:
    token = agent_session_id_var.set(session_id)
    try:
        yield
    finally:
        agent_session_id_var.reset(token)


async def _seed_tenant(*, enabled: bool = False, tenant_id: UUID = _TENANT) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, tenant_id) is None:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"t-{tenant_id.hex[:8]}",
                    name="Tenant",
                    announce_gate_enabled=enabled,
                )
            )
        else:
            await session.execute(
                update(Tenant).where(Tenant.id == tenant_id).values(announce_gate_enabled=enabled)
            )
        await session.commit()
    reset_announce_gate_cache_for_testing()


# ---------------------------------------------------------------------------
# Enablement resolver
# ---------------------------------------------------------------------------


async def test_enablement_defaults_off_for_unset_tenant() -> None:
    """A tenant with the default flag resolves to OFF."""
    await _seed_tenant(enabled=False)
    assert await announce_gate_enabled(_TENANT) is False


async def test_enablement_reads_true_when_opted_in() -> None:
    """An opted-in tenant resolves to ON."""
    await _seed_tenant(enabled=True)
    assert await announce_gate_enabled(_TENANT) is True


async def test_enablement_missing_tenant_row_is_off() -> None:
    """No tenant row -> OFF (fail-open default), no exception."""
    assert await announce_gate_enabled(uuid.uuid4()) is False


async def test_enablement_is_cached() -> None:
    """A second read inside the TTL does not hit the DB again."""
    await _seed_tenant(enabled=True)
    assert await announce_gate_enabled(_TENANT) is True
    # Flip the row directly (no cache reset) -- the cached ON value wins.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == _TENANT).values(announce_gate_enabled=False)
        )
        await session.commit()
    assert await announce_gate_enabled(_TENANT) is True


async def test_enablement_fails_open_on_db_error() -> None:
    """A DB teardown resolves to OFF and warn-logs; never raises."""
    with (
        patch(
            "meho_backplane.broadcast.announce_gate.get_sessionmaker",
            new=MagicMock(side_effect=RuntimeError("db down")),
        ),
        patch("meho_backplane.broadcast.announce_gate._log") as mock_log,
    ):
        assert await announce_gate_enabled(_TENANT) is False
    assert any(
        call.args and call.args[0] == "announce_gate_enablement_read_failed"
        for call in mock_log.warning.call_args_list
    )


# ---------------------------------------------------------------------------
# Gate decision (announce_gate_blocks)
# ---------------------------------------------------------------------------


async def test_gate_blocks_enabled_write_caution_no_claim() -> None:
    """enabled + write-class + caution + no claim -> remediation string."""
    await _seed_tenant(enabled=True)
    with patch(
        "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim",
        new=AsyncMock(return_value=False),
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.delete", safety_level="caution", target_name="vault-1"
        )
    assert block is not None
    assert "meho_broadcast_announce" in block


async def test_gate_passes_when_claim_covers_op() -> None:
    """An active covering claim lets the op through."""
    await _seed_tenant(enabled=True)
    with patch(
        "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim",
        new=AsyncMock(return_value=True),
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.delete", safety_level="caution", target_name="vault-1"
        )
    assert block is None


async def test_gate_passes_for_disabled_tenant() -> None:
    """A disabled tenant is unaffected -- no policy read of the claim scan."""
    await _seed_tenant(enabled=False)
    claim = AsyncMock(return_value=False)
    with patch(
        "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim", new=claim
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.delete", safety_level="caution", target_name="vault-1"
        )
    assert block is None
    # Short-circuited on enablement -- the claim scan never ran.
    claim.assert_not_called()


async def test_gate_passes_for_read_class_op() -> None:
    """A read-class op is never gated, even in an enabled tenant."""
    await _seed_tenant(enabled=True)
    claim = AsyncMock(return_value=False)
    with patch(
        "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim", new=claim
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.list", safety_level="caution", target_name="vault-1"
        )
    assert block is None
    claim.assert_not_called()


async def test_gate_passes_for_safe_write_op() -> None:
    """A write-class op below ``caution`` safety is never gated."""
    await _seed_tenant(enabled=True)
    claim = AsyncMock(return_value=False)
    with patch(
        "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim", new=claim
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.delete", safety_level="safe", target_name="vault-1"
        )
    assert block is None
    claim.assert_not_called()


async def test_gate_fails_open_on_claim_scan_error() -> None:
    """A claim-scan teardown resolves to no-block and warn-logs."""
    await _seed_tenant(enabled=True)
    with (
        patch(
            "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim",
            new=AsyncMock(side_effect=RuntimeError("valkey down")),
        ),
        patch("meho_backplane.broadcast.announce_gate._log") as mock_log,
    ):
        block = await announce_gate_blocks(
            _operator(), op_id="vault.kv.delete", safety_level="caution", target_name="vault-1"
        )
    assert block is None
    assert any(
        call.args and call.args[0] == "announce_gate_check_failed"
        for call in mock_log.warning.call_args_list
    )


# ---------------------------------------------------------------------------
# Dispatch integration + CLI/MCP parity
# ---------------------------------------------------------------------------


class _NoOpVaultConnector(Connector):
    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _module_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    def __init__(self, *, name: str = "vault-1") -> None:
        self.product = "vault"
        self.fingerprint = _FakeFingerprint(version=None)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = name
        self.host = "test.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


@pytest.fixture(autouse=True)
def _reset_dispatch_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def _quiet_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _register_op(
    op_id: str, stub_embedding_service: AsyncMock, *, safety_level: str = "caution"
) -> None:
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=_module_handler,
        summary="Test op.",
        description="Test op.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
        safety_level=safety_level,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("session", [_SESSION, None], ids=["mcp-session", "cli-no-session"])
async def test_dispatch_rejected_when_gate_enabled_and_no_claim(
    session: UUID | None,
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """Enabled tenant + caution write + no claim -> denied, both surfaces.

    Parametrised over an MCP session and a session-less CLI dispatch: the
    gate keys on announce-state, not on the surface, so both are rejected
    identically -- the shared-dispatch parity the acceptance criteria
    require.
    """
    await _seed_tenant(enabled=True)
    await _register_op("vault.kv.delete", stub_embedding_service)
    with (
        patch(
            "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim",
            new=AsyncMock(return_value=False),
        ),
        _bound_session(session),
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.delete",
            target=_FakeTarget(),
            params={},
        )
    assert result.status == "denied"
    assert result.extras["error_code"] == "announce_required"
    assert "meho_broadcast_announce" in result.error


async def test_dispatch_executes_when_gate_disabled(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """A disabled tenant runs the same op unaffected."""
    await _seed_tenant(enabled=False)
    await _register_op("vault.kv.delete", stub_embedding_service)
    with _bound_session(_SESSION):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.delete",
            target=_FakeTarget(),
            params={},
        )
    assert result.status == "ok"
    assert result.result == {"echo": {}}


async def test_dispatch_executes_when_claim_present(
    stub_embedding_service: AsyncMock,
    _quiet_broadcast: list[BroadcastEvent],
) -> None:
    """An enabled tenant with a covering claim runs the op."""
    await _seed_tenant(enabled=True)
    await _register_op("vault.kv.delete", stub_embedding_service)
    with (
        patch(
            "meho_backplane.broadcast.announce_gate.caller_has_active_announce_claim",
            new=AsyncMock(return_value=True),
        ),
        _bound_session(_SESSION),
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.kv.delete",
            target=_FakeTarget(),
            params={},
        )
    assert result.status == "ok"
