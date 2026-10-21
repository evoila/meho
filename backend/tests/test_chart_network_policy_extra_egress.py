# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for ``networkPolicy.extraEgress`` (#3516).

With the backplane NetworkPolicy on, egress is limited to the three CIDR
rules, the in-cluster stores and DNS. ``extraEgress`` adds operator rules
after those, exactly as written, so an install can reach the systems MEHO
manages and Vault / Keycloak behind an in-cluster ingress (the redirect to
the ingress Pod port happens before the rule is checked). Without the key
the policy renders as before. A rule without a ``to`` list is refused by the
schema and, when the schema is skipped, by the template, because Kubernetes
reads a missing or empty ``to`` as every destination. The schema also
refuses unknown keys, so a typo cannot drop a port limit. Skips where
``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import json
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

# An ingress-fronted install: managed systems on a routed range, and
# Vault / Keycloak behind Traefik, whose Service sends 443 to the Pod's 8443.
_MANAGED_TARGETS = {
    "to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}],
    "ports": [{"port": 443, "protocol": "TCP"}, {"port": 22, "protocol": "TCP"}],
}
_TRAEFIK_WEBSECURE = {
    "to": [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
        }
    ],
    "ports": [{"port": 8443, "protocol": "TCP"}],
}

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


def _egress(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["metadata"]["labels"].get("app.kubernetes.io/name") == "meho"
    )
    rules: list[dict[str, Any]] = policy["spec"]["egress"]
    return rules


def _set_extra_egress(rules: object) -> tuple[str, str]:
    return ("--set-json", f"networkPolicy.extraEgress={json.dumps(rules)}")


def test_default_render_adds_no_extra_rule() -> None:
    default = _egress()
    assert default == _egress(*_set_extra_egress([]))
    assert default[-1]["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}


def test_ingress_fronted_vault_keycloak_rules_are_added_as_written() -> None:
    default = _egress()
    egress = _egress(*_set_extra_egress([_MANAGED_TARGETS, _TRAEFIK_WEBSECURE]))
    assert egress == [*default, _MANAGED_TARGETS, _TRAEFIK_WEBSECURE]


@pytest.mark.parametrize(
    "rules",
    [
        [{}],
        [{"to": []}],
        [{"ports": [{"port": 443, "protocol": "TCP"}]}],
        [{"to": [{}]}],
        [{"to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}], "port": [{"port": 443}]}],
        [{"to": [{"ipblock": {"cidr": "192.0.2.0/24"}}]}],
        [{"to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}], "ports": [{"prot": 443}]}],
        _MANAGED_TARGETS,
    ],
    ids=[
        "empty-rule",
        "empty-to",
        "ports-only",
        "empty-peer",
        "typo-in-rule",
        "typo-in-peer",
        "typo-in-port",
        "not-a-list",
    ],
)
def test_schema_refuses_invalid_extra_egress(rules: object) -> None:
    result = _run(*_set_extra_egress(rules))
    assert result.returncode != 0
    assert "schema" in result.stderr
    assert "extraEgress" in result.stderr


@pytest.mark.parametrize(
    "rule",
    [{}, {"to": []}, {"to": None}, {"ports": [{"port": 443, "protocol": "TCP"}]}],
    ids=["empty-rule", "empty-to", "null-to", "ports-only"],
)
def test_template_refuses_rule_without_to_when_schema_is_skipped(rule: object) -> None:
    result = _run("--skip-schema-validation", *_set_extra_egress([rule]))
    assert result.returncode != 0
    assert "networkPolicy.extraEgress[0] has no 'to' list" in result.stderr
