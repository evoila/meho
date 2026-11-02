# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval sign-in flow op and the identity provider list.

``keycloak.authflow.ensure_approval_flow`` and
``keycloak.identity_provider.list`` run through the real dispatch stack
against a small fake Keycloak (respx). No running Keycloak and no Vault.

What is proven:

* the flow op is ``caution`` with approval and a plain ``write`` on the feed;
  the list is ``safe``;
* a realm without the flow gets it with the steps of the realm file, and the
  client gets it as its browser flow; a second run writes nothing;
* a flow with other steps stops the op, names the difference, and nothing
  is written; a missing client stops the op before anything is written;
* the binding keeps the client's other settings and reads the binding back;
* the identity provider list follows every page, finds the flows that
  redirect to a provider, and carries no secret; a failed read is an error;
* the flow op waits for approval before it writes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_FLOW_OP = "keycloak.authflow.ensure_approval_flow"
_IDP_OP = "keycloak.identity_provider.list"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-authflow-test"
_KC_HOST = "keycloak-authflow.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_ADMIN = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005621")
_CLIENT_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_FLOW_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_FLOW = "meho approval sign-in"
_SUB = "meho approval sign-in - one-time code"
_FLOW_Q = "meho%20approval%20sign-in"
_SUB_Q = "meho%20approval%20sign-in%20-%20one-time%20code"
_MADE_UP_SECRET = "made-up-idp-secret-5621"


def _operator(sub: str = "kc-authflow-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Flow Test Operator",
        email=None,
        raw_jwt="<kc-authflow-raw-jwt>",
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
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_authflow",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# A small fake Keycloak: flows, steps and one client
# ---------------------------------------------------------------------------


def _step(
    level: int, name: str, requirement: str, index: int, *, flow: bool = False
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"exec-{level}-{index}",
        "level": level,
        "index": index,
        "requirement": requirement,
        "authenticationFlow": flow,
    }
    if flow:
        row["displayName"] = name
        row["providerId"] = "basic-flow"
    else:
        row["displayName"] = name.replace("-", " ").title()
        row["providerId"] = name
    return row


def _good_steps() -> list[dict[str, Any]]:
    return [
        _step(0, "auth-username-password-form", "REQUIRED", 0),
        _step(0, _SUB, "CONDITIONAL", 1, flow=True),
        _step(1, "conditional-user-configured", "REQUIRED", 0),
        _step(1, "auth-otp-form", "REQUIRED", 1),
    ]


class FakeKeycloak:
    """Keeps the realm's flows, steps and the client between requests."""

    def __init__(self, *, flow_steps: list[dict[str, Any]] | None, client: dict[str, Any] | None):
        self.flow_steps = flow_steps
        self.client = client
        self.writes: list[tuple[str, str, Any]] = []

    def mount(self, mock: respx.MockRouter) -> None:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"{_ADMIN}/clients").mock(side_effect=self._clients)
        mock.route(path=f"{_ADMIN}/clients/{_CLIENT_UUID}").mock(side_effect=self._client)
        mock.route(path=f"{_ADMIN}/authentication/flows").mock(side_effect=self._flows)
        mock.route(path__regex=rf"^{_ADMIN}/authentication/flows/[^/]+/executions.*$").mock(
            side_effect=self._executions
        )

    def _clients(self, request: httpx.Request) -> httpx.Response:
        rows = [self.client] if self.client else []
        return httpx.Response(200, json=rows)

    def _client(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            body = json.loads(request.content)
            self.writes.append(("PUT", request.url.path, body))
            self.client = body
            return httpx.Response(204)
        return httpx.Response(200, json=self.client)

    def _flows(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            self.writes.append(("POST", request.url.path, body))
            self.flow_steps = []
            return httpx.Response(
                201, headers={"Location": f"{_ADMIN}/authentication/flows/{_FLOW_ID}"}
            )
        rows: list[dict[str, Any]] = [{"id": "browser-id", "alias": "browser", "topLevel": True}]
        if self.flow_steps is not None:
            rows.append({"id": _FLOW_ID, "alias": _FLOW, "topLevel": True})
        return httpx.Response(200, json=rows)

    def _executions(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?")[0]
        assert self.flow_steps is not None
        if request.method == "GET":
            if raw.endswith(f"/flows/{_FLOW_Q}/executions"):
                return httpx.Response(200, json=self.flow_steps)
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        self.writes.append((request.method, raw, body))
        if request.method == "PUT":
            for row in self.flow_steps:
                if row["id"] == body["id"]:
                    row["requirement"] = body["requirement"]
            return httpx.Response(204)
        level = 0 if f"/flows/{_FLOW_Q}/" in raw else 1
        index = sum(1 for row in self.flow_steps if row["level"] == level)
        if raw.endswith("/executions/flow"):
            self.flow_steps.append(_step(level, body["alias"], "DISABLED", index, flow=True))
        else:
            self.flow_steps.append(_step(level, body["provider"], "DISABLED", index))
        return httpx.Response(201)


def _client(browser: str | None = None) -> dict[str, Any]:
    overrides: dict[str, str] = {"direct_grant": "dg-flow-id"}
    if browser:
        overrides["browser"] = browser
    return {
        "id": _CLIENT_UUID,
        "clientId": "meho-cli-approve",
        "publicClient": True,
        "redirectUris": ["http://127.0.0.1:*"],
        "attributes": {"pkce.code.challenge.method": "S256"},
        "authenticationFlowBindingOverrides": overrides,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_ops_register_with_their_risk_levels(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_([_FLOW_OP, _IDP_OP])
                    )
                )
            ).scalars()
        }
    assert rows[_FLOW_OP].safety_level == "caution"
    assert rows[_FLOW_OP].requires_approval is True
    assert rows[_IDP_OP].safety_level == "safe"
    assert rows[_IDP_OP].requires_approval is False


def test_flow_op_is_a_plain_write_on_the_feed() -> None:
    assert classify_op(_FLOW_OP) == "write"
    assert classify_op(_IDP_OP) == "read"


async def test_unknown_param_is_invalid(kc: KeycloakConnector) -> None:
    result = await _run(_FLOW_OP, {"realm": "master"})
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# ensure_approval_flow
# ---------------------------------------------------------------------------


async def test_missing_flow_is_made_bound_and_second_run_changes_nothing(
    kc: KeycloakConnector,
) -> None:
    fake = FakeKeycloak(flow_steps=None, client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run(_FLOW_OP, {})
        writes_after_first = len(fake.writes)
        second = await _run(_FLOW_OP, {})

    assert first["status"] == "ok", first
    out = first["result"]
    assert out["flow_created"] is True
    assert out["binding_changed"] is True
    assert out["flow_id"] == _FLOW_ID
    assert out["browser_flow_id"] == _FLOW_ID
    assert out["previous_browser_flow_id"] is None
    assert [(s["level"], s["step"], s["requirement"]) for s in out["flow_steps"]] == [
        (0, "auth-username-password-form", "REQUIRED"),
        (0, _SUB, "CONDITIONAL"),
        (1, "conditional-user-configured", "REQUIRED"),
        (1, "auth-otp-form", "REQUIRED"),
    ]

    # The flow and its steps went to the right parents.
    posts = [(path, body) for method, path, body in fake.writes if method == "POST"]
    assert posts[0][1]["alias"] == _FLOW
    assert posts[0][1]["providerId"] == "basic-flow"
    assert posts[1] == (
        f"{_ADMIN}/authentication/flows/{_FLOW_Q}/executions/execution",
        {"provider": "auth-username-password-form"},
    )
    assert posts[2][0] == f"{_ADMIN}/authentication/flows/{_FLOW_Q}/executions/flow"
    assert posts[2][1]["alias"] == _SUB
    assert posts[3][0] == f"{_ADMIN}/authentication/flows/{_SUB_Q}/executions/execution"
    assert posts[4][1] == {"provider": "auth-otp-form"}

    # The binding kept the other override and every other client setting.
    assert fake.client is not None
    assert fake.client["authenticationFlowBindingOverrides"] == {
        "direct_grant": "dg-flow-id",
        "browser": _FLOW_ID,
    }
    assert fake.client["redirectUris"] == ["http://127.0.0.1:*"]
    assert fake.client["attributes"] == {"pkce.code.challenge.method": "S256"}

    assert second["status"] == "ok", second
    assert second["result"]["flow_created"] is False
    assert second["result"]["binding_changed"] is False
    assert len(fake.writes) == writes_after_first


async def test_same_steps_and_other_binding_only_binds(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(flow_steps=_good_steps(), client=_client(browser="old-flow-id"))
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run(_FLOW_OP, {})
    assert result["status"] == "ok", result
    assert result["result"]["flow_created"] is False
    assert result["result"]["binding_changed"] is True
    assert result["result"]["previous_browser_flow_id"] == "old-flow-id"
    assert [method for method, _, _ in fake.writes] == ["PUT"]


@pytest.mark.parametrize(
    ("steps", "named"),
    [
        (_good_steps()[:2], "step 3 ('conditional-user-configured') is missing"),
        (
            [*_good_steps()[:3], _step(1, "auth-otp-form", "ALTERNATIVE", 1)],
            "step 4 is 'auth-otp-form' (ALTERNATIVE",
        ),
        ([*_good_steps(), _step(0, "auth-cookie", "ALTERNATIVE", 2)], "the flow has 5 steps"),
        ([_step(0, "auth-cookie", "ALTERNATIVE", 0), *_good_steps()], "step 1 is 'auth-cookie'"),
    ],
)
async def test_other_steps_stop_and_name_the_difference(
    kc: KeycloakConnector, steps: list[dict[str, Any]], named: str
) -> None:
    fake = FakeKeycloak(flow_steps=steps, client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run(_FLOW_OP, {})
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "other_steps" in text
    assert named in text
    assert fake.writes == []


async def test_missing_client_stops_before_any_write(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(flow_steps=None, client=None)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run(_FLOW_OP, {"client_id": "meho-cli-approve"})
    assert result["status"] != "ok"
    assert "client_not_found" in json.dumps(result)
    assert fake.writes == []


async def test_binding_that_does_not_read_back_is_an_error(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(flow_steps=_good_steps(), client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        # Keycloak accepts the PUT but keeps the old client. (respx uses the
        # first matching route, so these go before the fake's routes.)
        mock.put(f"{_ADMIN}/clients/{_CLIENT_UUID}").respond(204)
        mock.get(f"{_ADMIN}/clients/{_CLIENT_UUID}").respond(200, json=_client())
        fake.mount(mock)
        result = await _run(_FLOW_OP, {})
    assert result["status"] != "ok"
    assert "check_failed" in json.dumps(result)


async def test_flow_list_that_is_not_a_list_is_an_error(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(flow_steps=None, client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_ADMIN}/authentication/flows").respond(200, json={"error": "odd"})
        fake.mount(mock)
        result = await _run(_FLOW_OP, {})
    assert result["status"] != "ok"
    assert "KeycloakUnexpectedResponseError" in json.dumps(result)
    assert fake.writes == []


async def test_flow_op_waits_for_approval(kc: KeycloakConnector) -> None:
    fake = FakeKeycloak(flow_steps=None, client=_client())
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        call = await call_operation(
            _operator(),
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": _FLOW_OP,
                "target": _TARGET_NAME,
                "params": {},
            },
        )
    assert call["status"] == "awaiting_approval", call
    assert fake.writes == []


# ---------------------------------------------------------------------------
# identity_provider.list
# ---------------------------------------------------------------------------


def _idp(alias: str, **extra: Any) -> dict[str, Any]:
    row = {
        "alias": alias,
        "displayName": alias.title(),
        "providerId": "oidc",
        "enabled": True,
        "hideOnLogin": False,
        "config": {"clientId": "meho", "clientSecret": _MADE_UP_SECRET},
    }
    row.update(extra)
    return row


def _mount_idp_flows(mock: respx.MockRouter) -> respx.Route:
    """Mount the flows and configs. Returns the config route no one may read."""
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )
    mock.get(f"{_ADMIN}/authentication/flows").respond(
        200,
        json=[
            {"id": "b", "alias": "browser"},
            {"id": "c", "alias": "company sso"},
            {"id": "p", "alias": "partner"},
        ],
    )
    mock.get(url__regex=r".*/flows/browser/executions$").respond(
        200,
        json=[
            {"providerId": "auth-cookie", "requirement": "ALTERNATIVE"},
            {
                "providerId": "identity-provider-redirector",
                "requirement": "DISABLED",
                "authenticationConfig": "cfg-off",
            },
        ],
    )
    mock.get(url__regex=r".*/flows/company%20sso/executions$").respond(
        200,
        json=[
            {
                "providerId": "identity-provider-redirector",
                "requirement": "ALTERNATIVE",
                "authenticationConfig": "cfg-on",
            }
        ],
    )
    # A redirect inside a DISABLED sub-flow cannot run. The next sub-flow at
    # the same level is on again, and its redirect counts.
    mock.get(url__regex=r".*/flows/partner/executions$").respond(
        200,
        json=[
            _step(0, "old partner", "DISABLED", 0, flow=True),
            {
                "level": 1,
                "index": 0,
                "providerId": "identity-provider-redirector",
                "requirement": "ALTERNATIVE",
                "authenticationConfig": "cfg-nested-off",
            },
            _step(0, "new partner", "ALTERNATIVE", 1, flow=True),
            {
                "level": 1,
                "index": 0,
                "providerId": "identity-provider-redirector",
                "requirement": "ALTERNATIVE",
                "authenticationConfig": "cfg-nested-on",
            },
        ],
    )
    mock.get(f"{_ADMIN}/authentication/config/cfg-on").respond(
        200, json={"id": "cfg-on", "alias": "to-corp", "config": {"defaultProvider": "corp"}}
    )
    mock.get(f"{_ADMIN}/authentication/config/cfg-nested-on").respond(
        200,
        json={"id": "cfg-nested-on", "alias": "to-idp-1", "config": {"defaultProvider": "idp-1"}},
    )
    return mock.get(f"{_ADMIN}/authentication/config/cfg-nested-off").respond(
        200,
        json={
            "id": "cfg-nested-off",
            "alias": "to-partner",
            "config": {"defaultProvider": "partner"},
        },
    )
    mock.get(f"{_ADMIN}/authentication/config/cfg-off").respond(
        200, json={"id": "cfg-off", "alias": "off", "config": {"defaultProvider": "idp-0"}}
    )


async def test_identity_providers_every_page_redirects_and_no_secret(
    kc: KeycloakConnector,
) -> None:
    first_page = [_idp("corp", hideOnLogin=True), _idp("partner")]
    first_page += [_idp(f"idp-{n}") for n in range(98)]
    legacy = _idp("legacy", hideOnLogin=None, config={"hideOnLoginPage": "true"})
    legacy.pop("hideOnLogin")

    def _page(request: httpx.Request) -> httpx.Response:
        first = int(request.url.params["first"])
        assert request.url.params["max"] == "100"
        return httpx.Response(200, json=first_page if first == 0 else [legacy])

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        nested_off = _mount_idp_flows(mock)
        listing = mock.get(f"{_ADMIN}/identity-provider/instances").mock(side_effect=_page)
        result = await _run(_IDP_OP, {})

    assert result["status"] == "ok", result
    out = result["result"]
    assert listing.call_count == 2
    assert out["total"] == 101
    assert out["complete"] is True
    rows = {row["alias"]: row for row in out["rows"]}
    assert rows["corp"] == {
        "alias": "corp",
        "display_name": "Corp",
        "type": "oidc",
        "enabled": True,
        "hide_on_login": True,
        "redirect_flows": ["company sso"],
    }
    # A DISABLED redirector does not count.
    assert rows["idp-0"]["redirect_flows"] == []
    # A redirector inside a DISABLED sub-flow does not count either; its
    # config is never read. The sub-flow after it is on, so its redirect counts.
    assert rows["partner"]["redirect_flows"] == []
    assert nested_off.call_count == 0
    assert rows["idp-1"]["redirect_flows"] == ["partner"]
    assert rows["legacy"]["hide_on_login"] is True
    assert _MADE_UP_SECRET not in json.dumps(result)
    assert "config" not in rows["corp"]


async def test_identity_provider_read_failure_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_idp_flows(mock)
        mock.get(f"{_ADMIN}/identity-provider/instances").respond(500, json={"error": "boom"})
        result = await _run(_IDP_OP, {})
    assert result["status"] != "ok"
    assert result.get("result") in (None, {})


async def test_identity_provider_list_that_is_not_a_list_is_an_error(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_idp_flows(mock)
        mock.get(f"{_ADMIN}/identity-provider/instances").respond(200, json={"rows": []})
        result = await _run(_IDP_OP, {})
    assert result["status"] != "ok"
    assert "KeycloakUnexpectedResponseError" in json.dumps(result)
