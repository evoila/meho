# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed-tier conformance for ``winsrv.feature.remove`` (#3288 operator ruling).

``winsrv.feature.remove`` uninstalls a Windows role/feature. It shipped
caution-tier + approval-free; the #3288 operator ruling promotes it to
``safety_level="dangerous"`` + ``requires_approval=True`` — disruptive enough to
demand a human, but **not** ``destructive`` (reversible by reinstall,
data-preserving), so the destructive tier's preview-hash binding + mandatory
blast-radius statement do NOT apply. It mirrors the ``winsrv.localuser.delete``
dangerous-tier mould.

These tests prove the dangerous tier holds:

* **The full governed flow**: a bare dispatch parks (no preview-hash binding at
  this tier) → a *distinct human* approves → audited resume runs the uninstall.
* **The tier holds**: agent ``DENY``, no self-approval under break-glass, no
  satellite mint (``dangerous`` → EXCLUDED, never runner-minted).

The PowerShell-over-SSH transport is mocked by ``_RecordingWinsrvConnector``
(overriding the ``_run_command`` seam ``pwsh_run`` calls). Synthetic fixtures.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select

import meho_backplane.connectors.winsrv  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.winsrv import WinsrvConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations import typed_register as tr_module
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "winsrv-ssh-2022.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003288")
_OP_ID = "winsrv.feature.remove"
_FEATURE = "Failover-Clustering"
_TARGET_NAME = "test-winsrv"

_UNINSTALL_OK = json.dumps(
    {
        "ok": True,
        "success": True,
        "exit_code": "Success",
        "restart_needed": False,
        "features_changed": [_FEATURE],
    }
)


# ---------------------------------------------------------------------------
# Fixtures + doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _proc(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.exit_status = exit_status
    return p


def _decode_encoded_command(cmd: str) -> str:
    b64 = cmd.split("-EncodedCommand", 1)[1].strip()
    return base64.b64decode(b64).decode("utf-16-le")


class _RecordingWinsrvConnector(WinsrvConnector):
    """A WinsrvConnector whose pwsh transport is canned + recorded.

    Overrides the single IO seam the remove handler reaches — ``_run_command``.
    A subclass (not a duck type) so the resolver's bound-method rebind + the
    ``_CONNECTOR_INSTANCE_CACHE`` seeding behave exactly as in production.
    """

    def __init__(self) -> None:
        super().__init__()
        self.uninstall_calls: list[str] = []

    async def _run_command(
        self, target: Any, cmd: str, *, operator: Operator | None = None, timeout: float = 30.0
    ) -> Any:
        script = _decode_encoded_command(cmd)
        if "Uninstall-WindowsFeature" in script:
            self.uninstall_calls.append(script)
            return _proc(_UNINSTALL_OK)
        raise AssertionError(f"unexpected pwsh script: {script!r}")


def _make_operator(
    *, sub: str = "op-1", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="winsrv feature.remove conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "2022.x") -> None:
        self.version = version


class _FakeWinsrvTarget:
    """Duck-typed target for direct ``dispatch(...)`` (no DB name-resolve)."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "winsrv"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = "winsrv-ssh"
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "win.example.test"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.secret_ref = "meho/testing/winsrv/test-winsrv"


def _seed_instance(recorder: _RecordingWinsrvConnector) -> None:
    _CONNECTOR_INSTANCE_CACHE[WinsrvConnector] = recorder  # type: ignore[assignment]


async def _register_ops() -> None:
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await WinsrvConnector.register_operations()


async def _bootstrap(recorder: _RecordingWinsrvConnector) -> None:
    await _register_ops()
    _seed_instance(recorder)


async def _seed_target() -> UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="winsrv",
                host="win.example.test",
                port=22,
                fqdn=None,
                secret_ref="meho/testing/winsrv/test-winsrv",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2022.x"},
                preferred_impl_id="winsrv-ssh",
                notes="seeded by test_connectors_winsrv_feature_remove",
            )
        )
        await s.commit()
    return target_id


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _args(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": {"name": _FEATURE},
    }
    base.update(overrides)
    return base


# ===========================================================================
# Registration — the op is dangerous + requires_approval (NOT destructive)
# ===========================================================================


async def test_op_registered_dangerous_requires_approval() -> None:
    await _register_ops()
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert row.source_kind == "typed"
    # NOT destructive — no destructive tag, no blast-radius preview at this tier.
    assert "destructive" not in (row.tags or [])


# ===========================================================================
# The full governed flow — a bare park (no preview-hash binding at this tier)
# ===========================================================================


async def test_full_governed_flow_park_approve_resume() -> None:
    """bare dispatch parks (dangerous tier) → distinct human → uninstall runs."""
    recorder = _RecordingWinsrvConnector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")

    # A bare call parks — no preview_hash needed at the dangerous tier.
    call = await call_operation(requester, _args())
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert recorder.uninstall_calls == []  # nothing uninstalled pre-approval

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["action"] == "remove"
    assert resume.result["op_class"] == "write"
    assert len(recorder.uninstall_calls) == 1


# ===========================================================================
# No agent execution path — an AGENT principal is DENY'd
# ===========================================================================


async def test_agent_principal_is_denied() -> None:
    recorder = _RecordingWinsrvConnector()
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeWinsrvTarget(),
        params={"name": _FEATURE},
    )
    assert result.status == "denied", result
    assert recorder.uninstall_calls == []  # never executed
    assert await _pending_count() == 0  # never parked either


# ===========================================================================
# No self-approval, even under APPROVAL_ALLOW_SELF_APPROVAL
# ===========================================================================


async def test_no_self_approval_even_under_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingWinsrvConnector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="solo-operator")
    call = await call_operation(requester, _args())
    request_id = UUID(call["extras"]["approval_request_id"])

    # Break-glass ON — the dangerous tier does NOT permit self-approval either.
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, request_id, operator=requester, params=None)


# ===========================================================================
# No satellite mint — dangerous → EXCLUDED, the gateway refuses OP_NOT_SAFE
# ===========================================================================


async def test_satellite_mint_refuses_op_not_safe() -> None:
    recorder = _RecordingWinsrvConnector()
    await _bootstrap(recorder)

    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params={"name": _FEATURE},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
