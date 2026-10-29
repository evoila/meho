# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write steps are checked under the connector the target resolves to (#4991).

Inside a vCenter multi-step operation, each write step is checked against the
standing grants. The connector id of that check is the target's own connector:
``vmware-rest-8.0`` for a vCenter 8.0.x target, ``vmware-rest-9.0`` for 9.x.
So the grants an operator writes from the step list for one connector match.

These tests run ``vmware.composite.vm.create`` through the production
:func:`dispatch` as a service principal, with a recording connector double in
place of the real vCenter session.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRest80Connector, VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ServicePrincipalGrant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.governed_subops import build_governed_subops_response
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _TENANT_ID,
    _VM_CREATE_UNATTENDED_PARAMS,
    _VM_CREATE_WRITE_STEPS,
    _approval_row_count,
    _FakeFingerprint,
    _FakeVmwareTarget,
    _make_operator,
    _RecordingVmwareConnector,
)

_VM_CREATE = "vmware.composite.vm.create"
_SERVICE_SUB = "svc-blueprint"
_CONNECTOR_8_0 = "vmware-rest-8.0"
_CONNECTOR_9_0 = "vmware-rest-9.0"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches and the connector registry around each test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registrations skip ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield a session against the autouse-migrated SQLite engine."""
    async with get_sessionmaker()() as s:
        yield s


class _Recording80Connector(_RecordingVmwareConnector):
    """The recording double with the identity of the 8.0 catalog."""

    version = "8.0"


async def _bootstrap_both_catalogs(
    stub_embedding_service: AsyncMock,
) -> tuple[_RecordingVmwareConnector, _Recording80Connector]:
    """Register both vSphere catalogs and their composites, one recorder each.

    The resolver picks the 8.0 class for an 8.0.x target and the 9.0 class
    for a 9.x target; the seeded instance cache hands back the matching
    recorder instead of a real Vault-backed connector.
    """
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    register_connector_v2(
        product="vmware", version="8.0", impl_id="vmware-rest", cls=VmwareRest80Connector
    )
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    await register_vmware_composite_operations_8_0(embedding_service=stub_embedding_service)
    recorders = (_RecordingVmwareConnector(), _Recording80Connector())
    for recorder in recorders:
        recorder.responses.update(
            {
                "/vcenter/folder": {"value": [{"folder": "group-v1"}]},
                "/vcenter/vm": {"value": "vm-123"},
            }
        )
    reset_dispatcher_caches()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorders[0]  # type: ignore[assignment]
    _CONNECTOR_INSTANCE_CACHE[VmwareRest80Connector] = recorders[1]  # type: ignore[assignment]
    return recorders


async def _persist_target(version: str) -> UUID:
    """Persist a vmware target row with *version*, so a resume finds it by id."""
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=f"vcenter-{version}",
                product="vmware",
                version=version,
                host="vcenter.test.invalid",
                aliases=[],
            )
        )
        await s.commit()
    return target_id


async def _seed_grant(*, op_id: str, connector_id: str, target_id: UUID) -> None:
    """Give the service principal a standing grant on one op for one connector."""
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                reason="unattended build",
                created_by_sub="ops-admin",
            )
        )
        await s.commit()


async def _service_vm_create(
    *, connector_id: str, version: str, step_grants: dict[str, str]
) -> tuple[Any, UUID]:
    """Run ``vm.create`` as the service principal against a *version* target.

    The grant on the operation itself names *connector_id* (the connector of
    the call). *step_grants* maps each write step to the connector id its
    grant names.
    """
    target_id = await _persist_target(version)
    await _seed_grant(op_id=_VM_CREATE, connector_id=connector_id, target_id=target_id)
    for step_op_id, step_connector_id in step_grants.items():
        await _seed_grant(op_id=step_op_id, connector_id=step_connector_id, target_id=target_id)
    target = _FakeVmwareTarget(target_id=target_id)
    target.fingerprint = _FakeFingerprint(version=version)
    result = await dispatch(
        operator=_make_operator(sub=_SERVICE_SUB, principal_kind=PrincipalKind.SERVICE),
        connector_id=connector_id,
        op_id=_VM_CREATE,
        target=target,
        params=_VM_CREATE_UNATTENDED_PARAMS,
    )
    return result, target_id


_VM_CREATE_CALLS = [
    ("GET", "/vcenter/folder"),
    ("POST", "/vcenter/vm"),
    ("POST", "/vcenter/vm/vm-123/hardware/ethernet"),
    ("POST", "/vcenter/vm/vm-123/power?action=start"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connector_id", "version"),
    [(_CONNECTOR_8_0, "8.0.3"), (_CONNECTOR_9_0, "9.0.0")],
)
async def test_step_grants_naming_the_target_connector_run_unattended(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    connector_id: str,
    version: str,
) -> None:
    """Step grants that name the target's connector match: no approval request.

    A vCenter 8.0.x target is checked under ``vmware-rest-8.0``, a 9.x target
    under ``vmware-rest-9.0``. Only the recorder of the target's connector
    sees the writes.
    """
    rec_9_0, rec_8_0 = await _bootstrap_both_catalogs(stub_embedding_service)

    result, _target_id = await _service_vm_create(
        connector_id=connector_id,
        version=version,
        step_grants=dict.fromkeys(_VM_CREATE_WRITE_STEPS, connector_id),
    )

    assert result.status == "ok", result.error
    assert result.result["status"] == "created"
    used, unused = (rec_8_0, rec_9_0) if connector_id == _CONNECTOR_8_0 else (rec_9_0, rec_8_0)
    assert used.calls == _VM_CREATE_CALLS
    assert unused.calls == []
    assert await _approval_row_count() == 0


@pytest.mark.asyncio
async def test_step_grant_naming_9_0_does_not_match_a_vcenter_8_target(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """On a vCenter 8.0.x target, a step grant that names ``vmware-rest-9.0`` does not match.

    The create step waits for approval under ``vmware-rest-8.0`` and nothing
    is written. After a person approves, the operation runs again on the 8.0
    catalog and creates the VM once.
    """
    _rec_9_0, rec_8_0 = await _bootstrap_both_catalogs(stub_embedding_service)
    step_grants = dict.fromkeys(_VM_CREATE_WRITE_STEPS, _CONNECTOR_8_0)
    step_grants["POST:/vcenter/vm"] = _CONNECTOR_9_0

    result, _target_id = await _service_vm_create(
        connector_id=_CONNECTOR_8_0, version="8.0.3", step_grants=step_grants
    )

    assert result.status == "awaiting_approval", result.error
    assert [verb for verb, _ in rec_8_0.calls] == ["GET"]
    async with get_sessionmaker()() as s:
        pending = (await s.scalars(select(ApprovalRequest))).one()
    assert pending.op_id == "POST:/vcenter/vm"
    assert pending.connector_id == _CONNECTOR_8_0

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        await approve_request(s, pending.id, operator=reviewer, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, pending.id)
    assert row is not None
    resume = await resume_dispatch_after_approval(operator=reviewer, request=row, params=None)

    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "created"
    assert [c for c in rec_8_0.calls if c[0] == "POST"] == [
        call for call in _VM_CREATE_CALLS if call[0] == "POST"
    ]


@pytest.mark.asyncio
async def test_grants_written_from_the_8_0_step_list_match(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """The step list asked for ``vmware-rest-8.0`` names that connector, and its grants match.

    An operator writes one grant per grantable step, with the connector id
    the step list answers. ``vm.create`` on a vCenter 8.0.x target then runs
    with no approval request.
    """
    _rec_9_0, rec_8_0 = await _bootstrap_both_catalogs(stub_embedding_service)
    listing = await build_governed_subops_response(
        tenant_id=_TENANT_ID, op_id=_VM_CREATE, connector_id_override=_CONNECTOR_8_0
    )
    assert listing is not None
    assert listing.connector_id == _CONNECTOR_8_0
    grantable = [step.op_id for step in listing.governed_subops if step.grantable]
    assert set(_VM_CREATE_WRITE_STEPS) <= set(grantable)

    result, _target_id = await _service_vm_create(
        connector_id=_CONNECTOR_8_0,
        version="8.0.3",
        step_grants=dict.fromkeys(grantable, listing.connector_id),
    )

    assert result.status == "ok", result.error
    assert rec_8_0.calls == _VM_CREATE_CALLS
    assert await _approval_row_count() == 0
