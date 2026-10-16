# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the approval custody key (#3712).

Every credential-write operation that must wait for approval needs
``APPROVAL_HANDOFF_ENCRYPTION_KEY``. Without it MEHO denies the operation
(#3537). The chart keeps ``approvalHandoff`` off by default, because a
bring-your-own-Secret install must point it at a Secret first. So the
default render must not stay silent: it must print a warning in the
install notes.

``helm template`` does not print ``NOTES.txt``, so the notes are rendered
through a throwaway ConfigMap in a temporary copy of the chart. That runs
the real template text with the real values. The test skips where
``helm`` is absent; CI sets ``MEHO_REQUIRE_HELM=1`` so there it fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_ENV_NAME = "APPROVAL_HANDOFF_ENCRYPTION_KEY"
_WARNING = "WARNING: approvalHandoff.enabled=false"

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

_ENABLED = [
    "--set",
    "approvalHandoff.enabled=true",
    "--set",
    "approvalHandoff.secretName=meho-approval-handoff",
]

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


def _backplane_env(*extra: str) -> list[dict[str, Any]]:
    result = _helm_template(_CHART_DIR, *extra)
    assert result.returncode == 0, result.stderr
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "test-meho":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            env: list[dict[str, Any]] = container.get("env", [])
            return env
    raise AssertionError("backplane Deployment not found in the rendered chart")


def _render_notes(tmp_path: Path, *extra: str) -> str:
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "notes-probe.txt")
    (chart / "templates" / "zz-notes-probe.yaml").write_text(_NOTES_PROBE, encoding="utf-8")
    result = _helm_template(chart, "--show-only", "templates/zz-notes-probe.yaml", *extra)
    assert result.returncode == 0, result.stderr
    notes: str = yaml.safe_load(result.stdout)["data"]["notes"]
    return notes


def test_default_install_renders_no_key_and_warns(tmp_path: Path) -> None:
    """Default values: no key env, so the install notes must warn about it."""
    assert _ENV_NAME not in {entry["name"] for entry in _backplane_env()}
    notes = _render_notes(tmp_path)
    assert _WARNING in notes
    assert "deny every credential-write operation" in notes
    assert "Fernet.generate_key()" in notes
    assert "features.approval_custody" in notes


def test_enabled_install_wires_key_from_secret_and_does_not_warn(tmp_path: Path) -> None:
    """Enabled: the key comes only from the Secret, and no warning prints."""
    env = {entry["name"]: entry for entry in _backplane_env(*_ENABLED)}
    assert env[_ENV_NAME] == {
        "name": _ENV_NAME,
        "valueFrom": {
            "secretKeyRef": {
                "name": "meho-approval-handoff",
                "key": "approval_handoff_encryption_key",
            }
        },
    }
    assert _WARNING not in _render_notes(tmp_path, *_ENABLED)


def test_enabled_without_secret_name_fails_at_render() -> None:
    """Turning custody on without a Secret name is refused by the schema."""
    result = _helm_template(_CHART_DIR, "--set", "approvalHandoff.enabled=true")
    assert result.returncode != 0
    assert "approvalHandoff" in result.stderr
