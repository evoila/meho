# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST tests for ``/api/v1/service-principals/run-approvals`` (#4126).

* Only a service principal asks for a run approval.
* Approve is a person's decision: a machine token gets 403, a person's
  stored token (no fresh approval sign-in) gets 401 with the RFC 9470
  challenge, and the request stays pending. There is no MCP tool.
* Revoke needs a person; end is for the principal that asked.
* Every route is limited to the caller's tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.main import app
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SERVICE_SUB = "svc-sub-4126"
_BASE = "/api/v1/service-principals/run-approvals"
_BODY: dict[str, Any] = {
    "work_ref": "run-0001",
    "entries": [{"connector_id": "vmware-rest-9.0", "op_id": "POST:/vcenter/vm"}],
    "reason": "build one VM",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("FRESH_SIGN_IN_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


_DANGEROUS_OP = "POST:/vcenter/vm/{vm}/power?action=reset"


@pytest.fixture(autouse=True)
async def _descriptors(_settings_env: None) -> None:
    """The operations the tests list; approve reads each one's risk level."""
    async with get_sessionmaker()() as session:
        for op_id, level in (("POST:/vcenter/vm", "caution"), (_DANGEROUS_OP, "dangerous")):
            session.add(
                EndpointDescriptor(
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    method="POST",
                    source_kind="ingested",
                    safety_level=level,
                    requires_approval=False,
                    parameter_schema={},
                    tenant_id=None,
                )
            )
        await session.commit()


@pytest.fixture
def key() -> Any:
    return make_rsa_keypair("kid-run-approvals")


@pytest.fixture
def client(key: Any) -> Iterator[TestClient]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        yield TestClient(app)


def _service(key: Any, *, tenant: uuid.UUID = _TENANT_A, sub: str = _SERVICE_SUB) -> dict[str, str]:
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tenant),
        principal_kind="service",
    )
    return {"Authorization": f"Bearer {token}"}


def _agent(key: Any) -> dict[str, str]:
    token = mint_token(
        key,
        sub="agent-sub",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT_A),
        principal_kind="agent",
    )
    return {"Authorization": f"Bearer {token}"}


def _person(
    key: Any,
    *,
    tenant: uuid.UUID = _TENANT_A,
    fresh: bool = True,
    role: TenantRole = TenantRole.OPERATOR,
) -> dict[str, str]:
    token = mint_token(
        key,
        sub="person-approver",
        tenant_role=role.value,
        tenant_id=str(tenant),
        fresh_sign_in=fresh,
    )
    return {"Authorization": f"Bearer {token}"}


def _request(client: TestClient, key: Any) -> str:
    response = client.post(_BASE, headers=_service(key), json=_BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["principal_sub"] == _SERVICE_SUB
    return str(body["id"])


def _status(client: TestClient, key: Any, run_approval_id: str) -> str:
    response = client.get(f"{_BASE}/{run_approval_id}", headers=_service(key))
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


def test_service_principal_asks_and_a_person_approves(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)

    response = client.post(f"{_BASE}/{run_approval_id}/approve", headers=_person(key))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert response.json()["approved_by_sub"] == "person-approver"
    # The add-on reads the result while it waits.
    assert _status(client, key, run_approval_id) == "approved"
    listed = client.get(_BASE, params={"status": "approved"}, headers=_person(key))
    assert [item["id"] for item in listed.json()["run_approvals"]] == [run_approval_id]


@pytest.mark.asyncio
async def test_approve_writes_an_audit_row_with_the_sign_in(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)
    response = client.post(f"{_BASE}/{run_approval_id}/approve", headers=_person(key))
    assert response.status_code == 200, response.text

    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(AuditLog.operator_sub == "person-approver")
            )
        ).all()
    approve_rows = [r for r in rows if r.payload.get("op_id") == "run_approval.approve"]
    assert len(approve_rows) == 1
    assert approve_rows[0].payload["run_approval_id"] == run_approval_id
    assert approve_rows[0].payload["approval_auth"]["channel"] == "rest"


def test_list_with_a_dangerous_op_needs_the_high_risk_group(client: TestClient, key: Any) -> None:
    body = {
        **_BODY,
        "entries": [*_BODY["entries"], {"connector_id": "vmware-rest-9.0", "op_id": _DANGEROUS_OP}],
    }
    created = client.post(_BASE, headers=_service(key), json=body)
    assert created.status_code == 201, created.text
    run_approval_id = created.json()["id"]
    url = f"{_BASE}/{run_approval_id}/approve"

    denied = client.post(url, headers=_person(key))
    assert denied.status_code == 403
    assert denied.json()["detail"].startswith("approval_group_required")
    assert _status(client, key, run_approval_id) == "pending"

    allowed = client.post(url, headers=_person(key, role=TenantRole.TENANT_ADMIN))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "approved"


def test_person_cannot_ask_for_a_run_approval(client: TestClient, key: Any) -> None:
    response = client.post(_BASE, headers=_person(key), json=_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": "service_principal_required"}


@pytest.mark.parametrize("who", ["service", "agent"])
def test_token_of_a_machine_cannot_approve(client: TestClient, key: Any, who: str) -> None:
    run_approval_id = _request(client, key)
    headers = _service(key) if who == "service" else _agent(key)

    response = client.post(f"{_BASE}/{run_approval_id}/approve", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"].startswith("human_principal_required")
    assert _status(client, key, run_approval_id) == "pending"


def test_stored_token_of_a_person_alone_cannot_approve(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)

    response = client.post(f"{_BASE}/{run_approval_id}/approve", headers=_person(key, fresh=False))

    assert response.status_code == 401
    assert "insufficient_user_authentication" in response.headers["WWW-Authenticate"]
    assert _status(client, key, run_approval_id) == "pending"


def test_no_mcp_tool_reaches_run_approvals() -> None:
    names = [name for name in mcp_registry._TOOLS if "run_approval" in name]
    assert names == []


def test_machine_cannot_revoke_and_a_person_can(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)
    client.post(f"{_BASE}/{run_approval_id}/approve", headers=_person(key))

    denied = client.post(f"{_BASE}/{run_approval_id}/revoke", headers=_service(key), json={})
    revoked = client.post(
        f"{_BASE}/{run_approval_id}/revoke", headers=_person(key, fresh=False), json={}
    )

    assert denied.status_code == 403
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    again = client.post(f"{_BASE}/{run_approval_id}/revoke", headers=_person(key), json={})
    assert again.status_code == 409


def test_the_principal_that_asked_ends_its_run(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)

    other = client.post(f"{_BASE}/{run_approval_id}/end", headers=_service(key, sub="svc-other"))
    ended = client.post(f"{_BASE}/{run_approval_id}/end", headers=_service(key))

    assert other.status_code == 404
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "ended"


def test_other_tenant_sees_and_approves_nothing(client: TestClient, key: Any) -> None:
    run_approval_id = _request(client, key)

    shown = client.get(f"{_BASE}/{run_approval_id}", headers=_person(key, tenant=_TENANT_B))
    approved = client.post(
        f"{_BASE}/{run_approval_id}/approve", headers=_person(key, tenant=_TENANT_B)
    )
    listed = client.get(_BASE, headers=_person(key, tenant=_TENANT_B))

    assert shown.status_code == 404
    assert approved.status_code == 404
    assert listed.json() == {"run_approvals": []}
    assert _status(client, key, run_approval_id) == "pending"


def test_read_only_role_cannot_list(client: TestClient, key: Any) -> None:
    response = client.get(_BASE, headers=_person(key, role=TenantRole.READ_ONLY))

    assert response.status_code == 403


def test_destructive_entry_and_second_request_are_refused(client: TestClient, key: Any) -> None:
    body = {
        **_BODY,
        "entries": [{"connector_id": "vmware-rest-9.0", "op_id": "DELETE:/vcenter/vm/{vm}"}],
    }
    refused = client.post(_BASE, headers=_service(key), json=body)
    assert refused.status_code == 422
    assert "never covers it" in refused.json()["detail"]

    _request(client, key)
    second = client.post(_BASE, headers=_service(key), json=_BODY)
    assert second.status_code == 409
