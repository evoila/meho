# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The passkey step op: ``keycloak.authflow.ensure_passkey_code_skip``.

The op runs through the real dispatch stack against a small fake Keycloak
(respx). The fake answers the way the Keycloak 26.8.1 Admin API does
(``AuthenticationManagementResource``): a flow's steps come back as one flat
list, depth first, with ``level``, ``index``, ``priority``; a sub-flow row
has its alias in ``displayName`` and no ``providerId``; a copy names its
sub-flows and configs ``<new name> <old name>``; a new step is ``DISABLED``
and goes to the end; ``raise-priority`` swaps a step with the one before it.
The built-in ``browser`` flow of a realm made before 26.4 has the steps of
Keycloak 26.3 (``DefaultAuthenticationFlows.browserFlow``). All names are
made up.

What is proven:

* a realm made before 26.4 gets a bound copy with the step, right after
  ``conditional-user-configured``, required, with ``webauthn-passwordless``
  and ``included`` off; a second run writes nothing;
* a realm that has the step (made on 26.4 or newer) is not changed;
* a custom browser flow, or a flow with no conditional one-time code
  sub-flow, stops the run and nothing is written;
* a copy left by a run that stopped half-way is finished; a copy with other
  steps, or with other settings on a step, stops the run;
* the bind keeps the realm's other settings, also a setting changed while
  the flow was set up, and is read back; a browser flow changed meanwhile
  stops the run before the bind;
* the op is ``caution`` with approval, a plain ``write`` on the feed, and
  waits for approval; its approval card names the realm and both flows.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
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
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_OP = "keycloak.authflow.ensure_passkey_code_skip"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-passkey-test"
_KC_HOST = "keycloak-passkey.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "example-realm"
_ADMIN_REALM = "master"
_ADMIN = f"/admin/realms/{_REALM}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005663")
_COPY = "browser with passkeys"
_CODE_SUB = "Browser - Conditional 2FA"
_MADE_UP_SMTP_PASSWORD = "made-up-smtp-password-5663"

#: The provider display names Keycloak 26.8.1 lists (``getDisplayType``).
_DISPLAY = {
    "auth-cookie": "Cookie",
    "auth-spnego": "Kerberos",
    "identity-provider-redirector": "Identity Provider Redirector",
    "conditional-user-configured": "Condition - user configured",
    "organization": "Organization Identity-First Login",
    "auth-username-password-form": "Username Password Form",
    "auth-otp-form": "OTP Form",
    "webauthn-authenticator": "WebAuthn Authenticator",
    "auth-recovery-authn-code-form": "Recovery Authentication Code Form",
    "conditional-credential": "Condition - credential",
}


def _operator(sub: str = "kc-passkey-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Passkey Test Operator",
        email=None,
        raw_jwt="<kc-passkey-raw-jwt>",
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
                notes="seeded by test_connectors_keycloak_authflow_passkey",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params=params or {},
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# A small fake Keycloak that keeps flows, steps, configs and the realm
# ---------------------------------------------------------------------------


def _ex(
    provider: str | None = None,
    requirement: str = "REQUIRED",
    *,
    flow: str | None = None,
    config: str | None = None,
) -> dict[str, Any]:
    return {"provider": provider, "flow": flow, "requirement": requirement, "config": config}


class FakeKeycloak:
    """Flows as ``alias -> {builtIn, topLevel, steps}``; each step a dict as in :func:`_ex`."""

    def __init__(self, flows: dict[str, dict[str, Any]], *, browser_flow: str = "browser"):
        self.flows = flows
        self.configs: dict[str, dict[str, Any]] = {}
        self.ids: dict[str, str] = {}
        self.realm: dict[str, Any] = {
            "id": "realm-id",
            "realm": _REALM,
            "enabled": True,
            "browserFlow": browser_flow,
            "directGrantFlow": "direct grant",
            "smtpServer": {"host": "smtp.example.invalid", "password": "**********"},
            "attributes": {"frontendUrl": ""},
        }
        self.writes: list[tuple[str, str, Any]] = []
        #: Runs once, on the first flow write: another operator at work meanwhile.
        self.on_first_flow_write: Callable[[], None] | None = None
        self._counter = 0
        for name, item in flows.items():
            item.setdefault("id", f"flow-{name}")
            for step in item["steps"]:
                self._new_id(step)

    def _new_id(self, step: dict[str, Any]) -> None:
        self._counter += 1
        step.setdefault("id", f"exec-{self._counter}")
        step.setdefault("priority", self._counter * 10)

    def add_config(self, alias: str, config: dict[str, str]) -> str:
        config_id = f"config-{len(self.configs) + 1}"
        self.configs[config_id] = {"id": config_id, "alias": alias, "config": dict(config)}
        return config_id

    # -- the Admin API -----------------------------------------------------

    def mount(self, mock: respx.MockRouter) -> None:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.route(path=_ADMIN).mock(side_effect=self._realm)
        mock.route(path=f"{_ADMIN}/authentication/flows").mock(side_effect=self._flow_list)
        mock.route(path__regex=rf"^{_ADMIN}/authentication/flows/[^/]+/.+$").mock(
            side_effect=self._flow_sub
        )
        mock.route(path__regex=rf"^{_ADMIN}/authentication/executions/[^/]+/.+$").mock(
            side_effect=self._execution
        )
        mock.route(path__regex=rf"^{_ADMIN}/authentication/config/[^/]+$").mock(
            side_effect=self._config
        )

    def _realm(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            body = json.loads(request.content)
            self.writes.append(("PUT", request.url.path, body))
            assert body["browserFlow"] in self.flows
            self.realm = body
            return httpx.Response(204)
        return httpx.Response(200, json=self.realm)

    def _flow_list(self, request: httpx.Request) -> httpx.Response:
        rows = [
            {
                "id": item["id"],
                "alias": alias,
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": item["builtIn"],
            }
            for alias, item in self.flows.items()
            if item["topLevel"]
        ]
        return httpx.Response(200, json=rows)

    def _flow_sub(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?")[0]
        rest = raw[len(f"{_ADMIN}/authentication/flows/") :]
        alias_q, _, tail = rest.partition("/")
        alias = unquote(alias_q)
        if alias not in self.flows:
            return httpx.Response(404, json={"error": "Flow not found"})
        if request.method == "GET" and tail == "executions":
            return httpx.Response(200, json=self._listed(alias, 0))
        body = json.loads(request.content)
        self.writes.append((request.method, raw, body))
        self._first_flow_write()
        if tail == "copy":
            return self._copy(alias, body["newName"])
        if tail == "executions" and request.method == "PUT":
            for item in self.flows.values():
                for step in item["steps"]:
                    if step["id"] == body["id"]:
                        step["requirement"] = body["requirement"]
            return httpx.Response(204)
        if tail == "executions/execution":
            if self.flows[alias]["builtIn"]:
                return httpx.Response(400, json={"error": "built in flow"})
            step = _ex(body["provider"], "DISABLED")
            self._new_id(step)
            self.flows[alias]["steps"].append(step)
            return httpx.Response(201)
        return httpx.Response(405)

    def _copy(self, alias: str, new_name: str) -> httpx.Response:
        if new_name in self.flows:
            return httpx.Response(409, json={"errorMessage": "already exists"})
        self._copy_flow(alias, new_name, new_name, top_level=True)
        return httpx.Response(201)

    def _copy_flow(self, alias: str, new_alias: str, prefix: str, *, top_level: bool) -> None:
        steps = []
        for step in self.flows[alias]["steps"]:
            new = dict(step)
            new.pop("id")
            if step["flow"]:
                sub_alias = f"{prefix} {step['flow']}"
                self._copy_flow(step["flow"], sub_alias, prefix, top_level=False)
                new["flow"] = sub_alias
            if step["config"]:
                old = self.configs[step["config"]]
                new["config"] = self.add_config(f"{prefix} {old['alias']}", old["config"])
            self._counter += 1
            new["id"] = f"exec-{self._counter}"
            steps.append(new)
        self.flows[new_alias] = {
            "id": f"flow-{new_alias}",
            "builtIn": False,
            "topLevel": top_level,
            "steps": steps,
        }

    def _find(self, execution_id: str) -> tuple[str, int]:
        for alias, item in self.flows.items():
            for index, step in enumerate(item["steps"]):
                if step["id"] == execution_id:
                    return alias, index
        raise AssertionError(f"no execution {execution_id}")

    def _execution(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        rest = raw[len(f"{_ADMIN}/authentication/executions/") :]
        execution_id, _, tail = rest.partition("/")
        body = json.loads(request.content) if request.content else None
        self.writes.append((request.method, raw, body))
        self._first_flow_write()
        alias, index = self._find(execution_id)
        steps = self.flows[alias]["steps"]
        if tail == "config":
            assert body is not None
            if any(item["alias"] == body["alias"] for item in self.configs.values()):
                return httpx.Response(409, json={"errorMessage": "config exists"})
            steps[index]["config"] = self.add_config(body["alias"], body["config"])
            return httpx.Response(201)
        if tail == "raise-priority":
            if self.flows[alias]["builtIn"]:
                return httpx.Response(400)
            if index > 0:
                before, step = steps[index - 1], steps[index]
                before["priority"], step["priority"] = step["priority"], before["priority"]
                steps[index - 1], steps[index] = step, before
            return httpx.Response(204)
        return httpx.Response(405)

    def _first_flow_write(self) -> None:
        hook, self.on_first_flow_write = self.on_first_flow_write, None
        if hook is not None:
            hook()

    def _config(self, request: httpx.Request) -> httpx.Response:
        config_id = request.url.path.rsplit("/", 1)[-1]
        if config_id not in self.configs:
            return httpx.Response(404, json={"error": "Could not find authenticator config"})
        return httpx.Response(200, json=self.configs[config_id])

    def _listed(self, alias: str, level: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, step in enumerate(self.flows[alias]["steps"]):
            row: dict[str, Any] = {
                "id": step["id"],
                "requirement": step["requirement"],
                "level": level,
                "index": index,
                "priority": step["priority"],
            }
            if step["flow"]:
                row.update(
                    displayName=step["flow"],
                    description="",
                    configurable=False,
                    authenticationFlow=True,
                    flowId=self.flows[step["flow"]]["id"],
                    requirementChoices=["REQUIRED", "ALTERNATIVE", "DISABLED", "CONDITIONAL"],
                )
                rows.append(row)
                rows.extend(self._listed(step["flow"], level + 1))
                continue
            row.update(
                displayName=_DISPLAY[step["provider"]],
                configurable=step["provider"] == "conditional-credential",
                providerId=step["provider"],
                requirementChoices=["REQUIRED", "DISABLED"],
            )
            if step["config"]:
                row["authenticationConfig"] = step["config"]
                row["alias"] = self.configs[step["config"]]["alias"]
            rows.append(row)
        return rows

    # -- helpers for the asserts --------------------------------------------

    def code_steps(self, alias: str) -> list[tuple[str, str]]:
        return [(s["provider"], s["requirement"]) for s in self.flows[alias]["steps"]]


def _browser_flows() -> dict[str, dict[str, Any]]:
    """The built-in browser flow of Keycloak 26.3, or of 26.4 and newer."""
    code = [
        _ex("conditional-user-configured"),
        _ex("auth-otp-form", "ALTERNATIVE"),
        _ex("webauthn-authenticator", "DISABLED"),
        _ex("auth-recovery-authn-code-form", "DISABLED"),
    ]
    flows: dict[str, dict[str, Any]] = {
        "browser": {
            "builtIn": True,
            "topLevel": True,
            "steps": [
                _ex("auth-cookie", "ALTERNATIVE"),
                _ex("auth-spnego", "DISABLED"),
                _ex("identity-provider-redirector", "ALTERNATIVE"),
                _ex(requirement="ALTERNATIVE", flow="Organization"),
                _ex(requirement="ALTERNATIVE", flow="forms"),
            ],
        },
        "Organization": {
            "builtIn": True,
            "topLevel": False,
            "steps": [_ex(requirement="CONDITIONAL", flow="Browser - Conditional Organization")],
        },
        "Browser - Conditional Organization": {
            "builtIn": True,
            "topLevel": False,
            "steps": [_ex("conditional-user-configured"), _ex("organization", "ALTERNATIVE")],
        },
        "forms": {
            "builtIn": True,
            "topLevel": False,
            "steps": [
                _ex("auth-username-password-form"),
                _ex(requirement="CONDITIONAL", flow=_CODE_SUB),
            ],
        },
        _CODE_SUB: {"builtIn": True, "topLevel": False, "steps": code},
        "direct grant": {"builtIn": True, "topLevel": True, "steps": []},
    }
    return flows


def _fake(*, with_condition: bool = False, **kwargs: Any) -> FakeKeycloak:
    fake = FakeKeycloak(_browser_flows(), **kwargs)
    if with_condition:
        config_id = fake.add_config(
            "browser-conditional-credential", {"credentials": "webauthn-passwordless"}
        )
        step = _ex("conditional-credential", config=config_id)
        fake._new_id(step)
        fake.flows[_CODE_SUB]["steps"].insert(1, step)
    return fake


_COPY_CODE_SUB = f"{_COPY} {_CODE_SUB}"
_WANTED_CODE_STEPS = [
    ("conditional-user-configured", "REQUIRED"),
    ("conditional-credential", "REQUIRED"),
    ("auth-otp-form", "ALTERNATIVE"),
    ("webauthn-authenticator", "DISABLED"),
    ("auth-recovery-authn-code-form", "DISABLED"),
]


def _condition_config(fake: FakeKeycloak, alias: str) -> dict[str, str]:
    step = next(s for s in fake.flows[alias]["steps"] if s["provider"] == "conditional-credential")
    config: dict[str, str] = fake.configs[step["config"]]["config"]
    return config


# ---------------------------------------------------------------------------
# Registration, approval and the approval card
# ---------------------------------------------------------------------------


async def test_op_registers_as_caution_with_approval(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    assert "keycloak.realm.update" in row.description
    assert classify_op(_OP) == "write"


async def test_unknown_param_is_invalid(kc: KeycloakConnector) -> None:
    result = await _run({"realm": "master"})
    assert result["extras"]["error_code"] == "invalid_params"


async def test_op_waits_for_approval_and_the_card_names_realm_and_flows(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        parked = await dispatch(
            operator=_operator(), connector_id=_CONNECTOR_ID, op_id=_OP, target=target, params={}
        )
        call = await call_operation(
            _operator(),
            {"connector_id": _CONNECTOR_ID, "op_id": _OP, "target": _TARGET_NAME, "params": {}},
        )
    assert parked.status == "awaiting_approval", parked.error
    assert call["status"] == "awaiting_approval", call
    assert fake.writes == []
    async with get_sessionmaker()() as session:
        pending = await session.get(
            ApprovalRequest, uuid.UUID(parked.extras["approval_request_id"])
        )
        assert pending is not None
        card = pending.proposed_effect["preview"]
    assert card["realm"] == _REALM
    assert card["copies_flow"] == "browser"
    assert card["new_flow"] == _COPY
    assert "Every browser login" in card["affects"]
    assert "keycloak.realm.update" in card["way_back"]


# ---------------------------------------------------------------------------
# The four cases of the task
# ---------------------------------------------------------------------------


async def test_old_realm_gets_a_bound_copy_and_second_run_changes_nothing(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        first = await _run()
        writes_after_first = len(fake.writes)
        second = await _run()

    assert first["status"] == "ok", first
    out = first["result"]
    assert out["previous_browser_flow"] == "browser"
    assert out["browser_flow"] == _COPY
    assert (out["changed"], out["flow_copied"], out["step_added"], out["binding_changed"]) == (
        True,
        True,
        True,
        True,
    )
    assert out["unrequested_changes"] == []

    # The copy has the step right after the user check, required, and the
    # config makes it true only when no passkey was used: a password login
    # still asks for the code, a passkey login does not.
    assert fake.code_steps(_COPY_CODE_SUB) == _WANTED_CODE_STEPS
    assert _condition_config(fake, _COPY_CODE_SUB) == {
        "credentials": "webauthn-passwordless",
        "included": "false",
    }
    # The built-in flow is untouched, and the realm uses the copy.
    assert fake.code_steps(_CODE_SUB)[1] == ("auth-otp-form", "ALTERNATIVE")
    assert fake.realm["browserFlow"] == _COPY
    listed = [(s["level"], s["step"]) for s in out["flow_steps"]]
    assert (1, _COPY_CODE_SUB) in listed
    assert (2, "conditional-credential") in listed

    assert second["status"] == "ok", second
    assert second["result"]["changed"] is False
    assert second["result"]["browser_flow"] == _COPY
    assert len(fake.writes) == writes_after_first


async def test_bind_keeps_the_other_realm_settings(kc: KeycloakConnector) -> None:
    fake = _fake()
    before = copy.deepcopy(fake.realm)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    assert {k: v for k, v in fake.realm.items() if k != "browserFlow"} == {
        k: v for k, v in before.items() if k != "browserFlow"
    }
    realm_puts = [body for method, path, body in fake.writes if path == _ADMIN]
    assert len(realm_puts) == 1


async def test_setting_changed_during_flow_setup_is_kept(kc: KeycloakConnector) -> None:
    fake = _fake()

    def other_operator() -> None:
        fake.realm["displayName"] = "Changed while the flow was set up"
        fake.realm["attributes"]["frontendUrl"] = "https://login.example.invalid"

    fake.on_first_flow_write = other_operator
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    assert result["result"]["unrequested_changes"] == []
    assert fake.on_first_flow_write is None  # the hook ran
    assert fake.realm["browserFlow"] == _COPY
    assert fake.realm["displayName"] == "Changed while the flow was set up"
    assert fake.realm["attributes"]["frontendUrl"] == "https://login.example.invalid"


async def test_browser_flow_changed_during_flow_setup_stops_before_the_bind(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()

    def other_operator() -> None:
        fake.realm["browserFlow"] = "direct grant"

    fake.on_first_flow_write = other_operator
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "realm_changed" in text
    assert "direct grant" in text
    assert fake.on_first_flow_write is None
    assert not any(path == _ADMIN for _, path, _ in fake.writes)
    assert fake.realm["browserFlow"] == "direct grant"
    # The copy has the step, so a new run (after a person checks the realm)
    # finds it finished.
    assert fake.code_steps(_COPY_CODE_SUB) == _WANTED_CODE_STEPS


async def test_realm_with_the_step_is_not_changed(kc: KeycloakConnector) -> None:
    fake = _fake(with_condition=True)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["changed"] is False
    assert out["browser_flow"] == "browser"
    assert fake.writes == []
    assert _COPY not in fake.flows


async def test_custom_browser_flow_stops_and_changes_nothing(kc: KeycloakConnector) -> None:
    flows = _browser_flows()
    flows["my login"] = {"builtIn": False, "topLevel": True, "steps": [_ex("auth-cookie")]}
    fake = FakeKeycloak(flows, browser_flow="my login")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "custom_flow" in text
    assert "my login" in text
    assert fake.writes == []


async def test_flow_without_code_sub_flow_stops_and_changes_nothing(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()
    fake.flows["forms"]["steps"].pop()  # drop the one-time code sub-flow
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "no_code_step" in json.dumps(result)
    assert fake.writes == []


async def test_code_sub_flow_that_is_not_conditional_stops(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake.flows["forms"]["steps"][1]["requirement"] = "REQUIRED"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "no_code_step" in json.dumps(result)
    assert fake.writes == []


async def test_condition_with_other_settings_in_built_in_flow_stops(
    kc: KeycloakConnector,
) -> None:
    fake = _fake(with_condition=True)
    step = fake.flows[_CODE_SUB]["steps"][1]
    fake.configs[step["config"]]["config"] = {"credentials": "otp", "included": "true"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "other_steps" in json.dumps(result)
    assert fake.writes == []


# ---------------------------------------------------------------------------
# A run that stopped half-way
# ---------------------------------------------------------------------------


async def test_copy_without_the_step_is_finished(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake._copy("browser", _COPY)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["flow_copied"] is False
    assert out["step_added"] is True
    assert not any(path.endswith("/copy") for _, path, _ in fake.writes)
    assert fake.code_steps(_COPY_CODE_SUB) == _WANTED_CODE_STEPS
    assert fake.realm["browserFlow"] == _COPY


async def test_copy_with_a_disabled_step_and_no_config_is_finished(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()
    fake._copy("browser", _COPY)
    step = _ex("conditional-credential", "DISABLED")
    fake._new_id(step)
    fake.flows[_COPY_CODE_SUB]["steps"].append(step)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    assert result["result"]["step_added"] is False
    assert fake.code_steps(_COPY_CODE_SUB) == _WANTED_CODE_STEPS
    assert _condition_config(fake, _COPY_CODE_SUB)["included"] == "false"


async def test_copy_with_other_steps_stops(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake._copy("browser", _COPY)
    fake.flows[_COPY]["steps"][0]["requirement"] = "DISABLED"  # cookie switched off
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "other_steps" in text
    assert "auth-cookie" in text
    assert fake.writes == []
    assert fake.realm["browserFlow"] == "browser"


async def test_copy_whose_other_step_has_other_settings_stops(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake._copy("browser", _COPY)
    # Someone gave the copy's identity-provider redirect a default provider.
    config_id = fake.add_config("browser with passkeys idp", {"defaultProvider": "partner-idp"})
    fake.flows[_COPY]["steps"][2]["config"] = config_id
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    text = json.dumps(result)
    assert "other_steps" in text
    assert "identity-provider-redirector" in text
    assert "partner-idp" not in text
    assert fake.writes == []
    assert fake.realm["browserFlow"] == "browser"


async def test_copy_with_the_same_settings_under_copied_aliases_is_finished(
    kc: KeycloakConnector,
) -> None:
    fake = _fake()
    config_id = fake.add_config("idp-redirector", {"defaultProvider": "partner-idp"})
    fake.flows["browser"]["steps"][2]["config"] = config_id
    fake._copy("browser", _COPY)
    copied = fake.flows[_COPY]["steps"][2]["config"]
    assert copied != config_id
    assert fake.configs[copied]["alias"] == f"{_COPY} idp-redirector"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] == "ok", result
    assert result["result"]["flow_copied"] is False
    assert result["result"]["step_added"] is True
    assert fake.code_steps(_COPY_CODE_SUB) == _WANTED_CODE_STEPS
    assert fake.realm["browserFlow"] == _COPY


async def test_copy_whose_condition_has_other_settings_stops(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake._copy("browser", _COPY)
    config_id = fake.add_config("someone else", {"credentials": "otp"})
    step = _ex("conditional-credential", config=config_id)
    fake._new_id(step)
    fake.flows[_COPY_CODE_SUB]["steps"].insert(1, step)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "other_steps" in json.dumps(result)
    assert fake.writes == []


# ---------------------------------------------------------------------------
# Failures are errors, never a success
# ---------------------------------------------------------------------------


async def test_failed_config_write_leaves_the_realm_unbound(kc: KeycloakConnector) -> None:
    fake = _fake()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(url__regex=r".*/authentication/executions/[^/]+/config$").respond(500)
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert fake.realm["browserFlow"] == "browser"
    assert not any(path == _ADMIN for _, path, _ in fake.writes)


async def test_bind_that_does_not_read_back_is_an_error(kc: KeycloakConnector) -> None:
    fake = _fake()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        # Keycloak accepts the PUT but keeps the old browser flow.
        mock.put(_ADMIN).respond(204)
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "check_failed" in json.dumps(result)


async def test_step_list_that_is_not_a_list_is_an_error(kc: KeycloakConnector) -> None:
    fake = _fake()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_ADMIN}/authentication/flows/browser/executions").respond(
            200, json={"error": "odd"}
        )
        fake.mount(mock)
        result = await _run()
    assert result["status"] != "ok"
    assert "KeycloakUnexpectedResponseError" in json.dumps(result)
    assert fake.writes == []


async def test_result_and_errors_carry_no_realm_secret(kc: KeycloakConnector) -> None:
    fake = _fake()
    fake.realm["smtpServer"]["password"] = _MADE_UP_SMTP_PASSWORD
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        fake.mount(mock)
        ok = await _run()
    assert ok["status"] == "ok", ok
    assert _MADE_UP_SMTP_PASSWORD not in json.dumps(ok)
