# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Round trip: ``Meho-Work-Ref`` header -> audit rows of the control-plane verbs (#3804).

The CLI sends the global ``--work-ref`` as the ``Meho-Work-Ref`` header on
every backplane request. These tests send the same requests the four
required CLI verbs send -- ``targets add``, ``agent grant create``,
``connector ingest`` and ``connector edit-op`` -- with that header, let
them succeed, and then look the rows up the way ``meho audit query
--work-ref`` does: ``audit_log.work_ref = :ref``.

Two kinds of rows must be found:

* the HTTP request row the audit middleware writes for the request, and
* the action rows the connector service writes inside the request
  (``meho_connector_edit_op``, ``meho_connector_llm_grouping`` for an
  ingest, ...). They land only because the middleware binds the header
  for the whole request, not just around its own write.

The edit-op test also checks the other half of the contract: a request
without the header writes an action row with ``work_ref`` NULL, so
nothing leaks from one request to the next.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import meho_backplane.api.v1.connectors_ingest as _ingest_route
from meho_backplane.api.v1.connectors_ingest import set_llm_client_factory
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.main import app
from meho_backplane.operations.ingest import catalog as _catalog_mod
from meho_backplane.operations.ingest import (
    default_llm_client_factory,
    reset_job_registry_for_tests,
)
from meho_backplane.operations.ingest._internals import (
    AUDIT_METHOD,
    OP_EDIT_OP,
    OP_LLM_GROUPING,
)
from meho_backplane.operations.ingest.catalog import ConnectorSpecCatalog, ConnectorSpecEntry
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_WORK_REF = "gh:evoila/meho#3804"
_WITH_REF = {"Meho-Work-Ref": _WORK_REF}
_TENANT = uuid.UUID("33333333-3333-3333-3333-333333333333")
_EMBEDDING_PATCH = "meho_backplane.operations.ingest._upsert.encode_endpoint_text"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`~meho_backplane.settings.Settings` requires."""
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


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    """Start from empty registries and restore the fail-closed LLM factory.

    An empty connector registry makes ``targets add`` skip product
    validation (the same shape ``tests._targets_helpers`` uses). The
    ingest job registry is process-wide, so it is wiped on both sides.
    """
    clear_registry()
    reset_job_registry_for_tests()
    yield
    clear_registry()
    reset_job_registry_for_tests()
    set_llm_client_factory(default_llm_client_factory)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_headers(key: Any, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Bearer header for a human ``tenant_admin`` of ``_TENANT``, plus *extra*."""
    token = mint_token(
        key,
        sub="op-work-ref",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
    )
    return {"Authorization": f"Bearer {token}", **(extra or {})}


async def _rows_with_work_ref(ref: str) -> list[AuditLog]:
    """Look rows up the way ``meho audit query --work-ref`` does: exact match."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AuditLog).where(AuditLog.work_ref == ref).order_by(AuditLog.occurred_at)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _rows_for_op(op_id: str) -> list[AuditLog]:
    """Every action row whose ``path`` is *op_id*, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(AuditLog).where(AuditLog.path == op_id).order_by(AuditLog.occurred_at)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def _service_rows() -> list[AuditLog]:
    """Every service-level row (the connector service's ``method``)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.method == AUDIT_METHOD))
        return list(result.scalars().all())


async def _seed_connector(tenant_id: uuid.UUID) -> None:
    """One staged group with two ingested ops under ``vmware-rest-9.0``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                group_key="group-0",
                name="Group 0",
                when_to_use="Use group 0 for things.",
                review_status="staged",
            ),
        )
        for index in range(2):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=f"GET:/api/v1/group-0/{index}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/v1/group-0/{index}",
                    group_id=group_id,
                    summary=f"Operation {index}",
                    is_enabled=False,
                ),
            )
        await session.commit()


async def _register_agent_principal(tenant_id: uuid.UUID, client_id: str) -> None:
    """Register the agent principal a grant names; the grant service rejects unknown subs."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentPrincipal(
                tenant_id=tenant_id,
                name="deploy-bot",
                keycloak_client_id=client_id,
                keycloak_internal_id=str(uuid.uuid4()),
                owner_sub="seed-owner",
                created_by_sub="seed-owner",
            )
        )
        await session.commit()


class _StubLlmClient:
    """Deterministic grouping answers for the one-op ``_fixture_minimal.yaml`` spec."""

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        if "ropose" in system_prompt:
            return (
                '[{"group_key": "things", "name": "Things", '
                '"when_to_use": "Use these operations to work with things."}]'
            )
        return '{"GET:/things": "things"}'


def _install_shipped_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ingest route at a one-entry catalog backed by the packaged minimal spec.

    Same shape as ``tests.test_api_v1_connectors_ingest._patch_catalog``:
    the route resolves ``load_catalog`` through its own module attribute,
    and the cached loader is cleared so an earlier test cannot leave the
    on-disk catalog behind.
    """
    fake = ConnectorSpecCatalog(
        entries=(
            ConnectorSpecEntry(
                product="shippedt1",
                version="1.0",
                impl_id="shippedt1-rest",
                requires_connector_class="ProfiledRestConnector_shippedt1",
                upstream=None,
                spec_resource="_fixture_minimal.yaml",
                profile_resource="_fixture_minimal.yaml",
            ),
        )
    )
    _catalog_mod.load_catalog.cache_clear()
    monkeypatch.setattr(_ingest_route, "load_catalog", lambda: fake)
    set_llm_client_factory(_StubLlmClient)


# ---------------------------------------------------------------------------
# targets add / agent grant create -- the request row carries the reference
# ---------------------------------------------------------------------------


async def test_targets_add_request_row_found_by_work_ref() -> None:
    """``meho targets add --work-ref`` -> ``POST /api/v1/targets`` row has the ref."""
    key = make_rsa_keypair("kid-wr-targets")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        response = TestClient(app).post(
            "/api/v1/targets",
            json={"name": "wr-host", "product": "ssh", "host": "target.example.test"},
            headers=_admin_headers(key, extra=_WITH_REF),
        )
    assert response.status_code == 201, response.text

    rows = await _rows_with_work_ref(_WORK_REF)
    assert [(row.method, row.path) for row in rows] == [("POST", "/api/v1/targets")]


async def test_agent_grant_create_request_row_found_by_work_ref() -> None:
    """``meho agent grant create --work-ref`` -> ``POST /api/v1/agents/grants`` row has the ref."""
    await _register_agent_principal(_TENANT, "agent:deploy-bot")
    key = make_rsa_keypair("kid-wr-grants")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        response = TestClient(app).post(
            "/api/v1/agents/grants",
            json={
                "principal_sub": "agent:deploy-bot",
                "op_pattern": "vm.list",
                "verdict": "auto-execute",
            },
            headers=_admin_headers(key, extra=_WITH_REF),
        )
    assert response.status_code == 201, response.text

    rows = await _rows_with_work_ref(_WORK_REF)
    assert [(row.method, row.path) for row in rows] == [("POST", "/api/v1/agents/grants")]


# ---------------------------------------------------------------------------
# connector edit-op -- the action row carries it too, and does not leak
# ---------------------------------------------------------------------------


async def test_connector_edit_op_action_row_found_by_work_ref_and_does_not_leak() -> None:
    """``meho connector edit-op --work-ref`` tags the request row AND the edit-op row.

    A second edit without the header, on the same client, lands its
    action row with ``work_ref`` NULL: the bind is per request.
    """
    await _seed_connector(_TENANT)
    key = make_rsa_keypair("kid-wr-edit-op")
    client = TestClient(app)
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        tagged = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "dangerous"},
            headers=_admin_headers(key, extra=_WITH_REF),
        )
        untagged = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/1",
            json={"safety_level": "dangerous"},
            headers=_admin_headers(key),
        )
    assert tagged.status_code == 200, tagged.text
    assert untagged.status_code == 200, untagged.text

    found = await _rows_with_work_ref(_WORK_REF)
    paths = sorted(row.path for row in found)
    assert len(paths) == 2, paths
    assert OP_EDIT_OP in paths
    assert any(path.startswith("/api/v1/connectors/vmware-rest-9.0/operations/") for path in paths)

    action_rows = await _rows_for_op(OP_EDIT_OP)
    assert sorted((row.payload["op_id"], row.work_ref) for row in action_rows) == [
        ("GET:/api/v1/group-0/0", _WORK_REF),
        ("GET:/api/v1/group-0/1", None),
    ]


# ---------------------------------------------------------------------------
# connector ingest -- sync path and the background job
# ---------------------------------------------------------------------------


async def test_connector_ingest_sync_action_rows_found_by_work_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``meho connector ingest --work-ref`` (inline run) tags the request and grouping rows."""
    _install_shipped_catalog(monkeypatch)
    key = make_rsa_keypair("kid-wr-ingest")
    with (
        respx.mock as router,
        patch(_EMBEDDING_PATCH, AsyncMock(return_value=[0.25] * 384)),
    ):
        mock_discovery_and_jwks(router, public_jwks(key))
        response = TestClient(app).post(
            "/api/v1/connectors/ingest",
            json={"catalog_entry": "shippedt1/1.0", "async": False},
            headers=_admin_headers(key, extra=_WITH_REF),
        )
    assert response.status_code == 200, response.text
    assert response.json()["ingestion"]["inserted_count"] == 1

    found = await _rows_with_work_ref(_WORK_REF)
    paths = {row.path for row in found}
    assert "/api/v1/connectors/ingest" in paths
    assert OP_LLM_GROUPING in paths
    # Every service-level row of the ingest carries it, not only the grouping row.
    service_rows = await _service_rows()
    assert service_rows, "the ingest wrote no service-level rows"
    assert {row.work_ref for row in service_rows} == {_WORK_REF}


async def test_connector_ingest_background_job_rows_found_by_work_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (background job) ingest inherits the request's work-ref.

    The route starts the pipeline with ``asyncio.create_task``, which
    copies the request context -- so the job's grouping row carries the
    header value even though the request has long returned 202. Driven
    over ``ASGITransport`` so the task runs on this loop (the sync
    ``TestClient`` tears it down with the request portal).
    """
    _install_shipped_catalog(monkeypatch)
    key = make_rsa_keypair("kid-wr-ingest-job")
    with (
        respx.mock as router,
        patch(_EMBEDDING_PATCH, AsyncMock(return_value=[0.25] * 384)),
    ):
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            started = await client.post(
                "/api/v1/connectors/ingest",
                json={"catalog_entry": "shippedt1/1.0"},
                headers=_admin_headers(key, extra=_WITH_REF),
            )
            assert started.status_code == 202, started.text
            job_id = started.json()["job_id"]
            deadline = time.monotonic() + 30.0
            while True:
                polled = await client.get(
                    f"/api/v1/connectors/ingest/jobs/{job_id}",
                    headers=_admin_headers(key),
                )
                assert polled.status_code == 200, polled.text
                if polled.json()["status"] != "running":
                    break
                assert time.monotonic() < deadline, "ingest job did not finish in time"
                await asyncio.sleep(0.05)
    assert polled.json()["status"] == "succeeded", polled.text

    grouping_rows = await _rows_for_op(OP_LLM_GROUPING)
    assert [row.work_ref for row in grouping_rows] == [_WORK_REF]
