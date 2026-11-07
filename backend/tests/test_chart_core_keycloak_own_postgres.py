# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks: the built-in Keycloak on your own PostgreSQL.

The install turns the built-in PostgreSQL off (``corePostgres.enabled:
false``) and keeps the built-in Keycloak. Keycloak then needs its own
database and role on your server. There are two ways: a hook Job makes them
(``coreKeycloak.database.create``), or you name them in values. Skips where
``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"
_TENANT = str(uuid.UUID(int=0x1F2E3D4C5B6A79881F2E3D4C5B6A7988))
_RELEASE = "test"

_OWN_POSTGRES = [
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "ingress.tls.secretName=meho-tls",
    "--set",
    "corePostgres.enabled=false",
    "--set",
    "postgres.credentialsSecret=meho-postgres",
    "--set",
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    f"coreKeycloak.firstTenantId={_TENANT}",
    "--set",
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "coreVault.enabled=false",
    "--set",
    "vault.address=https://vault.test",
    "--set",
    "config.vaultAddr=https://vault.test",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
]

_HOST = ["--set", "coreKeycloak.database.host=db.example.test"]
_CREATE = [
    *_HOST,
    "--set",
    "coreKeycloak.database.create.enabled=true",
    "--set",
    "coreKeycloak.database.create.adminSecret.name=db-admin",
]
_NAMED = [
    *_HOST,
    "--set",
    "coreKeycloak.database.passwordSecret.name=kc-db-password",
    "--set",
    "coreKeycloak.database.passwordSecret.key=pw",
    "--set",
    "coreKeycloak.database.name=kcdb",
    "--set",
    "coreKeycloak.database.user=kcuser",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", _RELEASE, str(_CHART_DIR), *_OWN_POSTGRES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _statefulset(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"].endswith("keycloak")
    )


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container["env"]}


def _database_job(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    jobs = [
        d
        for d in docs
        if d["kind"] == "Job" and d["metadata"]["name"].endswith("-keycloak-database")
    ]
    return jobs[0] if jobs else None


def _plan(docs: list[dict[str, Any]]) -> dict[str, Any]:
    job = next(
        d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-core-secrets")
    )
    env = _env(job["spec"]["template"]["spec"]["containers"][0])
    plan: dict[str, Any] = json.loads(env["MEHO_CORE_SECRETS_PLAN"]["value"])
    return plan


def test_chart_makes_the_database_with_a_job() -> None:
    docs = _render(*_CREATE)
    job = _database_job(docs)
    assert job is not None
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    # After the secrets Job (-30), before the migration Job (-10).
    assert -30 < int(annotations["helm.sh/hook-weight"]) < -10
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = _env(container)
    assert env["PGHOST"]["value"] == "db.example.test"
    assert env["PGUSER"]["valueFrom"]["secretKeyRef"] == {"name": "db-admin", "key": "username"}
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "db-admin", "key": "password"}
    # The role's password is the core Secret that the secrets Job makes.
    assert env["KC_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{_RELEASE}-core-postgres-keycloak",
        "key": "password",
    }
    script = "\n".join(container["command"])
    assert "CREATE ROLE" in script
    assert "CREATE DATABASE" in script
    assert "WHERE NOT EXISTS" in script  # safe to run twice


def test_job_password_is_not_on_a_command_line_or_in_the_manifest() -> None:
    job = _database_job(_render(*_CREATE))
    assert job is not None
    container = job["spec"]["template"]["spec"]["containers"][0]
    script = "\n".join(container["command"])
    assert "KC_DB_PASSWORD" in script  # read from the environment by psql
    assert "-v pw=" not in script
    assert all("value" not in e for e in container["env"] if "PASSWORD" in e["name"])


def test_secrets_job_makes_the_role_password_and_checks_the_admin_secret() -> None:
    plan = _plan(_render(*_CREATE))
    made = {e["name"]: e["kind"] for e in plan["make"]}
    assert made[f"{_RELEASE}-core-postgres-keycloak"] == "postgres-keycloak"
    # No built-in PostgreSQL: none of its Secrets.
    assert f"{_RELEASE}-core-postgres-superuser" not in made
    checks = {c["name"]: c["keys"] for c in plan["check"]}
    assert checks["db-admin"] == ["username", "password"]


def test_keycloak_pod_reads_the_database_from_values_when_you_name_it() -> None:
    docs = _render(*_NAMED)
    assert _database_job(docs) is None
    pod = _statefulset(docs)["spec"]["template"]["spec"]
    env = _env(pod["containers"][0])
    assert env["KC_DB_URL_HOST"]["value"] == "db.example.test"
    assert env["KC_DB_URL_DATABASE"]["value"] == "kcdb"
    assert env["KC_DB_USERNAME"]["value"] == "kcuser"
    assert env["KC_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "kc-db-password",
        "key": "pw",
    }
    wait = "\n".join(pod["initContainers"][0]["command"])
    assert "-U kcuser -d kcdb" in wait


def test_your_own_password_secret_is_checked_not_made() -> None:
    plan = _plan(_render(*_NAMED))
    assert f"{_RELEASE}-core-postgres-keycloak" not in {e["name"] for e in plan["make"]}
    checks = {c["name"]: c["keys"] for c in plan["check"]}
    assert checks["kc-db-password"] == ["pw"]


def test_tls_mode_reaches_keycloak_and_the_job() -> None:
    docs = _render(*_CREATE, "--set", "coreKeycloak.database.sslMode=verify-full")
    env = _env(_statefulset(docs)["spec"]["template"]["spec"]["containers"][0])
    assert env["KC_DB_URL_PROPERTIES"]["value"] == "?sslmode=verify-full"
    job = _database_job(docs)
    assert job is not None
    assert (
        _env(job["spec"]["template"]["spec"]["containers"][0])["PGSSLMODE"]["value"]
        == "verify-full"
    )


def test_no_tls_setting_without_a_tls_mode() -> None:
    env = _env(_statefulset(_render(*_NAMED))["spec"]["template"]["spec"]["containers"][0])
    assert "KC_DB_URL_PROPERTIES" not in env


def test_database_job_is_not_a_backplane_pod_and_keycloak_policy_has_no_egress() -> None:
    docs = _render(*_CREATE, "--set", "coreKeycloak.networkPolicy.enabled=true")
    job = _database_job(docs)
    assert job is not None
    labels = job["spec"]["template"]["metadata"]["labels"]
    # The backplane's NetworkPolicy selects `app.kubernetes.io/name: meho`.
    assert "app.kubernetes.io/name" not in labels
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-keycloak")
    )
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    # The backplane's own database rule still uses the address range.
    backplane = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["metadata"]["labels"].get("app.kubernetes.io/name") == "meho"
    )
    assert "10.0.1.0/24" in json.dumps(backplane["spec"]["egress"])


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            ["--set", "coreKeycloak.database.passwordSecret.name=kc-pw"],
            "coreKeycloak.database.host is empty",
        ),
        (_HOST, "coreKeycloak.database.passwordSecret.name is empty"),
        (
            [*_HOST, "--set", "coreKeycloak.database.create.enabled=true"],
            "coreKeycloak.database.create.adminSecret.name is empty",
        ),
        (
            [
                *_HOST,
                "--set",
                "coreKeycloak.database.create.enabled=true",
                "--set",
                f"coreKeycloak.database.create.adminSecret.name={_RELEASE}-core-keycloak-db-admin",
            ],
            "must not be",
        ),
        (
            [*_HOST, "--set", "coreKeycloak.database.passwordSecret.name=Bad_Name"],
            "passwordSecret",
        ),
        ([*_NAMED, "--set", "coreKeycloak.database.sslMode=maybe"], "sslMode"),
        ([*_NAMED, "--set", "coreKeycloak.database.user=Kc-User"], "user"),
    ],
    ids=[
        "no-host",
        "no-password-secret",
        "no-admin-secret",
        "reserved-admin-secret-name",
        "bad-secret-name",
        "bad-tls-mode",
        "bad-user",
    ],
)
def test_bad_values_stop_the_render_with_a_plain_message(extra: list[str], message: str) -> None:
    result = _run(*extra)
    assert result.returncode != 0
    assert message in result.stderr, result.stderr


@pytest.mark.parametrize(
    "setting",
    [
        "coreKeycloak.database.name=other",
        "coreKeycloak.database.user=other",
        "coreKeycloak.database.passwordSecret.name=other",
        "coreKeycloak.database.create.enabled=true",
        "coreKeycloak.database.sslMode=require",
    ],
)
def test_database_keys_are_refused_with_the_built_in_postgres(setting: str) -> None:
    built_in = [
        "--set",
        "corePostgres.enabled=true",
        "--set",
        "postgres.credentialsSecret=",
        "--set",
        setting,
    ]
    result = _run(*built_in)
    assert result.returncode != 0
    assert "only for your own PostgreSQL" in result.stderr, result.stderr


def test_built_in_postgres_keeps_the_fixed_keycloak_settings() -> None:
    docs = _render("--set", "corePostgres.enabled=true", "--set", "postgres.credentialsSecret=")
    env = _env(_statefulset(docs)["spec"]["template"]["spec"]["containers"][0])
    assert env["KC_DB_URL_HOST"]["value"] == f"{_RELEASE}-postgres"
    assert env["KC_DB_URL_DATABASE"]["value"] == "keycloak"
    assert env["KC_DB_USERNAME"]["value"] == "keycloak"
    assert env["KC_DB_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{_RELEASE}-core-postgres-keycloak",
        "key": "password",
    }
    assert _database_job(docs) is None
