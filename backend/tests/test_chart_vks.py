# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``deploy/values-examples/values-vks.yaml`` is the file a VKS install starts from (#5833).

A VKS cluster brings no ingress controller, so the file names Traefik, and its
network layer (Antrea) enforces network rules. The network rules of the
built-in OpenBao and Keycloak are off by default; the file turns them on, and
lets Traefik into Keycloak so logins work. This test keeps the file valid and
those rules on.

Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from tests.test_chart_image_registry import _CHART_DIR

_VALUES_VKS = _CHART_DIR.parents[1] / "values-examples" / "values-vks.yaml"
_FROM_TRAEFIK = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}}

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def test_values_vks_renders_the_built_in_core_behind_traefik() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "meho",
            str(_CHART_DIR),
            "-f",
            str(_VALUES_VKS),
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
    docs = {
        f"{doc['kind']}/{doc['metadata']['name']}": doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }
    core = {"StatefulSet/meho-postgres", "StatefulSet/meho-keycloak", "StatefulSet/meho-vault"}
    assert core <= set(docs)
    assert docs["Ingress/meho"]["spec"]["ingressClassName"] == "traefik"
    assert _FROM_TRAEFIK in docs["NetworkPolicy/meho"]["spec"]["ingress"][0]["from"]
    assert _FROM_TRAEFIK in docs["NetworkPolicy/meho-keycloak"]["spec"]["ingress"][0]["from"]
    assert _FROM_TRAEFIK not in docs["NetworkPolicy/meho-vault"]["spec"]["ingress"][0]["from"]
