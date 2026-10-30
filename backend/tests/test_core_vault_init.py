# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``vault-init`` Job of the built-in core (#4044).

The Job runs here against a fake OpenBao that answers the API calls the Job
makes and checks every call of a non-root token against the policies the
Job wrote, so a later run proves the ``meho-configurator`` policy is enough.
The Secret store and the cluster's keys are fakes too. The chart side is in
``test_chart_core_openbao.py``; a real OpenBao and Keycloak in
``tests/integration/test_core_vault_init_openbao.py``.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from meho_backplane.core import vault_init
from meho_backplane.core.vault_init import (
    ACCESSOR_MARK,
    PLAN_ENV,
    HttpBao,
    Plan,
    Reply,
    VaultInitError,
    load_plan,
    load_policies,
    pem_keys,
    set_up,
)

_POLICY_DIR = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "charts"
    / "meho"
    / "files"
    / "openbao"
    / "policies"
)
_NAMESPACE = "meho"
_SA_JWT = "header.sa-token-for-openbao.signature"


def _plan(tmp_path: Path, **overrides: Any) -> Plan:
    token_file = tmp_path / "token"
    token_file.write_text(_SA_JWT + "\n")
    fields: dict[str, Any] = {
        "openbao_url": "http://rel-vault:8200",
        "keycloak_url": "http://rel-keycloak:8080/auth",
        "jwks_url": "http://rel-keycloak:8080/auth/realms/meho/protocol/openid-connect/certs",
        "bound_issuer": "https://meho.test/auth/realms/meho",
        "audience": "meho-backplane",
        "policy_dir": str(_POLICY_DIR),
        "service_account": "rel-vault-init",
        "sa_token_file": str(token_file),
        "unseal_secret": "rel-core-vault-unseal",
        "init_secret": "rel-core-vault-init",
        "scheduler_secret": "rel-core-vault-scheduler",
        "snapshot_secret": "rel-core-vault-snapshot",
    }
    fields.update(overrides)
    return Plan(**fields)


def _capabilities(policy: str) -> list[tuple[str, set[str]]]:
    return [
        (path, set(json.loads(caps)))
        for path, caps in re.findall(r'path "([^"]+)" \{\s*capabilities = (\[[^\]]*\])', policy)
    ]


def _matches(pattern: str, path: str) -> bool:
    if "+" in pattern:
        return fnmatch.fnmatchcase(path, pattern.replace("+", "[!/]*"))
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


class FakeBao:
    """The OpenBao API calls the Job makes, with ACL checks for non-root tokens."""

    def __init__(self) -> None:
        self.initialized = False
        self.sealed = True
        self.key = "unseal-" + secrets.token_hex(8)
        self.root = "root-" + secrets.token_hex(8)
        # token -> its policies; None means root.
        self.tokens: dict[str, list[str] | None] = {}
        self.secret_mounts: dict[str, dict[str, Any]] = {"cubbyhole/": {"type": "cubbyhole"}}
        self.auth_mounts: dict[str, dict[str, Any]] = {
            "token/": {"type": "token", "accessor": "auth_token_0"}
        }
        self.written: dict[str, Any] = {}
        self.policies: dict[str, str] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.kv: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.denied: list[tuple[str, str]] = []
        self.sa_login_ok = True
        # Calls that fail as if the pod were not up yet.
        self.down = 0
        # Writes of the jwt config that fail as if Keycloak's keys were not
        # reachable yet.
        self.jwks_errors = 0
        self.jwks_error = "error checking jwks URL"
        # Health checks that answer "standby", as right after an unseal.
        self.standby = 0

    def state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "mounts": self.secret_mounts,
                "auth": self.auth_mounts,
                "written": self.written,
                "policies": self.policies,
                "groups": self.groups,
                "kv": self.kv,
                "periodic": sorted(
                    t for t, p in self.tokens.items() if p and "meho-configurator" not in p
                ),
            }
        )

    def _allowed(self, token: str, method: str, path: str) -> bool:
        policies = self.tokens[token]
        if policies is None:
            return True
        need = {"GET": {"read"}, "DELETE": {"delete"}}.get(method, {"create", "update"})
        for name in policies:
            for pattern, caps in _capabilities(self.policies.get(name, "")):
                if _matches(pattern, path) and caps & need:
                    return True
        return False

    async def call(self, method: str, path: str, *, token: str = "", json: Any = None) -> Reply:
        self.calls.append((method, path))
        if self.down:
            self.down -= 1
            raise VaultInitError("OpenBao request failed: ConnectError")
        if path == "sys/init":
            if method == "GET":
                return Reply(200, {"initialized": self.initialized})
            assert json == {"secret_shares": 1, "secret_threshold": 1}
            if self.initialized:
                return Reply(400, {"errors": ["already initialized"]})
            self.initialized = True
            self.tokens[self.root] = None
            return Reply(200, {"keys_base64": [self.key], "root_token": self.root})
        if path == "sys/unseal":
            if json["key"] != self.key:
                return Reply(400, {"errors": ["wrong key"]})
            self.sealed = False
            return Reply(200, {"sealed": False})
        if path == "sys/seal-status":
            return Reply(200, {"sealed": self.sealed})
        if path == "sys/health":
            if not self.initialized:
                return Reply(501, {"initialized": False})
            if self.sealed:
                return Reply(503, {"sealed": True})
            if self.standby:
                self.standby -= 1
                return Reply(429, {"standby": True})
            return Reply(200, {"standby": False})
        if self.sealed:
            return Reply(503, {"errors": ["Vault is sealed"]})
        if path == "auth/kubernetes-sa/login":
            role = self.written.get("auth/kubernetes-sa/role/vault-init")
            if not (self.sa_login_ok and role and json == {"role": "vault-init", "jwt": _SA_JWT}):
                return Reply(400, {"errors": ["role not found or token refused"]})
            new = "configurator-" + secrets.token_hex(8)
            self.tokens[new] = list(role["token_policies"])
            return Reply(200, {"auth": {"client_token": new}})
        if token not in self.tokens:
            return Reply(403, {"errors": ["permission denied"]})
        if path in ("auth/token/lookup-self", "auth/token/revoke-self"):
            if path.endswith("revoke-self"):
                del self.tokens[token]
                return Reply(204, None)
            return Reply(200, {"data": {"id": token}})
        if not self._allowed(token, method, path):
            self.denied.append((method, path))
            return Reply(403, {"errors": ["permission denied"]})
        if path == "auth/jwt/config" and self.jwks_errors:
            self.jwks_errors -= 1
            return Reply(400, {"errors": [self.jwks_error]})
        return self._route(method, path, json)

    def _route(self, method: str, path: str, body: Any) -> Reply:
        if path in ("sys/mounts", "sys/auth"):
            mounts = self.secret_mounts if path == "sys/mounts" else self.auth_mounts
            return Reply(200, {"data": copy.deepcopy(mounts), **copy.deepcopy(mounts)})
        if path.startswith("sys/mounts/"):
            self.secret_mounts[path.removeprefix("sys/mounts/") + "/"] = dict(body)
            return Reply(204, None)
        if path.startswith("sys/auth/"):
            name = path.removeprefix("sys/auth/")
            self.auth_mounts[name + "/"] = {"type": body["type"], "accessor": f"auth_jwt_{name}"}
            return Reply(204, None)
        if path.startswith("sys/policies/acl/"):
            self.policies[path.removeprefix("sys/policies/acl/")] = body["policy"]
            return Reply(204, None)
        if path.startswith("identity/group/name/"):
            name = path.removeprefix("identity/group/name/")
            if method == "GET":
                if name not in self.groups:
                    return Reply(404, {"errors": []})
                return Reply(200, {"data": copy.deepcopy(self.groups[name])})
            group = self.groups.setdefault(name, {"id": f"group-{name}", "name": name, "alias": {}})
            group.update(type=body["type"], policies=body["policies"])
            return Reply(204, None)
        if path.startswith("identity/group-alias"):
            group = next(g for g in self.groups.values() if g["id"] == body["canonical_id"])
            group["alias"] = {"id": f"alias-{group['name']}", **body}
            return Reply(200, {"data": {"id": group["alias"]["id"]}})
        if path == "auth/token/create/meho-periodic":
            role = self.written["auth/token/roles/meho-periodic"]
            assert set(body["policies"]) <= set(role["allowed_policies"])
            new = "periodic-" + secrets.token_hex(8)
            self.tokens[new] = list(body["policies"])
            return Reply(200, {"auth": {"client_token": new}})
        if path.startswith("secret/data/"):
            if method == "GET":
                found = self.kv.get(path)
                return Reply(404, {"errors": []}) if found is None else Reply(200, {"data": found})
            self.kv[path] = body
            return Reply(200, {"data": {"version": 1}})
        self.written[path] = copy.deepcopy(body)
        return Reply(204, None)


class FakeCluster:
    def __init__(self) -> None:
        self.keys = ["-----BEGIN PUBLIC KEY-----\ncluster-key\n-----END PUBLIC KEY-----\n"]
        self.fail = False

    async def issuer_keys(self) -> tuple[str, list[str]]:
        if self.fail:
            raise VaultInitError("cannot read the cluster's signing keys: HTTP 500")
        return "https://kubernetes.default.svc.cluster.local", list(self.keys)


class FakeStore:
    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}

    async def read_data(self, name: str) -> dict[str, str] | None:
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    async def put(self, name: str, data: Mapping[str, str]) -> None:
        self.secrets.setdefault(name, {}).update(data)

    async def delete(self, name: str) -> None:
        self.secrets.pop(name, None)


async def _ready() -> bool:
    return True


async def _run(
    plan: Plan, bao: FakeBao, store: FakeStore, cluster: FakeCluster | None = None
) -> None:
    await set_up(plan, _NAMESPACE, bao, cluster or FakeCluster(), store, _ready)


async def test_fresh_install_sets_everything_up(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)

    assert store.secrets[plan.unseal_secret] == {"key": bao.key}
    assert plan.init_secret not in store.secrets
    assert bao.root not in bao.tokens, "the root token must be revoked"
    assert not bao.sealed
    assert bao.secret_mounts["secret/"] == {"type": "kv", "options": {"version": "2"}}
    assert bao.written["auth/jwt/config"] == {
        "jwks_url": plan.jwks_url,
        "bound_issuer": plan.bound_issuer,
    }
    assert set(bao.policies) == vault_init.REQUIRED_POLICIES
    tenant = bao.policies["meho-tenant-operator"]
    assert ACCESSOR_MARK not in tenant
    assert "{{identity.entity.aliases.auth_jwt_jwt.metadata.tenant_id}}" in tenant

    mcp = bao.written["auth/jwt/role/meho-mcp"]
    assert mcp["bound_audiences"] == ["meho-backplane"]
    assert mcp["claim_mappings"] == {"tenant_id": "tenant_id", "tenant_role": "tenant_role"}
    assert mcp["groups_claim"] == "tenant_role"
    assert mcp["token_policies"] == ["meho-mcp"]
    assert {g["alias"]["name"]: n for n, g in bao.groups.items()} == {
        "read_only": "meho-tenant-read-only",
        "operator": "meho-tenant-operator",
        "tenant_admin": "meho-tenant-admin",
    }
    assert all(g["alias"]["mount_accessor"] == "auth_jwt_jwt" for g in bao.groups.values())
    assert all(g["type"] == "external" for g in bao.groups.values())
    admin = bao.written["auth/jwt/role/meho-admin"]
    assert admin["bound_claims"] == {"groups": ["MEHO-Platform-Admins"]}
    assert admin["token_policies"] == ["meho-admin"]

    sa_role = bao.written["auth/kubernetes-sa/role/vault-init"]
    assert sa_role["bound_subject"] == "system:serviceaccount:meho:rel-vault-init"
    assert sa_role["bound_audiences"] == ["openbao"]
    assert sa_role["token_policies"] == ["meho-configurator"]
    assert bao.written["auth/kubernetes-sa/config"]["jwt_validation_pubkeys"] == FakeCluster().keys

    assert bao.tokens[store.secrets[plan.scheduler_secret]["token"]] == ["meho-scheduler"]
    assert bao.tokens[store.secrets[plan.snapshot_secret]["token"]] == ["meho-snapshot"]
    assert bao.written["auth/token/roles/meho-periodic"]["orphan"] is True
    assert "secret/data/meho/test/federation" in bao.kv


async def test_second_run_logs_in_with_its_account_and_changes_nothing(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    before, secrets_before = bao.state(), copy.deepcopy(store.secrets)
    first_run_calls = len(bao.calls)
    capfd.readouterr()

    await _run(plan, bao, store)

    out = capfd.readouterr().out
    assert "logged in with service account rel-vault-init" in out
    assert "root token" not in out
    assert bao.denied == [], "meho-configurator must cover every call of a later run"
    assert bao.state() == before
    assert store.secrets == secrets_before
    second_run = bao.calls[first_run_calls:]
    assert ("PUT", "sys/init") not in second_run
    # A real OpenBao stamps a rewritten group's alias, even when nothing changed.
    assert not [path for _method, path in second_run if path.startswith("identity/group-alias")]
    assert ("POST", "identity/group/name/meho-tenant-admin") not in second_run


async def test_a_run_that_stopped_halfway_goes_on_with_the_root_token(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store, cluster = FakeBao(), FakeStore(), FakeCluster()
    cluster.fail = True
    with pytest.raises(VaultInitError, match="signing keys"):
        await _run(plan, bao, store, cluster)
    assert store.secrets[plan.init_secret] == {"token": bao.root}

    cluster.fail = False
    await _run(plan, bao, store, cluster)

    assert bao.root not in bao.tokens
    assert plan.init_secret not in store.secrets
    assert "auth/kubernetes-sa/role/vault-init" in bao.written


async def test_a_left_over_secret_with_a_revoked_root_token_is_deleted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    # An earlier run stopped right after the revoke, before the delete.
    store.secrets[plan.init_secret] = {"token": bao.root}

    await _run(plan, bao, store)

    assert plan.init_secret not in store.secrets
    assert bao.denied == []


async def test_set_up_server_without_a_way_in_stops_with_one_sentence(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    bao.sa_login_ok = False

    with pytest.raises(VaultInitError, match="cannot log in with its service account") as err:
        await _run(plan, bao, store)
    assert f"root token into the Secret {plan.init_secret}" in str(err.value)


async def test_a_server_set_up_by_hand_is_taken_over_with_a_root_token(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.initialized, bao.sealed = True, False
    bao.tokens[bao.root] = None
    store.secrets[plan.init_secret] = {"token": bao.root}

    await _run(plan, bao, store)

    assert bao.root not in bao.tokens
    assert plan.init_secret not in store.secrets
    assert bao.tokens[store.secrets[plan.scheduler_secret]["token"]] == ["meho-scheduler"]
    assert ("PUT", "sys/init") not in bao.calls


async def test_the_job_waits_until_openbao_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_init, "_POLL_S", 0)
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.down = 3

    await _run(plan, bao, store)

    assert bao.root not in bao.tokens
    assert store.secrets[plan.unseal_secret] == {"key": bao.key}


async def test_the_job_waits_until_openbao_is_active_after_the_unseal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_init, "_POLL_S", 0)
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.standby = 2

    await _run(plan, bao, store)

    unseal = bao.calls.index(("PUT", "sys/unseal"))
    assert bao.calls[unseal + 1 : unseal + 4] == [("GET", "sys/health")] * 3
    assert bao.root not in bao.tokens


async def test_the_job_waits_until_openbao_reads_keycloaks_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(vault_init, "_POLL_S", 0)
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.jwks_errors = 3

    await _run(plan, bao, store)

    assert bao.calls.count(("POST", "auth/jwt/config")) == 4
    assert bao.written["auth/jwt/config"] == {
        "jwks_url": plan.jwks_url,
        "bound_issuer": plan.bound_issuer,
    }
    assert bao.root not in bao.tokens
    assert capfd.readouterr().out.count("cannot read Keycloak's keys yet") == 1


async def test_keycloaks_keys_that_stay_unreadable_stop_after_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault_init, "_POLL_S", 0)
    # Each look at the clock is one second later.
    clock = iter(range(1000))
    monkeypatch.setattr(vault_init, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(vault_init, "_JWKS_WAIT_S", 5)
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.jwks_errors = 1000

    with pytest.raises(
        VaultInitError,
        match=r"^cannot write auth/jwt/config: HTTP 400: error checking jwks URL$",
    ):
        await _run(plan, bao, store)
    assert bao.calls.count(("POST", "auth/jwt/config")) == 6
    assert "auth/jwt/config" not in bao.written


async def test_another_jwt_config_error_stops_at_once(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    bao.jwks_errors, bao.jwks_error = 5, "invalid jwks_url"

    with pytest.raises(VaultInitError, match="invalid jwks_url"):
        await _run(plan, bao, store)
    assert bao.calls.count(("POST", "auth/jwt/config")) == 1


async def test_a_dead_token_in_a_secret_is_replaced(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    old = store.secrets[plan.snapshot_secret]["token"]
    del bao.tokens[old]

    await _run(plan, bao, store)

    new = store.secrets[plan.snapshot_secret]["token"]
    assert new != old
    assert bao.tokens[new] == ["meho-snapshot"]


async def test_a_new_server_replaces_secrets_left_from_an_earlier_server(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    store.secrets[plan.unseal_secret] = {"key": "old-key"}
    store.secrets[plan.scheduler_secret] = {"token": "old-token"}

    await _run(plan, bao, store)

    assert store.secrets[plan.unseal_secret] == {"key": bao.key}
    assert store.secrets[plan.scheduler_secret]["token"] in bao.tokens


async def test_output_never_holds_the_key_or_a_token(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    await _run(plan, bao, store)

    captured = capfd.readouterr()
    output = captured.out + captured.err
    values = [
        bao.key,
        bao.root,
        *bao.tokens,
        *(v for s in store.secrets.values() for v in s.values()),
    ]
    for value in values:
        assert value not in output


async def test_a_secret_mount_of_the_wrong_kind_stops(tmp_path: Path) -> None:
    bao = FakeBao()
    bao.secret_mounts["secret/"] = {"type": "kv", "options": {"version": "1"}}
    with pytest.raises(VaultInitError, match="not a key-value store version 2"):
        await _run(_plan(tmp_path), bao, FakeStore())


async def test_a_missing_policy_file_stops_before_any_call(tmp_path: Path) -> None:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "meho-mcp.hcl").write_text('path "x" { capabilities = ["read"] }')
    bao = FakeBao()
    with pytest.raises(VaultInitError, match="meho-admin"):
        await _run(_plan(tmp_path, policy_dir=str(policies)), bao, FakeStore())
    assert bao.calls == []


async def test_a_sealed_server_that_stays_sealed_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    bao, store = FakeBao(), FakeStore()
    await _run(plan, bao, store)
    bao.sealed = True
    monkeypatch.setattr(vault_init, "_WAIT_S", -1)

    with pytest.raises(VaultInitError, match="still sealed"):
        await _run(plan, bao, store)


def test_policy_files_template_only_on_the_login_accessor() -> None:
    policies = load_policies(str(_POLICY_DIR))
    for name in ("meho-tenant-read-only", "meho-tenant-operator", "meho-tenant-admin"):
        paths = [path for path, _ in _capabilities(policies[name])]
        assert paths, name
        for path in paths:
            # A glob inside the templated part would let one tenant match another.
            assert any(
                path.startswith(
                    f"secret/{kind}/tenants/{{{{identity.entity.aliases.{ACCESSOR_MARK}.metadata.tenant_id}}}}/"
                )
                for kind in ("data", "metadata", "delete")
            ), path


def test_only_tenant_admins_reach_the_privileged_path() -> None:
    policies = load_policies(str(_POLICY_DIR))
    tenant = f"{{{{identity.entity.aliases.{ACCESSOR_MARK}.metadata.tenant_id}}}}"
    privileged = {
        f"secret/data/tenants/{tenant}/privileged/*",
        f"secret/metadata/tenants/{tenant}/privileged/*",
    }
    for name in ("meho-tenant-read-only", "meho-tenant-operator"):
        rules = dict(_capabilities(policies[name]))
        assert all(rules.get(path) == {"deny"} for path in privileged), name
    admin = dict(_capabilities(policies["meho-tenant-admin"]))
    assert "deny" not in set().union(*admin.values())
    assert "read" in admin[f"secret/data/tenants/{tenant}/privileged/*"]


def _jwk(public: Any) -> dict[str, Any]:
    algorithm = (
        jwt.algorithms.RSAAlgorithm
        if isinstance(public, rsa.RSAPublicKey)
        else jwt.algorithms.ECAlgorithm
    )
    key: dict[str, Any] = json.loads(algorithm.to_jwk(public))
    return key


def test_pem_keys_reads_rsa_and_ec_keys_and_skips_others() -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    ec_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    jwks = [_jwk(rsa_key) | {"use": "sig"}, _jwk(ec_key), _jwk(rsa_key) | {"use": "enc"}]

    pems = pem_keys(jwks)

    assert len(pems) == 2
    loaded = [serialization.load_pem_public_key(p.encode()) for p in pems]
    assert loaded[0].public_numbers() == rsa_key.public_numbers()  # type: ignore[union-attr]
    assert loaded[1].public_numbers() == ec_key.public_numbers()  # type: ignore[union-attr]


def test_pem_keys_without_a_signing_key_stops() -> None:
    with pytest.raises(VaultInitError, match="no signing key"):
        pem_keys([])


def test_load_plan_errors_name_the_problem() -> None:
    with pytest.raises(VaultInitError, match="is not set"):
        load_plan({})
    with pytest.raises(VaultInitError, match="openbao_url"):
        load_plan({PLAN_ENV: json.dumps({"keycloak_url": "x"})})


async def test_http_bao_sends_the_token_only_when_there_is_one() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"initialized": True})

    async with httpx.AsyncClient(
        base_url="http://bao:8200", transport=httpx.MockTransport(handler)
    ) as http:
        bao = HttpBao(http)
        assert (await bao.call("GET", "sys/init")).data == {"initialized": True}
        await bao.call("PUT", "sys/policies/acl/x", token="t0k", json={"policy": "p"})

    assert seen[0].url.path == "/v1/sys/init"
    assert "x-vault-token" not in seen[0].headers
    assert seen[1].headers["x-vault-token"] == "t0k"


async def test_http_bao_failure_names_no_request_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom with secret-ish detail")

    async with httpx.AsyncClient(
        base_url="http://bao:8200", transport=httpx.MockTransport(handler)
    ) as http:
        with pytest.raises(VaultInitError) as err:
            await HttpBao(http).call("GET", "sys/init")
    assert str(err.value) == "OpenBao request failed: ConnectError"
