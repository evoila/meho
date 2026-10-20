# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in OpenBao subchart (#4044).

Off by default: the chart then renders no OpenBao object at all, so a
bring-your-own install renders what it rendered before. The parent's
``coreVault:`` block maps onto ``charts/openbao/`` through a dependency
alias, and the subchart's own schema checks every value when it is on.
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

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"

_BASE_OVERRIDES = [
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "ingress.tls.secretName=meho-tls",
    "--set",
    "postgres.credentialsSecret=meho-postgres",
    "--set",
    "vault.address=https://vault.test",
    "--set",
    "keycloak.issuer=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakAudience=meho-backplane",
    "--set",
    "config.vaultAddr=https://vault.test",
    "--set",
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set",
    "networkPolicy.keycloakCIDR=10.0.3.0/24",
]

_ON = ["--set", "coreVault.enabled=true"]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _is_openbao(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/name") == "openbao")


def test_off_by_default_renders_nothing() -> None:
    docs = _render()
    assert not [d for d in docs if _is_openbao(d)]
    # Saying "off" out loud renders the same objects as leaving it out.
    explicit = _render("--set", "coreVault.enabled=false")
    assert [(d["kind"], d["metadata"]["name"]) for d in explicit] == [
        (d["kind"], d["metadata"]["name"]) for d in docs
    ]


def _openbao_docs(*extra: str, release: str = "test") -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", release, str(_CHART_DIR), *_BASE_OVERRIDES, *_ON, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d and _is_openbao(d)]


def _one(docs: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    (doc,) = [d for d in docs if d["kind"] == kind and name in (None, d["metadata"]["name"])]
    return doc


def _container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (container,) = _one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"]
    result: dict[str, Any] = container
    return result


def _config(docs: list[dict[str, Any]]) -> str:
    text: str = _one(docs, "ConfigMap")["data"]["server.hcl"]
    return text


def test_on_renders_server_services_and_config() -> None:
    docs = _openbao_docs()
    assert sorted((d["kind"], d["metadata"]["name"]) for d in docs) == [
        ("ConfigMap", "test-vault-config"),
        ("Service", "test-vault"),
        ("Service", "test-vault-internal"),
        ("StatefulSet", "test-vault"),
    ]
    sts = _one(docs, "StatefulSet")
    assert sts["spec"]["replicas"] == 1
    assert sts["spec"]["serviceName"] == "test-vault-internal"
    (claim,) = sts["spec"]["volumeClaimTemplates"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert "storageClassName" not in claim["spec"]


def test_official_image_pinned_by_digest() -> None:
    assert _container(_openbao_docs())["image"] == (
        "quay.io/openbao/openbao-ubi@sha256:33c4c3a7aeb8b812d570939bd7275f829901031e0a6b3e655207d8f0945bd248"
    )
    by_tag = _container(_openbao_docs("--set", "coreVault.image.digest="))
    assert by_tag["image"] == "quay.io/openbao/openbao-ubi:2.7.1"


def test_config_file_for_openbao() -> None:
    docs = _openbao_docs()
    hcl = _config(docs)
    assert 'storage "raft"' in hcl
    assert 'path = "/openbao/data"' in hcl
    assert "tls_disable     = 1" in hcl
    assert "ui = false" in hcl
    assert 'file_path = "stdout"' in hcl
    assert "disable_mlock" not in hcl
    container = _container(docs)
    assert container["args"] == ["server"]
    env = {e["name"]: e for e in container["env"]}
    assert env["SKIP_CHOWN"]["value"] == "true"
    assert env["BAO_ADDR"]["value"] == "http://127.0.0.1:8200"
    assert env["BAO_API_ADDR"]["value"] == "http://$(POD_NAME).test-vault-internal:8200"
    assert env["BAO_CLUSTER_ADDR"]["value"] == "https://$(POD_NAME).test-vault-internal:8201"
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts == {"data": "/openbao/data", "config": "/openbao/config"}


def test_vault_image_gets_vault_names_and_mlock_key() -> None:
    docs = _openbao_docs("--set", "coreVault.binary=vault")
    hcl = _config(docs)
    assert "disable_mlock = true" in hcl
    assert "audit" not in hcl
    assert 'path = "/vault/data"' in hcl
    container = _container(docs)
    assert container["name"] == "vault"
    assert container["readinessProbe"]["exec"]["command"] == ["vault", "status"]
    names = {e["name"] for e in container["env"]}
    assert {"VAULT_ADDR", "VAULT_API_ADDR", "VAULT_CLUSTER_ADDR"} <= names
    assert not [n for n in names if n.startswith("BAO_")]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts == {"data": "/vault/data", "config": "/vault/config"}


def test_locked_pod_is_not_ready_and_not_restarted() -> None:
    container = _container(_openbao_docs())
    assert container["readinessProbe"]["exec"]["command"] == ["bao", "status"]
    assert "livenessProbe" not in container
    assert "startupProbe" not in container


def test_both_services_reach_a_locked_pod() -> None:
    docs = _openbao_docs()
    client = _one(docs, "Service", "test-vault")
    headless = _one(docs, "Service", "test-vault-internal")
    assert client["spec"]["publishNotReadyAddresses"] is True
    assert headless["spec"]["publishNotReadyAddresses"] is True
    assert headless["spec"]["clusterIP"] == "None"
    assert [p["port"] for p in client["spec"]["ports"]] == [8200]
    assert [p["port"] for p in headless["spec"]["ports"]] == [8200, 8201]


def test_security_settings() -> None:
    docs = _openbao_docs()
    pod = _one(docs, "StatefulSet")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    ctx = pod["securityContext"]
    assert (ctx["runAsUser"], ctx["runAsGroup"], ctx["fsGroup"]) == (100, 1000, 1000)
    assert ctx["runAsNonRoot"] is True
    assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}
    container = _container(docs)
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["resources"]["limits"]["memory"] == "512Mi"


def test_openshift_sets_no_user_or_group_id() -> None:
    sts = _one(_openbao_docs("--set", "global.openshift=true"), "StatefulSet")
    ctx = sts["spec"]["template"]["spec"]["securityContext"]
    assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(ctx)
    assert ctx["runAsNonRoot"] is True


def test_config_change_restarts_the_pod() -> None:
    def checksum(*extra: str) -> str:
        sts = _one(_openbao_docs(*extra), "StatefulSet")
        value: str = sts["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        return value

    assert checksum() == checksum()
    assert checksum() != checksum("--set", "coreVault.binary=vault")


def test_networkpolicy_off_by_default_and_admits_backplane_when_on() -> None:
    assert not [d for d in _openbao_docs() if d["kind"] == "NetworkPolicy"]
    policy = _one(_openbao_docs("--set", "coreVault.networkPolicy.enabled=true"), "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "openbao",
        "app.kubernetes.io/instance": "test",
    }
    (rule,) = policy["spec"]["ingress"]
    backplane = {"app.kubernetes.io/name": "meho", "app.kubernetes.io/instance": "test"}
    assert rule["from"] == [{"podSelector": {"matchLabels": backplane}}]
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]


def test_nothing_cluster_wide_by_default() -> None:
    docs = _openbao_docs()
    assert not [d for d in docs if d["kind"].startswith("Cluster") or d["kind"] == "ServiceAccount"]
    pod = _one(docs, "StatefulSet")["spec"]["template"]["spec"]
    assert "serviceAccountName" not in pod


def test_auth_delegator_binds_its_own_account() -> None:
    docs = _openbao_docs("--set", "coreVault.authDelegator=true", "--namespace", "meho")
    _one(docs, "ServiceAccount", "test-vault")
    binding = _one(docs, "ClusterRoleBinding")
    assert binding["metadata"]["name"] == "meho-test-vault-auth-delegator"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "system:auth-delegator",
    }
    (subject,) = binding["subjects"]
    assert subject == {"kind": "ServiceAccount", "name": "test-vault", "namespace": "meho"}
    pod = _one(docs, "StatefulSet")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "test-vault"
    assert pod["automountServiceAccountToken"] is True


def test_long_release_name_keeps_names_valid() -> None:
    release = "r" * 53  # Helm's longest release name
    docs = _openbao_docs(release=release)
    sts = _one(docs, "StatefulSet")
    assert len(sts["metadata"]["name"]) <= 52
    assert sts["spec"]["serviceName"] == f"{sts['metadata']['name']}-internal"
    assert all(len(d["metadata"]["name"]) <= 63 for d in docs)


def test_on_runs_the_core_secrets_job() -> None:
    names = {(d["kind"], d["metadata"]["name"]) for d in _render(*_ON)}
    assert ("Job", "test-core-secrets") in names
    assert ("ServiceAccount", "test-core-bootstrap") in names


@pytest.mark.parametrize(
    ("extra", "messages"),
    [
        (["--set", "coreVault.binary=consul"], ("binary",)),
        (["--set", "coreVault.image.digest=sha256:xyz"], ("digest",)),
        (["--set", "coreVault.persistence.size=big"], ("size",)),
        (["--set", "coreVault.authDelegator=yes"], ("authDelegator",)),
        (["--set", "coreVault.unknownKey=1"], ("unknownKey",)),
    ],
    ids=["bad-binary", "bad-digest", "bad-disk-size", "auth-delegator-not-bool", "unknown-key"],
)
def test_bad_values_fail_at_render(extra: list[str], messages: tuple[str, ...]) -> None:
    result = _run(*_ON, *extra)
    assert result.returncode != 0
    assert any(m in result.stderr for m in messages), result.stderr
