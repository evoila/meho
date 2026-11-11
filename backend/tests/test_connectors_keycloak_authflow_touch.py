# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The touch flow op ``keycloak.authflow.ensure_approval_touch`` (#6213).

The op runs through the real dispatch stack against a small fake Keycloak
(respx) that keeps login flows as a tree, step settings, the realm's
attributes and one client. No running Keycloak and no Vault.

What is proven:

* the op's flow is the one of the chart's realm file: same steps, same
  requirements, same settings;
* a realm without the flow gets it, ``meho-touch`` at level 3 next to the
  realm's other names, the ``amr`` mapper and the binding; a second run
  writes nothing;
* a flow with another shape or other settings stops the op and nothing is
  written; a missing client, or a Keycloak without a step of the flow (older
  than 26.4), stops it before anything is written; a realm
  that maps ``meho-touch`` to another level stops it before the bind;
* a realm whose passkey policy does not ask for user verification stops the
  op before anything is written;
* an ``amr`` mapper that leaves the claim out of the ID token gets its token
  settings turned on before the bind;
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
    REDIRECT_CONFIG_ALIAS,
    TOUCH_FLOW_ALIAS,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.core import company_login
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
_POLICY_UV = "webAuthnPolicyPasswordlessUserVerificationRequirement"
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

    def __init__(
        self,
        *,
        client: dict[str, Any] | None,
        loa_map: str | None = None,
        verification: str | None = "required",
    ) -> None:
        self.client = client
        self.mappers: list[dict[str, Any]] = [{"name": "tenant-id", "protocolMapper": "x"}]
        attributes = {"frontendUrl": ""}
        if loa_map is not None:
            attributes["acr.loa.map"] = loa_map
        self.realm: dict[str, Any] = {"realm": _REALM, "attributes": attributes}
        if verification is not None:
            self.realm[_POLICY_UV] = verification
        # alias -> {"id", "top", "steps": [step]}; a step is a dict with
        # "id", "provider" or "sub", "requirement", "config".
        self.flows: dict[str, dict[str, Any]] = {
            "browser": {"id": "browser-id", "top": True, "steps": []}
        }
        self.configs: dict[str, dict[str, Any]] = {}
        self.providers = {node.name for _, node in wanted_steps() if node.children is None}
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
        mock.route(
            path__regex=rf"^{_ADMIN}/clients/{_CLIENT_UUID}/protocol-mappers/models/[^/]+$"
        ).mock(side_effect=self._mapper)
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
        mock.get(f"{_ADMIN}/authentication/authenticator-providers").mock(
            side_effect=lambda _: httpx.Response(
                200, json=[{"id": p, "displayName": p} for p in sorted(self.providers)]
            )
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

    def _mapper(self, request: httpx.Request) -> httpx.Response:
        mapper_id = request.url.raw_path.decode().rsplit("/", 1)[1]
        index = next(i for i, m in enumerate(self.mappers) if m.get("id") == mapper_id)
        if request.method == "PUT":
            self.mappers[index] = self._log(request)
            return httpx.Response(204)
        return httpx.Response(200, json=self.mappers[index])

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
        if request.method == "PUT":
            self.configs[config_id] = self._log(request)
            return httpx.Response(204)
        if request.method == "DELETE":
            self._log(request)
            del self.configs[config_id]
            for flow in self.flows.values():
                for step in flow["steps"]:
                    if step.get("config") == config_id:
                        del step["config"]
            return httpx.Response(204)
        return httpx.Response(200, json=self.configs[config_id])

    def redirect_to(self, provider: str) -> None:
        """Give the realm's browser flow a redirect step that goes to *provider*."""
        self.configs["browser-redirect"] = {
            "id": "browser-redirect",
            "alias": "meho-company-login-redirect",
            "config": {"defaultProvider": provider},
        }
        self.flows["browser"]["steps"] = [
            {"id": "cookie", "provider": "auth-cookie", "requirement": "ALTERNATIVE"},
            {
                "id": "redirector",
                "provider": "identity-provider-redirector",
                "requirement": "ALTERNATIVE",
                "config": "browser-redirect",
            },
        ]

    def touch_redirect(self) -> dict[str, Any] | None:
        """The settings of the touch flow's redirect step, or ``None``."""
        step = next(
            row
            for row in self.listed(TOUCH_FLOW_ALIAS)
            if row.get("providerId") == "identity-provider-redirector"
        )
        config_id = step.get("authenticationConfig")
        return self.configs[config_id] if config_id else None


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
    assert json.loads(realm["attributes"]["acr.loa.map"]) == {"meho-touch": 3}


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
    fake = FakeKeycloak(client=_client(), loa_map='{"meho-approve":2}')
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
    assert out["redirect_default_copied"] is False
    assert fake.touch_redirect() is None
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
    levels = json.loads(fake.realm["attributes"]["acr.loa.map"])
    assert levels == {"meho-approve": 2, "meho-touch": 3}
    assert fake.realm["attributes"]["frontendUrl"] == ""
    assert any(m["protocolMapper"] == "oidc-amr-mapper" for m in fake.mappers)
    assert fake.client is not None
    assert fake.client["authenticationFlowBindingOverrides"] == {
        "direct_grant": "dg-flow-id",
        "browser": flow_id,
    }
    assert fake.client["redirectUris"] == ["https://meho.example.test/ui/auth/callback"]

    assert second["status"] == "ok", second
    flags = (
        "flow_created",
        "loa_map_changed",
        "amr_mapper_added",
        "amr_mapper_fixed",
        "redirect_default_copied",
        "binding_changed",
    )
    for flag in flags:
        assert second["result"][flag] is False, flag
    assert len(fake.writes) == writes_after_first


async def test_the_bind_is_the_last_write(kc: KeycloakConnector) -> None:
    """The console uses the flow only when the flow and its settings are complete."""
    fake = FakeKeycloak(client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    assert result["result"]["bound"] is True
    assert result["result"]["not_bound_reason"] is None
    assert fake.writes[-1][1] == f"{_ADMIN}/clients/{_CLIENT_UUID}"


# ---------------------------------------------------------------------------
# A realm or a client with its own login flow is not bound
# ---------------------------------------------------------------------------


async def test_realm_with_its_own_login_flow_is_not_bound(kc: KeycloakConnector) -> None:
    """Level 1 copies the built-in login; binding would drop the realm's extra steps."""
    fake = FakeKeycloak(client=_client())
    fake.flows["browser with extra checks"] = {"id": "custom-id", "top": True, "steps": []}
    fake.realm["browserFlow"] = "browser with extra checks"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["flow_created"] is True
    assert out["bound"] is False
    assert "browser with extra checks" in out["not_bound_reason"]
    assert out["binding_changed"] is False
    assert out["browser_flow_id"] is None
    assert fake.client is not None
    assert "browser" not in fake.client["authenticationFlowBindingOverrides"]
    assert ("PUT", f"{_ADMIN}/clients/{_CLIENT_UUID}") not in [(m, p) for m, p, _ in fake.writes]


async def test_client_with_its_own_login_flow_is_not_bound(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client(browser="old-flow-id"))
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["bound"] is False
    assert "old-flow-id" in out["not_bound_reason"]
    assert out["browser_flow_id"] == "old-flow-id"
    assert out["previous_browser_flow_id"] == "old-flow-id"
    assert fake.client is not None
    assert fake.client["authenticationFlowBindingOverrides"]["browser"] == "old-flow-id"


async def test_replace_custom_login_binds_over_the_client_flow(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(client=_client(browser="old-flow-id"))
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({"replace_custom_login": True})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["bound"] is True
    assert out["previous_browser_flow_id"] == "old-flow-id"
    assert out["browser_flow_id"] == fake.flows[TOUCH_FLOW_ALIAS]["id"]
    assert fake.writes[-1][1] == f"{_ADMIN}/clients/{_CLIENT_UUID}"


def _login_steps(fake: FakeKeycloak, alias: str, prefix: str = "") -> None:
    """Give *fake* the built-in login as flow *alias*; a copy's sub-flows carry *prefix*."""
    forms, code = f"{prefix}forms", f"{prefix}Browser - Conditional OTP"
    fake.flows[alias] = {
        "id": f"{alias}-id",
        "top": True,
        "steps": [
            {"id": f"{alias}-cookie", "provider": "auth-cookie", "requirement": "ALTERNATIVE"},
            {"id": f"{alias}-forms", "sub": forms, "requirement": "ALTERNATIVE"},
        ],
    }
    fake.flows[forms] = {
        "id": f"{forms}-id",
        "top": False,
        "steps": [
            {
                "id": f"{alias}-pw",
                "provider": "auth-username-password-form",
                "requirement": "REQUIRED",
            },
            {"id": f"{alias}-code", "sub": code, "requirement": "CONDITIONAL"},
        ],
    }
    fake.flows[code] = {
        "id": f"{code}-id",
        "top": False,
        "steps": [
            {
                "id": f"{alias}-user",
                "provider": "conditional-user-configured",
                "requirement": "REQUIRED",
            },
            {"id": f"{alias}-otp", "provider": "auth-otp-form", "requirement": "REQUIRED"},
        ],
    }


def _passkey_copy(fake: FakeKeycloak) -> None:
    """Give *fake* the built-in login and the copy the passkey op makes, and bind the copy."""
    _login_steps(fake, "browser")
    prefix = "browser with passkeys "
    _login_steps(fake, "browser with passkeys", prefix)
    fake.configs["skip-code"] = {
        "id": "skip-code",
        "alias": "browser with passkeys - skip code after passkey",
        "config": {"credentials": "webauthn-passwordless", "included": "false"},
    }
    fake.flows[f"{prefix}Browser - Conditional OTP"]["steps"].insert(
        1,
        {
            "id": "condition",
            "provider": "conditional-credential",
            "requirement": "REQUIRED",
            "config": "skip-code",
        },
    )
    fake.realm["browserFlow"] = "browser with passkeys"


async def test_realm_with_the_passkey_copy_of_the_login_is_bound(kc: KeycloakConnector) -> None:
    """MEHO's own 'browser with passkeys' copy keeps the built-in steps."""
    fake = FakeKeycloak(client=_client())
    _passkey_copy(fake)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    assert result["result"]["bound"] is True


@pytest.mark.parametrize(
    "change",
    [
        "extra_required_step",
        "code_step_turned_off",
        "condition_settings",
        "no_condition",
    ],
)
async def test_edited_passkey_copy_of_the_login_is_not_bound(
    kc: KeycloakConnector, change: str
) -> None:
    """The flow can be edited: an added or changed step there keeps the console unbound."""
    fake = FakeKeycloak(client=_client())
    _passkey_copy(fake)
    forms = fake.flows["browser with passkeys forms"]["steps"]
    code = fake.flows["browser with passkeys Browser - Conditional OTP"]["steps"]
    if change == "extra_required_step":
        forms.append({"id": "extra", "provider": "auth-otp-form", "requirement": "REQUIRED"})
    elif change == "code_step_turned_off":
        code[-1]["requirement"] = "DISABLED"
    elif change == "condition_settings":
        fake.configs["skip-code"]["config"]["included"] = "true"
    else:
        del code[1]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["bound"] is False
    assert "browser with passkeys" in out["not_bound_reason"]
    assert fake.client is not None
    assert "browser" not in fake.client["authenticationFlowBindingOverrides"]
    assert ("PUT", f"{_ADMIN}/clients/{_CLIENT_UUID}") not in [(m, p) for m, p, _ in fake.writes]


async def test_bound_client_stays_bound_on_a_run_without_replace_custom_login(
    kc: KeycloakConnector,
) -> None:
    """A run without the option reports the binding a run with it made."""
    fake = FakeKeycloak(client=_client())
    fake.flows["browser with extra checks"] = {"id": "custom-id", "top": True, "steps": []}
    fake.realm["browserFlow"] = "browser with extra checks"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run({"replace_custom_login": True})
        fake.writes.clear()
        second = await _run({})
    assert first["result"]["bound"] is True
    assert second["status"] == "ok", second
    out = second["result"]
    flow_id = fake.flows[TOUCH_FLOW_ALIAS]["id"]
    assert out["bound"] is True
    assert out["not_bound_reason"] is None
    assert out["binding_changed"] is False
    assert out["browser_flow_id"] == flow_id
    assert out["previous_browser_flow_id"] == flow_id
    assert fake.writes == []


# ---------------------------------------------------------------------------
# The company login redirect
# ---------------------------------------------------------------------------


def test_redirect_settings_name_is_the_one_of_the_setup_step() -> None:
    """The chart's setup step and the op give the touch flow's redirect the same name."""
    assert f"{company_login.REDIRECT_CONFIG_ALIAS}-touch" == REDIRECT_CONFIG_ALIAS


async def test_company_login_redirect_is_copied_before_the_bind(kc: KeycloakConnector) -> None:
    """With the redirect on, the console still goes straight to the company login."""
    fake = FakeKeycloak(client=_client())
    fake.redirect_to("company")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run({})
        writes_after_first = len(fake.writes)
        second = await _run({})
    assert first["status"] == "ok", first
    assert first["result"]["redirect_default_copied"] is True
    copied = fake.touch_redirect()
    assert copied is not None
    assert (copied["alias"], copied["config"]) == (
        REDIRECT_CONFIG_ALIAS,
        {"defaultProvider": "company"},
    )
    # The bind stays the last write: the console uses the flow only with its redirect.
    assert fake.writes[writes_after_first - 1][1] == f"{_ADMIN}/clients/{_CLIENT_UUID}"
    # The realm's own browser flow is not changed.
    assert fake.configs["browser-redirect"]["config"] == {"defaultProvider": "company"}

    assert second["status"] == "ok", second
    assert second["result"]["redirect_default_copied"] is False
    assert len(fake.writes) == writes_after_first


async def test_other_redirect_default_on_the_touch_flow_is_updated(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(client=_client())
    await _made(fake)
    fake.redirect_to("company")
    step = next(
        s
        for s in fake.flows[f"{TOUCH_FLOW_ALIAS} - sign-in"]["steps"]
        if s.get("provider") == "identity-provider-redirector"
    )
    fake.configs["old"] = {
        "id": "old",
        "alias": REDIRECT_CONFIG_ALIAS,
        "config": {"defaultProvider": "old-company"},
    }
    step["config"] = "old"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] == "ok", result
    assert result["result"]["redirect_default_copied"] is True
    assert fake.configs["old"]["config"] == {"defaultProvider": "company"}
    assert ("PUT", f"{_ADMIN}/authentication/config/old") in [(m, p) for m, p, _ in fake.writes]


async def test_removed_company_login_default_is_removed_from_the_touch_flow(
    kc: KeycloakConnector,
) -> None:
    """Redirect on, run, redirect off, run again: the console stops going to it too."""
    fake = FakeKeycloak(client=_client())
    fake.redirect_to("company")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        assert (await _run({}))["status"] == "ok"
        assert fake.touch_redirect() is not None
        redirector = fake.flows["browser"]["steps"][1]
        del redirector["config"]
        del fake.configs["browser-redirect"]
        fake.writes.clear()
        second = await _run({})
        third = await _run({})
    assert second["status"] == "ok", second
    assert second["result"]["redirect_default_copied"] is True
    assert fake.touch_redirect() is None
    assert [m for m, p, _ in fake.writes if "/authentication/config/" in p] == ["DELETE"]
    assert third["result"]["redirect_default_copied"] is False


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
    level_3 = fake.flows[f"{TOUCH_FLOW_ALIAS} - level 3"]["steps"]
    level_3[1]["requirement"] = "DISABLED"
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
    condition = fake.flows[f"{TOUCH_FLOW_ALIAS} - level 3"]["steps"][0]
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
    fake = FakeKeycloak(client=_client(), loa_map='{"meho-touch":2}')
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    assert "loa_conflict" in json.dumps(result)
    assert fake.realm["attributes"]["acr.loa.map"] == '{"meho-touch":2}'
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


async def test_keycloak_without_a_step_of_the_flow_is_not_changed(kc: KeycloakConnector) -> None:
    """Keycloak before 26.4 has no ``conditional-credential``: stop before any write."""
    fake = FakeKeycloak(client=_client())
    fake.providers.discard("conditional-credential")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "missing_step" in text
    assert "conditional-credential" in text
    assert fake.writes == []


# ---------------------------------------------------------------------------
# The passkey policy and the ``amr`` mapper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verification", ["preferred", "discouraged", "not specified", None])
async def test_passkey_policy_without_user_verification_stops_before_any_write(
    kc: KeycloakConnector, verification: str | None
) -> None:
    """Without ``required`` a touch could pass without a PIN or fingerprint."""
    fake = FakeKeycloak(client=_client(), verification=verification)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "verification_not_required" in text
    assert _POLICY_UV in text
    assert fake.writes == []
    assert TOUCH_FLOW_ALIAS not in fake.flows


def _amr_mapper_without_id_token() -> dict[str, Any]:
    return {
        "id": "amr-old",
        "name": "amr",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-amr-mapper",
        "config": {
            "id.token.claim": "false",
            "access.token.claim": "true",
            "userinfo.token.claim": "true",
        },
    }


async def test_amr_mapper_without_the_id_token_claim_is_fixed_before_the_bind(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(client=_client())
    fake.mappers.append(_amr_mapper_without_id_token())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run({})
        writes_after_first = len(fake.writes)
        second = await _run({})
    assert first["status"] == "ok", first
    assert first["result"]["amr_mapper_added"] is False
    assert first["result"]["amr_mapper_fixed"] is True
    amr = [m for m in fake.mappers if m.get("protocolMapper") == "oidc-amr-mapper"]
    assert len(amr) == 1
    assert amr[0]["id"] == "amr-old"
    assert amr[0]["config"] == {
        "id.token.claim": "true",
        "access.token.claim": "true",
        "introspection.token.claim": "true",
        "userinfo.token.claim": "true",
    }
    assert ("PUT", f"{_ADMIN}/clients/{_CLIENT_UUID}/protocol-mappers/models/amr-old") in [
        (m, p) for m, p, _ in fake.writes
    ]
    assert fake.writes[writes_after_first - 1][1] == f"{_ADMIN}/clients/{_CLIENT_UUID}"

    assert second["status"] == "ok", second
    assert second["result"]["amr_mapper_fixed"] is False
    assert len(fake.writes) == writes_after_first


async def test_amr_mapper_fix_that_does_not_read_back_is_not_bound(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(client=_client())
    fake.mappers.append(_amr_mapper_without_id_token())
    path = f"{_ADMIN}/clients/{_CLIENT_UUID}/protocol-mappers/models/amr-old"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        # Keycloak accepts the PUT but keeps the old mapper.
        mock.put(path).respond(204)
        mock.get(path).respond(200, json=_amr_mapper_without_id_token())
        fake.mount(mock)
        result = await _run({})
    assert result["status"] != "ok"
    assert "check_failed" in json.dumps(result)
    assert fake.client == _client()
