# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The chart renders the knowledge settings into the config map.

The shared knowledge scope (``KB_SHARED_TENANT_IDS``) and the weekly
knowledge upkeep (``KNOWLEDGE_UPKEEP_ENABLED``,
``KNOWLEDGE_UPKEEP_INTERVAL_SECONDS``). With no values set, the chart
must give the same settings as the backplane's own defaults.

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

from meho_backplane.settings import Settings

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


_TENANT_A = "11111111-1111-4111-8111-111111111111"
_TENANT_B = "22222222-2222-4222-8222-222222222222"


def _config_map_data(rendered: str) -> dict[str, Any]:
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            data = doc.get("data") or {}
            if "KB_SHARED_TENANT_IDS" in data:
                return dict(data)
    raise AssertionError("no ConfigMap with KB_SHARED_TENANT_IDS rendered")


def test_values_set_render_all_three_keys() -> None:
    result = _helm_template(
        "--set-string",
        f"config.kbSharedTenantIds={_TENANT_A}\\,{_TENANT_B}",
        "--set-string",
        "config.knowledgeUpkeepEnabled=false",
        "--set",
        "config.knowledgeUpkeepIntervalSeconds=3600",
    )
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    assert data["KB_SHARED_TENANT_IDS"] == f"{_TENANT_A},{_TENANT_B}"
    assert data["KNOWLEDGE_UPKEEP_ENABLED"] == "false"
    assert data["KNOWLEDGE_UPKEEP_INTERVAL_SECONDS"] == "3600"


def test_no_values_give_the_backplane_defaults() -> None:
    result = _helm_template()
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    assert data["KB_SHARED_TENANT_IDS"] == ""
    assert data["KNOWLEDGE_UPKEEP_ENABLED"] == "true"
    assert data["KNOWLEDGE_UPKEEP_INTERVAL_SECONDS"] == "604800"

    fields = Settings.model_fields
    with_chart = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        keycloak_issuer_url="https://keycloak.test/realms/meho",
        keycloak_audience="meho-backplane",
        kb_shared_tenant_ids=data["KB_SHARED_TENANT_IDS"],
        knowledge_upkeep_enabled=data["KNOWLEDGE_UPKEEP_ENABLED"] == "true",
        knowledge_upkeep_interval_seconds=int(data["KNOWLEDGE_UPKEEP_INTERVAL_SECONDS"]),
    )
    assert with_chart.kb_shared_tenant_ids == fields["kb_shared_tenant_ids"].default
    assert with_chart.kb_shared_tenant_set == frozenset()
    assert with_chart.knowledge_upkeep_enabled == fields["knowledge_upkeep_enabled"].default
    assert (
        with_chart.knowledge_upkeep_interval_seconds
        == fields["knowledge_upkeep_interval_seconds"].default
    )


@pytest.mark.parametrize("interval", ["59", "604801", "0"])
def test_interval_out_of_range_fails_the_schema(interval: str) -> None:
    result = _helm_template("--set", f"config.knowledgeUpkeepIntervalSeconds={interval}")
    assert result.returncode != 0
    assert "knowledgeUpkeepIntervalSeconds" in result.stderr


@pytest.mark.parametrize("interval", ["60", "604800"])
def test_interval_at_the_limits_renders(interval: str) -> None:
    result = _helm_template("--set", f"config.knowledgeUpkeepIntervalSeconds={interval}")
    assert result.returncode == 0, result.stderr
    assert _config_map_data(result.stdout)["KNOWLEDGE_UPKEEP_INTERVAL_SECONDS"] == interval


def test_interval_as_text_fails_the_schema() -> None:
    result = _helm_template("--set-string", "config.knowledgeUpkeepIntervalSeconds=weekly")
    assert result.returncode != 0
    assert "knowledgeUpkeepIntervalSeconds" in result.stderr


def test_upkeep_switch_takes_only_true_or_false() -> None:
    result = _helm_template("--set-string", "config.knowledgeUpkeepEnabled=yes")
    assert result.returncode != 0
    assert "knowledgeUpkeepEnabled" in result.stderr


@pytest.mark.parametrize("ids", ["not-a-uuid", f"{_TENANT_A}\\,team-a", f"{_TENANT_A}\\,"])
def test_shared_tenant_ids_must_be_tenant_ids(ids: str) -> None:
    result = _helm_template("--set-string", f"config.kbSharedTenantIds={ids}")
    assert result.returncode != 0
    assert "kbSharedTenantIds" in result.stderr
