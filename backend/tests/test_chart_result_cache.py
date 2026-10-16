# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the dedicated result-cache subchart (#3639).

The subchart is off by default. With ``resultCache.enabled=true`` it must
render one single-replica Deployment with no PVC, a Service, its own ingress
NetworkPolicy, and a ``valkey.conf`` with an explicit ``maxmemory`` and
``noeviction``. The parent chart must add an egress rule to it and point the
backplane at it. The subchart name is pinned (no ``nameOverride``), both
network policies admit the Pod port even when the Service port changes, and a
config checksum on the pod template makes a changed ``valkey.conf`` roll the
pod. Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
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


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _result_cache_docs(*extra: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    docs = _render("--set", "resultCache.enabled=true", *extra)
    return docs, {d["kind"]: d for d in docs if _is_result_cache(d)}


def _backplane_deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "test-meho"
    )


def _backplane_env(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    container = _backplane_deployment(docs)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in container["env"]}


def _parent_egress_to_result_cache(docs: list[dict[str, Any]]) -> dict[str, Any]:
    parent_np = next(
        d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "test-meho"
    )
    return next(
        rule
        for rule in parent_np["spec"]["egress"]
        for peer in rule.get("to", [])
        if peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
        == "result-cache"
    )


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
    docs, _ = _result_cache_docs("--set", "resultCache.writerEnabled=true")
    egress = _parent_egress_to_result_cache(docs)
    assert egress["to"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "result-cache",
        "app.kubernetes.io/instance": "test",
    }

    env = _backplane_env(docs)
    assert env["RESULT_CACHE_REDIS_URL"]["value"] == "redis://test-result-cache:6379/0"
    secret_ref = env["RESULT_CACHE_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == "test-result-cache-auth"
    assert env["RESULT_CACHE_WRITER_ENABLED"]["value"] == "true"


def test_backplane_references_line_up_with_subchart_names() -> None:
    """The URL host, the Secret reference, and the egress selector all point at
    the resources the subchart really renders."""
    docs, ours = _result_cache_docs()
    env = _backplane_env(docs)

    host = env["RESULT_CACHE_REDIS_URL"]["value"].removeprefix("redis://").split(":")[0]
    assert host == ours["Service"]["metadata"]["name"]

    secret_ref = env["RESULT_CACHE_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == ours["Secret"]["metadata"]["name"]
    assert secret_ref["key"] in ours["Secret"]["data"]
    store_env = ours["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"][0]
    assert store_env["valueFrom"]["secretKeyRef"] == secret_ref

    pod_labels = ours["Deployment"]["spec"]["template"]["metadata"]["labels"]
    egress_selector = _parent_egress_to_result_cache(docs)["to"][0]["podSelector"]["matchLabels"]
    assert all(pod_labels.get(k) == v for k, v in egress_selector.items())


def test_name_override_is_rejected() -> None:
    """A renamed store would leave the backplane pointing at the default
    Service and Secret names, so the schema refuses the key."""
    result = _run("--set", "resultCache.enabled=true", "--set", "resultCache.nameOverride=cache")
    assert result.returncode != 0
    assert "nameOverride" in result.stderr


def test_custom_service_port_keeps_policies_on_the_pod_port() -> None:
    docs, ours = _result_cache_docs("--set", "resultCache.service.port=6380")

    service_port = ours["Service"]["spec"]["ports"][0]
    assert service_port["port"] == 6380
    assert service_port["targetPort"] == "redis"
    container = ours["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    assert container["ports"] == [{"name": "redis", "containerPort": 6379, "protocol": "TCP"}]
    assert "port 6379" in ours["ConfigMap"]["data"]["valkey.conf"]

    assert _backplane_env(docs)["RESULT_CACHE_REDIS_URL"]["value"] == (
        "redis://test-result-cache:6380/0"
    )
    assert ours["NetworkPolicy"]["spec"]["ingress"][0]["ports"] == [
        {"port": 6379, "protocol": "TCP"}
    ]
    assert _parent_egress_to_result_cache(docs)["ports"] == [{"port": 6379, "protocol": "TCP"}]


def _config_checksum(*extra: str) -> str:
    _, ours = _result_cache_docs(*extra)
    annotations = ours["Deployment"]["spec"]["template"]["metadata"]["annotations"]
    return str(annotations["checksum/config"])


def test_memory_settings_change_rolls_the_pod() -> None:
    """Valkey reads its config at start-up only, so a new ``maxmemory`` or
    ``maxmemoryPolicy`` must change the pod template."""
    default = _config_checksum()
    assert default == _config_checksum()
    assert _config_checksum("--set", "resultCache.maxmemory=512mb") != default
    assert _config_checksum("--set", "resultCache.maxmemoryPolicy=allkeys-lru") != default


def test_pod_annotations_render_next_to_the_checksum() -> None:
    _, ours = _result_cache_docs("--set", "resultCache.podAnnotations.example=yes")
    annotations = ours["Deployment"]["spec"]["template"]["metadata"]["annotations"]
    assert annotations["example"] == "yes"
    assert "checksum/config" in annotations
