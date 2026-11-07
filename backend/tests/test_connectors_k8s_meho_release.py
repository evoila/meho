# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``k8s.meho.release.info``: what a MEHO install runs today.

The Kubernetes API is a set of fake API classes that hand back real
``kubernetes_asyncio`` model objects. The HTTP calls to ``/version`` and
``/ready`` go through ``httpx.MockTransport``. One test runs the op against
a small ``aiohttp`` server through the real, instrumented ``ApiClient`` with
trace capture on, to prove the trace holds no values text and no Secret
data.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import Token
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from aiohttp import web
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.client.models import (
    V1Container,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentList,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1LabelSelector,
    V1ListMeta,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
    V1Secret,
    V1SecretList,
)

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
)
from meho_backplane.connectors.kubernetes._wire_span import instrument_api_client
from meho_backplane.connectors.kubernetes.ops_meho_release import parse_image, values_hash
from meho_backplane.connectors.kubernetes.redaction import REDACTED_PREFIX
from meho_backplane.flight_recorder import capture
from meho_backplane.settings import get_settings

_MOD = "meho_backplane.connectors.kubernetes.ops_meho_release"
_DIGEST = "sha256:" + "ab" * 32
_POD_DIGEST = "sha256:" + "cd" * 32
_OTHER_POD_DIGEST = "sha256:" + "ef" * 32
_SIDECAR_DIGEST = "sha256:" + "12" * 32
_INSTANCE = "app.kubernetes.io/instance"
#: Made-up values. None of them may appear in a result or a trace.
_SECRET_BLOB = "made-up-helm-release-blob-7731"
_VALUES_TEXT = "adminPassword: made-up-values-text-5521"
_SET_VALUE = "made-up-set-value-9913"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(name="k8s-unit", host="k8s-unit.test.invalid", port=6443, secret_ref="k8s/x")


def _make_connector() -> KubernetesConnector:
    async def _loader(_target: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.test:6443"}}],
            "users": [{"name": "u1", "user": {"token": "stub-token"}}],
        }

    return KubernetesConnector(kubeconfig_loader=_loader)


def _operator() -> Operator:
    return Operator(
        sub="op-release-test",
        name="Release Test Operator",
        email=None,
        raw_jwt="op.release.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000005916"),
        tenant_role=TenantRole.OPERATOR,
    )


def _deployment(
    image: str = f"registry.example/meho:1.4.0@{_DIGEST}",
    *,
    name: str = "meho",
    release: str = "meho",
    container: str = "backplane",
) -> V1Deployment:
    """A Deployment as the chart renders it: instance label, ``backplane`` container."""
    labels = {"app.kubernetes.io/name": "meho", _INSTANCE: release}
    return V1Deployment(
        metadata=V1ObjectMeta(name=name, labels=labels),
        spec=V1DeploymentSpec(
            selector=V1LabelSelector(match_labels=labels),
            template=V1PodTemplateSpec(
                spec=V1PodSpec(
                    containers=[
                        V1Container(name="sidecar", image="registry.example/side:1"),
                        V1Container(name=container, image=image),
                    ]
                )
            ),
        ),
        status=V1DeploymentStatus(replicas=1, ready_replicas=1),
    )


def _pod(
    image: str,
    digest: str | None,
    *,
    sidecar_digest: str | None = _SIDECAR_DIGEST,
    container: str = "backplane",
) -> V1Pod:
    """A pod of the Deployment. The sidecar status comes first on purpose."""
    statuses = [
        V1ContainerStatus(
            name="sidecar",
            image="registry.example/side:1",
            image_id=f"registry.example/side@{sidecar_digest}" if sidecar_digest else "",
            ready=True,
            restart_count=0,
        ),
        V1ContainerStatus(
            name=container,
            image=image,
            image_id=f"registry.example/meho@{digest}" if digest else "",
            ready=True,
            restart_count=0,
        ),
    ]
    return V1Pod(
        spec=V1PodSpec(
            containers=[
                V1Container(name="sidecar", image="registry.example/side:1"),
                V1Container(name=container, image=image),
            ]
        ),
        status=V1PodStatus(container_statuses=statuses),
    )


def _helm_secret(revision: int, status: str) -> V1Secret:
    return V1Secret(
        metadata=V1ObjectMeta(
            name=f"sh.helm.release.v1.meho.v{revision}",
            labels={
                "owner": "helm",
                "name": "meho",
                "status": status,
                "version": str(revision),
                "modifiedAt": "1700000000",
            },
        ),
        data={"release": _SECRET_BLOB},
        type="helm.sh/release.v1",
    )


def _helm_chart_obj(
    *, name: str = "meho", namespace: str = "meho", target_namespace: str | None = None
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "chart": "https://user:pw@charts.example/meho-1.4.0.tgz",
        "version": "1.4.0",
        "valuesContent": _VALUES_TEXT,
        "set": {"image.tag": "1.4.0", "adminPassword": _SET_VALUE},
    }
    if target_namespace is not None:
        spec["targetNamespace"] = target_namespace
    return {
        "apiVersion": "helm.cattle.io/v1",
        "kind": "HelmChart",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def _chart_list(*charts: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "HelmChartList", "metadata": {}, "items": list(charts)}


@dataclass
class _Cluster:
    """What the fake API answers. An ``Exception`` value is raised."""

    deployments: list[V1Deployment] | Exception = field(default_factory=lambda: [_deployment()])
    chart: dict[str, Any] | Exception = field(default_factory=_helm_chart_obj)
    cluster_charts: dict[str, Any] | Exception = field(default_factory=_chart_list)
    secret_pages: list[V1SecretList] | Exception = field(
        default_factory=lambda: [
            V1SecretList(
                items=[_helm_secret(2, "deployed"), _helm_secret(1, "superseded")],
                metadata=V1ListMeta(),
            )
        ]
    )
    pods: V1PodList | Exception = field(default_factory=lambda: V1PodList(items=[]))


def _raise_or(value: Any) -> AsyncMock:
    if isinstance(value, Exception):
        return AsyncMock(side_effect=value)
    return AsyncMock(return_value=value)


async def _run(
    cluster: _Cluster,
    params: dict[str, Any] | None = None,
    *,
    http: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[dict[str, Any], _Fakes]:
    apps, core, custom = MagicMock(), MagicMock(), MagicMock()
    deployments = cluster.deployments
    apps.list_namespaced_deployment = _raise_or(
        V1DeploymentList(items=deployments, metadata=V1ListMeta())
        if isinstance(deployments, list)
        else deployments
    )
    custom.get_namespaced_custom_object = _raise_or(cluster.chart)
    custom.list_cluster_custom_object = _raise_or(cluster.cluster_charts)
    pages = cluster.secret_pages
    core.list_namespaced_secret = (
        AsyncMock(side_effect=pages) if isinstance(pages, list) else _raise_or(pages)
    )
    core.list_namespaced_pod = _raise_or(cluster.pods)
    stack = [
        patch(
            "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
            new_callable=AsyncMock,
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(f"{_MOD}.client.AppsV1Api", return_value=apps),
        patch(f"{_MOD}.client.CoreV1Api", return_value=core),
        patch(f"{_MOD}.client.CustomObjectsApi", return_value=custom),
    ]
    if http is not None:
        stack.append(
            patch(f"{_MOD}.build_pinned_async_transport", return_value=httpx.MockTransport(http))
        )
    for p in stack:
        p.start()
    try:
        result = await _make_connector().k8s_meho_release_info(
            _operator(), _TARGET, params or {"namespace": "meho"}
        )
    finally:
        for p in reversed(stack):
            p.stop()
    return result, _Fakes(apps=apps, core=core, custom=custom)


@dataclass
class _Fakes:
    apps: MagicMock
    core: MagicMock
    custom: MagicMock


def _api_error(status: int) -> ApiException:
    return ApiException(status=status, reason="made-up reason with token abc123")


# ---------------------------------------------------------------------------
# Registration and pure helpers
# ---------------------------------------------------------------------------


def test_registered_with_the_caller_address_floor() -> None:
    op = next(o for o in KUBERNETES_OPS if o.op_id == "k8s.meho.release.info")
    assert op.safety_level == "caution"
    assert op.requires_approval is True
    assert op.parameter_schema["properties"]["host"]["x-meho-caller-address"] is True
    assert "read-only" in op.tags
    assert op.handler_attr == "k8s_meho_release_info"
    assert op.parameter_schema["required"] == ["namespace"]
    assert op.llm_instructions is not None


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("meho:1.4", ("meho", "1.4", None)),
        (
            "reg.example:5000/meho/meho:1.4@sha256:x",
            ("reg.example:5000/meho/meho", "1.4", "sha256:x"),
        ),
        ("reg.example:5000/meho", ("reg.example:5000/meho", None, None)),
        ("meho@sha256:x", ("meho", None, "sha256:x")),
    ],
)
def test_parse_image(image: str, expected: tuple[str, str | None, str | None]) -> None:
    parsed = parse_image(image)
    assert (parsed["repository"], parsed["tag"], parsed["digest"]) == expected


def test_values_hash_is_stable_and_changes_with_values() -> None:
    a = values_hash({"valuesContent": "a: 1", "set": {"x": "1"}})
    assert a == values_hash({"set": {"x": "1"}, "valuesContent": "a: 1"})
    assert a != values_hash({"valuesContent": "a: 2", "set": {"x": "1"}})
    assert values_hash({}) != a


# ---------------------------------------------------------------------------
# Kubernetes parts
# ---------------------------------------------------------------------------


async def test_full_install_reports_every_part_without_secret_data() -> None:
    result, fakes = await _run(_Cluster())
    core = fakes.core
    assert result["deployment"] == {
        "state": "present",
        "name": "meho",
        "replicas": 1,
        "ready_replicas": 1,
        "image": f"registry.example/meho:1.4.0@{_DIGEST}",
        "tag": "1.4.0",
        "digest": _DIGEST,
    }
    assert fakes.apps.list_namespaced_deployment.await_args.kwargs["label_selector"] == (
        f"{_INSTANCE}=meho"
    )
    chart = result["helm_chart"]
    assert chart["state"] == "present"
    assert chart["version"] == "1.4.0"
    assert chart["chart_namespace"] == "meho"
    assert chart["chart"] == "https://charts.example/meho-1.4.0.tgz"
    assert chart["values_hash"] == values_hash(_helm_chart_obj()["spec"])
    fakes.custom.list_cluster_custom_object.assert_not_awaited()
    releases = result["helm_releases"]
    assert [(r["revision"], r["status"]) for r in releases["revisions"]] == [
        (1, "superseded"),
        (2, "deployed"),
    ]
    assert releases["incomplete"] is False
    assert "meho" not in result
    core.list_namespaced_secret.assert_awaited_once()
    assert core.list_namespaced_secret.await_args.kwargs["label_selector"] == "owner=helm,name=meho"
    text = json.dumps(result)
    for forbidden in (_SECRET_BLOB, _VALUES_TEXT, _SET_VALUE, "made-up-values-text", "user:pw"):
        assert forbidden not in text


async def test_everything_missing_is_not_present_and_does_not_fail() -> None:
    cluster = _Cluster(
        deployments=[],
        chart=_api_error(404),
        cluster_charts=_api_error(404),
        secret_pages=[V1SecretList(items=[], metadata=V1ListMeta())],
    )
    result, _ = await _run(cluster)
    assert result["deployment"] == {"state": "not present"}
    assert result["helm_chart"] == {"state": "not present"}
    assert result["helm_releases"] == {"state": "not present"}


async def test_failed_reads_are_unknown_not_missing() -> None:
    cluster = _Cluster(
        deployments=_api_error(403),
        chart=_api_error(500),
        secret_pages=TimeoutError("took too long"),
    )
    result, _ = await _run(cluster)
    assert result["deployment"] == {"state": "unknown", "error": "HTTP 403"}
    assert result["helm_chart"] == {"state": "unknown", "error": "HTTP 500"}
    assert result["helm_releases"] == {"state": "unknown", "error": "TimeoutError"}
    assert "abc123" not in json.dumps(result)


@pytest.mark.parametrize("which", ["deployments", "chart", "secret_pages"])
async def test_401_reaches_the_dispatcher(which: str) -> None:
    with pytest.raises(ApiException) as caught:
        await _run(_Cluster(**{which: _api_error(401)}))
    assert caught.value.status == 401


async def test_tag_only_image_takes_the_digest_from_the_running_pod() -> None:
    image = "registry.example/meho:1.4.0"
    cluster = _Cluster(
        deployments=[_deployment(image)], pods=V1PodList(items=[_pod(image, _POD_DIGEST)])
    )
    result, fakes = await _run(cluster)
    assert result["deployment"]["digest"] == _POD_DIGEST
    assert "pod_digests" not in result["deployment"]
    assert fakes.core.list_namespaced_pod.await_args.kwargs["label_selector"] == (
        f"{_INSTANCE}=meho,app.kubernetes.io/name=meho"
    )


async def test_sidecar_digest_is_never_used() -> None:
    """The sidecar status comes first and has a digest; the backplane one has none."""
    image = "registry.example/meho:1.4.0"
    cluster = _Cluster(deployments=[_deployment(image)], pods=V1PodList(items=[_pod(image, None)]))
    result, _ = await _run(cluster)
    assert result["deployment"]["digest"] is None
    assert _SIDECAR_DIGEST not in json.dumps(result)


async def test_pod_of_an_older_rollout_is_skipped() -> None:
    """During a rollout an old pod runs the previous tag; its digest is not the new one."""
    image = "registry.example/meho:1.4.0"
    pods = [_pod("registry.example/meho:1.3.0", _OTHER_POD_DIGEST), _pod(image, _POD_DIGEST)]
    cluster = _Cluster(deployments=[_deployment(image)], pods=V1PodList(items=pods))
    result, _ = await _run(cluster)
    assert result["deployment"]["digest"] == _POD_DIGEST
    assert _OTHER_POD_DIGEST not in json.dumps(result)


async def test_two_builds_of_one_tag_are_reported_as_mixed() -> None:
    """Two pods run the same tag but not the same build: no digest, both listed."""
    image = "registry.example/meho:latest"
    pods = [_pod(image, _OTHER_POD_DIGEST), _pod(image, _POD_DIGEST)]
    cluster = _Cluster(deployments=[_deployment(image)], pods=V1PodList(items=pods))
    result, _ = await _run(cluster)
    assert result["deployment"]["digest"] is None
    assert result["deployment"]["pod_digests"] == sorted([_POD_DIGEST, _OTHER_POD_DIGEST])


async def test_digest_stays_null_when_pods_cannot_be_read() -> None:
    cluster = _Cluster(
        deployments=[_deployment("registry.example/meho:1.4.0")], pods=_api_error(403)
    )
    result, _ = await _run(cluster)
    assert result["deployment"]["state"] == "present"
    assert result["deployment"]["digest"] is None
    assert result["deployment"]["digest_error"] == "HTTP 403"


async def test_pod_read_401_reaches_the_dispatcher() -> None:
    cluster = _Cluster(
        deployments=[_deployment("registry.example/meho:1.4.0")], pods=_api_error(401)
    )
    with pytest.raises(ApiException):
        await _run(cluster)


# The chart names the Deployment after the release (``_helpers.tpl``,
# ``meho.fullname``): the release name when it contains the chart name,
# else ``<release>-meho``, or ``fullnameOverride``. The op never uses the
# name: it finds the Deployment by the instance label and its container.
@pytest.mark.parametrize(
    ("release", "deployment_name"),
    [("meho", "meho"), ("demo", "demo-meho"), ("meho-prod", "meho-prod"), ("x", "custom-name")],
)
async def test_deployment_is_found_by_release_label_not_by_name(
    release: str, deployment_name: str
) -> None:
    keycloak = _deployment(
        "registry.example/keycloak:26", name=f"{release}-core-keycloak", release=release
    )
    keycloak.spec.template.spec.containers = [
        V1Container(name="keycloak", image="registry.example/keycloak:26")
    ]
    backplane = _deployment(name=deployment_name, release=release)
    cluster = _Cluster(deployments=[keycloak, backplane])
    result, fakes = await _run(cluster, {"namespace": "ns1", "release": release})
    assert result["deployment"]["name"] == deployment_name
    assert result["deployment"]["tag"] == "1.4.0"
    assert fakes.apps.list_namespaced_deployment.await_args.kwargs["label_selector"] == (
        f"{_INSTANCE}={release}"
    )


async def test_release_objects_without_a_backplane_container_are_not_present() -> None:
    keycloak = _deployment("registry.example/keycloak:26", name="demo-core-keycloak")
    keycloak.spec.template.spec.containers = [
        V1Container(name="keycloak", image="registry.example/keycloak:26")
    ]
    result, _ = await _run(_Cluster(deployments=[keycloak]))
    assert result["deployment"] == {"state": "not present"}


async def test_several_backplane_deployments_are_unknown_with_candidates() -> None:
    cluster = _Cluster(deployments=[_deployment(name="meho-b"), _deployment(name="meho-a")])
    result, _ = await _run(cluster)
    assert result["deployment"] == {
        "state": "unknown",
        "error": "several Deployments match",
        "candidates": ["meho-a", "meho-b"],
    }


async def test_helm_chart_in_another_namespace_is_found_by_target_namespace() -> None:
    """The RKE2 layout: the HelmChart sits in kube-system and installs into meho."""
    other = _helm_chart_obj(namespace="kube-system", target_namespace="other")
    wanted = _helm_chart_obj(namespace="kube-system", target_namespace="meho")
    cluster = _Cluster(chart=_api_error(404), cluster_charts=_chart_list(other, wanted))
    result, fakes = await _run(cluster)
    chart = result["helm_chart"]
    assert chart["state"] == "present"
    assert chart["chart_namespace"] == "kube-system"
    assert chart["version"] == "1.4.0"
    assert chart["values_hash"] == values_hash(wanted["spec"])
    assert fakes.custom.list_cluster_custom_object.await_args.args == (
        "helm.cattle.io",
        "v1",
        "helmcharts",
    )
    assert _VALUES_TEXT not in json.dumps(result)


async def test_helm_chart_that_targets_another_namespace_is_not_this_install() -> None:
    local = _helm_chart_obj(namespace="meho", target_namespace="other")
    cluster = _Cluster(chart=local, cluster_charts=_chart_list(local))
    result, _ = await _run(cluster)
    assert result["helm_chart"] == {"state": "not present", "incomplete": False}


async def test_helm_chart_cluster_list_forbidden_is_unknown() -> None:
    cluster = _Cluster(chart=_api_error(404), cluster_charts=_api_error(403))
    result, _ = await _run(cluster)
    assert result["helm_chart"] == {"state": "unknown", "error": "HTTP 403"}


async def test_helm_secrets_follow_every_page_and_flag_a_reached_cap() -> None:
    pages = [
        V1SecretList(items=[_helm_secret(1, "superseded")], metadata=V1ListMeta(_continue="t1")),
        V1SecretList(items=[_helm_secret(2, "deployed")], metadata=V1ListMeta()),
    ]
    result, fakes = await _run(_Cluster(secret_pages=pages))
    assert [r["revision"] for r in result["helm_releases"]["revisions"]] == [1, 2]
    assert fakes.core.list_namespaced_secret.await_args_list[1].kwargs["_continue"] == "t1"

    endless = [
        V1SecretList(items=[_helm_secret(i, "superseded")], metadata=V1ListMeta(_continue="t"))
        for i in range(10)
    ]
    result, _ = await _run(_Cluster(secret_pages=endless))
    assert result["helm_releases"]["incomplete"] is True
    assert len(result["helm_releases"]["revisions"]) == 10


async def test_release_param_changes_every_lookup() -> None:
    cluster = _Cluster(deployments=[])
    result, fakes = await _run(cluster, {"namespace": "ns1", "release": "other"})
    assert result["release"] == "other"
    assert fakes.core.list_namespaced_secret.await_args.kwargs["label_selector"] == (
        "owner=helm,name=other"
    )
    assert fakes.custom.get_namespaced_custom_object.await_args.kwargs["name"] == "other"


# ---------------------------------------------------------------------------
# /version and /ready
# ---------------------------------------------------------------------------


def _http(ready_status: int, ready_body: Any, version_body: Any | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(
                200,
                json=version_body
                or {
                    "git_sha": "abc1234",
                    "build_date": "2026-11-01",
                    "chart_version": "1.4.0",
                    "connector_packages": [],
                    "extra": "dropped",
                },
            )
        return httpx.Response(ready_status, json=ready_body)

    return handler


_PARAMS = {"namespace": "meho", "host": "meho.example.test"}


async def test_host_healthy_ready_and_version() -> None:
    body = {"status": "ready", "checks": [{"name": "db", "ok": True, "detail": "revision=r1"}]}
    result, _ = await _run(_Cluster(), _PARAMS, http=_http(200, body))
    meho = result["meho"]
    assert meho["host"] == "meho.example.test"
    assert meho["version"] == {
        "state": "present",
        "git_sha": "abc1234",
        "build_date": "2026-11-01",
        "chart_version": "1.4.0",
        "connector_packages": [],
    }
    assert meho["ready"]["ready"] is True
    assert meho["ready"]["db_ahead"] is False
    assert meho["ready"]["checks"] == [{"name": "db", "ok": True}]


async def test_host_reports_db_ahead() -> None:
    body = {
        "status": "ready",
        "checks": [{"name": "db", "ok": True, "detail": "current=r9 head=r8 db_ahead=true"}],
    }
    result, _ = await _run(_Cluster(), _PARAMS, http=_http(200, body))
    assert result["meho"]["ready"]["db_ahead"] is True
    assert result["meho"]["ready"]["ready"] is True


async def test_host_not_ready() -> None:
    body = {"status": "not_ready", "checks": [{"name": "db", "ok": False, "detail": "x"}]}
    result, _ = await _run(_Cluster(), _PARAMS, http=_http(503, body))
    ready = result["meho"]["ready"]
    assert ready["ready"] is False
    assert ready["http_status"] == 503
    assert ready["status"] == "not_ready"


async def test_host_not_reachable_is_unknown() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("made-up connect failure")

    result, _ = await _run(_Cluster(), _PARAMS, http=refuse)
    assert result["meho"]["ready"] == {"state": "unknown", "error": "ConnectError"}
    assert result["meho"]["version"] == {"state": "unknown", "error": "ConnectError"}
    assert result["deployment"]["state"] == "present"


async def test_host_with_odd_answers_is_unknown() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text="not json")

    result, _ = await _run(_Cluster(), _PARAMS, http=odd)
    assert result["meho"]["version"] == {"state": "unknown", "error": "HTTP 502"}
    assert result["meho"]["ready"] == {"state": "unknown", "error": "HTTP 200"}


async def test_blocked_address_is_unknown_with_the_real_guard() -> None:
    result, _ = await _run(_Cluster(), {"namespace": "meho", "host": "127.0.0.1"})
    assert result["meho"]["ready"]["state"] == "unknown"
    assert result["meho"]["version"]["state"] == "unknown"


@pytest.mark.parametrize("host", ["a/b", "u:p@h", "h?x=1", "h #", "../x", "h:99999x", ""])
async def test_bad_host_is_refused(host: str) -> None:
    with pytest.raises(ValueError, match="host must be"):
        await _run(_Cluster(), {"namespace": "meho", "host": host})


# ---------------------------------------------------------------------------
# Trace capture: the recorded API answers hold no values text and no Secret data
# ---------------------------------------------------------------------------


def _op_context() -> capture._OpContext:
    op = next(o for o in KUBERNETES_OPS if o.op_id == "k8s.meho.release.info")
    return capture._OpContext(
        op_id=op.op_id,
        connector_id="kubernetes",
        impl_id="kubernetes-1",
        product="kubernetes",
        tags=tuple(op.tags),
        body_paths=(),
        delete_shaped_patterns=(),
    )


@pytest.fixture
def scope() -> Iterator[capture._CaptureScope]:
    """Bind an active capture scope and the op context, as the dispatcher does."""
    sc = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    cap_token: Token[Any] = capture._active_capture_var.set(sc)
    op_token: Token[Any] = capture._op_context_var.set(_op_context())
    try:
        yield sc
    finally:
        capture._op_context_var.reset(op_token)
        capture._active_capture_var.reset(cap_token)


@pytest.fixture
async def api_server() -> AsyncIterator[client.ApiClient]:
    """A small Kubernetes API: the RKE2 layout with the chart in kube-system."""
    headers = {"Audit-Id": "audit-release"}
    api = client.ApiClient(configuration=client.Configuration(host="http://127.0.0.1"))
    to_json = api.sanitize_for_serialization

    async def deployments(_request: web.Request) -> web.Response:
        body = to_json(V1DeploymentList(items=[_deployment()], metadata=V1ListMeta()))
        return web.json_response({**body, "kind": "DeploymentList"}, headers=headers)

    async def chart_missing(_request: web.Request) -> web.Response:
        status = {"kind": "Status", "code": 404, "reason": "NotFound"}
        return web.json_response(status, status=404, headers=headers)

    async def charts(_request: web.Request) -> web.Response:
        wanted = _helm_chart_obj(namespace="kube-system", target_namespace="meho")
        return web.json_response(_chart_list(wanted), headers=headers)

    async def secrets(_request: web.Request) -> web.Response:
        body = to_json(V1SecretList(items=[_helm_secret(1, "deployed")], metadata=V1ListMeta()))
        return web.json_response({**body, "kind": "SecretList"}, headers=headers)

    app = web.Application()
    app.router.add_get("/apis/apps/v1/namespaces/meho/deployments", deployments)
    app.router.add_get("/apis/helm.cattle.io/v1/namespaces/meho/helmcharts/meho", chart_missing)
    app.router.add_get("/apis/helm.cattle.io/v1/helmcharts", charts)
    app.router.add_get("/api/v1/namespaces/meho/secrets", secrets)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    await api.close()
    api = instrument_api_client(
        client.ApiClient(configuration=client.Configuration(host=f"http://127.0.0.1:{port}"))
    )
    try:
        yield api
    finally:
        await api.close()
        await runner.cleanup()


async def test_trace_capture_records_no_values_text_and_no_secret_data(
    api_server: client.ApiClient, scope: capture._CaptureScope
) -> None:
    with patch.object(KubernetesConnector, "_get_api_client", AsyncMock(return_value=api_server)):
        result = await _make_connector().k8s_meho_release_info(
            _operator(), _TARGET, {"namespace": "meho"}
        )
    expected_spec = _helm_chart_obj(namespace="kube-system", target_namespace="meho")["spec"]
    assert result["deployment"]["digest"] == _DIGEST
    assert result["helm_chart"]["chart_namespace"] == "kube-system"
    assert result["helm_chart"]["values_hash"] == values_hash(expected_spec)
    assert [r["revision"] for r in result["helm_releases"]["revisions"]] == [1]

    spans = [dict(span.attributes) for span in scope.spans]
    assert [span.status for span in scope.spans] == ["200", "404", "200", "200"]
    chart_span = spans[2]
    recorded_spec = chart_span["response_body"]["items"][0]["spec"]
    assert recorded_spec["valuesContent"].startswith(REDACTED_PREFIX)
    assert recorded_spec["set"]["adminPassword"].startswith(REDACTED_PREFIX)
    assert recorded_spec["version"] == "1.4.0"
    assert spans[3]["response_body"]["items"][0]["data"]["release"].startswith(REDACTED_PREFIX)
    dumped = json.dumps(spans) + json.dumps(result)
    for forbidden in (_VALUES_TEXT, _SET_VALUE, _SECRET_BLOB, "made-up-values-text"):
        assert forbidden not in dumped
    assert scope.redaction_uncertain is False
