# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed Keycloak deletes: user, group and client (#3944).

Drives ``keycloak.user.delete`` / ``keycloak.group.delete`` /
``keycloak.client.delete`` through the real dispatch stack against a
respx-mocked Keycloak Admin REST API. No running Keycloak and no Vault: the
admin-credential loader is a stub and the connector instance is preseeded
into the dispatcher's cache.

What is proven:

* the three ops register on the ``destructive`` risk level with approval
  required and the ``destructive`` tag;
* name lookup (exact username / clientId / group name at every level), and a
  name with more than one match is refused;
* the guards: built-in clients, the target's own login client and user, and
  a group with sub-groups unless ``delete_subgroups`` is true;
* already gone → ``already_absent: true``; ``deleted: true`` only after a
  read answers 404;
* the approval card (preview) for each kind, with no secret in it;
* the approval gate: no preview hash → refused; a guard breach is refused
  before anything waits for approval (the reason names the guard); a normal
  delete waits, a second person approves, and only then the DELETE runs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_delete_schemas import DELETE_WRITE_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakAdminCredentialsLoader,
    KeycloakClientCredentials,
    KeycloakPasswordCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import (
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-delete-e2e"
# A second target whose managed realm IS the realm it logs in to, so the
# "target's own login client / user" guards apply.
_ADMIN_REALM_TARGET_NAME = "kc-delete-e2e-admin-realm"
_KC_HOST = "keycloak-delete-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_LOGIN_CLIENT_ID = "meho-admin"
_LOGIN_SERVICE_ACCOUNT = "service-account-meho-admin"

_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_USER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_GROUP_UUID = "77777777-7777-7777-7777-777777777777"
_OTHER_GROUP_UUID = "88888888-8888-8888-8888-888888888888"
_CLIENT_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003944")

_BUILTIN_CLIENTS = (
    "admin-cli",
    "account",
    "account-console",
    "broker",
    "realm-management",
    "security-admin-console",
)


def _operator(sub: str = "kc-delete-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Delete Test Operator",
        email=None,
        raw_jwt="<kc-delete-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _client_creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id=_LOGIN_CLIENT_ID, client_secret="stub-secret")

    return _load()


def _password_creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakPasswordCredentials(username="break-glass-admin", password="stub-password")

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


async def _seed_target(name: str, managed_realm: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": managed_realm, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_delete_ops",
            )
        )
        await session.commit()


def _wire_connector(loader: KeycloakAdminCredentialsLoader) -> KeycloakConnector:
    instance = KeycloakConnector(credentials_loader=loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    return instance


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_target(_TARGET_NAME, _REALM)
    await _seed_target(_ADMIN_REALM_TARGET_NAME, _ADMIN_REALM)
    connector = _wire_connector(_client_creds_loader)
    yield connector
    await connector.aclose()


async def _target(name: str = _TARGET_NAME) -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, name)


async def _run(op_id: str, params: dict[str, Any], *, target: str = _TARGET_NAME) -> dict[str, Any]:
    """Run an op as an already-approved call (the handler path)."""
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(target),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


@dataclass
class _FakeDescriptor:
    op_id: str


async def _card(
    op_id: str,
    params: dict[str, Any],
    connector: KeycloakConnector | None,
    *,
    target: str = _TARGET_NAME,
) -> dict[str, Any] | None:
    """Build the park-time proposed effect (the approval card) for an op."""
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_operator(),
            target=await _target(target),
            params=params,
            connector_id=_CONNECTOR_ID,
        )
    )


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        count = await session.execute(select(func.count()).select_from(ApprovalRequest))
        return int(count.scalar_one())


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


def _user(username: str = "event-guest", uuid_: str = _USER_UUID) -> dict[str, Any]:
    return {"id": uuid_, "username": username, "enabled": True}


def _group(subgroups: int | None = 0, **extra: Any) -> dict[str, Any]:
    rep: dict[str, Any] = {"id": _GROUP_UUID, "name": "event-team", "path": "/event-team"}
    if subgroups is not None:
        rep["subGroupCount"] = subgroups
    return {**rep, **extra}


def _client(client_id: str = "event-app", **extra: Any) -> dict[str, Any]:
    return {"id": _CLIENT_UUID, "clientId": client_id, "enabled": True, **extra}


def _gone_after_delete(rep: dict[str, Any]) -> list[httpx.Response]:
    """GET answers: the object before the delete, then 404 after it."""
    return [httpx.Response(200, json=rep), httpx.Response(404)]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_delete_ops_register_destructive_and_approval_gated(kc: KeycloakConnector) -> None:
    """All three ops are destructive, need approval, and carry the destructive tag."""
    op_ids = ("keycloak.user.delete", "keycloak.group.delete", "keycloak.client.delete")
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(op_ids))
                )
            )
            .scalars()
            .all()
        )
    assert sorted(row.op_id for row in rows) == sorted(op_ids)
    for row in rows:
        assert row.safety_level == "destructive", row.op_id
        assert row.requires_approval is True, row.op_id
        assert "destructive" in row.tags, row.op_id
    groups = {op.op_id: op.group_key for op in DELETE_WRITE_OPS}
    assert groups == {
        "keycloak.user.delete": "user_write",
        "keycloak.group.delete": "group_write",
        "keycloak.client.delete": "client_write",
    }


def test_delete_ops_classify_as_plain_write() -> None:
    """No secret rides in the params, so the broadcast feed shows them in full."""
    for op_id in ("keycloak.user.delete", "keycloak.group.delete", "keycloak.client.delete"):
        assert classify_op(op_id) == "write"


async def test_delete_without_id_or_name_is_invalid_params(kc: KeycloakConnector) -> None:
    """The schema needs the id or the name; neither is refused before any call."""
    result = await _run("keycloak.user.delete", {})
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# User delete — the handler
# ---------------------------------------------------------------------------


async def test_user_delete_by_id_reports_deleted_only_after_404(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").mock(
            side_effect=_gone_after_delete(_user())
        )
        del_route = mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)
        result = await _run("keycloak.user.delete", {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "realm": _REALM,
        "id": _USER_UUID,
        "username": "event-guest",
        "deleted": True,
        "already_absent": False,
    }
    assert del_route.call_count == 1
    assert get_route.call_count == 2  # the lookup, then the 404 check


async def test_user_delete_by_username_uses_exact_lookup(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        list_route = mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[_user("Event-Guest")]
        )
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(404)
        del_route = mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)
        result = await _run("keycloak.user.delete", {"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"]["deleted"] is True
    assert result["result"]["id"] == _USER_UUID
    query = list_route.calls.last.request.url.params
    assert query["username"] == "event-guest"
    assert query["exact"] == "true"
    assert del_route.call_count == 1


async def test_user_delete_already_gone_by_id(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(404)
        del_route = mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)
        result = await _run("keycloak.user.delete", {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"]["already_absent"] is True
    assert result["result"]["deleted"] is False
    assert not del_route.called


async def test_user_delete_already_gone_by_name(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        result = await _run("keycloak.user.delete", {"username": "event-guest"})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "realm": _REALM,
        "id": None,
        "username": "event-guest",
        "deleted": False,
        "already_absent": True,
    }


async def test_user_delete_404_on_delete_is_already_absent(kc: KeycloakConnector) -> None:
    """Removed by someone else between the lookup and the DELETE."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(200, json=_user())
        mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(404)
        result = await _run("keycloak.user.delete", {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"]["already_absent"] is True
    assert result["result"]["deleted"] is False


async def test_user_delete_still_readable_after_delete_is_an_error(
    kc: KeycloakConnector,
) -> None:
    """deleted=true is never reported while the object can still be read."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(200, json=_user())
        mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)
        result = await _run("keycloak.user.delete", {"id": _USER_UUID})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteVerificationError"


async def test_user_delete_refuses_ambiguous_username(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[_user("event-guest"), _user("EVENT-GUEST", _OTHER_USER_UUID)]
        )
        del_route = mock.delete(url__regex=r".*/users/.*").respond(204)
        result = await _run("keycloak.user.delete", {"username": "event-guest"})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteRefusedError"
    assert "(ambiguous_name)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_user_delete_refuses_target_login_service_account(kc: KeycloakConnector) -> None:
    """With a client secret, the target logs in as the client's service account."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_ADMIN_REALM}/users/{_USER_UUID}").respond(
            200, json=_user(_LOGIN_SERVICE_ACCOUNT)
        )
        del_route = mock.delete(url__regex=r".*/users/.*").respond(204)
        result = await _run(
            "keycloak.user.delete", {"id": _USER_UUID}, target=_ADMIN_REALM_TARGET_NAME
        )
    assert result["status"] == "error", result
    assert "(target_login_user)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_user_delete_refuses_target_password_login_user(kc: KeycloakConnector) -> None:
    """With a username and password, the target logs in as that user."""
    connector = _wire_connector(_password_creds_loader)
    try:
        with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
            _mount_token(mock)
            mock.get(f"/admin/realms/{_ADMIN_REALM}/users").respond(
                200, json=[_user("break-glass-admin")]
            )
            del_route = mock.delete(url__regex=r".*/users/.*").respond(204)
            result = await _run(
                "keycloak.user.delete",
                {"username": "break-glass-admin"},
                target=_ADMIN_REALM_TARGET_NAME,
            )
    finally:
        await connector.aclose()
    assert result["status"] == "error", result
    assert "(target_login_user)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_login_guard_does_not_apply_in_another_realm(kc: KeycloakConnector) -> None:
    """A same-named user in the managed realm is not the target's login user."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").mock(
            side_effect=_gone_after_delete(_user(_LOGIN_SERVICE_ACCOUNT))
        )
        del_route = mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)
        result = await _run("keycloak.user.delete", {"id": _USER_UUID})
    assert result["status"] == "ok", result
    assert result["result"]["deleted"] is True
    assert del_route.call_count == 1


# ---------------------------------------------------------------------------
# Client delete — the handler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", _BUILTIN_CLIENTS)
async def test_client_delete_refuses_builtin_clients(kc: KeycloakConnector, client_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[_client(client_id)])
        del_route = mock.delete(url__regex=r".*/clients/.*").respond(204)
        result = await _run("keycloak.client.delete", {"client_id": client_id})
    assert result["status"] == "error", result
    assert "(builtin_client)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_client_delete_by_id_still_checks_the_builtin_list(kc: KeycloakConnector) -> None:
    """The guard reads the stored clientId, so passing the id does not skip it."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}").respond(
            200, json=_client("realm-management")
        )
        del_route = mock.delete(url__regex=r".*/clients/.*").respond(204)
        result = await _run("keycloak.client.delete", {"id": _CLIENT_UUID})
    assert result["status"] == "error", result
    assert "(builtin_client)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_client_delete_refuses_target_login_client(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_ADMIN_REALM}/clients").respond(
            200, json=[_client(_LOGIN_CLIENT_ID)]
        )
        del_route = mock.delete(url__regex=r".*/clients/.*").respond(204)
        result = await _run(
            "keycloak.client.delete",
            {"client_id": _LOGIN_CLIENT_ID},
            target=_ADMIN_REALM_TARGET_NAME,
        )
    assert result["status"] == "error", result
    assert "(target_login_client)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_client_delete_by_client_id_deletes_and_verifies(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        list_route = mock.get(f"/admin/realms/{_REALM}/clients").respond(
            200, json=[_client("event-app")]
        )
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}").respond(404)
        del_route = mock.delete(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}").respond(204)
        result = await _run("keycloak.client.delete", {"client_id": "event-app"})
    assert result["status"] == "ok", result
    assert result["result"] == {
        "realm": _REALM,
        "id": _CLIENT_UUID,
        "client_id": "event-app",
        "deleted": True,
        "already_absent": False,
    }
    assert list_route.calls.last.request.url.params["clientId"] == "event-app"
    assert del_route.call_count == 1


# ---------------------------------------------------------------------------
# Group delete — the handler
# ---------------------------------------------------------------------------


async def test_group_delete_by_name_searches_every_level(kc: KeycloakConnector) -> None:
    nested = {"id": _GROUP_UUID, "name": "event-team", "path": "/events/event-team"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        search_route = mock.get(f"/admin/realms/{_REALM}/groups").respond(200, json=[nested])
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").mock(
            side_effect=_gone_after_delete({**nested, "subGroupCount": 0})
        )
        del_route = mock.delete(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(204)
        result = await _run("keycloak.group.delete", {"name": "event-team"})
    assert result["status"] == "ok", result
    assert result["result"]["deleted"] is True
    assert result["result"]["path"] == "/events/event-team"
    query = search_route.calls.last.request.url.params
    assert query["search"] == "event-team"
    assert query["exact"] == "true"
    assert query["populateHierarchy"] == "false"
    assert del_route.call_count == 1


async def test_group_delete_refuses_ambiguous_name(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups").respond(
            200,
            json=[
                {"id": _GROUP_UUID, "name": "event-team", "path": "/event-team"},
                {"id": _OTHER_GROUP_UUID, "name": "event-team", "path": "/org/event-team"},
            ],
        )
        del_route = mock.delete(url__regex=r".*/groups/.*").respond(204)
        result = await _run("keycloak.group.delete", {"name": "event-team"})
    assert result["status"] == "error", result
    message = result["extras"]["exception_message"]
    assert "(ambiguous_name)" in message
    assert "/event-team" in message and "/org/event-team" in message
    assert not del_route.called


async def test_group_delete_refuses_group_with_subgroups(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(200, json=_group(2))
        del_route = mock.delete(url__regex=r".*/groups/.*").respond(204)
        result = await _run("keycloak.group.delete", {"id": _GROUP_UUID})
    assert result["status"] == "error", result
    assert "(group_has_subgroups)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_group_delete_with_unknown_subgroup_count_is_refused(
    kc: KeycloakConnector,
) -> None:
    """A missing subGroupCount counts as "has sub-groups" (fail closed)."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(
            200, json=_group(subgroups=None)
        )
        del_route = mock.delete(url__regex=r".*/groups/.*").respond(204)
        result = await _run("keycloak.group.delete", {"id": _GROUP_UUID})
    assert result["status"] == "error", result
    assert "(group_has_subgroups)" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_group_delete_with_subgroups_and_flag_deletes(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").mock(
            side_effect=_gone_after_delete(_group(2))
        )
        del_route = mock.delete(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(204)
        result = await _run("keycloak.group.delete", {"id": _GROUP_UUID, "delete_subgroups": True})
    assert result["status"] == "ok", result
    assert result["result"]["deleted"] is True
    assert del_route.call_count == 1


# ---------------------------------------------------------------------------
# The approval card (park-time preview)
# ---------------------------------------------------------------------------


async def test_user_card_shows_username_enabled_and_groups(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[_user()])
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(
            200,
            json=[
                {"id": _GROUP_UUID, "name": "event-team", "path": "/event-team"},
                {"id": _OTHER_GROUP_UUID, "name": "guests", "path": "/events/guests"},
            ],
        )
        effect = await _card("keycloak.user.delete", {"username": "event-guest"}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"] == {
        "kind": "keycloak_user",
        "username": "event-guest",
        "enabled": True,
        "groups": ["/event-team", "/events/guests"],
        "realm": _REALM,
        "id": _USER_UUID,
        "present": True,
    }
    assert block["children"] == []
    assert block["irreversibility"] == "permanent"
    assert blast_radius_missing_reason(effect["preview"]) is None


async def test_group_card_shows_member_and_subgroup_counts(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(200, json=_group(2))
        members_route = mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}/members").respond(
            200, json=[_user(f"member-{n}") for n in range(3)]
        )
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}/children").respond(
            200,
            json=[
                {"id": "1", "name": "day-1", "path": "/event-team/day-1"},
                {"id": "2", "name": "day-2", "path": "/event-team/day-2"},
            ],
        )
        effect = await _card(
            "keycloak.group.delete", {"id": _GROUP_UUID, "delete_subgroups": True}, kc
        )
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"]["name"] == "event-team"
    assert block["object"]["member_count"] == 3
    assert block["object"]["member_count_capped"] is False
    assert block["object"]["subgroup_count"] == 2
    assert block["children"] == [
        {"kind": "sub_group", "name": "day-1", "path": "/event-team/day-1"},
        {"kind": "sub_group", "name": "day-2", "path": "/event-team/day-2"},
    ]
    assert members_route.calls.last.request.url.params["briefRepresentation"] == "true"


async def test_group_card_caps_the_member_count(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(200, json=_group(0))
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}/members").respond(
            200, json=[{"id": str(n)} for n in range(1001)]
        )
        effect = await _card("keycloak.group.delete", {"id": _GROUP_UUID}, kc)
    assert effect is not None
    obj = effect["preview"]["blast_radius"]["object"]
    assert obj["member_count"] == 1000
    assert obj["member_count_capped"] is True


async def test_client_card_shows_type_and_never_the_secret(kc: KeycloakConnector) -> None:
    rep = _client(
        "event-app", publicClient=False, secret="s3cr3t-value", serviceAccountsEnabled=True
    )
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}").respond(200, json=rep)
        effect = await _card("keycloak.client.delete", {"id": _CLIENT_UUID}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"]["client_id"] == "event-app"
    assert block["object"]["enabled"] is True
    assert block["object"]["client_type"] == "confidential"
    assert block["children"] == [
        {"kind": "service_account_user", "username": "service-account-event-app"}
    ]
    assert "s3cr3t-value" not in json.dumps(effect)


async def test_card_for_an_object_already_gone_says_nothing_to_delete(
    kc: KeycloakConnector,
) -> None:
    """A repeated teardown still gets a valid card, so it can wait for approval."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[])
        effect = await _card("keycloak.client.delete", {"client_id": "event-app"}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"]["present"] is False
    assert block["irreversibility"] == "none-object-already-absent"
    assert blast_radius_missing_reason(effect["preview"]) is None


async def test_card_without_a_live_connector_declines() -> None:
    """The egress-free preview_operation path has no connector: the builder declines."""
    await _seed_target(_TARGET_NAME, _REALM)
    effect = await _card("keycloak.user.delete", {"id": _USER_UUID}, None)
    assert effect is None


async def test_card_refuses_a_guarded_delete(kc: KeycloakConnector) -> None:
    """A guard breach raises in the builder: the card is marked unavailable."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[_client("admin-cli")])
        effect = await _card("keycloak.client.delete", {"client_id": "admin-cli"}, kc)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "(builtin_client)" in effect["preview_error"]
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert "(builtin_client)" in reason


# ---------------------------------------------------------------------------
# The approval gate (the same meta-tools the REST API and MCP use)
# ---------------------------------------------------------------------------


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": _TARGET_NAME, "params": params}


async def test_delete_without_preview_hash_is_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        call = await call_operation(
            _operator(), _args("keycloak.user.delete", {"username": "event-guest"})
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_guard_refuses_before_the_request_waits_for_approval(
    kc: KeycloakConnector,
) -> None:
    args = _args("keycloak.client.delete", {"client_id": "admin-cli"})
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[_client("admin-cli")])
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(requester, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "(builtin_client)" in call["extras"]["reason"]
    assert "admin-cli" in call["extras"]["reason"]
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_user_delete_waits_for_a_second_person_then_runs(kc: KeycloakConnector) -> None:
    """Preview → wait for approval → a second person approves → the delete runs once."""
    args = _args("keycloak.user.delete", {"username": "event-guest"})
    requester = _operator("kc-delete-requester")
    approver = _operator("kc-delete-approver")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[_user()])
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/groups").respond(
            200, json=[{"id": _GROUP_UUID, "name": "event-team", "path": "/event-team"}]
        )
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(404)
        del_route = mock.delete(f"/admin/realms/{_REALM}/users/{_USER_UUID}").respond(204)

        preview = await preview_operation(requester, args)
        assert preview["status"] == "ok", preview
        call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
        assert call["status"] == "awaiting_approval", call
        assert not del_route.called  # nothing runs before approval
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            effect = dict(row.proposed_effect)
        assert effect["safety_level"] == "destructive"
        assert effect["blast_radius"]["object"]["username"] == "event-guest"
        assert effect["blast_radius"]["object"]["groups"] == ["/event-team"]

        # The requester cannot approve their own destructive request.
        async with get_sessionmaker()() as session:
            with pytest.raises(SelfApprovalForbiddenError):
                await approve_request(session, request_id, operator=requester, params=None)

        async with get_sessionmaker()() as session:
            approved = await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        assert approved.status == "approved"

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed
    assert resumed.result == {
        "realm": _REALM,
        "id": _USER_UUID,
        "username": "event-guest",
        "deleted": True,
        "already_absent": False,
    }
    assert del_route.call_count == 1
