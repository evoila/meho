# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The mail server (SMTP) password of ``keycloak.realm.update``.

``keycloak.realm.update`` takes the realm's SMTP password only as a
reference, ``smtp_password_secret_ref``. These tests drive the op through
``dispatch`` against a respx-mocked Keycloak Admin REST API and prove:

* with the reference, MEHO reads the password under the operator's
  identity and the ``PUT`` body carries it as ``smtpServer.password``;
  the result, the audit row and the flight recorder trace do not;
* a call with a password inside ``representation.smtpServer`` is refused
  before it waits for approval: no approval request is stored, nothing is
  sent to Keycloak, and the value is in none of the error, the audit row,
  the flight recorder and the event outbox (the activity feed);
* ``preview_operation`` gives the same refusal;
* a call with only the reference waits for approval, and the stored
  params hold the reference, never the password;
* the handler refuses an inline password too, and a stored secret
  without a usable value fails before anything is sent.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector  # also loads the registry
from meho_backplane.connectors.keycloak.ops_write_realm_smtp import (
    _realm_update_preview,
    has_inline_smtp_password,
    with_smtp_password,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    DispatchTrace,
    DispatchTraceSpan,
    EventOutbox,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._park_check import park_check_registered
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload, consume
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_OP = "keycloak.realm.update"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "keycloak-realm-smtp"
_KC_HOST = "keycloak-realm-smtp.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "example-realm"
_REALM_PATH = f"/admin/realms/{_REALM}"
_SMTP_REF = "<smtp-password-ref>"

#: A made-up password. It must never show up in any output.
_SMTP_PASSWORD = "made-up-smtp-password-0815-abcdef"

_REALM_BEFORE: dict[str, Any] = {
    "id": "realm-uuid-smtp",
    "realm": _REALM,
    "enabled": True,
    "smtpServer": {"host": "old-mail.example.test", "port": "25"},
}

_SMTP_SETTINGS: dict[str, str] = {
    "host": "mail.example.test",
    "port": "587",
    "from": "keycloak@example.test",
    "auth": "true",
    "user": "keycloak",
    "starttls": "true",
}

#: Every credential-backend read: (param, mount, secret_ref).
_SECRET_READS: list[tuple[str, str, str | None]] = []


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="admin-cli", client_secret="stub-secret")

    return _load()


async def _fake_load_param_secret_data(
    target: Any, operator: Operator, *, param: str, mount: str
) -> dict[str, Any]:
    """Stand in for the credential backend: one stored SMTP password."""
    _SECRET_READS.append((param, mount, target.secret_ref))
    return {"password": f"  {_SMTP_PASSWORD}\n", "other": "not-the-password"}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    _SECRET_READS.clear()
    yield
    reset_dispatcher_caches()
    _SECRET_READS.clear()


@pytest.fixture
async def operator(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Operator]:
    """Register the ops, seed a tenant with the recorder on and a Keycloak target."""
    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.ops_write.load_param_secret_data",
        _fake_load_param_secret_data,
    )
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"kc-smtp-{tenant_id.hex[:8]}",
                name="Keycloak realm SMTP",
                flight_recorder_enabled=True,
            )
        )
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
                notes="seeded by test_connectors_keycloak_realm_smtp_password",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector  # type: ignore[assignment]
    yield Operator(
        sub="keycloak-realm-smtp-operator",
        name="Keycloak realm SMTP operator",
        email=None,
        raw_jwt="<keycloak-realm-smtp-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )
    await connector.aclose()


async def _dispatch(operator: Operator, params: dict[str, Any], *, approved: bool) -> Any:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, operator.tenant_id, _TARGET_NAME)
    return await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params=params,
        _approved=approved,
    )


def _mount_keycloak(mock: respx.MockRouter, after: dict[str, Any]) -> tuple[Any, Any]:
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token-smtp", "expires_in": 300}
    )
    get_route = mock.get(_REALM_PATH).mock(
        side_effect=[
            httpx.Response(200, json=_REALM_BEFORE),
            httpx.Response(200, json=after),
        ]
    )
    put_route = mock.put(_REALM_PATH).respond(204)
    return get_route, put_route


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


def _row_text(row: Any) -> str:
    return repr({c.key: getattr(row, c.key) for c in row.__table__.columns})


async def _assert_password_stored_nowhere() -> None:
    for model in (
        ApprovalRequest,
        ApprovalExecutionPayload,
        AuditLog,
        DispatchTrace,
        DispatchTraceSpan,
        EventOutbox,
    ):
        for row in await _rows(model):
            assert _SMTP_PASSWORD not in _row_text(row), model.__name__


def _inline_params() -> dict[str, Any]:
    return {"representation": {"smtpServer": {**_SMTP_SETTINGS, "password": _SMTP_PASSWORD}}}


def _ref_params() -> dict[str, Any]:
    return {
        "representation": {"smtpServer": dict(_SMTP_SETTINGS)},
        "smtp_password_secret_ref": _SMTP_REF,
        "smtp_password_secret_mount": "kv-smtp",
    }


# ---------------------------------------------------------------------------
# The reference sets the password
# ---------------------------------------------------------------------------


def test_the_check_before_approval_is_registered() -> None:
    assert park_check_registered(_OP)


@pytest.mark.asyncio
async def test_reference_sets_the_smtp_password_in_the_put_body(operator: Operator) -> None:
    after = copy.deepcopy(_REALM_BEFORE)
    after["smtpServer"] = {**_SMTP_SETTINGS, "password": "**********"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        get_route, put_route = _mount_keycloak(mock, after)
        result = await _dispatch(operator, _ref_params(), approved=True)

    assert result.status == "ok", result.error
    assert _SECRET_READS == [("smtp_password_secret_ref", "kv-smtp", _SMTP_REF)]
    assert get_route.call_count == 2
    body = json.loads(put_route.calls.last.request.content)
    assert body["smtpServer"] == {**_SMTP_SETTINGS, "password": _SMTP_PASSWORD}
    assert body["enabled"] is True
    assert result.result["changed_fields"] == ["smtpServer"]
    assert result.result["unrequested_changes"] == []
    assert _SMTP_PASSWORD not in result.model_dump_json()
    # The recorder is on, so the call left a trace to search.
    assert [span for span in await _rows(DispatchTraceSpan) if span.span_kind == "vendor_call"]
    await _assert_password_stored_nowhere()


@pytest.mark.asyncio
async def test_reference_without_smtp_settings_adds_only_the_password(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(
            operator,
            {"representation": {}, "smtp_password_secret_ref": _SMTP_REF},
            approved=True,
        )

    assert result.status == "ok", result.error
    assert _SECRET_READS == [("smtp_password_secret_ref", "secret", _SMTP_REF)]
    body = json.loads(put_route.calls.last.request.content)
    assert body["smtpServer"] == {**_REALM_BEFORE["smtpServer"], "password": _SMTP_PASSWORD}


@pytest.mark.asyncio
async def test_update_without_reference_reads_no_secret(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, {"representation": {"enabled": True}}, approved=True)

    assert result.status == "ok", result.error
    assert _SECRET_READS == []
    body = json.loads(put_route.calls.last.request.content)
    assert body["smtpServer"] == _REALM_BEFORE["smtpServer"]


@pytest.mark.asyncio
async def test_stored_secret_without_the_key_fails_before_the_put(operator: Operator) -> None:
    params = {**_ref_params(), "smtp_password_secret_key": "missing"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, params, approved=True)

    assert result.status == "error"
    assert "KeycloakPasswordSecretError" in (result.error or "")
    assert put_route.call_count == 0
    assert _SMTP_PASSWORD not in result.model_dump_json()


@pytest.mark.asyncio
async def test_smtp_server_that_is_not_an_object_fails_before_the_put(operator: Operator) -> None:
    params = {
        "representation": {"smtpServer": "mail.example.test"},
        "smtp_password_secret_ref": _SMTP_REF,
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, params, approved=True)

    assert result.status == "error"
    assert put_route.call_count == 0
    assert _SMTP_PASSWORD not in result.model_dump_json()


# ---------------------------------------------------------------------------
# An inline password is refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["password", "Password", "PASSWORD"])
async def test_inline_password_is_refused_before_it_waits(operator: Operator, key: str) -> None:
    params = {"representation": {"smtpServer": {**_SMTP_SETTINGS, key: _SMTP_PASSWORD}}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        get_route, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, params, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "refused_before_approval"
    assert "smtp_password_secret_ref" in (result.error or "")
    assert _SMTP_PASSWORD not in result.model_dump_json()
    assert get_route.call_count == 0
    assert put_route.call_count == 0
    assert await _rows(ApprovalRequest) == []
    audit = await _rows(AuditLog)
    assert [row.payload["result_status"] for row in audit] == ["denied"]
    await _assert_password_stored_nowhere()


@pytest.mark.asyncio
async def test_inline_password_with_a_reference_is_refused_too(operator: Operator) -> None:
    params = {**_inline_params(), "smtp_password_secret_ref": _SMTP_REF}
    result = await _dispatch(operator, params, approved=False)

    assert result.extras["error_code"] == "refused_before_approval"
    assert _SECRET_READS == []
    assert await _rows(ApprovalRequest) == []
    await _assert_password_stored_nowhere()


@pytest.mark.asyncio
async def test_preview_refuses_an_inline_password_the_same_way(operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, operator.tenant_id, _TARGET_NAME)
    envelope = await preview_dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=target,
        params=_inline_params(),
    )

    assert envelope["extras"]["error_code"] == "refused_before_approval"
    assert "smtp_password_secret_ref" in envelope["error"]
    assert _SMTP_PASSWORD not in json.dumps(envelope, default=str)
    assert await _rows(ApprovalRequest) == []


@pytest.mark.asyncio
async def test_handler_refuses_an_inline_password(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        get_route, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, _inline_params(), approved=True)

    assert result.status == "error"
    assert get_route.call_count == 0
    assert put_route.call_count == 0
    assert _SMTP_PASSWORD not in result.model_dump_json()
    await _assert_password_stored_nowhere()


# ---------------------------------------------------------------------------
# A waiting request holds only the reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_request_stores_only_the_reference(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, _ref_params(), approved=False)

    assert result.status == "awaiting_approval", result.error
    assert put_route.call_count == 0
    assert _SECRET_READS == []
    requests = await _rows(ApprovalRequest)
    assert len(requests) == 1
    # A credential write keeps the params of a waiting request only in the
    # encrypted handoff store, not on the approval row.
    assert requests[0].params is None
    async with get_sessionmaker()() as session:
        payload = (await session.execute(select(ApprovalExecutionPayload))).scalar_one()
        stored = await consume(session, payload.handle, requests[0].id, operator.tenant_id)
    assert stored["params"] == _ref_params()
    # The approver sees the realm, the requested fields and the password source.
    effect = json.dumps(requests[0].proposed_effect)
    assert '"realm": "example-realm"' in effect
    assert "mail.example.test" in effect
    assert '"smtp_password_secret_ref": "<smtp-password-ref>"' in effect
    await _assert_password_stored_nowhere()


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"representation": None},
        {"representation": "text"},
        {"representation": {}},
        {"representation": {"smtpServer": None}},
        {"representation": {"smtpServer": "mail.example.test"}},
        {"representation": {"smtpServer": {"host": "mail.example.test"}}},
        {"representation": {"password": "top-level-is-not-smtp"}},
    ],
)
def test_no_inline_smtp_password(params: dict[str, Any]) -> None:
    assert has_inline_smtp_password(params) is False


@dataclass
class _FakeDescriptor:
    op_id: str


def _card_ctx(params: dict[str, Any]) -> PreviewContext:
    """A preview context with an operator and a descriptor, as the dispatcher builds it."""
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=_OP),  # type: ignore[arg-type]
        connector_instance=None,
        operator=Operator(
            sub="keycloak-realm-smtp-card",
            name="Keycloak realm SMTP card",
            email=None,
            raw_jwt="<keycloak-realm-smtp-card-raw-jwt>",
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000935"),
            tenant_role=TenantRole.OPERATOR,
        ),
        target=None,
        params=params,
        connector_id=_CONNECTOR_ID,
    )


@pytest.mark.asyncio
async def test_approval_card_hides_a_password_value() -> None:
    ctx = _card_ctx({**_inline_params(), "realm": "other-realm"})
    card = await _realm_update_preview(ctx)

    assert card is not None
    assert card["realm"] == "other-realm"
    assert card["representation"]["smtpServer"]["host"] == "mail.example.test"
    assert _SMTP_PASSWORD not in json.dumps(card)
    assert "smtp_password_source" not in card


#: Made-up values under secret-shaped names that are not in the Keycloak
#: list of secret fields. The card must hide each of them.
_NAMED_SECRETS: dict[str, str] = {
    "authTokenClientSecret": "made-up-token-client-secret-4711",
    "clientSecret": "made-up-idp-client-secret-4712",
    "someApiKey": "made-up-api-key-4713",
}


def _named_secret_params() -> dict[str, Any]:
    params = _ref_params()
    params["representation"] = {
        "smtpServer": {
            **_SMTP_SETTINGS,
            "authTokenClientSecret": _NAMED_SECRETS["authTokenClientSecret"],
        },
        "identityProviders": [
            {"alias": "corp-idp", "config": {"clientSecret": _NAMED_SECRETS["clientSecret"]}},
        ],
        "attributes": {"someApiKey": _NAMED_SECRETS["someApiKey"], "frontendUrl": "x"},
    }
    return params


@pytest.mark.asyncio
async def test_approval_card_hides_every_secret_named_value() -> None:
    ctx = _card_ctx(_named_secret_params())
    card = await _realm_update_preview(ctx)

    assert card is not None
    shown = card["representation"]
    assert shown["smtpServer"]["authTokenClientSecret"] == "***REDACTED***"
    assert shown["identityProviders"][0]["config"]["clientSecret"] == "***REDACTED***"
    assert shown["attributes"]["someApiKey"] == "***REDACTED***"
    # Plain settings stay readable for the approver.
    assert shown["smtpServer"]["host"] == "mail.example.test"
    assert shown["identityProviders"][0]["alias"] == "corp-idp"
    assert shown["attributes"]["frontendUrl"] == "x"
    text = json.dumps(card)
    for value in _NAMED_SECRETS.values():
        assert value not in text


@pytest.mark.asyncio
async def test_waiting_request_card_holds_no_secret_named_value(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, _named_secret_params(), approved=False)

    assert result.status == "awaiting_approval", result.error
    assert put_route.call_count == 0
    requests = await _rows(ApprovalRequest)
    assert len(requests) == 1
    effect = json.dumps(requests[0].proposed_effect)
    assert "mail.example.test" in effect
    row_text = _row_text(requests[0])
    for value in _NAMED_SECRETS.values():
        assert value not in effect
        assert value not in row_text


#: Made-up secret-shaped text under plain key names. The card must hide
#: each of them, as the generic params echo does.
_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJtYWRlLXVwLXN1YmplY3QiLCJuYW1lIjoibWFkZS11cCJ9"
    ".bWFkZS11cC1zaWduYXR1cmUtdmFsdWUtMDkzNQ"
)
_BEARER = "made-up-bearer-value-a1b2c3d4e5f6g7h8i9"
_TOKEN = "made-up-token-value-z9y8x7w6v5u4t3s2"
_SHAPED_SECRETS = (_JWT, _BEARER, _TOKEN)


def _shaped_secret_params() -> dict[str, Any]:
    params = _ref_params()
    params["representation"] = {
        "smtpServer": dict(_SMTP_SETTINGS),
        "identityProviders": [
            {"alias": "corp-idp", "config": {"note": f"Bearer {_BEARER}"}},
        ],
        "attributes": {"raw": _JWT, "hook": f"token={_TOKEN}", "frontendUrl": "x"},
    }
    return params


@pytest.mark.asyncio
async def test_approval_card_hides_secret_shaped_text_under_plain_keys() -> None:
    card = await _realm_update_preview(_card_ctx(_shaped_secret_params()))

    assert card is not None
    shown = card["representation"]
    assert shown["smtpServer"]["host"] == "mail.example.test"
    assert shown["identityProviders"][0]["alias"] == "corp-idp"
    assert shown["attributes"]["frontendUrl"] == "x"
    text = json.dumps(card)
    for value in _SHAPED_SECRETS:
        assert value not in text


@pytest.mark.asyncio
async def test_waiting_request_card_holds_no_secret_shaped_text(operator: Operator) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put_route = _mount_keycloak(mock, _REALM_BEFORE)
        result = await _dispatch(operator, _shaped_secret_params(), approved=False)

    assert result.status == "awaiting_approval", result.error
    assert put_route.call_count == 0
    requests = await _rows(ApprovalRequest)
    assert len(requests) == 1
    effect = json.dumps(requests[0].proposed_effect)
    assert "mail.example.test" in effect
    row_text = _row_text(requests[0])
    for value in _SHAPED_SECRETS:
        assert value not in effect
        assert value not in row_text


def test_with_smtp_password_keeps_the_other_settings() -> None:
    assert with_smtp_password(None, "pw") == {"password": "pw"}
    assert with_smtp_password({"host": "h"}, "pw") == {"host": "h", "password": "pw"}
    with pytest.raises(ValueError, match="must be an object"):
        with_smtp_password(["host"], "pw")
