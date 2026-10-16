# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the dedicated result-cache subchart (#3639).

The subchart is off by default. With ``resultCache.enabled=true`` it must
render one single-replica Deployment with no PVC, a Service, its own ingress
NetworkPolicy, and a ``valkey.conf`` with an explicit ``maxmemory`` and
``noeviction``. The parent chart must add an egress rule to it and point the
backplane at it. Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
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

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _render(*extra: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _is_result_cache(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/name") == "result-cache")


def test_result_cache_is_off_by_default() -> None:
    docs = _render()
    assert not [d for d in docs if _is_result_cache(d)]
    backplane = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "test-meho"
    )
    env_names = {e["name"] for e in backplane["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "RESULT_CACHE_REDIS_URL" not in env_names


def test_result_cache_renders_bounded_ephemeral_store() -> None:
    docs = _render("--set", "resultCache.enabled=true")
    ours = {d["kind"]: d for d in docs if _is_result_cache(d)}
    assert set(ours) == {"Deployment", "Service", "NetworkPolicy", "ConfigMap", "Secret"}
    assert not [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    assert len([d for d in docs if d["kind"] == "NetworkPolicy"]) == 3

    deployment = ours["Deployment"]
    assert deployment["metadata"]["name"] == "test-result-cache"
    assert deployment["spec"]["replicas"] == 1
    assert "volumeClaimTemplates" not in deployment["spec"]
    assert ours["Service"]["spec"]["ports"][0]["port"] == 6379

    conf = ours["ConfigMap"]["data"]["valkey.conf"]
    assert "maxmemory 768mb" in conf
    assert "maxmemory-policy noeviction" in conf
    assert 'save ""' in conf

    ingress = ours["NetworkPolicy"]["spec"]["ingress"][0]
    assert ingress["from"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "meho",
        "app.kubernetes.io/instance": "test",
    }
    assert ingress["ports"] == [{"port": 6379, "protocol": "TCP"}]


def test_parent_chart_wires_egress_and_backplane_env() -> None:
    docs = _render("--set", "resultCache.enabled=true", "--set", "resultCache.writerEnabled=true")
    parent_np = next(
        d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "test-meho"
    )
    selectors = [
        peer["podSelector"]["matchLabels"]
        for rule in parent_np["spec"]["egress"]
        for peer in rule.get("to", [])
        if "podSelector" in peer and "namespaceSelector" not in peer
    ]
    assert {"app.kubernetes.io/name": "result-cache", "app.kubernetes.io/instance": "test"} in (
        selectors
    )

    backplane = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "test-meho"
    )
    env = {e["name"]: e for e in backplane["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["RESULT_CACHE_REDIS_URL"]["value"] == "redis://test-result-cache:6379/0"
    secret_ref = env["RESULT_CACHE_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == "test-result-cache-auth"
    assert env["RESULT_CACHE_WRITER_ENABLED"]["value"] == "true"
