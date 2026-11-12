# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the role check before an upgrade (#6336).

With the built-in Keycloak, a pre-upgrade Job checks that ``meho-admin`` has
``manage-realm``. It runs before the database Jobs, never retries, keeps its
log when it fails, holds the secret only by reference, carries no Kubernetes
token, and the Keycloak NetworkPolicy admits its pod. Its name names the
role, so Helm's error does too. Skips where ``helm`` is absent unless
``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.core.keycloak import Plan

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"
_RELEASE = "rel"
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"

_BASE = [
    "--set",
    "corePostgres.enabled=false",
    "--set",
    "coreKeycloak.enabled=false",
    "--set",
    "coreVault.enabled=false",
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
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "postgres.credentialsSecret=",
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


def _render(*extra: str, release: str = _RELEASE) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", release, str(_CHART_DIR), *_BASE, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _check_job(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            d
            for d in docs
            if d["kind"] == "Job" and d["metadata"]["name"].endswith("-check-manage-realm")
        ),
        None,
    )


def _hook_weight(docs: list[dict[str, Any]], name: str) -> int:
    doc = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == name)
    return int(doc["metadata"]["annotations"]["helm.sh/hook-weight"])


def test_no_built_in_keycloak_renders_no_check() -> None:
    assert _check_job(_render()) is None
    assert (
        _check_job(
            _render("--set", "corePostgres.enabled=true", "--set", "postgres.credentialsSecret=")
        )
        is None
    )


def test_check_is_a_pre_upgrade_hook_that_keeps_a_failed_log() -> None:
    job = _check_job(_render(*_CORE_ON))
    assert job is not None
    assert job["metadata"]["name"] == f"{_RELEASE}-check-manage-realm"
    annotations = job["metadata"]["annotations"]
    # Only before an upgrade: a new install gets the role from the realm file.
    assert annotations["helm.sh/hook"] == "pre-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation,hook-succeeded"
    assert "ttlSecondsAfterFinished" not in job["spec"]
    assert job["spec"]["backoffLimit"] == 0

    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == f"{_RELEASE}-core-bootstrap"
    (container,) = pod["containers"]
    assert container["command"] == [
        "python",
        "-m",
        "meho_backplane.core.bootstrap",
        "keycloak-role-check",
    ]
    assert container["image"] == "ghcr.io/evoila/meho:test"
    assert container["terminationMessagePolicy"] == "FallbackToLogsOnError"


def test_check_runs_after_the_secrets_and_before_every_other_pre_upgrade_job() -> None:
    # Keycloak on your own PostgreSQL: its database Job and the migration Job are hooks too.
    extra = [
        "--set",
        "corePostgres.enabled=false",
        "--set",
        "postgres.credentialsSecret=meho-postgres",
        "--set",
        "coreKeycloak.database.host=pg.test",
        "--set",
        "coreKeycloak.database.create.enabled=true",
        "--set",
        "coreKeycloak.database.create.adminSecret.name=pg-admin",
    ]
    docs = _render(*_CORE_ON, *extra)
    weight = _hook_weight(docs, f"{_RELEASE}-check-manage-realm")
    assert _hook_weight(docs, f"{_RELEASE}-core-secrets") < weight
    others = [
        d["metadata"]["name"]
        for d in docs
        if d["kind"] == "Job"
        and "pre-upgrade" in d["metadata"].get("annotations", {}).get("helm.sh/hook", "")
        and d["metadata"]["name"]
        not in {f"{_RELEASE}-core-secrets", f"{_RELEASE}-check-manage-realm"}
    ]
    assert others, "the render has no other pre-upgrade Job to compare with"
    for name in others:
        assert weight < _hook_weight(docs, name), name
    # The hook account is made first (weight -40).
    account = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == f"{_RELEASE}-core-bootstrap"
    )
    assert int(account["metadata"]["annotations"]["helm.sh/hook-weight"]) < weight


def test_secret_comes_by_reference_and_the_plan_matches_the_model() -> None:
    job = _check_job(_render(*_CORE_ON))
    assert job is not None
    env = {e["name"]: e for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert set(env) == {"MEHO_CORE_KEYCLOAK_PLAN", "MEHO_REALM_ADMIN_CLIENT_SECRET"}
    assert "value" not in env["MEHO_REALM_ADMIN_CLIENT_SECRET"]
    assert env["MEHO_REALM_ADMIN_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{_RELEASE}-core-keycloak-admin",
        "key": "client_secret",
        "optional": True,
    }
    plan = Plan.model_validate_json(env["MEHO_CORE_KEYCLOAK_PLAN"]["value"])
    assert plan.keycloak_url == f"http://{_RELEASE}-keycloak:8080/auth"


def test_keycloak_network_policy_admits_the_check_pod() -> None:
    docs = _render(*_CORE_ON, "--set", "coreKeycloak.networkPolicy.enabled=true")
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == f"{_RELEASE}-keycloak"
    )
    job = _check_job(docs)
    assert job is not None
    labels = job["spec"]["template"]["metadata"]["labels"]
    (rule,) = policy["spec"]["ingress"]
    assert any(
        "podSelector" in peer and peer["podSelector"]["matchLabels"].items() <= labels.items()
        for peer in rule["from"]
    )


def test_a_long_release_name_keeps_the_role_in_the_job_name() -> None:
    release = "a" * 53
    job = _check_job(_render(*_CORE_ON, release=release))
    assert job is not None
    name = job["metadata"]["name"]
    assert len(name) <= 63
    assert name.endswith("-check-manage-realm")
