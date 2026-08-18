# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.events_ingest` (#2881).

Acceptance-criteria coverage:

* oversize body -> 413 before the body is buffered (streaming-guard unit +
  Content-Length endpoint path)
* missing / paused source -> uniform 404 (no existence oracle)
* bad signature -> 401
* accepted delivery -> 202 ``{event_id}`` with one ``event_outbox`` row
  (``origin`` = source id, ``dedupe_key`` set) and one synchronous
  ``__ingest__`` audit row (criterion 2)
* duplicate delivery -> 200 with no second outbox/audit row (criterion 1)
* broadcast failure is fail-open (the 202 still lands)
* rate-limit -> 429 with ``Retry-After``
* authenticated invalid JSON -> 400
* deeply-nested within-cap body -> parser fails closed (400, never a 500)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from meho_backplane.api.v1.events_ingest import _read_capped_body, router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EventOutbox, Tenant
from meho_backplane.events.ingest.rate_limit import IngestRateLimitError
from meho_backplane.events.ingest.service import IngestPayloadError, _parse_json_or_fail
from meho_backplane.middleware import RequestContextMiddleware

from ._event_source_helpers import (
    _insert_event_source,
    _settings_env,  # noqa: F401  (autouse fixture)
)
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID

_SECRET_VALUE = "shared-hmac-key"
_SECRET_REF = f"tenants/{DEFAULT_TENANT_ID}/event-sources/prod-am"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


@pytest.fixture(autouse=True)
def _stub_service_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub the Vault read, the rate limiter, and the broadcast for the endpoint.

    The Vault read returns the known secret (no socket); the rate limiter is a
    no-op (its own behaviour is unit-tested); the broadcast is a no-op AsyncMock
    (no Valkey). Individual tests re-patch a seam to drive its failure path.
    """

    async def _read_secret(_operator: object, _ref: str) -> SecretStr:
        return SecretStr(_SECRET_VALUE)

    async def _no_rate_limit(*_a: object, **_k: object) -> None:
        return None

    async def _no_broadcast(_event: object) -> None:
        return None

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.enforce_ingest_rate_limit", _no_rate_limit
    )
    monkeypatch.setattr("meho_backplane.events.ingest.service.publish_event", _no_broadcast)
    yield


async def _seed_tenant() -> None:
    """Insert the default tenant so ``event_outbox.tenant_id`` FK is satisfied."""
    sm = get_sessionmaker()
    async with sm() as session:
        tid = uuid.UUID(DEFAULT_TENANT_ID)
        if await session.get(Tenant, tid) is None:
            session.add(Tenant(id=tid, slug="tenant-a", name="Tenant A"))
            await session.commit()


async def _seed_source(**kwargs: object) -> None:
    await _seed_tenant()
    defaults: dict[str, object] = {
        "slug": "prod-am",
        "name": "prod-am",
        "kind": "alertmanager",
        "auth_strategy": "hmac-sha256",
        "secret_ref": _SECRET_REF,
        "status": "active",
    }
    defaults.update(kwargs)
    await _insert_event_source(**defaults)


def _signed(body: bytes, *, ts: float | None = None, secret: str = _SECRET_VALUE) -> dict[str, str]:
    ts = time.time() if ts is None else ts
    ts_str = f"{ts}"
    sig = hmac.new(secret.encode(), ts_str.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Meho-Signature": sig, "X-Meho-Timestamp": ts_str}


async def _count(model: object) -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# --------------------------------------------------------------------------
# resolution / 404
# --------------------------------------------------------------------------


def test_missing_source_returns_uniform_404(client: TestClient) -> None:
    resp = client.post("/api/v1/events/ingest/nope", content=b"{}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"error": "no_event_source"}


@pytest.mark.asyncio
async def test_paused_source_returns_same_404(client: TestClient) -> None:
    await _seed_source(status="paused")
    body = b'{"x":1}'
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    # Paused is indistinguishable from missing -- same body, no oracle.
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"error": "no_event_source"}


# --------------------------------------------------------------------------
# body cap / 413
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_body_returns_413(client: TestClient) -> None:
    await _seed_source()
    huge = b"x" * (256 * 1024 + 1)
    resp = client.post("/api/v1/events/ingest/prod-am", content=huge, headers=_signed(huge))
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_capped_reader_streams_before_buffering() -> None:
    """The streaming running-total guard rejects even without Content-Length."""

    async def _stream():
        # No content-length header; three chunks that together exceed the cap.
        for _ in range(3):
            yield b"a" * 100

    request = SimpleNamespace(headers={}, stream=_stream)
    with pytest.raises(Exception) as exc:  # _IngestBodyTooLargeError
        await _read_capped_body(request, max_bytes=150)  # type: ignore[arg-type]
    assert type(exc.value).__name__ == "_IngestBodyTooLargeError"


# --------------------------------------------------------------------------
# auth / 401
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_signature_returns_401(client: TestClient) -> None:
    await _seed_source()
    body = b'{"alert":"firing"}'
    headers = _signed(body)
    headers["X-Meho-Signature"] = "0" * 64
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_source_without_secret_fails_closed_401(client: TestClient) -> None:
    await _seed_source(slug="no-secret", name="no-secret", secret_ref=None)
    body = b"{}"
    resp = client.post("/api/v1/events/ingest/no-secret", content=body, headers=_signed(body))
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# accept / 202 + audit + outbox
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_event_publishes_outbox_and_audit(client: TestClient) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert", "severity": "critical"}).encode()
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["deduplicated"] is False
    assert isinstance(payload["event_id"], int)

    sm = get_sessionmaker()
    async with sm() as session:
        outbox = (await session.execute(select(EventOutbox))).scalars().all()
        assert len(outbox) == 1
        row = outbox[0]
        assert row.origin is not None  # source id
        assert row.dedupe_key is not None
        assert row.event_kind == "external.alertmanager.alert"
        assert row.payload["source"]["slug"] == "prod-am"
        assert row.payload["data"] == {"type": "alert", "severity": "critical"}

        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.method == "INGEST")))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        audit = audits[0]
        assert audit.operator_sub == "__ingest__"
        assert audit.path == "events.ingest"
        assert audit.status_code == 202
        assert audit.payload["origin"] == row.origin


@pytest.mark.asyncio
async def test_duplicate_delivery_is_idempotent_200(client: TestClient) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert"}).encode()

    first = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert first.status_code == 202
    # Same body -> same dedupe_key -> the retry is an idempotent ack.
    second = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["event_id"] == first.json()["event_id"]

    # No second outbox row, no second audit row.
    assert await _count(EventOutbox) == 1
    async with get_sessionmaker()() as session:
        n_audit = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.method == "INGEST")
            )
        ).scalar_one()
    assert n_audit == 1


@pytest.mark.asyncio
async def test_broadcast_failure_is_fail_open(client: TestClient, monkeypatch) -> None:
    await _seed_source()

    async def _boom(_event: object) -> None:
        raise RuntimeError("valkey down")

    monkeypatch.setattr("meho_backplane.events.ingest.service.publish_event", _boom)
    body = json.dumps({"type": "alert"}).encode()
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    # The audit row committed before the broadcast, so the delivery still 202s.
    assert resp.status_code == 202
    assert await _count(EventOutbox) == 1


# --------------------------------------------------------------------------
# rate-limit / 429 mapping, invalid payload / 400
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_returns_429_with_retry_after(client: TestClient, monkeypatch) -> None:
    await _seed_source()

    async def _limited(*_a: object, **_k: object) -> None:
        raise IngestRateLimitError(limit=1, window_seconds=60, retry_after_seconds=42)

    monkeypatch.setattr("meho_backplane.events.ingest.service.enforce_ingest_rate_limit", _limited)
    body = b'{"type":"alert"}'
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "42"
    assert resp.json()["detail"]["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_authenticated_invalid_json_returns_400(client: TestClient) -> None:
    await _seed_source()
    body = b"this is not json"
    resp = client.post("/api/v1/events/ingest/prod-am", content=body, headers=_signed(body))
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_payload"
    # Nothing published on a rejected payload.
    assert await _count(EventOutbox) == 0


def test_deeply_nested_body_fails_closed_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body that overflows the JSON parser's recursion guard fails closed (400, never 500).

    ``json.loads`` can raise ``RecursionError`` -- not ``JSONDecodeError`` -- on a
    deeply-nested body. The exact nesting depth that trips the C-stack ceiling is
    environment-dependent (interpreter build, C-stack size, worker thread), so rather
    than rely on a specific depth -- which is flaky across CPython builds -- we force
    ``json.loads`` to raise ``RecursionError`` and assert ``_parse_json_or_fail`` maps
    it to :class:`IngestPayloadError`. The endpoint turns that into its clean 400 -- the
    ``IngestPayloadError`` -> 400 mapping is covered end-to-end by
    :func:`test_authenticated_invalid_json_returns_400`. Before the fix the
    ``RecursionError`` propagated uncaught and the request 500'd.
    """

    def _raise_recursion(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding JSON")

    # ``service`` calls the stdlib ``json.loads``; patch it to raise deterministically.
    monkeypatch.setattr(json, "loads", _raise_recursion)
    with pytest.raises(IngestPayloadError) as exc:
        _parse_json_or_fail(b"[]")
    # It is specifically the recursion path that was caught and re-raised.
    assert isinstance(exc.value.__cause__, RecursionError)
