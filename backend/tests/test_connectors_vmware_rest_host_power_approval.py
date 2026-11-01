# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The host power operations wait for a second person through the dispatcher.

Each of the five changes is registered with ``requires_approval=True``. A
person's call parks with an approval request and sends nothing to vCenter.
For the maintenance-mode start and the reboot, the approved resume then runs
the call once, and the write step does not wait for a second approval.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _CONNECTOR_ID,
    _TENANT_ID,
    _bootstrap,
    _FakeVmwareTarget,
    _make_operator,
    _RecordingVmwareConnector,
    _vmomi_task_moref,
)


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


class _Recorder(_RecordingVmwareConnector):
    """The shared recording double, plus the keyword the strict object read passes."""

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: Any = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        del promote_managed_object_not_found, session_used, session_token
        return await super()._post_vmomi_json(target, path, operator=operator, json=json)


def _host(maintenance: bool) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "HostSystem", "value": "host-1"},
                "propSet": [
                    {"name": "name", "val": "<host-name>"},
                    {"name": "runtime.connectionState", "val": "connected"},
                    {"name": "runtime.powerState", "val": "poweredOn"},
                    {"name": "runtime.inMaintenanceMode", "val": maintenance},
                ],
            }
        ]
    }


#: op id -> (the vCenter method, whether the host is in maintenance mode).
_METHODS: dict[str, tuple[str, bool]] = {
    "vmware.composite.host.maintenance.enter": ("EnterMaintenanceMode_Task", False),
    "vmware.composite.host.reboot": ("RebootHost_Task", True),
    "vmware.composite.host.shutdown": ("ShutdownHost_Task", True),
    "vmware.composite.host.standby.enter": ("PowerDownHostToStandBy_Task", True),
    "vmware.composite.host.standby.exit": ("PowerUpHostFromStandBy_Task", True),
}


async def _setup(recorder: _Recorder, stub_embedding_service: AsyncMock) -> _FakeVmwareTarget:
    await _bootstrap(recorder, stub_embedding_service)  # type: ignore[arg-type]
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name="test-vcenter",
                product="vmware",
                host="vcenter.test.invalid",
                aliases=[],
            )
        )
        await s.commit()
    return _FakeVmwareTarget(target_id=target_id)


@pytest.mark.parametrize("op_id", sorted(_METHODS))
async def test_a_persons_call_waits_for_approval_and_sends_nothing(
    op_id: str, stub_embedding_service: AsyncMock
) -> None:
    method, maintenance = _METHODS[op_id]
    recorder = _Recorder()
    recorder.vmomi_responses["HostSystem"] = _host(maintenance)
    target = await _setup(recorder, stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(sub="ops-human", principal_kind=PrincipalKind.USER),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params={"host": "host-1"},
    )

    assert result.status == "awaiting_approval", result.error
    assert [p for p, _ in recorder.vmomi_calls if p.endswith(method)] == []


@pytest.mark.parametrize(
    ("op_id", "reads", "expected"),
    [
        ("vmware.composite.host.maintenance.enter", [False, False], "accepted"),
        ("vmware.composite.host.reboot", [True, True, True], "done"),
    ],
    ids=["maintenance.enter", "reboot"],
)
async def test_one_approval_runs_the_call(
    op_id: str, reads: list[bool], expected: str, stub_embedding_service: AsyncMock
) -> None:
    method = _METHODS[op_id][0]
    recorder = _Recorder()
    # The host reads: the approval preview, then the handler's reads.
    recorder.vmomi_responses["HostSystem"] = iter([_host(m) for m in reads])
    recorder.vmomi_responses[f"/HostSystem/host-1/{method}"] = _vmomi_task_moref("task-1")
    target = await _setup(recorder, stub_embedding_service)
    params = {"host": "host-1"}

    parked = await dispatch(
        operator=_make_operator(sub="ops-human", principal_kind=PrincipalKind.USER),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
    )
    assert parked.status == "awaiting_approval", parked.error

    reviewer = _make_operator(sub="ops-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as s:
        row = await approve_request(
            s, UUID(parked.extras["approval_request_id"]), operator=reviewer, params=params
        )
        await s.commit()
    assert row.status == ApprovalRequestStatus.APPROVED.value

    done = await dispatch(
        operator=reviewer,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    assert done.status == "ok", done.error
    assert done.result["status"] == expected
    assert done.result["task"] == "task-1"
    assert [p for p, _ in recorder.vmomi_calls if p.endswith(method)] == [
        f"/HostSystem/host-1/{method}"
    ]
    async with get_sessionmaker()() as s:
        count = (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar()
    assert count == 1, "the write step must not wait for a second approval"
