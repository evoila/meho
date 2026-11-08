# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the ``update-strategy`` pre-upgrade Job (#5962).

The chart renders the Job, its account and its rights only when the chart asks
for ``Recreate`` and the live Deployment still has a ``rollingUpdate`` block.
The chart reads the live Deployment with ``lookup``, which is always empty in
``helm template``. So most tests render a copy of the chart in which the
``lookup`` line returns a fixed Deployment. The test skips where ``helm`` is
absent; CI sets ``MEHO_REQUIRE_HELM=1`` so there it fails.
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
_TEMPLATE = Path("templates") / "update-strategy-job.yaml"
_RELEASE = "rel"
_LOOKUP_LINE = (
    '{{- $live := lookup "apps/v1" "Deployment" .Release.Namespace (include "meho.fullname" .) }}'
)
# What Kubernetes filled in on a Deployment made by a chart without a strategy.
_LIVE_ROLLING = (
    '{{- $live := dict "spec" (dict "strategy" (dict "type" "RollingUpdate"'
    ' "rollingUpdate" (dict "maxSurge" "25%" "maxUnavailable" "25%"))) }}'
)
_LIVE_RECREATE = '{{- $live := dict "spec" (dict "strategy" (dict "type" "Recreate")) }}'

_BASE = [
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


def _render(chart: Path, *args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", _RELEASE, str(chart), "--namespace", "meho", *_BASE, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _chart_with_live(tmp_path: Path, live_line: str) -> Path:
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    template = chart / _TEMPLATE
    text = template.read_text()
    assert text.count(_LOOKUP_LINE) == 1, "the lookup line changed; update this test"
    template.write_text(text.replace(_LOOKUP_LINE, live_line))
    return chart


def _ours(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        d["kind"]: d
        for d in docs
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "update-strategy"
    }


def test_plain_render_has_no_job() -> None:
    # No live Deployment: a new install, `helm template` and a client dry run.
    assert _ours(_render(_CHART_DIR)) == {}


def test_live_rolling_update_block_renders_the_job(tmp_path: Path) -> None:
    ours = _ours(_render(_chart_with_live(tmp_path, _LIVE_ROLLING)))

    assert set(ours) == {"ServiceAccount", "Role", "RoleBinding", "Job"}
    for obj in ours.values():
        assert obj["metadata"]["name"] == "rel-meho-update-strategy"
        assert obj["metadata"]["annotations"]["helm.sh/hook"] == "pre-upgrade"
        assert (
            obj["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
            == "before-hook-creation,hook-succeeded"
        )
    # The rights exist before the Job, and the Job runs after the migration Job (-10).
    assert int(ours["Role"]["metadata"]["annotations"]["helm.sh/hook-weight"]) < int(
        ours["Job"]["metadata"]["annotations"]["helm.sh/hook-weight"]
    )
    assert int(ours["Job"]["metadata"]["annotations"]["helm.sh/hook-weight"]) > -10

    assert ours["Role"]["rules"] == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "verbs": ["get", "patch"],
            "resourceNames": ["rel-meho"],
        }
    ]
    assert ours["RoleBinding"]["subjects"] == [
        {"kind": "ServiceAccount", "name": "rel-meho-update-strategy", "namespace": "meho"}
    ]

    pod = ours["Job"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "rel-meho-update-strategy"
    assert pod["restartPolicy"] == "Never"
    (container,) = pod["containers"]
    assert container["command"] == ["python", "-m", "meho_backplane.update_strategy"]
    env = {e["name"]: e for e in container["env"]}
    assert env["MEHO_DEPLOYMENT_NAME"]["value"] == "rel-meho"
    assert env["POD_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"
    # Not the backplane's selector labels: its NetworkPolicy blocks the Kubernetes API.
    assert "app.kubernetes.io/name" not in ours["Job"]["spec"]["template"]["metadata"]["labels"]


def test_job_uses_the_backplane_image(tmp_path: Path) -> None:
    docs = _render(_chart_with_live(tmp_path, _LIVE_ROLLING))
    deployment = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "rel-meho"
    )
    backplane = next(
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "backplane"
    )
    (container,) = _ours(docs)["Job"]["spec"]["template"]["spec"]["containers"]

    assert container["image"] == backplane["image"]


def test_live_recreate_renders_no_job(tmp_path: Path) -> None:
    assert _ours(_render(_chart_with_live(tmp_path, _LIVE_RECREATE))) == {}


def test_rolling_update_values_render_no_job(tmp_path: Path) -> None:
    chart = _chart_with_live(tmp_path, _LIVE_ROLLING)

    assert _ours(_render(chart, "--set", "updateStrategy.type=RollingUpdate")) == {}


def test_reuse_values_from_an_older_release_still_renders_the_job(tmp_path: Path) -> None:
    # `helm upgrade --reuse-values` from a release without `updateStrategy`.
    chart = _chart_with_live(tmp_path, _LIVE_ROLLING)

    assert "Job" in _ours(_render(chart, "--set", "updateStrategy=null"))


def test_job_follows_the_pod_placement_values(tmp_path: Path) -> None:
    # Same placement as the migration Job and the secrets Job.
    chart = _chart_with_live(tmp_path, _LIVE_ROLLING)
    docs = _render(
        chart,
        "--set",
        "nodeSelector.pool=ops",
        "--set",
        "tolerations[0].key=dedicated",
        "--set",
        "tolerations[0].operator=Exists",
        "--set",
        "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution"
        ".nodeSelectorTerms[0].matchExpressions[0].key=zone",
        "--set",
        "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution"
        ".nodeSelectorTerms[0].matchExpressions[0].operator=Exists",
    )
    pod = _ours(docs)["Job"]["spec"]["template"]["spec"]

    assert pod["nodeSelector"] == {"pool": "ops"}
    assert pod["tolerations"] == [{"key": "dedicated", "operator": "Exists"}]
    assert pod["affinity"] == {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchExpressions": [{"key": "zone", "operator": "Exists"}]}]
            }
        }
    }


def test_plain_job_has_no_placement(tmp_path: Path) -> None:
    pod = _ours(_render(_chart_with_live(tmp_path, _LIVE_ROLLING)))["Job"]["spec"]["template"][
        "spec"
    ]

    assert "nodeSelector" not in pod
    assert "tolerations" not in pod
    assert "affinity" not in pod
