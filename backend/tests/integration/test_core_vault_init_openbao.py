# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#4044 — the ``vault-init`` Job against a real OpenBao and a real Keycloak.

OpenBao (the subchart's image) and Keycloak (the subchart's image, realm file
imported) share one Docker network, as they share a namespace in the chart:
OpenBao fetches Keycloak's keys at ``http://keycloak:8080``, while tokens
carry the public issuer. The Job runs on the test host with a fake Secret
store and fake cluster keys (a key pair made here), exactly as it runs in the
pod otherwise:

1. The first run initialises OpenBao, stores the unseal key, sets everything
   up and revokes the root token.
2. Users of two tenants log in through ``meho-cli`` and the ``meho-mcp``
   role: each reads and writes only its own tenant's secrets. Only a member
   of MEHO-Platform-Admins gets the ``meho-admin`` login.
3. The second run logs in with the service-account token and changes
   nothing. No output line holds the key or a token.

Set ``MEHO_TEST_CORE_KEYCLOAK_IMAGE`` / ``MEHO_TEST_CORE_OPENBAO_IMAGE`` to
use mirrors of the subcharts' images. Skips when Docker is missing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from meho_backplane.core.vault_init import HttpBao, Plan, set_up
from tests.integration.conftest import DOCKER_AVAILABLE, SKIP_REASON

_CHART = Path(__file__).resolve().parents[3] / "deploy" / "charts" / "meho"
_PUBLIC_URL = "https://meho.example.test"
_ISSUER = f"{_PUBLIC_URL}/auth/realms/meho"
_TENANT_A = "6b0d7c55-41a8-4b52-9a8e-2f3c1d0e9a77"
_TENANT_B = "0f7e2a4c-5d3b-4e1a-9c8f-7b6a5d4c3b2a"
_CLUSTER_ISSUER = "https://kubernetes.default.svc.cluster.local"
_NAMESPACE = "meho"
_PASSWORD = "it-" + secrets.token_hex(8)
# The server config the chart renders (charts/openbao/templates/configmap.yaml),
# with the addresses the StatefulSet gives through the environment.
_HCL = """
ui           = false
api_addr     = "http://openbao:8200"
cluster_addr = "https://openbao:8201"
storage "raft" {
  path    = "/openbao/file"
  node_id = "openbao-0"
}
listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = 1
}
audit "file" "stdout" {
  options {
    file_path = "stdout"
  }
}
"""


class _Store:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.history: list[tuple[str, dict[str, str]]] = []

    async def read_data(self, name: str) -> dict[str, str] | None:
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    async def put(self, name: str, data: Mapping[str, str]) -> None:
        self.secrets.setdefault(name, {}).update(data)
        self.history.append((name, dict(data)))

    async def delete(self, name: str) -> None:
        self.secrets.pop(name, None)


class _Cluster:
    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._pem = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    async def issuer_keys(self) -> tuple[str, list[str]]:
        return _CLUSTER_ISSUER, [self._pem]


@dataclass
class _Env:
    bao_url: str
    kc_url: str
    plan: Plan
    store: _Store
    cluster: _Cluster
    key: rsa.RSAPrivateKey
    outputs: list[str] = field(default_factory=list)


def _image(subchart: str, variable: str) -> str:
    override = os.environ.get(variable)
    if override:
        return override
    image = yaml.safe_load((_CHART / "charts" / subchart / "values.yaml").read_text())["image"]
    return f"{image['repository']}@{image['digest']}"


def _wait(url: str, timeout: float, ok: Any = lambda r: r.is_success) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if ok(httpx.get(url)):
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"{url} did not answer in time")
        time.sleep(2)


def _sa_token(key: rsa.RSAPrivateKey, subject: str) -> str:
    now = int(time.time())
    claims = {
        "iss": _CLUSTER_ISSUER,
        "sub": subject,
        "aud": ["openbao"],
        "iat": now,
        "exp": now + 600,
    }
    return jwt.encode(claims, key, algorithm="RS256")


async def _run(env: _Env) -> None:
    async def realm_ready() -> bool:
        try:
            reply = httpx.get(f"{env.kc_url}/realms/meho/.well-known/openid-configuration")
        except httpx.TransportError:
            return False
        return reply.is_success

    out = io.StringIO()
    async with httpx.AsyncClient(base_url=env.bao_url, timeout=30) as http:
        with contextlib.redirect_stdout(out):
            await set_up(env.plan, _NAMESPACE, HttpBao(http), env.cluster, env.store, realm_ready)
    env.outputs.append(out.getvalue())


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Env]:
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network

    from tests._strategies import wait_for_log_message

    tmp = tmp_path_factory.mktemp("openbao")
    (tmp / "config").mkdir()
    (tmp / "config" / "server.hcl").write_text(_HCL)

    network = Network()
    network.create()
    keycloak = (
        DockerContainer(_image("keycloak", "MEHO_TEST_CORE_KEYCLOAK_IMAGE"))
        .with_network(network)
        .with_network_aliases("keycloak")
        .with_command("start-dev --import-realm")
        .with_env("KC_HTTP_RELATIVE_PATH", "/auth")
        .with_env("KC_HOSTNAME", f"{_PUBLIC_URL}/auth")
        .with_env("KC_HOSTNAME_BACKCHANNEL_DYNAMIC", "true")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", _PASSWORD)
        .with_env("MEHO_REALM_PUBLIC_URL", _PUBLIC_URL)
        .with_env("MEHO_REALM_TENANT_ID", _TENANT_A)
        .with_env("MEHO_REALM_ADMIN_CLIENT_SECRET", secrets.token_hex(16))
        .with_env("MEHO_REALM_WEB_CLIENT_SECRET", secrets.token_hex(16))
        .with_env("MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET", secrets.token_hex(16))
        .with_volume_mapping(
            str(_CHART / "charts" / "keycloak" / "files"), "/opt/keycloak/data/import", "ro"
        )
        .with_exposed_ports(8080)
    )
    openbao = (
        DockerContainer(_image("openbao", "MEHO_TEST_CORE_OPENBAO_IMAGE"))
        .with_network(network)
        .with_network_aliases("openbao")
        .with_command("server")
        .with_env("SKIP_CHOWN", "true")
        .with_volume_mapping(str(tmp / "config"), "/openbao/config", "ro")
        .with_exposed_ports(8200)
    )
    try:
        keycloak.start()
        openbao.start()
    except Exception as exc:
        network.remove()
        pytest.skip(f"a container failed to start ({type(exc).__name__}): {exc}")
    try:
        wait_for_log_message(keycloak, "Listening on:", timeout=240)
        kc_url = f"http://{keycloak.get_container_host_ip()}:{keycloak.get_exposed_port(8080)}/auth"
        bao_url = f"http://{openbao.get_container_host_ip()}:{openbao.get_exposed_port(8200)}"
        _wait(f"{kc_url}/realms/meho/.well-known/openid-configuration", 120)
        _wait(f"{bao_url}/v1/sys/seal-status", 60, ok=lambda r: r.status_code == 200)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token_file = tmp / "sa-token"
        token_file.write_text(_sa_token(key, f"system:serviceaccount:{_NAMESPACE}:rel-vault-init"))
        plan = Plan(
            openbao_url=bao_url,
            keycloak_url=kc_url,
            jwks_url="http://keycloak:8080/auth/realms/meho/protocol/openid-connect/certs",
            bound_issuer=_ISSUER,
            audience="meho-backplane",
            policy_dir=str(_CHART / "files" / "openbao" / "policies"),
            service_account="rel-vault-init",
            sa_token_file=str(token_file),
            unseal_secret="rel-core-vault-unseal",
            init_secret="rel-core-vault-init",
            scheduler_secret="rel-core-vault-scheduler",
            snapshot_secret="rel-core-vault-snapshot",
        )
        env = _Env(bao_url, kc_url, plan, _Store(), _Cluster(key), key)
        _make_users(kc_url)
        yield env
    finally:
        openbao.stop()
        keycloak.stop()
        network.remove()


def _make_users(kc_url: str) -> None:
    master = httpx.post(
        f"{kc_url}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": _PASSWORD,
        },
    )
    master.raise_for_status()
    headers = {"Authorization": f"Bearer {master.json()['access_token']}"}
    admin = f"{kc_url}/admin/realms/meho"

    def call(method: str, path: str, body: Any = None) -> Any:
        reply = httpx.request(method, f"{admin}/{path}", headers=headers, json=body)
        reply.raise_for_status()
        return reply.json() if reply.content else None

    # Test only: the password login on meho-cli, so the test gets user tokens.
    (cli,) = call("GET", "clients?clientId=meho-cli")
    call("PUT", f"clients/{cli['id']}", {**cli, "directAccessGrantsEnabled": True})
    call(
        "POST",
        "groups",
        {
            "name": "B-Admins",
            "attributes": {"tenant_id": [_TENANT_B], "tenant_role": ["tenant_admin"]},
        },
    )
    groups = {g["name"]: g["id"] for g in call("GET", "groups?max=100")}
    for user, member_of in (
        ("anna", ["MEHO-Admins"]),
        ("bob", ["B-Admins"]),
        ("pat", ["MEHO-Admins", "MEHO-Platform-Admins"]),
    ):
        call(
            "POST",
            "users",
            {
                "username": user,
                "enabled": True,
                "email": f"{user}@meho.example.test",
                "emailVerified": True,
                "firstName": user,
                "lastName": "Test",
                "credentials": [{"type": "password", "value": _PASSWORD, "temporary": False}],
            },
        )
        (found,) = call("GET", f"users?username={user}&exact=true")
        for group in member_of:
            call("PUT", f"users/{found['id']}/groups/{groups[group]}")


def _user_jwt(env: _Env, user: str) -> str:
    reply = httpx.post(
        f"{env.kc_url}/realms/meho/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "meho-cli",
            "username": user,
            "password": _PASSWORD,
            "scope": "openid",
        },
    )
    reply.raise_for_status()
    token: str = reply.json()["access_token"]
    return token


def _login(env: _Env, role: str, user_jwt: str) -> httpx.Response:
    return httpx.post(f"{env.bao_url}/v1/auth/jwt/login", json={"role": role, "jwt": user_jwt})


def _bao(env: _Env, method: str, path: str, token: str, body: Any = None) -> int:
    reply = httpx.request(
        method, f"{env.bao_url}/v1/{path}", headers={"X-Vault-Token": token}, json=body
    )
    return reply.status_code


def _admin_token(env: _Env) -> str:
    reply = _login(env, "meho-admin", _user_jwt(env, "pat"))
    assert reply.status_code == 200, reply.text
    token: str = reply.json()["auth"]["client_token"]
    return token


def _state(env: _Env) -> dict[str, Any]:
    """What a run could change, read with the admin login."""
    token = _admin_token(env)

    def get(path: str, method: str = "GET") -> Any:
        reply = httpx.request(method, f"{env.bao_url}/v1/{path}", headers={"X-Vault-Token": token})
        assert reply.status_code == 200, (path, reply.text)
        return reply.json()["data"]

    policies = get("sys/policies/acl", "LIST")["keys"]
    state: dict[str, Any] = {
        "mounts": sorted(get("sys/mounts")),
        "auth": {k: v["accessor"] for k, v in get("sys/auth").items()},
        "policies": {p: get(f"sys/policies/acl/{p}")["policy"] for p in policies},
        "groups": {
            g: get(f"identity/group/name/{g}")["alias"]
            for g in get("identity/group/name", "LIST")["keys"]
        },
        "test_secret": get("secret/data/meho/test/federation")["metadata"]["version"],
    }
    for mount in ("jwt", "kubernetes-sa"):
        state[f"{mount}/config"] = get(f"auth/{mount}/config")
        for role in get(f"auth/{mount}/role", "LIST")["keys"]:
            state[f"{mount}/role/{role}"] = get(f"auth/{mount}/role/{role}")
    state["token-role"] = get("auth/token/roles/meho-periodic")
    return state


async def test_first_run_sets_up_and_revokes_the_root_token(env: _Env) -> None:
    await _run(env)

    puts = dict(env.store.history)
    root = puts[env.plan.init_secret]["token"]
    assert _bao(env, "GET", "auth/token/lookup-self", root) == 403
    assert env.plan.init_secret not in env.store.secrets
    assert set(env.store.secrets) == {
        env.plan.unseal_secret,
        env.plan.scheduler_secret,
        env.plan.snapshot_secret,
    }
    seal = httpx.get(f"{env.bao_url}/v1/sys/seal-status").json()
    assert seal["sealed"] is False
    assert (seal["n"], seal["t"]) == (1, 1)


async def test_each_tenant_reads_and_writes_only_its_own_secrets(env: _Env) -> None:
    write = {"data": {"v": "x"}}
    tokens = {}
    for user in ("anna", "bob"):
        reply = _login(env, "meho-mcp", _user_jwt(env, user))
        assert reply.status_code == 200, reply.text
        tokens[user] = reply.json()["auth"]["client_token"]

    a = f"secret/data/tenants/{_TENANT_A}"
    b = f"secret/data/tenants/{_TENANT_B}"
    assert _bao(env, "POST", f"{a}/target1", tokens["anna"], write) == 200
    assert _bao(env, "GET", f"{a}/target1", tokens["anna"]) == 200
    assert _bao(env, "POST", f"{b}/target1", tokens["bob"], write) == 200
    assert _bao(env, "GET", f"{b}/target1", tokens["anna"]) == 403
    assert _bao(env, "POST", f"{b}/target2", tokens["anna"], write) == 403
    assert _bao(env, "GET", f"{a}/target1", tokens["bob"]) == 403
    # Both read the health check's test secret (policy meho-mcp).
    for token in tokens.values():
        assert _bao(env, "GET", "secret/data/meho/test/federation", token) == 200


async def test_only_platform_admins_get_the_admin_login(env: _Env) -> None:
    assert _bao(env, "GET", "sys/policies/acl/meho-mcp", _admin_token(env)) == 200
    assert _login(env, "meho-admin", _user_jwt(env, "anna")).status_code in (400, 403)


async def test_scheduler_token_writes_agent_credentials(env: _Env) -> None:
    token = env.store.secrets[env.plan.scheduler_secret]["token"]
    body = {"data": {"client_secret": "x"}}
    assert _bao(env, "POST", "secret/data/agents/t_c/credentials", token, body) == 200
    assert _bao(env, "GET", f"secret/data/tenants/{_TENANT_A}/target1", token) == 403


async def test_second_run_logs_in_with_its_account_and_changes_nothing(env: _Env) -> None:
    before, secrets_before = _state(env), json.dumps(env.store.secrets, sort_keys=True)

    await _run(env)

    assert "logged in with service account rel-vault-init" in env.outputs[-1]
    assert _state(env) == before
    assert json.dumps(env.store.secrets, sort_keys=True) == secrets_before
    # Another service account of the namespace is refused.
    other = _sa_token(env.key, f"system:serviceaccount:{_NAMESPACE}:other")
    login = httpx.post(
        f"{env.bao_url}/v1/auth/kubernetes-sa/login", json={"role": "vault-init", "jwt": other}
    )
    assert login.status_code in (400, 403)


async def test_no_output_line_holds_the_key_or_a_token(env: _Env) -> None:
    assert len(env.outputs) == 2
    output = "\n".join(env.outputs)
    values = {v for _name, data in env.store.history for v in data.values()}
    assert values
    for value in values:
        assert value not in output
