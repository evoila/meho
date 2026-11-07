# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every helm test pod passes the restricted PodSecurity profile (#5141).

A cluster that enforces the restricted profile refuses a pod without
``runAsNonRoot: true`` and a ``seccompProfile``. The busybox test pods run
as root unless a user is set, so they also need a non-root ``runAsUser``.
With ``global.openshift: true`` the user and group ids are left out.

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import pytest

from tests.test_chart_image_registry import _ALL_ON, _pods

_OPENSHIFT = ["--set", "global.openshift=true"]
_CONSOLE_AND_RUNTIME = [
    *_ALL_ON,
    "--set",
    "uiConsole.enabled=true",
    "--set",
    "agent.enabled=true",
    "--set",
    "keycloakAdmin.enabled=true",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _test_pods(*extra: str) -> dict[str, dict[str, Any]]:
    pods = {key: spec for key, spec in _pods(*extra).items() if "-test-" in key}
    assert {k.split("/")[1].removeprefix("test-meho-test-") for k in pods} >= {
        "pgvector",
        "agent-runtime-config",
        "ui-console-config",
    }
    return pods


def test_every_test_pod_meets_the_restricted_profile() -> None:
    for key, spec in _test_pods(*_CONSOLE_AND_RUNTIME).items():
        pod = spec["securityContext"]
        assert pod["runAsNonRoot"] is True, key
        assert pod["runAsUser"] not in (None, 0), key
        assert pod["seccompProfile"]["type"] == "RuntimeDefault", key
        for container in spec["containers"]:
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, key
            assert sc["capabilities"]["drop"] == ["ALL"], key


def test_busybox_test_pods_run_as_nobody() -> None:
    pods = _test_pods(*_CONSOLE_AND_RUNTIME)
    for name in ("agent-runtime-config", "ui-console-config"):
        pod = pods[f"Pod/test-meho-test-{name}"]["securityContext"]
        assert (pod["runAsUser"], pod["runAsGroup"]) == (65534, 65534), name


def test_openshift_leaves_out_the_ids_of_every_test_pod() -> None:
    for key, spec in _test_pods(*_CONSOLE_AND_RUNTIME, *_OPENSHIFT).items():
        pod = spec["securityContext"]
        assert "runAsUser" not in pod, key
        assert "runAsGroup" not in pod, key
        assert pod["runAsNonRoot"] is True, key
        assert pod["seccompProfile"]["type"] == "RuntimeDefault", key
