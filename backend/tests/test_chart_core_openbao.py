# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in OpenBao subchart (#4044).

Off by default: the chart then renders no OpenBao object at all, so a
bring-your-own install renders what it rendered before. The parent's
``coreVault:`` block maps onto ``charts/openbao/`` through a dependency
alias, and the subchart's own schema checks every value when it is on.
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

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"

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

_ON = ["--set", "coreVault.enabled=true"]

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


def _is_openbao(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/name") == "openbao")


def test_off_by_default_renders_nothing() -> None:
    docs = _render()
    assert not [d for d in docs if _is_openbao(d)]
    # Saying "off" out loud renders the same objects as leaving it out.
    explicit = _render("--set", "coreVault.enabled=false")
    assert [(d["kind"], d["metadata"]["name"]) for d in explicit] == [
        (d["kind"], d["metadata"]["name"]) for d in docs
    ]


def test_on_runs_the_core_secrets_job() -> None:
    names = {(d["kind"], d["metadata"]["name"]) for d in _render(*_ON)}
    assert ("Job", "test-core-secrets") in names
    assert ("ServiceAccount", "test-core-bootstrap") in names


@pytest.mark.parametrize(
    ("extra", "messages"),
    [
        (["--set", "coreVault.binary=consul"], ("binary",)),
        (["--set", "coreVault.image.digest=sha256:xyz"], ("digest",)),
        (["--set", "coreVault.persistence.size=big"], ("size",)),
        (["--set", "coreVault.authDelegator=yes"], ("authDelegator",)),
        (["--set", "coreVault.unknownKey=1"], ("unknownKey",)),
    ],
    ids=["bad-binary", "bad-digest", "bad-disk-size", "auth-delegator-not-bool", "unknown-key"],
)
def test_bad_values_fail_at_render(extra: list[str], messages: tuple[str, ...]) -> None:
    result = _run(*_ON, *extra)
    assert result.returncode != 0
    assert any(m in result.stderr for m in messages), result.stderr
