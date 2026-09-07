# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render assertions for the F14a workload-isolation hardening (#276).

Two surfaces are pinned here:

* The **umbrella chart** ships a default-deny NetworkPolicy selecting the
  backplane Pods, and the backplane Deployment asserts an unprivileged
  ServiceAccount (``automountServiceAccountToken: false``) plus a
  ``runAsNonRoot`` securityContext. These already shipped; the tests guard
  them against regression (acceptance criterion 5).

* The **broadcast subchart** now renders its **own** ingress NetworkPolicy
  selecting the broadcast Pod (admitting tcp/6379 only from the backplane
  Pods) and a chart-managed ``requirepass`` Secret consumed through
  ``secretKeyRef`` by both the store and the backplane client. ``protected-
  mode`` stays on while auth is enabled (acceptance criterion 6).

The authoritative chart gate is ``.github/workflows/chart.yml`` (lint +
``helm template`` + ``kubeconform``). This test mirrors the assertions at
the unit layer so the regression is catchable from ``pytest``; it skips
cleanly where ``helm`` is absent (the backend unit-test sandbox does not
ship it — the workflow gate covers that environment).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

# backend/tests/<this> → parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

# Minimal chassis-required overrides that satisfy values.schema.json.
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
    shutil.which("helm") is None,
    reason="helm not installed in this sandbox; chart.yml workflow gate covers CI",
)


def _render_one(template: str, *extra: str) -> dict[str, Any]:
    """Return the parsed single manifest for ``--show-only <template>``."""
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            *extra,
            "--show-only",
            template,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return cast("dict[str, Any]", yaml.safe_load(result.stdout))


def _render_all(*extra: str) -> str:
    """Return the raw multi-document render of the whole chart."""
    result = subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Umbrella chart — NetworkPolicy / securityContext / ServiceAccount (AC 5)
# ---------------------------------------------------------------------------


def test_backplane_networkpolicy_is_default_deny_with_explicit_selectors() -> None:
    """The backplane NetworkPolicy selects the backplane Pods and bounds egress."""
    np = _render_one("templates/networkpolicy.yaml")
    assert np["kind"] == "NetworkPolicy"
    assert np["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "meho"
    assert set(np["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # Egress is CIDR-bounded to the operator-supplied Postgres/Vault/Keycloak
    # blocks, not a blanket allow.
    egress_cidrs = {
        peer["ipBlock"]["cidr"]
        for rule in np["spec"]["egress"]
        for peer in rule.get("to", [])
        if "ipBlock" in peer
    }
    assert {"10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"} <= egress_cidrs


def test_backplane_deployment_runs_unprivileged_with_no_token_mount() -> None:
    """securityContext + ServiceAccount keys asserted by the chart, not the image."""
    dep = _render_one("templates/deployment.yaml")
    pod_spec = dep["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "test-meho"
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is True


def test_service_account_disables_token_automount() -> None:
    sa = _render_one("templates/serviceaccount.yaml")
    assert sa["kind"] == "ServiceAccount"
    assert sa["automountServiceAccountToken"] is False


# ---------------------------------------------------------------------------
# Broadcast subchart — own ingress NetworkPolicy (AC 6)
# ---------------------------------------------------------------------------


def test_broadcast_networkpolicy_selects_the_store_pod() -> None:
    """The subchart ships its OWN policy selecting the broadcast Pod.

    A Pod is ingress-isolated only when some policy selects it; the parent
    policy selects the backplane, so without this the store stays reachable
    cluster-wide even at ``networkPolicy.enabled=true``.
    """
    np = _render_one("charts/broadcast/templates/networkpolicy.yaml")
    assert np["kind"] == "NetworkPolicy"
    assert np["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "broadcast"
    assert np["spec"]["policyTypes"] == ["Ingress"]


def test_broadcast_networkpolicy_admits_only_the_backplane_on_6379() -> None:
    np = _render_one("charts/broadcast/templates/networkpolicy.yaml")
    ingress = np["spec"]["ingress"]
    assert len(ingress) == 1
    ports = ingress[0]["ports"]
    assert ports == [{"port": 6379, "protocol": "TCP"}]
    from_labels = ingress[0]["from"][0]["podSelector"]["matchLabels"]
    assert from_labels["app.kubernetes.io/name"] == "meho"
    assert from_labels["app.kubernetes.io/instance"] == "test"


def test_broadcast_networkpolicy_admits_extra_consumers() -> None:
    """``extraAllowedSelectors`` adds explicitly-listed consumer peers."""
    np = _render_one(
        "charts/broadcast/templates/networkpolicy.yaml",
        "--set",
        "broadcast.networkPolicy.extraAllowedSelectors[0].app\\.kubernetes\\.io/name=satellite-runner",
    )
    peers = [p["podSelector"]["matchLabels"] for p in np["spec"]["ingress"][0]["from"]]
    assert {"app.kubernetes.io/name": "satellite-runner"} in peers


# ---------------------------------------------------------------------------
# Broadcast subchart — Valkey auth Secret + wiring (AC 6)
# ---------------------------------------------------------------------------


def test_broadcast_auth_secret_carries_requirepass_key() -> None:
    secret = _render_one("charts/broadcast/templates/secret.yaml")
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "test-broadcast-auth"
    assert "requirepass" in secret["data"]
    assert secret["data"]["requirepass"]  # non-empty base64 payload


def test_broadcast_store_reads_requirepass_via_secret_key_ref() -> None:
    dep = _render_one("charts/broadcast/templates/deployment.yaml")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    ref = env["VALKEY_REQUIREPASS"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "test-broadcast-auth", "key": "requirepass"}
    # The password is layered in at start-up, never written to the ConfigMap
    # or the command line.
    assert "$VALKEY_REQUIREPASS" in "\n".join(container["command"])


def test_broadcast_configmap_keeps_protected_mode_on_with_auth() -> None:
    cm = _render_one("charts/broadcast/templates/configmap.yaml")
    conf = cm["data"]["valkey.conf"]
    assert "protected-mode yes" in conf
    # requirepass is Secret-sourced, never in the ConfigMap.
    assert "requirepass" not in conf


def test_backplane_client_reads_broadcast_password_via_secret_key_ref() -> None:
    dep = _render_one("templates/deployment.yaml")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    # The URL stays password-free; the password rides its own secretKeyRef.
    assert "@" not in env["BROADCAST_REDIS_URL"]["value"]
    ref = env["BROADCAST_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "test-broadcast-auth", "key": "requirepass"}


# ---------------------------------------------------------------------------
# Broadcast subchart — auth toggles (AC 6 edge behaviour)
# ---------------------------------------------------------------------------


def test_auth_disabled_falls_back_to_configmap_and_no_secret() -> None:
    """Disabling auth keeps the store working (protected-mode off, no Secret)."""
    rendered = _render_all("--set", "broadcast.auth.enabled=false")
    assert "test-broadcast-auth" not in rendered
    assert "BROADCAST_REDIS_PASSWORD" not in rendered
    cm = _render_one(
        "charts/broadcast/templates/configmap.yaml",
        "--set",
        "broadcast.auth.enabled=false",
    )
    assert "protected-mode no" in cm["data"]["valkey.conf"]


def test_existing_secret_is_referenced_and_no_secret_rendered() -> None:
    """An operator-supplied Secret is referenced by both sides; none rendered."""
    extra = ("--set", "broadcast.auth.existingSecret=my-valkey-secret")
    store = _render_one("charts/broadcast/templates/deployment.yaml", *extra)
    store_env = {e["name"]: e for e in store["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (
        store_env["VALKEY_REQUIREPASS"]["valueFrom"]["secretKeyRef"]["name"] == "my-valkey-secret"
    )
    client = _render_one("templates/deployment.yaml", *extra)
    client_env = {e["name"]: e for e in client["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (
        client_env["BROADCAST_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"]
        == "my-valkey-secret"
    )
    # The chart renders no Secret of its own when existingSecret is set — the
    # chart-managed name never appears anywhere in the render.
    rendered = _render_all(*extra)
    assert "test-broadcast-auth" not in rendered


# ---------------------------------------------------------------------------
# Migration Job — least-privilege credentials (AC 4, relocated from F15 #277)
# ---------------------------------------------------------------------------


def test_migration_job_carries_only_the_database_credential() -> None:
    """The migrate Job gets ONLY ``DATABASE_URL`` — never the broadcast, agent,
    Keycloak, UI-console, or check-runner secrets the backplane Deployment wires.

    A ``pre-install,pre-upgrade`` hook running Alembic needs the database URL
    and nothing else; carrying the broadcast ``requirepass`` or any LLM/OAuth
    credential would widen the Job's blast radius past what it requires. The
    broadcast auth wiring (#276) is added to the backplane Deployment only —
    this guards that it did not leak into the migration hook.
    """
    job = _render_one("templates/migration-job.yaml")
    container = job["spec"]["template"]["spec"]["containers"][0]
    env_names = {e["name"] for e in container.get("env", [])}
    assert env_names == {"DATABASE_URL"}
    # No ServiceAccount token is mounted either — the runner needs no API access.
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
