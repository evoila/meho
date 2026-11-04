# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the wiring of the built-in core to the backplane (#4047).

The three built-in parts are on by default. With only the answers of a
place (host name and first tenant id), the backplane reads its database
URL, its Vault address, login role and mount, the scheduler's token, the
approval hand-off key and the TLS certificate from what the install makes,
and its NetworkPolicy reaches each part by pod label. The tenant guard
(``VAULT_KV_TENANT_SCOPE_PREFIX``) stays at the code default. Values that
a built-in part cannot work with stop the render. The bring-your-own
render (every part off) is pinned byte for byte by
``test_chart_bring_your_own_render.py``.
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
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"  # gitleaks:allow

# The answers of a place: everything else is a default.
_PLACE = [
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    f"coreKeycloak.firstTenantId={_TENANT}",
]

_NOTES_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: notes-probe
data:
  notes: {{ tpl (.Files.Get "notes-probe.txt") . | quote }}
"""

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str, chart: Path = _CHART_DIR) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(chart), *_PLACE, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> list[dict[str, Any]]:
    result = _run(*extra)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _find(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        if doc["kind"] == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"{kind}/{name} not rendered")


def _backplane(docs: list[dict[str, Any]]) -> dict[str, Any]:
    pod: dict[str, Any] = _find(docs, "Deployment", "test-meho")["spec"]["template"]["spec"]
    return pod


def _env(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    container = _backplane(docs)["containers"][0]
    return {entry["name"]: entry for entry in container["env"]}


def _config(docs: list[dict[str, Any]]) -> dict[str, str]:
    data: dict[str, str] = _find(docs, "ConfigMap", "test-meho-config")["data"]
    return data


def _secret_ref(entry: dict[str, Any]) -> tuple[str, str]:
    ref = entry["valueFrom"]["secretKeyRef"]
    return ref["name"], ref["key"]


def _fails(*extra: str) -> str:
    result = _run(*extra)
    assert result.returncode != 0, "the render should have stopped"
    return result.stderr


def test_default_install_turns_on_all_three_parts() -> None:
    docs = _render()
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {
        ("StatefulSet", "test-postgres"),
        ("StatefulSet", "test-keycloak"),
        ("StatefulSet", "test-vault"),
    } <= names


def _migrate_env(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    migrate = _backplane(docs)["initContainers"][0]
    return {entry["name"]: entry for entry in migrate["env"]}


def test_database_url_comes_from_the_made_secrets() -> None:
    docs = _render()
    # The migrate init container runs the migrations with the owner login,
    # the running backplane uses the narrower meho_app login (#5699).
    assert _secret_ref(_migrate_env(docs)["DATABASE_URL"]) == ("test-core-postgres-meho", "url")
    assert _secret_ref(_env(docs)["DATABASE_URL"]) == ("test-core-postgres-meho-app", "url")


def test_vault_settings_point_at_the_built_in_openbao() -> None:
    config = _config(_render())
    assert config["VAULT_ADDR"] == "http://test-vault:8200"
    # The role and the mount the setup Job makes (core/vault_init.py).
    assert config["VAULT_OIDC_ROLE"] == "meho-mcp"
    assert config["VAULT_OIDC_MOUNT_PATH"] == "jwt"
    assert config["CREDENTIAL_BACKEND"] == "vault"


def test_vault_settings_follow_the_service_port() -> None:
    config = _config(_render("--set", "coreVault.service.port=8300"))
    assert config["VAULT_ADDR"] == "http://test-vault:8300"


def test_tenant_guard_and_role_map_stay_at_the_code_defaults() -> None:
    """One role serves every tenant; the guard keeps each one in its own path."""
    docs = _render()
    for name in ("VAULT_KV_TENANT_SCOPE_PREFIX", "VAULT_OIDC_ROLE_BY_TENANT"):
        assert name not in _config(docs)
        assert name not in _env(docs)


def test_scheduler_token_is_a_file_from_an_optional_secret() -> None:
    docs = _render()
    path = _config(docs)["VAULT_SCHEDULER_TOKEN_FILE"]
    assert path == "/var/run/meho/vault-scheduler/token"
    pod = _backplane(docs)
    volume = next(v for v in pod["volumes"] if v["name"] == "vault-scheduler")
    assert volume["secret"] == {
        "secretName": "test-core-vault-scheduler",
        "optional": True,
        "items": [{"key": "token", "path": "token"}],
    }
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "vault-scheduler")
    assert mount == {
        "name": "vault-scheduler",
        "mountPath": str(Path(path).parent),
        "readOnly": True,
    }
    assert "VAULT_SCHEDULER_TOKEN" not in _env(docs)


def test_approval_hand_off_is_on_by_default_with_the_made_key() -> None:
    env = _env(_render())
    assert _secret_ref(env["APPROVAL_HANDOFF_ENCRYPTION_KEY"]) == (
        "test-core-approval-handoff",
        "approval_handoff_encryption_key",
    )


def test_approval_hand_off_can_be_turned_off_or_named() -> None:
    assert "APPROVAL_HANDOFF_ENCRYPTION_KEY" not in _env(
        _render("--set", "approvalHandoff.enabled=false")
    )
    env = _env(_render("--set", "approvalHandoff.secretName=my-handoff"))
    assert _secret_ref(env["APPROVAL_HANDOFF_ENCRYPTION_KEY"])[0] == "my-handoff"


def test_ingress_uses_the_made_certificate() -> None:
    ingress = _find(_render(), "Ingress", "test-meho")
    assert ingress["spec"]["tls"] == [{"hosts": ["meho.test"], "secretName": "test-core-tls"}]


def test_network_policy_reaches_each_part_by_pod_label() -> None:
    # On by default, and with the built-in parts it needs no CIDR.
    policy = _find(_render(), "NetworkPolicy", "test-meho")
    selected = {}
    for rule in policy["spec"]["egress"]:
        for peer in rule.get("to", []):
            assert "ipBlock" not in peer, "a built-in part must not need a CIDR"
            labels = peer.get("podSelector", {}).get("matchLabels", {})
            if labels.get("app.kubernetes.io/instance") == "test":
                selected[labels["app.kubernetes.io/name"]] = [p["port"] for p in rule["ports"]]
    assert selected["postgres"] == [5432]
    assert selected["openbao"] == [8200]
    assert selected["keycloak"] == [8080]


def test_the_parts_admit_the_backplane() -> None:
    """Each part's own NetworkPolicy lets in the pods the backplane rule selects."""
    # OpenBao's own policy is off by default (#4050 turns it on).
    docs = _render("--set", "coreVault.networkPolicy.enabled=true")
    for name in ("test-postgres", "test-vault"):
        policy = _find(docs, "NetworkPolicy", name)
        peers = [peer for rule in policy["spec"]["ingress"] for peer in rule["from"]]
        labels = [peer.get("podSelector", {}).get("matchLabels", {}) for peer in peers]
        assert any(found.get("app.kubernetes.io/name") == "meho" for found in labels), name


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            ["--set", "postgres.credentialsSecret=my-db"],
            'postgres.credentialsSecret must be empty or "test-core-postgres-meho"',
        ),
        (["--set", "vault.address=https://vault.test"], "vault.address must be empty"),
        (["--set", "config.vaultAddr=https://vault.test"], "config.vaultAddr must be empty"),
        (
            ["--set", "eso.enabled=true", "--set", "eso.secretStore.name=store"],
            "eso.enabled must be false",
        ),
        (
            [
                "--set",
                "config.credentialBackend=gsm",
                "--set",
                "gsm.enabled=true",
                "--set",
                "gsm.project=p",
            ],
            "config.credentialBackend must be vault",
        ),
        (
            [
                "--set",
                "extraEnv[0].name=VAULT_KV_TENANT_SCOPE_PREFIX",
                "--set",
                "extraEnv[0].value=",
            ],
            "must not set VAULT_KV_TENANT_SCOPE_PREFIX empty",
        ),
    ],
)
def test_values_a_built_in_part_cannot_use_stop_the_render(extra: list[str], message: str) -> None:
    assert message in _fails(*extra)


def test_the_made_database_secret_name_is_accepted() -> None:
    docs = _render("--set", "postgres.credentialsSecret=test-core-postgres-meho")
    assert _secret_ref(_migrate_env(docs)["DATABASE_URL"])[0] == "test-core-postgres-meho"


def test_a_tenant_guard_of_its_own_is_accepted() -> None:
    _render(
        "--set",
        "extraEnv[0].name=VAULT_KV_TENANT_SCOPE_PREFIX",
        "--set",
        "extraEnv[0].value=secret/tenants/{tenant_id}/",
    )


@pytest.mark.parametrize(
    "key",
    ["postgres.credentialsSecret", "vault.address", "ingress.tls.secretName"],
)
def test_bring_your_own_still_needs_its_values(key: str) -> None:
    byo = [
        "--set",
        "corePostgres.enabled=false",
        "--set",
        "coreKeycloak.enabled=false",
        "--set",
        "coreVault.enabled=false",
        "--set",
        "keycloak.issuer=https://keycloak.test/realms/meho",
        "--set",
        "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
        "--set",
        "config.keycloakAudience=meho-backplane",
        "--set",
        "postgres.credentialsSecret=meho-postgres",
        "--set",
        "vault.address=https://vault.test",
        "--set",
        "config.vaultAddr=https://vault.test",
        "--set",
        "ingress.tls.secretName=meho-tls",
        "--set",
        "networkPolicy.enabled=false",
    ]
    assert _run(*byo).returncode == 0, _run(*byo).stderr
    # Helm 3 names the key dotted (`postgres.credentialsSecret`), Helm 4 as
    # a path (`/postgres/credentialsSecret`). CI pins Helm 3.
    stderr = _fails(*byo, "--set", f"{key}=")
    assert key in stderr or "/" + key.replace(".", "/") in stderr, stderr


def test_bring_your_own_network_policy_still_needs_the_cidrs() -> None:
    byo = [
        "--set",
        "corePostgres.enabled=false",
        "--set",
        "coreVault.enabled=false",
        "--set",
        "postgres.credentialsSecret=meho-postgres",
        "--set",
        "vault.address=https://vault.test",
        "--set",
        "config.vaultAddr=https://vault.test",
        "--set",
        "networkPolicy.enabled=true",
    ]
    stderr = _fails(*byo)
    assert "postgresCIDR" in stderr
    assert "vaultCIDR" in stderr


def test_notes_name_the_parts_the_one_time_secrets_and_the_checks(tmp_path: Path) -> None:
    chart = tmp_path / "meho"
    shutil.copytree(_CHART_DIR, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "notes-probe.txt")
    (chart / "templates" / "zz-notes-probe.yaml").write_text(_NOTES_PROBE, encoding="utf-8")
    result = _run("--show-only", "templates/zz-notes-probe.yaml", chart=chart)
    assert result.returncode == 0, result.stderr
    notes: str = yaml.safe_load(result.stdout)["data"]["notes"]
    assert "Parts: PostgreSQL, Keycloak, OpenBao, wired to MEHO by the chart." in notes
    for secret in ("test-core-first-login", "test-core-vault-unseal", "test-core-tls"):
        assert f"- {secret}:" in notes
    assert "meho login https://meho.test" in notes
    assert "meho status" in notes
    assert "https://meho.test/ready" in notes
    assert "MEHO logs in at http://test-vault:8200 with the role meho-mcp on jwt/." in notes
    # The approval hand-off key is on, so the warning about it is gone.
    assert "approvalHandoff.enabled=false" not in notes
