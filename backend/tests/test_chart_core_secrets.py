# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in core's ``secrets`` Job (#4041).

With every built-in part off the chart renders none of these objects, so
bring-your-own installs do not change. With a part on it renders the hook
ServiceAccount, Role and RoleBinding (weight -40) and the ``secrets`` Job
(weight -30), and the Job's plan matches the Python model in
``meho_backplane.core.secrets``. The test skips where ``helm`` is absent;
CI sets ``MEHO_REQUIRE_HELM=1`` so there it fails.
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

from meho_backplane.core.secrets import Plan

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_RELEASE = "rel"

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

# The Keycloak subchart (#4040) needs its host name and first tenant id
# whenever coreKeycloak is on, and the schema needs corePostgres with it.
# The chart then derives the issuer, so the hand-set one is cleared (#4591).
_CORE_ON = [
    "--set",
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    "coreKeycloak.firstTenantId=3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c",
]

_HOOK_PHASES = "pre-install,pre-upgrade,post-install,post-upgrade"
_CORE_NAMES = [
    f"{_RELEASE}-core-{kind}"
    for kind in (
        "postgres-superuser",
        "postgres-meho",
        "postgres-keycloak",
        "keycloak-bootstrap",
        "keycloak-admin",
        "ui-auth",
        "check-runner",
        "approval-handoff",
        "broadcast-auth",
        "tls",
        "first-login",
        "ci-login",
    )
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _render(*extra: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", _RELEASE, str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _core_objects(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        doc["kind"]: doc
        for doc in docs
        if doc["kind"] in {"ServiceAccount", "Role", "RoleBinding", "Job"}
        and "-core-" in doc["metadata"]["name"]
    }


def _plan(docs: list[dict[str, Any]]) -> Plan:
    job = _core_objects(docs)["Job"]
    env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    return Plan.model_validate_json(env["MEHO_CORE_SECRETS_PLAN"]["value"])


def _made(plan: Plan) -> dict[str, str]:
    return {entry.name: entry.kind for entry in plan.make}


def test_core_off_renders_no_core_object() -> None:
    """Bring-your-own (every built-in part off) gets none of the new objects."""
    docs = _render()
    assert _core_objects(docs) == {}
    assert not [d for d in docs if "-core-" in d["metadata"]["name"]]


def test_secrets_job_is_a_pre_install_hook_after_the_rights() -> None:
    objects = _core_objects(_render(*_CORE_ON))

    job = objects["Job"]
    assert job["metadata"]["name"] == f"{_RELEASE}-core-secrets"
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-weight"] == "-30"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert job["spec"]["backoffLimit"] == 3
    assert job["spec"]["ttlSecondsAfterFinished"] == 600

    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == f"{_RELEASE}-core-bootstrap"
    assert pod["automountServiceAccountToken"] is True
    container = pod["containers"][0]
    assert container["command"] == ["python", "-m", "meho_backplane.core.secrets"]
    assert container["image"] == "ghcr.io/evoila/meho:test"

    for kind in ("ServiceAccount", "Role", "RoleBinding"):
        meta = objects[kind]["metadata"]
        assert meta["name"] == f"{_RELEASE}-core-bootstrap"
        assert meta["annotations"]["helm.sh/hook"] == _HOOK_PHASES
        assert meta["annotations"]["helm.sh/hook-weight"] == "-40"
        assert "helm.sh/hook-delete-policy" not in meta["annotations"]
    assert objects["ServiceAccount"]["automountServiceAccountToken"] is False


def test_job_pod_is_outside_the_backplane_network_policy() -> None:
    """The backplane NetworkPolicy allows no Kubernetes API egress."""
    docs = _render(*_CORE_ON)
    labels = _core_objects(docs)["Job"]["spec"]["template"]["metadata"]["labels"]
    assert labels.get("app.kubernetes.io/name") != "meho"


def test_role_grants_exactly_the_designed_rights() -> None:
    docs = _render(*_CORE_ON, "--set", "approvalHandoff.secretName=my-handoff")
    rules = _core_objects(docs)["Role"]["rules"]

    by_verbs = {tuple(rule["verbs"]): rule for rule in rules}
    # The vault-init Job's rights are on its own Role (#4044).
    assert set(by_verbs) == {("create",), ("get", "patch"), ("get",)}
    for rule in rules:
        assert rule["apiGroups"] == [""]
        assert rule["resources"] == ["secrets"]
    assert "resourceNames" not in by_verbs[("create",)]
    assert by_verbs[("get", "patch")]["resourceNames"] == _CORE_NAMES
    assert by_verbs[("get",)]["resourceNames"] == ["meho-postgres", "my-handoff"]
    assert not any(
        kind in {"ClusterRole", "ClusterRoleBinding"} for kind in [d["kind"] for d in docs]
    )


def test_role_has_no_operator_rule_when_nothing_is_named() -> None:
    docs = _render(*_CORE_ON, "--set", f"postgres.credentialsSecret={_RELEASE}-core-postgres-meho")
    verbs = [tuple(rule["verbs"]) for rule in _core_objects(docs)["Role"]["rules"]]
    assert ("get",) not in verbs


def test_postgres_plan_makes_the_three_database_secrets() -> None:
    plan = _plan(
        _render(
            "--set",
            "corePostgres.enabled=true",
            "--set",
            f"postgres.credentialsSecret={_RELEASE}-core-postgres-meho",
        )
    )
    made = _made(plan)
    assert made == {
        f"{_RELEASE}-core-postgres-superuser": "postgres-superuser",
        f"{_RELEASE}-core-postgres-meho": "postgres-meho",
        f"{_RELEASE}-core-postgres-keycloak": "postgres-keycloak",
        f"{_RELEASE}-core-approval-handoff": "approval-handoff",
        f"{_RELEASE}-core-broadcast-auth": "broadcast-auth",
    }
    meho = next(e for e in plan.make if e.kind == "postgres-meho")
    assert meho.host == f"{_RELEASE}-postgres"
    assert plan.check == ()


def test_keycloak_plan_makes_the_bootstrap_and_three_client_secrets() -> None:
    made = _made(
        _plan(
            _render(
                *_CORE_ON,
                "--set",
                f"postgres.credentialsSecret={_RELEASE}-core-postgres-meho",
            )
        )
    )
    assert set(made.values()) == {
        "postgres-superuser",
        "postgres-meho",
        "postgres-keycloak",
        "keycloak-bootstrap",
        "keycloak-admin",
        "ui-auth",
        "check-runner",
        "approval-handoff",
        "broadcast-auth",
    }


def test_operator_named_secrets_are_checked_with_the_keys_meho_reads() -> None:
    plan = _plan(
        _render(
            *_CORE_ON,
            "--set",
            "corePostgres.existingSecrets.superuser=my-su",
            "--set",
            "coreKeycloak.existingSecrets.bootstrap=my-boot",
            "--set",
            "approvalHandoff.secretName=my-handoff",
            "--set",
            "broadcast.auth.existingSecret=my-valkey",
        )
    )
    checks = {entry.name: (entry.values_key, entry.keys) for entry in plan.check}
    assert checks == {
        "my-su": ("corePostgres.existingSecrets.superuser", ("password",)),
        "meho-postgres": ("postgres.credentialsSecret", ("password", "url")),
        "my-boot": ("coreKeycloak.existingSecrets.bootstrap", ("client_id", "client_secret")),
        "my-handoff": ("approvalHandoff.secretName", ("approval_handoff_encryption_key",)),
        "my-valkey": ("broadcast.auth.existingSecret", ("requirepass",)),
    }
    made = set(_made(plan).values())
    assert made.isdisjoint({"postgres-superuser", "postgres-meho", "keycloak-bootstrap"})
    assert made.isdisjoint({"approval-handoff", "broadcast-auth", "tls"})


def test_tls_secret_is_made_only_when_the_core_name_is_used() -> None:
    named = _plan(_render(*_CORE_ON))
    assert "tls" not in _made(named).values()
    assert all(entry.name != "meho-tls" for entry in named.check)

    core = _plan(_render(*_CORE_ON, "--set", f"ingress.tls.secretName={_RELEASE}-core-tls"))
    tls = next(e for e in core.make if e.kind == "tls")
    assert (tls.name, tls.host) == (f"{_RELEASE}-core-tls", "meho.test")

    no_tls = _plan(
        _render(*_CORE_ON, "--set", "ingress.tls.enabled=false", "--set", "ingress.tls.secretName=")
    )
    assert "tls" not in _made(no_tls).values()


def test_broadcast_secret_follows_the_subchart_switches() -> None:
    off = _plan(_render(*_CORE_ON, "--set", "broadcast.auth.enabled=false"))
    assert "broadcast-auth" not in _made(off).values()


def test_no_template_function_makes_a_secret() -> None:
    """Values come from the Job; the render carries no Secret object of the core."""
    docs = _render(*_CORE_ON, "--set", f"ingress.tls.secretName={_RELEASE}-core-tls")
    assert not [d for d in docs if d["kind"] == "Secret" and "-core-" in d["metadata"]["name"]]
    for path in (_CHART_DIR / "templates").glob("*core-secrets*"):
        text = path.read_text(encoding="utf-8")
        assert "randAlphaNum" not in text
        assert "lookup" not in text
    rendered = json.dumps(docs)
    assert "argocd.argoproj.io" not in rendered


def test_schema_rejects_an_unknown_core_key() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            _RELEASE,
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            "--set",
            "corePostgres.existingSecrets.admin=x",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "admin" in result.stderr
