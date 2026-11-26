# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the install's delete names only add (#7061).

The setting ``SERVICE_GRANT_DELETE_SHAPED_PATTERNS`` lists the names that
make MEHO treat a call as a delete. A delete always waits for a person, and
no standing grant covers it.

* With the switch on, the setting only adds names to the built-in delete
  names. An empty setting, or one that lists other names, keeps every
  built-in name.
* With the switch on and other names listed, a service login calls an
  operation with a built-in delete name over REST: the call waits, its
  standing grant does not cover it, and the audit rows say so.
* With the switch off, the setting works as before: a listed name list
  replaces the built-in names.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, ServicePrincipalGrant, Tenant
from meho_backplane.delete_names import DELETE_SHAPED_OP_ID_PATTERNS
from meho_backplane.main import app
from meho_backplane.operations import register_typed_operation
from meho_backplane.settings import (
    _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS,
    get_settings,
)
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.test_agent_grant_required import (
    _TENANT,
    _reset_state,
    _seed_vault_target,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

__all__ = ["_reset_state", "stub_embedding_service"]

#: Ends in ``.delete``: a built-in delete name (``*.delete``).
_OP = "vault.kv.stand_in.delete"
_CONNECTOR = "vault-1.x"
_SERVICE_SUB = "service-delete-names"
_OTHER_NAMES = "acme.custom.nuke, acme.custom.wipe"
_PARAMS: dict[str, Any] = {"path": "app/config"}

_RUNS: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.delenv("AGENT_GRANT_REQUIRED", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _set(monkeypatch: pytest.MonkeyPatch, *, switch: bool, names: str | None) -> tuple[str, ...]:
    """Set the switch and the delete-name setting; return the names MEHO uses."""
    monkeypatch.setenv("CUSTOM_POLICY", "true" if switch else "false")
    if names is None:
        monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    else:
        monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", names)
    get_settings.cache_clear()
    return get_settings().service_grant_delete_shaped_patterns


# --- The names MEHO uses ------------------------------------------------------------


def test_the_built_in_names_include_the_delete_names_module() -> None:
    """The built-in list is read from the code, so the tests below read it too."""
    assert set(DELETE_SHAPED_OP_ID_PATTERNS) <= set(_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS)


@pytest.mark.parametrize("names", [None, "", "  ", " , ,"])
def test_switch_on_with_an_empty_setting_keeps_every_built_in_name(
    monkeypatch: pytest.MonkeyPatch, names: str | None
) -> None:
    assert _set(monkeypatch, switch=True, names=names) == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


def test_switch_on_with_other_names_keeps_every_built_in_name_and_adds_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patterns = _set(monkeypatch, switch=True, names=_OTHER_NAMES)
    assert patterns == (
        *_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS,
        "acme.custom.nuke",
        "acme.custom.wipe",
    )


def test_switch_on_lists_a_name_once_when_the_setting_repeats_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built-in name in the setting, or a name listed twice, is kept once."""
    built_in = _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS[1]
    patterns = _set(monkeypatch, switch=True, names=f"acme.custom.nuke,{built_in},acme.custom.nuke")
    assert patterns == (*_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS, "acme.custom.nuke")


def test_switch_off_keeps_todays_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off, a listed name list replaces the built-in names, as today."""
    assert _set(monkeypatch, switch=False, names=_OTHER_NAMES) == (
        "acme.custom.nuke",
        "acme.custom.wipe",
    )
    assert _set(monkeypatch, switch=False, names="") == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )
    assert _set(monkeypatch, switch=False, names=None) == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


# --- The whole path: a service login calls a delete over REST ------------------------


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del operator, target
    _RUNS.append(params)
    return {"done": True}


@pytest.fixture
async def op(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """A ``caution`` operation whose name is a built-in delete name."""
    _RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_handler,
        summary="Stand-in delete.",
        description="Counts its runs.",
        parameter_schema={"type": "object"},
        safety_level="caution",
        requires_approval=False,
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    yield
    _RUNS.clear()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-delete-names-add-only")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _seed_target_and_grant() -> tuple[str, uuid.UUID]:
    """A stored target and a standing grant for the delete on it.

    The grant is written past the create check, as a grant saved before the
    setting changed would be.
    """
    target = await _seed_vault_target()
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="t-delete-names", name="Tenant delete names"))
        session.add(
            ServicePrincipalGrant(
                id=grant_id,
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=_OP,
                connector_id=_CONNECTOR,
                target_id=target.id,
                reason="saved before the setting changed",
                created_by_sub="admin-1",
            )
        )
        await session.commit()
    return target.name, grant_id


async def _call_as_service(client: tuple[httpx.AsyncClient, Any], target: str) -> httpx.Response:
    ac, key = client
    token = mint_token(
        key,
        sub=_SERVICE_SUB,
        tenant_role="operator",
        tenant_id=str(_TENANT),
        principal_kind="service",
        extra_claims={"client_id": "svc-delete-names"},
    )
    return await ac.post(
        "/api/v1/operations/call",
        json={"connector_id": _CONNECTOR, "op_id": _OP, "target": target, "params": _PARAMS},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _grant_use_rows(grant_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "APPROVAL", AuditLog.path == "approval.decision"
                )
            )
        ).scalars()
        return [r for r in rows if r.payload.get("grant_id") == str(grant_id)]


async def _approval_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(select(ApprovalRequest).where(ApprovalRequest.op_id == _OP))
            ).scalars()
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("op")
async def test_switch_on_a_built_in_delete_name_waits_and_no_grant_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    _set(monkeypatch, switch=True, names=_OTHER_NAMES)
    target, grant_id = await _seed_target_and_grant()

    response = await _call_as_service(client, target)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_approval", response.text
    assert _RUNS == []
    assert await _grant_use_rows(grant_id) == []
    (request,) = await _approval_requests()
    assert request.principal_sub == _SERVICE_SUB
    assert request.request_audit_id is not None
    async with get_sessionmaker()() as session:
        row = await session.get(AuditLog, request.request_audit_id)
    assert row is not None
    assert (row.method, row.path) == ("APPROVAL", "approval.request")
    assert row.payload["op_id"] == _OP
    assert row.payload["principal_sub"] == _SERVICE_SUB
    assert row.payload["result_status"] == "request"


@pytest.mark.asyncio
@pytest.mark.usefixtures("op")
async def test_switch_off_the_same_setting_lets_the_grant_cover_it_as_today(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    """The control: with the switch off, the listed names replace ``*.delete``."""
    _set(monkeypatch, switch=False, names=_OTHER_NAMES)
    target, grant_id = await _seed_target_and_grant()

    response = await _call_as_service(client, target)

    assert response.status_code == 200, response.text
    assert response.json()["status"] != "awaiting_approval", response.text
    assert _RUNS == [_PARAMS]
    (row,) = await _grant_use_rows(grant_id)
    assert row.payload["decision"] == "auto-approved"
    assert await _approval_requests() == []
