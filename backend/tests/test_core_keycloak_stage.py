# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The realm stage of the built-in core's ``bootstrap`` Job (#4043).

The stage runs here against a fake Keycloak that answers the Admin REST
calls the stage makes, seeded the way ``--import-realm`` seeds a fresh
install from the real realm file, and against a fake Secret store. The chart
side is covered by ``test_chart_core_bootstrap.py``; a real Keycloak by
``tests/integration/test_core_bootstrap_keycloak.py``.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from meho_backplane.core import bootstrap as core_bootstrap
from meho_backplane.core import keycloak as core_keycloak
from meho_backplane.core.keycloak import (
    ADMIN_SECRET_ENV,
    BOOTSTRAP_ID_ENV,
    BOOTSTRAP_SECRET_ENV,
    PLAN_ENV,
    HttpAdmin,
    Plan,
    RealmStageError,
    Reply,
    finish_realm,
    load_plan,
    load_realm_file,
)

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
_ROLE_GROUPS = {
    "MEHO-Readers",
    "MEHO-Operators",
    "MEHO-SA-Operators",
    "MEHO-Admins",
    "MEHO-Approvers",
}
_ALNUM_32 = re.compile(r"^[A-Za-z0-9]{32}$")
_TENANT = "3f1c2a7e-9b4d-4e2a-8c1f-5d6e7f8a9b0c"
_CI_TENANT = "9a7b6c5d-4e3f-4a2b-8c1d-0e9f8a7b6c5d"
_LAB_B = "5d0f7a3e-1c2b-4e8d-9f6a-7b3c2d1e0f9a"
_ROLES = ("Readers", "Operators", "SA-Operators", "Admins", "Approvers")

_ENV = {
    BOOTSTRAP_ID_ENV: "boot-client",
    BOOTSTRAP_SECRET_ENV: "boot-secret-value",
    ADMIN_SECRET_ENV: "admin-secret-value",
    "MEHO_REALM_PUBLIC_URL": "https://meho.test",
    "MEHO_REALM_TENANT_ID": _TENANT,
    "MEHO_REALM_TENANT_NAME": "main",
    "MEHO_REALM_WEB_CLIENT_SECRET": "web-secret-value",
    "MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET": "runner-secret-value",
}


def _plan(realm_file: Path, **overrides: Any) -> Plan:
    return Plan.model_validate(
        {
            "keycloak_url": "http://rel-keycloak:8080/auth",
            "realm_file": str(realm_file),
            "first_login_secret": "rel-core-first-login",
            "admin_username": "admin",
            "approver_username": "approver",
            **overrides,
        }
    )


class FakeStore:
    def __init__(self, existing: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.secrets: dict[str, dict[str, str]] = {k: dict(v) for k, v in (existing or {}).items()}
        self.created: list[str] = []

    async def read_data(self, name: str) -> dict[str, str] | None:
        data = self.secrets.get(name)
        return None if data is None else dict(data)

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self.secrets:
            return False
        self.secrets[name] = dict(data)
        self.created.append(name)
        return True


class FakeKeycloak:
    """The Admin REST calls the stage makes, over an in-memory realm."""

    def __init__(self, *, bootstrap: tuple[str, str] | None, ignore_secret_updates: bool = False):
        self.master_clients: dict[str, dict[str, Any]] = {}
        if bootstrap:
            self.master_clients[bootstrap[0]] = {"id": str(uuid.uuid4()), "secret": bootstrap[1]}
        self.scopes: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.session: str | None = None
        self.ignore_secret_updates = ignore_secret_updates
        # Usernames of people whose next create fails inside Keycloak.
        self.fail_user_create: set[str] = set()

    def import_realm(self, realm: Mapping[str, Any]) -> None:
        """Seed what Keycloak's ``--import-realm`` creates from the file."""
        for name in ("basic", "roles", "web-origins", "acr", "profile", "email", "offline_access"):
            self.scopes[name] = {"id": str(uuid.uuid4()), "name": name}
        for scope in realm.get("clientScopes", []):
            self.scopes[scope["name"]] = {"id": str(uuid.uuid4()), **copy.deepcopy(scope)}
        for group in realm.get("groups", []):
            self.groups[group["name"]] = {"id": str(uuid.uuid4()), **copy.deepcopy(group)}
        for client in realm.get("clients", []):
            self._add_client(client)

    def _add_client(self, rep: Mapping[str, Any]) -> str:
        client = copy.deepcopy(dict(rep))
        client["id"] = str(uuid.uuid4())
        for mapper in client.get("protocolMappers", []):
            mapper["id"] = str(uuid.uuid4())
        self.clients[client["clientId"]] = client
        return client["id"]

    def _group_by_id(self, group_id: str) -> dict[str, Any] | None:
        return next((g for g in self.groups.values() if g["id"] == group_id), None)

    def _client_by_id(self, client_id: str) -> dict[str, Any] | None:
        return next((c for c in self.clients.values() if c["id"] == client_id), None)

    def state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "master": self.master_clients,
                "scopes": self.scopes,
                "groups": self.groups,
                "clients": self.clients,
                "users": self.users,
            }
        )

    async def realm_ready(self) -> bool:
        return True

    def _accepts(self, realm: str, client_id: str, client_secret: str) -> bool:
        if realm == "master":
            client = self.master_clients.get(client_id)
            return bool(client and client["secret"] == client_secret)
        client = self.clients.get(client_id)
        return bool(
            client
            and client.get("serviceAccountsEnabled")
            and client.get("secret") == client_secret
        )

    async def login(self, realm: str, client_id: str, client_secret: str) -> bool:
        if self._accepts(realm, client_id, client_secret):
            self.session = f"{realm}/{client_id}"
            return True
        return False

    async def check_login(self, realm: str, client_id: str, client_secret: str) -> bool:
        return self._accepts(realm, client_id, client_secret)

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Reply:
        self.calls.append((method, path))
        if self.session is None:
            return Reply(401, {"error": "HTTP 401 Unauthorized"})
        params = dict(params or {})
        handler = getattr(self, f"_{method.lower()}")
        result: Reply = handler(path, json, params)
        return result

    def _get(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if path == "/admin/realms/master/clients":
            client = self.master_clients.get(params["clientId"])
            return Reply(
                200, [{"id": client["id"], "clientId": params["clientId"]}] if client else []
            )
        if path == "/admin/realms/meho/client-scopes":
            return Reply(200, [{"id": s["id"], "name": s["name"]} for s in self.scopes.values()])
        if path == "/admin/realms/meho/groups":
            assert params == {"search": params["search"], "exact": "true"}
            group = self.groups.get(params["search"])
            return Reply(200, [{"id": group["id"], "name": group["name"]}] if group else [])
        if path == "/admin/realms/meho/clients":
            client = self.clients.get(params["clientId"])
            return Reply(200, [copy.deepcopy(client)] if client else [])
        if path == "/admin/realms/meho/users":
            assert params == {"username": params["username"], "exact": "true"}
            user = self.users.get(params["username"])
            return Reply(200, [{"id": user["id"], "username": user["username"]}] if user else [])
        if match := re.fullmatch(r"/admin/realms/meho/groups/([^/]+)", path):
            group = self._group_by_id(match.group(1))
            if group is None:
                return Reply(404, {"error": "Could not find group by id"})
            return Reply(200, copy.deepcopy(group))
        if match := re.fullmatch(r"/admin/realms/meho/clients/([^/]+)(/.*)?", path):
            client = self._client_by_id(match.group(1))
            if client is None:
                return Reply(404, {"error": "Could not find client"})
            match match.group(2):
                case None:
                    return Reply(200, copy.deepcopy(client))
                case "/protocol-mappers/models":
                    return Reply(200, copy.deepcopy(client.get("protocolMappers", [])))
                case "/default-client-scopes" | "/optional-client-scopes" as kind:
                    names = client.get(f"{kind[1:].split('-')[0]}ClientScopes", [])
                    return Reply(200, [{"id": self.scopes[n]["id"], "name": n} for n in names])
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if path == "/admin/realms/meho/client-scopes":
            if body["name"] in self.scopes:
                return Reply(409, {"errorMessage": "Client Scope name must be unique"})
            new_id = str(uuid.uuid4())
            self.scopes[body["name"]] = {"id": new_id, **copy.deepcopy(body)}
            return Reply(201, None, f"{path}/{new_id}")
        if path == "/admin/realms/meho/groups":
            if body["name"] in self.groups:
                return Reply(409, {"errorMessage": "Top level group named already exists."})
            new_id = str(uuid.uuid4())
            self.groups[body["name"]] = {"id": new_id, **copy.deepcopy(body)}
            return Reply(201, None, f"{path}/{new_id}")
        if path == "/admin/realms/meho/clients":
            if body["clientId"] in self.clients:
                return Reply(409, {"errorMessage": "Client meho already exists"})
            for name in body.get("defaultClientScopes", []) + body.get("optionalClientScopes", []):
                assert name in self.scopes, name
            return Reply(201, None, f"{path}/{self._add_client(body)}")
        if path == "/admin/realms/meho/users":
            if body["username"] in self.users:
                return Reply(409, {"errorMessage": "User exists with same username"})
            # Keycloak joins the groups of a new user in the same request and
            # the same transaction: a failure makes no user at all.
            groups = [p.removeprefix("/") for p in body.get("groups", [])]
            if body["username"] in self.fail_user_create:
                self.fail_user_create.discard(body["username"])
                return Reply(500, {"error": "unknown_error"})
            if any(g not in self.groups for g in groups):
                return Reply(500, {"error": "unknown_error"})
            new_id = str(uuid.uuid4())
            user = {"id": new_id, **copy.deepcopy(body), "groups": groups}
            self.users[body["username"]] = user
            return Reply(201, None, f"{path}/{new_id}")
        if match := re.fullmatch(
            r"/admin/realms/meho/clients/([^/]+)/protocol-mappers/models", path
        ):
            client = self._client_by_id(match.group(1))
            assert client is not None
            if any(m["name"] == body["name"] for m in client.get("protocolMappers", [])):
                return Reply(409, {"errorMessage": "Protocol mapper exists with same name"})
            mapper = {"id": str(uuid.uuid4()), **copy.deepcopy(body)}
            client.setdefault("protocolMappers", []).append(mapper)
            return Reply(201, None, f"{path}/{mapper['id']}")
        raise AssertionError(f"unexpected POST {path}")

    def _put(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if match := re.fullmatch(r"/admin/realms/meho/groups/([^/]+)", path):
            group = self._group_by_id(match.group(1))
            assert group is not None
            assert body["id"] == group["id"]
            assert body["name"] == group["name"]
            # Keycloak replaces the whole attribute map.
            group["attributes"] = copy.deepcopy(body.get("attributes") or {})
            return Reply(204, None)
        if match := re.fullmatch(r"/admin/realms/meho/clients/([^/]+)", path):
            client = self._client_by_id(match.group(1))
            assert client is not None
            assert body["id"] == client["id"]
            if not self.ignore_secret_updates:
                client["secret"] = body.get("secret", client.get("secret"))
            return Reply(204, None)
        if match := re.fullmatch(
            r"/admin/realms/meho/clients/([^/]+)/(default|optional)-client-scopes/([^/]+)", path
        ):
            client = self._client_by_id(match.group(1))
            assert client is not None
            name = next(n for n, s in self.scopes.items() if s["id"] == match.group(3))
            client.setdefault(f"{match.group(2)}ClientScopes", []).append(name)
            return Reply(204, None)
        raise AssertionError(f"unexpected PUT {path}")

    def _delete(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if match := re.fullmatch(r"/admin/realms/master/clients/([^/]+)", path):
            assert self.session is not None and self.session.startswith("master/")
            for client_id, client in list(self.master_clients.items()):
                if client["id"] == match.group(1):
                    del self.master_clients[client_id]
                    return Reply(204, None)
            return Reply(404, {"error": "Could not find client"})
        raise AssertionError(f"unexpected DELETE {path}")


def _realm_as_imported(realm_file: Path = _REALM_FILE) -> dict[str, Any]:
    return load_realm_file(str(realm_file), _ENV)


def _fresh(*, bootstrap: bool = True, **kwargs: Any) -> FakeKeycloak:
    kc = FakeKeycloak(
        bootstrap=(_ENV[BOOTSTRAP_ID_ENV], _ENV[BOOTSTRAP_SECRET_ENV]) if bootstrap else None,
        **kwargs,
    )
    kc.import_realm(_realm_as_imported())
    return kc


@pytest.fixture
def realm_file() -> Path:
    return _REALM_FILE


async def test_fresh_install_makes_people_sets_secrets_and_removes_the_first_start_account(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    kc = _fresh()
    store = FakeStore()

    await finish_realm(_plan(realm_file), _ENV, kc, store)

    secret = store.secrets["rel-core-first-login"]
    assert set(secret) == {
        "admin_username",
        "admin_password",
        "approver_username",
        "approver_password",
    }
    assert (secret["admin_username"], secret["approver_username"]) == ("admin", "approver")
    assert _ALNUM_32.match(secret["admin_password"])
    assert _ALNUM_32.match(secret["approver_password"])
    assert secret["admin_password"] != secret["approver_password"]

    admin = kc.users["admin"]
    assert admin["enabled"] is True
    assert admin["credentials"] == [
        {"type": "password", "value": secret["admin_password"], "temporary": True}
    ]
    assert admin["requiredActions"] == ["UPDATE_PASSWORD"]
    assert admin["groups"] == ["MEHO-Admins", "MEHO-Platform-Admins"]
    approver = kc.users["approver"]
    assert approver["credentials"][0]["value"] == secret["approver_password"]
    assert approver["credentials"][0]["temporary"] is True
    assert approver["groups"] == ["MEHO-SA-Operators"]

    assert kc.clients["meho-admin"]["secret"] == _ENV[ADMIN_SECRET_ENV]
    assert kc.clients["meho-web"]["secret"] == _ENV["MEHO_REALM_WEB_CLIENT_SECRET"]
    assert (
        kc.clients["meho-check-runner"]["secret"] == (_ENV["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"])
    )
    assert kc.master_clients == {}
    assert "meho-ci" not in kc.clients
    out = capfd.readouterr().out
    assert "login: first-start account" in out
    assert "secret rel-core-first-login: created" in out
    assert "user admin: created (in MEHO-Admins, MEHO-Platform-Admins)" in out
    assert "user approver: created (in MEHO-SA-Operators)" in out
    assert "first-start account: removed" in out


async def test_each_person_is_in_exactly_one_role_group(realm_file: Path) -> None:
    kc = _fresh()
    await finish_realm(_plan(realm_file), _ENV, kc, FakeStore())

    for user in kc.users.values():
        assert len(_ROLE_GROUPS & set(user["groups"])) == 1, user["username"]
    assert set(kc.groups) > _ROLE_GROUPS
    assert set(kc.groups["MEHO-Platform-Admins"]["attributes"]) == {"platform_admin"}


async def test_second_run_changes_nothing(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    kc = _fresh()
    store = FakeStore()
    plan = _plan(realm_file)
    await finish_realm(plan, _ENV, kc, store)
    first = kc.state()
    first_secrets = copy.deepcopy(store.secrets)
    kc.calls.clear()
    capfd.readouterr()

    await finish_realm(plan, _ENV, kc, store)

    assert kc.state() == first
    assert store.secrets == first_secrets
    assert store.created == ["rel-core-first-login"]
    assert not [c for c in kc.calls if c[0] in ("POST", "DELETE")]
    out = capfd.readouterr().out
    assert "login: meho-admin" in out
    assert "created" not in out
    assert "user admin: kept" in out
    assert "secret rel-core-first-login: kept" in out


async def test_upgrade_adds_what_a_newer_realm_file_brings(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """An older install lacks a scope, a group, a client and a mapper the new file has."""
    old = _realm_as_imported()
    old["clientScopes"] = [s for s in old["clientScopes"] if s["name"] != "mcp:admin"]
    old["groups"] = [g for g in old["groups"] if g["name"] != "MEHO-Platform-Admins"]
    old["clients"] = [c for c in old["clients"] if c["clientId"] != "meho-check-runner"]
    mcp = next(c for c in old["clients"] if c["clientId"] == "meho-mcp")
    mcp["optionalClientScopes"].remove("mcp:admin")
    mcp["protocolMappers"] = [m for m in mcp["protocolMappers"] if m["name"] != "platform-admin"]
    kc = FakeKeycloak(bootstrap=None)
    kc.import_realm(old)
    kept_mapper_ids = {m["name"]: m["id"] for m in kc.clients["meho-mcp"]["protocolMappers"]}
    kc.users["admin"] = {"id": "u1", "username": "admin", "groups": ["MEHO-Admins"]}
    kc.users["approver"] = {"id": "u2", "username": "approver", "groups": ["MEHO-SA-Operators"]}

    await finish_realm(_plan(_REALM_FILE), _ENV, kc, FakeStore())

    assert "mcp:admin" in kc.scopes
    assert kc.groups["MEHO-Platform-Admins"]["attributes"] == {"platform_admin": ["true"]}
    assert (
        kc.clients["meho-check-runner"]["secret"] == (_ENV["MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET"])
    )
    mcp_now = kc.clients["meho-mcp"]
    assert "mcp:admin" in mcp_now["optionalClientScopes"]
    mappers = {m["name"]: m for m in mcp_now["protocolMappers"]}
    assert mappers["platform-admin"]["config"]["user.attribute"] == "platform_admin"
    for name, mapper_id in kept_mapper_ids.items():
        assert mappers[name]["id"] == mapper_id, "existing mappers are not recreated"
    # Existing people are left alone, even when the first-login Secret is missing.
    assert kc.users["admin"]["groups"] == ["MEHO-Admins"]
    out = capfd.readouterr().out
    assert "client scope mcp:admin: created" in out
    assert "group MEHO-Platform-Admins: created" in out
    assert "client meho-check-runner: created" in out
    assert (
        "client meho-mcp: kept (mapper platform-admin added, optional scope mcp:admin added)" in out
    )
    assert "client meho-web: kept (secret set)" in out
    assert "user admin: kept" in out


async def test_output_never_contains_a_secret_value(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    kc = _fresh()
    store = FakeStore()
    plan = _plan(
        realm_file,
        ci={"enabled": True, "tenant_id": _CI_TENANT, "secret_name": "rel-core-ci-login"},
    )
    await finish_realm(plan, _ENV, kc, store)
    kc.master_clients["boot-client"] = {"id": "again", "secret": _ENV[BOOTSTRAP_SECRET_ENV]}
    await finish_realm(plan, _ENV, kc, store)

    captured = capfd.readouterr()
    output = captured.out + captured.err
    values = [v for k, v in _ENV.items() if "SECRET" in k]
    values += [v for data in store.secrets.values() for k, v in data.items() if "password" in k]
    values += [store.secrets["rel-core-ci-login"]["client_secret"]]
    assert len(values) == 7
    for value in values:
        assert value not in output
    assert "\n" in output


async def test_without_the_first_start_account_the_stage_logs_in_as_meho_admin(
    realm_file: Path,
) -> None:
    kc = _fresh(bootstrap=False)
    env = {k: v for k, v in _ENV.items() if not k.startswith("MEHO_CORE_BOOTSTRAP")}

    await finish_realm(_plan(realm_file), env, kc, FakeStore())

    assert kc.session == "meho/meho-admin"
    assert "admin" in kc.users


async def test_no_login_at_all_stops_the_stage(realm_file: Path) -> None:
    kc = _fresh(bootstrap=False)
    kc.clients["meho-admin"]["secret"] = "rotated-in-keycloak-only"

    with pytest.raises(RealmStageError, match="cannot log in to Keycloak"):
        await finish_realm(_plan(realm_file), _ENV, kc, FakeStore())
    assert kc.users == {}


async def test_first_start_account_stays_when_meho_admin_cannot_log_in(
    realm_file: Path,
) -> None:
    kc = _fresh(ignore_secret_updates=True)
    kc.clients["meho-admin"]["secret"] = "not-the-secret-in-the-kubernetes-secret"

    with pytest.raises(RealmStageError, match="meho-admin cannot log in"):
        await finish_realm(_plan(realm_file), _ENV, kc, FakeStore())
    assert "boot-client" in kc.master_clients
    assert "admin" in kc.users, "the realm work before the swap is done"


async def test_existing_first_login_secret_is_the_source_for_missing_people(
    realm_file: Path,
) -> None:
    stored = {
        "admin_username": "root-person",
        "admin_password": "P" * 32,
        "approver_username": "second",
        "approver_password": "Q" * 32,
    }
    store = FakeStore(existing={"rel-core-first-login": stored})
    kc = _fresh()

    await finish_realm(_plan(realm_file), _ENV, kc, store)

    assert store.created == []
    assert store.secrets["rel-core-first-login"] == stored
    assert set(kc.users) == {"root-person", "second"}
    assert kc.users["root-person"]["credentials"][0]["value"] == "P" * 32


async def test_a_failed_create_leaves_no_half_made_person_and_the_next_run_makes_both(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A person and their groups are one request: a failure makes neither."""
    kc = _fresh()
    kc.fail_user_create.add("admin")
    store = FakeStore()
    plan = _plan(realm_file)

    with pytest.raises(RealmStageError, match="cannot create user admin: HTTP 500"):
        await finish_realm(plan, _ENV, kc, store)
    assert kc.users == {}, "no person without groups is left behind"
    assert "boot-client" in kc.master_clients, "the first-start account stays for the retry"
    stored = copy.deepcopy(store.secrets["rel-core-first-login"])
    capfd.readouterr()

    await finish_realm(plan, _ENV, kc, store)

    assert kc.users["admin"]["groups"] == ["MEHO-Admins", "MEHO-Platform-Admins"]
    assert kc.users["approver"]["groups"] == ["MEHO-SA-Operators"]
    assert kc.users["admin"]["credentials"][0]["value"] == stored["admin_password"]
    assert store.secrets["rel-core-first-login"] == stored
    assert kc.master_clients == {}
    out = capfd.readouterr().out
    assert "user admin: created (in MEHO-Admins, MEHO-Platform-Admins)" in out
    assert "user approver: created (in MEHO-SA-Operators)" in out


async def test_a_missing_group_is_reported_before_the_person_is_made(tmp_path: Path) -> None:
    realm = _realm_as_imported()
    realm["groups"] = [g for g in realm["groups"] if g["name"] != "MEHO-Platform-Admins"]
    kc = FakeKeycloak(bootstrap=None)
    kc.import_realm(realm)
    older_file = tmp_path / "realm-without-the-platform-group.json"
    older_file.write_text(json.dumps(realm))

    with pytest.raises(RealmStageError, match="group MEHO-Platform-Admins does not exist"):
        await finish_realm(_plan(older_file), _ENV, kc, FakeStore())
    assert kc.users == {}


@pytest.mark.parametrize(
    ("admin", "approver"),
    [("admin", "admin"), ("Admin", "admin"), ("admin", " admin ")],
)
async def test_two_names_for_the_same_keycloak_user_stop_the_stage_before_the_secret(
    realm_file: Path, admin: str, approver: str
) -> None:
    kc = _fresh()
    store = FakeStore()
    plan = _plan(realm_file, admin_username=admin, approver_username=approver)

    with pytest.raises(RealmStageError, match="two different people") as caught:
        await finish_realm(plan, _ENV, kc, store)

    assert "the chart names" in str(caught.value)
    assert store.secrets == {}
    assert kc.users == {}
    assert "boot-client" in kc.master_clients


async def test_a_first_login_secret_with_one_name_twice_stops_the_stage(realm_file: Path) -> None:
    stored = {
        "admin_username": "same",
        "admin_password": "P" * 32,
        "approver_username": "SAME",
        "approver_password": "Q" * 32,
    }
    store = FakeStore(existing={"rel-core-first-login": stored})
    kc = _fresh()

    with pytest.raises(RealmStageError, match='Secret "rel-core-first-login" names'):
        await finish_realm(_plan(realm_file), _ENV, kc, store)
    assert kc.users == {}


async def test_first_login_secret_without_a_key_stops_the_stage(realm_file: Path) -> None:
    store = FakeStore(existing={"rel-core-first-login": {"admin_username": "admin"}})

    with pytest.raises(RealmStageError) as caught:
        await finish_realm(_plan(realm_file), _ENV, _fresh(), store)

    assert '"rel-core-first-login"' in str(caught.value)
    assert '"admin_password"' in str(caught.value)


async def test_ci_login_makes_a_client_a_group_and_a_secret(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    kc = _fresh()
    store = FakeStore()
    plan = _plan(
        realm_file,
        ci={"enabled": True, "tenant_id": _CI_TENANT, "secret_name": "rel-core-ci-login"},
    )

    await finish_realm(plan, _ENV, kc, store)

    secret = store.secrets["rel-core-ci-login"]
    assert secret["client_id"] == "meho-ci"
    assert _ALNUM_32.match(secret["client_secret"])
    client = kc.clients["meho-ci"]
    assert client["secret"] == secret["client_secret"]
    assert client["serviceAccountsEnabled"] is True
    assert client["publicClient"] is False
    claims = {
        m["config"]["claim.name"]: m["config"]["claim.value"]
        for m in client["protocolMappers"]
        if m["protocolMapper"] == "oidc-hardcoded-claim-mapper"
    }
    assert claims == {"tenant_id": _TENANT, "tenant_role": "tenant_admin"}
    audiences = {
        m["config"]["included.custom.audience"]
        for m in client["protocolMappers"]
        if m["protocolMapper"] == "oidc-audience-mapper"
    }
    assert audiences == {"meho-backplane", "https://meho.test/mcp"}
    assert "basic" in client["defaultClientScopes"]
    assert kc.groups["MEHO-CI-Admins"]["attributes"] == {
        "tenant_id": [_CI_TENANT],
        "tenant_role": ["tenant_admin"],
    }
    out = capfd.readouterr().out
    assert "secret rel-core-ci-login: created" in out
    assert "client meho-ci: created" in out

    state = kc.state()
    await finish_realm(plan, _ENV, kc, store)
    assert kc.state() == state


async def test_realm_file_placeholder_without_a_value_stops_the_stage(tmp_path: Path) -> None:
    env = {k: v for k, v in _ENV.items() if k != "MEHO_REALM_WEB_CLIENT_SECRET"}

    with pytest.raises(RealmStageError) as caught:
        await finish_realm(_plan(_REALM_FILE), env, _fresh(), FakeStore())

    assert str(caught.value) == (
        "the realm file needs MEHO_REALM_WEB_CLIENT_SECRET, which is not set on the Job"
    )


@pytest.mark.parametrize(
    "missing", [ADMIN_SECRET_ENV, "MEHO_REALM_TENANT_ID", "MEHO_REALM_TENANT_NAME"]
)
async def test_required_env_is_checked_before_any_call(missing: str) -> None:
    env = {k: v for k, v in _ENV.items() if k != missing}
    kc = _fresh()

    with pytest.raises(RealmStageError, match=f"{missing} is not set"):
        await finish_realm(_plan(_REALM_FILE), env, kc, FakeStore())
    assert kc.calls == []


def test_load_realm_file_fills_every_placeholder() -> None:
    realm = load_realm_file(str(_REALM_FILE), _ENV)
    text = json.dumps(realm)
    assert "${" not in text
    web = next(c for c in realm["clients"] if c["clientId"] == "meho-web")
    assert web["secret"] == "web-secret-value"
    assert web["redirectUris"] == ["https://meho.test/ui/auth/callback"]
    assert realm["groups"][0]["attributes"]["tenant_id"] == [_TENANT]
    assert realm["groups"][0]["attributes"]["meho_tenant"] == [f"{_TENANT}:main:Readers"]


def test_load_realm_file_errors_name_the_file(tmp_path: Path) -> None:
    with pytest.raises(RealmStageError, match="cannot read the realm file"):
        load_realm_file(str(tmp_path / "missing.json"), _ENV)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RealmStageError, match="is not valid JSON"):
        load_realm_file(str(bad), _ENV)


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "is not set"),
        ("not json", "is not a valid plan"),
        ('{"keycloak_url": "u"}', "realm_file"),
        (
            '{"keycloak_url": "u", "realm_file": "f", "first_login_secret": "s", '
            '"admin_username": "a", "approver_username": "b", "extra": 1}',
            "extra",
        ),
    ],
)
def test_load_plan_rejects_a_bad_plan(raw: str, problem: str) -> None:
    with pytest.raises(RealmStageError, match=re.escape(problem)):
        load_plan({PLAN_ENV: raw} if raw else {})


# --- Several tenants (docs/decisions/2026-10-28-several-tenants-per-person.md)


async def test_extra_tenant_gets_five_groups_with_only_meho_tenant(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    kc = _fresh()
    plan = _plan(realm_file, tenants=[{"name": "lab-b", "id": _LAB_B}])

    store = FakeStore()
    await finish_realm(plan, _ENV, kc, store)

    for role in _ROLES:
        group = kc.groups[f"MEHO-lab-b-{role}"]
        assert group["attributes"] == {"meho_tenant": [f"{_LAB_B}:lab-b:{role}"]}
    for user in kc.users.values():
        assert not [g for g in user["groups"] if g.startswith("MEHO-lab-b-")]
    assert "group MEHO-lab-b-Readers: created" in capfd.readouterr().out

    state = kc.state()
    kc.calls.clear()
    await finish_realm(plan, _ENV, kc, store)
    assert kc.state() == state
    assert not [c for c in kc.calls if c[0] in ("POST", "DELETE")]
    assert not [c for c in kc.calls if c[0] == "PUT" and "/groups/" in c[1]]


async def test_existing_extra_tenant_group_loses_the_old_access_attributes(
    realm_file: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A group made by hand with tenant_id and tenant_role keeps only meho_tenant and the rest."""
    kc = _fresh()
    kc.groups["MEHO-lab-b-Operators"] = {
        "id": "g-lab-b-operators",
        "name": "MEHO-lab-b-Operators",
        "attributes": {
            "tenant_id": [_LAB_B],
            "tenant_role": ["operator"],
            "approver": ["true"],
            "note": ["kept"],
        },
    }
    plan = _plan(realm_file, tenants=[{"name": "lab-b", "id": _LAB_B}])

    store = FakeStore()
    await finish_realm(plan, _ENV, kc, store)

    assert kc.groups["MEHO-lab-b-Operators"]["attributes"] == {
        "meho_tenant": [f"{_LAB_B}:lab-b:Operators"],
        "note": ["kept"],
    }
    assert kc.groups["MEHO-lab-b-Readers"]["attributes"] == {
        "meho_tenant": [f"{_LAB_B}:lab-b:Readers"]
    }
    assert kc.groups["MEHO-Operators"]["attributes"]["tenant_id"] == [_TENANT]
    out = capfd.readouterr().out
    assert "group MEHO-lab-b-Operators: kept (attributes set)" in out

    state = kc.state()
    kc.calls.clear()
    await finish_realm(plan, _ENV, kc, store)
    assert kc.state() == state
    assert not [c for c in kc.calls if c[0] == "PUT" and "/groups/" in c[1]]


async def test_one_tenant_install_gets_meho_tenant_on_its_groups(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """An install from before: groups without meho_tenant, clients without the mapper."""
    old = _realm_as_imported()
    for group in old["groups"]:
        group["attributes"].pop("meho_tenant", None)
        group["attributes"]["note"] = ["kept"]
    for client in old["clients"]:
        client["protocolMappers"] = [
            m for m in client.get("protocolMappers", []) if m["name"] != "meho-tenants"
        ]
    kc = FakeKeycloak(bootstrap=None)
    kc.import_realm(old)
    kc.users["admin"] = {"id": "u1", "username": "admin", "groups": ["MEHO-Admins"]}
    kc.users["approver"] = {"id": "u2", "username": "approver", "groups": ["MEHO-SA-Operators"]}

    await finish_realm(_plan(_REALM_FILE), _ENV, kc, FakeStore())

    roles = {
        "MEHO-Readers": "Readers",
        "MEHO-Operators": "Operators",
        "MEHO-SA-Operators": "SA-Operators",
        "MEHO-Admins": "Admins",
        "MEHO-Approvers": "Approvers",
    }
    for name, role in roles.items():
        attributes = kc.groups[name]["attributes"]
        assert attributes["meho_tenant"] == [f"{_TENANT}:main:{role}"], name
        assert attributes["tenant_id"] == [_TENANT]
        assert attributes["note"] == ["kept"]
    assert kc.groups["MEHO-Platform-Admins"]["attributes"] == {
        "platform_admin": ["true"],
        "note": ["kept"],
    }
    for client_id in ("meho-cli", "meho-mcp", "meho-web"):
        mappers = {m["name"]: m for m in kc.clients[client_id]["protocolMappers"]}
        assert mappers["meho-tenants"]["config"]["claim.name"] == "meho_tenants"
    out = capfd.readouterr().out
    assert "group MEHO-Readers: kept (attributes set)" in out
    assert "group MEHO-Platform-Admins: kept\n" in out
    assert "client meho-cli: kept (mapper meho-tenants added)" in out


@pytest.mark.parametrize(
    ("tenants", "problem"),
    [
        ([{"name": "Lab-B", "id": _LAB_B}], "name"),
        ([{"name": "lab:b", "id": _LAB_B}], "name"),
        ([{"name": "platform", "id": _LAB_B}], "taken"),
        ([{"name": "lab-b", "id": "not-a-uuid"}], "id"),
        (
            [{"name": "lab-b", "id": _LAB_B}, {"name": "lab-b", "id": _CI_TENANT}],
            "its own name",
        ),
    ],
)
def test_plan_refuses_bad_tenants(tenants: list[dict[str, str]], problem: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(problem)):
        _plan(_REALM_FILE, tenants=tenants)


@pytest.mark.parametrize(
    "tenant", [{"name": "main", "id": _LAB_B}, {"name": "lab-b", "id": _TENANT.upper()}]
)
async def test_extra_tenant_like_the_first_stops_before_any_call(tenant: dict[str, str]) -> None:
    kc = _fresh()
    with pytest.raises(RealmStageError, match="the name or the id of the first tenant"):
        await finish_realm(_plan(_REALM_FILE, tenants=[tenant]), _ENV, kc, FakeStore())
    assert kc.calls == []


# --- HttpAdmin over httpx ---------------------------------------------------


def _http(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://rel-keycloak:8080/auth", transport=httpx.MockTransport(handler)
    )


async def test_http_admin_login_and_bearer_calls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/realms/master/protocol/openid-connect/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["client_secret"] == "right":
                return httpx.Response(200, json={"access_token": "tok-123"})
            return httpx.Response(401, json={"error": "invalid_client"})
        if request.url.path == "/auth/admin/realms/meho/groups":
            return httpx.Response(
                201, headers={"Location": "http://x/auth/admin/realms/meho/groups/g1"}
            )
        return httpx.Response(200, json=[{"id": "1"}])

    async with _http(handler) as http:
        admin = HttpAdmin(http)
        assert await admin.login("master", "boot", "wrong") is False
        assert await admin.check_login("master", "boot", "right") is True
        assert await admin.login("master", "boot", "right") is True
        created = await admin.call("POST", "/admin/realms/meho/groups", json={"name": "g"})
        listed = await admin.call("GET", "/admin/realms/meho/clients", params={"clientId": "c"})

    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert form["grant_type"] == "client_credentials"
    assert created == Reply(201, None, "http://x/auth/admin/realms/meho/groups/g1")
    assert listed == Reply(200, [{"id": "1"}], "")
    assert seen[-1].headers["Authorization"] == "Bearer tok-123"
    assert seen[-1].url.params["clientId"] == "c"
    assert seen[-2].headers["Content-Type"] == "application/json"


async def test_http_admin_server_errors_and_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(503, text="down")
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": "x"})
        raise httpx.ConnectError("refused")

    async with _http(handler) as http:
        admin = HttpAdmin(http)
        assert await admin.realm_ready() is True
        with pytest.raises(RealmStageError, match="HTTP 503"):
            await admin.login("meho", "meho-admin", "s")
        with pytest.raises(RealmStageError, match="ConnectError"):
            await admin.call("GET", "/admin/realms/meho/clients")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _http(unreachable) as http:
        admin = HttpAdmin(http)
        assert await admin.realm_ready() is False
        with pytest.raises(RealmStageError, match="ConnectError"):
            await admin.login("meho", "meho-admin", "s")


# --- The Job entry point -----------------------------------------------------


def test_main_runs_the_keycloak_stage(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    ran: list[tuple[Plan, str]] = []

    async def fake_run(plan: Plan, environ: Mapping[str, str], namespace: str) -> None:
        ran.append((plan, namespace))

    monkeypatch.setattr(core_keycloak, "_run_in_cluster", fake_run)
    monkeypatch.setenv(PLAN_ENV, _plan(_REALM_FILE).model_dump_json())
    monkeypatch.setenv("POD_NAMESPACE", "meho")

    assert core_bootstrap.main(["keycloak"]) == 0
    assert ran == [(_plan(_REALM_FILE), "meho")]
    assert capfd.readouterr().err == ""


def test_main_reports_one_line_on_failure(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(PLAN_ENV, _plan(_REALM_FILE).model_dump_json())
    monkeypatch.delenv("POD_NAMESPACE", raising=False)

    assert core_bootstrap.main(["keycloak"]) == 1
    err = capfd.readouterr().err.strip().splitlines()
    assert err == ["core_bootstrap_failed: POD_NAMESPACE is not set; the chart sets it on the Job."]


def test_main_refuses_an_unknown_stage(capfd: pytest.CaptureFixture[str]) -> None:
    assert core_bootstrap.main(["vault"]) == 2
    assert core_bootstrap.main([]) == 2
    assert "stages: keycloak" in capfd.readouterr().err
