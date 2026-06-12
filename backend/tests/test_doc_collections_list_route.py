# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``GET /api/v1/doc_collections`` (G4.6-T4 #1553).

The REST sibling of the ``list_doc_collections`` MCP tool + the ``meho docs
collections list`` CLI verb. Coverage:

* **Entitlement filter** — only collections the operator holds
  ``meho-docs:<key>`` for appear; a visible-but-not-entitled collection is
  dropped, so every listed key is one ``search_docs`` will accept. An
  unprovisioned operator (no ``meho-docs:*``) gets an empty list.
* **Tenant scope + dedupe** — global + the tenant's own rows; a
  tenant-curated row shadowing a global key appears once (tenant wins).
* **Vendor filter** — exact-match narrows the catalogue.
* **Keyset pagination** — by ``collection_key``; ``cursor`` resumes.
* **RBAC** — ``read_only`` lists (the read floor is operator and read_only
  is below operator? no — read is allowed; this asserts the operator gate)
  and unauthenticated → 401.
* **Central audit** — one row, ``op_id="meho.docs.collections.list"``,
  ``op_class="read"``.

JWT is minted against the shared OIDC helper so the route's
``require_role(OPERATOR)`` dependency reads a real verified operator
carrying the capabilities under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.doc_collections import router as doc_collections_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_TENANT_ID = UUID("33333333-3333-3333-3333-333333333333")
_OTHER_TENANT_ID = UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads, around every test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


async def _seed(
    *,
    collection_key: str,
    vendor: str = "VMware by Broadcom",
    tenant_id: UUID | None = None,
    status: str = "ready",
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollectionORM(
                tenant_id=tenant_id,
                collection_key=collection_key,
                vendor=vendor,
                products=["vsphere"],
                description=f"{vendor} docs.",
                when_to_use="Product questions.",
                backend={"type": "corpus-http"},
                status=status,
            ),
        )


def _seed_sync(**kwargs: object) -> None:
    asyncio.run(_seed(**kwargs))  # type: ignore[arg-type]


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(doc_collections_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _get(
    client: TestClient,
    *,
    capabilities: list[str],
    tenant_id: UUID = _TENANT_ID,
    role: str = TenantRole.OPERATOR.value,
    params: dict[str, object] | None = None,
) -> object:
    """Mint a token and issue ``GET /api/v1/doc_collections``."""
    # Each call mints with a fresh keypair under the same kid; clear the
    # module-level JWKS cache so a second call in the same test verifies
    # against its own key rather than the first call's cached JWKS.
    clear_jwks_cache()
    key = _make_rsa_keypair("kid-A")
    token = _mint_token(
        key,
        sub="op-1",
        tenant_role=role,
        tenant_id=str(tenant_id),
        capabilities=capabilities,
    )
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.get(
            "/api/v1/doc_collections",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )


async def _audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Entitlement filter
# ---------------------------------------------------------------------------


def test_lists_only_entitled_collections(client: TestClient) -> None:
    """A provisioned operator sees only the collections it is entitled to."""
    _seed_sync(collection_key="vmware", vendor="VMware by Broadcom")
    _seed_sync(collection_key="netapp", vendor="NetApp")
    response = _get(client, capabilities=["meho-docs", "meho-docs:vmware"])
    assert response.status_code == 200  # type: ignore[attr-defined]
    rows = response.json()  # type: ignore[attr-defined]
    assert [r["collection_key"] for r in rows] == ["vmware"]
    assert rows[0]["vendor"] == "VMware by Broadcom"
    # The backend record never appears in the summary shape.
    assert "backend" not in rows[0]


def test_unprovisioned_operator_gets_empty_list(client: TestClient) -> None:
    """No ``meho-docs:*`` capability → empty list (matches CLI hidden UX)."""
    _seed_sync(collection_key="vmware")
    response = _get(client, capabilities=[])
    assert response.status_code == 200  # type: ignore[attr-defined]
    assert response.json() == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tenant scope + dedupe
# ---------------------------------------------------------------------------


def test_tenant_row_shadows_global_key_once(client: TestClient) -> None:
    """A tenant-curated row shadowing a global key appears once — tenant wins."""
    _seed_sync(collection_key="vmware", vendor="Global VMware")
    _seed_sync(collection_key="vmware", vendor="Tenant VMware", tenant_id=_TENANT_ID)
    response = _get(client, capabilities=["meho-docs", "meho-docs:vmware"])
    rows = response.json()  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert rows[0]["vendor"] == "Tenant VMware"


def test_other_tenant_row_is_invisible(client: TestClient) -> None:
    """A row curated by another tenant is out of scope."""
    _seed_sync(collection_key="vmware", vendor="Other", tenant_id=_OTHER_TENANT_ID)
    response = _get(client, capabilities=["meho-docs", "meho-docs:vmware"])
    assert response.json() == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Vendor filter + pagination
# ---------------------------------------------------------------------------


def test_vendor_filter_narrows(client: TestClient) -> None:
    """Exact-match ``vendor`` narrows the catalogue."""
    _seed_sync(collection_key="vmware", vendor="VMware by Broadcom")
    _seed_sync(collection_key="netapp", vendor="NetApp")
    response = _get(
        client,
        capabilities=["meho-docs", "meho-docs:vmware", "meho-docs:netapp"],
        params={"vendor": "NetApp"},
    )
    rows = response.json()  # type: ignore[attr-defined]
    assert [r["collection_key"] for r in rows] == ["netapp"]


def test_vendor_filter_runs_after_dedupe_for_a_shadowed_key(client: TestClient) -> None:
    """``vendor`` filters the tenant-wins row, never the shadowed global one.

    A global key ``vmware`` (vendor ``A``) is shadowed by a tenant-curated
    row for the same key under a different vendor ``B`` (the tenant row
    wins). With the filter applied after the tenant-first dedupe,
    ``vendor=B`` returns the tenant row for ``vmware`` while ``vendor=A``
    does NOT — the shadowed global vendor is not searchable. The old
    SQL-side filter leaked the global row's metadata under ``vendor=A``.
    """
    _seed_sync(collection_key="vmware", vendor="A")
    _seed_sync(collection_key="vmware", vendor="B", tenant_id=_TENANT_ID)
    caps = ["meho-docs", "meho-docs:vmware"]

    by_tenant_vendor = _get(client, capabilities=caps, params={"vendor": "B"})
    tenant_rows = by_tenant_vendor.json()  # type: ignore[attr-defined]
    assert [r["collection_key"] for r in tenant_rows] == ["vmware"]
    assert tenant_rows[0]["vendor"] == "B"

    by_global_vendor = _get(client, capabilities=caps, params={"vendor": "A"})
    assert by_global_vendor.json() == []  # type: ignore[attr-defined]


def test_keyset_pagination(client: TestClient) -> None:
    """``cursor`` resumes after the last key seen."""
    for key in ("alpha", "bravo", "charlie"):
        _seed_sync(collection_key=key, vendor=key.title())
    caps = ["meho-docs", "meho-docs:alpha", "meho-docs:bravo", "meho-docs:charlie"]

    first = _get(client, capabilities=caps, params={"limit": 2})
    assert [r["collection_key"] for r in first.json()] == ["alpha", "bravo"]  # type: ignore[attr-defined]

    second = _get(client, capabilities=caps, params={"limit": 2, "cursor": "bravo"})
    assert [r["collection_key"] for r in second.json()] == ["charlie"]  # type: ignore[attr-defined]


def test_keyset_pagination_with_vendor_filter_after_dedupe(client: TestClient) -> None:
    """The cursor still windows by ``collection_key`` with ``vendor`` set.

    ``alpha`` and ``charlie`` share vendor ``Acme``; ``bravo`` is a
    different vendor. A ``limit=1`` walk filtered to ``Acme`` yields
    ``alpha`` then resumes after it — skipping the filtered-out ``bravo``
    in SQL — to ``charlie``, terminating on the empty page.
    """
    _seed_sync(collection_key="alpha", vendor="Acme")
    _seed_sync(collection_key="bravo", vendor="Other")
    _seed_sync(collection_key="charlie", vendor="Acme")
    caps = ["meho-docs", "meho-docs:alpha", "meho-docs:bravo", "meho-docs:charlie"]

    first = _get(client, capabilities=caps, params={"vendor": "Acme", "limit": 1})
    assert [r["collection_key"] for r in first.json()] == ["alpha"]  # type: ignore[attr-defined]

    second = _get(
        client, capabilities=caps, params={"vendor": "Acme", "limit": 1, "cursor": "alpha"}
    )
    assert [r["collection_key"] for r in second.json()] == ["charlie"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_unauthenticated_is_401(client: TestClient) -> None:
    """A request without a bearer token is rejected 401."""
    response = client.get("/api/v1/doc_collections")
    assert response.status_code == 401


def test_read_only_role_is_403(client: TestClient) -> None:
    """The list floor is OPERATOR — a read_only principal is rejected 403."""
    _seed_sync(collection_key="vmware")
    response = _get(
        client,
        capabilities=["meho-docs", "meho-docs:vmware"],
        role=TenantRole.READ_ONLY.value,
    )
    assert response.status_code == 403  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Central audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_row_carries_canonical_op_id() -> None:
    """One audit row per list call with the canonical op_id."""
    client = TestClient(_build_app())
    await _seed(collection_key="vmware")
    _get(client, capabilities=["meho-docs", "meho-docs:vmware"])

    rows = await _audit_rows()
    list_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.list"]
    assert len(list_rows) == 1
    assert list_rows[0].payload["op_class"] == "read"
