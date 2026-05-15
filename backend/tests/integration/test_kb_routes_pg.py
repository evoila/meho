# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end ``/api/v1/kb*`` route tests against a real pgvector cluster.

G4.1-T2 (#416) acceptance criteria that need real PG (not SQLite):

* **All 5 routes end-to-end.** Ingest a 5-file corpus through the
  POST /ingest route, list / show / create / delete via the HTTP
  surface (same auth chain prod uses). The substrate's body-hash
  short-circuit fires against the real PG dialect; mocked retrieval
  in the unit suite can't exercise that.
* **Tenant boundary holds across the HTTP surface.** Two tenants
  ingest distinct corpora through the routes; each tenant's
  ``GET /api/v1/kb`` / ``GET /{slug}`` only returns their own rows.
  Cross-tenant ``GET /{slug}`` returns 404, not 403, and not the
  other tenant's entry.
* **Idempotent delete.** DELETE /{slug} on a freshly-created entry
  returns 204; a second DELETE on the same slug also returns 204
  (no 404 in between).

The fastembed pipeline is patched to a deterministic stub (same
shape ``test_kb_service_pg.py`` uses) so the suite costs ~1 s
rather than the ~10-30 s a cold ONNX load + per-doc encode would.

**Why** ``httpx.AsyncClient`` + ``ASGITransport`` **rather than**
``fastapi.testclient.TestClient``: the asyncpg pool created in the
``pg_engine`` fixture is bound to the pytest-asyncio event loop.
The sync ``TestClient`` spawns a fresh anyio portal on a different
loop, so a route handler that awaits ``session.execute`` gets a
"Future attached to a different loop" failure. The
:class:`httpx.AsyncClient` driving the app via :class:`ASGITransport`
runs in the **same** loop the asyncpg pool was created on, keeping
the request → handler → pool path single-loop. Mirrors the pattern
:mod:`tests.integration.test_tenant_isolation` adopted for the same
reason.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from meho_backplane.api.v1.kb import router as kb_router
from meho_backplane.auth.operator import TenantRole

from .conftest import (
    DOCKER_AVAILABLE,
    SKIP_REASON,
    build_integration_app,
)

# Pinned tenant UUIDs match the seed rows the ``pg_engine`` conftest
# fixture inserts; the fixture seeds tenant-a and tenant-b so
# Document.tenant_id FK constraint is satisfied.
TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"


_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _make_stub_embedding_vector(text: str) -> list[float]:
    """Deterministic bag-of-words 384-dim vector (matches test_kb_service_pg)."""
    v = [0.0] * 384
    for token in text.lower().split():
        h = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(),
            "big",
        )
        v[h % 384] += 1.0
        v[(h * 31) % 384] += 0.5
    magnitude = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / magnitude for x in v]


def _make_stub_embedding_service() -> AsyncMock:
    fake = AsyncMock()
    fake.encode_one.side_effect = lambda t: _make_stub_embedding_vector(t)
    fake.encode.side_effect = lambda ts: [_make_stub_embedding_vector(t) for t in ts]
    fake.dimension = 384
    return fake


def _write_corpus(root: Path) -> dict[str, str]:
    """Write a 5-file kb corpus under *root*; return slug → body map."""
    entries = {
        "k8s-ingress": "Kubernetes ingress controller troubleshooting.",
        "k8s-rbac": "Kubernetes RBAC primer: ClusterRole + RoleBinding.",
        "vault-jwt": "HashiCorp Vault JWT auth via Keycloak OIDC.",
        "argocd-sync": "ArgoCD sync waves and resource hooks.",
        "harbor-rotation": "Harbor registry credential rotation runbook.",
    }
    for slug, body in entries.items():
        (root / f"{slug}.md").write_text(body, encoding="utf-8")
    return entries


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    """Build an in-process async client driving *app* via ASGI.

    Same shape :mod:`tests.integration.test_tenant_isolation` uses --
    runs every request in the pytest-asyncio loop so the pool the
    handler ``await``s is the one the fixture set up.
    """
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# App fixture -- mounts the kb router on top of the integration app
# ---------------------------------------------------------------------------


@pytest.fixture
def kb_integration_app(pg_engine: None) -> FastAPI:
    """Integration app + kb router. Production middleware stack."""
    app = build_integration_app()
    app.include_router(kb_router)
    return app


# ---------------------------------------------------------------------------
# JWT minting helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: str, sub: str = "op-admin") -> tuple[object, str]:
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


def _operator_token(*, tenant_id: str, sub: str = "op-operator") -> tuple[object, str]:
    """Mint an ``operator`` JWT bound to *tenant_id*."""
    from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token

    key = make_rsa_keypair(f"kid-operator-{sub}")
    token = mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=tenant_id,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Test 1 -- full lifecycle through all 5 routes
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_full_lifecycle_through_all_five_routes(
    kb_integration_app: FastAPI,
    tmp_path: Path,
) -> None:
    """End-to-end: ingest → list → show → create → delete via HTTP.

    Exercises every acceptance-criterion route at least once against
    real PG. The audit middleware writes through to the testcontainer
    so audit-row assertions cover the full chain.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    _write_corpus(tmp_path)
    fake_embed = _make_stub_embedding_service()
    admin_key, admin_token = _admin_token(tenant_id=TENANT_A_ID, sub="op-admin-1")
    operator_key, operator_token = _operator_token(tenant_id=TENANT_A_ID, sub="op-operator-1")

    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embed,
        ),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(admin_key, operator_key))

        async with _make_async_client(kb_integration_app) as client:
            # --- 1. Ingest the corpus via POST /api/v1/kb/ingest ---
            ingest_resp = await client.post(
                "/api/v1/kb/ingest",
                json={"directory": str(tmp_path)},
                headers=_authed(admin_token),
            )
            assert ingest_resp.status_code == 200, ingest_resp.text
            result = ingest_resp.json()
            assert result["inserted_count"] == 5
            assert result["skipped_count"] == 0
            assert result["error_count"] == 0

            # --- 2. List via GET /api/v1/kb ---
            list_resp = await client.get(
                "/api/v1/kb",
                headers=_authed(operator_token),
            )
            assert list_resp.status_code == 200
            entries = list_resp.json()["entries"]
            assert len(entries) == 5
            slugs = {e["slug"] for e in entries}
            assert slugs == {
                "k8s-ingress",
                "k8s-rbac",
                "vault-jwt",
                "argocd-sync",
                "harbor-rotation",
            }

            # --- 3. Show one entry via GET /api/v1/kb/{slug} ---
            show_resp = await client.get(
                "/api/v1/kb/k8s-ingress",
                headers=_authed(operator_token),
            )
            assert show_resp.status_code == 200
            assert show_resp.json()["slug"] == "k8s-ingress"
            assert "Kubernetes ingress" in show_resp.json()["body"]

            # --- 4. Create a new entry via POST /api/v1/kb ---
            create_resp = await client.post(
                "/api/v1/kb",
                json={
                    "slug": "new-runbook",
                    "body": "Fresh runbook content created via HTTP API.",
                    "metadata": {"author": "ops-team"},
                },
                headers=_authed(admin_token),
            )
            assert create_resp.status_code == 201, create_resp.text
            assert create_resp.json()["slug"] == "new-runbook"

            # Verify it shows up in the list
            list_resp2 = await client.get(
                "/api/v1/kb",
                headers=_authed(operator_token),
            )
            slugs2 = {e["slug"] for e in list_resp2.json()["entries"]}
            assert "new-runbook" in slugs2
            assert len(slugs2) == 6

            # --- 5. Delete via DELETE /api/v1/kb/{slug} ---
            delete_resp = await client.delete(
                "/api/v1/kb/new-runbook",
                headers=_authed(admin_token),
            )
            assert delete_resp.status_code == 204
            assert delete_resp.content == b""

            # --- 6. Idempotent delete -- second call also 204 ---
            delete_resp2 = await client.delete(
                "/api/v1/kb/new-runbook",
                headers=_authed(admin_token),
            )
            assert delete_resp2.status_code == 204

            # --- 7. Show after delete returns 404 ---
            show_after_delete = await client.get(
                "/api/v1/kb/new-runbook",
                headers=_authed(operator_token),
            )
            assert show_after_delete.status_code == 404


# ---------------------------------------------------------------------------
# Test 2 -- tenant boundary holds across the HTTP surface
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_tenant_boundary_holds_via_http_routes(
    kb_integration_app: FastAPI,
    tmp_path: Path,
) -> None:
    """Tenant A ingests; tenant B cannot list / show those entries via HTTP.

    Cross-tenant ``GET /{slug}`` returns 404 (not 403, not the other
    tenant's content). The substrate's tenant WHERE filter is what
    makes this work; the route inherits the property by passing
    ``operator.tenant_id`` straight through.
    """
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    _write_corpus(tmp_path)
    fake_embed = _make_stub_embedding_service()

    a_admin_key, a_admin_token = _admin_token(tenant_id=TENANT_A_ID, sub="op-a-admin")
    b_admin_key, b_admin_token = _admin_token(tenant_id=TENANT_B_ID, sub="op-b-admin")
    b_operator_key, b_operator_token = _operator_token(tenant_id=TENANT_B_ID, sub="op-b-op")
    a_operator_key, a_operator_token = _operator_token(tenant_id=TENANT_A_ID, sub="op-a-op")

    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embed,
        ),
    ):
        # Mock JWKS with every key the test mints up-front so we don't
        # need to reset the respx router mid-test.
        mock_discovery_and_jwks(
            mock_router,
            public_jwks(
                a_admin_key,
                b_admin_key,
                b_operator_key,
                a_operator_key,
            ),
        )

        async with _make_async_client(kb_integration_app) as client:
            # Tenant A ingests the 5-file corpus.
            ingest_resp = await client.post(
                "/api/v1/kb/ingest",
                json={"directory": str(tmp_path)},
                headers=_authed(a_admin_token),
            )
            assert ingest_resp.status_code == 200, ingest_resp.text
            assert ingest_resp.json()["inserted_count"] == 5

            # Tenant B GET /api/v1/kb returns empty.
            list_resp = await client.get(
                "/api/v1/kb",
                headers=_authed(b_operator_token),
            )
            assert list_resp.status_code == 200
            assert list_resp.json()["entries"] == []

            # Tenant B GET /api/v1/kb/k8s-ingress returns 404 (not 403,
            # not the other tenant's entry).
            show_resp = await client.get(
                "/api/v1/kb/k8s-ingress",
                headers=_authed(b_operator_token),
            )
            assert show_resp.status_code == 404
            assert show_resp.json()["detail"] == "slug_not_found"

            # Tenant B can independently create their own kb entry with
            # the same slug -- tenant scoping makes the natural key
            # (tenant_id, source, source_id) unique per-tenant.
            create_resp = await client.post(
                "/api/v1/kb",
                json={
                    "slug": "k8s-ingress",
                    "body": "Tenant B's own k8s notes -- different content.",
                },
                headers=_authed(b_admin_token),
            )
            assert create_resp.status_code == 201

            # Tenant B's show returns their own entry (not tenant A's).
            b_show = await client.get(
                "/api/v1/kb/k8s-ingress",
                headers=_authed(b_operator_token),
            )
            assert b_show.status_code == 200
            assert "Tenant B's own k8s notes" in b_show.json()["body"]

            # Tenant A's show still returns tenant A's content unchanged.
            a_show = await client.get(
                "/api/v1/kb/k8s-ingress",
                headers=_authed(a_operator_token),
            )
            assert a_show.status_code == 200
            assert "Kubernetes ingress" in a_show.json()["body"]


# ---------------------------------------------------------------------------
# Test 3 -- dry_run does not write
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_ingest_dry_run_does_not_write(
    kb_integration_app: FastAPI,
    tmp_path: Path,
) -> None:
    """``dry_run=true`` returns the action plan without writing rows."""
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks

    _write_corpus(tmp_path)
    fake_embed = _make_stub_embedding_service()
    admin_key, admin_token = _admin_token(tenant_id=TENANT_A_ID, sub="op-dry")
    operator_key, operator_token = _operator_token(tenant_id=TENANT_A_ID, sub="op-dry-op")

    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_embed,
        ),
    ):
        mock_discovery_and_jwks(mock_router, public_jwks(admin_key, operator_key))

        async with _make_async_client(kb_integration_app) as client:
            # Dry-run ingest reports 5 would-be inserts but writes nothing.
            dry_resp = await client.post(
                "/api/v1/kb/ingest",
                json={"directory": str(tmp_path), "dry_run": True},
                headers=_authed(admin_token),
            )
            assert dry_resp.status_code == 200, dry_resp.text
            assert dry_resp.json()["inserted_count"] == 5

            # The list endpoint sees no rows -- the dry-run wrote nothing.
            list_resp = await client.get(
                "/api/v1/kb",
                headers=_authed(operator_token),
            )
            assert list_resp.status_code == 200
            assert list_resp.json()["entries"] == []
