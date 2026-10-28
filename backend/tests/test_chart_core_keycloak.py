# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in Keycloak subchart (#4040).

Off by default: the chart then renders no Keycloak object at all, so a
bring-your-own install renders what it rendered before. With
``coreKeycloak.enabled=true`` the subchart renders a one-replica StatefulSet
on the official image (pinned by digest), the realm ConfigMap and a Service,
and the backplane's Ingress gains ``/auth/realms/meho`` and
``/auth/resources`` on the MEHO host. Every secret reaches the pod through
``secretKeyRef``; the realm ConfigMap holds only placeholders. Skips where
``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"
_REALM_FILE = _CHART_DIR / "charts" / "keycloak" / "files" / "meho-realm.json"
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"

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

_ON = [
    # The chart derives the issuer from the built-in Keycloak (#4591).
    "--set",
    "keycloak.issuer=",
    "--set",
    "config.keycloakIssuerUrl=",
    "--set",
    "coreKeycloak.enabled=true",
    "--set",
    "corePostgres.enabled=true",
    "--set",
    "coreKeycloak.hostname=meho.test",
    "--set",
    f"coreKeycloak.firstTenantId={_TENANT}",
]

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _run(*extra: str, release: str = "test") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", release, str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str, release: str = "test") -> list[dict[str, Any]]:
    result = _run(*extra, release=release)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _is_keycloak(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/name") == "keycloak")


def _keycloak_docs(*extra: str, release: str = "test") -> dict[str, dict[str, Any]]:
    return {d["kind"]: d for d in _render(*_ON, *extra, release=release) if _is_keycloak(d)}


def _ingress_paths(*extra: str, release: str = "test") -> list[dict[str, Any]]:
    ingress = next(d for d in _render(*_ON, *extra, release=release) if d["kind"] == "Ingress")
    (rule,) = ingress["spec"]["rules"]
    paths: list[dict[str, Any]] = rule["http"]["paths"]
    return paths


def _pod(docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    spec: dict[str, Any] = docs["StatefulSet"]["spec"]["template"]["spec"]
    return spec


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container["env"]}


def test_off_by_default_renders_nothing() -> None:
    docs = _render()
    assert not [d for d in docs if _is_keycloak(d)]
    # Saying "off" out loud renders the same objects as leaving it out.
    explicit = _render("--set", "coreKeycloak.enabled=false")
    assert [(d["kind"], d["metadata"]["name"]) for d in explicit] == [
        (d["kind"], d["metadata"]["name"]) for d in docs
    ]


def test_on_renders_statefulset_service_and_realm() -> None:
    docs = _keycloak_docs()
    assert set(docs) == {"StatefulSet", "Service", "ConfigMap"}
    sts = docs["StatefulSet"]
    assert sts["metadata"]["name"] == "test-keycloak"
    assert sts["spec"]["replicas"] == 1
    assert sts["spec"]["serviceName"] == "test-keycloak"
    service = docs["Service"]
    assert service["metadata"]["name"] == "test-keycloak"
    assert service["spec"]["ports"][0]["port"] == 8080


def test_official_image_pinned_by_digest() -> None:
    pod = _pod(_keycloak_docs())
    (keycloak,) = pod["containers"]
    assert re.fullmatch(r"quay\.io/keycloak/keycloak@sha256:[0-9a-f]{64}", keycloak["image"])
    assert keycloak["args"] == ["start", "--import-realm"]
    (wait,) = pod["initContainers"]
    assert re.fullmatch(r"pgvector/pgvector@sha256:[0-9a-f]{64}", wait["image"])
    script = wait["command"][-1]
    assert "pg_isready" in script
    # Without -U the check never passes under a user id with no passwd entry.
    assert "-U keycloak" in script


def test_keycloak_settings() -> None:
    env = _env(_pod(_keycloak_docs())["containers"][0])
    expected = {
        "KC_DB": "postgres",
        "KC_DB_URL_HOST": "test-postgres",
        "KC_DB_URL_PORT": "5432",
        "KC_DB_URL_DATABASE": "keycloak",
        "KC_DB_USERNAME": "keycloak",
        "KC_DB_POOL_INITIAL_SIZE": "2",
        "KC_DB_POOL_MIN_SIZE": "2",
        "KC_DB_POOL_MAX_SIZE": "20",
        "KC_HTTP_RELATIVE_PATH": "/auth",
        "KC_HOSTNAME": "https://meho.test/auth",
        "KC_HOSTNAME_BACKCHANNEL_DYNAMIC": "true",
        "KC_PROXY_HEADERS": "xforwarded",
        "KC_HTTP_ENABLED": "true",
        "KC_HEALTH_ENABLED": "true",
        "KC_CACHE": "local",
        "MEHO_REALM_PUBLIC_URL": "https://meho.test",
        "MEHO_REALM_TENANT_ID": _TENANT,
    }
    assert {k: env[k]["value"] for k in expected} == expected


def test_secrets_come_from_the_secrets_job_by_name() -> None:
    env = _env(_pod(_keycloak_docs())["containers"][0])
    refs = {
        name: (e["valueFrom"]["secretKeyRef"]["name"], e["valueFrom"]["secretKeyRef"]["key"])
        for name, e in env.items()
        if "valueFrom" in e
    }
    assert refs == {
        "KC_DB_PASSWORD": ("test-core-postgres-keycloak", "password"),
        "KC_BOOTSTRAP_ADMIN_CLIENT_ID": ("test-core-keycloak-bootstrap", "client_id"),
        "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET": ("test-core-keycloak-bootstrap", "client_secret"),
        "MEHO_REALM_ADMIN_CLIENT_SECRET": ("test-core-keycloak-admin", "client_secret"),
        "MEHO_REALM_WEB_CLIENT_SECRET": ("test-core-ui-auth", "client_secret"),
        "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": ("test-core-check-runner", "client_secret"),
    }
    for e in env.values():
        assert "value" in e or "optional" not in e["valueFrom"]["secretKeyRef"]


def test_bootstrap_secret_can_be_your_own() -> None:
    # The secrets Job reads the same key, so the two always agree.
    env = _env(
        _pod(_keycloak_docs("--set", "coreKeycloak.existingSecrets.bootstrap=my-bootstrap"))[
            "containers"
        ][0]
    )
    for name in ("KC_BOOTSTRAP_ADMIN_CLIENT_ID", "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"):
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "my-bootstrap"


def test_every_realm_placeholder_has_an_env_var() -> None:
    env = _env(_pod(_keycloak_docs())["containers"][0])
    placeholders = set(re.findall(r"\$\{([^}]*)\}", _REALM_FILE.read_text()))
    assert placeholders
    assert placeholders <= set(env)


def test_realm_configmap_is_the_file_and_mounted_for_import() -> None:
    docs = _keycloak_docs()
    cm = docs["ConfigMap"]
    assert cm["metadata"]["name"] == "test-keycloak-realm"
    assert json.loads(cm["data"]["meho-realm.json"]) == json.loads(_REALM_FILE.read_text())
    pod = _pod(docs)
    (mount,) = pod["containers"][0]["volumeMounts"]
    assert mount["mountPath"] == "/opt/keycloak/data/import"
    assert mount["readOnly"] is True
    volume = next(v for v in pod["volumes"] if v["name"] == mount["name"])
    assert volume["configMap"]["name"] == "test-keycloak-realm"


def test_probes_resources_and_security() -> None:
    pod = _pod(_keycloak_docs())
    keycloak = pod["containers"][0]
    assert keycloak["startupProbe"]["httpGet"] == {
        "path": "/auth/health/started",
        "port": "management",
    }
    assert (
        keycloak["startupProbe"]["failureThreshold"] * keycloak["startupProbe"]["periodSeconds"]
        == 300
    )
    assert keycloak["readinessProbe"]["httpGet"]["path"] == "/auth/health/ready"
    assert keycloak["livenessProbe"]["httpGet"]["path"] == "/auth/health/live"
    assert {p["name"]: p["containerPort"] for p in keycloak["ports"]} == {
        "http": 8080,
        "management": 9000,
    }
    assert keycloak["resources"]["limits"]["memory"] == "2Gi"
    assert keycloak["resources"]["limits"]["cpu"] == "1"
    assert keycloak["resources"]["requests"]["memory"] == "1Gi"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert "runAsUser" not in pod["securityContext"]
    sc = keycloak["securityContext"]
    assert "runAsUser" not in sc
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}
    # The stock image rebuilds under /opt/keycloak at every start.
    assert sc["readOnlyRootFilesystem"] is False
    wait = pod["initContainers"][0]["securityContext"]
    assert wait["runAsUser"] == 999
    assert wait["readOnlyRootFilesystem"] is True


def test_openshift_sets_no_user_or_group_id() -> None:
    pod = _pod(
        _keycloak_docs(
            "--set",
            "global.openshift=true",
            "--set",
            "coreKeycloak.podSecurityContext.runAsUser=1000",
            "--set",
            "coreKeycloak.podSecurityContext.fsGroup=0",
        )
    )
    contexts = [pod["securityContext"]] + [
        c["securityContext"] for c in pod["initContainers"] + pod["containers"]
    ]
    for ctx in contexts:
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(ctx), ctx
    assert pod["securityContext"]["runAsNonRoot"] is True


def test_ingress_routes_realm_and_static_files_only() -> None:
    docs = _render(*_ON)
    (ingress,) = [d for d in docs if d["kind"] == "Ingress"]
    assert ingress["metadata"]["name"] == "test-meho"
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "meho.test"
    paths = rule["http"]["paths"]
    assert [p["path"] for p in paths] == ["/", "/auth/realms/meho", "/auth/resources"]
    for p in paths[1:]:
        assert p["pathType"] == "Prefix"
        assert p["backend"]["service"] == {"name": "test-keycloak", "port": {"number": 8080}}
    assert paths[0]["backend"]["service"]["name"] == "test-meho"
    assert ingress["spec"]["tls"] == [{"hosts": ["meho.test"], "secretName": "meho-tls"}]


def test_expose_admin_routes_the_admin_console() -> None:
    paths = [p["path"] for p in _ingress_paths("--set", "coreKeycloak.ingress.exposeAdmin=true")]
    assert paths == [
        "/",
        "/auth/realms/meho",
        "/auth/resources",
        "/auth/admin",
        "/auth/realms/master",
    ]


def test_no_ingress_when_ingress_is_off() -> None:
    docs = _render(
        *_ON, "--set", "ingress.enabled=false", "--set", "config.backplaneUrl=https://meho.test"
    )
    assert not [d for d in docs if d["kind"] == "Ingress"]
    assert [d for d in docs if d["kind"] == "StatefulSet" and _is_keycloak(d)]


def test_networkpolicy_off_by_default_and_admits_backplane_when_on() -> None:
    assert "NetworkPolicy" not in _keycloak_docs()
    np = _keycloak_docs(
        "--set",
        "coreKeycloak.networkPolicy.enabled=true",
        "--set-json",
        'coreKeycloak.networkPolicy.extraFrom=[{"namespaceSelector":{"matchLabels":'
        '{"kubernetes.io/metadata.name":"kube-system"}}}]',
    )["NetworkPolicy"]
    assert np["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "keycloak",
        "app.kubernetes.io/instance": "test",
    }
    (rule,) = np["spec"]["ingress"]
    assert rule["from"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "meho",
        "app.kubernetes.io/instance": "test",
    }
    assert rule["from"][1] == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}
    }
    assert rule["ports"] == [{"port": "http", "protocol": "TCP"}]


def test_long_release_name_keeps_statefulset_and_ingress_in_step() -> None:
    release = "r" * 53
    docs = _keycloak_docs(release=release)
    name = docs["StatefulSet"]["metadata"]["name"]
    assert len(name) <= 52
    assert docs["Service"]["metadata"]["name"] == name
    backend = _ingress_paths(release=release)[1]["backend"]["service"]
    assert backend["name"] == name


@pytest.mark.parametrize(
    ("extra", "messages"),
    [
        # Helm 3 and Helm 4 word a failed if/then rule differently.
        (["--set", "corePostgres.enabled=false"], ('Must validate "then"', "'allOf' failed")),
        (["--set", "coreKeycloak.hostname=other.test"], ("must equal ingress.host",)),
        (["--set", "coreKeycloak.hostname="], ("coreKeycloak.hostname is empty",)),
        (["--set", "coreKeycloak.firstTenantId="], ("coreKeycloak.firstTenantId is empty",)),
        (["--set", "coreKeycloak.firstTenantId=not-a-uuid"], ("firstTenantId",)),
        (["--set", "coreKeycloak.unknownKey=1"], ("unknownKey",)),
    ],
    ids=[
        "needs-postgres",
        "host-mismatch",
        "no-hostname",
        "no-tenant-id",
        "bad-tenant-id",
        "unknown-key",
    ],
)
def test_bad_values_fail_at_render(extra: list[str], messages: tuple[str, ...]) -> None:
    result = _run(*_ON, *extra)
    assert result.returncode != 0
    assert any(m in result.stderr for m in messages), result.stderr


# --- The backplane uses the built-in Keycloak (#4591) ----------------------


def _backplane(*extra: str) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    """Return the backplane's ConfigMap data, container env and NetworkPolicy."""
    docs = _render(*extra)
    config = next(
        d for d in docs if d["kind"] == "ConfigMap" and "KEYCLOAK_ISSUER_URL" in d.get("data", {})
    )
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["labels"].get("app.kubernetes.io/name") == "meho"
    )
    env = _env(deployment["spec"]["template"]["spec"]["containers"][0])
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["metadata"]["labels"].get("app.kubernetes.io/name") == "meho"
    )
    return config["data"], env, policy


def test_backplane_settings_come_from_the_built_in_keycloak() -> None:
    data, env, _ = _backplane(*_ON)
    assert {k: v for k, v in data.items() if "KEYCLOAK" in k} == {
        # Tokens carry the public address; pods call the Service.
        "KEYCLOAK_ISSUER_URL": "https://meho.test/auth/realms/meho",
        "KEYCLOAK_DISCOVERY_URL": "http://test-keycloak:8080/auth/realms/meho",
        "KEYCLOAK_AUDIENCE": "meho-backplane",
        "KEYCLOAK_CLI_CLIENT_ID": "meho-cli",
        "UI_KEYCLOAK_CLIENT_ID": "meho-web",
        "KEYCLOAK_JWKS_CACHE_TTL_SECONDS": "300",
        "KEYCLOAK_JWT_LEEWAY_SECONDS": "30",
    }
    assert env["KEYCLOAK_ADMIN_URL"]["value"] == "http://test-keycloak:8080/auth/admin/realms/meho"
    assert env["KEYCLOAK_ADMIN_CLIENT_ID"]["value"] == "meho-admin"
    assert env["KEYCLOAK_ADMIN_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "test-core-keycloak-admin",
        "key": "client_secret",
    }


def test_audience_and_client_ids_match_the_realm_file() -> None:
    realm = json.loads(_REALM_FILE.read_text())
    clients = {c["clientId"]: c for c in realm["clients"]}
    data, env, _ = _backplane(*_ON)
    assert clients[data["KEYCLOAK_AUDIENCE"]]["publicClient"] is False
    assert clients[data["KEYCLOAK_CLI_CLIENT_ID"]]["publicClient"] is True
    assert clients[data["UI_KEYCLOAK_CLIENT_ID"]]["publicClient"] is False
    assert clients[env["KEYCLOAK_ADMIN_CLIENT_ID"]["value"]]["serviceAccountsEnabled"] is True


def test_service_address_follows_a_long_release_name() -> None:
    release = "r" * 53
    docs = _render(*_ON, release=release)
    service = next(d for d in docs if d["kind"] == "Service" and _is_keycloak(d))
    config = next(d for d in docs if d.get("data", {}).get("KEYCLOAK_DISCOVERY_URL"))
    name = service["metadata"]["name"]
    assert config["data"]["KEYCLOAK_DISCOVERY_URL"] == f"http://{name}:8080/auth/realms/meho"


def test_bring_your_own_keycloak_settings_are_unchanged() -> None:
    data, env, policy = _backplane(
        "--set",
        "config.keycloakCliClientId=my-cli",
        "--set",
        "keycloakAdmin.enabled=true",
        "--set",
        "keycloakAdmin.url=https://keycloak.test/admin/realms/meho",
        "--set",
        "keycloakAdmin.clientId=my-admin",
        "--set",
        "keycloakAdmin.clientSecret.secretName=my-admin-secret",
    )
    assert "KEYCLOAK_DISCOVERY_URL" not in data
    assert data["KEYCLOAK_ISSUER_URL"] == "https://keycloak.test/realms/meho"
    assert data["KEYCLOAK_AUDIENCE"] == "meho-backplane"
    assert data["KEYCLOAK_CLI_CLIENT_ID"] == "my-cli"
    assert data["UI_KEYCLOAK_CLIENT_ID"] == ""
    assert env["KEYCLOAK_ADMIN_URL"]["value"] == "https://keycloak.test/admin/realms/meho"
    assert env["KEYCLOAK_ADMIN_CLIENT_ID"]["value"] == "my-admin"
    assert env["KEYCLOAK_ADMIN_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "my-admin-secret",
        "key": "client_secret",
    }
    keycloak_rule = next(r for r in policy["spec"]["egress"] if r["ports"][0]["port"] == 443)
    assert keycloak_rule["to"] == [{"ipBlock": {"cidr": "10.0.3.0/24"}}]


def test_admin_settings_stay_off_without_either_switch() -> None:
    _, env, _ = _backplane()
    assert not {"KEYCLOAK_ADMIN_URL", "KEYCLOAK_ADMIN_CLIENT_ID"} & set(env)


def test_backplane_may_call_the_built_in_keycloak_pod() -> None:
    _, _, policy = _backplane(*_ON, "--set", "coreKeycloak.service.port=80")
    rules = [r for r in policy["spec"]["egress"] if "podSelector" in r["to"][0]]
    keycloak = next(
        r
        for r in rules
        if r["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "keycloak"
    )
    assert keycloak["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] == "test"
    # The pod's port, not the Service's.
    assert keycloak["ports"] == [{"port": 8080, "protocol": "TCP"}]
    assert not [r for r in policy["spec"]["egress"] if r["ports"][0]["port"] == 443]


def test_pods_may_call_over_plain_http_because_the_public_address_is_https() -> None:
    # Keycloak checks `sslRequired` against the frontend address. A fixed
    # https `KC_HOSTNAME` is what lets the backplane call the Service over
    # plain HTTP from any pod address (proved in
    # tests/integration/test_core_keycloak_realm_import.py).
    assert json.loads(_REALM_FILE.read_text())["sslRequired"] == "external"
    env = _env(_pod(_keycloak_docs())["containers"][0])
    assert env["KC_HOSTNAME"]["value"].startswith("https://")


@pytest.mark.parametrize(
    ("key", "core_kind"),
    [
        ("corePostgres.existingSecrets.keycloak", "postgres-keycloak"),
        ("keycloakAdmin.clientSecret.secretName", "keycloak-admin"),
        ("uiConsole.secretName", "ui-auth"),
        ("checkRunner.clientSecret.secretName", "check-runner"),
    ],
)
def test_secret_names_keycloak_reads_must_be_the_core_ones(key: str, core_kind: str) -> None:
    result = _run(*_ON, "--set", f"{key}=my-secret")
    assert result.returncode != 0
    assert (
        f'{key} must be empty or "test-core-{core_kind}" while coreKeycloak.enabled is on'
        in result.stderr
    ), result.stderr
    # The core name itself is fine: the secrets Job makes that Secret.
    assert _run(*_ON, "--set", f"{key}=test-core-{core_kind}").returncode == 0


@pytest.mark.parametrize("key", ["config.keycloakIssuerUrl", "keycloak.issuer"])
def test_hand_set_issuer_fails_with_the_built_in_keycloak(key: str) -> None:
    result = _run(*_ON, "--set", f"{key}=https://keycloak.test/realms/meho")
    assert result.returncode != 0
    assert f"{key} must be empty while coreKeycloak.enabled is on" in result.stderr


def test_issuer_audience_and_cidr_stay_required_for_bring_your_own() -> None:
    keys = (
        "keycloak.issuer",
        "config.keycloakIssuerUrl",
        "config.keycloakAudience",
        "networkPolicy.keycloakCIDR",
    )
    for key in keys:
        result = _run("--set", f"{key}=")
        assert result.returncode != 0, key
    # The built-in Keycloak needs none of them: the chart derives the
    # settings, and the NetworkPolicy reaches the pod by its labels.
    blank = [arg for key in keys[2:] for arg in ("--set", f"{key}=")]
    assert _run(*_ON, *blank).returncode == 0


# --- The company login's private CA (#4592) ---------------------------------

_COMPANY_ON = [
    "--set",
    "coreKeycloak.companyLogin.enabled=true",
    "--set",
    "coreKeycloak.companyLogin.discoveryUrl=https://idp.test/.well-known/openid-configuration",
    "--set",
    "coreKeycloak.companyLogin.clientId=meho-keycloak",
    "--set",
    "coreKeycloak.companyLogin.clientSecret.secretName=company-login",
]


def test_company_login_ca_bundle_is_mounted_as_the_truststore() -> None:
    pod = _pod(
        _keycloak_docs(
            *_COMPANY_ON,
            "--set",
            "coreKeycloak.companyLogin.caBundle.configMapName=company-ca",
            "--set",
            "coreKeycloak.companyLogin.caBundle.key=root.pem",
        )
    )
    (container,) = pod["containers"]
    assert _env(container)["KC_TRUSTSTORE_PATHS"]["value"] == (
        "/opt/keycloak/conf/company-login-ca/root.pem"
    )
    mount = next(m for m in container["volumeMounts"] if m["name"] == "company-login-ca")
    assert mount == {
        "name": "company-login-ca",
        "mountPath": "/opt/keycloak/conf/company-login-ca",
        "readOnly": True,
    }
    volume = next(v for v in pod["volumes"] if v["name"] == "company-login-ca")
    assert volume["configMap"] == {
        "name": "company-ca",
        "items": [{"key": "root.pem", "path": "root.pem"}],
    }


@pytest.mark.parametrize(
    "extra",
    [
        [],
        _COMPANY_ON,
        ["--set", "coreKeycloak.companyLogin.caBundle.configMapName=company-ca"],
    ],
    ids=["off", "on-without-ca", "ca-but-off"],
)
def test_no_truststore_without_a_ca_bundle_or_with_the_login_off(extra: list[str]) -> None:
    pod = _pod(_keycloak_docs(*extra))
    (container,) = pod["containers"]
    assert "KC_TRUSTSTORE_PATHS" not in _env(container)
    assert [v["name"] for v in pod["volumes"]] == ["realm"]
