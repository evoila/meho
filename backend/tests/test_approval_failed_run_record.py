# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved write that fails when it runs is recorded as a failure (#3864).

A person approves a parked ``keycloak.user.create`` with ``/decide``. MEHO then
runs it. These tests cover the two ways that run can fail:

* MEHO may not read the password ref in Vault.
* The Keycloak service account may not create users (HTTP 403).

In both cases the approve response says ``error``, the run has its own audit
row with ``result_status: error`` under its real op id, the stored result
points at that row, and no user is created.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import hvac.exceptions
import pytest
import requests.exceptions
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import secret_ref_prefix
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_client

_CONNECTOR_ID = "keycloak-admin-26.x"
_OP_ID = "keycloak.user.create"
_TARGET_NAME = "kc-approval-run"
_KC_HOST = "kc-approval-run.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000038a4")
_PASSWORD_REF = "tenants/team-a/new-user-password"
_PARAMS: dict[str, Any] = {
    "representation": {"username": "new-user"},
    "password_secret_ref": _PASSWORD_REF,
}

_REQUESTER = Operator(
    sub="requester",
    name="Requester",
    email=None,
    raw_jwt="requester-jwt",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


def _stub_admin_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture
async def keycloak_target() -> AsyncIterator[Any]:
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
                secret_ref="tenants/team-a/kc-admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_approval_failed_run_record",
            )
        )
        await session.commit()
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    connector = KeycloakConnector(credentials_loader=_stub_admin_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield target
    await connector.aclose()


async def _park(target: Any) -> uuid.UUID:
    parked = await dispatch(
        operator=_REQUESTER,
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=target,
        params=_PARAMS,
    )
    assert parked.status == "awaiting_approval", parked.error
    return uuid.UUID(parked.extras["approval_request_id"])


def _approve(request_id: uuid.UUID, *, users_status: int) -> tuple[dict[str, Any], int]:
    """Approve over ``/decide`` as a second person; return body + user POSTs."""
    key = make_rsa_keypair("kid-3864")
    with respx.mock(assert_all_called=False) as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        mock.post(f"{_KC_BASE_URL}/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        create_user = mock.post(url__regex=rf"^{_KC_BASE_URL}/admin/realms/[^/]+/users$").respond(
            users_status, json={"error": "HTTP 403 Forbidden"}
        )
        token = mint_token(
            key,
            fresh_sign_in=True,
            sub="approver",
            tenant_role=TenantRole.OPERATOR.value,
            tenant_id=str(_TENANT_ID),
        )
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/approvals/{request_id}/decide",
                headers={"Authorization": f"Bearer {token}"},
                json={"decision": "approved"},
            )
    assert response.status_code == 200, response.text
    return response.json(), create_user.call_count


async def _run_rows(request_id: uuid.UUID) -> tuple[ApprovalRequest, list[AuditLog], AuditLog]:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    decide_rows = [row for row in rows if row.path == f"/api/v1/approvals/{request_id}/decide"]
    assert len(decide_rows) == 1
    return request, [row for row in rows if row.method == "DISPATCH"], decide_rows[0]


def _assert_failed_run_recorded(
    request: ApprovalRequest, run_rows: list[AuditLog], *, error_code: str
) -> AuditLog:
    assert len(run_rows) == 1
    run = run_rows[0]
    # The run row names its own op, not the approve route's "approval.decide".
    assert run.path == _OP_ID
    assert run.payload["op_id"] == _OP_ID
    assert run.payload["result_status"] == "error"
    assert run.payload["error"]["error_code"] == error_code
    assert run.payload["approval_request_id"] == str(request.id)
    assert run.parent_audit_id == request.request_audit_id
    assert run.operator_sub == "approver"
    # The stored result of the approved run is an error and points at the row.
    assert request.resume_result is not None
    assert request.resume_result["status"] == "error"
    assert request.resume_result["extras"]["error_code"] == error_code
    assert request.resume_result["audit_id"] == str(run.id)
    return run


async def test_unreadable_password_ref_fails_the_approved_run(
    monkeypatch: pytest.MonkeyPatch, keycloak_target: Any
) -> None:
    async def _vault_denies(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise hvac.exceptions.Forbidden("permission denied")

    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.load_vault_secret_data", _vault_denies
    )
    request_id = await _park(keycloak_target)

    body, user_posts = _approve(request_id, users_status=201)

    assert body["decision"] == "approved"
    assert body["dispatch_status"] == "error"
    assert body["dispatch_error"].startswith("vault_credentials_read_error:")
    assert _PASSWORD_REF not in body["dispatch_error"]
    assert user_posts == 0, "no user may be created when the password cannot be read"

    request, run_rows, decide_row = await _run_rows(request_id)
    run = _assert_failed_run_recorded(request, run_rows, error_code="vault_credentials_read_error")
    assert run.payload["error"] == {
        "error_code": "vault_credentials_read_error",
        "param": "password_secret_ref",
        "secret_ref_prefix": "tenants/…",
        "cause_class": "Forbidden",
    }
    # The route's own row keeps its label once the run is over.
    assert decide_row.payload["op_id"] == "approval.decide"


@pytest.mark.parametrize(
    "read_exc",
    [
        requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='vault.test', port=443): Max retries exceeded "
            f"with url: /v1/secret/data/{_PASSWORD_REF}"
        ),
        requests.exceptions.ReadTimeout(
            f"HTTPSConnectionPool(host='vault.test', port=443): Read timed out "
            f"(url: /v1/secret/data/{_PASSWORD_REF})"
        ),
    ],
    ids=["connection_error", "read_timeout"],
)
async def test_unreachable_vault_read_fails_the_approved_run(
    monkeypatch: pytest.MonkeyPatch, keycloak_target: Any, read_exc: Exception
) -> None:
    """Vault login works, then the secret read itself fails on the wire.

    These are ``requests`` errors, not hvac ones, and their message carries
    the full secret path. The run must still fail with
    ``vault_credentials_read_error`` and never show that path.
    """
    fake = install_fake_client(monkeypatch, read_exc=read_exc)
    request_id = await _park(keycloak_target)

    body, user_posts = _approve(request_id, users_status=201)

    # Login happened and the read was attempted on the password ref.
    assert fake.auth.jwt.login_calls, "the read failure must come after a successful login"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == _PASSWORD_REF
    assert body["dispatch_status"] == "error"
    assert body["dispatch_error"].startswith("vault_credentials_read_error:")
    assert _PASSWORD_REF not in body["dispatch_error"]
    assert user_posts == 0

    request, run_rows, _ = await _run_rows(request_id)
    run = _assert_failed_run_recorded(request, run_rows, error_code="vault_credentials_read_error")
    assert run.payload["error"] == {
        "error_code": "vault_credentials_read_error",
        "param": "password_secret_ref",
        "secret_ref_prefix": "tenants/…",
        "cause_class": type(read_exc).__name__,
    }
    assert _PASSWORD_REF not in str(run.payload)
    assert _PASSWORD_REF not in str(request.resume_result)


async def test_keycloak_403_fails_the_approved_run(
    monkeypatch: pytest.MonkeyPatch, keycloak_target: Any
) -> None:
    async def _vault_password(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"password": "not-a-real-password"}

    monkeypatch.setattr(
        "meho_backplane.connectors._shared.vault_creds.load_vault_secret_data", _vault_password
    )
    request_id = await _park(keycloak_target)

    body, user_posts = _approve(request_id, users_status=403)

    assert body["dispatch_status"] == "error"
    assert body["dispatch_error"].startswith("connector_http_403:")
    assert user_posts == 1

    request, run_rows, _ = await _run_rows(request_id)
    run = _assert_failed_run_recorded(request, run_rows, error_code="connector_http_403")
    assert run.payload["error"]["http_status"] == 403
    assert "not-a-real-password" not in str(run.payload)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("tenants/team-a/new-user-password", "tenants/…"),
        ("vault:tenants/team-a/pw", "vault:tenants/…"),
        ("gsm:proj/secret#password", "gsm:proj/…"),
        ("single-segment", "…"),
        ("  secret/meho/x  ", "secret/…"),
    ],
)
def test_secret_ref_prefix_keeps_only_the_first_segment(ref: str, expected: str) -> None:
    assert secret_ref_prefix(ref) == expected
