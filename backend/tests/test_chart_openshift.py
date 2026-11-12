# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""OpenShift picks the user ids of every pod (#4102).

OpenShift's default security rule (``restricted-v2``) gives each project its
own range of user ids and refuses a pod that asks for a fixed one outside
it. With ``global.openshift: true`` every pod of the chart and its subcharts
leaves out ``runAsUser``, ``runAsGroup`` and ``fsGroup``; the rest of the
security context stays. A pod the switch misses fails only on an OpenShift
install, so these tests check every pod: with every part on (the render and
the pod list come from ``test_chart_image_registry.py``), and in a
bring-your-own render, which adds the migration Job.

``deploy/values-examples/values-openshift.yaml`` is the file an OpenShift
install starts from; the last test keeps it valid.

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import pytest
import yaml

from tests.test_chart_image_registry import _ALL_ON, _CHART_DIR, _EXPECTED_PODS, _pods

_IDS = {"runAsUser", "runAsGroup", "fsGroup"}
_OPENSHIFT = ["--set", "global.openshift=true"]
_VALUES_OPENSHIFT = _CHART_DIR.parents[1] / "values-examples" / "values-openshift.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _assert_no_fixed_ids(key: str, spec: dict[str, Any]) -> None:
    assert not _IDS & set(spec.get("securityContext", {})), key
    for container in spec.get("initContainers", []) + spec["containers"]:
        assert not _IDS & set(container.get("securityContext", {})), f"{key}/{container['name']}"


def test_openshift_leaves_out_the_user_ids_in_every_pod() -> None:
    all_on = _pods(*_ALL_ON, *_OPENSHIFT)
    assert set(all_on) == _EXPECTED_PODS
    bring_your_own = _pods(*_OPENSHIFT)
    assert "Job/test-meho-migrate" in bring_your_own
    for key, spec in {**all_on, **bring_your_own}.items():
        _assert_no_fixed_ids(key, spec)


def test_other_clusters_keep_the_fixed_user_ids() -> None:
    pods = _pods(*_ALL_ON)
    for key in ("Deployment/test-meho", "Job/test-core-secrets", "Job/test-core-bootstrap"):
        assert pods[key]["securityContext"]["runAsUser"] == 1001, key
    for key in ("Deployment/test-broadcast", "Deployment/test-result-cache"):
        assert pods[key]["securityContext"]["runAsUser"] == 999, key
        assert pods[key]["securityContext"]["fsGroup"] == 999, key


def test_values_openshift_renders_the_built_in_core() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "meho",
            str(_CHART_DIR),
            "-f",
            str(_VALUES_OPENSHIFT),
            "--set",
            "image.tag=test",
            "--set",
            "ingress.host=meho.test",
            "--set",
            "coreKeycloak.hostname=meho.test",
            "--set",
            "coreKeycloak.firstTenantId=00000000-0000-0000-0000-000000000001",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in docs}
    core = {"StatefulSet/meho-postgres", "StatefulSet/meho-keycloak", "StatefulSet/meho-vault"}
    assert core <= names
    ingress = next(d for d in docs if d["kind"] == "Ingress")
    assert ingress["spec"]["ingressClassName"] == "openshift-default"
    for doc in docs:
        if doc["kind"] in {"Deployment", "StatefulSet", "Job"}:
            key = f"{doc['kind']}/{doc['metadata']['name']}"
            _assert_no_fixed_ids(key, doc["spec"]["template"]["spec"])
