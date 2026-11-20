# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Kubernetes apply or API write waits for a person unless every object in it is plain.

A plain object is one of ``PLAIN_OBJECTS`` in
``connectors/kubernetes/op_classes.py``, read by API group and kind (or
plural, for a path of an ingested Kubernetes API file). Any other object
waits: a person approves each call, and no standing grant, no agent grant
and no run approval covers it, also not as a step of a multi-step run.
A path of an ingested API file can have any prefix in front.

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
from meho_backplane.connectors.kubernetes.op_classes import PLAIN_OBJECTS, manifest_needs_a_person
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
from meho_backplane.policy.store import connector_id_of
from tests.test_credential_calls_always_wait import (
    _RUN,
    _SERVICE,
    _TENANT,
    _agent,
    _agent_grant,
    _name_in_every_grant,
    _register_all,
    _service,
)
from tests.test_guest_reads_and_pod_applies_wait import (
    _LOGIN_IDS,
    _LOGINS,
    _assert_waits_with_and_without_a_grant,
    _ingested,
    _row,
    _settings_env,
    _verdict,
)

__all__ = ["_settings_env"]

#: Objects that run a pod, give rights, read a Secret or change how the
#: cluster checks requests: ``(apiVersion, kind, plural, namespaced)``. The
#: last one is a made-up custom kind.
_WAITING: list[tuple[str, str, str, bool]] = [
    ("helm.cattle.io/v1", "HelmChart", "helmcharts", True),
    ("helm.cattle.io/v1", "HelmChartConfig", "helmchartconfigs", True),
    ("argoproj.io/v1alpha1", "Workflow", "workflows", True),
    ("argoproj.io/v1alpha1", "CronWorkflow", "cronworkflows", True),
    ("argoproj.io/v1alpha1", "WorkflowTemplate", "workflowtemplates", True),
    ("argoproj.io/v1alpha1", "ClusterWorkflowTemplate", "clusterworkflowtemplates", False),
    ("argoproj.io/v1alpha1", "Application", "applications", True),
    ("argoproj.io/v1alpha1", "ApplicationSet", "applicationsets", True),
    ("argoproj.io/v1alpha1", "AppProject", "appprojects", True),
    ("tekton.dev/v1", "TaskRun", "taskruns", True),
    ("tekton.dev/v1", "PipelineRun", "pipelineruns", True),
    ("tekton.dev/v1", "Task", "tasks", True),
    ("tekton.dev/v1", "Pipeline", "pipelines", True),
    ("serving.knative.dev/v1", "Service", "services", True),
    ("serving.knative.dev/v1", "Configuration", "configurations", True),
    ("helm.toolkit.fluxcd.io/v2", "HelmRelease", "helmreleases", True),
    ("kustomize.toolkit.fluxcd.io/v1", "Kustomization", "kustomizations", True),
    ("keda.sh/v1alpha1", "ScaledJob", "scaledjobs", True),
    (
        "admissionregistration.k8s.io/v1",
        "MutatingWebhookConfiguration",
        "mutatingwebhookconfigurations",
        False,
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingWebhookConfiguration",
        "validatingwebhookconfigurations",
        False,
    ),
    (
        "admissionregistration.k8s.io/v1alpha1",
        "MutatingAdmissionPolicy",
        "mutatingadmissionpolicies",
        False,
    ),
    (
        "admissionregistration.k8s.io/v1alpha1",
        "MutatingAdmissionPolicyBinding",
        "mutatingadmissionpolicybindings",
        False,
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "validatingadmissionpolicies",
        False,
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "validatingadmissionpolicybindings",
        False,
    ),
    ("apiregistration.k8s.io/v1", "APIService", "apiservices", False),
    ("apiextensions.k8s.io/v1", "CustomResourceDefinition", "customresourcedefinitions", False),
    ("certificates.k8s.io/v1", "CertificateSigningRequest", "certificatesigningrequests", False),
    ("cert-manager.io/v1", "Issuer", "issuers", True),
    ("cert-manager.io/v1", "ClusterIssuer", "clusterissuers", False),
    ("widgets.example.test/v1", "Widget", "widgets", True),
]
_WAITING_IDS = [f"{api_version.split('/')[0]}-{kind}" for api_version, kind, _, _ in _WAITING]

_PLAIN_IDS = [f"{group or 'core'}-{kind}" for group, kind, _ in PLAIN_OBJECTS]

#: The plain objects whose write through an ingested API file keeps its rules:
#: all but an Ingress, whose body MEHO does not read there.
_PATH_PLAIN = [entry for entry in PLAIN_OBJECTS if entry[1] != "Ingress"]
_PATH_PLAIN_IDS = [f"{group or 'core'}-{kind}" for group, kind, _ in _PATH_PLAIN]

#: Plain kinds whose object waits because of one of its fields.
_FIELDS_THAT_WAIT: list[tuple[str, str, dict[str, Any]]] = [
    ("v1", "Namespace", {"labels": {"pod-security.kubernetes.io/enforce": "baseline"}}),
    ("v1", "Namespace", {"labels": {"team": "a"}}),
    ("v1", "Namespace", {"annotations": {"note": "a"}}),
    ("networking.k8s.io/v1", "Ingress", {"annotations": {"cert-manager.io/issuer": "a"}}),
    ("networking.k8s.io/v1", "Ingress", {"labels": {"team": "a"}}),
    ("networking.k8s.io/v1", "Ingress", {"spec": {"tls": [{"secretName": "a"}]}}),
]
_FIELDS_THAT_WAIT_IDS = [
    "namespace-pod-security-label",
    "namespace-other-label",
    "namespace-annotation",
    "ingress-issuer-annotation",
    "ingress-label",
    "ingress-tls",
]


def _with_fields(api_version: str, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    doc = _object(api_version, kind)
    for key in ("labels", "annotations"):
        if key in fields:
            doc["metadata"][key] = fields[key]
    if "spec" in fields:
        doc["spec"] = fields["spec"]
    return doc


#: A prefix a proxy puts in front of the Kubernetes API paths.
_PREFIX = "/k8s/clusters/{cluster}"


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


def _api_version(group: str) -> str:
    return f"{group}/v1" if group else "v1"


def _write_path(api_version: str, plural: str, namespaced: bool, *, name: bool = False) -> str:
    base = f"/apis/{api_version}" if "/" in api_version else f"/api/{api_version}"
    path = f"{base}/namespaces/{{namespace}}" if namespaced else base
    path += f"/{plural}"
    return path + "/{name}" if name else path


def _plain_path(group: str, plural: str, *, name: bool = False) -> str:
    return _write_path(_api_version(group), plural, plural != "namespaces", name=name)


# ---------------------------------------------------------------------------
# The manifest check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_version", "kind", "plural", "namespaced"), _WAITING, ids=_WAITING_IDS
)
def test_an_apply_of_an_object_that_is_not_plain_waits(
    api_version: str, kind: str, plural: str, namespaced: bool
) -> None:
    params = {"manifest": _manifest(api_version, kind)}
    assert manifest_needs_a_person(params) is True
    assert call_always_waits("k8s.apply", params) is True


@pytest.mark.parametrize(("group", "kind", "plural"), PLAIN_OBJECTS, ids=_PLAIN_IDS)
def test_an_apply_of_a_plain_object_keeps_its_rules(group: str, kind: str, plural: str) -> None:
    params = {"manifest": _manifest(_api_version(group), kind)}
    assert manifest_needs_a_person(params) is False
    assert (
        manifest_needs_a_person({"manifest": _manifest(_api_version(group), kind.lower())}) is False
    )


@pytest.mark.parametrize(
    ("api_version", "kind", "fields"), _FIELDS_THAT_WAIT, ids=_FIELDS_THAT_WAIT_IDS
)
def test_a_namespace_or_ingress_with_a_field_that_can_change_checks_waits(
    api_version: str, kind: str, fields: dict[str, Any]
) -> None:
    doc = _with_fields(api_version, kind, fields)
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(doc)}) is True
    listed = {"apiVersion": "v1", "kind": "List", "items": [doc]}
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(listed)}) is True
    in_kind_list = {"apiVersion": api_version, "kind": f"{kind}List", "items": [doc]}
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(in_kind_list)}) is True


@pytest.mark.parametrize(
    "doc",
    [
        _object("v1", "Namespace") | {"metadata": "a"},
        _object("networking.k8s.io/v1", "Ingress") | {"metadata": ["a"]},
        _object("networking.k8s.io/v1", "Ingress") | {"spec": "a"},
    ],
    ids=["namespace-metadata-not-a-map", "ingress-metadata-not-a-map", "ingress-spec-not-a-map"],
)
def test_a_namespace_or_ingress_meho_cannot_read_waits(doc: dict[str, Any]) -> None:
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(doc)}) is True


@pytest.mark.parametrize(
    "doc",
    [
        _object("v1", "Namespace") | {"metadata": {"name": "a", "labels": {}}},
        {"apiVersion": "v1", "kind": "Namespace"},
        _object("networking.k8s.io/v1", "Ingress")
        | {"spec": {"tls": [], "rules": [{"host": "a.example.test"}]}},
    ],
    ids=["namespace-empty-labels", "namespace-no-metadata", "ingress-rules-without-tls"],
)
def test_a_bare_namespace_or_ingress_keeps_its_rules(doc: dict[str, Any]) -> None:
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(doc)}) is False


def test_a_knative_service_waits_and_a_core_service_does_not() -> None:
    assert manifest_needs_a_person({"manifest": _manifest("serving.knative.dev/v1", "Service")})
    assert not manifest_needs_a_person({"manifest": _manifest("v1", "Service")})


@pytest.mark.parametrize("kind", ["list", "LIST", "List", "PodList"])
def test_a_list_in_any_case_holding_a_pod_waits(kind: str) -> None:
    listed = {"apiVersion": "v1", "kind": kind, "items": [_object("v1", "Pod")]}
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(listed)}) is True


@pytest.mark.parametrize(
    "doc",
    [
        {"apiVersion": "v1", "kind": "list", "items": [_object("v1", "ConfigMap")]},
        {"apiVersion": "v1", "kind": "ConfigMapList", "items": [_object("v1", "ConfigMap")]},
        {"apiVersion": "v1", "kind": "List", "items": []},
        {"apiVersion": "v1", "kind": "List"},
    ],
    ids=["lower-case-list", "config-map-list", "empty-list", "list-without-items"],
)
def test_a_list_of_plain_objects_keeps_its_rules(doc: dict[str, Any]) -> None:
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(doc)}) is False


@pytest.mark.parametrize(
    "doc",
    [
        _object("v1", "ConfigMap") | {"apiVersion": None},
        _object("v1", "ConfigMap") | {"apiVersion": "v2"},
        _object("v1", "ConfigMap") | {"apiVersion": "/v1"},
        _object("v1", "ConfigMap") | {"apiVersion": "a/b/c"},
        _object("v1", "ConfigMap") | {"apiVersion": 1},
        _object("v1", "ConfigMap") | {"kind": ""},
        _object("v1", "ConfigMap") | {"kind": None},
        _object("Networking.k8s.io/v1", "Ingress"),
        _object("networking.k8s.io/v1", "ConfigMap"),
        _object("v1", "Ingress"),
        _object("widgets.example.test/v1", "WidgetList", items=[]),
        _object("apps/v1", "DeploymentList", items=[]),
        {"apiVersion": "v1", "kind": "List", "items": [{"kind": "ConfigMap"}]},
        {"apiVersion": "v1", "kind": "List", "items": "not-a-list"},
    ],
    ids=[
        "no-api-version",
        "core-group-other-version",
        "empty-group",
        "too-many-parts",
        "api-version-not-text",
        "empty-kind",
        "no-kind",
        "group-not-lower-case",
        "plain-kind-of-another-group",
        "group-kind-in-the-core-group",
        "custom-list-kind",
        "list-of-a-kind-that-waits",
        "item-without-api-version",
        "items-not-a-list",
    ],
)
def test_an_object_meho_cannot_match_to_a_plain_kind_waits(doc: dict[str, Any]) -> None:
    assert manifest_needs_a_person({"manifest": yaml.safe_dump(doc)}) is True


def test_a_second_document_that_is_not_plain_waits() -> None:
    manifest = _manifest("v1", "ConfigMap") + "---\n" + _manifest("helm.cattle.io/v1", "HelmChart")
    assert manifest_needs_a_person({"manifest": manifest}) is True


# ---------------------------------------------------------------------------
# The paths of an ingested Kubernetes API file
# ---------------------------------------------------------------------------


def _waiting_writes() -> list[str]:
    ops = []
    for api_version, _, plural, namespaced in _WAITING:
        for prefix in ("", _PREFIX):
            ops.append(f"POST:{prefix}{_write_path(api_version, plural, namespaced)}")
            ops.append(f"PUT:{prefix}{_write_path(api_version, plural, namespaced, name=True)}")
            ops.append(f"PATCH:{prefix}{_write_path(api_version, plural, namespaced, name=True)}")
    return ops


@pytest.mark.parametrize("op_id", _waiting_writes())
def test_an_api_write_of_an_object_that_is_not_plain_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
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


def _plain_writes() -> list[str]:
    ops = []
    for group, _, plural in _PATH_PLAIN:
        for prefix in ("", _PREFIX):
            ops.append(f"POST:{prefix}{_plain_path(group, plural)}")
            ops.append(f"PUT:{prefix}{_plain_path(group, plural, name=True)}")
            ops.append(f"PATCH:{prefix}{_plain_path(group, plural, name=True)}")
    return ops


@pytest.mark.parametrize("op_id", _plain_writes())
def test_an_api_write_of_a_plain_object_keeps_its_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False


@pytest.mark.parametrize("prefix", ["", _PREFIX], ids=["no-prefix", "prefix"])
def test_an_api_write_of_an_ingress_waits(prefix: str) -> None:
    for op_id in (
        f"POST:{prefix}{_plain_path('networking.k8s.io', 'ingresses')}",
        f"PUT:{prefix}{_plain_path('networking.k8s.io', 'ingresses', name=True)}",
        f"PATCH:{prefix}{_plain_path('networking.k8s.io', 'ingresses', name=True)}",
    ):
        assert ingested_op_is_credential_call(op_id) is True, op_id
        assert credential_grant_reason(op_id) is not None


@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts",
        "DELETE:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts/{name}",
        "PUT:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts/{name}/status",
        "PATCH:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
        f"PUT:{_PREFIX}/apis/apps/v1/namespaces/{{namespace}}/statefulsets/{{name}}/status",
        "PUT:/api/v1/namespaces/{name}/finalize",
        "PATCH:/api/v1/namespaces/{name}/status",
        "PATCH:/api/v1/nodes/{name}",
        "POST:/api/v1/persistentvolumes",
        "PUT:/api/v1/namespaces/{name}",
    ],
)
def test_reads_status_and_scale_writes_and_cluster_wide_core_writes_keep_their_rules(
    op_id: str,
) -> None:
    assert ingested_op_is_credential_call(op_id) is False


# ---------------------------------------------------------------------------
# Every login waits, with and without a grant, and in an approved run
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_version", "kind", "plural", "namespaced"), _WAITING, ids=_WAITING_IDS
)
async def test_a_k8s_apply_of_an_object_that_is_not_plain_waits_for_every_login(
    api_version: str, kind: str, plural: str, namespaced: bool
) -> None:
    rows = await _register_all()
    params = {"manifest": _manifest(api_version, kind)}
    await _assert_waits_with_and_without_a_grant(_row(rows, "k8s.apply"), params)
    for operator in (_service(), _agent()):
        assert await _step_waits(operator, "k8s.apply", params), operator.principal_kind


@pytest.mark.asyncio
@pytest.mark.parametrize(("group", "kind", "plural"), PLAIN_OBJECTS, ids=_PLAIN_IDS)
async def test_a_k8s_apply_of_a_plain_object_keeps_its_grant(
    group: str, kind: str, plural: str
) -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    await _name_in_every_grant([apply_row])
    params = {"manifest": _manifest(_api_version(group), kind)}
    verdict, reason = await _verdict(_service(), apply_row, params)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason
    assert not await _step_waits(_service(), "k8s.apply", params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_version", "kind", "fields"), _FIELDS_THAT_WAIT, ids=_FIELDS_THAT_WAIT_IDS
)
async def test_a_k8s_apply_of_a_namespace_or_ingress_with_such_a_field_waits_for_every_login(
    api_version: str, kind: str, fields: dict[str, Any]
) -> None:
    rows = await _register_all()
    params = {"manifest": yaml.safe_dump(_with_fields(api_version, kind, fields))}
    await _assert_waits_with_and_without_a_grant(_row(rows, "k8s.apply"), params)
    for operator in (_service(), _agent()):
        assert await _step_waits(operator, "k8s.apply", params), operator.principal_kind


@pytest.mark.asyncio
async def test_a_knative_service_waits_and_a_core_service_keeps_its_grant() -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    await _assert_waits_with_and_without_a_grant(
        apply_row, {"manifest": _manifest("serving.knative.dev/v1", "Service")}
    )
    verdict, reason = await _verdict(
        _service(), apply_row, {"manifest": _manifest("v1", "Service")}
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", _LOGINS, ids=_LOGIN_IDS)
async def test_a_lower_case_list_with_a_pod_waits_for_every_login(operator: Operator) -> None:
    rows = await _register_all()
    listed = {"apiVersion": "v1", "kind": "list", "items": [_object("v1", "Pod")]}
    verdict, reason = await _verdict(
        operator, _row(rows, "k8s.apply"), {"manifest": yaml.safe_dump(listed)}
    )
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason


async def _store(op_id: str) -> EndpointDescriptor:
    row = _ingested(op_id)
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", _PREFIX], ids=["no-prefix", "prefix"])
@pytest.mark.parametrize(
    ("api_version", "kind", "plural", "namespaced"), _WAITING, ids=_WAITING_IDS
)
async def test_an_api_write_of_an_object_that_is_not_plain_waits_for_every_login(
    api_version: str, kind: str, plural: str, namespaced: bool, prefix: str
) -> None:
    op_id = f"POST:{prefix}{_write_path(api_version, plural, namespaced)}"
    row = await _store(op_id)
    params = {"namespace": "apps", "cluster": "c-1"}
    await _assert_waits_with_and_without_a_grant(row, params)
    for operator in (_service(), _agent()):
        assert await _step_waits(operator, op_id, params), operator.principal_kind


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", _PREFIX], ids=["no-prefix", "prefix"])
async def test_an_api_write_of_an_ingress_waits_for_every_login(prefix: str) -> None:
    op_id = f"POST:{prefix}{_plain_path('networking.k8s.io', 'ingresses')}"
    row = await _store(op_id)
    params = {"namespace": "apps", "cluster": "c-1"}
    await _assert_waits_with_and_without_a_grant(row, params)
    for operator in (_service(), _agent()):
        assert await _step_waits(operator, op_id, params), operator.principal_kind


@pytest.mark.asyncio
@pytest.mark.parametrize(("group", "kind", "plural"), _PATH_PLAIN, ids=_PATH_PLAIN_IDS)
async def test_an_api_write_of_a_plain_object_keeps_its_grant(
    group: str, kind: str, plural: str
) -> None:
    row = await _store(f"POST:{_plain_path(group, plural)}")
    await _name_in_every_grant([row])
    verdict, reason = await _verdict(_service(), row, {"namespace": "apps"})
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


# ---------------------------------------------------------------------------
# Grants, agent grants, run approvals and sensors
# ---------------------------------------------------------------------------

_HELM_CHART_WRITE = "POST:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts"
_WEBHOOK_WRITE = (
    f"PUT:{_PREFIX}/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/{{name}}"
)
_WAITING_WRITES = [_HELM_CHART_WRITE, _WEBHOOK_WRITE]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _WAITING_WRITES)
async def test_grant_create_refuses_an_api_write_that_waits(op_id: str) -> None:
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
@pytest.mark.parametrize("op_id", _WAITING_WRITES)
async def test_agent_grant_create_refuses_an_api_write_that_waits(op_id: str) -> None:
    with pytest.raises(AgentGrantValidationError) as refused:
        await AgentGrantService().grant(
            _TENANT, "op-admin", _agent_grant(op_id, PermissionVerdict.AUTO_EXECUTE)
        )
    assert refused.value.message == f"{credential_grant_reason(op_id)} Leave it out."


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _WAITING_WRITES)
async def test_run_approval_create_refuses_an_api_write_that_waits(op_id: str) -> None:
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


@pytest.mark.asyncio
async def test_grant_create_still_takes_k8s_apply() -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    created = await ServicePrincipalGrantService().create(
        _TENANT,
        "op-admin",
        ServiceGrantCreate(
            principal_sub=_SERVICE,
            op_id="k8s.apply",
            connector_id=connector_id_of(apply_row),
            reason="unattended deploy",
        ),
        creator_role=TenantRole.TENANT_ADMIN,
    )
    assert created.op_id == "k8s.apply"


def test_a_sensor_of_an_api_write_that_waits_is_refused() -> None:
    payload = SimpleNamespace(connector_id="kubeapi-1.30", op_id=_HELM_CHART_WRITE, params={})
    with pytest.raises(SensorRequiresNoPersonError):
        _require_read(payload, SimpleNamespace(safety_level="safe"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_sensor_of_an_api_write_that_waits_reads_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lookup(**_kwargs: Any) -> Any:
        return SimpleNamespace(safety_level="safe")

    monkeypatch.setattr(sensor_runner, "lookup_descriptor", _lookup)
    snap = SimpleNamespace(
        connector_id="kubeapi-1.30", op_id=_WEBHOOK_WRITE, params={}, tenant_id=_TENANT
    )
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
