# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in core's ``bootstrap`` Job (#4043).

With ``coreKeycloak.enabled=true`` the chart renders a post-install and
post-upgrade hook Job, weight 10, that runs the realm stage from the
backplane image under the hook account of #4041. Its plan holds no secret;
every secret reaches it through ``secretKeyRef``. The role groups are one
values list (``coreKeycloak.groups``), and each first-login person must
name a group from it. The install notes name the first-login Secret and the
command that reads it, never a value. Skips where ``helm`` is absent unless
``MEHO_REQUIRE_HELM=1``.
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

_ON = [
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    f"coreKeycloak.firstTenantId={_TENANT}",
]

_NOTES_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: notes-probe
data:
  notes: {{ tpl (.Files.Get "notes-probe.txt") . | quote }}
"""

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str, chart: Path = _CHART_DIR) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(chart), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _job(*extra: str) -> dict[str, Any] | None:
    for doc in _render(*extra):
        if doc["kind"] == "Job" and doc["metadata"]["name"] == "test-core-bootstrap":
            return doc
    return None


def _container(job: dict[str, Any] | None) -> dict[str, Any]:
    assert job is not None
    container: dict[str, Any] = job["spec"]["template"]["spec"]["containers"][0]
    return container


def _env(job: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in _container(job)["env"]}


def _plan(*extra: str) -> dict[str, Any]:
    plan: dict[str, Any] = json.loads(_env(_job(*_ON, *extra))["MEHO_CORE_BOOTSTRAP_PLAN"]["value"])
    return plan


def _configmap(*extra: str) -> dict[str, str]:
    for doc in _render(*extra):
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "test-meho-config":
            data: dict[str, str] = doc["data"]
            return data
    raise AssertionError("backplane ConfigMap not found")


def test_off_by_default_renders_no_job_and_no_role_setting() -> None:
    assert _job() is None
    assert "JWT_PLATFORM_ADMIN_ROLE_NAME" not in _configmap()


def test_job_is_a_post_install_and_post_upgrade_hook_under_the_hook_account() -> None:
    job = _job(*_ON)
    assert job is not None
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "post-install,post-upgrade"
    assert annotations["helm.sh/hook-weight"] == "10"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "test-core-bootstrap"
    assert pod["automountServiceAccountToken"] is True
    assert pod["restartPolicy"] == "Never"
    # The labels the Keycloak subchart's NetworkPolicy admits.
    assert job["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/instance": "test",
        "app.kubernetes.io/component": "core-bootstrap",
    }
    container = _container(job)
    deployment = next(
        d
        for d in _render(*_ON)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "test-meho"
    )
    backplane = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == backplane["image"]
    assert container["command"] == ["python", "-m", "meho_backplane.core.bootstrap", "keycloak"]


def test_job_reads_the_realm_file_keycloak_imported() -> None:
    job = _job(*_ON)
    container = _container(job)
    assert _env(job)["MEHO_CORE_REALM_FILE"]["value"] == "/realm/meho-realm.json"
    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/realm")
    assert job is not None
    volume = next(
        v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == mount["name"]
    )
    assert volume["configMap"]["name"] == "test-keycloak-realm"


def test_secrets_reach_the_job_only_through_secret_refs() -> None:
    env = _env(_job(*_ON))
    refs = {
        name: (e["valueFrom"]["secretKeyRef"]["name"], e["valueFrom"]["secretKeyRef"]["key"])
        for name, e in env.items()
        if "secretKeyRef" in e.get("valueFrom", {})
    }
    assert refs == {
        "KC_BOOTSTRAP_ADMIN_CLIENT_ID": ("test-core-keycloak-bootstrap", "client_id"),
        "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET": ("test-core-keycloak-bootstrap", "client_secret"),
        "MEHO_REALM_ADMIN_CLIENT_SECRET": ("test-core-keycloak-admin", "client_secret"),
        "MEHO_REALM_WEB_CLIENT_SECRET": ("test-core-ui-auth", "client_secret"),
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": ("test-core-check-runner", "client_secret"),
    }
    plain = {name for name, e in env.items() if "value" in e}
    assert plain == {"MEHO_CORE_BOOTSTRAP_PLAN", "MEHO_CORE_REALM_FILE", "MEHO_REALM_PUBLIC_URL"}
    assert env["MEHO_REALM_PUBLIC_URL"]["value"] == "https://meho.test"


def test_own_bootstrap_secret_is_used_by_the_job_too() -> None:
    env = _env(_job(*_ON, "--set", "coreKeycloak.existingSecrets.bootstrap=my-bootstrap"))
    for name in ("KC_BOOTSTRAP_ADMIN_CLIENT_ID", "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"):
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "my-bootstrap"


def test_plan_names_the_service_the_groups_and_the_two_people() -> None:
    plan = _plan()
    assert plan["keycloak_url"] == "http://test-keycloak:8080/auth"
    assert plan["tenant_id"] == _TENANT
    assert plan["first_login_secret"] == "test-core-first-login"
    assert plan["admin"] == {"username": "admin", "group": "MEHO-Admins"}
    assert plan["approver"] == {"username": "approver", "group": "MEHO-SA-Operators"}
    assert {g["name"]: (g["tenant_role"], g["approver"]) for g in plan["groups"]} == {
        "MEHO-Readers": ("read_only", False),
        "MEHO-Operators": ("operator", False),
        "MEHO-SA-Operators": ("operator", False),
        "MEHO-Admins": ("tenant_admin", False),
        "MEHO-Approvers": ("read_only", True),
    }


def test_renamed_groups_flow_from_the_one_values_list() -> None:
    plan = _plan(
        "--set-json",
        'coreKeycloak.groups=[{"name":"Ops","tenantRole":"operator"},'
        '{"name":"Admins","tenantRole":"tenant_admin"}]',
        "--set",
        "coreKeycloak.firstLogin.admin.group=Admins",
        "--set",
        "coreKeycloak.firstLogin.approver.group=Ops",
    )
    assert [g["name"] for g in plan["groups"]] == ["Ops", "Admins"]
    assert plan["approver"]["group"] == "Ops"


def test_person_in_a_group_outside_the_list_fails_at_render() -> None:
    result = _run(*_ON, "--set", "coreKeycloak.firstLogin.approver.group=MEHO-Nobody")
    assert result.returncode != 0
    assert "coreKeycloak.firstLogin.approver.group" in result.stderr
    assert "is not a name in coreKeycloak.groups" in result.stderr


def test_plan_holds_no_secret() -> None:
    raw = _env(_job(*_ON))["MEHO_CORE_BOOTSTRAP_PLAN"]["value"]
    assert "secret" not in json.loads(raw).keys() - {"first_login_secret"}
    assert "password" not in raw


def test_backplane_reads_platform_admin_from_the_realm_role() -> None:
    assert _configmap(*_ON)["JWT_PLATFORM_ADMIN_ROLE_NAME"] == "platform_admin"


def test_realm_file_has_the_platform_admin_role_and_no_static_groups() -> None:
    realm = json.loads(
        (_CHART_DIR / "charts" / "keycloak" / "files" / "meho-realm.json").read_text()
    )
    assert [r["name"] for r in realm["roles"]["realm"]] == ["platform_admin"]
    assert "groups" not in realm


def test_hook_role_may_write_the_first_login_secret() -> None:
    role = next(
        d
        for d in _render(*_ON)
        if d["kind"] == "Role" and d["metadata"]["name"] == "test-core-bootstrap"
    )
    named = [r for r in role["rules"] if "patch" in r["verbs"]]
    assert "test-core-first-login" in named[0]["resourceNames"]


def test_notes_name_the_secret_and_the_command_never_a_value(tmp_path: Path) -> None:
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "notes-probe.txt")
    (chart / "templates" / "zz-notes-probe.yaml").write_text(_NOTES_PROBE, encoding="utf-8")
    result = _run(*_ON, "--show-only", "templates/zz-notes-probe.yaml", chart=chart)
    assert result.returncode == 0, result.stderr
    notes: str = yaml.safe_load(result.stdout)["data"]["notes"]
    assert "test-core-first-login" in notes
    assert (
        "get secret test-core-first-login -o jsonpath='{.data.admin_password}' | base64 -d" in notes
    )
    assert "meho login https://meho.test" in notes
    assert "Keycloak asks for a new one at the first login" in notes
    assert "Change them" in notes
