# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The MEHO Deployment allows more time to roll out than the startup probe.

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


def _meho_deployment(rendered: str) -> dict[str, Any]:
    for doc in yaml.safe_load_all(rendered):
        if (
            isinstance(doc, dict)
            and doc.get("kind") == "Deployment"
            and doc["metadata"]["name"] == "test-meho"
        ):
            return dict(doc)
    raise AssertionError("no MEHO Deployment rendered")


def test_default_deadline_is_above_the_startup_budget() -> None:
    result = _helm_template()
    assert result.returncode == 0, result.stderr
    spec = _meho_deployment(result.stdout)["spec"]
    probe = spec["template"]["spec"]["containers"][0]["startupProbe"]
    budget = probe["failureThreshold"] * probe["periodSeconds"]
    assert spec["progressDeadlineSeconds"] > budget


def test_operator_can_set_the_deadline() -> None:
    result = _helm_template("--set", "progressDeadlineSeconds=2400")
    assert result.returncode == 0, result.stderr
    spec = _meho_deployment(result.stdout)["spec"]
    assert spec["progressDeadlineSeconds"] == 2400


def test_a_deadline_of_zero_is_refused() -> None:
    result = _helm_template("--set", "progressDeadlineSeconds=0")
    assert result.returncode != 0
    assert "progressDeadlineSeconds" in result.stderr
