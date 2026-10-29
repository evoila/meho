# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test one Keycloak login: ``keycloak.user.check_login``.

The op runs through the real dispatcher against recorded Keycloak answers
(respx) and a fake Vault. No Keycloak and no Vault server is used.

What is proven:

* the op registers on the ``caution`` risk level with approval, in its own
  group, and classifies as ``credential_write``;
* an accepted login sends one token request with the password grant, then
  ends the new session with the refresh token;
* a wrong password answers ``refused`` / 401 / ``invalid_grant`` and is not
  retried;
* a disabled user answers ``refused`` / 400 / ``Account disabled``;
* a client without direct access grants answers ``error``;
* a Vault secret without the password key is an error, and no login is tried;
* the password and the tokens appear in no result, approval row, audit row,
  broadcast view, flight-recorder span or log line.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_login_check import LOGIN_CHECK_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._vault_fakes import install_fake_client

_OP_ID = "keycloak.user.check_login"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-check-login"
_KC_HOST = "keycloak-check-login.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "master"
_TOKEN_PATH = f"/realms/{_REALM}/protocol/openid-connect/token"
_LOGOUT_PATH = f"/realms/{_REALM}/protocol/openid-connect/logout"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005229")

#: Made-up values. The test fails if any of them shows up outside the
#: requests sent to Keycloak.
_PASSWORD = "Old-canary-pw-5f1e-must-not-leak"
_ACCESS_TOKEN = "access-canary-token-91ab-must-not-leak"
_REFRESH_TOKEN = "refresh-canary-token-37cd-must-not-leak"
_SECRETS_THAT_MUST_NOT_LEAK = (_PASSWORD, _ACCESS_TOKEN, _REFRESH_TOKEN)

_PASSWORD_REF = "keycloak/master/admin-old"
_NO_KEY_REF = "keycloak/master/no-password-key"
_PARAMS: dict[str, Any] = {
    "realm": _REALM,
    "username": "admin",
    "password_secret_ref": _PASSWORD_REF,
}

#: Recorded Keycloak 26 token-endpoint answers.
_ACCEPTED = {
    "access_token": _ACCESS_TOKEN,
    "expires_in": 60,
    "refresh_expires_in": 1800,
    "refresh_token": _REFRESH_TOKEN,
    "token_type": "Bearer",
    "not-before-policy": 0,
    "session_state": "5f3c1a7e-0000-4000-8000-000000000001",
    "scope": "profile email",
}
_WRONG_PASSWORD = {"error": "invalid_grant", "error_description": "Invalid user credentials"}
_DISABLED_USER = {"error": "invalid_grant", "error_description": "Account disabled"}
_NO_DIRECT_GRANTS = {
    "error": "unauthorized_client",
    "error_description": "Client not allowed for direct access grants",
}


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=f"Person {sub}",
        email=None,
        raw_jwt=f"{sub}.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def vault_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fake Vault: the password at one path, a secret without it at another."""
    fake = install_fake_client(monkeypatch, secret={})
    secrets = {
        _PASSWORD_REF: {"password": _PASSWORD},
        _NO_KEY_REF: {"note": "no password here"},
    }
    reads: list[str] = []

    def _read(path: str, mount_point: str = "secret", **_: Any) -> dict[str, Any]:
        reads.append(path)
        return {"data": {"data": secrets[path], "metadata": {"version": 1, "path": path}}}

    monkeypatch.setattr(fake.secrets.kv.v2, "read_secret_version", _read)
    return reads


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Switch the flight recorder on and keep each trace's spans in memory."""
    kept: list[Any] = []

    async def _record_trace(**kwargs: Any) -> None:
        kept.extend(kwargs["spans"])

    monkeypatch.setattr(capture, "should_capture", AsyncMock(return_value=True))
    monkeypatch.setattr(capture, "record_trace", _record_trace)
    return kept


@pytest.fixture
async def kc(vault_reads: list[str]) -> AsyncIterator[KeycloakConnector]:
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
                extras={"managed_realm": "meho", "admin_realm": "master"},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_check_login",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(params: dict[str, Any]) -> Any:
    """Run the op as an already-approved call (the handler path)."""
    return await dispatch(
        operator=_person("kc-check-approver"),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=await _target(),
        params=params,
        _approved=True,
    )


def _form(route: respx.Route) -> dict[str, list[str]]:
    return parse_qs(route.calls.last.request.content.decode())


def _assert_nothing_leaks(label: str, blob: str) -> None:
    for secret in _SECRETS_THAT_MUST_NOT_LEAK:
        assert secret not in blob, f"a password or token appears in {label}"


async def _audit_rows_json() -> str:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert rows, "the dispatch wrote no audit rows"
    return json.dumps(
        [
            {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns}
            for row in rows
        ],
        default=str,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registers_caution_with_approval(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID)
            )
        ).scalar_one()
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    (op,) = LOGIN_CHECK_OPS
    assert op.group_key == "user_login_check"
    assert op.llm_instructions is not None
    assert "direct access grants" in op.llm_instructions["preconditions"]
    assert "lockout" in op.description


def test_classifies_credential_write_and_broadcast_is_aggregate_only() -> None:
    assert classify_op(_OP_ID) == "credential_write"
    payload = redact_payload("credential_write", dict(_PARAMS), "ok")
    assert payload == {"op_class": "credential_write", "result_status": "ok"}


# ---------------------------------------------------------------------------
# The answers
# ---------------------------------------------------------------------------


async def test_accepted_login_ends_the_session_at_once(
    kc: KeycloakConnector, vault_reads: list[str]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH).respond(200, json=_ACCEPTED)
        logout = mock.post(_LOGOUT_PATH).respond(204)
        result = await _run(_PARAMS)
    assert result.status == "ok", result.error
    assert result.result == {
        "result": "accepted",
        "http_status": 200,
        "error": None,
        "error_description": None,
        "session_ended": True,
        "realm": _REALM,
        "username": "admin",
        "client_id": "admin-cli",
    }
    assert vault_reads == [_PASSWORD_REF]
    assert token.call_count == 1
    assert _form(token) == {
        "grant_type": ["password"],
        "client_id": ["admin-cli"],
        "username": ["admin"],
        "password": [_PASSWORD],
    }
    assert "authorization" not in token.calls.last.request.headers
    assert logout.call_count == 1
    assert _form(logout) == {"client_id": ["admin-cli"], "refresh_token": [_REFRESH_TOKEN]}


async def test_wrong_password_is_refused_and_not_retried(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH).respond(401, json=_WRONG_PASSWORD)
        logout = mock.post(_LOGOUT_PATH).respond(204)
        result = await _run(_PARAMS)
    assert result.status == "ok", result.error
    assert result.result["result"] == "refused"
    assert result.result["http_status"] == 401
    assert result.result["error"] == "invalid_grant"
    assert result.result["error_description"] == "Invalid user credentials"
    assert result.result["session_ended"] is None
    assert token.call_count == 1
    assert not logout.called


async def test_disabled_user_is_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH).respond(400, json=_DISABLED_USER)
        result = await _run(_PARAMS)
    assert result.status == "ok", result.error
    assert result.result["result"] == "refused"
    assert result.result["http_status"] == 400
    assert result.result["error"] == "invalid_grant"
    assert result.result["error_description"] == "Account disabled"
    assert token.call_count == 1


async def test_client_without_direct_grants_is_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH).respond(400, json=_NO_DIRECT_GRANTS)
        result = await _run({**_PARAMS, "client_id": "meho-web"})
    assert result.status == "ok", result.error
    assert result.result["result"] == "error"
    assert result.result["error"] == "unauthorized_client"
    assert result.result["client_id"] == "meho-web"
    assert _form(token)["client_id"] == ["meho-web"]
    assert token.call_count == 1


async def test_realm_defaults_to_the_managed_realm(kc: KeycloakConnector) -> None:
    params = {key: value for key, value in _PARAMS.items() if key != "realm"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post("/realms/meho/protocol/openid-connect/token").respond(
            401, json=_WRONG_PASSWORD
        )
        result = await _run(params)
    assert result.status == "ok", result.error
    assert result.result["realm"] == "meho"
    assert token.call_count == 1


async def test_missing_vault_key_tries_no_login(
    kc: KeycloakConnector, vault_reads: list[str]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        token = mock.post(_TOKEN_PATH).respond(200, json=_ACCEPTED)
        result = await _run({**_PARAMS, "password_secret_ref": _NO_KEY_REF})
    assert result.status != "ok"
    assert "KeycloakPasswordSecretError" in str(result.model_dump(mode="json"))
    assert vault_reads == [_NO_KEY_REF]
    assert not token.called


async def test_inline_password_is_rejected(kc: KeycloakConnector) -> None:
    result = await _run({**_PARAMS, "password": _PASSWORD})
    assert result.status != "ok"
    assert result.extras["error_code"] == "invalid_params"


# ---------------------------------------------------------------------------
# Nothing leaks: approval, audit, broadcast, flight recorder, logs
# ---------------------------------------------------------------------------


async def test_password_and_tokens_leak_nowhere(
    kc: KeycloakConnector,
    spans: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    requester, approver = _person("kc-check-requester"), _person("kc-check-approver")
    caplog.set_level(logging.DEBUG)
    with (
        capture_logs() as logs,
        respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock,
    ):
        token = mock.post(_TOKEN_PATH).respond(200, json=_ACCEPTED)
        logout = mock.post(_LOGOUT_PATH).respond(204)

        parked = await dispatch(
            operator=requester,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=await _target(),
            params=dict(_PARAMS),
        )
        assert parked.status == "awaiting_approval", parked.error
        assert not token.called  # nothing runs before approval
        request_id = uuid.UUID(parked.extras["approval_request_id"])

        async with get_sessionmaker()() as session:
            pending = await session.get(ApprovalRequest, request_id)
            assert pending is not None
            assert pending.params is None  # kept in the encrypted handoff store
            preview = pending.proposed_effect["preview"]
            assert isinstance(preview, dict)
            assert preview["username"] == "admin"
            assert preview["realm"] == _REALM
            assert preview["password_secret_ref"] == _PASSWORD_REF

        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=approver)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            result = await resume_dispatch_after_approval(operator=approver, request=row)

    assert result.status == "ok", result.error
    assert result.result["result"] == "accepted"
    assert result.result["session_ended"] is True
    assert token.call_count == 1
    assert logout.call_count == 1

    _assert_nothing_leaks("the result", result.model_dump_json())
    async with get_sessionmaker()() as session:
        approval = await session.get(ApprovalRequest, request_id)
    assert approval is not None
    _assert_nothing_leaks(
        "the approval request",
        json.dumps(
            {c.name: getattr(approval, c.name) for c in ApprovalRequest.__table__.columns},
            default=str,
        ),
    )
    _assert_nothing_leaks("the audit rows", await _audit_rows_json())
    _assert_nothing_leaks(
        "the broadcast view",
        json.dumps(redact_payload(classify_op(_OP_ID), dict(_PARAMS), "ok")),
    )
    assert [span.name for span in spans if span.span_kind == "typed"] == [_OP_ID]
    # Should a future change record the token call as a vendor-call span, the
    # pin keeps its bodies out of the trace.
    assert classify_body_exclusion(_OP_ID).excluded is True
    _assert_nothing_leaks(
        "the flight-recorder spans",
        json.dumps([dataclasses.asdict(span) for span in spans], default=str),
    )
    _assert_nothing_leaks("the structlog events", json.dumps(logs, default=str))
    _assert_nothing_leaks("the stdlib log records", caplog.text)


async def test_refused_login_leaks_nothing_even_when_keycloak_echoes_the_password(
    kc: KeycloakConnector, spans: list[Any]
) -> None:
    echo = {"error": "invalid_grant", "error_description": f"bad password {_PASSWORD}"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(401, json=echo)
        result = await _run(_PARAMS)
    assert result.status == "ok", result.error
    assert result.result["result"] == "refused"
    assert result.result["error_description"] == "bad password ***"
    _assert_nothing_leaks("the result", result.model_dump_json())
    _assert_nothing_leaks("the audit rows", await _audit_rows_json())
    _assert_nothing_leaks(
        "the flight-recorder spans",
        json.dumps([dataclasses.asdict(span) for span in spans], default=str),
    )
