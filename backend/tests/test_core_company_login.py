# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The company login of the realm stage (#4592), against a fake Keycloak.

The fake is the one of ``test_core_keycloak_stage.py`` plus the Admin REST
calls for identity providers, their mappers, the login flow's
"Identity Provider Redirector" step, group members and linked logins. A real
Keycloak, with a second realm as the company login, is in
``tests/integration/test_core_company_login_keycloak.py``.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from meho_backplane.core.company_login import (
    CLEANUP_CONFIG_KEY,
    MAPPER_PREFIX,
    REDIRECT_CONFIG_ALIAS,
    SECRET_ENV,
    CompanyLoginPlan,
)
from meho_backplane.core.keycloak import RealmStageError, Reply, finish_realm
from tests.test_core_keycloak_stage import (
    _ENV,
    _REALM_FILE,
    FakeKeycloak,
    FakeStore,
    _plan,
    _realm_as_imported,
)

_BASE = "/admin/realms/meho"
_IDP = f"{_BASE}/identity-provider/instances"
_DISCOVERY = "https://idp.test/application/o/meho/.well-known/openid-configuration"
_SECRET = "company-secret-value-0123456789"
_MASK = "**********"
_ENV_ON = {**_ENV, SECRET_ENV: _SECRET}
_FOUND = {
    "issuer": "https://idp.test/application/o/meho/",
    "authorizationUrl": "https://idp.test/application/o/authorize/",
    "tokenUrl": "https://idp.test/application/o/token/",
    "userInfoUrl": "https://idp.test/application/o/userinfo/",
    "jwksUrl": "https://idp.test/application/o/meho/jwks/",
    "useJwksUrl": "true",
    "validateSignature": "true",
    "logoutUrl": "https://idp.test/application/o/meho/end-session/",
    "metadataDescriptorUrl": _DISCOVERY,
}


class CompanyKeycloak(FakeKeycloak):
    """The fake Keycloak, plus identity providers and the browser login flow."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.providers: dict[str, dict[str, Any]] = {}
        self.provider_mappers: dict[str, dict[str, Any]] = {}
        self.auth_configs: dict[str, dict[str, Any]] = {}
        self.redirector: dict[str, Any] = {
            "id": "exec-idp",
            "providerId": "identity-provider-redirector",
        }
        # The reply to the discovery request, or the error that request raises.
        self.discovery: Reply | RealmStageError = Reply(200, dict(_FOUND))
        self.forbidden = False
        # Usernames of the people that came in through the company login.
        self.linked: set[str] = set()
        # Says which requests (method, path) time out instead of answering.
        self.times_out: Callable[[str, str], bool] | None = None

    def state(self) -> dict[str, Any]:
        return {
            **super().state(),
            **copy.deepcopy(
                {
                    "providers": self.providers,
                    "provider_mappers": self.provider_mappers,
                    "auth_configs": self.auth_configs,
                    "redirector": self.redirector,
                }
            ),
        }

    def _company_path(self, path: str) -> bool:
        return "/identity-provider/" in path or "/authentication/" in path or path == _BASE

    def person(self, username: str, groups: list[str], *, linked: bool) -> None:
        self.users[username] = {"id": str(uuid.uuid4()), "username": username, "groups": groups}
        if linked:
            self.linked.add(username)

    def _user_by_id(self, user_id: str) -> dict[str, Any]:
        return next(u for u in self.users.values() if u["id"] == user_id)

    async def call(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Reply:
        if self.forbidden and self._company_path(path):
            self.calls.append((method, path))
            return Reply(403, {"error": "HTTP 403 Forbidden"})
        if self.times_out is not None and self.times_out(method, path):
            self.calls.append((method, path))
            raise RealmStageError("Keycloak request failed: ReadTimeout")
        return await super().call(method, path, json=json, params=params)

    def _get(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if path == _BASE:
            return Reply(200, {"realm": "meho", "browserFlow": "browser"})
        if path == f"{_BASE}/authentication/flows/browser/executions":
            return Reply(
                200,
                [
                    {"id": "exec-cookie", "providerId": "auth-cookie"},
                    copy.deepcopy(self.redirector),
                ],
            )
        if match := re.fullmatch(rf"{_BASE}/authentication/config/([^/]+)", path):
            config = self.auth_configs.get(match.group(1))
            return Reply(200, copy.deepcopy(config)) if config else Reply(404, None)
        if match := re.fullmatch(rf"{_BASE}/groups/([^/]+)/members", path):
            assert params["briefRepresentation"] == "true"
            name = next(n for n, g in self.groups.items() if g["id"] == match.group(1))
            members = [
                {"id": u["id"], "username": u["username"]}
                for u in self.users.values()
                if name in u["groups"]
            ]
            first, limit = int(params["first"]), int(params["max"])
            return Reply(200, members[first : first + limit])
        if match := re.fullmatch(rf"{_BASE}/users/([^/]+)/federated-identity", path):
            user = self._user_by_id(match.group(1))
            links = [{"identityProvider": "company", "userId": "x", "userName": user["username"]}]
            return Reply(200, links if user["username"] in self.linked else [])
        if match := re.fullmatch(rf"{_IDP}/([^/]+)/mappers", path):
            alias = match.group(1)
            return Reply(
                200,
                [
                    copy.deepcopy(m)
                    for m in self.provider_mappers.values()
                    if m["identityProviderAlias"] == alias
                ],
            )
        if match := re.fullmatch(rf"{_IDP}/([^/]+)", path):
            provider = self.providers.get(match.group(1))
            if provider is None:
                return Reply(404, {"error": "Could not find identity provider"})
            shown = copy.deepcopy(provider)
            shown["config"]["clientSecret"] = _MASK
            return Reply(200, shown)
        return super()._get(path, body, params)

    def _post(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if path == f"{_BASE}/identity-provider/import-config":
            assert body == {"providerId": "oidc", "alias": "company", "fromUrl": _DISCOVERY}
            if isinstance(self.discovery, RealmStageError):
                raise self.discovery
            return self.discovery
        if path == _IDP:
            if body["alias"] in self.providers:
                return Reply(409, {"errorMessage": "Identity Provider company already exists"})
            self.providers[body["alias"]] = {"internalId": str(uuid.uuid4()), **copy.deepcopy(body)}
            return Reply(201, None, f"{path}/{body['alias']}")
        if match := re.fullmatch(rf"{_IDP}/([^/]+)/mappers", path):
            assert body["identityProviderAlias"] == match.group(1)
            if any(m["name"] == body["name"] for m in self.provider_mappers.values()):
                return Reply(409, {"errorMessage": "mapper exists"})
            new_id = str(uuid.uuid4())
            self.provider_mappers[new_id] = {"id": new_id, **copy.deepcopy(body)}
            return Reply(201, None, f"{path}/{new_id}")
        if path == f"{_BASE}/authentication/executions/exec-idp/config":
            new_id = str(uuid.uuid4())
            self.auth_configs[new_id] = {"id": new_id, **copy.deepcopy(body)}
            self.redirector["authenticationConfig"] = new_id
            return Reply(201, None, f"{path}/{new_id}")
        return super()._post(path, body, params)

    def _put(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if match := re.fullmatch(rf"{_IDP}/([^/]+)/mappers/([^/]+)", path):
            assert body["id"] == match.group(2)
            self.provider_mappers[match.group(2)] = copy.deepcopy(body)
            return Reply(204, None)
        if match := re.fullmatch(rf"{_IDP}/([^/]+)", path):
            provider = self.providers[match.group(1)]
            assert body["internalId"] == provider["internalId"]
            new = copy.deepcopy(body)
            if new["config"].get("clientSecret") == _MASK:
                new["config"]["clientSecret"] = provider["config"]["clientSecret"]
            self.providers[match.group(1)] = new
            return Reply(204, None)
        if match := re.fullmatch(rf"{_BASE}/authentication/config/([^/]+)", path):
            self.auth_configs[match.group(1)] = copy.deepcopy(body)
            return Reply(204, None)
        return super()._put(path, body, params)

    def _delete(self, path: str, body: Any, params: dict[str, str]) -> Reply:
        if match := re.fullmatch(rf"{_IDP}/([^/]+)/mappers/([^/]+)", path):
            del self.provider_mappers[match.group(2)]
            return Reply(204, None)
        if match := re.fullmatch(rf"{_BASE}/authentication/config/([^/]+)", path):
            del self.auth_configs[match.group(1)]
            self.redirector.pop("authenticationConfig", None)
            return Reply(204, None)
        if match := re.fullmatch(rf"{_BASE}/users/([^/]+)/groups/([^/]+)", path):
            user = self._user_by_id(match.group(1))
            name = next(n for n, g in self.groups.items() if g["id"] == match.group(2))
            user["groups"] = [g for g in user["groups"] if g != name]
            return Reply(204, None)
        return super()._delete(path, body, params)

    def mappers(self) -> dict[str, dict[str, Any]]:
        return {m["name"]: m for m in self.provider_mappers.values()}

    def default_provider(self) -> str | None:
        config_id = self.redirector.get("authenticationConfig")
        if not config_id:
            return None
        value: str | None = self.auth_configs[config_id]["config"].get("defaultProvider")
        return value


def _company(**overrides: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "discovery_url": _DISCOVERY,
        "client_id": "meho-keycloak",
        "group_map": {"Ops": "MEHO-Operators", "Readers": "MEHO-Readers"},
        **overrides,
    }


def _fresh() -> CompanyKeycloak:
    kc = CompanyKeycloak(bootstrap=None)
    kc.import_realm(_realm_as_imported())
    return kc


async def _run(kc: CompanyKeycloak, *, store: FakeStore | None = None, **company: Any) -> None:
    plan = _plan(_REALM_FILE, company_login=_company(**company))
    await finish_realm(plan, _ENV_ON, kc, store or FakeStore())


async def test_off_by_default_touches_nothing_of_the_company_login() -> None:
    kc = _fresh()
    await finish_realm(_plan(_REALM_FILE), _ENV, kc, FakeStore())
    # The realm stage lists the login flows for the realm file's own flows.
    calls = [path for _, path in kc.calls if path != f"{_BASE}/authentication/flows"]
    assert not any(kc._company_path(path) for path in calls)
    assert CompanyLoginPlan().enabled is False


async def test_first_run_makes_the_provider_the_mappers_and_the_redirect(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)

    provider = kc.providers["company"]
    assert provider["providerId"] == "oidc"
    assert provider["displayName"] == "Company login"
    assert provider["enabled"] is True
    assert provider["trustEmail"] is False
    assert provider["firstBrokerLoginFlowAlias"] == "first broker login"
    config = provider["config"]
    assert {k: config[k] for k in _FOUND} == _FOUND
    assert config["clientId"] == "meho-keycloak"
    assert config["clientSecret"] == _SECRET
    assert config["clientAuthMethod"] == "client_secret_post"
    assert config["defaultScope"] == "openid profile email"
    assert (config["pkceEnabled"], config["pkceMethod"]) == ("true", "S256")
    assert (config["validateSignature"], config["useJwksUrl"]) == ("true", "true")
    assert config["syncMode"] == "FORCE"

    mappers = kc.mappers()
    assert set(mappers) == {f"{MAPPER_PREFIX}Ops", f"{MAPPER_PREFIX}Readers"}
    ops = mappers[f"{MAPPER_PREFIX}Ops"]
    assert ops["identityProviderMapper"] == "oidc-advanced-group-idp-mapper"
    assert ops["config"]["syncMode"] == "FORCE"
    assert ops["config"]["group"] == "/MEHO-Operators"
    assert ops["config"]["are.claim.values.regex"] == "false"
    assert json.loads(ops["config"]["claims"]) == [{"key": "groups", "value": "Ops"}]

    assert kc.default_provider() == "company"
    config_id = kc.redirector["authenticationConfig"]
    assert kc.auth_configs[config_id]["alias"] == REDIRECT_CONFIG_ALIAS

    output = capfd.readouterr().out
    assert "identity provider company: created" in output
    assert "auto-redirect: set to company" in output
    assert _SECRET not in output


async def test_second_run_changes_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    kc = _fresh()
    store = FakeStore()
    await _run(kc, store=store)
    before = kc.state()
    capfd.readouterr()

    await _run(kc, store=store)

    assert kc.state() == before
    output = capfd.readouterr().out
    for word in ("created", "updated", "removed", "set to"):
        assert word not in output, output
    assert "identity provider company: kept (secret set)" in output
    assert _SECRET not in output


async def test_a_new_secret_in_the_secret_reaches_keycloak() -> None:
    kc = _fresh()
    await _run(kc)
    env = {**_ENV_ON, SECRET_ENV: "rotated-secret-value-9876543210"}
    await finish_realm(_plan(_REALM_FILE, company_login=_company()), env, kc, FakeStore())
    assert kc.providers["company"]["config"]["clientSecret"] == "rotated-secret-value-9876543210"


async def test_a_changed_line_changes_its_mapper_and_a_removed_line_removes_it(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)
    # A mapper someone made by hand stays.
    kc.provider_mappers["hand"] = {
        "id": "hand",
        "name": "hand-made",
        "identityProviderAlias": "company",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {},
    }
    ops_id = kc.mappers()[f"{MAPPER_PREFIX}Ops"]["id"]
    capfd.readouterr()

    await _run(kc, group_map={"Ops": "MEHO-Admins"})

    mappers = kc.mappers()
    assert set(mappers) == {f"{MAPPER_PREFIX}Ops", "hand-made"}
    assert mappers[f"{MAPPER_PREFIX}Ops"]["id"] == ops_id
    assert mappers[f"{MAPPER_PREFIX}Ops"]["config"]["group"] == "/MEHO-Admins"
    output = capfd.readouterr().out
    assert f"mapper {MAPPER_PREFIX}Ops: updated" in output
    assert f"mapper {MAPPER_PREFIX}Readers: removed" in output


async def test_people_leave_the_groups_of_a_removed_or_changed_line(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)
    # Jane came in through the company login: Ops and Readers gave her two
    # groups, and someone made her an approver by hand.
    kc.person("jane", ["MEHO-Operators", "MEHO-Readers", "MEHO-Approvers"], linked=True)
    kc.person("bob", ["MEHO-Readers"], linked=True)
    # A local account is never touched.
    kc.person("local", ["MEHO-Operators", "MEHO-Readers"], linked=False)
    capfd.readouterr()

    # Ops now gives another group; Readers is no longer mapped.
    await _run(kc, group_map={"Ops": "MEHO-Admins"})

    assert kc.users["jane"]["groups"] == ["MEHO-Approvers"]
    assert kc.users["bob"]["groups"] == []
    assert kc.users["local"]["groups"] == ["MEHO-Operators", "MEHO-Readers"]
    output = capfd.readouterr().out
    assert "group /MEHO-Operators: 1 company login member(s) removed" in output
    assert "group /MEHO-Readers: 2 company login member(s) removed" in output


async def test_a_line_that_keeps_its_group_takes_nobody_out() -> None:
    kc = _fresh()
    await _run(kc)
    kc.person("jane", ["MEHO-Operators"], linked=True)
    kc.calls.clear()
    await _run(kc, groups_claim="roles")
    assert kc.users["jane"]["groups"] == ["MEHO-Operators"]
    assert not any(path.endswith("/members") for _, path in kc.calls)
    # The provider is written once, by the provider step; no cleanup note.
    assert kc.calls.count(("PUT", f"{_IDP}/company")) == 1
    assert CLEANUP_CONFIG_KEY not in kc.providers["company"]["config"]


def _leaves_a_group(method: str, path: str) -> bool:
    return method == "DELETE" and "/users/" in path and "/groups/" in path


async def test_a_cleanup_that_times_out_is_finished_at_the_next_run(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)
    kc.person("jane", ["MEHO-Operators"], linked=True)
    kc.person("bob", ["MEHO-Readers"], linked=True)
    kc.times_out = _leaves_a_group

    # Ops now gives another group; Readers is no longer mapped.
    with pytest.raises(RealmStageError, match="ReadTimeout"):
        await _run(kc, group_map={"Ops": "MEHO-Admins"})

    # Keycloak already has the new mappers, so they no longer say which
    # groups they granted; the note on the provider does.
    mappers = kc.mappers()
    assert mappers[f"{MAPPER_PREFIX}Ops"]["config"]["group"] == "/MEHO-Admins"
    assert f"{MAPPER_PREFIX}Readers" not in mappers
    assert kc.users["jane"]["groups"] == ["MEHO-Operators"]
    assert kc.users["bob"]["groups"] == ["MEHO-Readers"]
    note = kc.providers["company"]["config"][CLEANUP_CONFIG_KEY]
    assert json.loads(note) == ["/MEHO-Operators", "/MEHO-Readers"]
    kc.times_out = None
    capfd.readouterr()

    await _run(kc, group_map={"Ops": "MEHO-Admins"})

    assert kc.users["jane"]["groups"] == []
    assert kc.users["bob"]["groups"] == []
    assert CLEANUP_CONFIG_KEY not in kc.providers["company"]["config"]
    assert kc.providers["company"]["config"]["clientSecret"] == _SECRET
    output = capfd.readouterr().out
    assert "identity provider company: finishing the group cleanup of an earlier run" in output
    assert f"mapper {MAPPER_PREFIX}Ops: kept" in output
    assert "group /MEHO-Operators: 1 company login member(s) removed" in output
    assert "group /MEHO-Readers: 1 company login member(s) removed" in output
    assert _SECRET not in output


async def test_a_run_that_stops_at_a_mapper_change_leaves_the_note_for_the_next_run() -> None:
    kc = _fresh()
    await _run(kc)
    kc.person("jane", ["MEHO-Operators", "MEHO-Readers"], linked=True)
    kc.times_out = lambda method, path: method in ("PUT", "DELETE") and "/mappers/" in path

    with pytest.raises(RealmStageError, match="ReadTimeout"):
        await _run(kc, group_map={"Ops": "MEHO-Admins"})

    assert kc.mappers()[f"{MAPPER_PREFIX}Ops"]["config"]["group"] == "/MEHO-Operators"
    assert json.loads(kc.providers["company"]["config"][CLEANUP_CONFIG_KEY]) == [
        "/MEHO-Operators",
        "/MEHO-Readers",
    ]
    assert kc.users["jane"]["groups"] == ["MEHO-Operators", "MEHO-Readers"]
    kc.times_out = None

    await _run(kc, group_map={"Ops": "MEHO-Admins"})

    assert kc.mappers()[f"{MAPPER_PREFIX}Ops"]["config"]["group"] == "/MEHO-Admins"
    assert f"{MAPPER_PREFIX}Readers" not in kc.mappers()
    assert kc.users["jane"]["groups"] == []
    assert CLEANUP_CONFIG_KEY not in kc.providers["company"]["config"]


async def test_an_unreadable_note_stops_the_stage() -> None:
    kc = _fresh()
    await _run(kc)
    kc.providers["company"]["config"][CLEANUP_CONFIG_KEY] = "not json"
    with pytest.raises(RealmStageError, match=f"unreadable {CLEANUP_CONFIG_KEY}"):
        await _run(kc)


async def test_a_big_group_is_read_page_by_page() -> None:
    kc = _fresh()
    await _run(kc)
    for n in range(250):
        kc.person(f"p{n}", ["MEHO-Readers"], linked=True)
    await _run(kc, group_map={"Ops": "MEHO-Operators"})
    assert all(kc.users[f"p{n}"]["groups"] == [] for n in range(250))
    assert sum(path.endswith("/members") for _, path in kc.calls) == 3


async def test_a_changed_groups_claim_changes_every_mapper() -> None:
    kc = _fresh()
    await _run(kc)
    await _run(kc, groups_claim="roles")
    for mapper in kc.mappers().values():
        assert json.loads(mapper["config"]["claims"])[0]["key"] == "roles"


async def test_auto_redirect_off_removes_the_default_and_on_sets_it_again(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)
    await _run(kc, auto_redirect=False)
    assert kc.default_provider() is None
    assert kc.auth_configs == {}
    assert "auto-redirect: removed" in capfd.readouterr().out

    await _run(kc, auto_redirect=False)
    assert "auto-redirect: off" in capfd.readouterr().out

    await _run(kc)
    assert kc.default_provider() == "company"


async def test_a_default_set_to_another_provider_is_changed_on_and_kept_off() -> None:
    kc = _fresh()
    kc.auth_configs["other"] = {
        "id": "other",
        "alias": "by-hand",
        "config": {"defaultProvider": "x"},
    }
    kc.redirector["authenticationConfig"] = "other"

    await _run(kc, auto_redirect=False)
    assert kc.default_provider() == "x"

    await _run(kc)
    assert kc.default_provider() == "company"
    assert kc.auth_configs["other"]["alias"] == "by-hand"


async def test_a_changed_setting_updates_the_provider(capfd: pytest.CaptureFixture[str]) -> None:
    kc = _fresh()
    await _run(kc)
    kc.providers["company"]["trustEmail"] = True
    kc.providers["company"]["config"]["syncMode"] = "IMPORT"
    capfd.readouterr()

    await _run(kc, display_name="Acme login")

    provider = kc.providers["company"]
    assert provider["trustEmail"] is False
    assert provider["config"]["syncMode"] == "FORCE"
    assert provider["displayName"] == "Acme login"
    assert "updated (displayName, syncMode, trustEmail; secret set)" in capfd.readouterr().out


async def test_unreachable_discovery_keeps_the_saved_addresses(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kc = _fresh()
    await _run(kc)
    kc.discovery = Reply(500, {"error": "unknown_error"})

    await _run(kc, auto_redirect=False)

    assert kc.providers["company"]["config"]["tokenUrl"] == _FOUND["tokenUrl"]
    assert kc.default_provider() is None
    assert "discovery document not readable, kept the saved addresses" in capfd.readouterr().out


async def test_unreachable_discovery_on_the_first_run_stops_the_stage() -> None:
    kc = _fresh()
    kc.discovery = Reply(500, {"error": "unknown_error"})
    with pytest.raises(RealmStageError, match="cannot read the discovery document"):
        await _run(kc)
    assert kc.providers == {}


async def test_a_timed_out_discovery_keeps_the_saved_addresses(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Keycloak waits for the company login longer than the Job waits for
    # Keycloak: the request raises instead of answering.
    kc = _fresh()
    await _run(kc)
    kc.discovery = RealmStageError("Keycloak request failed: ReadTimeout")

    await _run(kc, auto_redirect=False)

    assert kc.providers["company"]["config"]["tokenUrl"] == _FOUND["tokenUrl"]
    assert kc.default_provider() is None
    output = capfd.readouterr().out
    assert "discovery document not readable, kept the saved addresses" in output
    assert "auto-redirect: removed" in output


async def test_a_timed_out_discovery_on_the_first_run_stops_the_stage() -> None:
    kc = _fresh()
    kc.discovery = RealmStageError("Keycloak request failed: ReadTimeout")
    with pytest.raises(RealmStageError, match=r"cannot read the discovery document.*ReadTimeout"):
        await _run(kc)
    assert kc.providers == {}


async def test_a_company_login_without_signing_keys_is_refused() -> None:
    kc = _fresh()
    kc.discovery = Reply(
        200,
        {
            k: v
            for k, v in _FOUND.items()
            if k not in ("jwksUrl", "useJwksUrl", "validateSignature")
        },
    )
    with pytest.raises(RealmStageError, match="publishes no signing keys"):
        await _run(kc)
    assert kc.providers == {}


async def test_an_unknown_meho_group_stops_the_stage_before_any_change() -> None:
    kc = _fresh()
    with pytest.raises(RealmStageError, match="MEHO-Nobody"):
        await _run(kc, group_map={"Ops": "MEHO-Nobody"})
    assert kc.providers == {}
    assert kc.provider_mappers == {}


async def test_missing_rights_name_the_roles_to_add() -> None:
    kc = _fresh()
    kc.forbidden = True
    with pytest.raises(RealmStageError, match="manage-identity-providers and manage-realm"):
        await _run(kc)


async def test_the_secret_env_is_required_when_on() -> None:
    kc = _fresh()
    with pytest.raises(RealmStageError, match=SECRET_ENV):
        await finish_realm(_plan(_REALM_FILE, company_login=_company()), _ENV, kc, FakeStore())


@pytest.mark.parametrize("missing", ["discovery_url", "client_id"])
async def test_discovery_url_and_client_id_are_required(missing: str) -> None:
    kc = _fresh()
    with pytest.raises(RealmStageError, match="discoveryUrl and clientId"):
        blank: dict[str, Any] = {missing: ""}
        await _run(kc, **blank)


def test_plan_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="nope"):
        CompanyLoginPlan.model_validate({"nope": 1})
