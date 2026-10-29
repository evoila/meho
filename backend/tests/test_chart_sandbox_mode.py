# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for sandbox mode (#4622).

The default renders no sandbox setting. With ``sandboxMode.enabled=true``
the ConfigMap carries ``MEHO_SANDBOX_MODE`` and
``MEHO_SANDBOX_MODE_CONFIRM`` (and ``MEHO_SANDBOX_DELETE_GRANTS_FROM_API``
when ``sandboxMode.deleteGrantsFromApi`` is on too, #5251), the render fails when ``confirmHost`` is
not ``ingress.host`` (or is missing), and the install notes warn. Skips
where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"
_HOST = "meho.sandbox.test"

_BASE_OVERRIDES = [
    "--set",
    "image.tag=test",
    "--set",
    f"ingress.host={_HOST}",
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

_ON = ["--set", "sandboxMode.enabled=true", "--set", f"sandboxMode.confirmHost={_HOST}"]

_NOTES_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: notes-probe
data:
  notes: {{ tpl (.Files.Get "notes-probe.txt") . | quote }}
"""

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _helm_template(chart: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(chart), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _configmap_data(*extra: str) -> dict[str, Any]:
    result = _helm_template(_CHART_DIR, *extra)
    assert result.returncode == 0, result.stderr
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "test-meho-config":
            data: dict[str, Any] = doc["data"]
            return data
    raise AssertionError("backplane ConfigMap not found in the rendered chart")


def _render_notes(tmp_path: Path, *extra: str) -> str:
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "notes-probe.txt")
    (chart / "templates" / "zz-notes-probe.yaml").write_text(_NOTES_PROBE, encoding="utf-8")
    result = _helm_template(chart, "--show-only", "templates/zz-notes-probe.yaml", *extra)
    assert result.returncode == 0, result.stderr
    notes: str = yaml.safe_load(result.stdout)["data"]["notes"]
    return notes


def test_default_renders_no_sandbox_setting(tmp_path: Path) -> None:
    data = _configmap_data()
    assert not [key for key in data if key.startswith("MEHO_SANDBOX")]
    assert "sandbox mode is ON" not in _render_notes(tmp_path)


def test_enabled_renders_both_settings_and_warns(tmp_path: Path) -> None:
    data = _configmap_data(*_ON)
    assert data["MEHO_SANDBOX_MODE"] == "true"
    assert data["MEHO_SANDBOX_MODE_CONFIRM"] == _HOST
    notes = _render_notes(tmp_path, *_ON)
    assert "WARNING: sandbox mode is ON" in notes
    assert "disposable" in notes


def test_delete_grants_from_api_renders_only_with_the_mode_on() -> None:
    assert "MEHO_SANDBOX_DELETE_GRANTS_FROM_API" not in _configmap_data(*_ON)
    on = _configmap_data(*_ON, "--set", "sandboxMode.deleteGrantsFromApi=true")
    assert on["MEHO_SANDBOX_DELETE_GRANTS_FROM_API"] == "true"
    off = _configmap_data("--set", "sandboxMode.deleteGrantsFromApi=true")
    assert not [key for key in off if key.startswith("MEHO_SANDBOX")]


def test_confirm_host_must_equal_ingress_host() -> None:
    result = _helm_template(
        _CHART_DIR,
        "--set",
        "sandboxMode.enabled=true",
        "--set",
        "sandboxMode.confirmHost=other.sandbox.test",
    )
    assert result.returncode != 0
    assert "sandboxMode.confirmHost" in result.stderr
    assert "must equal ingress.host" in result.stderr


def test_confirm_host_is_required_when_enabled() -> None:
    result = _helm_template(_CHART_DIR, "--set", "sandboxMode.enabled=true")
    assert result.returncode != 0
    assert "sandboxMode" in result.stderr
