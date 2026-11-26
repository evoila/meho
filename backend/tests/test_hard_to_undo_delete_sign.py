# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, an operation that is hard to undo has a delete sign.

Each call goes the whole path: a service login sends
``POST /api/v1/operations/call`` with a signed token, the real gate decides,
and the test reads the audit rows.

* Switch on: a standing grant and a run approval no longer cover an
  operation with ``hard_to_undo``. The call waits for a person, and no grant
  use row is written. A new grant for it is refused.
* Switch off: the same row gives today's answer. The grant and the run
  approval cover the call.
* Sandbox mode, switch on: a delete grant covers the call, also for
  ``stops`` and ``overwrites``. An ordinary grant still does not.
* The query in the upgrade note lists the grants the switch touches.

The connector, the product and the target are made up.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select, text

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    RunApproval,
    SandboxDeleteGrant,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.main import app
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    turn_sandbox_mode_on,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007056")
_SERVICE = "svc:undo-bot"
_CONNECTOR = "undo-api-1.0"
_OP = "undo.widget.reset"
_TARGET = "undo-widget-01"
_RUN = "run-undo-widget"
_KINDS = ("deletes", "stops", "overwrites")
_UPGRADE_ROW = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "upgrade-rows"
    / "v0.49.0"
    / "hard-to-undo-no-standing-grant.md"
)

#: Every run of the stand-in operation, so a test can prove what ran.
_RUNS: list[dict[str, Any]] = []


class _UndoConnector(Connector):
    product = "undo"
    version = "1.0"
    impl_id = "undo-api"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


async def _reset(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target
    _RUNS.append(params)
    return {"reset": True}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    register_connector_v2(product="undo", version="1.0", impl_id="undo-api", cls=_UndoConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    get_settings.cache_clear()
    clear_jwks_cache()


def _custom_policy(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


async def _register(hard_to_undo: str | None) -> None:
    """A ``caution`` operation that waits for approval, with no other delete sign."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product="undo",
        version="1.0",
        impl_id="undo-api",
        op_id=_OP,
        handler=_reset,
        summary="Reset a widget.",
        description="Sets one widget of a made-up product back to its first state.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution",
        requires_approval=True,
        hard_to_undo=hard_to_undo,  # type: ignore[arg-type]
        embedding_service=embedding,
    )


async def _seed_target() -> uuid.UUID:
    async with get_sessionmaker()() as session:
        target = Target(
            tenant_id=_TENANT,
            name=_TARGET,
            aliases=[],
            product="undo",
            host="widget.example.invalid",
            port=443,
            fqdn=None,
            secret_ref=None,
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.0"},
            notes="seeded by test_hard_to_undo_delete_sign",
        )
        session.add(target)
        await session.commit()
        return target.id


async def _seed_grant() -> uuid.UUID:
    """A standing grant of the service login, written before the switch is turned on."""
    return await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
    )


async def _seed_run_approval(target_id: uuid.UUID) -> uuid.UUID:
    """A live run approval that lists the operation on the target, approved by a person."""
    approval_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            RunApproval(
                id=approval_id,
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                work_ref=_RUN,
                entries=[{"connector_id": _CONNECTOR, "op_id": _OP, "target_id": str(target_id)}],
                reason="test: reset the widget in this run",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
    return approval_id


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-hard-to-undo")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _call(
    client: tuple[httpx.AsyncClient, Any], *, work_ref: str | None = None
) -> dict[str, Any]:
    """The service login calls the operation over REST, as a real caller does."""
    ac, key = client
    token = mint_token(
        key,
        sub=_SERVICE,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(_TENANT),
        principal_kind="service",
        extra_claims={"client_id": "svc-undo-client"},
    )
    body: dict[str, Any] = {
        "connector_id": _CONNECTOR,
        "op_id": _OP,
        "target": _TARGET,
        "params": {},
    }
    if work_ref is not None:
        body["work_ref"] = work_ref
    response = await ac.post(
        "/api/v1/operations/call", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def _decision_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.path.in_(("approval.decision", "approval.request")))
        )
        return list(rows.scalars().all())


async def _waits_for_a_person(result: dict[str, Any]) -> None:
    assert result["status"] == "awaiting_approval", result
    assert _RUNS == []
    request_rows = [r for r in await _decision_rows() if r.path == "approval.request"]
    assert [r.payload.get("op_id") for r in request_rows] == [_OP]
    assert [r for r in await _decision_rows() if r.path == "approval.decision"] == []


# --- Switch on: no standing grant and no run approval covers it -------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_on_a_standing_grant_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    _custom_policy(monkeypatch, on=True)
    await _register(kind)
    await _seed_target()
    grant_id = await _seed_grant()

    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_on_a_run_approval_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    _custom_policy(monkeypatch, on=True)
    await _register(kind)
    approval_id = await _seed_run_approval(await _seed_target())

    await _waits_for_a_person(await _call(client, work_ref=_RUN))
    rows = await _decision_rows()
    assert all(r.payload.get("run_approval_id") != str(approval_id) for r in rows)


async def test_switch_on_a_new_grant_for_it_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _custom_policy(monkeypatch, on=True)
    await _register("stops")
    payload = ServiceGrantCreate(
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
        reason="reset widgets at night",
    )
    with pytest.raises(GrantValidationError, match="hard to undo"):
        await ServicePrincipalGrantService().create(_TENANT, "op-admin", payload)
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(ServicePrincipalGrant))).scalars().all() == []


# --- Switch off: today's answer ----------------------------------------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_off_a_standing_grant_still_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    _custom_policy(monkeypatch, on=False)
    await _register(kind)
    await _seed_target()
    grant_id = await _seed_grant()

    result = await _call(client)
    assert result["status"] == "ok", result
    assert len(_RUNS) == 1
    assert len(await grant_use_rows(grant_id)) == 1


async def test_switch_off_a_run_approval_still_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    _custom_policy(monkeypatch, on=False)
    await _register("overwrites")
    approval_id = await _seed_run_approval(await _seed_target())

    result = await _call(client, work_ref=_RUN)
    assert result["status"] == "ok", result
    assert len(_RUNS) == 1
    used = [
        r
        for r in await _decision_rows()
        if r.path == "approval.decision" and r.payload.get("run_approval_id") == str(approval_id)
    ]
    assert len(used) == 1


async def test_switch_off_a_new_grant_for_it_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    _custom_policy(monkeypatch, on=False)
    await _register("stops")
    payload = ServiceGrantCreate(
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
        reason="reset widgets at night",
    )
    entry = await ServicePrincipalGrantService().create(_TENANT, "op-admin", payload)
    assert entry.op_id == _OP


# --- Sandbox mode: the delete exception stays ---------------------------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_sandbox_switch_on_a_delete_grant_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _custom_policy(monkeypatch, on=True)
    await _register(kind)
    await _seed_target()
    grant_id = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
    )

    result = await _call(client)
    assert result["status"] == "ok", result
    assert len(_RUNS) == 1
    assert len(await grant_use_rows(grant_id)) == 1


async def test_sandbox_switch_on_an_ordinary_grant_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    _custom_policy(monkeypatch, on=True)
    await _register("stops")
    await _seed_target()
    grant_id = await _seed_grant()

    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []


# --- The upgrade note: the query lists the grants the switch touches ----------------


def _upgrade_note_query() -> str:
    action = _UPGRADE_ROW.read_text(encoding="utf-8").split("## Action", 1)[1]
    match = re.search(r"`(SELECT [^`]+)`", action)
    assert match is not None, "the upgrade note has no query"
    return match.group(1).replace("\\|", "|")


async def test_upgrade_note_query_lists_the_grants_the_switch_touches(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    _custom_policy(monkeypatch, on=False)
    await _register("deletes")
    await _seed_target()
    touched = await _seed_grant()
    await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        revoked_at=datetime.now(UTC),
    )
    await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id="other-api-1.0",
    )

    async with get_sessionmaker()() as session:
        listed = (await session.execute(text(_upgrade_note_query()))).all()
    assert [(row.principal_sub, row.op_id, row.hard_to_undo) for row in listed] == [
        (_SERVICE, _OP, "deletes")
    ]
    assert len(await grant_use_rows(touched)) == 0

    # The grant the query lists is the one that stops covering the call.
    assert (await _call(client))["status"] == "ok"
    assert len(await grant_use_rows(touched)) == 1
    _custom_policy(monkeypatch, on=True)
    assert (await _call(client))["status"] == "awaiting_approval"
    assert len(_RUNS) == 1
    assert len(await grant_use_rows(touched)) == 1


async def test_upgrade_note_query_lists_no_grant_of_an_op_that_is_not_hard_to_undo() -> None:
    await _register(None)
    await _seed_grant()
    async with get_sessionmaker()() as session:
        assert (await session.execute(text(_upgrade_note_query()))).all() == []


# --- Unused delete grant table with the mode off -----------------------------------


async def test_switch_on_without_sandbox_reads_no_delete_grant(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    _custom_policy(monkeypatch, on=True)
    await _register("deletes")
    await _seed_target()
    grant_id = await seed_delete_grant(
        tenant_id=_TENANT,
        principal_sub=_SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
    )
    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(SandboxDeleteGrant))).scalars().all()) == 1
