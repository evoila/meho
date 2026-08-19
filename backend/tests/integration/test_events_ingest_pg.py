# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres coverage for the inbound ingest endpoint (#2881).

The SQLite unit suite (:mod:`tests.test_api_v1_events_ingest`) proves the
endpoint's logic; this module runs the two most dialect-sensitive paths --
the same-transaction ``event_outbox`` + ``__ingest__`` ``audit_log`` commit,
and the ``(tenant_id, dedupe_key)`` partial-unique dedupe collision -- against
a real ``pgvector/pgvector:pg16`` container so the JSONB envelope round-trip,
the real ``event_source`` / ``event_outbox`` FKs to ``tenant``, and the
production dialect's ``IntegrityError`` on the partial unique index are all
exercised on Postgres.

The ``pg_engine`` fixture from :mod:`tests.integration.conftest` points the
process-wide engine at the container, re-seeds the two pinned ``tenant`` rows
(``tenant-a`` = ``1111...``), and skips the module when Docker is unavailable
(agent sandboxes), so this runs in CI where containers are provisioned. The
source is registered under the pinned ``tenant-a`` so its ``event_outbox`` /
``event_source`` FKs resolve.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy import func, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EventOutbox
from tests._event_source_helpers import _insert_event_source
from tests.test_api_v1_events_ingest import _build_app, _count, _signed

#: The pinned ``tenant-a`` row the integration conftest re-seeds per test.
_PINNED_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SECRET_REF = f"tenants/{_PINNED_TENANT}/event-sources/prod-am"


@pytest.fixture(autouse=True)
def _stub_service_seams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub the Vault read / rate limiter / broadcast (no external sockets)."""

    async def _read_secret(_operator: object, _ref: str) -> SecretStr:
        return SecretStr("shared-hmac-key")

    async def _no_op(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(
        "meho_backplane.events.ingest.service.read_event_source_secret", _read_secret
    )
    monkeypatch.setattr("meho_backplane.events.ingest.service.enforce_ingest_rate_limit", _no_op)
    monkeypatch.setattr("meho_backplane.events.ingest.service.publish_event", _no_op)
    yield


async def _seed_source() -> None:
    """Register the source under the pre-seeded pinned ``tenant-a``."""
    await _insert_event_source(
        tenant_id=_PINNED_TENANT,
        slug="prod-am",
        name="prod-am",
        kind="alertmanager",
        auth_strategy="hmac-sha256",
        secret_ref=_SECRET_REF,
        status="active",
    )


def _async_client() -> httpx.AsyncClient:
    """Drive the app in-process on the test's event loop.

    ASGITransport keeps the request handler and the ``pg_engine``-bound
    asyncpg pool on the same loop -- a threaded ``TestClient`` would run the
    handler on its own loop and asyncpg rejects the cross-loop use.
    """
    return httpx.AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_accepted_event_commits_outbox_and_audit_on_pg(pg_engine: None) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert", "severity": "critical"}).encode()

    async with _async_client() as client:
        resp = await client.post(
            "/api/v1/events/ingest/prod-am", content=body, headers=_signed(body)
        )
    assert resp.status_code == 202

    sm = get_sessionmaker()
    async with sm() as session:
        outbox = (await session.execute(select(EventOutbox))).scalars().all()
        assert len(outbox) == 1
        assert outbox[0].origin is not None
        assert outbox[0].dedupe_key is not None
        # JSONB envelope round-trips through the real dialect. The synthetic
        # body has no Alertmanager ``status``, so it lands under ``raw`` with
        # an empty match set (#2882 normaliser).
        assert outbox[0].payload["raw"] == {"type": "alert", "severity": "critical"}

        audits = (
            (await session.execute(select(AuditLog).where(AuditLog.method == "INGEST")))
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].operator_sub == "__ingest__"
        assert audits[0].tenant_id == _PINNED_TENANT


@pytest.mark.asyncio
async def test_duplicate_delivery_dedupes_on_pg_partial_unique(pg_engine: None) -> None:
    await _seed_source()
    body = json.dumps({"type": "alert"}).encode()

    async with _async_client() as client:
        first = await client.post(
            "/api/v1/events/ingest/prod-am", content=body, headers=_signed(body)
        )
        assert first.status_code == 202
        # Same body -> same dedupe_key -> the PG partial unique index collides,
        # the IntegrityError maps to an idempotent 200 with the original id.
        second = await client.post(
            "/api/v1/events/ingest/prod-am", content=body, headers=_signed(body)
        )
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["event_id"] == first.json()["event_id"]

    assert await _count(EventOutbox) == 1
    async with get_sessionmaker()() as session:
        n_audit = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.method == "INGEST")
            )
        ).scalar_one()
    assert n_audit == 1
