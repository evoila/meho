# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Keycloak target works on one realm only, and every answer names it.

* The three ops that take a realm value (``keycloak.realm.update``,
  ``keycloak.realm.create``, ``keycloak.user.check_login``) refuse another
  realm than the managed realm before the call waits for approval. Nothing
  is stored and nothing is sent to Keycloak. The managed realm works.
* The handler refuses it too, for a call that is already approved.
* Each read and write answer has a ``managed_realm`` field.
* The approval card of a write shows the realm.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
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
from meho_backplane.db.models import ApprovalRequest, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.dispatcher import set_default_reducer
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
_CHECK_LOGIN = "keycloak.user.check_login"


def _fenced_params(op_id: str, realm: str) -> dict[str, Any]:
    """Params of a fenced op that name *realm*."""
    if op_id == _UPDATE:
        return {"realm": realm, "representation": {"enabled": True}}
    if op_id == _CREATE:
        return {"representation": {"realm": realm}}
    return {"realm": realm, "username": "someone", "password_secret_ref": "example/ref"}


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="admin-cli", client_secret="stub-secret")

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


def test_the_fenced_ops_are_the_three_that_work_on_the_managed_realm() -> None:
    assert {_UPDATE, _CREATE, _CHECK_LOGIN} == FENCED_OPS


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_UPDATE, _CREATE, _CHECK_LOGIN])
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
@pytest.mark.parametrize("op_id", [_UPDATE, _CREATE, _CHECK_LOGIN])
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
@pytest.mark.parametrize("op_id", [_UPDATE, _CREATE, _CHECK_LOGIN])
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
    for op_id in (_UPDATE, _CREATE, _CHECK_LOGIN):
        assert realm_fence_problem(op_id, _fenced_params(op_id, _REALM), target) is None
        assert realm_fence_problem(op_id, _fenced_params(op_id, _OTHER), target) is not None
    # A call that names no realm uses the managed realm.
    assert realm_fence_problem(_UPDATE, {"representation": {}}, target) is None
    # The delete is not fenced: its realm is the realm to remove.
    assert realm_fence_problem("keycloak.realm.delete", {"realm": _OTHER}, target) is None


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


@pytest.mark.asyncio
async def test_create_of_the_managed_realm_works(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        post_route = mock.post("/admin/realms").respond(201)
        result = await _dispatch(operator, _CREATE, _fenced_params(_CREATE, _REALM), approved=True)

    assert result.status == "ok", result.error
    assert post_route.call_count == 1
    assert result.result[REALM_FIELD] == _REALM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "path", "body"),
    [
        ("keycloak.realm.get", _REALM_PATH, {"realm": _REALM}),
        ("keycloak.user.list", f"{_REALM_PATH}/users", []),
        ("keycloak.client.list", f"{_REALM_PATH}/clients", []),
        ("keycloak.role.list", f"{_REALM_PATH}/roles", []),
        ("keycloak.group.list", f"{_REALM_PATH}/groups", []),
    ],
)
async def test_each_read_answer_names_the_realm(
    operator: Operator, op_id: str, path: str, body: Any
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(path).respond(200, json=body)
        result = await _dispatch(operator, op_id, {}, approved=False)

    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result[REALM_FIELD] == _REALM
    assert next(iter(result.result)) == REALM_FIELD


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
