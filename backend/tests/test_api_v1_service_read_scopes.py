# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST tests for ``/api/v1/service-principals/read-scopes/{principal_sub}`` (#4992).

Three contracts:

* **RBAC** -- every route needs the ``operator`` role; a ``read_only`` JWT
  gets HTTP 403 ``insufficient_role``.
* **People only on set and clear** -- a machine ``principal_kind`` (agent or
  service) holding the ``operator`` role gets HTTP 403
  ``human_principal_required`` on set and clear, before the route runs.
  Show stays role-only.
* **Happy path (operator)** -- set -> show -> set again -> clear -> show
  (404) -> clear again (404), with one audit row per change that carries
  the stored entries; a wildcard ``principal_sub`` and a bad entry are 422.
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
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PRINCIPAL = "svc:deploy-bot"
_BASE = "/api/v1/service-principals/read-scopes"
_PATH = f"{_BASE}/{_PRINCIPAL}"
_TARGET_ID = "11111111-1111-1111-1111-111111111111"
_BODY: dict[str, Any] = {
    "entries": [
        {"target_id": _TARGET_ID},
        {"target_product": "vmware", "target_name_pattern": "dc-*"},
        {"no_target": True},
    ]
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)


def _token(key: Any, *, role: TenantRole, sub: str = "op-test") -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(_TENANT_A))


def _machine_operator_token(key: Any, *, kind: str, sub: str) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT_A),
        principal_kind=kind,
    )


async def _audit_rows(operator_sub: str, method: str, path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.operator_sub == operator_sub,
                        AuditLog.method == method,
                        AuditLog.path == path,
                    )
                )
            )
            .scalars()
            .all()
        )


_ENDPOINTS: tuple[tuple[str, dict[str, Any] | None], ...] = (
    ("PUT", _BODY),
    ("GET", None),
    ("DELETE", None),
)


@pytest.mark.parametrize("method, body", _ENDPOINTS)
def test_read_only_role_is_rejected(
    client: TestClient, method: str, body: dict[str, Any] | None
) -> None:
    """``read_only`` JWT -> HTTP 403 ``insufficient_role`` on every route."""
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        response = client.request(method, _PATH, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


# RUNNER is refused earlier on the wire (401 missing_runner_id), so it never
# reaches the people-only check over REST. Same split as the grant tests.
@pytest.mark.parametrize("kind", ("agent", "service"))
@pytest.mark.parametrize("method, body", (_ENDPOINTS[0], _ENDPOINTS[2]))
async def test_machine_principal_denied_on_set_and_clear(
    client: TestClient, method: str, body: dict[str, Any] | None, kind: str
) -> None:
    """A machine ``operator`` token -> 403 ``human_principal_required`` on set and clear."""
    sub = f"machine-{kind}-{method.lower()}"
    key = make_rsa_keypair(f"kid-machine-{kind}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_machine_operator_token(key, kind=kind, sub=sub)}"}
        response = client.request(method, _PATH, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("human_principal_required"), response.text

    rows = await _audit_rows(sub, method, _PATH)
    assert len(rows) == 1
    assert rows[0].status_code == 403


async def test_operator_set_show_clear_round_trip(client: TestClient) -> None:
    """set (200) -> show -> set again -> clear (204) -> show (404) -> clear (404)."""
    key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}

        before = client.get(_PATH, headers=headers)
        assert before.status_code == 404
        assert before.json() == {"detail": "read_scope_not_found"}

        set_response = client.put(_PATH, headers=headers, json=_BODY)
        assert set_response.status_code == 200, set_response.text
        stored = set_response.json()
        assert stored["principal_sub"] == _PRINCIPAL
        assert stored["tenant_id"] == str(_TENANT_A)
        assert stored["set_by_sub"] == "op-test"
        assert [entry["target_id"] for entry in stored["entries"]] == [_TARGET_ID, None, None]
        assert stored["entries"][1]["target_product"] == "vmware"
        assert stored["entries"][1]["target_name_pattern"] == "dc-*"
        assert stored["entries"][2]["no_target"] is True

        shown = client.get(_PATH, headers=headers)
        assert shown.status_code == 200
        assert shown.json() == stored

        again = client.put(_PATH, headers=headers, json={"entries": [{"no_target": True}]})
        assert again.status_code == 200, again.text
        assert len(again.json()["entries"]) == 1

        cleared = client.delete(_PATH, headers=headers)
        assert cleared.status_code == 204

        after = client.get(_PATH, headers=headers)
        assert after.status_code == 404

        re_clear = client.delete(_PATH, headers=headers)
        assert re_clear.status_code == 404

    set_rows = await _audit_rows("op-test", "PUT", _PATH)
    assert len(set_rows) == 2
    assert set_rows[0].status_code == 200
    assert set_rows[0].payload["op_id"] == "service_read_scope.set"
    assert set_rows[0].payload["agent_name"] == _PRINCIPAL
    assert set_rows[0].payload["entries"] == [
        {"target_id": _TARGET_ID},
        {"target_product": "vmware", "target_name_pattern": "dc-*"},
        {"no_target": True},
    ]
    clear_rows = await _audit_rows("op-test", "DELETE", _PATH)
    assert [row.status_code for row in clear_rows] == [204, 404]
    assert clear_rows[0].payload["op_id"] == "service_read_scope.clear"


@pytest.mark.parametrize(
    "path, body, fragment",
    [
        (f"{_BASE}/svc:*", _BODY, "wildcards are not permitted"),
        (_PATH, {"entries": []}, "at least 1"),
        (_PATH, {"entries": [{}]}, "exactly one"),
        (_PATH, {"entries": [{"target_id": _TARGET_ID, "no_target": True}]}, "exactly one"),
        (_PATH, {"entries": [{"target_name_pattern": "dc-*", "colour": "red"}]}, "colour"),
    ],
)
def test_set_rejects_bad_input(
    client: TestClient, path: str, body: dict[str, Any], fragment: str
) -> None:
    """A wildcard principal, an empty scope, or a bad entry is refused with 422."""
    key = make_rsa_keypair("kid-op2")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        response = client.put(path, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert fragment in response.text


def test_show_is_tenant_scoped(client: TestClient) -> None:
    """A scope set in one tenant is a 404 for an operator of another tenant."""
    key = make_rsa_keypair("kid-tenant")
    other_tenant = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers_a = {"Authorization": f"Bearer {_token(key, role=TenantRole.OPERATOR)}"}
        set_in_a = client.put(_PATH, headers=headers_a, json=_BODY)
        assert set_in_a.status_code == 200
        token_b = mint_token(
            key, sub="op-b", tenant_role=TenantRole.OPERATOR.value, tenant_id=str(other_tenant)
        )
        headers_b = {"Authorization": f"Bearer {token_b}"}
        shown_in_b = client.get(_PATH, headers=headers_b)
        cleared_in_b = client.delete(_PATH, headers=headers_b)
        still_in_a = client.get(_PATH, headers=headers_a)
    assert shown_in_b.status_code == 404
    assert cleared_in_b.status_code == 404
    assert still_in_a.status_code == 200
