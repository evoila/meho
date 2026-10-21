# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Calls under a delete grant in sandbox mode (#4622).

The harness is the one ``test_connectors_vmware_rest_vm_destroy.py`` uses
(a recording connector double, the migrated SQLite engine, the embedding
stub). The service principal holds delete grants for
``vmware.composite.vm.destroy`` and for its delete step.

* **Calls, mode on** -- a ``destructive`` call under a matching delete
  grant without ``preview_hash`` is refused; a wrong hash gives
  ``preview_hash_mismatch``; a missing blast radius gives
  ``blast_radius_required``; the right hash with a valid blast radius runs
  on both arms (vCenter 9 REST, older vCenter SOAP) with no approval
  request.
* **Order** -- each refused call leaves no grant-use audit row and no
  ``approval.auto_approved`` event.
* **Failed audit write** -- when the grant-use row cannot be committed,
  the call does not run.
* **Grant states** -- an expired, a revoked, and a grant for another
  target, operation or service principal do not clear a call; a grant
  revoked between two steps stops the second step; a clock moved past the
  expiry stops the grant.
* **Audit content** -- the grant-use row of a ``destructive`` call holds
  the preview hash and the blast radius; a delete below ``destructive``
  holds the proposed effect; the row of a step names its parent.
* **Other delete signs** -- the verb ``DELETE``, the tag ``destructive``
  and a delete name run under a delete grant with the mode on and wait
  with it off.
* **Steps** -- a ``destructive`` step gate is still refused under a
  matching grant.
* **Unchanged** -- a person, an agent login, an agent run hosted by MEHO
  and a satellite get the same delete verdict as with the mode off.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    EventOutbox,
    PermissionVerdict,
    SandboxDeleteGrant,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests.test_connectors_vmware_rest_vm_destroy import (
    _CONNECTOR_ID,
    _OP_ID,
    _PARAMS,
    _TENANT_ID,
    _VM,
    _bootstrap,
    _FakeVmwareTarget,
    _make_operator,
    _pending_count,
    _RecordingVmwareConnector,
    _seed_target,
    _seed_vm_reads,
    _task_moref,
)

_SERVICE_SUB = "svc:test-runner"
_REST_STEP = "DELETE:/vcenter/vm/{vm}"
_VIM_STEP = "POST:/VirtualMachine/{moId}/Destroy_Task"
_ARGS: dict[str, Any] = {
    "connector_id": _CONNECTOR_ID,
    "op_id": _OP_ID,
    "target": "test-vcenter",
    "params": _PARAMS,
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
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


def _service() -> Operator:
    return _make_operator(sub=_SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)


async def _grant_vm_destroy(
    *, step: str = _REST_STEP, **overrides: Any
) -> tuple[uuid.UUID, uuid.UUID]:
    """Delete grants for the composite and its delete step, on any vmware target."""
    fields: dict[str, Any] = {
        "tenant_id": _TENANT_ID,
        "principal_sub": _SERVICE_SUB,
        "connector_id": _CONNECTOR_ID,
        "target_product": "vmware",
    }
    fields.update(overrides)
    op_grant = await seed_delete_grant(op_id=_OP_ID, **fields)
    step_grant = await seed_delete_grant(op_id=step, **fields)
    return op_grant, step_grant


async def _ready(
    stub_embedding_service: AsyncMock, *, about_version: str = "9.0.0"
) -> _RecordingVmwareConnector:
    recorder = _RecordingVmwareConnector(about_version=about_version)
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()
    return recorder


async def _preview_hash(operator: Operator) -> str:
    preview = await preview_operation(operator, _ARGS)
    assert preview["status"] == "ok", preview
    return str(preview["preview_hash"])


async def _auto_approved_events() -> list[str]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EventOutbox))).scalars().all()
    return [row.event_kind for row in rows if "auto_approved" in row.event_kind]


async def _audit_rows_for(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if row.payload.get("op_id") == op_id]


def _deleted(recorder: _RecordingVmwareConnector) -> bool:
    return ("DELETE", f"/vcenter/vm/{_VM}") in recorder.calls


# ---------------------------------------------------------------------------
# The destructive call: preview hash, blast radius, both arms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_right_hash_runs_on_the_rest_arm_with_no_approval(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    op_grant, step_grant = await _grant_vm_destroy()
    service = _service()

    call = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert call["status"] == "ok", call
    assert call["result"]["status"] == "destroyed"
    assert call["result"]["arm"] == "rest"
    assert _deleted(recorder)
    assert await _pending_count() == 0

    (op_use,) = await grant_use_rows(op_grant)
    assert op_use.payload["delete_grant"] is True
    assert op_use.payload["preview_hash"] == call["extras"].get(
        "preview_hash", op_use.payload["preview_hash"]
    )
    assert op_use.payload["blast_radius"]["object"]["moid"] == _VM
    assert op_use.payload["blast_radius"]["irreversibility"] == "permanent"
    assert op_use.parent_audit_id is None

    (step_use,) = await grant_use_rows(step_grant)
    assert step_use.payload["delete_grant"] is True
    assert "proposed_effect" in step_use.payload
    assert "preview_hash" not in step_use.payload
    # The step's row names the audit row of its multi-step operation.
    assert step_use.parent_audit_id is not None
    async with get_sessionmaker()() as session:
        parent = await session.get(AuditLog, step_use.parent_audit_id)
    assert parent is not None
    assert parent.payload.get("op_id") == _OP_ID


@pytest.mark.asyncio
async def test_right_hash_runs_on_the_vim_arm_with_no_approval(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service, about_version="8.0.3")
    recorder.vmomi_responses[f"/VirtualMachine/{_VM}/Destroy_Task"] = _task_moref("task-91")
    op_grant, step_grant = await _grant_vm_destroy(step=_VIM_STEP)
    service = _service()

    call = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert call["status"] == "ok", call
    assert call["result"]["status"] == "destroyed"
    assert call["result"]["arm"] == "vim"
    assert any(
        path.endswith(f"/VirtualMachine/{_VM}/Destroy_Task") for path, _ in recorder.vmomi_calls
    )
    assert not _deleted(recorder)
    assert await _pending_count() == 0
    assert len(await grant_use_rows(op_grant)) == 1
    assert len(await grant_use_rows(step_grant)) == 1


@pytest.mark.asyncio
async def test_no_preview_hash_is_refused_with_no_grant_use(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy()

    call = await call_operation(_service(), _ARGS)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not _deleted(recorder)
    assert await _pending_count() == 0
    assert await grant_use_rows() == []
    assert await _auto_approved_events() == []


@pytest.mark.asyncio
async def test_wrong_preview_hash_is_refused_with_no_grant_use(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy()

    call = await call_operation(_service(), {**_ARGS, "preview_hash": "0" * 64})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_hash_mismatch"
    assert not _deleted(recorder)
    assert await _pending_count() == 0
    assert await grant_use_rows() == []
    assert await _auto_approved_events() == []


@pytest.mark.asyncio
async def test_missing_blast_radius_is_refused_with_no_grant_use(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    # The VM read gives no object, so the preview builds no blast radius.
    recorder.responses[f"/vcenter/vm/{_VM}"] = {"value": None}
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()
    await _grant_vm_destroy()
    service = _service()

    call = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert not _deleted(recorder)
    assert await _pending_count() == 0
    assert await grant_use_rows() == []
    assert await _auto_approved_events() == []


@pytest.mark.asyncio
async def test_failed_grant_use_audit_write_stops_the_call(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy()
    service = _service()
    preview_hash = await _preview_hash(service)

    async def _cannot_commit(**_kwargs: Any) -> uuid.UUID:
        raise RuntimeError("audit row could not be committed")

    monkeypatch.setattr(
        "meho_backplane.operations.service_grants._record_grant_use", _cannot_commit
    )
    with pytest.raises(RuntimeError, match="could not be committed"):
        await dispatch(
            operator=service,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=_FakeVmwareTarget(),
            params=dict(_PARAMS),
            preview_hash=preview_hash,
        )
    assert not _deleted(recorder)
    assert await grant_use_rows() == []


# ---------------------------------------------------------------------------
# Grant states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["expired", "revoked", "other_target", "other_op", "other_principal"],
)
async def test_a_grant_that_does_not_match_does_not_clear_the_call(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    overrides: dict[str, Any] = {}
    op_id = _OP_ID
    if state == "expired":
        overrides["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    elif state == "revoked":
        overrides["revoked_at"] = datetime.now(UTC)
    elif state == "other_target":
        overrides = {"target_product": None, "target_id": uuid.uuid4()}
    elif state == "other_op":
        op_id = "vmware.composite.vm.power"
    elif state == "other_principal":
        overrides["principal_sub"] = "svc:someone-else"
    await seed_delete_grant(
        tenant_id=_TENANT_ID,
        principal_sub=overrides.pop("principal_sub", _SERVICE_SUB),
        op_id=op_id,
        connector_id=_CONNECTOR_ID,
        target_product=overrides.pop("target_product", "vmware"),
        **overrides,
    )
    service = _service()

    call = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    # No grant covers the call: the service principal's delete waits for a person.
    assert call["status"] == "awaiting_approval", call
    assert not _deleted(recorder)
    assert await _pending_count() == 1
    assert await grant_use_rows() == []


@pytest.mark.asyncio
async def test_clock_moved_past_the_expiry_stops_the_grant(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No code renews a grant: once its expiry passes it clears nothing."""
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy(expires_at=soon(1))
    service = _service()

    first = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert first["status"] == "ok", first
    assert _deleted(recorder)

    class _Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr("meho_backplane.operations.service_grants.datetime", _Later)
    recorder.calls.clear()
    second = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert second["status"] == "awaiting_approval", second
    assert not _deleted(recorder)


@pytest.mark.asyncio
async def test_grant_revoked_between_two_steps_stops_the_second_step(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    _op_grant, step_grant = await _grant_vm_destroy()
    service = _service()
    preview_hash = await _preview_hash(service)

    original_about_version = recorder._about_version

    async def _revoke_then_answer(target: Any, operator: Operator) -> str | None:
        # The composite has cleared its own gate and read the VM; the
        # delete step's gate comes next. Revoke the step's grant now.
        async with get_sessionmaker()() as session:
            row = await session.get(SandboxDeleteGrant, step_grant)
            assert row is not None
            row.revoked_at = datetime.now(UTC)
            await session.commit()
        return await original_about_version(target, operator)

    monkeypatch.setattr(recorder, "_about_version", _revoke_then_answer)
    call = await call_operation(service, {**_ARGS, "preview_hash": preview_hash})
    assert call["status"] == "awaiting_approval", call
    assert not _deleted(recorder)
    assert await grant_use_rows(step_grant) == []


# ---------------------------------------------------------------------------
# Other delete signs below destructive: the gate alone decides
# ---------------------------------------------------------------------------


def _descriptor(op_id: str, **fields: Any) -> EndpointDescriptor:
    base: dict[str, Any] = {
        "product": "vmware",
        "version": "9.0",
        "impl_id": "vmware-rest",
        "op_id": op_id,
        "source_kind": "ingested",
        "safety_level": "dangerous",
        "requires_approval": False,
        "parameter_schema": {},
    }
    base.update(fields)
    return EndpointDescriptor(**base)


_SIGNS: dict[str, EndpointDescriptor] = {
    "verb": _descriptor("DELETE:/vcenter/vm/{vm}", method="DELETE"),
    "tag": _descriptor("vmware.vm.wipe", source_kind="typed", tags=["write", "destructive"]),
    "name": _descriptor("vmware.vm.delete", source_kind="typed"),
    "vendor_action_name": _descriptor(_VIM_STEP, method="POST"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", sorted(_SIGNS))
async def test_a_delete_below_destructive_runs_under_a_delete_grant(
    monkeypatch: pytest.MonkeyPatch, sign: str
) -> None:
    descriptor = _SIGNS[sign]
    target = _FakeVmwareTarget()
    grant_id = await seed_delete_grant(
        tenant_id=_TENANT_ID,
        principal_sub=_SERVICE_SUB,
        op_id=descriptor.op_id,
        connector_id=_CONNECTOR_ID,
        target_id=target.id,
    )

    turn_sandbox_mode_off(monkeypatch)
    verdict, reason = await policy_gate(
        operator=_service(),
        descriptor=descriptor,
        target=target,
        connector_id=_CONNECTOR_ID,
        params={"vm": _VM},
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    assert await grant_use_rows() == []

    turn_sandbox_mode_on(monkeypatch)
    verdict, reason = await policy_gate(
        operator=_service(),
        descriptor=descriptor,
        target=target,
        connector_id=_CONNECTOR_ID,
        params={"vm": _VM},
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    (row,) = await grant_use_rows(grant_id)
    assert row.payload["delete_grant"] is True
    assert "proposed_effect" in row.payload
    assert "preview_hash" not in row.payload


# ---------------------------------------------------------------------------
# Steps: a destructive step gate stays refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destructive_step_gate_is_refused_under_a_matching_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    grant_id = await seed_delete_grant(
        tenant_id=_TENANT_ID, principal_sub=_SERVICE_SUB, op_id=_OP_ID, connector_id=_CONNECTOR_ID
    )
    result = await enforce_subop_policy(
        operator=_service(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        safety_level="destructive",
        requires_approval=False,
        target=None,
        params=dict(_PARAMS),
    )
    assert result is not None
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert await grant_use_rows(grant_id) == []


# ---------------------------------------------------------------------------
# Unchanged with the mode on: people, agents, hosted runs, satellites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_person_still_waits(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    person = _make_operator(sub="op-person")
    await seed_delete_grant(
        tenant_id=_TENANT_ID,
        principal_sub="op-person",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_product="vmware",
    )
    call = await call_operation(person, {**_ARGS, "preview_hash": await _preview_hash(person)})
    assert call["status"] == "awaiting_approval", call
    assert not _deleted(recorder)
    assert await grant_use_rows() == []


@pytest.mark.asyncio
async def test_an_agent_login_is_still_refused(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await seed_delete_grant(
        tenant_id=_TENANT_ID,
        principal_sub="agent-1",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_product="vmware",
    )
    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeVmwareTarget(),
        params=dict(_PARAMS),
    )
    assert result.status == "denied", result
    assert not _deleted(recorder)
    assert await _pending_count() == 0


@pytest.mark.asyncio
async def test_a_hosted_agent_run_is_still_refused(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service principal's delete grant does not reach a run hosted by MEHO."""
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy()
    service = _service()
    preview_hash = await _preview_hash(service)
    with actor_delegation("agent:hosted-helper"):
        result = await dispatch(
            operator=service,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=_FakeVmwareTarget(),
            params=dict(_PARAMS),
            preview_hash=preview_hash,
        )
    assert result.status == "denied", result
    assert not _deleted(recorder)
    assert await grant_use_rows() == []


@pytest.mark.asyncio
async def test_satellite_mint_still_refuses_op_not_safe(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = _RecordingVmwareConnector()
    await _bootstrap(recorder, stub_embedding_service)
    await _grant_vm_destroy(target_product=None)

    async with get_sessionmaker()() as session:
        result = await mint_gateway_command(
            session,
            operator=_service(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params=dict(_PARAMS),
            runner_id="runner-1",
        )
        await session.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
    assert await grant_use_rows() == []


# ---------------------------------------------------------------------------
# Mode off: the same delete grants are inert; nothing reads their table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_delete_grants_are_inert(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    recorder = await _ready(stub_embedding_service)
    await _grant_vm_destroy()
    turn_sandbox_mode_off(monkeypatch)
    service = _service()

    call = await call_operation(service, {**_ARGS, "preview_hash": await _preview_hash(service)})
    assert call["status"] == "awaiting_approval", call
    assert not _deleted(recorder)
    assert await grant_use_rows() == []
