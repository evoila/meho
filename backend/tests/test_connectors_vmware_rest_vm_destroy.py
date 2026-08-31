# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conformance tests for the first governed destructive delete (#3198).

``vmware.composite.vm.destroy`` is the first ``safety_level="destructive"``
op modeled into the tier decided in
``docs/decisions/governed-delete-operations.md``. These tests prove the
hardest gate MEHO has holds on every surface *for this op*:

* **No agent execution path** — an AGENT principal is DENY'd (never parks,
  never runs).
* **No standing-grant path** — ``ServicePrincipalGrant`` refuses the op.
* **No self-approval, even under ``APPROVAL_ALLOW_SELF_APPROVAL``** — the
  destructive tier ignores the break-glass switch.
* **No satellite mint** — the gateway refuses with ``OP_NOT_SAFE``.
* **Dispatch refused without a matching preview hash** and **park refused
  without a blast-radius block** (the #3197 binding, now reachable on the
  composite surface).
* The **full governed flow**: preview → parked approval carrying the hash +
  blast radius → a *distinct human* approves → audited resume executes the
  delete. Plus the fail-closed power-state gate, the dual arm, and the two
  structural gaps this task closed (USER auto-execute, registration
  fail-fast).

Fixtures mirror ``test_connectors_vmware_rest_composites_write_e2e.py`` (the
recording connector double seeded as the resolved instance, the autouse-
migrated SQLite engine, the deterministic embedding stub).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import register_vmware_composite_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003198")
_OP_ID = "vmware.composite.vm.destroy"
_VM = "vm-1"
_PARAMS: dict[str, Any] = {"vm": _VM}


# ---------------------------------------------------------------------------
# Fixtures
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
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator(
    *,
    sub: str = "op-destroy",
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=sub,
        name="VM Destroy Conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "9.0") -> None:
        self.version = version


class _FakeVmwareTarget:
    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


def _retrieve_result(obj_type: str, moid: str, prop: str, val: Any) -> dict[str, Any]:
    return {
        "objects": [
            {"obj": {"type": obj_type, "value": moid}, "propSet": [{"name": prop, "val": val}]}
        ]
    }


def _task_moref(value: str) -> dict[str, str]:
    return {"type": "Task", "value": value}


class _RecordingVmwareConnector:
    """Records the sub-calls the destroy composite + its preview issue."""

    _MOUNT = "/api"

    def __init__(self, *, about_version: str | None = None) -> None:
        self.responses: dict[str, Any] = {}
        self.vmomi_responses: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.vmomi_calls: list[tuple[str, Any]] = []
        self._about = about_version

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    def _spec(self, path: str) -> str:
        return path[len(self._MOUNT) :] if path.startswith(self._MOUNT) else path

    async def _about_version(self, target: Any, operator: Operator) -> str | None:
        del target, operator
        return self._about

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        spec = self._spec(path)
        self.calls.append(("GET", spec))
        return self.responses.get(spec, {"value": {}})

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        data: Any = None,
        extra_headers: Any = None,
        timeout: Any = None,
    ) -> Any:
        spec = self._spec(path)
        self.calls.append((verb, spec))
        return self.responses.get(spec, {"value": {}})

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/RetrievePropertiesEx"):
            spec_type = json["specSet"][0]["propSet"][0]["type"]
            if spec_type in self.vmomi_responses:
                return self.vmomi_responses[spec_type]
            if spec_type == "Task":
                task_moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
                return _retrieve_result("Task", task_moid, "info", {"state": "success"})
            raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")
        return self.vmomi_responses[path]


def _seed_connector(recorder: _RecordingVmwareConnector) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]


async def _bootstrap(
    recorder: _RecordingVmwareConnector, stub_embedding_service: AsyncMock
) -> None:
    _seed_connector(recorder)
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)


def _powered_off_vm(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "doomed-vm",
        "power_state": "POWERED_OFF",
        "disks": {"disk-2000": {"capacity": 42949672960, "label": "Hard disk 1"}},
        "nics": {"nic-4000": {"mac_address": "00:50:56:aa:bb:cc"}},
    }
    info.update(overrides)
    return info


def _seed_vm_reads(
    recorder: _RecordingVmwareConnector, *, vm: str = _VM, **info_overrides: Any
) -> None:
    """Seed the VM.Info REST read + an empty snapshot vim read the preview needs."""
    recorder.responses[f"/vcenter/vm/{vm}"] = {"value": _powered_off_vm(**info_overrides)}
    recorder.vmomi_responses["VirtualMachine"] = _retrieve_result(
        "VirtualMachine", vm, "snapshot", {}
    )


async def _seed_target(name: str = "test-vcenter") -> UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product="vmware",
                host="vcenter.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_vm_destroy",
            )
        )
        await s.commit()
    return target_id


async def _pending_count() -> int:
    from sqlalchemy import func, select

    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


# ---------------------------------------------------------------------------
# The op is registered destructive + requires_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_registered_destructive_requires_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    recorder = _RecordingVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    async with get_sessionmaker()() as s:
        from sqlalchemy import select

        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert row.source_kind == "composite"


# ---------------------------------------------------------------------------
# The full governed flow (the keystone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_governed_flow_preview_park_approve_resume(
    stub_embedding_service: AsyncMock,
) -> None:
    """preview → park (hash + blast radius) → distinct human approve → resume DELETE."""
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }

    # 1) Preview binds a param-sensitive hash even though the op is a composite.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    assert isinstance(bound_hash, str) and len(bound_hash) == 64

    # 2) Governed call presenting the bound hash parks; nothing executed.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert all(verb == "GET" for verb, _ in recorder.calls), recorder.calls  # no write pre-approval

    # 3) The parked row carries the bound hash + the blast-radius statement.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"] == {
        "kind": "vm",
        "moid": _VM,
        "name": "doomed-vm",
        "power_state": "POWERED_OFF",
    }
    assert {
        "kind": "disk",
        "id": "disk-2000",
        "capacity_bytes": 42949672960,
        "label": "Hard disk 1",
    } in blast["children"]
    assert {"kind": "nic", "id": "nic-4000", "mac_address": "00:50:56:aa:bb:cc"} in blast[
        "children"
    ]
    assert blast["irreversibility"] == "permanent"

    # 4) A DIFFERENT operator approves (four-eyes) — the binding re-verifies.
    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the REST delete executes exactly once (9.0 arm).
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "destroyed"
    assert resume.result["arm"] == "rest"
    assert ("DELETE", "/vcenter/vm/vm-1") in recorder.calls


# ---------------------------------------------------------------------------
# Requirement 2 — dispatch refused without a matching preview hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_refused_without_preview_hash(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeVmwareTarget(),
        params=_PARAMS,
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert recorder.calls == [] or all(v == "GET" for v, _ in recorder.calls)
    assert await _pending_count() == 0


# ---------------------------------------------------------------------------
# Requirement 3 — park refused without a blast-radius block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_park_refused_without_blast_radius(stub_embedding_service: AsyncMock) -> None:
    """When the VM can't be read the builder declines → no blast radius → refused."""
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    # VM.Info read returns a non-dict → _read_vm_info None → builder returns None.
    recorder.responses[f"/vcenter/vm/{_VM}"] = {"value": None}
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"

    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert await _pending_count() == 0


# ---------------------------------------------------------------------------
# No agent execution path — an AGENT principal is DENY'd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_principal_is_denied(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeVmwareTarget(),
        params=_PARAMS,
    )
    assert result.status == "denied", result
    assert recorder.calls == []  # never executed
    assert await _pending_count() == 0  # never parked either


# ---------------------------------------------------------------------------
# No standing-grant path — ServicePrincipalGrant refuses the op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_grant_refuses_destroy() -> None:
    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub="svc-runner",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended teardown",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, "creator", payload)
    assert "destroy" in str(exc.value).lower() or "delete-shaped" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# No self-approval, even under APPROVAL_ALLOW_SELF_APPROVAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_self_approval_even_under_break_glass(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="solo-operator")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    # Break-glass ON — it does NOT reach the destructive tier.
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, request_id, operator=requester, params=None)


# ---------------------------------------------------------------------------
# No satellite mint — the gateway refuses with OP_NOT_SAFE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_satellite_mint_refuses_op_not_safe(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)

    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE


# ---------------------------------------------------------------------------
# Fail-closed on a running VM — refused, no delete, no implicit power-off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_powered_on_vm_refused_fail_closed(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder, power_state="POWERED_ON")
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "not_powered_off"
    assert resume.result["power_state"] == "POWERED_ON"
    # No delete of any kind fired — REST or vim.
    assert all(verb == "GET" for verb, _ in recorder.calls), recorder.calls
    assert not any("Destroy_Task" in path for path, _ in recorder.vmomi_calls)


# ---------------------------------------------------------------------------
# Dual arm — a pre-9.0 target routes through the vim Destroy_Task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vim_arm_pre_9_0_destroy_task(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector(about_version="8.0.3")
    _seed_vm_reads(recorder)
    recorder.vmomi_responses[f"/VirtualMachine/{_VM}/Destroy_Task"] = _task_moref("task-77")
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "destroyed"
    assert resume.result["arm"] == "vim"
    assert resume.result["task_id"] == "task-77"
    assert any(
        path.endswith(f"/VirtualMachine/{_VM}/Destroy_Task") for path, _ in recorder.vmomi_calls
    )
    # The 9.0-only REST DELETE never fired on the vim arm.
    assert ("DELETE", "/vcenter/vm/vm-1") not in recorder.calls


# ---------------------------------------------------------------------------
# Structural gap 1 — registration fail-fast: destructive ⇒ requires_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_rejects_destructive_without_requires_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    from meho_backplane.connectors.vmware_rest.composites._write import vm_destroy_composite

    with pytest.raises(ValueError, match="requires_approval"):
        await register_composite_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id="vmware.composite.vm.destroy_broken",
            handler=vm_destroy_composite,
            summary="broken destructive op",
            description="a destructive op that (illegally) does not require approval",
            parameter_schema={"type": "object", "properties": {"vm": {"type": "string"}}},
            when_to_use=None,
            group_key=None,
            safety_level="destructive",
            requires_approval=False,
            embedding_service=stub_embedding_service,
        )


# ---------------------------------------------------------------------------
# Structural gap 2 — a USER never auto-executes a destructive op, even when
# the descriptor is (mis)declared requires_approval=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_never_auto_executes_destructive_without_requires_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    """The runtime guarantee: destructive parks for a USER regardless of the flag.

    Registers the op normally (requires_approval=True), then flips the stored
    descriptor to requires_approval=False directly in the DB — the shape the
    fail-fast guard would refuse but which a hand-edited row could produce.
    A USER dispatch presenting the bound hash still PARKS (awaiting_approval),
    never auto-executes. Without the ``_non_agent_verdict`` destructive branch
    this would run the delete.
    """
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    from sqlalchemy import update

    async with get_sessionmaker()() as s:
        await s.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _OP_ID)
            .values(requires_approval=False)
        )
        await s.commit()
    reset_dispatcher_caches()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]

    requester = _make_operator(sub="op-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": "test-vcenter",
        "params": _PARAMS,
    }
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})

    assert call["status"] == "awaiting_approval", call
    assert all(verb == "GET" for verb, _ in recorder.calls), recorder.calls  # no write ran
    assert ("DELETE", "/vcenter/vm/vm-1") not in recorder.calls


# ---------------------------------------------------------------------------
# The composite preview hash is param-sensitive (the #3198 extension)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_preview_hash_is_param_sensitive(stub_embedding_service: AsyncMock) -> None:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    base = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": "test-vcenter"}

    p1 = await preview_operation(requester, {**base, "params": {"vm": "vm-1"}})
    p1b = await preview_operation(requester, {**base, "params": {"vm": "vm-1"}})
    p2 = await preview_operation(requester, {**base, "params": {"vm": "vm-2"}})
    assert p1["status"] == "ok"
    assert p1["preview_hash"] == p1b["preview_hash"]  # stable for identical args
    assert p1["preview_hash"] != p2["preview_hash"]  # a different delete → different hash
