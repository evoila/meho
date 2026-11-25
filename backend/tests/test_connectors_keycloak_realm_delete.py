# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed Keycloak realm delete (#3886).

Drives ``keycloak.realm.delete`` through the real dispatch stack against a
respx-mocked Keycloak Admin REST API. No running Keycloak and no Vault: the
admin-credential loader is a stub and the connector instance is preseeded
into the dispatcher's cache.

What is proven:

* the op registers on the ``destructive`` risk level, with approval required,
  the ``destructive`` tag, group ``realm_write`` and a required ``realm``;
* a target works on one realm only: a realm other than the target's managed
  realm is refused, also before approval, and nothing is sent to Keycloak;
* the guards (``protected_realm``): master, the target's admin realm, and
  the realm MEHO's own login uses (on every Keycloak target, whatever its
  host); a change of letter case does not skip them;
* the name in Keycloak's answer must be exactly the managed realm, in the
  preview and in the run (``realm_name_mismatch``);
* Keycloak's answer must have the realm's id: without one the delete is
  refused before approval and in the approved run (``realm_id_missing``);
* an agent reads the realm id of the card only while ``KEYCLOAK_ANSWER_REALM``
  is on; the stored card and a person's card always have it;
* a realm that is already gone → ``already_absent: true`` and no DELETE;
  ``deleted: true`` only after a read answers 404; a read that answers with
  something other than a realm object is an error, before and after the
  DELETE;
* the approval card: realm, enabled, user / client / group counts, no secret;
* the approval gate: no preview hash → refused; a protected realm is refused
  before anything waits for approval; a normal delete waits, a second person
  approves, and only then the DELETE runs.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.api.v1.approvals import get_approval_request
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import KeycloakDeleteRefusedError
from meho_backplane.connectors.keycloak.ops_write_delete_schemas import DELETE_WRITE_OPS
from meho_backplane.connectors.keycloak.ops_write_realm_delete import check_realm_guards
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import (
    PreviewContext,
    blast_radius_missing_reason,
    build_proposed_effect,
)
from meho_backplane.operations.approval_object_check import REASON_UNREADABLE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_OP = "keycloak.realm.delete"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-realm-delete-e2e"
_KC_HOST = "keycloak-realm-delete-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_REALM = "master"
_REALM = "throwaway-run-7"
_REALM_PATH = f"/admin/realms/{_REALM}"
_REALM_ID = "4f6c2a1e-0000-0000-0000-00000000d001"

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003886")


def _operator(sub: str = "kc-realm-delete-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Realm Delete Test Operator",
        email=None,
        raw_jwt="<kc-realm-delete-raw-jwt>",
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
    # The backplane's own login uses a realm the tests do not delete.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho-login")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


async def _seed_target() -> None:
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
                notes="seeded by test_connectors_keycloak_realm_delete",
            )
        )
        await session.commit()


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    await _seed_target()
    instance = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = instance
    yield instance
    await instance.aclose()


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(params: dict[str, Any]) -> dict[str, Any]:
    """Run the op as an already-approved call (the handler path)."""
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=await _target(),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


@dataclass
class _FakeDescriptor:
    op_id: str


async def _card(
    params: dict[str, Any], connector: KeycloakConnector | None
) -> dict[str, Any] | None:
    """Build the park-time proposed effect (the approval card)."""
    return await build_proposed_effect(
        PreviewContext(
            descriptor=_FakeDescriptor(op_id=_OP),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_operator(),
            target=await _target(),
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


def _realm_rep(enabled: bool = True, realm_id: str = _REALM_ID) -> dict[str, Any]:
    # smtpServer.password is a secret; the card must never echo it.
    return {
        "id": realm_id,
        "realm": _REALM,
        "enabled": enabled,
        "smtpServer": {"host": "smtp.test", "password": "smtp-secret-value"},
    }


def _mount_counts(mock: respx.MockRouter) -> None:
    mock.get(f"{_REALM_PATH}/users/count").respond(200, json=12)
    mock.get(f"{_REALM_PATH}/groups/count").respond(200, json={"count": 3})
    mock.get(f"{_REALM_PATH}/clients").respond(
        200,
        json=[
            {"id": "c1", "clientId": "account", "secret": "client-secret-value"},
            {"id": "c2", "clientId": "run-app"},
        ],
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_realm_delete_registers_destructive_and_approval_gated(
    kc: KeycloakConnector,
) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert "destructive" in row.tags
    (op,) = [op for op in DELETE_WRITE_OPS if op.op_id == _OP]
    assert op.group_key == "realm_write"
    assert op.parameter_schema["required"] == ["realm"]
    assert "keycloak.realm.update" in op.llm_instructions["when_to_use"]


async def test_realm_delete_without_realm_is_invalid_params(kc: KeycloakConnector) -> None:
    """The realm is required; the managed realm is never used as a default."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        result = await _run({})
    assert result["status"] != "ok"
    assert result["extras"]["error_code"] == "invalid_params"
    assert not del_route.called


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


async def test_realm_delete_reports_deleted_only_after_404(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get(_REALM_PATH).mock(
            side_effect=[httpx.Response(200, json=_realm_rep()), httpx.Response(404)]
        )
        del_route = mock.delete(_REALM_PATH).respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "ok", result
    assert result["result"] == {"realm": _REALM, "deleted": True, "already_absent": False}
    assert del_route.call_count == 1
    assert get_route.call_count == 2  # the read before, then the 404 check


async def test_realm_delete_already_gone_is_success(kc: KeycloakConnector) -> None:
    """Safe to run twice: an absent realm sends no DELETE."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(404)
        del_route = mock.delete(_REALM_PATH).respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "ok", result
    assert result["result"] == {"realm": _REALM, "deleted": False, "already_absent": True}
    assert not del_route.called


async def test_realm_delete_404_on_delete_is_already_absent(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        mock.delete(_REALM_PATH).respond(404)
        result = await _run({"realm": _REALM})
    assert result["status"] == "ok", result
    assert result["result"] == {"realm": _REALM, "deleted": False, "already_absent": True}


async def test_realm_still_readable_after_delete_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        mock.delete(_REALM_PATH).respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteVerificationError"


# A 2xx answer whose body is not a realm object. ``null`` has to be sent as
# raw content: ``httpx.Response(json=None)`` means "no body".
_NOT_A_REALM = [
    pytest.param(httpx.Response(200, json=[]), id="list"),
    pytest.param(httpx.Response(200, json="gone"), id="string"),
    pytest.param(
        httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
        id="null",
    ),
]


@pytest.mark.parametrize("answer", _NOT_A_REALM)
async def test_malformed_read_before_delete_is_an_error(
    kc: KeycloakConnector, answer: httpx.Response
) -> None:
    """A body that is not a realm object is never read as "absent"; no DELETE is sent."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(return_value=answer)
        del_route = mock.delete(_REALM_PATH).respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakUnexpectedResponseError"
    assert not del_route.called


@pytest.mark.parametrize("answer", _NOT_A_REALM)
async def test_malformed_read_after_delete_is_not_deleted_true(
    kc: KeycloakConnector, answer: httpx.Response
) -> None:
    """Only a 404 after the DELETE proves the realm is gone."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get(_REALM_PATH).mock(
            side_effect=[httpx.Response(200, json=_realm_rep()), answer]
        )
        del_route = mock.delete(_REALM_PATH).respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakUnexpectedResponseError"
    assert del_route.call_count == 1
    assert get_route.call_count == 2


#: A read answer whose ``realm`` is not exactly the managed realm, or is missing.
_OTHER_NAMES = [
    pytest.param({**_realm_rep(), "realm": "another-realm"}, id="another-name"),
    pytest.param({**_realm_rep(), "realm": _REALM.upper()}, id="another-case"),
    pytest.param({**_realm_rep(), "realm": "thröwaway-run-7"}, id="another-accent"),
    pytest.param({**_realm_rep(), "realm": f"{_REALM} "}, id="a-space-at-the-end"),
    pytest.param({**_realm_rep(), "realm": None}, id="null-name"),
    pytest.param({"enabled": True}, id="no-name"),
    pytest.param({}, id="empty"),
]


@pytest.mark.parametrize("rep", _OTHER_NAMES)
async def test_an_answer_that_is_not_the_managed_realm_prevents_the_delete(
    kc: KeycloakConnector, rep: dict[str, Any]
) -> None:
    """The name in Keycloak's answer must be exactly the managed realm; no DELETE is sent."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=rep)
        del_route = mock.delete(url__regex=r".*").respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteRefusedError"
    assert "(realm_name_mismatch)" in result["extras"]["exception_message"]
    assert not del_route.called


@pytest.mark.parametrize("rep", _OTHER_NAMES)
async def test_an_answer_that_is_not_the_managed_realm_gets_no_approval_request(
    kc: KeycloakConnector, rep: dict[str, Any]
) -> None:
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=rep)
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(requester, _args(_REALM))
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "(realm_name_mismatch)" in call["extras"]["reason"]
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_realm_name_is_encoded_in_the_path(kc: KeycloakConnector) -> None:
    """A dot in a realm name stays inside its one path segment."""
    async with get_sessionmaker()() as session:
        row = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
        row.extras = {"managed_realm": "a.b", "admin_realm": _ADMIN_REALM}
        await session.commit()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get_route = mock.get("/admin/realms/a%2Eb").respond(404)
        result = await _run({"realm": "a.b"})
    assert result["status"] == "ok", result
    assert get_route.call_count == 1


@pytest.mark.parametrize("realm", ["a/../b", "..", " Meho ", "name?x=", "name#"])
async def test_realm_delete_refuses_a_realm_that_is_not_a_plain_name(
    kc: KeycloakConnector, realm: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        route = mock.route().respond(200, json={})
        result = await _run({"realm": realm})
    assert result["status"] == "error", result
    assert "plain realm name" in str(result)
    assert not route.called


@pytest.mark.parametrize("realm", [_ADMIN_REALM, "MASTER", "other-realm", _REALM.upper()])
async def test_realm_delete_refuses_another_realm_than_the_managed_realm(
    kc: KeycloakConnector, realm: str
) -> None:
    """An approved call that names another realm sends nothing to Keycloak."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.route().respond(200, json={})
        result = await _run({"realm": realm})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "ValueError"
    assert "Use a target that names that realm" in result["extras"]["exception_message"]
    assert len(mock.calls) == 0


@pytest.mark.parametrize("realm", ["master", "MASTER", "ops-admin", "OPS-ADMIN"])
def test_guards_cover_master_and_a_custom_admin_realm(realm: str) -> None:
    """master stays protected when the target logs in to another admin realm."""
    target = SimpleNamespace(
        host="kc.test.invalid",
        extras={"admin_realm": "ops-admin", "managed_realm": "tenant-a"},
    )
    with pytest.raises(KeycloakDeleteRefusedError, match=r"\(protected_realm\)"):
        check_realm_guards(target, realm)  # type: ignore[arg-type]
    check_realm_guards(target, "tenant-a")  # type: ignore[arg-type]


def test_guards_refuse_a_managed_realm_that_is_also_the_admin_realm() -> None:
    """A target that logs in to the realm it manages can never delete it."""
    target = SimpleNamespace(
        host="kc.test.invalid",
        extras={"admin_realm": "tenant-a", "managed_realm": "tenant-a"},
    )
    with pytest.raises(KeycloakDeleteRefusedError, match=r"\(protected_realm\)"):
        check_realm_guards(target, "tenant-a")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ("KEYCLOAK_ISSUER_URL", f"https://{_KC_HOST.upper()}/realms/{_REALM}"),
        ("KEYCLOAK_ADMIN_URL", f"https://{_KC_HOST}:8443/admin/realms/{_REALM}"),
    ],
)
async def test_realm_delete_refuses_the_backplane_login_realm(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch, env: str, url: str
) -> None:
    """The realm MEHO's own login uses is refused (issuer URL and admin URL)."""
    monkeypatch.setenv(env, url)
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert "(protected_realm)" in result["extras"]["exception_message"]
    assert "MEHO's own login" in result["extras"]["exception_message"]
    assert not del_route.called


async def test_login_realm_is_refused_on_a_target_with_a_different_host(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEHO's own login realm is refused on every Keycloak target, whatever its host."""
    monkeypatch.setenv(
        "KEYCLOAK_ISSUER_URL", f"https://other-keycloak.test/realms/{_REALM.upper()}"
    )
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert "(protected_realm)" in result["extras"]["exception_message"]
    assert "MEHO's own login" in result["extras"]["exception_message"]
    assert not del_route.called


# ---------------------------------------------------------------------------
# The approval card (park-time preview)
# ---------------------------------------------------------------------------


async def test_realm_card_shows_enabled_and_counts_and_no_secret(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep(enabled=False))
        _mount_counts(mock)
        effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"] == {
        "kind": "keycloak_realm",
        "resource": "realm",
        "realm": _REALM,
        "present": True,
        "id": _REALM_ID,
        "enabled": False,
        "users": 12,
        "clients": 2,
        "groups": 3,
    }
    assert block["children"] == [
        {"kind": "users", "count": 12},
        {"kind": "clients", "count": 2},
        {"kind": "groups", "count": 3},
    ]
    assert block["irreversibility"] == "permanent"
    assert blast_radius_missing_reason(effect["preview"]) is None
    assert "secret-value" not in repr(effect)


async def test_realm_card_for_an_absent_realm_says_nothing_to_delete(
    kc: KeycloakConnector,
) -> None:
    """A repeated teardown still gets a valid card, so it can wait for approval."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(404)
        effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"]["present"] is False
    assert block["children"] == []
    assert block["irreversibility"] == "none-object-already-absent"
    assert blast_radius_missing_reason(effect["preview"]) is None


async def test_realm_card_for_a_malformed_read_is_unavailable(kc: KeycloakConnector) -> None:
    """A read that is not a realm object gives no card, so nothing waits for approval."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=[])
        effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "only a 404 counts as absent" in effect["preview_error"]


async def test_realm_card_with_no_clients_reports_zero(kc: KeycloakConnector) -> None:
    """An empty client list is a real count of zero, and the card is valid."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        mock.get(f"{_REALM_PATH}/users/count").respond(200, json=0)
        mock.get(f"{_REALM_PATH}/groups/count").respond(200, json={"count": 0})
        mock.get(f"{_REALM_PATH}/clients").respond(200, json=[])
        effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    block = effect["preview"]["blast_radius"]
    assert block["object"]["clients"] == 0
    assert {"kind": "clients", "count": 0} in block["children"]
    assert blast_radius_missing_reason(effect["preview"]) is None


@pytest.mark.parametrize("body", [{"error": "unknown_error"}, "oops", None, 7])
async def test_realm_card_with_a_malformed_client_list_is_unavailable(
    kc: KeycloakConnector, body: Any
) -> None:
    """A client read that is not a JSON array gives no card, never ``clients: 0``."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        mock.get(f"{_REALM_PATH}/users/count").respond(200, json=12)
        mock.get(f"{_REALM_PATH}/groups/count").respond(200, json={"count": 3})
        mock.get(f"{_REALM_PATH}/clients").respond(200, json=body)
        effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "the client count is unknown" in effect["preview_error"]
    assert "clients" not in effect.get("preview", {})


async def test_realm_card_without_a_live_connector_declines() -> None:
    await _seed_target()
    effect = await _card({"realm": _REALM}, None)
    assert effect is None


async def test_realm_card_refuses_a_protected_realm(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"https://keycloak.test/realms/{_REALM}")
    get_settings.cache_clear()
    effect = await _card({"realm": _REALM}, kc)
    assert effect is not None
    assert effect["preview_unavailable"] is True
    assert "(protected_realm)" in effect["preview_error"]
    reason = blast_radius_missing_reason(effect)
    assert reason is not None
    assert "(protected_realm)" in reason


# ---------------------------------------------------------------------------
# The approval gate (the same meta-tools the REST API and MCP use)
# ---------------------------------------------------------------------------


def _args(realm: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP,
        "target": _TARGET_NAME,
        "params": {"realm": realm},
    }


async def test_realm_delete_without_preview_hash_is_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        call = await call_operation(_operator(), _args(_REALM))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "preview_binding_required"
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_another_realm_is_refused_before_it_waits_for_approval(
    kc: KeycloakConnector,
) -> None:
    """A realm other than the managed realm: no request to Keycloak, nothing stored."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.route().respond(200, json={})
        call = await call_operation(_operator(), _args(_ADMIN_REALM))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "Use a target that names that realm" in str(call)
    assert len(mock.calls) == 0
    assert await _approval_rows() == 0


async def test_protected_realm_is_refused_before_it_waits_for_approval(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"https://keycloak.test/realms/{_REALM}")
    get_settings.cache_clear()
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(requester, _args(_REALM))
        assert preview["status"] == "ok", preview
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "(protected_realm)" in call["extras"]["reason"]
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_malformed_client_list_is_refused_before_it_waits_for_approval(
    kc: KeycloakConnector,
) -> None:
    """When the client count cannot be read, the request never waits for approval."""
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        mock.get(f"{_REALM_PATH}/users/count").respond(200, json=12)
        mock.get(f"{_REALM_PATH}/groups/count").respond(200, json={"count": 3})
        mock.get(f"{_REALM_PATH}/clients").respond(200, json={"error": "unknown_error"})
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(requester, _args(_REALM))
        assert preview["status"] == "ok", preview
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "the client count is unknown" in call["extras"]["reason"]
    assert not del_route.called
    assert await _approval_rows() == 0


async def test_realm_delete_waits_for_a_second_person_then_runs(kc: KeycloakConnector) -> None:
    """Preview → wait for approval → a second person approves → the delete runs once."""
    requester = _operator("kc-realm-delete-requester")
    # A MEHO-SA-Operator may approve another person's high-risk change (#3328).
    approver = _operator("kc-realm-delete-approver").model_copy(
        update={
            "self_approver": True,
            "tenant_role": TenantRole.TENANT_ADMIN,
            "platform_admin": True,
        }
    )
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        # The realm reads of the cards and the handler find the realm; the gone-check after
        # the delete does not.
        mock.get(_REALM_PATH).mock(
            side_effect=lambda _request: (
                httpx.Response(404) if del_route.called else httpx.Response(200, json=_realm_rep())
            )
        )
        _mount_counts(mock)
        del_route = mock.delete(_REALM_PATH).respond(204)

        preview = await preview_operation(requester, _args(_REALM))
        assert preview["status"] == "ok", preview
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
        assert call["status"] == "awaiting_approval", call
        assert not del_route.called  # nothing runs before approval
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            effect = dict(row.proposed_effect)
        assert effect["safety_level"] == "destructive"
        assert effect["blast_radius"]["object"]["users"] == 12
        # The realm in the first line of the card is the realm to delete.
        assert effect["target_realm"] == _REALM
        assert effect["blast_radius"]["object"]["realm"] == _REALM

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
    assert resumed.result == {"realm": _REALM, "deleted": True, "already_absent": False}
    assert del_route.call_count == 1


def _rep_with_id(realm_id: Any) -> dict[str, Any]:
    """The realm with the id *realm_id*; ``...`` leaves the id out."""
    rep = _realm_rep()
    if realm_id is ...:
        del rep["id"]
    else:
        rep["id"] = realm_id
    return rep


_NO_ID = [
    pytest.param(..., id="missing"),
    pytest.param("", id="empty"),
    pytest.param("  ", id="blank"),
    pytest.param(None, id="null"),
    pytest.param(7, id="not-a-string"),
]


@pytest.mark.parametrize("realm_id", _NO_ID)
async def test_a_realm_without_an_id_gets_no_approval_request(
    kc: KeycloakConnector, realm_id: Any
) -> None:
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_rep_with_id(realm_id))
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(requester, _args(_REALM))
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert "(realm_id_missing)" in call["extras"]["reason"]
    assert not del_route.called
    assert await _approval_rows() == 0


@pytest.mark.parametrize("realm_id", _NO_ID)
async def test_the_handler_sends_no_delete_for_a_realm_without_an_id(
    kc: KeycloakConnector, realm_id: Any
) -> None:
    """The handler alone, after every check before it."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_rep_with_id(realm_id))
        del_route = mock.delete(url__regex=r".*").respond(204)
        result = await _run({"realm": _REALM})
    assert result["status"] == "error", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteRefusedError"
    assert not del_route.called


def _approver() -> Operator:
    return _operator("kc-realm-delete-approver").model_copy(
        update={
            "self_approver": True,
            "tenant_role": TenantRole.TENANT_ADMIN,
            "platform_admin": True,
        }
    )


async def _wait_for_approval(requester: Operator) -> uuid.UUID:
    preview = await preview_operation(requester, _args(_REALM))
    call = await call_operation(
        requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
    )
    assert call["status"] == "awaiting_approval", call
    return uuid.UUID(call["extras"]["approval_request_id"])


@pytest.mark.parametrize("realm_id", _NO_ID)
async def test_an_approved_run_that_reads_no_realm_id_sends_no_delete(
    kc: KeycloakConnector, realm_id: Any
) -> None:
    """The card had an id; when the approved call runs, Keycloak answers without one."""
    rep = {"now": _realm_rep()}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(
            side_effect=lambda _request: httpx.Response(200, json=rep["now"])
        )
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        request_id = await _wait_for_approval(_operator())
        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=_approver(), params=None)
            await session.commit()

        rep["now"] = _rep_with_id(realm_id)
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=_approver(), request=row, params=None
            )
    assert resumed.status == "denied", resumed
    assert resumed.extras is not None
    assert resumed.extras["error_code"] == "object_changed_since_approval"
    assert resumed.extras["reason"] == REASON_UNREADABLE
    assert not del_route.called


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_an_agent_reads_the_realm_id_only_with_the_switch_on(
    kc: KeycloakConnector, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    monkeypatch.setenv("KEYCLOAK_ANSWER_REALM", "true" if on else "false")
    get_settings.cache_clear()
    requester = _operator()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).respond(200, json=_realm_rep())
        _mount_counts(mock)
        preview = await preview_operation(requester, _args(_REALM))
        request_id = await _wait_for_approval(requester)
        got = await _get_handler(requester, {"approval_request_id": str(request_id)})
        listed = await _list_handler(requester, {"status": "pending"})

    # The agent's preview never contacts Keycloak, so it has no id in both states.
    assert _REALM_ID not in json.dumps(preview)
    for answer in (got, listed):
        assert (_REALM_ID in json.dumps(answer)) is on
    agent = requester.model_copy(update={"principal_kind": PrincipalKind.AGENT})
    for reader, sees_id in ((agent, on), (requester, True)):
        view = await get_approval_request(request_id, operator=reader)
        assert view.reviewer_context is not None
        assert (_REALM_ID in json.dumps(view.proposed_effect)) is sees_id
        assert (_REALM_ID in json.dumps(view.reviewer_context.blast_radius)) is sees_id
        assert (_REALM_ID in view.model_dump_json()) is sees_id

    # The stored card keeps the id: MEHO compares it before the approved run.
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.proposed_effect["blast_radius"]["object"]["id"] == _REALM_ID
        assert row.proposed_effect["preview"]["blast_radius"]["object"]["id"] == _REALM_ID
        shown = got["proposed_effect"]["blast_radius"]["object"]
        stored = row.proposed_effect["blast_radius"]["object"]
        assert shown == {k: v for k, v in stored.items() if on or k != "id"}


async def test_a_realm_made_again_under_the_same_name_is_not_deleted(
    kc: KeycloakConnector,
) -> None:
    """The realm is deleted and made again while the request waits: the run is refused."""
    requester = _operator("kc-realm-delete-requester")
    approver = _operator("kc-realm-delete-approver").model_copy(
        update={
            "self_approver": True,
            "tenant_role": TenantRole.TENANT_ADMIN,
            "platform_admin": True,
        }
    )
    realm_id = {"now": _REALM_ID}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(
            side_effect=lambda _request: httpx.Response(
                200, json=_realm_rep(realm_id=realm_id["now"])
            )
        )
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)

        preview = await preview_operation(requester, _args(_REALM))
        call = await call_operation(
            requester, {**_args(_REALM), "preview_hash": preview["preview_hash"]}
        )
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            assert row.proposed_effect["blast_radius"]["object"]["id"] == _REALM_ID
            await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()

        realm_id["now"] = "4f6c2a1e-0000-0000-0000-00000000d002"
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "denied", resumed
    assert resumed.extras is not None
    assert resumed.extras["error_code"] == "object_changed_since_approval"
    assert not del_route.called


def _in_the_handler() -> bool:
    """True while the realm delete handler itself runs (not a check before it)."""
    return any(frame.function == "keycloak_realm_delete" for frame in inspect.stack())


@pytest.mark.parametrize("same_id", [True, False], ids=["same-id", "other-id"])
async def test_the_handler_compares_its_realm_id_with_the_approved_id(
    kc: KeycloakConnector, same_id: bool
) -> None:
    """Every check before the run reads the approved id; the handler's own read may not.

    With another id in the handler's read, the call is refused and no DELETE
    is sent. With the same id, the DELETE is sent.
    """
    other_id = "4f6c2a1e-0000-0000-0000-00000000d003"
    handler_reads: list[str] = []

    def _realm(_request: httpx.Request) -> httpx.Response:
        if del_route.called:
            return httpx.Response(404)
        realm_id = _REALM_ID
        if _in_the_handler():
            realm_id = _REALM_ID if same_id else other_id
            handler_reads.append(realm_id)
        return httpx.Response(200, json=_realm_rep(realm_id=realm_id))

    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_REALM_PATH).mock(side_effect=_realm)
        _mount_counts(mock)
        del_route = mock.delete(url__regex=r".*").respond(204)
        request_id = await _wait_for_approval(_operator())
        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=_approver(), params=None)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=_approver(), request=row, params=None
            )

    if same_id:
        assert resumed.status == "ok", resumed
        assert resumed.result == {"realm": _REALM, "deleted": True, "already_absent": False}
        assert del_route.call_count == 1
        return
    assert handler_reads == [other_id]
    assert resumed.status == "error", resumed
    assert resumed.extras is not None
    assert resumed.extras["exception_class"] == "KeycloakDeleteRefusedError"
    assert "(realm_id_changed)" in resumed.extras["exception_message"]
    assert not del_route.called
