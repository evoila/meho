# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``deploy/values-examples/values-k3s.yaml`` is the file a k3s install starts from.

k3s brings Traefik in the namespace ``kube-system`` and enforces network
rules. The file names Traefik, keeps it on HTTPS only, lets it into MEHO and
the built-in Keycloak, lets MEHO reach its own address through the Traefik
pods, turns on the web console (off by default) and turns on the network rules
of the built-in OpenBao and Keycloak. This test keeps the file valid and those
settings on.

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"
_CHART_DIR = _DEPLOY_DIR / "charts" / "meho"
_VALUES_K3S = _DEPLOY_DIR / "values-examples" / "values-k3s.yaml"
_FROM_KUBE_SYSTEM = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}
}
# The pods of the Traefik chart that k3s ships, and the pod port of its HTTPS
# entry point.
_TRAEFIK_PODS = {"app.kubernetes.io/name": "traefik"}
_TRAEFIK_HTTPS_POD_PORT = 8443

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _render() -> dict[str, dict[str, Any]]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "meho",
            str(_CHART_DIR),
            "-f",
            str(_VALUES_K3S),
            "--set",
            "image.tag=test",
            "--set",
            "ingress.host=meho.test",
            "--set",
            "coreKeycloak.hostname=meho.test",
            "--set",
            "coreKeycloak.firstTenantId=00000000-0000-0000-0000-000000000001",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {
        f"{doc['kind']}/{doc['metadata']['name']}": doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def test_values_k3s_renders_the_built_in_core() -> None:
    docs = _render()
    core = {"StatefulSet/meho-postgres", "StatefulSet/meho-keycloak", "StatefulSet/meho-vault"}
    assert core <= set(docs)


def test_values_k3s_serves_on_traefik_over_https_only() -> None:
    ingress = _render()["Ingress/meho"]
    assert ingress["spec"]["ingressClassName"] == "traefik"
    annotations = ingress["metadata"]["annotations"]
    assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"


def test_values_k3s_lets_only_kube_system_into_the_backplane() -> None:
    from_list = _render()["NetworkPolicy/meho"]["spec"]["ingress"][0]["from"]
    assert _FROM_KUBE_SYSTEM in from_list
    assert not any("ingress-nginx" in str(peer) for peer in from_list), (
        "the default namespace ingress-nginx must be gone"
    )


def test_values_k3s_lets_the_backplane_reach_the_traefik_pods_on_the_https_pod_port() -> None:
    egress = _render()["NetworkPolicy/meho"]["spec"]["egress"]
    rules = [
        rule
        for rule in egress
        if any(
            peer.get("podSelector", {}).get("matchLabels") == _TRAEFIK_PODS
            for peer in rule.get("to", [])
        )
    ]
    assert len(rules) == 1
    rule = rules[0]
    # One namespace, named. Never every namespace.
    assert rule["to"][0]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
    }
    assert [p["port"] for p in rule["ports"]] == [_TRAEFIK_HTTPS_POD_PORT]


def test_values_k3s_wires_the_console_secret() -> None:
    env = {
        e["name"]: e
        for e in _render()["Deployment/meho"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    for name in ("UI_KEYCLOAK_CLIENT_SECRET", "UI_SESSION_ENCRYPTION_KEY"):
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "meho-core-ui-auth"


def test_values_k3s_turns_on_the_network_rules_of_keycloak_and_openbao() -> None:
    docs = _render()
    keycloak_from = docs["NetworkPolicy/meho-keycloak"]["spec"]["ingress"][0]["from"]
    vault_from = docs["NetworkPolicy/meho-vault"]["spec"]["ingress"][0]["from"]
    assert _FROM_KUBE_SYSTEM in keycloak_from
    assert _FROM_KUBE_SYSTEM not in vault_from


def test_values_k3s_keeps_the_target_guard_on() -> None:
    # The chart writes the allowlist into the ConfigMap. Empty means MEHO
    # refuses every private target address.
    config = _render()["ConfigMap/meho-config"]["data"]
    assert config["MEHO_TARGET_SSRF_ALLOWLIST"] == ""


def test_values_k3s_names_the_console_secret_and_uses_the_default_storage_class() -> None:
    values = yaml.safe_load(_VALUES_K3S.read_text())
    assert values["uiConsole"]["secretName"] == "meho-core-ui-auth"
    assert values["corePostgres"]["persistence"]["storageClass"] == ""
    assert values["coreVault"]["persistence"]["storageClass"] == ""
    # An empty class renders no `storageClassName`, so the cluster default
    # (local-path on k3s) is used for both disks.
    docs = _render()
    for name in ("StatefulSet/meho-postgres", "StatefulSet/meho-vault"):
        for claim in docs[name]["spec"]["volumeClaimTemplates"]:
            assert "storageClassName" not in claim["spec"]
