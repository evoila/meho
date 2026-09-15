# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the optional image-digest pin (#284 / F15b).

The image pipeline promotes a release by applying tags to the exact
manifest **digest** it scanned + cosign-signed. To let a deploy pin that
immutable, content-addressed digest instead of the mutable
``repository:tag`` reference, the chart grew an optional
``image.digest`` value: when it is set, the backplane Deployment **and**
the pre-install migration Job both render ``repository@digest``; when it
is empty (the default) both fall back to the schema-required
``repository:tag``. A ``sha256:<64 hex>`` schema pattern rejects a
malformed pin at ``helm template`` time rather than letting it reach a
Pod-create rejection.

These tests pin that contract at the unit layer. Two failure modes they
guard against, both silent:

* A future template edit dropping the ``{{- if .Values.image.digest }}``
  branch would reopen F15b — the deploy would resolve a mutable tag even
  when handed the scanned+signed digest — while the CI chart gate
  (``.github/workflows/chart.yml``), which renders only the tag path,
  stays green.
* Loosening the ``digest`` schema pattern would let a malformed pin
  through to Pod create.

The authoritative chart gate is ``.github/workflows/chart.yml`` (lint +
``helm template`` + ``kubeconform``); this test mirrors the digest
assertions at the ``pytest`` layer and skips cleanly where ``helm`` is
absent (the backend unit-test sandbox does not ship it — the workflow
gate covers that environment).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

# The chart's default image repository (deploy/charts/meho/values.yaml).
_REPO = "ghcr.io/evoila/meho"
# A well-formed manifest digest (sha256 of the empty string — 64 hex chars).
_VALID_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# Minimal chassis-required overrides that satisfy values.schema.json.
# `image.tag` is always set (schema-required) so the digest tests also
# prove the digest *wins* over a present tag.
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


def _helm_template(template: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run ``helm template --show-only <template>`` without raising."""
    return subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            *extra,
            "--show-only",
            template,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _render_image(template: str, *extra: str) -> str:
    """Return the sole container image of the rendered ``template``."""
    result = _helm_template(template, *extra)
    assert result.returncode == 0, result.stderr
    manifest = cast("dict[str, Any]", yaml.safe_load(result.stdout))
    containers = manifest["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, f"expected one container, got {len(containers)}"
    return cast("str", containers[0]["image"])


def test_deployment_pins_by_digest_when_set() -> None:
    """With ``image.digest`` set the Deployment renders ``repository@digest``.

    The digest wins even though ``image.tag`` is also set (base overrides),
    proving the precedence the F15b deploy pin depends on.
    """
    image = _render_image(
        "templates/deployment.yaml",
        "--set-string",
        f"image.digest={_VALID_DIGEST}",
    )
    assert image == f"{_REPO}@{_VALID_DIGEST}"


def test_migration_job_pins_by_digest_when_set() -> None:
    """The migration Job must pin the same content-addressed digest (#284).

    A tag on the Job could resolve to different bytes than the Deployment;
    the digest pin keeps the migration runner and the backplane on the
    exact same scanned+signed image.
    """
    image = _render_image(
        "templates/migration-job.yaml",
        "--set-string",
        f"image.digest={_VALID_DIGEST}",
    )
    assert image == f"{_REPO}@{_VALID_DIGEST}"


def test_deployment_falls_back_to_tag_without_digest() -> None:
    """With no digest (the default) the Deployment renders ``repository:tag``."""
    image = _render_image("templates/deployment.yaml")
    assert image == f"{_REPO}:test"


def test_migration_job_falls_back_to_tag_without_digest() -> None:
    """With no digest (the default) the migration Job renders ``repository:tag``."""
    image = _render_image("templates/migration-job.yaml")
    assert image == f"{_REPO}:test"


def test_malformed_digest_is_schema_rejected() -> None:
    """A digest that is not ``sha256:<64 hex>`` fails values-schema validation.

    The pin must fail loud at ``helm template`` time rather than producing
    an image reference Kubernetes rejects at Pod create.
    """
    result = _helm_template(
        "templates/deployment.yaml",
        "--set-string",
        "image.digest=not-a-valid-digest",
    )
    assert result.returncode != 0
    stderr = result.stderr.lower()
    assert "digest" in stderr
    assert "schema" in stderr or "pattern" in stderr
