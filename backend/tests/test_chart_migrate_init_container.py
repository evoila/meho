# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Where the chart runs the database migration, in each mode (#5233).

* **Bring your own PostgreSQL** (``corePostgres.enabled: false``): the
  ``pre-install,pre-upgrade`` hook Job ``<release>-meho-migrate``. The
  database exists before ``helm install``. The render of this mode must not
  change (``test_chart_bring_your_own_render.py``).
* **Built-in PostgreSQL** (``corePostgres.enabled: true``): Helm runs
  hooks before it creates the database, so the Job is not rendered. The
  backplane Deployment gets the init container ``migrate`` instead, which
  runs the same runner before the backplane starts.

The init container must stay the same container as the Job: image (with
the digest pin), command, environment, security context, volumes and
resources. These tests compare the two renders, so a change to one that
misses the other fails here.

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"
_FORM_2_VALUES = [
    _REPO_ROOT / "deploy" / "linux-server" / "packaging" / "render-values.yaml",
    _REPO_ROOT / "deploy" / "linux-server" / "values-single-node.yaml",
]

_BASE_OVERRIDES = [
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "ingress.tls.secretName=meho-tls",
    "--set",
    "postgres.credentialsSecret=test-core-postgres-meho",
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

# Operator extras the Job and the init container must both carry.
_EXTRAS = [
    "--set",
    "image.digest=sha256:" + "a" * 64,
    "--set",
    "global.imageRegistry=registry.example.test/mirror",
    "--set-json",
    'extraEnv=[{"name":"SSL_CERT_FILE","value":"/etc/ssl/bundle/ca.crt"}]',
    "--set-json",
    'extraVolumeMounts=[{"name":"trust","mountPath":"/etc/ssl/bundle","readOnly":true}]',
    "--set-json",
    'extraVolumes=[{"name":"trust","configMap":{"name":"trust-bundle"}}]',
]

_CORE_ON = ["--set", "corePostgres.enabled=true"]

# Everything that makes the runner's container; `name` is "migrate" in both.
_SAME_FIELDS = (
    "name",
    "image",
    "imagePullPolicy",
    "command",
    "envFrom",
    "env",
    "resources",
    "securityContext",
    "volumeMounts",
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _render(
    *extra: str, base: list[str] | None = None, release: str = "test"
) -> list[dict[str, Any]]:
    overrides = _BASE_OVERRIDES if base is None else base
    result = subprocess.run(
        ["helm", "template", release, str(_CHART_DIR), *overrides, *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _migrate_jobs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-meho-migrate")
    ]


def _backplane_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (deployment,) = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["labels"]["app.kubernetes.io/name"] == "meho"
    ]
    spec: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return spec


def test_bring_your_own_keeps_the_hook_job_and_no_init_container() -> None:
    docs = _render()

    (job,) = _migrate_jobs(docs)
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "initContainers" not in _backplane_pod(docs)


def test_built_in_postgres_migrates_in_the_backplane_pod() -> None:
    docs = _render(*_CORE_ON)

    assert _migrate_jobs(docs) == []
    pod = _backplane_pod(docs)
    (init,) = pod["initContainers"]
    assert init["name"] == "migrate"
    assert init["command"] == ["python", "-m", "meho_backplane.db.migrate"]
    # The runner needs the database URL and nothing else.
    assert [e["name"] for e in init["env"]] == ["DATABASE_URL"]
    assert init["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "test-core-postgres-meho",
        "key": "url",
    }
    # The /tmp emptyDir is the pod's own volume.
    assert {"name": "tmp", "mountPath": "/tmp"} in init["volumeMounts"]
    assert {"name": "tmp", "emptyDir": {}} in pod["volumes"]


def test_built_in_postgres_runs_no_pre_install_job_that_needs_the_database() -> None:
    """Only the secrets Job runs before the release objects exist.

    A pre-install hook that needs the database waits for a database Helm
    has not created yet; that was the bug of #5233.
    """
    docs = _render(*_CORE_ON)

    pre_install_jobs = [
        d["metadata"]["name"]
        for d in docs
        if d["kind"] == "Job"
        and "pre-install" in d["metadata"].get("annotations", {}).get("helm.sh/hook", "")
    ]
    assert pre_install_jobs == ["test-core-secrets"]


def test_init_container_is_the_same_container_as_the_hook_job() -> None:
    (job,) = _migrate_jobs(_render(*_EXTRAS))
    job_pod = job["spec"]["template"]["spec"]
    job_container = job_pod["containers"][0]
    pod = _backplane_pod(_render(*_EXTRAS, *_CORE_ON))
    (init,) = pod["initContainers"]

    for field in _SAME_FIELDS:
        assert init.get(field) == job_container.get(field), field
    assert init["image"] == "registry.example.test/mirror/evoila/meho@sha256:" + "a" * 64
    assert init["image"] == pod["containers"][0]["image"]
    assert {"name": "SSL_CERT_FILE", "value": "/etc/ssl/bundle/ca.crt"} in init["env"]
    assert {"name": "trust", "configMap": {"name": "trust-bundle"}} in pod["volumes"]
    # Same pod-level security context as the Job: runAsNonRoot and the
    # RuntimeDefault seccomp profile hold for the init container too.
    assert pod["securityContext"] == job_pod["securityContext"]


def test_init_container_meets_the_restricted_pod_security_standard() -> None:
    pod = _backplane_pod(_render(*_CORE_ON))
    (init,) = pod["initContainers"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert init["securityContext"]["allowPrivilegeEscalation"] is False
    assert init["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert "privileged" not in init["securityContext"]


def test_form_2_render_migrates_in_the_backplane_pod() -> None:
    """The form-2 download values (built-in parts on) get the init container.

    ``install_core`` (``deploy/linux-server/lib/core.sh``) runs one
    ``helm upgrade --install --wait --wait-for-jobs`` with these values,
    release ``meho`` in namespace ``meho``.
    """
    files = [arg for path in _FORM_2_VALUES for arg in ("-f", str(path))]
    docs = _render(
        *files, "--set", "image.tag=test", "--namespace", "meho", base=[], release="meho"
    )

    assert _migrate_jobs(docs) == []
    (init,) = _backplane_pod(docs)["initContainers"]
    assert init["name"] == "migrate"
    assert init["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "meho-core-postgres-meho"
