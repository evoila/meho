# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_chart_namespace_scope.py`` (#4099).

The chart must install with the rights of a namespace admin, so it may
render nothing cluster-scoped and nothing in another namespace. The
``validate`` job in ``chart.yml`` pipes the real renders into the
script; these tests feed it small hand-written renders, including the
``system:auth-delegator`` exception that no values file turns on yet.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_chart_namespace_scope.py"

_DEPLOYMENT = """---
# Source: meho-chart/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-meho
"""
_CLUSTER_ROLE = """---
# Source: meho-chart/templates/clusterrole.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: test-meho
"""
_OTHER_NAMESPACE = """---
# Source: meho-chart/templates/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: test-meho
  namespace: kube-system
"""


def _binding(role: str) -> str:
    return f"""---
# Source: meho-chart/charts/openbao/templates/clusterrolebinding.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: test-openbao
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {role}
"""


def _run(rendered: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--namespace", "meho", *args],
        input=rendered,
        capture_output=True,
        text=True,
        check=False,
    )


def test_namespaced_objects_pass() -> None:
    result = _run(
        _DEPLOYMENT + "---\n# Source: x\nkind: Secret\nmetadata:\n  name: s\n  namespace: meho\n"
    )
    assert result.returncode == 0, result.stderr
    assert "all 2 objects" in result.stdout


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (_CLUSTER_ROLE, "ClusterRole/test-meho (meho-chart/templates/clusterrole.yaml)"),
        (
            _OTHER_NAMESPACE,
            "ConfigMap/test-meho (meho-chart/templates/configmap.yaml): namespace kube-system",
        ),
        (_binding("system:auth-delegator"), "ClusterRoleBinding/test-openbao"),
    ],
)
def test_object_outside_the_namespace_fails_and_names_object_and_file(
    extra: str, expected: str
) -> None:
    result = _run(_DEPLOYMENT + extra)
    assert result.returncode == 1
    assert expected in result.stderr


def test_auth_delegator_binding_passes_only_with_the_flag() -> None:
    assert (
        _run(_DEPLOYMENT + _binding("system:auth-delegator"), "--allow-auth-delegator").returncode
        == 0
    )
    other = _run(_DEPLOYMENT + _binding("cluster-admin"), "--allow-auth-delegator")
    assert other.returncode == 1
    assert "ClusterRoleBinding/test-openbao" in other.stderr


def test_empty_input_is_an_error() -> None:
    assert _run("").returncode == 2
