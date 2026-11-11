# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The touch flow op ``keycloak.authflow.ensure_approval_touch`` (#6213).

The op runs through the real dispatch stack against a small fake Keycloak
(respx) that keeps login flows as a tree, step settings, the realm's
attributes and one client. No running Keycloak and no Vault.

What is proven:

* the op's flow is the one of the chart's realm file: same steps, same
  requirements, same settings;
* a realm without the flow gets it, ``meho-touch`` at level 2 next to the
  realm's other names, the ``amr`` mapper and the binding; a second run
  writes nothing;
* a flow with another shape or other settings stops the op and nothing is
  written; a missing client stops it before anything is written; a realm
  that maps ``meho-touch`` to another level stops it before the bind;
* the binding keeps the client's other settings and is read back;
* the op is ``caution`` with approval, a plain ``write`` on the feed, and
  waits for approval; its approval card names the client and the flow.
"""

from __future__ import annotations

import copy
import itertools
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_authflow_touch import wanted_steps
from meho_backplane.connectors.keycloak.ops_authflow_touch_schemas import (
    AMR_MAPPER,
    TOUCH_FLOW_ALIAS,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_OP = "keycloak.authflow.ensure_approval_touch"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-touch-test"
_KC_HOST = "keycloak-touch.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "example-realm"
_ADMIN_REALM = "master"
_ADMIN = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000006213")
_CLIENT_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
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


def _operator() -> Operator:
    return Operator(
        sub="kc-touch-requester",
        name="Keycloak Touch Test Operator",
        email=None,
        raw_jwt="<kc-touch-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.8.1"},
                notes="seeded by test_connectors_keycloak_authflow_touch",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# A small fake Keycloak: flows as a tree, step settings, the realm, one client
# ---------------------------------------------------------------------------


class FakeKeycloak:
    """Keeps flows, step settings, the realm and the client between requests."""

    def __init__(self, *, client: dict[str, Any] | None, loa_map: str | None = None) -> None:
        self.client = client
        self.mappers: list[dict[str, Any]] = [{"name": "tenant-id", "protocolMapper": "x"}]
        attributes = {"frontendUrl": ""}
        if loa_map is not None:
            attributes["acr.loa.map"] = loa_map
        self.realm: dict[str, Any] = {"realm": _REALM, "attributes": attributes}
        # alias -> {"id", "top", "steps": [step]}; a step is a dict with
        # "id", "provider" or "sub", "requirement", "config".
        self.flows: dict[str, dict[str, Any]] = {
            "browser": {"id": "browser-id", "top": True, "steps": []}
        }
        self.configs: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self._ids = itertools.count(1)

    def mount(self, mock: respx.MockRouter) -> None:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"{_ADMIN}/clients").mock(side_effect=self._clients)
        mock.route(path=f"{_ADMIN}/clients/{_CLIENT_UUID}/protocol-mappers/models").mock(
            side_effect=self._mappers
        )
        mock.route(path=f"{_ADMIN}/clients/{_CLIENT_UUID}").mock(side_effect=self._client)
        mock.route(path=_ADMIN).mock(side_effect=self._realm)
        mock.route(path=f"{_ADMIN}/authentication/flows").mock(side_effect=self._flows)
        mock.route(path__regex=rf"^{_ADMIN}/authentication/flows/[^/]+/executions.*$").mock(
            side_effect=self._executions
        )
        mock.route(path__regex=rf"^{_ADMIN}/authentication/executions/[^/]+/config$").mock(
            side_effect=self._add_config
        )
        mock.route(path__regex=rf"^{_ADMIN}/authentication/config/[^/]+$").mock(
            side_effect=self._config
        )

    def _new_id(self) -> str:
        return f"id-{next(self._ids)}"

    def _log(self, request: httpx.Request) -> Any:
        body = json.loads(request.content) if request.content else None
        self.writes.append((request.method, request.url.raw_path.decode(), body))
        return body

    def _clients(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[self.client] if self.client else [])

    def _client(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.client = self._log(request)
            return httpx.Response(204)
        return httpx.Response(200, json=self.client)

    def _mappers(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.mappers.append({"id": self._new_id(), **self._log(request)})
            return httpx.Response(201)
        return httpx.Response(200, json=self.mappers)

    def _realm(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.realm = self._log(request)
            return httpx.Response(204)
        return httpx.Response(200, json=self.realm)

    def _flows(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = self._log(request)
            self.flows[body["alias"]] = {"id": self._new_id(), "top": True, "steps": []}
            return httpx.Response(201)
        rows = [
            {"id": flow["id"], "alias": alias, "topLevel": True}
            for alias, flow in self.flows.items()
            if flow["top"]
        ]
        return httpx.Response(200, json=rows)

    def listed(self, alias: str, level: int = 0) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for step in self.flows[alias]["steps"]:
            row: dict[str, Any] = {
                "id": step["id"],
                "level": level,
                "requirement": step["requirement"],
                "authenticationFlow": "sub" in step,
            }
            if "sub" in step:
                row["displayName"] = step["sub"]
            else:
                row["providerId"] = step["provider"]
                row["displayName"] = step["provider"].title()
            if step.get("config"):
                row["authenticationConfig"] = step["config"]
            rows.append(row)
            if "sub" in step:
                rows.extend(self.listed(step["sub"], level + 1))
        return rows

    def _step(self, step_id: str) -> dict[str, Any]:
        return next(s for f in self.flows.values() for s in f["steps"] if s["id"] == step_id)

    def _executions(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?")[0]
        alias = unquote(raw.split("/flows/")[1].split("/")[0])
        if request.method == "GET":
            return httpx.Response(200, json=self.listed(alias))
        body = self._log(request)
        if request.method == "PUT":
            self._step(body["id"])["requirement"] = body["requirement"]
            return httpx.Response(204)
        step: dict[str, Any] = {"id": self._new_id(), "requirement": "DISABLED"}
        if raw.endswith("/executions/flow"):
            step["sub"] = body["alias"]
            self.flows[body["alias"]] = {"id": self._new_id(), "top": False, "steps": []}
        else:
            step["provider"] = body["provider"]
        self.flows[alias]["steps"].append(step)
        return httpx.Response(201)

    def _add_config(self, request: httpx.Request) -> httpx.Response:
        step_id = request.url.raw_path.decode().split("/executions/")[1].split("/")[0]
        body = self._log(request)
        config_id = self._new_id()
        self.configs[config_id] = {"id": config_id, **body}
        self._step(step_id)["config"] = config_id
        return httpx.Response(201)

    def _config(self, request: httpx.Request) -> httpx.Response:
        config_id = request.url.raw_path.decode().rsplit("/", 1)[1]
        return httpx.Response(200, json=self.configs[config_id])


def _client(browser: str | None = None) -> dict[str, Any]:
    overrides: dict[str, str] = {"direct_grant": "dg-flow-id"}
    if browser:
        overrides["browser"] = browser
    return {
        "id": _CLIENT_UUID,
        "clientId": "meho-web",
        "publicClient": False,
        "redirectUris": ["https://meho.example.test/ui/auth/callback"],
        "attributes": {"pkce.code.challenge.method": "S256"},
        "authenticationFlowBindingOverrides": overrides,
    }


async def _made(fake: FakeKeycloak) -> None:
    """Let the op make the flow on *fake*, then forget the writes."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        assert (await _run({}))["status"] == "ok"
    fake.writes.clear()


# ---------------------------------------------------------------------------
# The op's flow is the realm file's flow
# ---------------------------------------------------------------------------


def _file_steps(flows: dict[str, Any], alias: str, level: int = 0) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for step in sorted(flows[alias]["authenticationExecutions"], key=lambda s: s["priority"]):
        name = step.get("authenticator") or step["flowAlias"]
        rows.append((level, name, step["requirement"], step.get("authenticatorConfig")))
        if step["authenticatorFlow"]:
            rows.extend(_file_steps(flows, step["flowAlias"], level + 1))
    return rows


def test_op_flow_is_the_flow_of_the_realm_file() -> None:
    realm = json.loads(_REALM_FILE.read_text())
    flows = {flow["alias"]: flow for flow in realm["authenticationFlows"]}
    configs = {c["alias"]: c["config"] for c in realm["authenticatorConfig"]}
    assert flows[TOUCH_FLOW_ALIAS]["description"]
    from_file = _file_steps(flows, TOUCH_FLOW_ALIAS)
    from_op = [
        (level, node.name, node.requirement, node.config[0] if node.config else None)
        for level, node in wanted_steps()
    ]
    assert from_op == from_file
    for _, node in wanted_steps():
        if node.config is not None:
            assert configs[node.config[0]] == node.config[1]
    web = next(c for c in realm["clients"] if c["clientId"] == "meho-web")
    file_mapper = next(m for m in web["protocolMappers"] if m["name"] == "amr")
    assert file_mapper == AMR_MAPPER
    assert json.loads(realm["attributes"]["acr.loa.map"]) == {"meho-touch": 2}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_op_registers_as_caution_with_approval(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    assert classify_op(_OP) == "write"


async def test_unknown_param_is_invalid(kc: KeycloakConnector) -> None:
    result = await _run({"realm": "master"})
    assert result["extras"]["error_code"] == "invalid_params"


async def test_op_waits_for_approval_and_the_card_names_client_and_flow(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(client=_client())
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        parked = await dispatch(
            operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_OP, target=target, params={}
        )
    assert parked.status == "awaiting_approval", parked.error
    assert fake.writes == []
    async with get_sessionmaker()() as session:
        pending = await session.get(
            ApprovalRequest, uuid.UUID(parked.extras["approval_request_id"])
        )
        assert pending is not None
        card = pending.proposed_effect["preview"]
    assert card["realm"] == _REALM
    assert card["client_id"] == "meho-web"
    assert card["new_flow"] == TOUCH_FLOW_ALIAS
    assert "meho-touch" in card["affects"]
    assert "authenticationFlowBindingOverrides" in card["way_back"]


# ---------------------------------------------------------------------------
# Making the flow
# ---------------------------------------------------------------------------


async def test_realm_without_the_flow_gets_all_of_it_and_a_second_run_changes_nothing(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(client=_client(), loa_map='{"gold":3}')
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run({})
        writes_after_first = len(fake.writes)
        second = await _run({})

    assert first["status"] == "ok", first
    out = first["result"]
    flow_id = fake.flows[TOUCH_FLOW_ALIAS]["id"]
    assert out["flow_created"] is True
    assert out["loa_map_changed"] is True
    assert out["amr_mapper_added"] is True
    assert out["binding_changed"] is True
    assert out["flow_id"] == flow_id
    assert out["browser_flow_id"] == flow_id
    assert out["previous_browser_flow_id"] is None
    assert [(s["level"], s["step"], s["requirement"]) for s in out["flow_steps"]] == [
        (level, node.name, node.requirement) for level, node in wanted_steps()
    ]

    # Every step with settings got its own settings, under its alias.
    rows = fake.listed(TOUCH_FLOW_ALIAS)
    for row, (_, node) in zip(rows, wanted_steps(), strict=True):
        if node.config is None:
            assert "authenticationConfig" not in row
            continue
        config = fake.configs[row["authenticationConfig"]]
        assert (config["alias"], config["config"]) == node.config

    # The realm keeps its other names; the client keeps its other settings.
    assert json.loads(fake.realm["attributes"]["acr.loa.map"]) == {"gold": 3, "meho-touch": 2}
    assert fake.realm["attributes"]["frontendUrl"] == ""
    assert any(m["protocolMapper"] == "oidc-amr-mapper" for m in fake.mappers)
    assert fake.client is not None
    assert fake.client["authenticationFlowBindingOverrides"] == {
        "direct_grant": "dg-flow-id",
        "browser": flow_id,
    }
    assert fake.client["redirectUris"] == ["https://meho.example.test/ui/auth/callback"]

    assert second["status"] == "ok", second
    for flag in ("flow_created", "loa_map_changed", "amr_mapper_added", "binding_changed"):
        assert second["result"][flag] is False, flag
    assert len(fake.writes) == writes_after_first


async def test_the_bind_is_the_last_write(kc: KeycloakConnector) -> None:
    """The console uses the flow only when the flow and its settings are complete."""
    fake = FakeKeycloak(client=_client(browser="old-flow-id"))
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    assert result["result"]["previous_browser_flow_id"] == "old-flow-id"
    assert fake.writes[-1][1] == f"{_ADMIN}/clients/{_CLIENT_UUID}"


async def test_missing_client_stops_before_any_write(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=None)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({"client_id": "meho-web"})
    assert result["status"] != "ok"
    assert "client_not_found" in json.dumps(result)
    assert fake.writes == []


# ---------------------------------------------------------------------------
# A flow or a realm with another shape
# ---------------------------------------------------------------------------


async def test_flow_with_a_step_turned_off_stops_without_a_write(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client())
    await _made(fake)
    level_2 = fake.flows[f"{TOUCH_FLOW_ALIAS} - level 2"]["steps"]
    level_2[1]["requirement"] = "DISABLED"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "other_steps" in text
    assert "webauthn-authenticator-passwordless" in text
    assert fake.writes == []


async def test_flow_with_other_settings_stops_without_a_write(kc: KeycloakConnector) -> None:
    """A touch level kept for a while (maximum age not 0) is not this flow."""
    fake = FakeKeycloak(client=_client())
    await _made(fake)
    condition = fake.flows[f"{TOUCH_FLOW_ALIAS} - level 2"]["steps"][0]
    fake.configs[condition["config"]]["config"]["loa-max-age"] = "300"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "other settings" in text
    assert "conditional-level-of-authentication" in text
    assert fake.writes == []


async def test_flow_with_an_extra_step_stops_without_a_write(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client())
    await _made(fake)
    fake.flows[TOUCH_FLOW_ALIAS]["steps"].append(
        {"id": "extra", "provider": "auth-spnego", "requirement": "ALTERNATIVE"}
    )
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    assert "other_steps" in json.dumps(result)
    assert fake.writes == []


async def test_realm_with_another_touch_level_is_not_bound(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client(), loa_map='{"meho-touch":3}')
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    assert "loa_conflict" in json.dumps(result)
    assert fake.realm["attributes"]["acr.loa.map"] == '{"meho-touch":3}'
    assert fake.client == _client()


async def test_binding_that_does_not_read_back_is_an_error(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client())
    await _made(fake)
    fake.client = _client()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        # Keycloak accepts the PUT but keeps the old client. (respx uses the
        # first matching route, so these go before the fake's routes.)
        mock.put(f"{_ADMIN}/clients/{_CLIENT_UUID}").respond(204)
        mock.get(f"{_ADMIN}/clients/{_CLIENT_UUID}").respond(200, json=copy.deepcopy(_client()))
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    assert "check_failed" in json.dumps(result)
