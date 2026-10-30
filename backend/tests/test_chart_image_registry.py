# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Install from the customer's own image registry (#4100).

A customer with no internet copies our images into their own registry.
Two chart settings then reach every pod, in the parent chart and in every
subchart:

* ``global.imageRegistry`` replaces the registry host of every image. The
  path, the tag and the digest stay.
* ``global.imagePullSecrets`` is added to every pod.

A pod the settings miss fails only on install day, at a site with no
internet. So these tests turn every part on and check every container of
every pod (Deployments, StatefulSets, Jobs and the ``helm test`` pods).

With both settings empty the render must not change; the existing
``test_chart_bring_your_own_render.py`` and ``test_chart_core_*.py`` guard
that.

``deploy/charts/meho/images.txt`` is the list the customer copies: every
image of the chart except MEHO's own (its tag is set per install). Each
line holds the image as the chart renders it and its path in the
customer's registry, with a tag. The last test fails when the list and the
chart differ.

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

_IMAGES_TXT = _CHART_DIR / "images.txt"
_MEHO_REPOSITORY = "ghcr.io/evoila/meho"
_REGISTRY = "registry.example.com"
_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_BASE_OVERRIDES = [
    "--set",
    "image.tag=test",
    "--set-string",
    f"image.digest={_DIGEST}",
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
    "config.backplaneUrl=https://meho.test",
    "--set",
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set",
    "networkPolicy.keycloakCIDR=10.0.3.0/24",
]

# Every part on: the five subcharts and the three `helm test` pods.
_ALL_ON = [
    # The chart derives the issuer from the built-in Keycloak (#4591).
    "--set",
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    "coreKeycloak.firstTenantId=00000000-0000-0000-0000-000000000001",
    "--set",
    "coreVault.enabled=true",
    "--set",
    "resultCache.enabled=true",
    "--set",
    "agent.enabled=true",
    "--set",
    "agent.secretName=agent",
    "--set",
    "keycloakAdmin.enabled=true",
    "--set",
    "keycloakAdmin.url=https://keycloak.test",
    "--set",
    "keycloakAdmin.clientId=admin",
    "--set",
    "keycloakAdmin.clientSecret.secretName=test-core-keycloak-admin",
    "--set",
    "uiConsole.enabled=true",
    "--set",
    "uiConsole.secretName=test-core-ui-auth",
    "--set",
    "config.uiKeycloakClientId=ui",
]

# The pods that render with `_ALL_ON`, so a part that silently stops
# rendering cannot make the checks below pass on fewer pods. With the
# built-in PostgreSQL on, the migration is the `migrate` init container of
# `Deployment/test-meho`, not a Job (#5233); `_images` covers init containers.
_EXPECTED_PODS = {
    "Deployment/test-meho",
    "Job/test-core-secrets",
    "Job/test-core-bootstrap",
    "StatefulSet/test-postgres",
    "StatefulSet/test-keycloak",
    "StatefulSet/test-vault",
    "Deployment/test-broadcast",
    "Deployment/test-result-cache",
    "Pod/test-meho-test-pgvector",
    "Pod/test-meho-test-agent-runtime-config",
    "Pod/test-meho-test-ui-console-config",
}

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _helm_template(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _pods(*extra: str) -> dict[str, dict[str, Any]]:
    """Return every pod spec of the render, keyed by ``Kind/name``."""
    result = _helm_template(*extra)
    assert result.returncode == 0, result.stderr
    pods = {}
    for doc in yaml.safe_load_all(result.stdout):
        if not doc:
            continue
        key = f"{doc['kind']}/{doc['metadata']['name']}"
        if doc["kind"] == "Pod":
            pods[key] = doc["spec"]
        elif doc["kind"] in {"Deployment", "StatefulSet", "Job"}:
            pods[key] = doc["spec"]["template"]["spec"]
    return pods


def _images(pods: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Return every container and init container image, keyed by ``pod/container``."""
    return {
        f"{key}/{c['name']}": c["image"]
        for key, spec in pods.items()
        for c in spec.get("initContainers", []) + spec["containers"]
    }


def test_every_image_moves_to_the_registry() -> None:
    before = _images(_pods(*_ALL_ON))
    after_pods = _pods(*_ALL_ON, "--set", f"global.imageRegistry={_REGISTRY}")
    assert set(after_pods) == _EXPECTED_PODS
    after = _images(after_pods)
    assert set(after) == set(before)
    for key, image in after.items():
        assert image.startswith(f"{_REGISTRY}/"), f"{key}: {image}"
        # The last path part holds the name, the tag and the digest; none may change.
        assert image.rsplit("/", 1)[-1] == before[key].rsplit("/", 1)[-1], f"{key}: {image}"


def test_every_pod_gets_the_pull_secret() -> None:
    pods = _pods(
        *_ALL_ON,
        "--set",
        "global.imagePullSecrets[0].name=reg",
        # The parent chart's own list is kept, and a secret in both lists is named once.
        "--set",
        "imagePullSecrets[0].name=reg",
        "--set",
        "imagePullSecrets[1].name=old",
    )
    assert set(pods) == _EXPECTED_PODS
    for key, spec in pods.items():
        assert spec.get("imagePullSecrets", []).count({"name": "reg"}) == 1, key
    assert pods["Deployment/test-meho"]["imagePullSecrets"] == [{"name": "reg"}, {"name": "old"}]


@pytest.mark.parametrize(
    ("registry", "repository", "expected"),
    [
        # A registry host is replaced.
        (_REGISTRY, "quay.io/team/app", f"{_REGISTRY}/team/app"),
        (_REGISTRY, "localhost:5000/x", f"{_REGISTRY}/x"),
        (_REGISTRY, "localhost/x", f"{_REGISTRY}/x"),
        # No host: a Docker Hub image, the registry goes in front.
        (_REGISTRY, "team/app", f"{_REGISTRY}/team/app"),
        # One word: Docker Hub's `library/` too, also with Docker Hub's host written out.
        (_REGISTRY, "busybox", f"{_REGISTRY}/library/busybox"),
        (_REGISTRY, "docker.io/busybox", f"{_REGISTRY}/library/busybox"),
        (_REGISTRY, "docker.io/team/app", f"{_REGISTRY}/team/app"),
        # The registry may hold a path.
        ("harbor.example.com/meho", "quay.io/team/app", "harbor.example.com/meho/team/app"),
    ],
)
def test_registry_rewrite_rule(registry: str, repository: str, expected: str) -> None:
    pods = _pods(
        "--set",
        "corePostgres.enabled=true",
        "--set",
        f"corePostgres.image.repository={repository}",
        "--set",
        f"global.imageRegistry={registry}",
    )
    images = {i for key, i in _images(pods).items() if key.startswith("StatefulSet/test-postgres/")}
    assert len(images) == 1
    image = images.pop()
    assert image.split("@")[0] == expected


def test_registry_with_a_trailing_slash_is_schema_rejected() -> None:
    result = _helm_template("--set", f"global.imageRegistry={_REGISTRY}/")
    assert result.returncode != 0
    assert "imageRegistry" in result.stderr


def test_images_txt_lists_every_image_of_the_chart() -> None:
    before = _images(_pods(*_ALL_ON))
    after = _images(_pods(*_ALL_ON, "--set", f"global.imageRegistry={_REGISTRY}"))
    # Each image as the chart renders it -> its path in the customer's registry.
    expected = {
        image: after[key].removeprefix(f"{_REGISTRY}/").split("@")[0]
        for key, image in before.items()
        if image.split("@")[0] != _MEHO_REPOSITORY
    }
    listed = dict(line.split() for line in _IMAGES_TXT.read_text().splitlines())
    want = "\n".join(f"{source} {path}:<tag>" for source, path in sorted(expected.items()))
    assert set(listed) == set(expected), f"images.txt is out of date. The chart's images:\n{want}"
    for source, destination in listed.items():
        assert "@sha256:" in source, f"{source}: pin it by digest in the chart"
        # The chart renders by digest only, so the tag is not checked: it is a
        # label in the customer's registry. Pods always pull by digest.
        path, _, tag = destination.partition(":")
        assert path == expected[source], f"{source}: the path must be {expected[source]}"
        assert tag, f"{source}: {destination} needs a tag"
