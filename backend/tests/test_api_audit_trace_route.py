# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``GET /api/v1/audit/{audit_id}/trace`` (#3215).

The operator read surface of the dispatch flight recorder
(``docs/decisions/dispatch-flight-recorder.md``): the REST route that serves one
dispatch's ordered, already redacted+capped spans behind the operator claim.

Coverage:

* RBAC -- operator claim required (``read_only`` → 403, unauthenticated → 401,
  ``tenant_admin`` → 200).
* Tenant isolation -- a cross-tenant ``audit_id`` returns 404 (never 403, never
  the other tenant's trace); existence never leaks.
* 404 semantics -- a missing audit row is 404, matching ``/show``.
* Empty state -- an audit row that exists but carries no captured trace is a
  200 with ``trace_present=False`` (not a 404): capture is opt-in/best-effort.
* Trace rendering -- a seeded trace returns its ordered spans with the redacted
  bodies, the truncation marker, and the redaction-uncertainty flag.

The capture seam (#3214) is not yet on ``main``; per the task, traces are
seeded directly via :func:`meho_backplane.flight_recorder.record_trace` with the
attribute shape the capture seam writes -- the read surface must render what is
stored regardless of who wrote it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.flight_recorder import SpanInput, record_trace
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.redaction.flight_recorder.verdict import BODY_OMITTED_MARKER
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A = UUID("33333333-3333-3333-3333-333333333333")
_TENANT_B = UUID("44444444-4444-4444-4444-444444444444")
_BASE = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` reads + reset the flight-recorder cache."""
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
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _token(
    key: Any,
    *,
    sub: str = "op-1",
    role: TenantRole = TenantRole.OPERATOR,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


async def _seed_tenant(tenant_id: UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _seed_audit_row(
    *,
    tenant_id: UUID,
    audit_id: UUID | None = None,
    second: int = 0,
) -> UUID:
    audit_id = audit_id or uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=audit_id,
                occurred_at=_BASE + timedelta(seconds=second),
                operator_sub="op-actor",
                tenant_id=tenant_id,
                method="POST",
                path="/mcp",
                status_code=200,
                duration_ms=Decimal("1.0"),
                payload={"op_id": "vsphere.vm.list", "op_class": "read"},
            )
        )
        await session.commit()
    return audit_id


def _vendor_and_flux_spans() -> list[SpanInput]:
    """A realistic vendor_call + jsonflux_reduction pair (capture-seam shape)."""
    return [
        SpanInput(
            span_kind="vendor_call",
            name="GET /rest/vcenter/vm",
            started_at=_BASE,
            duration_ms=Decimal("12.50"),
            status="200",
            attributes={
                "connector_id": "vmware-rest-9.0",
                "op_id": "vsphere.vm.list",
                "method": "GET",
                "url": "https://vendor.example/rest/vcenter/vm",
                "request_headers": {"accept": "application/json"},
                "response_headers": {"content-type": "application/json"},
                "request_body": None,
                "response_body": {"value": [{"vm": "vm-1", "name": "web-01"}]},
                "body_recorded": True,
            },
        ),
        SpanInput(
            span_kind="jsonflux_reduction",
            name="jsonflux.reduce",
            started_at=_BASE + timedelta(milliseconds=13),
            status="ok",
            attributes={
                "op_id": "vsphere.vm.list",
                "input_rows": 4000,
                "total_rows": 4000,
                "kept_fields": ["vm", "name"],
                "kept_field_count": 2,
                "output_bytes": 128,
                "handle": "h-abc-123",
            },
        ),
    ]


async def _seed_trace(
    *,
    audit_id: UUID,
    tenant_id: UUID,
    spans: list[SpanInput],
    redaction_uncertain: bool = False,
) -> UUID:
    trace_id = await record_trace(
        audit_id=audit_id,
        tenant_id=tenant_id,
        spans=spans,
        redaction_uncertain=redaction_uncertain,
        now=_BASE,
    )
    assert trace_id is not None, "record_trace returned None (seed failed)"
    return trace_id


# ---------------------------------------------------------------------------
# Happy path: a seeded trace renders its ordered spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_returns_ordered_spans_for_captured_trace(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)
    await _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=_vendor_and_flux_spans())

    key = make_rsa_keypair("kid-A")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_id"] == str(audit_id)
    assert body["trace_present"] is True
    trace = body["trace"]
    assert trace["redaction_uncertain"] is False
    spans = trace["spans"]
    assert [s["seq"] for s in spans] == [0, 1]
    assert spans[0]["span_kind"] == "vendor_call"
    assert spans[0]["attributes"]["method"] == "GET"
    assert spans[0]["attributes"]["url"] == "https://vendor.example/rest/vcenter/vm"
    assert spans[0]["attributes"]["response_body"] == {"value": [{"vm": "vm-1", "name": "web-01"}]}
    assert spans[1]["span_kind"] == "jsonflux_reduction"
    assert spans[1]["attributes"]["handle"] == "h-abc-123"


@pytest.mark.asyncio
async def test_trace_surfaces_truncation_and_uncertainty(client: TestClient) -> None:
    """A truncated + redaction-uncertain trace surfaces both markers (F5/F3)."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)
    spans = [
        SpanInput(
            span_kind="vendor_call",
            name="POST /rest/big",
            started_at=_BASE,
            duration_ms=Decimal("99.00"),
            status="200",
            attributes={
                "method": "POST",
                "url": "https://vendor.example/rest/big",
                "request_headers": {},
                "response_headers": {},
                "request_body": None,
                "response_body": BODY_OMITTED_MARKER,
                "body_recorded": True,
                "truncated": True,
                "redaction_reasons": ["response body truncated at 64 KB cap"],
            },
        ),
    ]
    await _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=spans, redaction_uncertain=True)

    key = make_rsa_keypair("kid-A")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    trace = resp.json()["trace"]
    # F5 degrade flag is surfaced so an operator sees the trace was withheld
    # from the agent handle.
    assert trace["redaction_uncertain"] is True
    span = trace["spans"][0]
    assert span["attributes"]["truncated"] is True
    assert span["attributes"]["response_body"] == BODY_OMITTED_MARKER


# ---------------------------------------------------------------------------
# Empty state: audit row exists but no trace captured → 200, not 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_audit_exists_but_no_trace_returns_200_empty(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)  # no trace seeded

    key = make_rsa_keypair("kid-A")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trace_present"] is False
    assert body["trace"] is None


# ---------------------------------------------------------------------------
# 404 semantics + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_missing_audit_row_returns_404(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A, "tenant-a")
    key = make_rsa_keypair("kid-A")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{uuid.uuid4()}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_trace_cross_tenant_returns_404_never_leaks(client: TestClient) -> None:
    """A tenant-B operator requesting tenant-A's dispatch gets 404, not the trace."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    await _seed_tenant(_TENANT_B, "tenant-b")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)
    await _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=_vendor_and_flux_spans())

    key = make_rsa_keypair("kid-A")
    token = _token(key, tenant_id=_TENANT_B)  # caller is tenant B
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404  # never 403, never 200
    assert resp.status_code != 403
    # The tenant-A vendor URL must never appear in a tenant-B response.
    assert "vendor.example" not in resp.text


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_trace_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get(f"/api/v1/audit/{uuid.uuid4()}/trace")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_trace_read_only_role_returns_403(client: TestClient) -> None:
    """``read_only`` is below the operator gate — 403 before any trace read."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)
    await _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=_vendor_and_flux_spans())

    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.READ_ONLY)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert "vendor.example" not in resp.text


@pytest.mark.asyncio
async def test_trace_tenant_admin_role_returns_200(client: TestClient) -> None:
    """``tenant_admin`` clears the operator gate (operator minimum)."""
    await _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = await _seed_audit_row(tenant_id=_TENANT_A)
    await _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=_vendor_and_flux_spans())

    key = make_rsa_keypair("kid-A")
    token = _token(key, role=TenantRole.TENANT_ADMIN)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/audit/{audit_id}/trace",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["trace_present"] is True


# ---------------------------------------------------------------------------
# OpenAPI mount surface
# ---------------------------------------------------------------------------


def test_openapi_lists_trace_route(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/audit/{audit_id}/trace" in paths
    get = paths["/api/v1/audit/{audit_id}/trace"]["get"]
    assert "audit" in get["tags"]
    ok_schema = get["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok_schema["$ref"].endswith("/AuditTraceResult")
