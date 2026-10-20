# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in core's ``bootstrap`` Job (#4043).

The Job renders only with ``coreKeycloak.enabled``. It is a post-install and
post-upgrade hook at weight 10 under the hook ServiceAccount of the
``secrets`` Job, runs the backplane image with the ``keycloak`` stage, gets
every secret by ``secretKeyRef``, mounts the realm ConfigMap, and its plan
matches the Python model. The install notes name the first-login Secret and
never a value. Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.core.keycloak import Plan

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_REALM_FILE = _CHART_DIR / "charts" / "keycloak" / "files" / "meho-realm.json"
_RELEASE = "rel"
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"

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
    "config.vaultAddr=https://vault.test",
    "--set",
    "keycloak.issuer=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakAudience=meho-backplane",
    "--set",
    "networkPolicy.enabled=false",
]
_CORE_ON = [
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    f"coreKeycloak.firstTenantId={_TENANT}",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", _RELEASE, str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _job(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            d
            for d in docs
            if d["kind"] == "Job" and d["metadata"]["name"] == f"{_RELEASE}-core-bootstrap"
        ),
        None,
    )


def _container(job: dict[str, Any]) -> dict[str, Any]:
    container: dict[str, Any] = job["spec"]["template"]["spec"]["containers"][0]
    return container


def _env(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in _container(job)["env"]}


def test_core_keycloak_off_renders_no_bootstrap_job() -> None:
    assert _job(_render()) is None
    assert _job(_render("--set", "corePostgres.enabled=true")) is None


def test_bootstrap_job_is_a_post_install_hook_under_the_hook_account() -> None:
    job = _job(_render(*_CORE_ON))
    assert job is not None
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    assert annotations["helm.sh/hook-weight"] == "10"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert job["spec"]["backoffLimit"] == 3
    assert job["spec"]["ttlSecondsAfterFinished"] == 600

    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == f"{_RELEASE}-core-bootstrap"
    assert pod["automountServiceAccountToken"] is True
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels.get("app.kubernetes.io/name") != "meho"

    container = _container(job)
    assert container["command"] == ["python", "-m", "meho_backplane.core.bootstrap", "keycloak"]
    assert container["image"] == "ghcr.io/evoila/meho:test"
    assert container["terminationMessagePolicy"] == "FallbackToLogsOnError"


def test_plan_names_keycloak_the_realm_file_and_the_first_login_secret() -> None:
    job = _job(_render(*_CORE_ON))
    assert job is not None
    plan = Plan.model_validate_json(_env(job)["MEHO_CORE_KEYCLOAK_PLAN"]["value"])
    assert plan.keycloak_url == f"http://{_RELEASE}-keycloak:8080/auth"
    assert plan.realm_file == "/opt/meho/realm/meho-realm.json"
    assert plan.first_login_secret == f"{_RELEASE}-core-first-login"
    assert (plan.admin_username, plan.approver_username) == ("admin", "approver")
    assert plan.ci.enabled is False
    assert plan.ci.secret_name == f"{_RELEASE}-core-ci-login"

    custom = _job(
        _render(
            *_CORE_ON,
            "--set",
            "coreKeycloak.firstLogin.adminUsername=root-person",
            "--set",
            "coreKeycloak.firstLogin.approverUsername=second",
            "--set",
            "coreKeycloak.ci.enabled=true",
            "--set",
            f"coreKeycloak.ci.tenantId={_TENANT}",
        )
    )
    assert custom is not None
    plan = Plan.model_validate_json(_env(custom)["MEHO_CORE_KEYCLOAK_PLAN"]["value"])
    assert (plan.admin_username, plan.approver_username) == ("root-person", "second")
    assert plan.ci.enabled is True
    assert plan.ci.tenant_id == _TENANT


def test_secrets_come_by_reference_and_the_placeholders_have_values() -> None:
    job = _job(_render(*_CORE_ON))
    assert job is not None
    env = _env(job)
    refs = {
        name: (
            e["valueFrom"]["secretKeyRef"]["name"],
            e["valueFrom"]["secretKeyRef"]["key"],
            e["valueFrom"]["secretKeyRef"].get("optional", False),
        )
        for name, e in env.items()
        if "secretKeyRef" in e.get("valueFrom", {})
    }
    assert refs == {
        "MEHO_REALM_ADMIN_CLIENT_SECRET": (
            f"{_RELEASE}-core-keycloak-admin",
            "client_secret",
            False,
        ),
        "MEHO_REALM_WEB_CLIENT_SECRET": (f"{_RELEASE}-core-ui-auth", "client_secret", False),
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": (
            f"{_RELEASE}-core-check-runner",
            "client_secret",
            False,
        ),
        "MEHO_CORE_BOOTSTRAP_CLIENT_ID": (f"{_RELEASE}-core-keycloak-bootstrap", "client_id", True),
        "MEHO_CORE_BOOTSTRAP_CLIENT_SECRET": (
            f"{_RELEASE}-core-keycloak-bootstrap",
            "client_secret",
            True,
        ),
    }
    assert env["MEHO_REALM_PUBLIC_URL"]["value"] == "https://meho.test"
    assert env["MEHO_REALM_TENANT_ID"]["value"] == _TENANT
    assert env["POD_NAMESPACE"]["valueFrom"] == {"fieldRef": {"fieldPath": "metadata.namespace"}}
    placeholders = set(re.findall(r"\$\{([^}]*)\}", _REALM_FILE.read_text()))
    assert placeholders
    assert placeholders <= set(env)
    # No value of a secret kind is rendered in the Job.
    for e in env.values():
        if "value" in e:
            assert "SECRET" not in e["name"]


def test_your_own_bootstrap_secret_is_read_by_the_job_too() -> None:
    job = _job(_render(*_CORE_ON, "--set", "coreKeycloak.existingSecrets.bootstrap=my-boot"))
    assert job is not None
    env = _env(job)
    for name in ("MEHO_CORE_BOOTSTRAP_CLIENT_ID", "MEHO_CORE_BOOTSTRAP_CLIENT_SECRET"):
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "my-boot"


def test_realm_file_is_mounted_from_the_subchart_configmap() -> None:
    docs = _render(*_CORE_ON)
    job = _job(docs)
    assert job is not None
    mounts = {m["name"]: m for m in _container(job)["volumeMounts"]}
    assert mounts["realm"]["mountPath"] == "/opt/meho/realm"
    assert mounts["realm"]["readOnly"] is True
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    assert volumes["realm"]["configMap"]["name"] == f"{_RELEASE}-keycloak-realm"
    assert [
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == f"{_RELEASE}-keycloak-realm"
    ]


def test_hook_role_may_read_and_patch_the_first_login_and_ci_secrets() -> None:
    role = next(
        d
        for d in _render(*_CORE_ON)
        if d["kind"] == "Role" and d["metadata"]["name"] == f"{_RELEASE}-core-bootstrap"
    )
    rule = next(r for r in role["rules"] if r["verbs"] == ["get", "patch"])
    assert f"{_RELEASE}-core-first-login" in rule["resourceNames"]
    assert f"{_RELEASE}-core-ci-login" in rule["resourceNames"]
    assert role["metadata"]["annotations"]["helm.sh/hook"] == (
        "pre-install,pre-upgrade,post-install,post-upgrade"
    )


def test_keycloak_network_policy_admits_the_job_pod() -> None:
    """With the subchart's NetworkPolicy on, the Job must still reach Keycloak."""
    docs = _render(*_CORE_ON, "--set", "coreKeycloak.networkPolicy.enabled=true")
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == f"{_RELEASE}-keycloak"
    )
    (rule,) = policy["spec"]["ingress"]
    job = _job(docs)
    assert job is not None
    job_labels = job["spec"]["template"]["metadata"]["labels"]
    admitted = [
        peer["podSelector"]["matchLabels"]
        for peer in rule["from"]
        if "podSelector" in peer
        and peer["podSelector"]["matchLabels"].items() <= job_labels.items()
    ]
    assert admitted == [
        {"app.kubernetes.io/instance": _RELEASE, "app.kubernetes.io/component": "core-bootstrap"}
    ]
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]


def test_notes_name_the_first_login_secret_and_the_command_but_no_value() -> None:
    notes = (_CHART_DIR / "templates" / "NOTES.txt").read_text(encoding="utf-8")
    block = notes[notes.index("First login (built-in Keycloak)") :]
    assert '{{ printf "%s-core-first-login" .Release.Name }}' in block
    assert "kubectl -n {{ .Release.Namespace }} get secret" in block
    assert "-o jsonpath='{.data.admin_password}' | base64 -d" in block
    assert "meho login https://{{ .Values.coreKeycloak.hostname }}" in block
    assert "new password at the first login" in block
    assert "MEHO-Admins" in block
    assert "MEHO-SA-Operators" in block
    assert "{{- if .Values.coreKeycloak.enabled }}" in notes[: notes.index("First login")]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--set", "coreKeycloak.ci.enabled=true"], "tenantId"),
        (
            ["--set", "coreKeycloak.ci.enabled=true", "--set", "coreKeycloak.ci.tenantId=nope"],
            "tenantId",
        ),
        (["--set", "coreKeycloak.firstLogin.adminUsername="], "adminUsername"),
        (["--set", "coreKeycloak.firstLogin.other=x"], "other"),
    ],
    ids=["ci-needs-tenant", "ci-bad-tenant", "empty-username", "unknown-key"],
)
def test_bad_values_fail_at_render(extra: list[str], message: str) -> None:
    result = _run(*_CORE_ON, *extra)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr
