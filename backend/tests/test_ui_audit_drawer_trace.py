# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the audit drawer's flight-recorder trace pane (#3215).

The console read surface of the dispatch flight recorder: a new section in
``audit/_drawer.html`` (after Lineage) rendered via the drawer route
``GET /ui/audit/show/{audit_id}``. Covers:

* a captured trace renders its ordered spans with method / URL / status /
  duration and the redacted bodies;
* the truncation marker + the redaction-uncertainty banner are visible;
* the empty state ("no trace captured") shows when an audit row carries no
  trace (best-effort / opt-in capture, not an error);
* vendor-controlled body text is HTML-escaped (never rendered as live markup),
  the template-injection guard.

Suite shape mirrors :mod:`backend.tests.test_ui_audit_drawer` (session-cookie
HTTP edge + DB-seeded rows). Traces are seeded directly via
:func:`meho_backplane.flight_recorder.record_trace` -- the capture seam (#3214)
is not yet on ``main`` and the read surface renders what is stored.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.flight_recorder import SpanInput, record_trace
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.redaction.flight_recorder.verdict import BODY_OMITTED_MARKER
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing

_BACKPLANE_URL = "https://meho.test"
_DEFAULT_ISSUER = "https://keycloak.test/realms/meho"
_DEFAULT_AUDIENCE = "meho-backplane"
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OPERATOR_SUB = "op-self"
_BASE = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_flight_recorder_config_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    reset_flight_recorder_config_cache_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_audit_row(*, tenant_id: uuid.UUID, row_id: uuid.UUID | None = None) -> uuid.UUID:
    resolved_id = row_id or uuid.uuid4()

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AuditLog(
                    id=resolved_id,
                    occurred_at=_BASE,
                    operator_sub="op-actor",
                    tenant_id=tenant_id,
                    method="POST",
                    path="/mcp",
                    status_code=200,
                    duration_ms=Decimal("1.0"),
                    payload={"op_id": "vsphere.vm.list", "op_class": "read"},
                )
            )
        return resolved_id

    return asyncio.run(_do())


def _seed_trace(
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    spans: list[SpanInput],
    redaction_uncertain: bool = False,
) -> None:
    async def _do() -> None:
        trace_id = await record_trace(
            audit_id=audit_id,
            tenant_id=tenant_id,
            spans=spans,
            redaction_uncertain=redaction_uncertain,
            now=_BASE,
        )
        assert trace_id is not None, "record_trace returned None (seed failed)"

    asyncio.run(_do())


def _seed_session_sync(*, tenant_id: uuid.UUID, operator_sub: str = _OPERATOR_SUB) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _vendor_and_flux_spans(*, response_body: Any = None) -> list[SpanInput]:
    body = response_body if response_body is not None else {"value": [{"vm": "vm-1"}]}
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
                "response_body": body,
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
                "kept_fields": ["vm", "name"],
                "handle": "h-abc-123",
            },
        ),
    ]


def test_drawer_renders_trace_pane_with_spans() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = _seed_audit_row(tenant_id=_TENANT_A)
    _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=_vendor_and_flux_spans())
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{audit_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "Flight recorder" in body
    # Per-span human-investigable substance (not GUIDs).
    assert "vendor_call" in body
    assert "https://vendor.example/rest/vcenter/vm" in body
    assert "jsonflux_reduction" in body
    assert "h-abc-123" in body  # the result-handle id
    # Not the empty state.
    assert "No trace captured" not in body


def test_drawer_trace_pane_shows_truncation_and_uncertainty() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = _seed_audit_row(tenant_id=_TENANT_A)
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
    _seed_trace(audit_id=audit_id, tenant_id=_TENANT_A, spans=spans, redaction_uncertain=True)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{audit_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "truncated" in body
    assert "Redaction uncertain" in body  # F5 operator-only banner
    assert "operator plane only" in body
    assert BODY_OMITTED_MARKER in body
    assert "truncated at 64 KB cap" in body  # the redaction reason is surfaced


def test_drawer_trace_pane_empty_state_when_no_trace() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = _seed_audit_row(tenant_id=_TENANT_A)  # no trace seeded
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{audit_id}")

    assert response.status_code == 200, response.text
    body = response.text
    assert "Flight recorder" in body
    assert "No trace captured" in body


def test_drawer_trace_body_is_html_escaped() -> None:
    """Vendor-controlled body text is escaped — never rendered as live markup."""
    _seed_tenant(_TENANT_A, "tenant-a")
    audit_id = _seed_audit_row(tenant_id=_TENANT_A)
    injection = {"note": "<script>alert(1)</script>"}
    _seed_trace(
        audit_id=audit_id,
        tenant_id=_TENANT_A,
        spans=_vendor_and_flux_spans(response_body=injection),
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get(f"/ui/audit/show/{audit_id}")

    assert response.status_code == 200, response.text
    body = response.text
    # The raw <script> tag must never reach the DOM as live markup: Jinja's
    # ``tojson`` escapes ``<`` / ``>`` to unicode inside the rendered JSON.
    assert "<script>alert(1)</script>" not in body
    assert "<script>" not in body
