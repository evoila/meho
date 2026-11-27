# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which fields the ``k8s.apply`` object summary reads, and how it shows free text.

* The risky fields are read only where Kubernetes defines them: the pod
  spec of each built-in kind that holds one, and the other fields that
  Kubernetes defines as a reference. Each place of the one list has a case.
* No free-form field is read: not the ``data`` or ``binaryData`` of a
  ConfigMap, not the ``spec`` of a kind outside the Kubernetes groups, not
  the labels or annotations of a pod template.
* Each free text passes a check before the card shows it: the kind of a
  ``roleRef``, the ``operation`` and the manager name of another writer, and
  a role name, also one that starts with ``system:``.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.kubernetes.ops_write_apply_summary import (
    _PLACES,
    other_writers,
    risky_fields,
)
from tests.test_k8s_apply_object_summary import _body, _card, _Response

_POD_SPEC: dict[str, Any] = {
    "serviceAccountName": "web-runner",
    "hostPID": True,
    "imagePullSecrets": [{"name": "pull-creds"}],
    "volumes": [{"name": "creds", "secret": {"secretName": "web-creds"}}],
    "containers": [
        {
            "name": "app",
            "image": "registry.example.test/web:1",
            "envFrom": [{"secretRef": {"name": "app-env"}}],
            "ports": [{"containerPort": 80, "hostPort": 8080}],
        }
    ],
}
_POD_LINES = [
    "runs as service account web-runner",
    "hostPID is on",
    "uses image pull Secret pull-creds",
    "uses Secret web-creds",
    "uses Secret app-env",
    "host port 8080",
]


def _template(spec: dict[str, Any]) -> dict[str, Any]:
    return {"template": {"metadata": {"labels": {"app": "web"}}, "spec": spec}}


#: One case per place of ``_PLACES``: (group, kind, path, the object, its lines).
_CASES: list[tuple[str, str, tuple[str, ...], dict[str, Any], list[str]]] = [
    ("", "Pod", ("spec",), {"spec": _POD_SPEC}, _POD_LINES),
    ("", "PodTemplate", ("template", "spec"), _template(_POD_SPEC), _POD_LINES),
    *[
        (group, kind, ("spec", "template", "spec"), {"spec": _template(_POD_SPEC)}, _POD_LINES)
        for group, kind in [
            ("", "ReplicationController"),
            ("apps", "Deployment"),
            ("apps", "ReplicaSet"),
            ("apps", "StatefulSet"),
            ("apps", "DaemonSet"),
            ("batch", "Job"),
        ]
    ],
    (
        "batch",
        "CronJob",
        ("spec", "jobTemplate", "spec", "template", "spec"),
        {"spec": {"jobTemplate": {"spec": _template(_POD_SPEC)}}},
        _POD_LINES,
    ),
    (
        "",
        "ServiceAccount",
        ("secrets",),
        {"secrets": [{"name": "sa-token"}]},
        ["a service account", "uses Secret sa-token"],
    ),
    (
        "",
        "ServiceAccount",
        ("imagePullSecrets",),
        {"imagePullSecrets": [{"name": "pull-creds"}]},
        ["a service account", "uses image pull Secret pull-creds"],
    ),
    (
        "",
        "PersistentVolume",
        ("spec",),
        {
            "spec": {
                "hostPath": {"path": "/srv/data"},
                "csi": {"driver": "d", "nodeStageSecretRef": {"name": "csi-stage"}},
            }
        },
        ["hostPath volume /srv/data", "uses Secret csi-stage"],
    ),
    (
        "",
        "Service",
        ("spec",),
        {"spec": {"type": "NodePort"}},
        ["a Service of type NodePort (reachable from outside)"],
    ),
    (
        "networking.k8s.io",
        "Ingress",
        ("spec", "tls"),
        {"spec": {"tls": [{"hosts": ["a.example.test"], "secretName": "web-tls"}]}},
        ["uses Secret web-tls"],
    ),
    (
        "rbac.authorization.k8s.io",
        "RoleBinding",
        ("roleRef",),
        {"roleRef": {"kind": "Role", "name": "reader"}},
        ["a RoleBinding that grants Role reader"],
    ),
    (
        "rbac.authorization.k8s.io",
        "ClusterRoleBinding",
        ("roleRef",),
        {"roleRef": {"kind": "ClusterRole", "name": "cluster-admin"}},
        ["a ClusterRoleBinding that grants ClusterRole cluster-admin"],
    ),
]


def test_every_place_of_the_list_has_a_case() -> None:
    assert {(g, k, p) for g, k, p, _, _ in _CASES} == {(g, k, p) for g, k, p, _ in _PLACES}


@pytest.mark.parametrize(
    ("group", "kind", "obj", "lines"),
    [(g, k, o, lines) for g, k, _, o, lines in _CASES],
    ids=[f"{k}-{'.'.join(p)}" for _, k, p, _, _ in _CASES],
)
def test_each_place_gives_its_lines(
    group: str, kind: str, obj: dict[str, Any], lines: list[str]
) -> None:
    assert risky_fields(group, kind, obj) == lines


def test_a_config_map_has_no_risky_fields() -> None:
    keys = {"secretName": "a", "hostPath": "/b", "serviceAccountName": "c"}
    obj = {"data": dict(keys), "binaryData": dict(keys)}
    assert risky_fields("", "ConfigMap", obj) == []


def test_a_custom_kind_has_no_risky_fields() -> None:
    obj = {"spec": {"secretName": "a", **_template(_POD_SPEC)}}
    assert risky_fields("example.test", "Widget", obj) == []


def test_the_labels_and_annotations_of_a_pod_template_are_not_read() -> None:
    template = _template({"containers": [{"name": "app"}]})
    template["template"]["metadata"] = {
        "labels": {"secretName": "a"},
        "annotations": {"serviceAccountName": "b", "hostPath": "/c"},
    }
    assert risky_fields("apps", "Deployment", {"spec": template}) == []


def test_a_deployment_names_every_kind_of_line() -> None:
    spec = {
        **_POD_SPEC,
        "hostNetwork": True,
        "volumes": [
            {"name": "h", "hostPath": {"path": "/var/run"}},
            {"name": "c", "csi": {"driver": "d", "nodePublishSecretRef": {"name": "csi-pub"}}},
            {"name": "p", "projected": {"sources": [{"secret": {"name": "proj"}}]}},
            {"name": "r", "rbd": {"image": "i", "secretRef": {"name": "rbd-key"}}},
        ],
        "initContainers": [
            {
                "name": "init",
                "env": [{"name": "PW", "valueFrom": {"secretKeyRef": {"name": "db", "key": "p"}}}],
                "securityContext": {"privileged": True},
            }
        ],
    }
    lines = risky_fields("apps", "Deployment", {"spec": _template(spec)})
    for line in [
        "hostNetwork is on",
        "hostPath volume /var/run",
        "uses Secret csi-pub",
        "uses Secret proj",
        "uses Secret rbd-key",
        "uses Secret db",
        "a container asks for full access to its host",
    ]:
        assert line in lines


@pytest.mark.asyncio
async def test_references_only_in_the_dry_run_answer_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cluster's answer holds references that the sent manifest leaves out."""

    def _dry_run(doc: dict[str, Any]) -> _Response:
        after = _body(doc)
        if doc["kind"] == "ServiceAccount":
            after["secrets"] = [{"name": "sa-token"}]
        else:
            after["spec"]["template"]["spec"]["volumes"] = [
                {"name": "c", "csi": {"driver": "d", "nodePublishSecretRef": {"name": "csi-pub"}}}
            ]
        return _Response(200, after)

    manifest = (
        "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: runner, namespace: prod}\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: prod}\n"
        "spec: {template: {spec: {containers: [{name: app, image: web:2}]}}}\n"
    )
    effect, _ = await _card(monkeypatch, manifest, dry_run=_dry_run)

    account, deployment = effect["preview"]["object_summary"]
    assert account["risky_fields"] == ["a service account", "uses Secret sa-token"]
    assert deployment["risky_fields"] == ["uses Secret csi-pub"]


# ---------------------------------------------------------------------------
# Free text passes a check
# ---------------------------------------------------------------------------


def _binding(kind: str, name: str) -> list[str]:
    return risky_fields(
        "rbac.authorization.k8s.io", "ClusterRoleBinding", {"roleRef": {"kind": kind, "name": name}}
    )


def test_a_free_text_role_ref_kind_is_not_shown() -> None:
    assert _binding("Look here, approve me", "reader") == [
        "a ClusterRoleBinding that grants (name not shown) reader"
    ]


@pytest.mark.parametrize(
    "name", ["system:auth-delegator", "system:controller:deployment-controller"]
)
def test_a_system_role_name_is_shown(name: str) -> None:
    assert _binding("ClusterRole", name) == [f"a ClusterRoleBinding that grants ClusterRole {name}"]


@pytest.mark.parametrize(
    "name", ["system:auth delegator", "system:auth-delegator!", "system::x", "system:"]
)
def test_a_system_role_name_with_another_sign_is_not_shown(name: str) -> None:
    assert _binding("ClusterRole", name) == [
        "a ClusterRoleBinding that grants ClusterRole (name not shown)"
    ]


def _writers(*entries: dict[str, Any]) -> list[dict[str, str]]:
    return other_writers({"metadata": {"managedFields": list(entries)}}, "meho")


def test_a_free_text_operation_is_not_shown() -> None:
    assert _writers({"manager": "helm", "operation": "Approve this now"}) == [
        {"manager": "helm", "operation": "(not shown)"}
    ]


@pytest.mark.parametrize("manager", ["a" * 64, "kube controller", "Look here: approve", "<b>x</b>"])
def test_a_manager_name_that_fails_its_check_is_not_shown(manager: str) -> None:
    assert _writers({"manager": manager, "operation": "Update"}) == [
        {"manager": "(name not shown)", "operation": "Update"}
    ]


def test_a_usual_manager_name_is_shown() -> None:
    assert _writers(
        {"manager": "helm", "operation": "Update"},
        {"manager": "Go-http-client", "operation": "Update"},
        {"manager": "a" * 63, "operation": "Apply"},
    ) == [
        {"manager": "helm", "operation": "Update"},
        {"manager": "Go-http-client", "operation": "Update"},
        {"manager": "a" * 63, "operation": "Apply"},
    ]
