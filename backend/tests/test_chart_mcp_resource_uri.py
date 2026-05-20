# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the MCP audience defaults (G0.8-T4, #633).

The Helm chart must derive ``BACKPLANE_URL`` / ``MCP_RESOURCE_URI`` from
the Ingress host when the operator sets neither, so the documented
``${BACKPLANE_URL}/mcp`` default actually materialises for the common
ingress-fronted deploy. Without it the resolved MCP audience is empty
and every ``/mcp`` request 401s — the consumer dogfood signal this Task
fixes.

The authoritative chart gate is ``.github/workflows/chart.yml`` (lint +
``helm template`` + ``kubeconform`` + the grep assertions added by this
Task). This test mirrors the grep assertion at the unit layer so the
regression is also catchable from ``pytest`` on any machine that has
``helm`` installed; it skips cleanly where ``helm`` is absent (the
backend unit-test sandbox does not ship it — the workflow gate covers
that environment).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

# The same minimal override set the chart.yml workflow uses, minus the
# MCP-specific knobs (left unset on purpose — that is what exercises the
# Ingress-derived default).
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


def test_mcp_resource_uri_derived_from_ingress_host_when_unset() -> None:
    """No MCP-specific values set → both env vars derived from the Ingress."""
    rendered = _render()
    assert 'MCP_RESOURCE_URI: "https://meho.test/mcp"' in rendered
    assert 'BACKPLANE_URL: "https://meho.test"' in rendered


def test_explicit_backplane_url_overrides_ingress_derivation() -> None:
    """``config.backplaneUrl`` wins; the MCP URI derives from it."""
    rendered = _render("--set", "config.backplaneUrl=https://meho.example.com")
    assert 'BACKPLANE_URL: "https://meho.example.com"' in rendered
    assert 'MCP_RESOURCE_URI: "https://meho.example.com/mcp"' in rendered


def test_explicit_mcp_resource_uri_overrides_derivation() -> None:
    """``config.mcpResourceUri`` is emitted verbatim for non-default mounts."""
    rendered = _render("--set", "config.mcpResourceUri=https://meho.test/api/mcp")
    assert 'MCP_RESOURCE_URI: "https://meho.test/api/mcp"' in rendered


def test_no_mcp_env_keys_when_ingress_disabled_and_nothing_set() -> None:
    """No Ingress + nothing set → keys omitted (backend startup guard fires)."""
    rendered = _render("--set", "ingress.enabled=false")
    assert "MCP_RESOURCE_URI:" not in rendered
    assert "BACKPLANE_URL:" not in rendered
