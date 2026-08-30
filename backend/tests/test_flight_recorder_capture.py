# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder capture-seam tests (#3214): vendor-call spans, F3 caps, F7.

Exercises the generic-connector vendor-call span through the **real** shared
httpx seam (:meth:`HttpConnector._request_json` / ``_post_json``) against a
respx-mocked endpoint, plus the F3 caps (per-span body truncation, span-count
and trace-byte overflow collapse) and the F7 best-effort invariant (a forced
redaction / caps failure never breaks the connector call). The composite,
JSONFlux, and full-dispatch F7 coverage lives in
``test_flight_recorder_capture_dispatch.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.flight_recorder.store import SpanInput
from meho_backplane.redaction.flight_recorder.verdict import (
    BODY_OMITTED_MARKER,
    SECRET_FAMILY_OMITTED_MARKER,
)
from meho_backplane.settings import get_settings

_BASE_URL = "https://vcenter.example.com"


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


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_tenant(*, slug: str, enabled: bool = True) -> UUID:
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        s.add(
            Tenant(
                id=tenant_id,
                slug=slug,
                name=f"Tenant {slug}",
                flight_recorder_enabled=enabled,
            )
        )
        await s.commit()
    return tenant_id


def _make_operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-capture",
        name=None,
        email=None,
        raw_jwt="tok-secret",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


def _make_target() -> Any:
    return SimpleNamespace(
        name="test-target",
        host="vcenter.example.com",
        port=443,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


def _descriptor(op_id: str, *, tags: tuple[str, ...] = (), source_kind: str = "ingested") -> Any:
    return SimpleNamespace(op_id=op_id, tags=list(tags), source_kind=source_kind)


def span_json(span: DispatchTraceSpan) -> str:
    """Flatten a span to a string for leak assertions."""
    return f"{span.name}|{span.attributes}"


class _ConcreteHttpConnector(HttpConnector):
    """Minimal concrete subclass sending a bearer auth header."""

    product = "test-http"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {"Authorization": f"Bearer {operator.raw_jwt}"}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _fetch_trace(audit_id: UUID) -> tuple[DispatchTrace | None, list[DispatchTraceSpan]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        header = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one_or_none()
        if header is None:
            return None, []
        spans = list(
            (
                await s.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == header.id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        return header, spans


async def _run_get(
    conn: _ConcreteHttpConnector,
    *,
    operator: Operator,
    target: Any,
    op_id: str,
    audit_id: UUID,
    path: str = "/api/items",
) -> Any:
    """Open a capture scope, run one GET through the seam, close the scope."""
    handle = await capture.begin_dispatch_capture(
        audit_id=audit_id,
        operator=operator,
        target=target,
        descriptor=_descriptor(op_id),
        connector_id="test-http-1.x",
    )
    try:
        return await conn._get_json(target, path, operator=operator)
    finally:
        await capture.end_dispatch_capture(handle)


# ---------------------------------------------------------------------------
# Vendor-call span: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_call_span_captured_and_redacted() -> None:
    tenant_id = await _seed_tenant(slug="fr-cap-basic")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [1, 2, 3]})
        result = await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    assert result == {"items": [1, 2, 3]}
    header, spans = await _fetch_trace(audit_id)
    assert header is not None
    assert header.redaction_uncertain is False
    assert len(spans) == 1
    span = spans[0]
    assert span.span_kind == "vendor_call"
    assert span.status == "200"
    assert span.name == "GET https://vcenter.example.com/api/items"
    assert span.duration_ms is not None
    assert span.attributes["method"] == "GET"
    assert span.attributes["op_id"] == "svc.items.list"
    # The auth header is stripped unread by the allowlist (fail-closed F2).
    assert "authorization" not in span.attributes["request_headers"]
    # A non-secret response body round-trips redacted.
    assert span.attributes["response_body"] == {"items": [1, 2, 3]}
    assert span.attributes["body_recorded"] is True


@pytest.mark.asyncio
async def test_vendor_call_span_carries_no_query_string_in_url() -> None:
    """The URL is stripped to the request line -- no query values leak."""
    tenant_id = await _seed_tenant(slug="fr-cap-query")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"ok": True})
        handle = await capture.begin_dispatch_capture(
            audit_id=audit_id,
            operator=operator,
            target=target,
            descriptor=_descriptor("svc.items.list"),
            connector_id="test-http-1.x",
        )
        try:
            await conn._request_json(
                target, "GET", "/api/items", operator=operator, params={"token": "sekret"}
            )
        finally:
            await capture.end_dispatch_capture(handle)
    await conn.aclose()

    _, spans = await _fetch_trace(audit_id)
    assert len(spans) == 1
    assert "sekret" not in span_json(spans[0])
    assert "?" not in spans[0].attributes["url"]


# ---------------------------------------------------------------------------
# F3 caps -- body truncation forces redaction-uncertainty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_response_body_truncates_and_marks_uncertain() -> None:
    tenant_id = await _seed_tenant(slug="fr-cap-trunc")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()
    huge = {"blob": "x" * 80_000}  # serializes to >64 KB

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json=huge)
        await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    header, spans = await _fetch_trace(audit_id)
    assert header is not None
    # Truncation ⇒ redaction uncertainty (the engine's rule) ⇒ operator-only.
    assert header.redaction_uncertain is True
    assert len(spans) == 1
    assert spans[0].attributes["truncated"] is True
    assert spans[0].attributes["response_body"] == BODY_OMITTED_MARKER


# ---------------------------------------------------------------------------
# F2 hard-excluded families -- a login/token op never records a body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_family_op_excludes_body() -> None:
    tenant_id = await _seed_tenant(slug="fr-cap-secret")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    handle = await capture.begin_dispatch_capture(
        audit_id=audit_id,
        operator=operator,
        target=target,
        descriptor=_descriptor("svc.session.login"),
        connector_id="test-http-1.x",
    )
    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.post("/login").respond(200, json={"token": "top-secret-value"})
        try:
            await conn._post_json(
                target, "/login", operator=operator, verb="POST", json={"password": "hunter2"}
            )
        finally:
            await capture.end_dispatch_capture(handle)
    await conn.aclose()

    header, spans = await _fetch_trace(audit_id)
    assert header is not None
    # A *placed* secret family is a certain omission -- body blank, not uncertain.
    assert header.redaction_uncertain is False
    assert len(spans) == 1
    assert spans[0].attributes["body_recorded"] is False
    assert spans[0].attributes["request_body"] == SECRET_FAMILY_OMITTED_MARKER
    assert spans[0].attributes["response_body"] == SECRET_FAMILY_OMITTED_MARKER
    assert "top-secret-value" not in span_json(spans[0])
    assert "hunter2" not in span_json(spans[0])


# ---------------------------------------------------------------------------
# F1 -- capture disabled records nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_capture_off_records_nothing() -> None:
    tenant_id = await _seed_tenant(slug="fr-cap-off", enabled=False)
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [1]})
        result = await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    assert result == {"items": [1]}
    header, spans = await _fetch_trace(audit_id)
    assert header is None
    assert spans == []


@pytest.mark.asyncio
async def test_global_kill_switch_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHT_RECORDER_ENABLED", "false")
    get_settings.cache_clear()
    tenant_id = await _seed_tenant(slug="fr-cap-kill")  # tenant is ON ...
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [1]})
        await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    # ... but the global kill switch overrides it (F1 precedence).
    header, _ = await _fetch_trace(audit_id)
    assert header is None


# ---------------------------------------------------------------------------
# F3 caps -- span-count and trace-byte overflow collapse (pure unit)
# ---------------------------------------------------------------------------


def _span(kind: str = "vendor_call", *, attributes: dict[str, Any] | None = None) -> SpanInput:
    return SpanInput(
        span_kind=kind,
        name="span",
        started_at=datetime.now(UTC),
        attributes=attributes or {},
    )


def test_span_count_cap_collapses_overflow() -> None:
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    for _ in range(55):
        scope.add(_span())
    assert len(scope.spans) == 50
    assert scope.overflow["vendor_call"] == 5
    scope.finalize(datetime.now(UTC))
    collapsed = [s for s in scope.spans if s.attributes.get("collapsed")]
    assert len(collapsed) == 1
    assert collapsed[0].attributes["collapsed_count"] == 5
    assert collapsed[0].span_kind == "vendor_call"


def test_trace_byte_cap_collapses_overflow() -> None:
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    big = {"blob": "x" * (600 * 1024)}  # ~600 KB each
    for _ in range(3):
        scope.add(_span(attributes=big))
    # First admitted; the next two would breach the 1 MB trace cap -> collapse.
    assert len(scope.spans) == 1
    assert scope.overflow["vendor_call"] == 2
    scope.finalize(datetime.now(UTC))
    collapsed = [s for s in scope.spans if s.attributes.get("collapsed")]
    assert collapsed[0].attributes["collapsed_count"] == 2


# ---------------------------------------------------------------------------
# F7 -- a forced recorder failure never breaks the connector call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f7_redaction_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redaction fault drops the span but leaves the connector result intact."""

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("redaction engine down")

    monkeypatch.setattr(capture, "redact_span", _boom)
    tenant_id = await _seed_tenant(slug="fr-cap-f7-redact")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [1, 2, 3]})
        result = await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    assert result == {"items": [1, 2, 3]}  # byte-identical, no exception propagated
    _, spans = await _fetch_trace(audit_id)
    assert spans == []  # the span was dropped, not leaked


@pytest.mark.asyncio
async def test_f7_caps_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caps/accounting fault drops the span but leaves the result intact."""

    def _boom(_span: Any) -> int:
        raise RuntimeError("caps module down")

    monkeypatch.setattr(capture, "approx_span_bytes", _boom)
    tenant_id = await _seed_tenant(slug="fr-cap-f7-caps")
    conn = _ConcreteHttpConnector()
    operator = _make_operator(tenant_id)
    target = _make_target()
    audit_id = uuid.uuid4()

    async with respx.mock(base_url=_BASE_URL) as mock:
        mock.get("/api/items").respond(200, json={"items": [7]})
        result = await _run_get(
            conn, operator=operator, target=target, op_id="svc.items.list", audit_id=audit_id
        )
    await conn.aclose()

    assert result == {"items": [7]}
