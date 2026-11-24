# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Kubernetes apply and every Kubernetes API write of an object waits for a person.

``k8s.apply`` waits on every call, for every kind of object. So does a
``POST``, ``PUT``, ``PATCH`` or ``DELETE`` on the path of an object or of a
collection of objects through an ingested Kubernetes API file, also with a
path prefix in front. No standing grant, no agent grant and no run approval
covers such a call, also not as a step of a multi-step run.

The card of an apply names every container, also when one command line
is longer than the card can show: that entry is cut and marked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.agents.grants import GrantValidationError as AgentGrantValidationError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.service import SensorRequiresNoPersonError, _require_read
from meho_backplane.connectors.kubernetes.ingested_paths import ingested_op_is_credential_call
from meho_backplane.connectors.kubernetes.ops_write_preview import (
    MANIFEST_ECHO_MAX_BYTES,
    apply_run_commands,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.op_classes import call_always_waits
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations.composite import approved_run_var, enforce_subop_policy
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
    _register_all,
    _service,
)
from tests.test_guest_reads_and_pod_applies_wait import (
    _LOGINS,
    _assert_waits_with_and_without_a_grant,
    _ingested,
    _row,
    _settings_env,
)

__all__ = ["_settings_env"]

#: ``(apiVersion, kind, plural, namespaced)`` of the kinds the tests cover:
#: a workload, a role, a Secret, a ConfigMap, a Service, an Ingress, a
#: Namespace, a NetworkPolicy, a PersistentVolumeClaim and more core and
#: cluster-wide kinds. The last one is a made-up custom kind.
_KINDS: list[tuple[str, str, str, bool]] = [
    ("apps/v1", "Deployment", "deployments", True),
    ("rbac.authorization.k8s.io/v1", "Role", "roles", True),
    ("v1", "Secret", "secrets", True),
    ("v1", "ConfigMap", "configmaps", True),
    ("v1", "Service", "services", True),
    ("networking.k8s.io/v1", "Ingress", "ingresses", True),
    ("v1", "Namespace", "namespaces", False),
    ("networking.k8s.io/v1", "NetworkPolicy", "networkpolicies", True),
    ("v1", "PersistentVolumeClaim", "persistentvolumeclaims", True),
    ("v1", "ServiceAccount", "serviceaccounts", True),
    ("autoscaling/v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers", True),
    ("scheduling.k8s.io/v1", "PriorityClass", "priorityclasses", False),
    ("v1", "Node", "nodes", False),
    ("widgets.example.test/v1", "Widget", "widgets", True),
]
_KIND_IDS = [f"{api_version.split('/')[0]}-{kind}" for api_version, kind, _, _ in _KINDS]

#: A prefix a proxy puts in front of the Kubernetes API paths.
_PREFIX = "/k8s/clusters/{cluster}"
_PREFIXES = ["", _PREFIX]


def _object(api_version: str, kind: str, **body: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": "a", "namespace": "apps"},
    }
    doc.update(body)
    return doc


def _manifest(api_version: str, kind: str, **body: Any) -> str:
    return yaml.safe_dump(_object(api_version, kind, **body))


def _write_path(api_version: str, plural: str, namespaced: bool, *, name: bool = False) -> str:
    base = f"/apis/{api_version}" if "/" in api_version else f"/api/{api_version}"
    path = f"{base}/namespaces/{{namespace}}" if namespaced else base
    path += f"/{plural}"
    return path + "/{name}" if name else path


def _writes(api_version: str, plural: str, namespaced: bool, prefix: str) -> list[str]:
    """The create, replace, patch and delete paths of one kind, and a delete of the collection."""
    collection = prefix + _write_path(api_version, plural, namespaced)
    one = prefix + _write_path(api_version, plural, namespaced, name=True)
    return [
        f"POST:{collection}",
        f"PUT:{one}",
        f"PATCH:{one}",
        f"DELETE:{one}",
        f"DELETE:{collection}",
    ]


async def _step_waits(operator: Operator, op_id: str, params: dict[str, Any]) -> bool:
    approved = approved_run_var.set(True)
    token = work_ref_var.set(_RUN)
    try:
        result = await enforce_subop_policy(
            operator=operator,
            connector_id="k8s-1.x",
            op_id=op_id,
            safety_level="caution",
            requires_approval=False,
            target=None,
            params=params,
        )
    finally:
        work_ref_var.reset(token)
        approved_run_var.reset(approved)
    return result is not None and result.status == "awaiting_approval"


async def _assert_waits_for_every_login(row: EndpointDescriptor, params: dict[str, Any]) -> None:
    """*row* waits as a direct call and as a step, for each login, with and without each grant."""
    await _assert_waits_with_and_without_a_grant(row, params)
    for operator in _LOGINS:
        assert await _step_waits(operator, row.op_id, params), operator.principal_kind


# ---------------------------------------------------------------------------
# k8s.apply
# ---------------------------------------------------------------------------


def _apply_params() -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = [
        {"manifest": _manifest(api_version, kind)} for api_version, kind, _, _ in _KINDS
    ]
    items = [_object(api_version, kind) for api_version, kind, _, _ in _KINDS]
    params.append(
        {"manifest": yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": items})}
    )
    params.append({"manifest": "---\n".join(yaml.safe_dump(item) for item in items)})
    params.extend(
        [{"manifest": "a: [b"}, {"manifest": ""}, {"manifest": 1}, {}],
    )
    return params


_APPLY_PARAM_IDS = [*_KIND_IDS, "list", "many-documents", "not-yaml", "empty", "not-text", "none"]


@pytest.mark.parametrize("params", _apply_params(), ids=_APPLY_PARAM_IDS)
def test_every_k8s_apply_always_waits(params: dict[str, Any]) -> None:
    assert call_always_waits("k8s.apply", params) is True
    assert credential_grant_reason("k8s.apply") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("api_version", "kind", "plural", "namespaced"), _KINDS, ids=_KIND_IDS)
async def test_a_k8s_apply_of_every_kind_waits_for_every_login(
    api_version: str, kind: str, plural: str, namespaced: bool
) -> None:
    rows = await _register_all()
    params = {"manifest": _manifest(api_version, kind)}
    await _assert_waits_for_every_login(_row(rows, "k8s.apply"), params)


# ---------------------------------------------------------------------------
# The paths of an ingested Kubernetes API file
# ---------------------------------------------------------------------------


def _all_writes() -> list[str]:
    return [
        op_id
        for api_version, _, plural, namespaced in _KINDS
        for prefix in _PREFIXES
        for op_id in _writes(api_version, plural, namespaced, prefix)
    ]


@pytest.mark.parametrize("op_id", _all_writes())
def test_every_api_write_of_an_object_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
    assert ingested_op_is_credential_call(op_id + "/") is True
    assert credential_grant_reason(op_id) is not None


@pytest.mark.parametrize(
    "op_id",
    [
        f"{_PREFIX}/api/v1/namespaces/{{namespace}}/secrets/{{name}}",
        f"{_PREFIX}/api/v1/namespaces/{{namespace}}/pods/{{name}}/exec",
        f"/proxy{_PREFIX}/api/v1/namespaces/{{namespace}}/pods",
        f"{_PREFIX}/apis/apps/v1/namespaces/{{namespace}}/deployments",
        f"{_PREFIX}/api/v1/namespaces/{{namespace}}/pods/{{name}}/ephemeralcontainers",
        f"{_PREFIX}/apis/certificates.k8s.io/v1/certificatesigningrequests/{{name}}/approval",
    ],
)
def test_a_credential_call_behind_a_path_prefix_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(f"POST:{op_id}") is True


@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts",
        "PUT:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts/{name}/status",
        "PATCH:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
        f"PUT:{_PREFIX}/apis/apps/v1/namespaces/{{namespace}}/statefulsets/{{name}}/status",
        f"PATCH:{_PREFIX}/api/v1/namespaces/{{name}}/status/",
        "GET:/api/v1/namespaces/{name}",
        "GET:/api/v1/nodes",
        "POST:/api/v1/accounts",
        f"PUT:{_PREFIX}/api/v1/accounts/{{id}}",
    ],
)
def test_reads_subresource_writes_and_other_apis_keep_their_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False


@pytest.mark.parametrize("method", ["post", "Put", "patch", "delete"])
def test_a_write_with_a_lower_case_method_waits(method: str) -> None:
    op_id = f"{method}:/api/v1/namespaces/{{namespace}}/configmaps/{{name}}"
    assert ingested_op_is_credential_call(op_id) is True
    assert credential_grant_reason(op_id) is not None


_OTHER_PRODUCT_WRITE = "POST:/apis/widgets.example.com/v1/namespaces/{namespace}/widgets"


@pytest.mark.asyncio
async def test_a_write_of_another_api_file_with_such_a_path_waits() -> None:
    row = _ingested(_OTHER_PRODUCT_WRITE)
    row.product, row.version, row.impl_id = "widgets", "2.0", "widgets-rest"
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    await _assert_waits_for_every_login(row, {"namespace": "apps", "cluster": "c-1"})
    with pytest.raises(GrantValidationError) as refused:
        await ServicePrincipalGrantService().create(
            _TENANT,
            "op-admin",
            ServiceGrantCreate(
                principal_sub=_SERVICE,
                op_id=_OTHER_PRODUCT_WRITE,
                connector_id="widgets-2.0-widgets-rest",
                reason="unattended deploy",
            ),
            creator_role=TenantRole.TENANT_ADMIN,
        )
    assert str(refused.value) == f"{credential_grant_reason(_OTHER_PRODUCT_WRITE)} Leave it out."


async def _store(op_id: str) -> EndpointDescriptor:
    row = _ingested(op_id)
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _all_writes())
async def test_every_api_write_of_an_object_waits_for_every_login(op_id: str) -> None:
    params = {"namespace": "apps", "name": "a", "cluster": "c-1"}
    await _assert_waits_for_every_login(await _store(op_id), params)


# ---------------------------------------------------------------------------
# Grants, agent grants, run approvals and sensors
# ---------------------------------------------------------------------------

_HELM_CHART_WRITE = "POST:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts"
_WEBHOOK_WRITE = (
    f"PUT:{_PREFIX}/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/{{name}}"
)
_EVICTION = "POST:/api/v1/namespaces/{namespace}/pods/{name}/eviction"
_FINALIZE = "PUT:/api/v1/namespaces/{name}/finalize"
_ADD_ON_ACTION = "PUT:/apis/widgets.example.test/v1/namespaces/{namespace}/widgets/{name}/start"
_ALWAYS_WAITING = [
    "k8s.apply",
    _HELM_CHART_WRITE,
    _WEBHOOK_WRITE,
    "PATCH:/api/v1/namespaces/{name}",
    f"POST:{_PREFIX}/api/v1/persistentvolumes/",
    "PUT:/api/v1/nodes/{name}",
    "PUT:/api/v1/namespaces/{namespace}/services/{name}",
    "DELETE:/api/v1/namespaces/{namespace}/configmaps/{name}",
    _EVICTION,
    _FINALIZE,
    _ADD_ON_ACTION,
    f"POST:{_PREFIX}/apis/widgets.example.test/v1/widgets/{{name}}/restart",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS_WAITING)
async def test_grant_create_refuses_a_call_that_always_waits(op_id: str) -> None:
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
@pytest.mark.parametrize("op_id", _ALWAYS_WAITING)
async def test_agent_grant_create_refuses_a_call_that_always_waits(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _ALWAYS_WAITING)
async def test_run_approval_create_refuses_a_call_that_always_waits(op_id: str) -> None:
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


_SENSOR_CALLS = [
    ("k8s-1.x", "k8s.apply", {"manifest": _manifest("v1", "ConfigMap")}),
    ("kubeapi-1.30", _HELM_CHART_WRITE, {}),
    ("kubeapi-1.30", _WEBHOOK_WRITE, {}),
    ("kubeapi-1.30", _EVICTION, {}),
]
_SENSOR_IDS = ["k8s-apply", "helm-chart-write", "webhook-write", "pod-eviction"]


@pytest.mark.parametrize(("connector_id", "op_id", "params"), _SENSOR_CALLS, ids=_SENSOR_IDS)
def test_a_sensor_of_a_call_that_always_waits_is_refused(
    connector_id: str, op_id: str, params: dict[str, Any]
) -> None:
    payload = SimpleNamespace(connector_id=connector_id, op_id=op_id, params=params)
    with pytest.raises(SensorRequiresNoPersonError):
        _require_read(payload, SimpleNamespace(safety_level="safe"))  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(("connector_id", "op_id", "params"), _SENSOR_CALLS, ids=_SENSOR_IDS)
async def test_a_sensor_of_a_call_that_always_waits_reads_unknown(
    monkeypatch: pytest.MonkeyPatch, connector_id: str, op_id: str, params: dict[str, Any]
) -> None:
    async def _lookup(**_kwargs: Any) -> Any:
        return SimpleNamespace(safety_level="safe")

    monkeypatch.setattr(sensor_runner, "lookup_descriptor", _lookup)
    snap = SimpleNamespace(connector_id=connector_id, op_id=op_id, params=params, tenant_id=_TENANT)
    outcome = await sensor_runner._safe_tier_floor(snap)  # type: ignore[arg-type]
    assert outcome is not None
    assert "op_waits_for_person" in json.dumps(outcome, default=str)


# ---------------------------------------------------------------------------
# The card names every container
# ---------------------------------------------------------------------------


def _no_redaction(entries: Any) -> Any:
    return entries


def test_the_card_names_every_container_when_one_command_line_is_too_long() -> None:
    long_line = "echo " + "x" * (MANIFEST_ECHO_MAX_BYTES + 100)
    containers = [
        {"name": "first", "image": "registry.example.test/a:1", "command": ["sh", "-c", long_line]},
        {"name": "second", "image": "registry.example.test/b:1", "command": ["run", "--fast"]},
        {"name": "third", "image": "registry.example.test/c:1", "args": ["--port", "80"]},
    ]
    manifest = _manifest("v1", "Pod", spec={"containers": containers})

    shown = apply_run_commands(manifest, redact=_no_redaction)

    assert [entry["container"] for entry in shown] == ["first", "second", "third"]
    first, second, third = shown
    assert "command" not in first
    assert first["cut"]
    assert first["command_cut"].startswith('command: ["sh", "-c", "echo xxx')
    assert first["image"] == "registry.example.test/a:1"
    assert second["command"] == ["run", "--fast"]
    assert third["args"] == ["--port", "80"]
    assert len(json.dumps(shown).encode("utf-8")) <= MANIFEST_ECHO_MAX_BYTES


def test_a_cut_entry_hides_secret_values_before_it_is_cut() -> None:
    secret = "made-up" + "-password-9"
    long_line = "x" * (MANIFEST_ECHO_MAX_BYTES + 100)
    containers = [
        {"name": "first", "image": "img", "command": ["login", "--password", secret, long_line]},
    ]
    shown = apply_run_commands(
        _manifest("v1", "Pod", spec={"containers": containers}), redact=_no_redaction
    )
    assert secret not in json.dumps(shown)
    assert shown[0]["cut"]


def test_the_card_counts_the_containers_left_out_when_their_names_do_not_fit() -> None:
    long_name = "n" * 2000
    containers = [
        {"name": f"{long_name}-{i}", "image": "img", "command": ["run"]} for i in range(12)
    ]
    shown = apply_run_commands(
        _manifest("v1", "Pod", spec={"containers": containers}), redact=_no_redaction
    )
    assert shown[-1]["omitted_containers"] == 12 - (len(shown) - 1)
    assert len(json.dumps(shown).encode("utf-8")) <= MANIFEST_ECHO_MAX_BYTES


def test_the_card_names_every_command_of_a_custom_kind() -> None:
    secret = "made-up" + "-token-7"
    doc = _object(
        "argoproj.io/v1alpha1",
        "WorkflowTemplate",
        spec={
            "templates": [
                {"name": "build", "container": {"image": "img-a", "command": ["make", "all"]}},
                {
                    "name": "check",
                    "script": {"image": "img-b", "command": ["python"], "source": "print(1)"},
                },
            ],
            "task": {
                "steps": [
                    {"name": "push", "image": "img-c", "args": ["push", "--token", secret]},
                ],
            },
        },
    )

    shown = apply_run_commands(yaml.safe_dump(doc), redact=_no_redaction)

    lines = {json.dumps(entry.get("command") or entry.get("args")) for entry in shown}
    assert len(shown) == 3
    assert {'["make", "all"]', '["python"]'} <= lines
    (step,) = [entry for entry in shown if entry["container"] == "push"]
    assert step["args"][:2] == ["push", "--token"]
    assert secret not in json.dumps(shown)


def test_the_card_names_a_probe_command_once() -> None:
    probe = {"exec": {"command": ["check", "--ready"]}}
    containers = [{"name": "web", "image": "img", "command": ["serve"], "readinessProbe": probe}]

    shown = apply_run_commands(
        _manifest("v1", "Pod", spec={"containers": containers}), redact=_no_redaction
    )

    assert len(shown) == 1
    assert shown[0]["exec_commands"] == {"readinessProbe": ["check", "--ready"]}
