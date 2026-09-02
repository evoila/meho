# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed-tier conformance for ``harbor.robot.delete`` (#3288 operator ruling).

``harbor.robot.delete`` permanently removes a **credential-bearing principal**:
re-creating the robot mints a BRAND-NEW secret, so every consumer still using
the old credential breaks silently (the lab signal
``bind9-harbor-dangerous-writes-bypass-approval-gate.yaml``). It shipped
caution-tier + approval-free; the #3288 operator ruling promotes it to the
governed-delete tier: ``safety_level="destructive"`` + ``requires_approval=True``
with a park-time blast-radius statement naming the robot identity + its project
association.

These tests prove the promotion holds on every surface, folded via the SINGLE
SOURCE (``safety_level="destructive"`` + the ``destructive`` tag), never a local
pattern list:

* **The park-time blast radius names the robot + its project association**, with
  an honest ``irreversibility="recreatable_new_secret"`` class.
* **The full governed flow**: preview → parked approval carrying the hash +
  blast radius → a *distinct human* approves → audited resume runs the DELETE.
* **The tier holds**: agent ``DENY``, no ``ServicePrincipalGrant``, no
  self-approval under break-glass, no satellite mint, dispatch refused without a
  matching preview hash.

The Harbor HTTP transport is mocked by ``_RecordingHarborConnector`` (overriding
the ``_http_client`` / ``auth_headers`` seams the ``robot_delete`` handler
reaches). All fixtures are synthetic — no lab names/IDs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

import meho_backplane.connectors.harbor  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.ops_robot_delete_preview import (
    _harbor_robot_delete_preview,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "harbor-rest-2.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003288")
_OP_ID = "harbor.robot.delete"
_PROJECT = "team-ci"
_ROBOT_ID = 7
_TARGET_NAME = "test-harbor"


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


@pytest.fixture
def _stub_embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _FakeResp:
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, recorder: _RecordingHarborConnector) -> None:
        self._recorder = recorder

    async def request(self, method: str, path: str, headers: Any = None) -> _FakeResp:
        self._recorder.delete_calls.append((method, path))
        return _FakeResp()


class _RecordingHarborConnector(HarborConnector):
    """A HarborConnector whose HTTP transport is canned + recorded.

    Overrides the IO seams the ``robot_delete`` handler reaches — ``_http_client``
    (the pooled DELETE client) and ``auth_headers`` (credential read) — so the
    resume path runs offline. A subclass (not a duck type) so the resolver's
    bound-method rebind + the ``_CONNECTOR_INSTANCE_CACHE`` seeding behave exactly
    as in production; the base ``robot_delete`` runs unchanged against the fakes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.delete_calls: list[tuple[str, str]] = []

    async def _http_client(self, target: Any) -> _FakeClient:  # type: ignore[override]
        return _FakeClient(self)

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:  # type: ignore[override]
        return {}


def _make_operator(
    *, sub: str = "op-1", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="harbor delete conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "2.x") -> None:
        self.version = version


class _FakeHarborTarget:
    """Duck-typed target for direct ``dispatch(...)`` (no DB name-resolve)."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "harbor"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = "harbor-rest"
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "harbor.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.secret_ref = "meho/testing/harbor/test-harbor"


def _seed_instance(recorder: _RecordingHarborConnector) -> None:
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = recorder  # type: ignore[assignment]


async def _register_ops(stub_embedding: AsyncMock) -> None:
    await register_harbor_robot_operations(embedding_service=stub_embedding)


async def _bootstrap(recorder: _RecordingHarborConnector, stub_embedding: AsyncMock) -> None:
    await _register_ops(stub_embedding)
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
                product="harbor",
                host="harbor.example.test",
                port=443,
                fqdn=None,
                secret_ref="meho/testing/harbor/test-harbor",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2.x"},
                preferred_impl_id="harbor-rest",
                notes="seeded by test_connectors_harbor_robot_delete",
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
        "params": {"project": _PROJECT, "id": _ROBOT_ID},
    }
    base.update(overrides)
    return base


def _preview_ctx(recorder: _RecordingHarborConnector, *, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=recorder,
        operator=_make_operator(),
        target=_FakeHarborTarget(),
        params=params,
        connector_id=_CONNECTOR_ID,
    )


# ===========================================================================
# Preview builder — the robot + project-association blast radius
# ===========================================================================


async def test_preview_names_robot_and_project_association() -> None:
    recorder = _RecordingHarborConnector()
    preview = await _harbor_robot_delete_preview(
        _preview_ctx(recorder, params={"project": _PROJECT, "id": _ROBOT_ID})
    )
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "harbor_robot",
        "id": _ROBOT_ID,
        "project": _PROJECT,
        "level": "project",
    }
    assert blast["children"] == [{"kind": "project_association", "project": _PROJECT}]
    assert blast["irreversibility"] == "recreatable_new_secret"
    assert blast["match_count"] == 1


async def test_preview_declines_on_missing_project() -> None:
    recorder = _RecordingHarborConnector()
    assert (
        await _harbor_robot_delete_preview(_preview_ctx(recorder, params={"id": _ROBOT_ID})) is None
    )


async def test_preview_declines_on_non_int_id() -> None:
    recorder = _RecordingHarborConnector()
    assert (
        await _harbor_robot_delete_preview(
            _preview_ctx(recorder, params={"project": _PROJECT, "id": "7"})
        )
        is None
    )


async def test_preview_declines_without_connector_instance() -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_make_operator(),
        target=_FakeHarborTarget(),
        params={"project": _PROJECT, "id": _ROBOT_ID},
        connector_id=_CONNECTOR_ID,
    )
    assert await _harbor_robot_delete_preview(ctx) is None


# ===========================================================================
# Registration — the op is destructive + requires_approval
# ===========================================================================


async def test_op_registered_destructive_requires_approval(_stub_embedding: AsyncMock) -> None:
    await _register_ops(_stub_embedding)
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert row.source_kind == "typed"
    assert "destructive" in (row.tags or [])


# ===========================================================================
# The full governed flow (the keystone)
# ===========================================================================


async def test_full_governed_flow_preview_park_approve_resume(_stub_embedding: AsyncMock) -> None:
    """preview → park (hash + blast radius) → distinct human → audited DELETE."""
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args()

    # 1) Preview binds a param-sensitive hash even for a typed op.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    assert isinstance(bound_hash, str) and len(bound_hash) == 64

    # 2) Governed call presenting the bound hash parks; no DELETE ran.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert recorder.delete_calls == []  # nothing deleted pre-approval

    # 3) The parked row carries the bound hash + the robot blast radius.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"]["kind"] == "harbor_robot"
    assert blast["object"]["id"] == _ROBOT_ID
    assert blast["object"]["project"] == _PROJECT
    assert blast["children"] == [{"kind": "project_association", "project": _PROJECT}]
    assert blast["irreversibility"] == "recreatable_new_secret"

    # 4) A DIFFERENT operator approves (four-eyes) — the binding re-verifies.
    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the DELETE fires exactly once.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["deleted"] is True
    assert resume.result["id"] == _ROBOT_ID
    assert len(recorder.delete_calls) == 1
    assert recorder.delete_calls[0][0] == "DELETE"


# ===========================================================================
# Requirement 2 — dispatch refused without a matching preview hash
# ===========================================================================


async def test_dispatch_refused_without_preview_hash(_stub_embedding: AsyncMock) -> None:
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeHarborTarget(),
        params={"project": _PROJECT, "id": _ROBOT_ID},
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert recorder.delete_calls == []
    assert await _pending_count() == 0


# ===========================================================================
# No agent execution path — an AGENT principal is DENY'd
# ===========================================================================


async def test_agent_principal_is_denied(_stub_embedding: AsyncMock) -> None:
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeHarborTarget(),
        params={"project": _PROJECT, "id": _ROBOT_ID},
    )
    assert result.status == "denied", result
    assert recorder.delete_calls == []  # never executed
    assert await _pending_count() == 0  # never parked either


# ===========================================================================
# No standing-grant path — ServicePrincipalGrant refuses via the single source
# ===========================================================================


async def test_service_grant_refuses_via_single_source_not_pattern_list(
    _stub_embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant is refused by ``safety_level="destructive"`` on the resolved
    descriptor, not by an op-id pattern list.

    ``harbor.robot.delete`` DOES carry a ``.delete`` suffix, so blank the
    delete-shaped glob list to prove the tier fold — not the pattern list — is
    what refuses (the #3213 single-source guard).
    """
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)
    monkeypatch.setattr(get_settings(), "service_grant_delete_shaped_patterns", (), raising=False)

    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub="svc-runner",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended decommission",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, "creator", payload)
    assert "destructive" in str(exc.value).lower()


# ===========================================================================
# No self-approval, even under APPROVAL_ALLOW_SELF_APPROVAL
# ===========================================================================


async def test_no_self_approval_even_under_break_glass(
    _stub_embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)
    await _seed_target()

    requester = _make_operator(sub="solo-operator")
    args = _args()
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    # Break-glass ON — it does NOT reach the destructive tier.
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, request_id, operator=requester, params=None)


# ===========================================================================
# No satellite mint — the gateway refuses with OP_NOT_SAFE
# ===========================================================================


async def test_satellite_mint_refuses_op_not_safe(_stub_embedding: AsyncMock) -> None:
    recorder = _RecordingHarborConnector()
    await _bootstrap(recorder, _stub_embedding)

    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params={"project": _PROJECT, "id": _ROBOT_ID},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
