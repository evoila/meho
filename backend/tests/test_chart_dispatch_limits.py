# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The chart renders the per-person request limits into the config map.

Skips when ``helm`` is absent; CI sets ``MEHO_REQUIRE_HELM=1`` so there
it fails instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
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

pytestmark = pytest.mark.skipif(
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
            if "DISPATCH_RATE_LIMIT_PER_MINUTE" in data:
                return dict(data)
    raise AssertionError("no ConfigMap with DISPATCH_RATE_LIMIT_PER_MINUTE rendered")


def test_limits_are_on_by_default() -> None:
    result = _helm_template()
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    assert data["DISPATCH_RATE_LIMIT_PER_MINUTE"] == "600"
    assert data["DISPATCH_MAX_CONCURRENT_OPS"] == "16"
    assert data["MCP_SESSION_START_LIMIT_PER_MINUTE"] == "60"


def test_limits_can_be_turned_off() -> None:
    result = _helm_template(
        "--set-string",
        "config.dispatchRateLimitPerMinute=0",
        "--set-string",
        "config.dispatchMaxConcurrentOps=0",
        "--set-string",
        "config.mcpSessionStartLimitPerMinute=0",
    )
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    assert data["DISPATCH_RATE_LIMIT_PER_MINUTE"] == "0"
    assert data["DISPATCH_MAX_CONCURRENT_OPS"] == "0"
    assert data["MCP_SESSION_START_LIMIT_PER_MINUTE"] == "0"


def test_non_numeric_limit_fails_at_install() -> None:
    result = _helm_template("--set", "config.dispatchRateLimitPerMinute=many")
    assert result.returncode != 0
    assert "dispatchRateLimitPerMinute" in result.stderr
