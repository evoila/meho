# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reads inside a guest, pod and rights applies, and Kubernetes API calls wait for a person.

These calls get the rule of a credential call: a person approves each call,
and no standing grant, no agent grant and no run approval covers it.

* ``proxmox.api.get`` of a path that reads inside a guest (a guest agent
  read such as ``file-read`` or ``exec-status``, the cloud-init data, a file
  from a backup), opens a console, or returns a stored secret. The plain
  reads (nodes, VM status and config, storage, tasks) run at once.
* ``k8s.apply`` of every kind of object.
* An operation an ingest makes from a Kubernetes API file that reads or
  writes a Secret, runs a command in a pod, opens a proxy, makes a token,
  or writes or deletes an object of any kind.

Each call waits for a person's own call, an agent login and a service
login, with and without a standing grant. The approval card of a pod
apply names what each container runs, for a person only. A sensor never
runs such a call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from meho_backplane.api.v1.approvals import _proposed_effect_for
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.service import SensorRequiresNoPersonError, _require_read
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes.ingested_paths import ingested_op_is_credential_call
from meho_backplane.connectors.kubernetes.op_classes import manifest_holds_credential
from meho_backplane.connectors.kubernetes.ops_write_dangerous import ApplyRejectedError
from meho_backplane.connectors.kubernetes.ops_write_preview import RUN_COMMANDS_KEY
from meho_backplane.connectors.proxmox.op_classes import path_reads_guest_or_secret
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.op_classes import call_always_waits, is_credential_capable
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.service_grants import credential_grant_reason
from meho_backplane.operations.wait_when_set import raise_descriptor_for_params
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import (
    _TENANT,
    _agent,
    _name_in_every_grant,
    _register_all,
    _service,
)

_RUN = "run-credential-calls"

# A made-up value, built from parts so no scanner reads it as a real one.
_TOKEN = "made-up" + "-token-value-7"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _person() -> Operator:
    return Operator(
        sub="op:caller",
        name="Caller",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


_LOGINS = [_person(), _agent(), _service()]
_LOGIN_IDS = ["person", "agent", "service"]


def _manifest(kind: str, api_version: str = "v1", **body: Any) -> str:
    doc = {"apiVersion": api_version, "kind": kind, "metadata": {"name": "a", "namespace": "apps"}}
    doc.update(body)
    return yaml.safe_dump(doc)


_POD_SPEC = {
    "containers": [
        {
            "name": "web",
            "image": "registry.example.test/web:1",
            "command": ["sh", "-c", "serve"],
            "args": ["--token", _TOKEN],
        }
    ]
}
_DEPLOYMENT = _manifest(
    "Deployment", "apps/v1", spec={"template": {"metadata": {}, "spec": _POD_SPEC}}
)
_CONFIG_MAP = _manifest("ConfigMap", data={"mode": "fast"})
_SERVICE_ACCOUNT = _manifest("ServiceAccount")


# ---------------------------------------------------------------------------
# proxmox.api.get: which paths wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "nodes/pve1/qemu/100/agent/file-read",
        "nodes/pve1/qemu/100/agent/exec-status",
        "nodes/pve1/qemu/100/agent/some-new-read",
        "nodes/pve1/qemu/100/agent/get-osinfo/extra",
        "nodes/pve1/qemu/100/cloudinit",
        "nodes/pve1/qemu/100/cloudinit/dump",
        "nodes/pve1/storage/backup/file-restore/download",
        "nodes/pve1/storage/backup/file-restore/list",
        "nodes/pve1/qemu/100/vncwebsocket",
        "nodes/pve1/lxc/200/mtunnelwebsocket",
        "nodes/pve1/vncwebsocket",
        "cluster/acme/plugins",
        "cluster/acme/plugins/dns-a",
        "cluster/notifications/endpoints/webhook/hook-a",
        "cluster/metrics/server/influx-a",
        "cluster/sdn/ipams/ipam-a",
        "cluster/sdn/dns/dns-a",
        "access/domains/ldap-a",
        "nodes/pve1/subscription",
        "NODES/pve1/QEMU/100/AGENT/FILE-READ",
        "nodes/pve1/qemu/100/agent/file-read/",
        "nodes/pve1/qemu/100/./agent/file-read",
        "nodes//qemu/100/agent/file-read",
        "",
    ],
)
def test_a_read_inside_a_guest_or_of_a_secret_waits(path: str) -> None:
    assert path_reads_guest_or_secret({"path": path}) is True
    assert call_always_waits("proxmox.api.get", {"path": path}) is True


@pytest.mark.parametrize("path", [None, 42, ["nodes"]])
def test_a_path_meho_cannot_read_waits(path: Any) -> None:
    assert path_reads_guest_or_secret({"path": path}) is True


@pytest.mark.parametrize(
    "path",
    [
        "version",
        "nodes",
        "cluster/status",
        "cluster/resources",
        "nodes/pve1/qemu",
        "nodes/pve1/qemu/100/config",
        "nodes/pve1/qemu/100/snapshot",
        "nodes/pve1/qemu/100/status/current",
        "nodes/pve1/qemu/100/agent",
        "nodes/pve1/qemu/100/agent/get-osinfo",
        "nodes/pve1/qemu/100/agent/network-get-interfaces",
        "nodes/pve1/qemu/100/agent/info",
        "nodes/pve1/storage",
        "nodes/pve1/tasks/UPID:pve1:0001:task:100:root@pam:/status",
        "access/domains",
        "cluster/acme/account",
        "storage",
    ],
)
def test_a_plain_read_runs_at_once(path: str) -> None:
    assert path_reads_guest_or_secret({"path": path}) is False
    assert call_always_waits("proxmox.api.get", {"path": path}) is False


# ---------------------------------------------------------------------------
# k8s.apply: which kinds wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "Pod",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ReplicaSet",
        "ReplicationController",
        "Job",
        "CronJob",
        "PodTemplate",
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
        "ExternalSecret",
        "ClusterExternalSecret",
        "SecretStore",
        "ClusterSecretStore",
        "PushSecret",
        "SealedSecret",
        "Certificate",
        "Secret",
    ],
)
def test_an_apply_of_a_pod_rights_or_secret_kind_waits(kind: str) -> None:
    assert call_always_waits("k8s.apply", {"manifest": _manifest(kind)}) is True


def test_a_list_or_a_second_document_with_a_deployment_waits() -> None:
    listed = yaml.safe_dump(
        {"apiVersion": "v1", "kind": "List", "items": [yaml.safe_load(_DEPLOYMENT)]}
    )
    assert call_always_waits("k8s.apply", {"manifest": listed}) is True
    assert call_always_waits("k8s.apply", {"manifest": _CONFIG_MAP + "---\n" + _DEPLOYMENT}) is True


@pytest.mark.parametrize(
    ("kind", "api_version"),
    [
        ("Service", "v1"),
        ("Namespace", "v1"),
        ("ServiceAccount", "v1"),
        ("NetworkPolicy", "networking.k8s.io/v1"),
    ],
)
def test_an_apply_of_other_kinds_waits_too(kind: str, api_version: str) -> None:
    assert call_always_waits("k8s.apply", {"manifest": _manifest(kind, api_version)}) is True


def test_only_the_manifest_with_a_credential_is_hidden_after_the_run() -> None:
    plain_spec = {"containers": [{"name": "web", "image": "web:1", "args": ["--port", "80"]}]}
    plain = _manifest("Deployment", "apps/v1", spec={"template": {"spec": plain_spec}})
    assert manifest_holds_credential({"manifest": plain}) is False
    assert manifest_holds_credential({"manifest": _DEPLOYMENT}) is True
    assert manifest_holds_credential({"manifest": _manifest("Secret")}) is True


# ---------------------------------------------------------------------------
# Operations made from a Kubernetes API file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/api/v1/namespaces/{namespace}/secrets",
        "GET:/api/v1/namespaces/{namespace}/secrets/{name}",
        "GET:/api/v1/secrets",
        "GET:/api/v1/watch/namespaces/{namespace}/secrets/{name}",
        "PUT:/api/v1/namespaces/{namespace}/secrets/{name}",
        "POST:/api/v1/namespaces/{namespace}/pods/{name}/exec",
        "GET:/api/v1/namespaces/{namespace}/pods/{name}/exec",
        "POST:/api/v1/namespaces/{namespace}/pods/{name}/attach",
        "POST:/api/v1/namespaces/{namespace}/pods/{name}/portforward",
        "GET:/api/v1/namespaces/{namespace}/pods/{name}/proxy/{path}",
        "GET:/api/v1/namespaces/{namespace}/services/{name}/proxy",
        "GET:/api/v1/nodes/{name}/proxy/{path}",
        "POST:/api/v1/namespaces/{namespace}/serviceaccounts/{name}/token",
        "POST:/api/v1/namespaces/{namespace}/pods",
        "PATCH:/api/v1/namespaces/{namespace}/pods/{name}/ephemeralcontainers",
        "POST:/apis/apps/v1/namespaces/{namespace}/deployments",
        "PATCH:/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
        "PUT:/apis/batch/v1/namespaces/{namespace}/cronjobs/{name}",
        "POST:/apis/batch/v1/namespaces/{namespace}/jobs",
        "POST:/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
        "PUT:/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/roles/{name}",
        "POST:/apis/external-secrets.io/v1beta1/namespaces/{namespace}/externalsecrets",
        "POST:/apis/cert-manager.io/v1/namespaces/{namespace}/certificates",
        "PUT:/apis/certificates.k8s.io/v1/certificatesigningrequests/{name}/approval",
        "GET:/api/v1/namespaces/{ns}/secrets/{secret}/",
        "DELETE:/api/v1/namespaces/{namespace}/pods/{name}",
        "POST:/api/v1/namespaces/{namespace}/serviceaccounts",
    ],
)
def test_a_kubernetes_api_call_that_handles_a_credential_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
    assert is_credential_capable(op_id) is True
    assert credential_grant_reason(op_id) is not None


@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/api/v1/namespaces/{namespace}/pods",
        "GET:/api/v1/namespaces/{namespace}/pods/{name}",
        "GET:/api/v1/namespaces/{namespace}/pods/{name}/log",
        "GET:/apis/apps/v1/namespaces/{namespace}/deployments",
        "PATCH:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
        "PUT:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/status",
        "GET:/apis/rbac.authorization.k8s.io/v1/clusterroles",
        "GET:/api/v1/repos/{owner}/{repo}/actions/secrets",
        "k8s.pod.list",
        "GET:secrets",
    ],
)
def test_other_kubernetes_api_calls_keep_their_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False
    assert is_credential_capable(op_id) is False


# ---------------------------------------------------------------------------
# The call waits for every login, with and without a standing grant
# ---------------------------------------------------------------------------


async def _verdict(
    operator: Operator, row: EndpointDescriptor, params: dict[str, Any]
) -> tuple[PermissionVerdict, str]:
    descriptor = await lookup_descriptor(
        tenant_id=_TENANT,
        product=row.product,
        version=row.version,
        impl_id=row.impl_id,
        op_id=row.op_id,
    )
    assert descriptor is not None, row.op_id
    raise_descriptor_for_params(descriptor, params)
    token = work_ref_var.set(_RUN)
    try:
        verdict, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=None,
            connector_id=connector_id_of(descriptor),
            params=params,
            decide_only=True,
        )
    finally:
        work_ref_var.reset(token)
    return verdict, reason or ""


async def _assert_waits_with_and_without_a_grant(
    row: EndpointDescriptor, params: dict[str, Any]
) -> None:
    for operator in _LOGINS:
        verdict, reason = await _verdict(operator, row, params)
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (operator.principal_kind, reason)
    await _name_in_every_grant([row])
    for operator in _LOGINS:
        verdict, reason = await _verdict(operator, row, params)
        assert verdict is PermissionVerdict.NEEDS_APPROVAL, (operator.principal_kind, reason)
        assert "auto-granted" not in reason, reason
        assert "by run approval" not in reason, reason


def _row(rows: list[EndpointDescriptor], op_id: str) -> EndpointDescriptor:
    return next(row for row in rows if row.op_id == op_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "nodes/pve1/qemu/100/agent/file-read",
        "nodes/pve1/qemu/100/agent/exec-status",
        "nodes/pve1/qemu/100/cloudinit/dump",
    ],
    ids=["file-read", "exec-status", "cloudinit-dump"],
)
async def test_a_proxmox_read_inside_a_guest_waits_for_every_login(path: str) -> None:
    rows = await _register_all()
    await _assert_waits_with_and_without_a_grant(
        _row(rows, "proxmox.api.get"), {"path": path, "query": {"file": "/etc/app.conf"}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", _LOGINS, ids=_LOGIN_IDS)
async def test_a_plain_proxmox_read_still_runs_at_once(operator: Operator) -> None:
    rows = await _register_all()
    verdict, reason = await _verdict(
        operator, _row(rows, "proxmox.api.get"), {"path": "cluster/resources"}
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        _DEPLOYMENT,
        _manifest("ClusterRoleBinding"),
        _manifest("ExternalSecret"),
        _CONFIG_MAP,
        _SERVICE_ACCOUNT,
    ],
    ids=["deployment", "cluster-role-binding", "external-secret", "config-map", "service-account"],
)
async def test_a_k8s_apply_of_every_kind_waits_for_every_login(
    manifest: str,
) -> None:
    rows = await _register_all()
    await _assert_waits_with_and_without_a_grant(_row(rows, "k8s.apply"), {"manifest": manifest})


def _ingested(op_id: str) -> EndpointDescriptor:
    """An ingested row that asks for no approval, as a plain spec ingest makes it."""
    return EndpointDescriptor(
        product="kubeapi",
        version="1.30",
        impl_id="kubeapi-rest",
        op_id=op_id,
        method=op_id.split(":", 1)[0],
        source_kind="ingested",
        safety_level="safe",
        requires_approval=False,
        parameter_schema={},
        tenant_id=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/api/v1/namespaces/{namespace}/secrets/{name}",
        "POST:/api/v1/namespaces/{namespace}/pods/{name}/exec",
        "POST:/apis/apps/v1/namespaces/{namespace}/deployments",
    ],
    ids=["secret-read", "pod-exec", "deployment-create"],
)
async def test_a_kubernetes_api_call_waits_for_every_login(op_id: str) -> None:
    row = _ingested(op_id)
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    await _assert_waits_with_and_without_a_grant(row, {"namespace": "apps", "name": "a"})


@pytest.mark.asyncio
async def test_a_plain_kubernetes_api_read_still_runs_at_once() -> None:
    row = _ingested("GET:/api/v1/namespaces/{namespace}/pods")
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    verdict, reason = await _verdict(_service(), row, {"namespace": "apps"})
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


# ---------------------------------------------------------------------------
# The approval card names what each container runs, for a person only
# ---------------------------------------------------------------------------


@dataclass
class _Descriptor:
    op_id: str


async def _apply_preview(
    monkeypatch: pytest.MonkeyPatch, manifest: str, *, agent: bool, dry_run_fails: bool = False
) -> dict[str, Any]:
    async def _fake_apply(
        connector: Any, target: Any, operator: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        if dry_run_fails:
            raise ApplyRejectedError(
                "the API server refused the dry run",
                http_status=403,
                upstream_message="denied by an admission webhook",
            )
        return {"dry_run": True, "applied": [], "total": 0}

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.k8s_apply", _fake_apply
    )
    ctx = PreviewContext(
        descriptor=_Descriptor(op_id="k8s.apply"),  # type: ignore[arg-type]
        connector_instance=KubernetesConnector(kubeconfig_loader=None),  # type: ignore[arg-type]
        operator=_person(),
        target=SimpleNamespace(id=uuid.uuid4(), name="cluster-a"),
        params={"manifest": manifest},
        connector_id="kubernetes-1.x",
        agent_preview=agent,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect


@pytest.mark.asyncio
async def test_the_card_of_a_pod_apply_names_what_each_container_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = await _apply_preview(monkeypatch, _DEPLOYMENT, agent=False)

    run_commands = effect["preview"][RUN_COMMANDS_KEY]
    assert run_commands == [
        {
            "object": "Deployment/apps/a",
            "container": "web",
            "image": "registry.example.test/web:1",
            "command": ["sh", "-c", "serve"],
            "args": ["--token", "***REDACTED***"],
        }
    ]
    assert _TOKEN not in json.dumps(effect)


@pytest.mark.asyncio
async def test_an_agent_never_reads_what_the_containers_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_preview = await _apply_preview(monkeypatch, _DEPLOYMENT, agent=True)
    assert RUN_COMMANDS_KEY not in own_preview["preview"]

    stored = await _apply_preview(monkeypatch, _DEPLOYMENT, agent=False)
    shown = proposed_effect_for_agent(
        "k8s.apply", stored, params={"manifest": _DEPLOYMENT}, connector_id="k", tenant=None
    )
    assert shown is not None
    assert RUN_COMMANDS_KEY not in shown["preview"]
    assert shown["preview"]["manifest"] == stored["preview"]["manifest"]

    row = SimpleNamespace(
        op_id="k8s.apply",
        proposed_effect=stored,
        params={"manifest": _DEPLOYMENT},
        connector_id="kubernetes-1.x",
        tenant_id=_TENANT,
    )
    for login in (_service(), _agent()):
        rest = _proposed_effect_for(login, row)  # type: ignore[arg-type]
        assert RUN_COMMANDS_KEY not in rest["preview"]
    assert _proposed_effect_for(_person(), row) == stored  # type: ignore[arg-type]


_HOOKED_POD = _manifest(
    "Pod",
    spec={
        "containers": [
            {
                "name": "web",
                "image": "registry.example.test/web:1",
                "lifecycle": {
                    "postStart": {"exec": {"command": ["sh", "-c", f"login --password {_TOKEN}"]}},
                    "preStop": {"exec": {"command": ["drain", "--now"]}},
                },
                "startupProbe": {"exec": {"command": ["check", "--start"]}},
                "livenessProbe": {"exec": {"command": ["check", "--token", _TOKEN]}},
                "readinessProbe": {"exec": {"command": ["check", "--ready"]}},
            },
            {
                "name": "side",
                "image": "registry.example.test/side:1",
                "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
            },
        ]
    },
)


@pytest.mark.asyncio
async def test_the_card_names_the_commands_of_hooks_and_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = await _apply_preview(monkeypatch, _HOOKED_POD, agent=False)

    web, side = effect["preview"][RUN_COMMANDS_KEY]
    assert web["exec_commands"] == {
        "lifecycle.postStart": ["sh", "-c", "login --password ***REDACTED***"],
        "lifecycle.preStop": ["drain", "--now"],
        "startupProbe": ["check", "--start"],
        "livenessProbe": ["check", "--token", "***REDACTED***"],
        "readinessProbe": ["check", "--ready"],
    }
    assert "exec_commands" not in side
    assert _TOKEN not in json.dumps(effect)


@pytest.mark.asyncio
async def test_a_failed_dry_run_still_names_what_each_container_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = await _apply_preview(monkeypatch, _HOOKED_POD, agent=False, dry_run_fails=True)

    assert effect["preview_unavailable"] is True
    assert "preview" not in effect
    web, side = effect[RUN_COMMANDS_KEY]
    assert web["object"] == "Pod/apps/a"
    assert web["exec_commands"]["livenessProbe"] == ["check", "--token", "***REDACTED***"]
    assert side["container"] == "side"
    assert _TOKEN not in json.dumps(effect)


@pytest.mark.asyncio
async def test_an_agent_never_reads_the_commands_of_a_failed_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own_preview = await _apply_preview(monkeypatch, _HOOKED_POD, agent=True, dry_run_fails=True)
    assert own_preview["preview_unavailable"] is True
    assert RUN_COMMANDS_KEY not in own_preview

    stored = await _apply_preview(monkeypatch, _HOOKED_POD, agent=False, dry_run_fails=True)
    shown = proposed_effect_for_agent(
        "k8s.apply", stored, params={"manifest": _HOOKED_POD}, connector_id="k", tenant=None
    )
    assert shown is not None
    assert RUN_COMMANDS_KEY not in shown
    assert shown["preview_unavailable"] is True

    row = SimpleNamespace(
        op_id="k8s.apply",
        proposed_effect=stored,
        params={"manifest": _HOOKED_POD},
        connector_id="kubernetes-1.x",
        tenant_id=_TENANT,
    )
    for login in (_service(), _agent()):
        assert RUN_COMMANDS_KEY not in _proposed_effect_for(login, row)  # type: ignore[arg-type]
    assert _proposed_effect_for(_person(), row) == stored  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_dry_run_of_a_manifest_meho_cannot_read_keeps_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = await _apply_preview(monkeypatch, "kind: [unclosed", agent=False, dry_run_fails=True)
    assert effect["preview_unavailable"] is True
    assert RUN_COMMANDS_KEY not in effect


@pytest.mark.asyncio
async def test_the_card_of_an_apply_without_a_pod_has_no_command_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = await _apply_preview(monkeypatch, _CONFIG_MAP, agent=False)
    assert RUN_COMMANDS_KEY not in effect["preview"]


@pytest.mark.asyncio
async def test_a_container_named_twice_by_an_alias_is_listed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = (
        "apiVersion: v1\nkind: List\nmetadata: {name: l}\n"
        "x: &c {name: web, image: registry.example.test/web:1}\n"
        "items:\n"
        "  - {apiVersion: v1, kind: Pod, metadata: {name: a}, spec: {containers: [*c, *c]}}\n"
    )
    effect = await _apply_preview(monkeypatch, manifest, agent=False)
    assert [entry["container"] for entry in effect["preview"][RUN_COMMANDS_KEY]] == ["web"]


# ---------------------------------------------------------------------------
# The card of a waiting read names what it reads
# ---------------------------------------------------------------------------


async def _card(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    ctx = PreviewContext(
        descriptor=_Descriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_person(),
        target=None,
        params=params,
        connector_id="c",
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect


@pytest.mark.asyncio
async def test_the_card_of_a_waiting_proxmox_read_names_the_path_and_the_query() -> None:
    params = {"path": "nodes/pve1/qemu/100/agent/file-read", "query": {"file": "/var/log/app.log"}}
    assert path_reads_guest_or_secret(params)

    effect = await _card("proxmox.api.get", params)

    assert effect["params_echo"] == params


@pytest.mark.asyncio
async def test_the_card_of_a_waiting_kubernetes_api_call_names_its_params() -> None:
    op_id = "POST:/api/v1/namespaces/{namespace}/pods/{name}/exec"
    params = {"namespace": "apps", "name": "web", "command": "uptime"}
    assert ingested_op_is_credential_call(op_id)

    effect = await _card(op_id, params)

    assert effect["params_echo"] == params


# ---------------------------------------------------------------------------
# A sensor never runs a call that waits
# ---------------------------------------------------------------------------


def test_a_sensor_of_a_read_inside_a_guest_is_refused() -> None:
    payload = SimpleNamespace(
        connector_id="proxmox-8.x",
        op_id="proxmox.api.get",
        params={"path": "nodes/pve1/qemu/100/agent/file-read"},
    )
    descriptor = SimpleNamespace(safety_level="safe")
    with pytest.raises(SensorRequiresNoPersonError) as refused:
        _require_read(payload, descriptor)  # type: ignore[arg-type]
    assert refused.value.error_code == "sensor_requires_safe_operation"

    payload.params = {"path": "cluster/resources"}
    _require_read(payload, descriptor)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_sensor_of_a_read_inside_a_guest_reads_unknown_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _lookup(**_kwargs: Any) -> Any:
        return SimpleNamespace(safety_level="safe")

    monkeypatch.setattr(sensor_runner, "lookup_descriptor", _lookup)
    guest = SimpleNamespace(
        connector_id="proxmox-8.x",
        op_id="proxmox.api.get",
        params={"path": "nodes/pve1/qemu/100/agent/exec-status"},
        tenant_id=_TENANT,
    )
    outcome = await sensor_runner._safe_tier_floor(guest)  # type: ignore[arg-type]
    assert outcome is not None
    assert "op_waits_for_person" in json.dumps(outcome, default=str)

    guest.params = {"path": "cluster/resources"}
    assert await sensor_runner._safe_tier_floor(guest) is None  # type: ignore[arg-type]
