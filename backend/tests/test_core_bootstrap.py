# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The built-in core's realm stage (#4043) against a fake Keycloak.

The fake keeps state the way Keycloak does (clients, mappers, secrets,
groups, users, realm roles, the master realm's bootstrap account) and
records every write, so the tests can prove that a second run writes
nothing. The chart side is ``test_chart_core_bootstrap.py``; the run against
a real Keycloak is ``tests/integration/test_core_bootstrap_keycloak.py``.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from meho_backplane.core import bootstrap
from meho_backplane.core.bootstrap import (
    DONE_ANNOTATION,
    CoreBootstrapError,
    Plan,
    load_plan,
    load_realm,
    main,
    reconcile,
)
from meho_backplane.core.secrets import StoredSecret

_REALM_FILE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "charts"
    / "meho"
    / "charts"
    / "keycloak"
    / "files"
    / "meho-realm.json"
)
_BASE = "http://kc.test:8080/auth"
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"
_SECRET_NAME = "rel-core-first-login"
_ALNUM_32 = re.compile(r"^[A-Za-z0-9]{32}$")

_ENV = {
    "KC_BOOTSTRAP_ADMIN_CLIENT_ID": "bootstrapIdAAAAAAAAAAAAAAAAAAAAAA",
    "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET": "bootstrapSecretBBBBBBBBBBBBBBBBB",
    "MEHO_REALM_ADMIN_CLIENT_SECRET": "adminSecretCCCCCCCCCCCCCCCCCCCCC",
    "MEHO_REALM_WEB_CLIENT_SECRET": "webSecretDDDDDDDDDDDDDDDDDDDDDDD",
    "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": "runnerSecretEEEEEEEEEEEEEEEEEEEE",
    "MEHO_REALM_PUBLIC_URL": "https://meho.test",
}

_GROUPS = [
    {"name": "MEHO-Readers", "tenant_role": "read_only"},
    {"name": "MEHO-Operators", "tenant_role": "operator"},
    {"name": "MEHO-SA-Operators", "tenant_role": "operator"},
    {"name": "MEHO-Admins", "tenant_role": "tenant_admin"},
    {"name": "MEHO-Approvers", "tenant_role": "read_only", "approver": True},
]


def _plan(**overrides: Any) -> Plan:
    return Plan.model_validate(
        {
            "keycloak_url": _BASE,
            "tenant_id": _TENANT,
            "groups": _GROUPS,
            "first_login_secret": _SECRET_NAME,
            "admin": {"username": "admin", "group": "MEHO-Admins"},
            "approver": {"username": "approver", "group": "MEHO-SA-Operators"},
            **overrides,
        }
    )


def _realm() -> dict[str, Any]:
    return load_realm(_REALM_FILE, _ENV)


class FakeStore:
    """In-memory :class:`~meho_backplane.core.bootstrap.FirstLoginStore`."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.annotations: dict[str, dict[str, str]] = {}

    async def read_data(self, name: str) -> StoredSecret | None:
        if name not in self.data:
            return None
        return StoredSecret(data=dict(self.data[name]), annotations=dict(self.annotations[name]))

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self.data:
            return False
        self.data[name] = dict(data)
        self.annotations[name] = {}
        return True

    async def annotate(self, name: str, annotations: Mapping[str, str]) -> None:
        self.annotations[name].update(annotations)


class FakeKeycloak:
    """Just enough of Keycloak 26's token and Admin REST API, with state."""

    def __init__(self, realm: Mapping[str, Any]) -> None:
        self.bootstrap: tuple[str, str] | None = (
            _ENV["KC_BOOTSTRAP_ADMIN_CLIENT_ID"],
            _ENV["KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"],
        )
        self.bootstrap_uuid = str(uuid.uuid4())
        # What `--import-realm` made: the realm file's clients and roles, no groups.
        self.clients: dict[str, dict[str, Any]] = {}
        for rep in realm["clients"]:
            self._add_client(copy.deepcopy(rep))
        self.roles = [{"id": str(uuid.uuid4()), "name": r["name"]} for r in realm["roles"]["realm"]]
        self.groups: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], int] = {}

    def _add_client(self, rep: dict[str, Any]) -> str:
        cid = str(uuid.uuid4())
        rep["id"] = cid
        rep.setdefault("secret", f"generated-{cid}")
        rep.setdefault("protocolMappers", [])
        self.clients[cid] = rep
        return cid

    def client(self, client_id: str) -> dict[str, Any]:
        return next(c for c in self.clients.values() if c["clientId"] == client_id)

    def user(self, username: str) -> dict[str, Any]:
        return next(u for u in self.users.values() if u["username"] == username)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path.removeprefix("/auth")
        query = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        method = request.method
        if method != "GET":
            self.writes.append((method, path))
        for (fail_method, pattern), status in self.fail.items():
            if method == fail_method and re.fullmatch(pattern, path):
                return httpx.Response(status, json={"errorMessage": "injected"})
        is_json = request.headers.get("content-type") == "application/json"
        body = json.loads(request.content) if is_json else None
        if path == "/realms/meho/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": "https://meho.test/auth/realms/meho"})
        if match := re.fullmatch(r"/realms/(\w+)/protocol/openid-connect/token", path):
            return self._token(match.group(1), parse_qs(request.content.decode()))
        if match := re.fullmatch(r"/admin/realms/(\w+)(/.*)", path):
            return self._admin(match.group(1), match.group(2), method, query, body)
        return httpx.Response(404)

    def _token(self, realm: str, form: dict[str, list[str]]) -> httpx.Response:
        creds = (form["client_id"][0], form["client_secret"][0])
        if realm == "master":
            ok = self.bootstrap is not None and creds == self.bootstrap
        else:
            admin = self.client("meho-admin")
            ok = creds == (admin["clientId"], admin["secret"])
        if not ok:
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(200, json={"access_token": f"token-{realm}"})

    def _admin(
        self, realm: str, path: str, method: str, query: dict[str, str], body: Any
    ) -> httpx.Response:
        if realm == "master":
            assert self.bootstrap is not None, "master admin call after the bootstrap was deleted"
            if path == "/clients" and method == "GET":
                found = query["clientId"] == self.bootstrap[0]
                return httpx.Response(
                    200,
                    json=[{"id": self.bootstrap_uuid, "clientId": self.bootstrap[0]}]
                    if found
                    else [],
                )
            if path == f"/clients/{self.bootstrap_uuid}" and method == "DELETE":
                self.bootstrap = None
                return httpx.Response(204)
            return httpx.Response(404)
        if path == "/clients":
            if method == "GET":
                return httpx.Response(
                    200,
                    json=[c for c in self.clients.values() if c["clientId"] == query["clientId"]],
                )
            cid = self._add_client(body)
            return httpx.Response(
                201, headers={"Location": f"{_BASE}/admin/realms/meho/clients/{cid}"}
            )
        if match := re.fullmatch(r"/clients/([^/]+)(/.*)?", path):
            rep = self.clients[match.group(1)]
            rest = match.group(2) or ""
            if rest == "/client-secret":
                return httpx.Response(200, json={"type": "secret", "value": rep["secret"]})
            if rest == "/protocol-mappers/models":
                if method == "GET":
                    return httpx.Response(200, json=rep["protocolMappers"])
                rep["protocolMappers"].append(body)
                return httpx.Response(201)
            if method == "GET":
                return httpx.Response(200, json=rep)
            assert body["id"] == rep["id"], "PUT must send back the full representation"
            self.clients[rep["id"]] = body
            return httpx.Response(204)
        if path == "/groups":
            if method == "GET":
                return httpx.Response(200, json=list(self.groups.values()))
            gid = str(uuid.uuid4())
            self.groups[gid] = {"id": gid, **body}
            return httpx.Response(201)
        if path == "/users":
            if method == "GET":
                found = [u for u in self.users.values() if u["username"] == query["username"]]
                return httpx.Response(200, json=found)
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "roles": [], **body}
            return httpx.Response(
                201, headers={"Location": f"{_BASE}/admin/realms/meho/users/{uid}"}
            )
        if match := re.fullmatch(r"/users/([^/]+)(/.*)?", path):
            uid, rest = match.group(1), match.group(2) or ""
            if method == "DELETE" and not rest:
                del self.users[uid]
                return httpx.Response(204)
            user = self.users[uid]
            if rest == "/groups":
                return httpx.Response(200, json=[{"name": g.lstrip("/")} for g in user["groups"]])
            if rest == "/role-mappings/realm/available":
                return httpx.Response(200, json=[r for r in self.roles if r not in user["roles"]])
            if rest == "/role-mappings/realm":
                user["roles"].extend(body)
                return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def keycloak() -> Any:
    fake = FakeKeycloak(_realm())
    with respx.mock(assert_all_called=False) as router:
        router.route(host="kc.test").mock(side_effect=fake.handle)
        yield fake


async def _run(keycloak: FakeKeycloak, store: FakeStore, plan: Plan | None = None) -> None:
    await reconcile(plan or _plan(), _realm(), _ENV, store)


async def test_fresh_install_makes_groups_people_and_drops_the_bootstrap(
    keycloak: FakeKeycloak,
) -> None:
    store = FakeStore()
    await _run(keycloak, store)

    groups = {g["name"]: g["attributes"] for g in keycloak.groups.values()}
    assert groups["MEHO-Admins"] == {"tenant_id": [_TENANT], "tenant_role": ["tenant_admin"]}
    assert groups["MEHO-Approvers"] == {
        "tenant_id": [_TENANT],
        "tenant_role": ["read_only"],
        "approver": ["true"],
    }
    assert set(groups) == {g["name"] for g in _GROUPS}

    secret = store.data[_SECRET_NAME]
    assert set(secret) == {
        "admin_username",
        "admin_password",
        "approver_username",
        "approver_password",
    }
    assert secret["admin_username"] == "admin"
    assert secret["approver_username"] == "approver"
    assert _ALNUM_32.match(secret["admin_password"])
    assert _ALNUM_32.match(secret["approver_password"])
    assert secret["admin_password"] != secret["approver_password"]
    assert store.annotations[_SECRET_NAME] == {DONE_ANNOTATION: "made"}

    for username, group, password_key, roles in (
        ("admin", "/MEHO-Admins", "admin_password", ["platform_admin"]),
        ("approver", "/MEHO-SA-Operators", "approver_password", []),
    ):
        user = keycloak.user(username)
        # One role group per person: two make the token's role unpredictable.
        assert user["groups"] == [group]
        assert user["credentials"] == [
            {"type": "password", "value": secret[password_key], "temporary": True}
        ]
        assert user["requiredActions"] == ["UPDATE_PASSWORD"]
        assert [r["name"] for r in user["roles"]] == roles

    assert keycloak.bootstrap is None
    assert keycloak.writes[-1][0] == "DELETE" and keycloak.writes[-1][1].startswith(
        "/admin/realms/master/clients/"
    )


async def test_second_run_writes_nothing_and_keeps_every_secret(keycloak: FakeKeycloak) -> None:
    store = FakeStore()
    await _run(keycloak, store)
    secrets_before = {c["clientId"]: c["secret"] for c in keycloak.clients.values()}
    first_login_before = dict(store.data[_SECRET_NAME])
    keycloak.writes.clear()

    await _run(keycloak, store)

    assert keycloak.writes == [
        ("POST", "/realms/master/protocol/openid-connect/token"),
        (
            "POST",
            "/realms/meho/protocol/openid-connect/token",
        ),
    ]
    assert {c["clientId"]: c["secret"] for c in keycloak.clients.values()} == secrets_before
    assert store.data[_SECRET_NAME] == first_login_before


async def test_client_secrets_equal_their_kubernetes_secrets(keycloak: FakeKeycloak) -> None:
    # A rotated Kubernetes Secret: Keycloak still has the old value.
    keycloak.client("meho-web")["secret"] = "old-web-secret"
    await _run(keycloak, FakeStore())

    assert keycloak.client("meho-web")["secret"] == _ENV["MEHO_REALM_WEB_CLIENT_SECRET"]
    assert keycloak.client("meho-admin")["secret"] == _ENV["MEHO_REALM_ADMIN_CLIENT_SECRET"]
    assert (
        keycloak.client("meho-check-runner")["secret"]
        == _ENV["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"]
    )
    # The PUT kept the rest of the client (it sent back the full representation).
    assert keycloak.client("meho-web")["redirectUris"] == ["https://meho.test/ui/auth/callback"]


async def test_upgrade_adds_a_missing_client_and_a_missing_mapper(keycloak: FakeKeycloak) -> None:
    mcp = keycloak.client("meho-mcp")
    del keycloak.clients[mcp["id"]]
    cli = keycloak.client("meho-cli")
    cli["protocolMappers"] = [m for m in cli["protocolMappers"] if m["name"] != "approver"]

    await _run(keycloak, FakeStore())

    assert keycloak.client("meho-mcp")["redirectUris"] == [
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]
    assert "approver" in {m["name"] for m in keycloak.client("meho-cli")["protocolMappers"]}


async def test_existing_group_is_left_as_it_is(keycloak: FakeKeycloak) -> None:
    keycloak.groups["g"] = {"id": "g", "name": "MEHO-Readers", "attributes": {"x": ["y"]}}
    await _run(keycloak, FakeStore())
    assert keycloak.groups["g"]["attributes"] == {"x": ["y"]}
    assert [g["name"] for g in keycloak.groups.values()].count("MEHO-Readers") == 1


async def test_a_person_deleted_after_the_first_install_is_not_made_again(
    keycloak: FakeKeycloak,
) -> None:
    store = FakeStore()
    await _run(keycloak, store)
    del keycloak.users[keycloak.user("approver")["id"]]

    await _run(keycloak, store)

    assert [u["username"] for u in keycloak.users.values()] == ["admin"]


async def test_half_made_person_is_removed_and_the_retry_finishes(keycloak: FakeKeycloak) -> None:
    store = FakeStore()
    keycloak.fail[("POST", r"/admin/realms/meho/users/[^/]+/role-mappings/realm")] = 500

    with pytest.raises(CoreBootstrapError, match="cannot create user admin: HTTP 500: injected"):
        await _run(keycloak, store)

    assert keycloak.users == {}
    # The bootstrap account stays for the retry, and the Secret is not marked.
    assert keycloak.bootstrap is not None
    assert store.annotations[_SECRET_NAME] == {}
    password = store.data[_SECRET_NAME]["admin_password"]

    keycloak.fail.clear()
    await _run(keycloak, store)

    assert keycloak.user("admin")["credentials"][0]["value"] == password
    assert [r["name"] for r in keycloak.user("admin")["roles"]] == ["platform_admin"]
    assert keycloak.bootstrap is None


async def test_admin_client_secret_is_fixed_through_the_bootstrap_account(
    keycloak: FakeKeycloak,
) -> None:
    keycloak.client("meho-admin")["secret"] = "not-the-kubernetes-value"
    await _run(keycloak, FakeStore())
    assert keycloak.client("meho-admin")["secret"] == _ENV["MEHO_REALM_ADMIN_CLIENT_SECRET"]
    assert keycloak.bootstrap is None


async def test_refused_admin_without_a_bootstrap_account_fails(keycloak: FakeKeycloak) -> None:
    keycloak.bootstrap = None
    keycloak.client("meho-admin")["secret"] = "not-the-kubernetes-value"
    with pytest.raises(CoreBootstrapError, match="bootstrap account is gone"):
        await _run(keycloak, FakeStore())


async def test_kept_secret_for_other_people_fails_with_the_fix(keycloak: FakeKeycloak) -> None:
    store = FakeStore()
    await store.create(
        _SECRET_NAME,
        {
            "admin_username": "root",
            "admin_password": "p" * 32,
            "approver_username": "approver",
            "approver_password": "q" * 32,
        },
        "Opaque",
    )
    with pytest.raises(CoreBootstrapError, match='does not hold admin_username "admin"'):
        await _run(keycloak, store)
    assert keycloak.users == {}


async def test_output_never_holds_a_secret_value(
    keycloak: FakeKeycloak, capfd: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    keycloak.client("meho-web")["secret"] = "old-web-secret"
    await _run(keycloak, store)
    await _run(keycloak, store)

    out, err = capfd.readouterr()
    values = [v for k, v in _ENV.items() if k != "MEHO_REALM_PUBLIC_URL"]
    values += [
        store.data[_SECRET_NAME]["admin_password"],
        store.data[_SECRET_NAME]["approver_password"],
    ]
    for value in values:
        assert value not in out
        assert value not in err
    assert "user admin: created in MEHO-Admins" in out
    assert "client meho-web: secret set" in out
    assert "bootstrap account: deleted" in out


async def test_keycloak_that_never_serves_the_realm_fails_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "_WAIT_FOR_REALM_S", 0.0)
    with respx.mock as router:
        router.route(host="kc.test").mock(return_value=httpx.Response(503))
        with pytest.raises(CoreBootstrapError, match=r"did not serve realm meho .* HTTP 503"):
            await reconcile(_plan(), _realm(), _ENV, FakeStore())


def test_plan_rejects_a_person_outside_the_groups() -> None:
    with pytest.raises(ValueError, match="group MEHO-Nobody of approver is not in groups"):
        _plan(approver={"username": "approver", "group": "MEHO-Nobody"})


def test_load_plan_needs_the_env_var() -> None:
    with pytest.raises(CoreBootstrapError, match="MEHO_CORE_BOOTSTRAP_PLAN is not set"):
        load_plan({})


def test_realm_file_placeholders_are_filled_and_a_missing_one_fails() -> None:
    realm = _realm()
    assert "${" not in json.dumps(realm)
    web = next(c for c in realm["clients"] if c["clientId"] == "meho-web")
    assert web["secret"] == _ENV["MEHO_REALM_WEB_CLIENT_SECRET"]
    env = {k: v for k, v in _ENV.items() if k != "MEHO_REALM_PUBLIC_URL"}
    with pytest.raises(CoreBootstrapError, match="MEHO_REALM_PUBLIC_URL is not set"):
        load_realm(_REALM_FILE, env)


def test_main_needs_the_stage_name(capfd: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert "usage: python -m meho_backplane.core.bootstrap keycloak" in capfd.readouterr().err
