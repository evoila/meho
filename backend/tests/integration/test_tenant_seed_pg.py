# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G0.8-T1 acceptance — JIT tenant seeding unblocks the first real write.

Initiative #634 (G0.8) DoD line 1, Task #628: a clean-room v0.2
deploy whose ``tenant`` table is empty must be able to perform a real
(non-dry-run) tenant-scoped write. Before this Task, the first such
write hit ``asyncpg.ForeignKeyViolationError`` on
``documents_tenant_id_fkey`` because nothing in the request path or
the runbooks seeded the ``tenant`` row.

The acceptance smoke stayed green only because every smoke ingest is
a ``dry_run`` (classify, no write) — exactly why this FK wall stayed
invisible until the consumer's first real write. These tests close
that gap by exercising the **real** write path against real Postgres
through the production middleware chain:

* ``test_first_real_ingest_succeeds_on_empty_tenant_table`` — the
  clean-room AC: empty ``tenant`` table, first non-dry-run
  ``POST /api/v1/kb/ingest`` returns 200 and the rows persist; the
  ``tenant`` row was seeded just-in-time by
  :func:`meho_backplane.tenancy.ensure_tenant`.
* ``test_concurrent_first_writes_seed_exactly_one_tenant_row`` —
  idempotency under the race the consumer would actually hit: N
  concurrent first requests for the same fresh ``tenant_id`` all
  attempt the seed; ``ON CONFLICT (id) DO NOTHING`` means exactly
  one ``tenant`` row lands.

The SQLite half of the matrix (idempotency, no-overwrite of an
existing row, dialect dispatch) lives in
:mod:`tests.test_tenancy_ensure`; this module is the PG-real,
HTTP-surface gate.

``httpx.AsyncClient`` + ``ASGITransport`` (not ``TestClient``) for
the same single-event-loop reason :mod:`tests.integration.test_kb_routes_pg`
documents: the asyncpg pool the ``pg_engine_empty_tenant`` fixture
creates is bound to the pytest-asyncio loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import func, select

from meho_backplane.api.v1.kb import router as kb_router
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant

from .conftest import (
    DOCKER_AVAILABLE,
    SKIP_REASON,
    build_integration_app,
)

_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

# A fresh tenant UUID with no row in the (truncated, un-seeded)
# ``tenant`` table — the clean-room deploy condition. Deliberately
# distinct from the ``TENANT_A_ID`` / ``TENANT_B_ID`` the seeded
# ``pg_engine`` fixture uses, so a regression that accidentally
# re-seeds would not mask the assertion.
FRESH_TENANT_ID: str = "9b7c2e10-3d44-4f6a-91b5-1de8c7a92f04"


def _make_stub_embedding_vector(text: str) -> list[float]:
    """Deterministic 384-d stub vector (same shape the kb route suite uses)."""
    seed = float(abs(hash(text)) % 1000) / 1000.0
    return [seed] * 384


def _make_stub_embedding_service() -> AsyncMock:
    fake = AsyncMock()
    fake.encode_one.side_effect = lambda t: _make_stub_embedding_vector(t)
    fake.encode.side_effect = lambda ts: [_make_stub_embedding_vector(t) for t in ts]
    fake.dimension = 384
    return fake


def _write_corpus(root: Path) -> dict[str, str]:
    """Write a small kb corpus under *root*; return slug → body map."""
    entries = {
        "k8s-ingress": "Kubernetes ingress controller troubleshooting.",
        "vault-jwt": "HashiCorp Vault JWT auth via Keycloak OIDC.",
    }
    for slug, body in entries.items():
        (root / f"{slug}.md").write_text(body, encoding="utf-8")
    return entries


def _admin_token(*, tenant_id: str, sub: str) -> tuple[object, str]:
    """Mint a ``tenant_admin`` JWT bound to *tenant_id*."""
    from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token

    key = make_rsa_keypair(f"kid-admin-{sub}")
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=tenant_id,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest.fixture
def clean_room_app(pg_engine_empty_tenant: None) -> FastAPI:
    """Integration app + kb router with an **empty** ``tenant`` table."""
    app = build_integration_app()
    app.include_router(kb_router)
    return app


async def _count_tenant_rows(tenant_id: UUID) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == tenant_id),
        )
        return int(result.scalar_one())


@_skip_no_docker
async def test_first_real_ingest_succeeds_on_empty_tenant_table(
    clean_room_app: FastAPI,
    tmp_path: Path,
) -> None:
    """Clean-room AC: empty ``tenant`` table, first real ingest persists.

    Before #628 this raised ``ForeignKeyViolationError`` on
    ``documents_tenant_id_fkey``. The assertion is the 200 + the
    persisted document rows + the just-in-time-seeded ``tenant`` row.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    _write_corpus(tmp_path)
    fake_embed = _make_stub_embedding_service()
    admin_key, admin_token = _admin_token(tenant_id=FRESH_TENANT_ID, sub="op-fresh-1")
    fresh_uuid = UUID(FRESH_TENANT_ID)

    # Precondition: the clean-room fixture left the table empty.
    assert await _count_tenant_rows(fresh_uuid) == 0

    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embed,
        ),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(admin_key))

        async with _make_async_client(clean_room_app) as client:
            resp = await client.post(
                "/api/v1/kb/ingest",
                json={"directory": str(tmp_path)},
                headers=_authed(admin_token),
            )

    # The load-bearing assertion: a real (non-dry-run) write succeeds
    # rather than 500-ing on the FK violation.
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["inserted_count"] == 2
    assert result["error_count"] == 0

    # The tenant row was seeded just-in-time — exactly one, derived
    # slug, never an out-of-band fixture insert.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(Tenant).where(Tenant.id == fresh_uuid))).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].slug == f"tenant-{fresh_uuid}"
    assert rows[0].name == f"tenant-{fresh_uuid}"


@_skip_no_docker
async def test_concurrent_first_writes_seed_exactly_one_tenant_row(
    clean_room_app: FastAPI,
    tmp_path: Path,
) -> None:
    """Idempotency under the real race: N concurrent first writes → one row.

    ``ON CONFLICT DO NOTHING`` arbitrated against *every* unique index
    (no named ``index_elements``) is what makes the concurrent
    first-write safe. Fire several real ingests for the same fresh
    ``tenant_id`` at once; every one must return 200 and the ``tenant``
    table must hold exactly one matching row afterwards.

    Regression guard for #983: when the arbiter named only ``id``,
    concurrent same-tenant inserts intermittently raised a
    ``unique_violation`` on the non-arbiter ``tenant_slug_idx`` (the
    slug-index conflict bypassed the ``id`` arbiter's
    speculative-insertion wait), turning one of the 8 ingests into a
    500 and failing the all-200 assertion. The bare arbiter closes
    that window.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    _write_corpus(tmp_path)
    fake_embed = _make_stub_embedding_service()
    admin_key, admin_token = _admin_token(tenant_id=FRESH_TENANT_ID, sub="op-race-1")
    fresh_uuid = UUID(FRESH_TENANT_ID)

    assert await _count_tenant_rows(fresh_uuid) == 0

    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embed,
        ),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(admin_key))

        async with _make_async_client(clean_room_app) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/kb/ingest",
                        json={"directory": str(tmp_path), "dry_run": True},
                        headers=_authed(admin_token),
                    )
                    for _ in range(8)
                )
            )

    # Every concurrent first request seeds-or-no-ops without raising;
    # dry_run keeps the assertion focused on the seed path (no document
    # writes to reason about), which is the idempotency contract under
    # test here.
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    assert await _count_tenant_rows(fresh_uuid) == 1
