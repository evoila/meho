# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stored operation with a short Kubernetes path gets the answer of the full path.

An operation that an older ingest stored, whose path looks like a
Kubernetes path without ``/api/v1`` or ``/apis/<group>/<version>``, waits
for a person in exactly the cases where the same call on the full path
waits: for every login, with and without a standing grant, an agent grant
and a run approval, and as a step. Grant create, agent grant create and
run approval create refuse such an operation. A plain read keeps its rules.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.service import SensorRequiresNoPersonError, _require_read
from meho_backplane.connectors.kubernetes.ingested_paths import ingested_op_is_credential_call
from meho_backplane.connectors.kubernetes.short_paths import full_kubernetes_path
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    credential_grant_reason,
)
from tests.test_credential_calls_always_wait import (
    _RUN,
    _SERVICE,
    _TENANT,
    _agent_grant,
    _service,
)
from tests.test_every_k8s_apply_and_api_write_waits import (
    _assert_waits_for_every_login,
    _store,
)
from tests.test_guest_reads_and_pod_applies_wait import _LOGINS, _settings_env, _verdict

__all__ = ["_settings_env"]

_WAITING = [
    "GET:/namespaces/{namespace}/secrets",
    "GET:/namespaces/{namespace}/secrets/{name}",
    "GET:/watch/namespaces/{namespace}/secrets",
    "POST:/namespaces/{namespace}/pods/{name}/exec",
    "GET:/namespaces/{namespace}/pods/{name}/portforward",
    "POST:/namespaces/{namespace}/serviceaccounts/{name}/token",
    "POST:/namespaces/{namespace}/configmaps",
    "PUT:/v1/namespaces/{namespace}/services/{name}",
    "DELETE:/apps/v1/namespaces/{namespace}/deployments/{name}",
    "PATCH:/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}",
    "GET:/x/namespaces/{namespace}/secrets",
]


def _lower(op_id: str) -> str:
    method, _, path = op_id.partition(":")
    return f"{method.lower()}:{path}"


_ALL_WAITING = [*_WAITING, *(_lower(op_id) for op_id in _WAITING)]

_KEEP_RULES = [
    "GET:/namespaces/{namespace}/pods",
    "GET:/apps/v1/namespaces/{namespace}/deployments/{name}",
]

_OTHER_PATHS = [
    "/nodes/{node}/qemu",
    "/namespaces/{namespace}/workflows/{id}",
    "/api/namespaces/{namespace}/services/{service}",
    "/cloud/namespaces/{namespace}",
    "/helm.cattle.io/v1/namespaces/{namespace}/helmcharts",
]

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

_PARAMS = {"namespace": "apps", "name": "a"}


def _short_paths() -> list[str]:
    return [op_id.partition(":")[2] for op_id in [*_WAITING, *_KEEP_RULES]]


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("path", _short_paths())
def test_a_short_path_gets_the_answer_of_the_full_path(path: str, method: str) -> None:
    full = full_kubernetes_path(path)
    assert full is not None
    expected = ingested_op_is_credential_call(f"{method}:{full}")
    assert ingested_op_is_credential_call(f"{method}:{path}") is expected
    assert ingested_op_is_credential_call(f"{method.lower()}:{path}") is expected


@pytest.mark.parametrize("op_id", _ALL_WAITING)
def test_a_short_path_that_waits_has_a_grant_refusal(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
    assert credential_grant_reason(op_id) is not None


@pytest.mark.parametrize("op_id", _KEEP_RULES)
def test_a_plain_read_of_a_short_path_keeps_its_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False
    assert credential_grant_reason(op_id) is None


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize("path", _OTHER_PATHS)
def test_other_paths_keep_their_rules(path: str, method: str) -> None:
    assert full_kubernetes_path(path) is None
    assert ingested_op_is_credential_call(f"{method}:{path}") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALL_WAITING)
async def test_a_stored_short_path_waits_for_every_login(op_id: str) -> None:
    await _assert_waits_for_every_login(await _store(op_id), _PARAMS)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _KEEP_RULES)
async def test_a_stored_plain_read_of_a_short_path_runs_at_once(op_id: str) -> None:
    row = await _store(op_id)
    for operator in _LOGINS:
        verdict, reason = await _verdict(operator, row, _PARAMS)
        assert verdict is PermissionVerdict.AUTO_EXECUTE, (operator.principal_kind, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _WAITING)
async def test_grant_create_refuses_a_short_path_that_waits(op_id: str) -> None:
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE,
                op_id=op_id,
                connector_id="kubeapi-1.30-kubeapi-rest",
                reason="unattended deploy",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _WAITING)
async def test_agent_grant_create_refuses_a_short_path_that_waits(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _WAITING)
async def test_run_approval_create_refuses_a_short_path_that_waits(op_id: str) -> None:
    with pytest.raises(RunApprovalValidationError) as refused:
        await RunApprovalService().request(
            _service(),
            RunApprovalCreate(
                work_ref=_RUN,
                entries=[RunApprovalEntry(connector_id="kubeapi-1.30-kubeapi-rest", op_id=op_id)],
                reason="one deploy",
            ),
        )
    assert refused.value.message == f"entries[0]: {credential_grant_reason(op_id)} Leave it out."


_SENSOR_OPS = [
    "GET:/namespaces/{namespace}/secrets",
    "PUT:/v1/namespaces/{namespace}/services/{name}",
]


@pytest.mark.parametrize("op_id", _SENSOR_OPS)
def test_a_sensor_of_a_short_path_that_waits_is_refused(op_id: str) -> None:
    payload = SimpleNamespace(connector_id="kubeapi-1.30", op_id=op_id, params=_PARAMS)
    with pytest.raises(SensorRequiresNoPersonError):
        _require_read(payload, SimpleNamespace(safety_level="safe"))  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _SENSOR_OPS)
async def test_a_sensor_of_a_short_path_that_waits_reads_unknown(
    monkeypatch: pytest.MonkeyPatch, op_id: str
) -> None:
    async def _lookup(**_kwargs: Any) -> Any:
        return SimpleNamespace(safety_level="safe")

    monkeypatch.setattr(sensor_runner, "lookup_descriptor", _lookup)
    snap = SimpleNamespace(
        connector_id="kubeapi-1.30", op_id=op_id, params=_PARAMS, tenant_id=_TENANT
    )
    outcome = await sensor_runner._safe_tier_floor(snap)  # type: ignore[arg-type]
    assert outcome is not None
    assert "op_waits_for_person" in str(outcome)
