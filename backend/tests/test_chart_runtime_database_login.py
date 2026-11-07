# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the database login of the running backplane.

The owner login (``postgres.credentialsSecret``) runs the migrations. The
running backplane can use a second, narrower login, the runtime role; the
migration container gets its name as ``DATABASE_RUNTIME_ROLE`` and grants
it its rights (``backend/src/meho_backplane/db/runtime_role.py``).

* Bring your own PostgreSQL with ``postgres.runtime`` empty: one login for
  everything, and no ``DATABASE_RUNTIME_ROLE`` anywhere (the render of this
  mode must not change, ``test_chart_bring_your_own_render.py``).
* Bring your own PostgreSQL with ``postgres.runtime.role`` and
  ``postgres.runtime.credentialsSecret``: the backplane reads
  ``DATABASE_URL`` from the runtime Secret, the hook Job keeps the owner
  login and gets the role name.
* Built-in PostgreSQL: the install makes the role ``meho_app`` and the
  Secret ``<release>-core-postgres-meho-app``; the backplane uses it and
  the ``migrate`` init container gets the role name.
* The schema refuses a runtime Secret without a role, with your own
  PostgreSQL a role without a Secret, a role name that is not a plain
  lower-case identifier, and with the built-in PostgreSQL a role other than
  ``meho_app``. The render refuses, with the built-in PostgreSQL, a runtime
  Secret other than ``<release>-core-postgres-meho-app``: the PostgreSQL
  pod mounts only that name, and the secrets Job would not make it.

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

from meho_backplane.core.secrets import Plan

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"

_BASE_OVERRIDES = [
    # Bring-your-own unless a test turns a built-in part on (#4047).
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
_OWN_DB = ["--set", "postgres.credentialsSecret=meho-postgres"]
_OWN_RUNTIME = [
    "--set",
    "postgres.runtime.role=meho_app",
    "--set",
    "postgres.runtime.credentialsSecret=meho-postgres-app",
]
_CORE_ON = [
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "postgres.credentialsSecret=test-core-postgres-meho",
]

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


def _backplane_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (deployment,) = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["labels"]["app.kubernetes.io/name"] == "meho"
    ]
    spec: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return spec


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {e["name"]: e for e in container["env"]}


def _database_secret(container: dict[str, Any]) -> str:
    name: str = _env(container)["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
    assert _env(container)["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["key"] == "url"
    return name


def _migrate_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The hook Job's container, or the init container with the built-in database."""
    jobs = [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-migrate")]
    if jobs:
        container: dict[str, Any] = jobs[0]["spec"]["template"]["spec"]["containers"][0]
        return container
    (init,) = _backplane_pod(docs)["initContainers"]
    return init


def test_one_login_for_everything_by_default() -> None:
    docs = _render(*_OWN_DB)

    backplane = _backplane_pod(docs)["containers"][0]
    migrate = _migrate_container(docs)
    assert _database_secret(backplane) == "meho-postgres"
    assert _database_secret(migrate) == "meho-postgres"
    assert "DATABASE_RUNTIME_ROLE" not in _env(migrate)
    assert "DATABASE_RUNTIME_ROLE" not in _env(backplane)


def test_own_database_with_a_runtime_login() -> None:
    docs = _render(*_OWN_DB, *_OWN_RUNTIME)

    backplane = _backplane_pod(docs)["containers"][0]
    migrate = _migrate_container(docs)
    assert _database_secret(backplane) == "meho-postgres-app"
    assert _database_secret(migrate) == "meho-postgres"
    assert _env(migrate)["DATABASE_RUNTIME_ROLE"] == {
        "name": "DATABASE_RUNTIME_ROLE",
        "value": "meho_app",
    }
    assert "DATABASE_RUNTIME_ROLE" not in _env(backplane)


def test_built_in_database_makes_the_runtime_login() -> None:
    docs = _render(*_CORE_ON)

    backplane = _backplane_pod(docs)["containers"][0]
    migrate = _migrate_container(docs)
    assert migrate["name"] == "migrate"
    assert _database_secret(backplane) == "test-core-postgres-meho-app"
    assert _database_secret(migrate) == "test-core-postgres-meho"
    assert _env(migrate)["DATABASE_RUNTIME_ROLE"]["value"] == "meho_app"

    # The secrets Job makes the Secret with the database host, and the hook
    # Role may read and patch it.
    job = next(
        d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "test-core-secrets"
    )
    plan = Plan.model_validate_json(
        _env(job["spec"]["template"]["spec"]["containers"][0])["MEHO_CORE_SECRETS_PLAN"]["value"]
    )
    entry = next(e for e in plan.make if e.kind == "postgres-meho-app")
    assert (entry.name, entry.host) == ("test-core-postgres-meho-app", "test-postgres")
    role = next(
        d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "test-core-bootstrap"
    )
    patch_rule = next(r for r in role["rules"] if r["verbs"] == ["get", "patch"])
    assert "test-core-postgres-meho-app" in patch_rule["resourceNames"]

    # The PostgreSQL pod mounts the new Secret for its SQL file.
    sts = next(
        d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "test-postgres"
    )
    volumes = {v["name"]: v for v in sts["spec"]["template"]["spec"]["volumes"]}
    assert volumes["meho-app"]["secret"]["secretName"] == "test-core-postgres-meho-app"


def test_built_in_database_accepts_the_same_names_and_nothing_else() -> None:
    same = _render(
        *_CORE_ON,
        "--set",
        "postgres.runtime.role=meho_app",
        "--set",
        "postgres.runtime.credentialsSecret=test-core-postgres-meho-app",
    )
    assert _database_secret(_backplane_pod(same)["containers"][0]) == "test-core-postgres-meho-app"

    other = _run(
        *_CORE_ON,
        "--set",
        "postgres.runtime.role=other_app",
        "--set",
        "postgres.runtime.credentialsSecret=my-app-login",
    )
    assert other.returncode != 0
    assert "allOf" in other.stderr


def test_built_in_database_refuses_another_runtime_secret() -> None:
    """The PostgreSQL pod mounts ``<release>-core-postgres-meho-app`` only.

    With another name the secrets Job would check that Secret instead of
    making the core one, and the database pod would wait forever. The render
    stops with one sentence that names the key and the expected Secret.
    """
    result = _run(
        *_CORE_ON,
        "--set",
        "postgres.runtime.role=meho_app",
        "--set",
        "postgres.runtime.credentialsSecret=my-app-login",
    )
    assert result.returncode != 0
    assert (
        'postgres.runtime.credentialsSecret must be empty or "test-core-postgres-meho-app"'
        in result.stderr
    )

    # The role name alone is the install's own default and renders.
    docs = _render(*_CORE_ON, "--set", "postgres.runtime.role=meho_app")
    assert _database_secret(_backplane_pod(docs)["containers"][0]) == "test-core-postgres-meho-app"


def test_schema_refuses_a_role_without_a_secret_with_your_own_database() -> None:
    """A role alone would get its rights while the backplane keeps the owner login."""
    no_secret = _run(*_OWN_DB, "--set", "postgres.runtime.role=meho_app")
    assert no_secret.returncode != 0
    assert "postgres.runtime.credentialsSecret" in no_secret.stderr
    assert "allOf" in no_secret.stderr


def test_schema_refuses_a_secret_without_a_role_and_a_bad_role_name() -> None:
    no_role = _run(*_OWN_DB, "--set", "postgres.runtime.credentialsSecret=meho-postgres-app")
    assert no_role.returncode != 0
    assert "allOf" in no_role.stderr

    for bad in ("Meho_App", "meho-app", "meho app", "1app"):
        result = _run(
            *_OWN_DB,
            "--set",
            f"postgres.runtime.role={bad}",
            "--set",
            "postgres.runtime.credentialsSecret=meho-postgres-app",
        )
        assert result.returncode != 0, bad
        assert "postgres.runtime.role" in result.stderr, bad
