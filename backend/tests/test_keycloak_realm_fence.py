# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Keycloak target works on one realm only, and every answer names it.

* The four ops that take a realm value (``keycloak.realm.update``,
  ``keycloak.realm.create``, ``keycloak.realm.delete``,
  ``keycloak.user.check_login``) refuse another realm than the managed realm
  before the call waits for approval. Nothing is stored and nothing is sent
  to Keycloak. The managed realm works.
* The handler refuses it too, for a call that is already approved.
* With the switch ``KEYCLOAK_ANSWER_REALM`` on, each answer of every
  registered read, and of a write, has a ``managed_realm`` field. With it
  off (the default), the answers are as before.
* The approval card of a write shows the realm.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.api.v1.approvals import _proposed_effect_for
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector  # also loads the registry
from meho_backplane.connectors.keycloak.answer_realm import REALM_FIELD
from meho_backplane.connectors.keycloak.realm_params import (
    FENCED_OPS,
    realm_fence_problem,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import without_internal_effect_keys
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import list_operation_groups
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "keycloak-fence"
_KC_HOST = "keycloak-fence.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "example-realm"
_OTHER = "other-realm"
_REALM_PATH = f"/admin/realms/{_REALM}"

_UPDATE = "keycloak.realm.update"
_CREATE = "keycloak.realm.create"
_DELETE = "keycloak.realm.delete"
_CHECK_LOGIN = "keycloak.user.check_login"
_FENCED = [_UPDATE, _CREATE, _DELETE, _CHECK_LOGIN]


def _registered_reads() -> list[str]:
    """Every op the connector registers that runs without approval: the reads.

    Runs the connector's own ``register_operations`` and keeps what it
    registers, so a new read is a new case.
    """
    registered: list[str] = []

    async def _keep(**op: Any) -> None:
        if not op["requires_approval"]:
            registered.append(op["op_id"])

    with patch("meho_backplane.operations.typed_register.register_typed_operation", _keep):
        asyncio.run(KeycloakConnector.register_operations())
    return registered


_READS = _registered_reads()
_ID = "0a0b0c0d-0000-0000-0000-000000000001"
_READ_PARAMS: dict[str, dict[str, Any]] = {
    "keycloak.client.get": {"id": _ID},
    "keycloak.role_mapping.get": {"id": _ID},
    "keycloak.role.users": {"name": "example-role"},
    "keycloak.group.member.list": {"id": _ID},
    "keycloak.user.get": {"id": _ID},
    "keycloak.user.sessions": {"id": _ID},
    "keycloak.user.lock_status": {"id": _ID},
}


def _fenced_params(op_id: str, realm: str) -> dict[str, Any]:
    """Params of a fenced op that name *realm*."""
    if op_id == _UPDATE:
        return {"realm": realm, "representation": {"enabled": True}}
    if op_id == _CREATE:
        return {"representation": {"realm": realm}}
    if op_id == _DELETE:
        return {"realm": realm}
    return {"realm": realm, "username": "someone", "password_secret_ref": "example/ref"}


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="admin-cli", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ANSWER_REALM", "true")
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
async def operator() -> AsyncIterator[Operator]:
    """Register the ops, seed a tenant and a Keycloak target that manages one realm."""
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=f"kc-fence-{tenant_id.hex[:8]}", name="Fence"))
        await session.commit()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=tenant_id,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="example/keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_keycloak_realm_fence",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector  # type: ignore[assignment]
    yield Operator(
        sub="keycloak-fence-operator",
        name="Keycloak fence operator",
        email=None,
        raw_jwt="<keycloak-fence-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    await connector.aclose()


async def _target(operator: Operator) -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, operator.tenant_id, _TARGET_NAME)


async def _dispatch(
    operator: Operator, op_id: str, params: dict[str, Any], *, approved: bool
) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(operator),
        params=params,
        _approved=approved,
    )


async def _approval_rows() -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _mount_token(mock: respx.MockRouter) -> Any:
    return mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-fence-token", "expires_in": 300}
    )


# ---------------------------------------------------------------------------
# Another realm is refused before approval
# ---------------------------------------------------------------------------


def test_every_op_that_takes_a_realm_is_fenced() -> None:
    assert set(_FENCED) == FENCED_OPS


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _FENCED)
async def test_another_realm_is_refused_before_approval(operator: Operator, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token_route = _mount_token(mock)
        result = await _dispatch(operator, op_id, _fenced_params(op_id, _OTHER), approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    text = result.error or ""
    assert "target that names that realm" in text
    assert _REALM in text
    assert _OTHER not in text
    assert token_route.call_count == 0
    assert len(mock.calls) == 0
    assert await _approval_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _FENCED)
async def test_preview_gives_the_same_refusal(operator: Operator, op_id: str) -> None:
    envelope = await preview_dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(operator),
        params=_fenced_params(op_id, _OTHER),
    )
    assert envelope["extras"]["error_code"] == "refused_before_approval"
    assert await _approval_rows() == []


@pytest.mark.asyncio
async def test_a_rename_in_the_representation_is_refused_too(operator: Operator) -> None:
    params = {"representation": {"realm": _OTHER, "enabled": True}}
    result = await _dispatch(operator, _UPDATE, params, approved=False)
    assert result.extras["error_code"] == "refused_before_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _FENCED)
async def test_the_handler_refuses_another_realm_too(operator: Operator, op_id: str) -> None:
    """A call that is already approved still sends nothing to Keycloak."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        result = await _dispatch(operator, op_id, _fenced_params(op_id, _OTHER), approved=True)

    assert result.status == "error"
    assert result.extras["exception_class"] == "ValueError"
    assert len(mock.calls) == 0


async def test_fence_problem_text_for_the_managed_realm_and_for_other_ops(
    operator: Operator,
) -> None:
    target = await _target(operator)
    for op_id in _FENCED:
        assert realm_fence_problem(op_id, _fenced_params(op_id, _REALM), target) is None
        assert realm_fence_problem(op_id, _fenced_params(op_id, _OTHER), target) is not None
    # A call that names no realm uses the managed realm.
    assert realm_fence_problem(_UPDATE, {"representation": {}}, target) is None
    # An op that takes no realm value is not fenced.
    assert realm_fence_problem("keycloak.user.list", {"realm": _OTHER}, target) is None


# ---------------------------------------------------------------------------
# The managed realm works, and the answer names it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_on_the_managed_realm_works_and_the_answer_names_it(
    operator: Operator,
) -> None:
    realm_body = {"id": "r", "realm": _REALM, "enabled": True}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=realm_body)
        put_route = mock.put(_REALM_PATH).respond(204)
        result = await _dispatch(operator, _UPDATE, _fenced_params(_UPDATE, _REALM), approved=True)

    assert result.status == "ok", result.error
    assert put_route.call_count == 1
    assert result.result[REALM_FIELD] == _REALM


def _sent_body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"representation": {"enabled": True}},
        {"representation": {"enabled": True, "realm": ""}, "realm": ""},
        {"representation": {"enabled": True, "realm": None}},
        {"representation": {"enabled": True}, "realm": _REALM},
        {"representation": {"enabled": True, "realm": _REALM}, "realm": _REALM},
    ],
    ids=["no-name", "empty-names", "null-name", "top-level", "both"],
)
async def test_create_sends_the_managed_realm_as_the_name(
    operator: Operator, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, params, approved=True)

    assert result.status == "ok", result.error
    assert result.result["realm"] == _REALM
    assert _sent_body(post_route) == {"enabled": True, "realm": _REALM}


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True], ids=["before-approval", "approved"])
@pytest.mark.parametrize(
    "params",
    [
        {"representation": {"realm": _REALM}, "realm": _OTHER},
        {"representation": {"realm": _OTHER}, "realm": _REALM},
    ],
    ids=["top-level-differs", "representation-differs"],
)
async def test_create_with_two_names_that_differ_is_refused(
    operator: Operator, params: dict[str, Any], approved: bool
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, params, approved=approved)

    assert result.status == ("error" if approved else "denied"), result
    if approved:
        assert result.extras["exception_class"] == "ValueError"
    else:
        assert result.extras["error_code"] == "refused_before_approval"
    assert len(mock.calls) == 0
    assert await _approval_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True], ids=["before-approval", "approved"])
async def test_create_without_a_name_needs_a_plain_ascii_managed_realm(
    operator: Operator, approved: bool
) -> None:
    async with get_sessionmaker()() as session:
        row = await resolve_target(session, operator.tenant_id, _TARGET_NAME)
        row.extras = {"managed_realm": "exämple"}
        await session.commit()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, {"representation": {}}, approved=approved)

    assert result.status == ("error" if approved else "denied"), result
    assert "plain ASCII" in f"{result.error} {result.extras}"
    assert len(mock.calls) == 0


@pytest.mark.asyncio
async def test_create_of_the_managed_realm_works(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, _fenced_params(_CREATE, _REALM), approved=True)

    assert result.status == "ok", result.error
    assert post_route.call_count == 1
    assert result.result[REALM_FIELD] == _REALM


_EVENTS_CONFIG = {
    "eventsEnabled": False,
    "eventsExpiration": 0,
    "eventsListeners": [],
    "enabledEventTypes": [],
    "adminEventsEnabled": False,
    "adminEventsDetailsEnabled": False,
}


def _keycloak_answer(request: httpx.Request) -> httpx.Response:
    """A small Keycloak: the realm, a count, one object by id, else an empty list."""
    path = request.url.path
    if path == _REALM_PATH:
        realm = {
            "realm": _REALM,
            "browserFlow": "browser",
            "bruteForceProtected": False,
            "eventsEnabled": True,
            "adminEventsEnabled": True,
        }
        return httpx.Response(200, json=realm)
    if path.endswith("/count"):
        return httpx.Response(200, json=0)
    if path.endswith("/events/config"):
        return httpx.Response(200, json=_EVENTS_CONFIG)
    if "/brute-force/users/" in path:
        return httpx.Response(200, json={"disabled": False, "numFailures": 0})
    if path.endswith((f"/{_ID}", "/role-mappings")):
        return httpx.Response(200, json={"id": _ID, "username": "someone", "enabled": True})
    return httpx.Response(200, json=[])


async def _read(operator: Operator, op_id: str) -> Any:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get(url__regex=r"/admin/realms/.*").mock(side_effect=_keycloak_answer)
        result = await _dispatch(operator, op_id, _READ_PARAMS.get(op_id, {}), approved=False)
    assert result.status == "ok", result.error
    assert get_route.called
    assert all(f"/admin/realms/{_REALM}" in str(c.request.url) for c in get_route.calls)
    return result.result


def test_the_read_list_has_every_read() -> None:
    assert {"keycloak.events.list", "keycloak.admin_events.list"} <= set(_READS)
    assert len(_READS) == len(set(_READS))
    assert set(_READ_PARAMS) <= set(_READS)


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _READS)
async def test_each_read_answer_names_the_realm(operator: Operator, op_id: str) -> None:
    answer = await _read(operator, op_id)
    assert isinstance(answer, dict)
    assert answer[REALM_FIELD] == _REALM
    assert next(iter(answer)) == REALM_FIELD


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _READS)
async def test_with_the_switch_off_each_read_answer_is_as_before(
    operator: Operator, op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_field = await _read(operator, op_id)
    monkeypatch.delenv("KEYCLOAK_ANSWER_REALM")
    get_settings.cache_clear()
    assert get_settings().keycloak_answer_realm is False
    without_field = await _read(operator, op_id)
    assert REALM_FIELD not in without_field
    assert {**without_field, REALM_FIELD: _REALM} == with_field


#: Agent-visible texts with the switch off, as release v0.48.0 has them. Each
#: is kept as released, except for one sentence of a rule the code enforces.
_REFUSED = "Another realm is refused."


async def _described(op_id: str) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        found = await session.execute(
            select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
        )
        return found.scalar_one()


@pytest.mark.asyncio
async def test_with_the_switch_off_the_texts_an_agent_reads_give_no_new_advice(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What discovery returns: the released texts, plus the one rule the code enforces."""
    monkeypatch.delenv("KEYCLOAK_ANSWER_REALM")
    get_settings.cache_clear()
    answer = await list_operation_groups(operator, {"connector_id": _CONNECTOR_ID})
    groups = {group["group_key"]: group["when_to_use"] for group in answer["groups"]}
    text = groups["realm_write"]
    assert "A target works on its managed realm only. Both require human approval." in text
    assert "create a new realm through" not in text

    login = await _described(_CHECK_LOGIN)
    assert login.parameter_schema["examples"] == [
        {
            "realm": "master",
            "username": "admin",
            "password_secret_ref": "keycloak/master/admin-old",
        }
    ]
    assert login.parameter_schema["properties"]["realm"]["description"] == (
        f"Realm to log in to. Default: the target's managed realm. {_REFUSED}"
    )
    assert login.llm_instructions["parameter_hints"]["realm"] == (
        f"Realm of the user, for example 'master'. Default: managed realm. {_REFUSED}"
    )
    create = await _described(_CREATE)
    assert "treated as an idempotent success" in create.description
    assert "dispatches via the admin-auth path" in create.description
    assert create.parameter_schema["properties"]["realm"]["description"] == (
        "Realm name (if not in representation). "
        "Another realm than the target's managed realm is refused."
    )
    update = await _described(_UPDATE)
    assert update.parameter_schema["properties"]["realm"]["description"] == (
        f"Realm to update (default managed_realm). {_REFUSED}"
    )
    delete = await _described(_DELETE)
    assert delete.llm_instructions["parameter_hints"]["realm"] == (
        "The exact name of the realm to delete. Required. "
        "Another realm than the target's managed realm is refused."
    )


@pytest.mark.asyncio
async def test_with_the_switch_off_a_write_answer_is_as_before(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYCLOAK_ANSWER_REALM", "false")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, _fenced_params(_CREATE, _REALM), approved=True)
    assert result.status == "ok", result.error
    assert REALM_FIELD not in result.result


# ---------------------------------------------------------------------------
# The approval card shows the realm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_UPDATE, _CREATE])
async def test_the_approval_card_of_a_write_shows_the_realm(operator: Operator, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(return_value=httpx.Response(200, json={"realm": _REALM}))
        result = await _dispatch(operator, op_id, _fenced_params(op_id, _REALM), approved=False)

    assert result.status == "awaiting_approval", result.error
    rows = await _approval_rows()
    assert len(rows) == 1
    assert rows[0].proposed_effect["target_realm"] == _REALM


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_the_approval_tools_give_the_realm_only_with_the_switch_on(
    operator: Operator, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """``meho_approvals_get`` and ``meho_approvals_list``; the stored card keeps the realm."""
    monkeypatch.setenv("KEYCLOAK_ANSWER_REALM", "true" if on else "false")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(return_value=httpx.Response(200, json={"realm": _REALM}))
        result = await _dispatch(operator, _CREATE, _fenced_params(_CREATE, _REALM), approved=False)
    assert result.status == "awaiting_approval", result.error
    (row,) = await _approval_rows()

    got = await _get_handler(operator, {"approval_request_id": str(row.id)})
    listed = await _list_handler(operator, {"status": "pending"})
    for effect in (got["proposed_effect"], listed["items"][0]["proposed_effect"]):
        assert effect.get("target_realm") == (_REALM if on else None)
        assert ("target_realm" in effect) is on
        assert {**effect, "target_realm": _REALM} == {
            **without_internal_effect_keys(row.proposed_effect),
            **({"preview": effect["preview"]} if "preview" in effect else {}),
        }
    # A person reads it in both states; an agent login on REST as in the MCP tools.
    assert _proposed_effect_for(operator, row)["target_realm"] == _REALM
    agent = operator.model_copy(update={"principal_kind": PrincipalKind.AGENT})
    assert ("target_realm" in _proposed_effect_for(agent, row)) is on
