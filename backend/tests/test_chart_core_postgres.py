# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart-render checks for the built-in PostgreSQL subchart (#4039).

With ``corePostgres.enabled=true`` the chart renders one PostgreSQL 17
StatefulSet (pgvector image, pinned by digest) with a reconcile sidecar, a
Service ``<release>-postgres``, a ConfigMap with the one SQL file and its two
scripts, and an ingress-only NetworkPolicy. The tests pin what the design
asks for:

* the superuser Secret is mounted by the ``postgres`` container alone;
* neither database role is a superuser, and the superuser creates ``vector``;
* passwords reach SQL only as psql variables;
* PGDATA is a sub-directory of the disk, with emptyDirs for the socket,
  ``/tmp`` and ``/dev/shm``;
* user id 999 by default, and no ``runAsUser``, ``runAsGroup`` or
  ``fsGroup`` with ``global.openshift=true`` (also on the helm test pod);
* the probes use TCP, not the socket.

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

from tests._pgvector_image import chart_postgres_image

_CHART_DIR = Path(__file__).resolve().parents[2] / "deploy" / "charts" / "meho"

_DIGEST = "sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"

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


def _is_postgres(doc: dict[str, Any]) -> bool:
    labels = doc.get("metadata", {}).get("labels", {})
    return bool(labels.get("app.kubernetes.io/name") == "postgres")


def _core(*extra: str, release: str = "test") -> dict[str, dict[str, Any]]:
    docs = _render("--set", "corePostgres.enabled=true", *extra, release=release)
    return {d["kind"]: d for d in docs if _is_postgres(d)}


def _containers(sts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in sts["spec"]["template"]["spec"]["containers"]}


def _mounts(container: dict[str, Any]) -> dict[str, str]:
    return {m["mountPath"]: m["name"] for m in container["volumeMounts"]}


def _volumes(sts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in sts["spec"]["template"]["spec"]["volumes"]}


def _test_pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        d for d in docs if d["kind"] == "Pod" and d["metadata"]["name"].endswith("-test-pgvector")
    )


def test_off_by_default() -> None:
    docs = _render()
    assert not [d for d in docs if _is_postgres(d)]


def test_renders_one_statefulset_with_service_config_and_policy() -> None:
    ours = _core()
    assert set(ours) == {"StatefulSet", "Service", "ConfigMap", "NetworkPolicy"}
    sts = ours["StatefulSet"]
    assert sts["metadata"]["name"] == "test-postgres"
    assert sts["spec"]["replicas"] == 1
    assert sts["spec"]["serviceName"] == "test-postgres"
    assert ours["Service"]["metadata"]["name"] == "test-postgres"
    assert ours["Service"]["spec"]["ports"][0]["port"] == 5432

    claim = sts["spec"]["volumeClaimTemplates"][0]
    assert claim["metadata"]["name"] == "data"
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert "storageClassName" not in claim["spec"]

    pod_spec = sts["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert sts["spec"]["template"]["metadata"]["annotations"]["checksum/config"]


def test_storage_class_and_size_are_values() -> None:
    sts = _core(
        "--set",
        "corePostgres.persistence.storageClass=fast",
        "--set",
        "corePostgres.persistence.size=20Gi",
    )["StatefulSet"]
    claim = sts["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["storageClassName"] == "fast"
    assert claim["resources"]["requests"]["storage"] == "20Gi"


def test_image_pinned_by_digest_and_tag_when_digest_is_empty() -> None:
    containers = _containers(_core()["StatefulSet"])
    expected = f"pgvector/pgvector@{_DIGEST}"
    assert containers["postgres"]["image"] == expected
    assert containers["reconcile"]["image"] == expected

    by_tag = _containers(_core("--set", "corePostgres.image.digest=")["StatefulSet"])
    assert by_tag["postgres"]["image"] == "pgvector/pgvector:0.8.7-pg17-bookworm"


def test_superuser_secret_is_mounted_by_the_server_container_alone() -> None:
    sts = _core()["StatefulSet"]
    containers = _containers(sts)
    volumes = _volumes(sts)

    assert volumes["superuser"]["secret"]["secretName"] == "test-core-postgres-superuser"
    assert volumes["meho"]["secret"]["secretName"] == "test-core-postgres-meho"
    assert volumes["keycloak"]["secret"]["secretName"] == "test-core-postgres-keycloak"
    for name in ("superuser", "meho", "keycloak"):
        assert volumes[name]["secret"]["items"] == [{"key": "password", "path": "password"}]

    server = containers["postgres"]
    env = {e["name"]: e for e in server["env"]}
    assert env["POSTGRES_PASSWORD_FILE"]["value"] == "/etc/core-postgres/secrets/superuser/password"
    assert "POSTGRES_PASSWORD" not in env
    assert _mounts(server)["/etc/core-postgres/secrets/superuser"] == "superuser"

    sidecar = containers["reconcile"]
    assert "superuser" not in _mounts(sidecar).values()
    assert "env" not in sidecar
    assert {"meho", "keycloak", "run"} <= set(_mounts(sidecar).values())


def test_existing_secrets_override_the_generated_names() -> None:
    volumes = _volumes(
        _core(
            "--set",
            "corePostgres.existingSecrets.superuser=my-su",
            "--set",
            "corePostgres.existingSecrets.keycloak=my-kc",
        )["StatefulSet"]
    )
    assert volumes["superuser"]["secret"]["secretName"] == "my-su"
    assert volumes["keycloak"]["secret"]["secretName"] == "my-kc"
    assert volumes["meho"]["secret"]["secretName"] == "test-core-postgres-meho"


def test_pgdata_is_a_subdirectory_and_scratch_paths_are_emptydirs() -> None:
    sts = _core()["StatefulSet"]
    server = _containers(sts)["postgres"]
    env = {e["name"]: e.get("value") for e in server["env"]}
    assert env["PGDATA"] == "/var/lib/postgresql/data/pgdata"

    mounts = _mounts(server)
    assert mounts["/var/lib/postgresql/data"] == "data"
    volumes = _volumes(sts)
    assert "emptyDir" in volumes[mounts["/var/run/postgresql"]]
    assert "emptyDir" in volumes[mounts["/tmp"]]
    assert volumes[mounts["/dev/shm"]]["emptyDir"]["medium"] == "Memory"
    assert server["securityContext"]["readOnlyRootFilesystem"] is True


def test_initdb_directory_holds_only_the_apply_script() -> None:
    sts = _core()["StatefulSet"]
    server = _containers(sts)["postgres"]
    initdb = _volumes(sts)[_mounts(server)["/docker-entrypoint-initdb.d"]]
    assert initdb["configMap"]["items"] == [{"key": "apply.sh", "path": "10-core.sh"}]
    assert initdb["configMap"]["defaultMode"] == 0o555


def test_probes_use_tcp_not_the_socket() -> None:
    server = _containers(_core()["StatefulSet"])["postgres"]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        command = server[probe]["exec"]["command"]
        assert command[0] == "pg_isready"
        assert command[command.index("-h") + 1] == "127.0.0.1"
    startup = server["startupProbe"]
    assert startup["failureThreshold"] * startup["periodSeconds"] >= 300


def test_sql_creates_two_owner_roles_without_superuser_and_the_extension() -> None:
    sql = _core()["ConfigMap"]["data"]["core.sql"]
    assert "\\getenv meho_pw MEHO_DB_PASSWORD" in sql
    assert "\\getenv keycloak_pw KEYCLOAK_DB_PASSWORD" in sql
    assert (
        "ALTER ROLE meho LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
        "CONNECTION LIMIT 60 PASSWORD :'meho_pw';"
    ) in sql
    assert (
        "ALTER ROLE keycloak LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
        "CONNECTION LIMIT 25 PASSWORD :'keycloak_pw';"
    ) in sql
    assert "CREATE DATABASE meho OWNER meho" in sql
    assert "CREATE DATABASE keycloak OWNER keycloak" in sql
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE meho FROM PUBLIC;" in sql
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE keycloak FROM PUBLIC;" in sql
    after_connect = sql.split("\\c meho", 1)[1]
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in after_connect
    # Every CREATE is guarded, so the file is safe to run twice.
    for line in sql.splitlines():
        if line.startswith(("CREATE ROLE", "CREATE DATABASE")):
            pytest.fail(f"unguarded CREATE: {line}")


def test_scripts_are_valid_shell_and_use_the_socket() -> None:
    data = _core()["ConfigMap"]["data"]
    for name in ("apply.sh", "reconcile.sh"):
        result = subprocess.run(["sh", "-n"], input=data[name], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"
    assert "-h /var/run/postgresql -U postgres" in data["apply.sh"]
    assert "pg_isready -q -h 127.0.0.1 -p 5432 -U postgres" in data["reconcile.sh"]
    assert "trap 'exit 0' TERM INT" in data["reconcile.sh"]


def test_user_id_999_by_default() -> None:
    sts = _core()["StatefulSet"]
    ctx = sts["spec"]["template"]["spec"]["securityContext"]
    assert ctx["runAsUser"] == 999
    assert ctx["runAsGroup"] == 999
    assert ctx["fsGroup"] == 999
    assert ctx["runAsNonRoot"] is True
    assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}


def test_openshift_leaves_out_the_user_ids() -> None:
    docs = _render("--set", "corePostgres.enabled=true", "--set", "global.openshift=true")
    sts = next(d for d in docs if d["kind"] == "StatefulSet" and _is_postgres(d))
    for ctx in (
        sts["spec"]["template"]["spec"]["securityContext"],
        _test_pod(docs)["spec"]["securityContext"],
    ):
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(ctx)
        assert ctx["runAsNonRoot"] is True
        assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in _containers(sts).values():
        assert "runAsUser" not in container["securityContext"]
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_network_policy_admits_backplane_and_keycloak_on_5432_only() -> None:
    docs = _render("--set", "corePostgres.enabled=true")
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy" and _is_postgres(d))
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "postgres",
        "app.kubernetes.io/instance": "test",
    }
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    assert rule["ports"] == [{"port": 5432, "protocol": "TCP"}]
    selectors = [peer["podSelector"]["matchLabels"] for peer in rule["from"]]
    assert selectors == [
        {"app.kubernetes.io/name": "meho", "app.kubernetes.io/instance": "test"},
        {"app.kubernetes.io/name": "keycloak", "app.kubernetes.io/instance": "test"},
    ]

    # The helm test pod and the migration Job pod carry the backplane labels,
    # so the policy lets them reach the database.
    backplane = selectors[0]
    test_labels = _test_pod(docs)["metadata"]["labels"]
    assert all(test_labels.get(k) == v for k, v in backplane.items())
    migrate = next(
        d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-migrate")
    )
    job_labels = migrate["spec"]["template"]["metadata"]["labels"]
    assert all(job_labels.get(k) == v for k, v in backplane.items())


def test_network_policy_can_be_turned_off() -> None:
    assert "NetworkPolicy" not in _core("--set", "corePostgres.networkPolicy.enabled=false")


def test_helm_test_pod_uses_the_core_image() -> None:
    pod = _test_pod(_render())
    container = pod["spec"]["containers"][0]
    assert container["image"] == f"pgvector/pgvector@{_DIGEST}"
    assert pod["spec"]["securityContext"]["runAsUser"] == 999
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True


# Helm refuses release names longer than 53 characters.
_LONGEST_RELEASE = "r" * 53


def test_long_release_name_keeps_names_within_limits() -> None:
    ours = _core(release=_LONGEST_RELEASE)
    # The StatefulSet's `controller-revision-hash` label adds 11 characters.
    assert len(ours["StatefulSet"]["metadata"]["name"]) <= 52
    assert len(ours["Service"]["metadata"]["name"]) <= 63
    assert ours["StatefulSet"]["spec"]["serviceName"] == ours["Service"]["metadata"]["name"]


def test_schema_accepts_openshift_and_rejects_unknown_core_postgres_keys() -> None:
    assert _run("--set", "global.openshift=true").returncode == 0
    bad = _run("--set", "corePostgres.enabled=true", "--set", "corePostgres.bogus=1")
    assert bad.returncode != 0
    assert "bogus" in bad.stderr
    bad_digest = _run("--set", "corePostgres.image.digest=latest")
    assert bad_digest.returncode != 0


def test_ci_database_tests_run_on_the_chart_postgres_image() -> None:
    """The database tests and their CI pins use the image the chart ships."""
    values = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    image = values["corePostgres"]["image"]
    expected = f"{image['repository']}@{image['digest']}"
    assert chart_postgres_image() == expected

    ci = (_CHART_DIR.parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    pins = [
        line.split(":", 1)[1].strip()
        for line in ci.splitlines()
        if line.strip().startswith("MEHO_TEST_PGVECTOR_IMAGE:")
    ]
    assert pins
    for pin in pins:
        assert pin.endswith(f"/{expected}"), pin
