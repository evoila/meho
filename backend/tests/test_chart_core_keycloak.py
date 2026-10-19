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


def test_existing_secrets_override_the_names() -> None:
    env = _env(
        _pod(
            _keycloak_docs(
                "--set",
                "coreKeycloak.existingSecrets.database=my-db",
                "--set",
                "coreKeycloak.existingSecrets.webClient=my-web",
            )
        )["containers"][0]
    )
    assert env["KC_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "my-db"
    assert env["MEHO_REALM_WEB_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "my-web"
    assert (
        env["MEHO_REALM_ADMIN_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["name"]
        == "test-core-keycloak-admin"
    )


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
    ("extra", "message"),
    [
        (["--set", "corePostgres.enabled=false"], "then"),
        (["--set", "coreKeycloak.hostname=other.test"], "must equal ingress.host"),
        (["--set", "coreKeycloak.hostname="], "coreKeycloak.hostname is empty"),
        (["--set", "coreKeycloak.firstTenantId=not-a-uuid"], "firstTenantId"),
        (["--set", "coreKeycloak.unknownKey=1"], "unknownKey"),
    ],
    ids=["needs-postgres", "host-mismatch", "no-hostname", "bad-tenant-id", "unknown-key"],
)
def test_bad_values_fail_at_render(extra: list[str], message: str) -> None:
    result = _run(*_ON, *extra)
    assert result.returncode != 0
    assert message in result.stderr or "allOf" in result.stderr, result.stderr
