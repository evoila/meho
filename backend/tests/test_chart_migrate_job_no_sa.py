# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the migrate hook's ServiceAccount (#2391).

The ``meho-migrate`` Job is a Helm ``pre-install,pre-upgrade`` hook. Helm
schedules hooks **before** it creates the chart's normal (non-hook)
resources — the ``meho`` ServiceAccount among them. A hook Job that
references that SA therefore deadlocks a fresh ``helm install``: the
apiserver rejects the migration pod with ``serviceaccount "meho" not
found`` on every admission attempt until the release times out.

The fix drops ``serviceAccountName`` from the migrate Job entirely — the
runner needs no Kubernetes API access, so the pod falls back to the
namespace ``default`` SA and (with ``automountServiceAccountToken:
false``) carries no token. These tests pin that the pre-install hook Job
never references the chart-managed SA, while the backplane Deployment
still does.

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
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

# Minimal chassis-required overrides that satisfy values.schema.json.
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


def _render_template(template: str, *extra: str) -> dict[str, Any]:
    """Return the parsed single manifest for ``--show-only <template>``."""
    result = subprocess.run(
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
        check=True,
    )
    return cast("dict[str, Any]", yaml.safe_load(result.stdout))


def test_migrate_job_is_a_pre_install_hook() -> None:
    """Guard the premise: the Job really is a pre-install/pre-upgrade hook."""
    job = _render_template("templates/migration-job.yaml")
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"


def test_migrate_job_has_no_service_account_name() -> None:
    """The hook Job must NOT reference the chart-managed (non-hook) SA (#2391).

    A ``serviceAccountName`` here would point at the ``meho`` SA that Helm
    only creates *after* the hook phase, deadlocking a cold ``helm install``.
    """
    job = _render_template("templates/migration-job.yaml")
    pod_spec = job["spec"]["template"]["spec"]
    assert "serviceAccountName" not in pod_spec
    # The token mount stays disabled — no API access, no token.
    assert pod_spec["automountServiceAccountToken"] is False


def test_migrate_job_has_no_sa_even_with_custom_sa_name() -> None:
    """Setting ``serviceAccount.name`` must not sneak a name onto the Job.

    An operator who customises the shared SA name still gets a Job with no
    ``serviceAccountName`` — the fix is unconditional, not a rename dodge.
    """
    job = _render_template(
        "templates/migration-job.yaml",
        "--set",
        "serviceAccount.name=custom-meho-sa",
    )
    assert "serviceAccountName" not in job["spec"]["template"]["spec"]


def test_deployment_still_binds_the_service_account() -> None:
    """Regression guard: only the Job changed — the Deployment keeps its SA."""
    deployment = _render_template("templates/deployment.yaml")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "test-meho"
