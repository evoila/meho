# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the optional ``VAULT_ADDR`` key (#2277).

Initiative #2227's promise is that a GCP-native adopter onboards with
zero new secret infrastructure — no Vault. The configmap therefore
renders ``VAULT_ADDR`` **only when** ``config.vaultAddr`` is non-empty,
so a ``gsm``-backend install (which leaves ``config.vaultAddr`` blank)
injects no empty ``VAULT_ADDR`` into the pod. A Vault-backend install
still emits it verbatim.

The authoritative chart gate is ``.github/workflows/chart.yml`` (lint +
``helm template`` + ``kubeconform``). This test mirrors the assertion at
the unit layer so the regression is catchable from ``pytest`` on any
machine with ``helm`` installed; it skips cleanly where ``helm`` is
absent (the backend unit-test sandbox does not ship it — the workflow
gate covers that environment).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_GSM_VALUES = _REPO_ROOT / "deploy" / "values-examples" / "values-gsm-example.yaml"

# Minimal chassis-required overrides, deliberately WITHOUT
# ``config.vaultAddr`` — each test sets (or omits) it to exercise the
# conditional.
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
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set",
    "networkPolicy.keycloakCIDR=10.0.3.0/24",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm not installed in this sandbox; chart.yml workflow gate covers CI",
)


def _render(*extra: str) -> str:
    """Return ``helm template`` output for the chart with the given overrides."""
    result = subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _lint(*extra: str) -> None:
    """Fail the test if ``helm lint`` rejects the chart with the given overrides."""
    subprocess.run(
        ["helm", "lint", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=True,
    )


def test_vault_addr_emitted_when_configured() -> None:
    """A Vault-backend install (``config.vaultAddr`` set) emits the key verbatim."""
    rendered = _render("--set", "config.vaultAddr=https://vault.test")
    assert 'VAULT_ADDR: "https://vault.test"' in rendered


def test_vault_addr_omitted_when_blank() -> None:
    """A ``gsm`` install leaves ``config.vaultAddr`` blank → no ``VAULT_ADDR`` key."""
    rendered = _render(
        "--set",
        "config.vaultAddr=",
        "--set",
        "config.credentialBackend=gsm",
        "--set",
        "config.gsmProject=my-proj",
        "--set",
        "gsm.enabled=true",
        "--set",
        "gsm.project=my-proj",
    )
    assert "VAULT_ADDR:" not in rendered


def test_vault_addr_omitted_for_gsm_example_values_file() -> None:
    """The shipped GSM-only values example renders no ``VAULT_ADDR`` key."""
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(_CHART_DIR),
            "-f",
            str(_GSM_VALUES),
            "--set",
            "image.tag=test",
            "--set",
            "ingress.tls.secretName=meho-tls",
            "--set",
            "postgres.credentialsSecret=meho-postgres",
            "--set",
            "networkPolicy.postgresCIDR=10.0.1.0/24",
            "--set",
            "networkPolicy.keycloakCIDR=10.0.3.0/24",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "VAULT_ADDR:" not in result.stdout


def test_helm_lint_passes_for_vault_and_gsm() -> None:
    """``helm lint`` passes both with and without ``config.vaultAddr`` set."""
    _lint("--set", "config.vaultAddr=https://vault.test")
    _lint(
        "--set",
        "config.vaultAddr=",
        "--set",
        "config.credentialBackend=gsm",
        "--set",
        "config.gsmProject=my-proj",
        "--set",
        "gsm.enabled=true",
        "--set",
        "gsm.project=my-proj",
    )
