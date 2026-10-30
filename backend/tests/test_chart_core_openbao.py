# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in OpenBao subchart and its setup Job (#4044).

Off by default: the chart then renders no OpenBao object at all, so a
bring-your-own install renders what it rendered before. The parent's
``coreVault:`` block maps onto ``charts/openbao/`` through a dependency
alias, and the subchart's own schema checks every value when it is on.
The parent renders the ``vault-init`` Job with its own account; the Job's
code is tested in ``test_core_vault_init.py``.
Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import json
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

# The setup Job trusts the built-in Keycloak, so OpenBao needs it on (and
# Keycloak needs the built-in PostgreSQL).
_ON = [
    "--set",
    "coreVault.enabled=true",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    "coreKeycloak.firstTenantId=3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c",
]
_POLICY_DIR = _CHART_DIR / "files" / "openbao" / "policies"

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


def _is_vault_init(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/component") == "vault-init")


def test_off_by_default_renders_nothing() -> None:
    docs = _render()
    assert not [d for d in docs if _is_openbao(d) or _is_vault_init(d)]
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
    server, _unseal = _one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"]
    result: dict[str, Any] = server
    return result


def _unseal_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    _server, unseal = _one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"]
    result: dict[str, Any] = unseal
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
    # Without it, a locked (never ready) pod would not get the change.
    assert _one(_openbao_docs(), "StatefulSet")["spec"]["podManagementPolicy"] == "Parallel"


def test_networkpolicy_off_by_default_and_admits_backplane_when_on() -> None:
    assert not [d for d in _openbao_docs() if d["kind"] == "NetworkPolicy"]
    policy = _one(_openbao_docs("--set", "coreVault.networkPolicy.enabled=true"), "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "openbao",
        "app.kubernetes.io/instance": "test",
    }
    (rule,) = policy["spec"]["ingress"]
    backplane = {"app.kubernetes.io/name": "meho", "app.kubernetes.io/instance": "test"}
    setup_job = {"app.kubernetes.io/instance": "test", "app.kubernetes.io/component": "vault-init"}
    assert rule["from"] == [
        {"podSelector": {"matchLabels": backplane}},
        {"podSelector": {"matchLabels": setup_job}},
    ]
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]


def test_nothing_cluster_wide_by_default() -> None:
    assert not [d for d in _render(*_ON) if d["kind"].startswith("Cluster")]
    docs = _openbao_docs()
    assert not [d for d in docs if d["kind"] == "ServiceAccount"]
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
        # The unseal mode is whether the Secret exists, never a value (#4046).
        (["--set", "coreVault.unseal.mode=self"], ("mode",)),
    ],
    ids=[
        "bad-binary",
        "bad-digest",
        "bad-disk-size",
        "auth-delegator-not-bool",
        "unknown-key",
        "no-unseal-mode",
    ],
)
def test_bad_values_fail_at_render(extra: list[str], messages: tuple[str, ...]) -> None:
    result = _run(*_ON, *extra)
    assert result.returncode != 0
    assert any(m in result.stderr for m in messages), result.stderr


def test_unseal_helper_reads_the_key_file_on_every_pass() -> None:
    docs = _openbao_docs()
    server, unseal = _container(docs), _unseal_container(docs)
    assert unseal["name"] == "unseal"
    assert unseal["image"] == server["image"]
    assert unseal["securityContext"] == server["securityContext"]
    assert {e["name"]: e["value"] for e in unseal["env"]} == {"BAO_ADDR": "http://127.0.0.1:8200"}
    script = unseal["args"][0]
    # The key goes from the file through a pipe (white space dropped, so a
    # line break at the end does no harm), and the call's output is thrown
    # away: the key is never on a command line, in the shell or in a log.
    assert "tr -d '[:space:]' </unseal/key | bao write sys/unseal key=- >/dev/null 2>&1" in script
    assert "$(" not in script
    assert "sleep 5" in script
    assert unseal["volumeMounts"] == [{"name": "unseal", "mountPath": "/unseal", "readOnly": True}]
    assert "unseal" not in {m["name"] for m in server["volumeMounts"]}
    pod = _one(docs, "StatefulSet")["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    # Optional and no subPath: the pod starts without the Secret, and the
    # file goes away when the Secret is deleted.
    assert volumes["unseal"]["secret"] == {"secretName": "test-core-vault-unseal", "optional": True}


def test_unseal_helper_on_a_vault_image() -> None:
    unseal = _unseal_container(_openbao_docs("--set", "coreVault.binary=vault"))
    assert "</unseal/key | vault write sys/unseal key=-" in unseal["args"][0]
    assert {e["name"] for e in unseal["env"]} == {"VAULT_ADDR"}


_NOTES_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: notes-probe
data:
  notes: {{ tpl (.Files.Get "notes-probe.txt") . | quote }}
"""


def test_notes_show_the_command_never_the_key(tmp_path: Path) -> None:
    # `helm template` does not print NOTES.txt; render it through a probe.
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "notes-probe.txt")
    (chart / "templates" / "zz-notes-probe.yaml").write_text(_NOTES_PROBE, encoding="utf-8")

    def notes(*extra: str) -> str:
        result = subprocess.run(
            [
                "helm",
                "template",
                "test",
                str(chart),
                *_BASE_OVERRIDES,
                "--namespace",
                "meho",
                "--show-only",
                "templates/zz-notes-probe.yaml",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        text: str = yaml.safe_load(result.stdout)["data"]["notes"]
        return text

    on = notes(*_ON)
    assert (
        "kubectl -n meho get secret test-core-vault-unseal -o jsonpath='{.data.key}' | base64 -d"
        in on
    )
    assert (
        "Copy this to your key store now; for a keyholder at every restart, "
        "delete the Secret and restart OpenBao" in on
    )
    assert "kubectl -n meho rollout restart statefulset test-vault" in on
    assert "OpenBao" not in notes()
    # Helm stores the NOTES text in the release; a `lookup` there would print
    # the key on every upgrade.
    assert "lookup" not in (_CHART_DIR / "templates" / "NOTES.txt").read_text(encoding="utf-8")


def _setup_docs(*extra: str, release: str = "test") -> dict[str, dict[str, Any]]:
    result = subprocess.run(
        [
            "helm",
            "template",
            release,
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            *_ON,
            "--namespace",
            "meho",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d and _is_vault_init(d)]
    by_kind = {d["kind"]: d for d in docs}
    assert len(by_kind) == len(docs)
    return by_kind


def _job_name(*extra: str) -> str:
    name: str = _setup_docs(*extra)["Job"]["metadata"]["name"]
    return name


def test_setup_job_is_a_release_object_with_its_own_account() -> None:
    docs = _setup_docs()
    assert sorted(docs) == ["ConfigMap", "Job", "Role", "RoleBinding", "ServiceAccount"]
    for doc in docs.values():
        # Not a hook: Helm --wait-for-jobs, klipper-helm and Argo CD wait for it.
        assert not any(k.startswith("helm.sh/hook") for k in doc["metadata"].get("annotations", {}))
    assert docs["ServiceAccount"]["metadata"]["name"] == "test-vault-init"
    assert docs["ServiceAccount"]["automountServiceAccountToken"] is False
    assert docs["RoleBinding"]["subjects"] == [
        {"kind": "ServiceAccount", "name": "test-vault-init", "namespace": "meho"}
    ]
    names = [
        "test-core-vault-unseal",
        "test-core-vault-init",
        "test-core-vault-scheduler",
        "test-core-vault-snapshot",
    ]
    assert docs["Role"]["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get", "patch"],
            "resourceNames": names,
        },
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["delete"],
            "resourceNames": ["test-core-vault-init"],
        },
    ]

    job = docs["Job"]
    assert job["metadata"]["name"].startswith("test-vault-init-")
    assert "ttlSecondsAfterFinished" not in job["spec"]
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "test-vault-init"
    assert pod["restartPolicy"] == "Never"
    assert job["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/instance": "test",
        "app.kubernetes.io/component": "vault-init",
    }
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["openbao-token"]["projected"]["sources"] == [
        {"serviceAccountToken": {"audience": "openbao", "expirationSeconds": 600, "path": "token"}}
    ]
    assert volumes["policies"]["configMap"]["name"] == "test-vault-init-policies"
    (container,) = pod["containers"]
    assert container["command"] == ["python", "-m", "meho_backplane.core.vault_init"]
    env = {e["name"]: e for e in container["env"]}
    plan = json.loads(env["MEHO_CORE_VAULT_PLAN"]["value"])
    assert plan == {
        "openbao_url": "http://test-vault:8200",
        "keycloak_url": "http://test-keycloak:8080/auth",
        "jwks_url": "http://test-keycloak:8080/auth/realms/meho/protocol/openid-connect/certs",
        "bound_issuer": "https://meho.test/auth/realms/meho",
        "audience": "meho-backplane",
        "policy_dir": "/opt/meho/openbao-policies",
        "service_account": "test-vault-init",
        "sa_token_file": "/var/run/secrets/openbao/token",
        "unseal_secret": "test-core-vault-unseal",
        "init_secret": "test-core-vault-init",
        "scheduler_secret": "test-core-vault-scheduler",
        "snapshot_secret": "test-core-vault-snapshot",
    }


def test_setup_job_carries_the_policy_files() -> None:
    data = _setup_docs()["ConfigMap"]["data"]
    assert data == {
        p.name: p.read_text(encoding="utf-8") for p in sorted(_POLICY_DIR.glob("*.hcl"))
    }
    assert len(data) == 8


def test_setup_job_name_changes_with_its_setup_only() -> None:
    name = _job_name()
    assert _job_name() == name
    # The plan, the image and the pod spec are part of the name ...
    assert (
        _job_name("--set", "coreKeycloak.hostname=other.test", "--set", "ingress.host=other.test")
        != name
    )
    assert _job_name("--set", "image.tag=other") != name
    assert _job_name("--set", "migrationJob.resources.limits.memory=1Gi") != name
    # ... OpenBao's own settings are not: they do not change the setup.
    assert _job_name("--set", "coreVault.persistence.size=20Gi") == name


def test_setup_job_needs_the_built_in_keycloak() -> None:
    result = _run("--set", "coreVault.enabled=true")
    assert result.returncode != 0
    assert "coreVault.enabled needs coreKeycloak.enabled" in result.stderr


def test_setup_job_names_stay_valid_for_a_long_release_name() -> None:
    docs = _setup_docs(release="r" * 53)
    assert all(len(d["metadata"]["name"]) <= 63 for d in docs.values())
