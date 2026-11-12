# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The chart renders ``config.sessionStore`` into the session store variables.

A set value renders its ``SESSION_STORE_*`` key; an empty value renders
nothing, so the default in ``session_store/config.py`` applies. A wrong
value fails the schema check.

Skips when ``helm`` is absent; CI sets ``MEHO_REQUIRE_HELM=1`` so there
it fails instead.
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_POLICY_DIR = _CHART_DIR / "files" / "openbao" / "policies"
_BUILT_IN_PATH = "secret/data/services/session-store"
_BASE_OVERRIDES = [
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

_helm_required = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _helm_template(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _config_map_data(rendered: str) -> dict[str, Any]:
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if "KEYCLOAK_ISSUER_URL" in data:
                return dict(data)
    raise AssertionError("no backplane ConfigMap rendered")


def _session_store_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key.startswith("SESSION_STORE_")}


@_helm_required
def test_the_three_needed_values_render() -> None:
    result = _helm_template(
        "--set",
        "config.sessionStore.endpoint=https://objects.example.com",
        "--set",
        "config.sessionStore.bucket=meho-sessions",
        "--set",
        f"config.sessionStore.credentialsVaultPath={_BUILT_IN_PATH}",
    )
    assert result.returncode == 0, result.stderr
    assert _session_store_keys(_config_map_data(result.stdout)) == {
        "SESSION_STORE_ENDPOINT": "https://objects.example.com",
        "SESSION_STORE_BUCKET": "meho-sessions",
        "SESSION_STORE_CREDENTIALS_VAULT_PATH": _BUILT_IN_PATH,
    }


@_helm_required
def test_every_value_renders_its_variable() -> None:
    values = {
        "region": ("SESSION_STORE_REGION", "eu-central-1"),
        "retentionDaysDefault": ("SESSION_STORE_RETENTION_DAYS_DEFAULT", "30"),
        "maxUploadBytes": ("SESSION_STORE_MAX_UPLOAD_BYTES", "1048576"),
        "maxUnpackedBytes": ("SESSION_STORE_MAX_UNPACKED_BYTES", "8388608"),
        "maxFiles": ("SESSION_STORE_MAX_FILES", "100"),
        "cleanupEnabled": ("SESSION_STORE_CLEANUP_ENABLED", "false"),
        "cleanupIntervalSeconds": ("SESSION_STORE_CLEANUP_INTERVAL_SECONDS", "60"),
    }
    args: list[str] = []
    for key, (_, value) in values.items():
        args += ["--set-string", f"config.sessionStore.{key}={value}"]
    result = _helm_template(*args)
    assert result.returncode == 0, result.stderr
    assert _session_store_keys(_config_map_data(result.stdout)) == dict(values.values())


@_helm_required
def test_no_values_render_no_variable() -> None:
    result = _helm_template()
    assert result.returncode == 0, result.stderr
    assert _session_store_keys(_config_map_data(result.stdout)) == {}


@_helm_required
def test_a_null_block_renders_no_variable() -> None:
    result = _helm_template("--set", "config.sessionStore=null")
    assert result.returncode == 0, result.stderr
    assert _session_store_keys(_config_map_data(result.stdout)) == {}


@_helm_required
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--set-string", "maxFiles=many"),
        ("--set", "maxFiles=2000"),
        ("--set-string", "maxFiles=0"),
        ("--set-string", "maxUploadBytes=-1"),
        ("--set-string", "retentionDaysDefault=1.5"),
        ("--set-string", "cleanupIntervalSeconds=59"),
        ("--set-string", "cleanupEnabled=yes"),
        ("--set", "cleanupEnabled=true"),
        ("--set-string", "endpoint=objects.example.com"),
        ("--set-string", "unknownSetting=x"),
    ],
)
def test_a_wrong_value_fails_the_schema(flag: str, value: str) -> None:
    result = _helm_template(flag, f"config.sessionStore.{value}")
    assert result.returncode != 0
    assert "config.sessionStore" in result.stderr


def _grants(policy: str) -> dict[str, set[str]]:
    return {
        path: set(re.findall(r'"([^"]+)"', caps))
        for path, caps in re.findall(r'path "([^"]+)" \{\s*capabilities = \[([^\]]*)\]', policy)
    }


def test_the_built_in_scheduler_policy_reads_only_the_documented_path() -> None:
    grants = _grants((_POLICY_DIR / "meho-scheduler.hcl").read_text())
    assert grants[_BUILT_IN_PATH] == {"read"}


def _matches(pattern: str, path: str) -> bool:
    """Vault policy match: ``+`` is one segment, a last ``*`` is any rest."""
    regex = ""
    for part in re.split(r"(\{\{[^}]*\}\}|\+|\*$)", pattern):
        if part == "+" or part.startswith("{{"):
            regex += "[^/]+"
        elif part == "*":
            regex += ".*"
        else:
            regex += re.escape(part)
    return re.fullmatch(regex, path) is not None


@pytest.mark.parametrize(
    "policy",
    ["meho-mcp", "meho-tenant-read-only", "meho-tenant-operator", "meho-tenant-admin"],
)
def test_the_documented_path_is_not_open_to_users(policy: str) -> None:
    grants = _grants((_POLICY_DIR / f"{policy}.hcl").read_text())
    assert grants, policy
    assert not [pattern for pattern in grants if _matches(pattern, _BUILT_IN_PATH)]


def test_the_matcher_follows_vault_rules() -> None:
    assert _matches("secret/data/meho/*", "secret/data/meho/a/b")
    assert _matches("secret/data/agents/+/credentials", "secret/data/agents/x/credentials")
    assert not _matches("secret/data/agents/+/credentials", "secret/data/agents/x/y/credentials")
    assert _matches("secret/data/tenants/{{identity.x}}/*", "secret/data/tenants/t1/k")
