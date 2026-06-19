# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.conventions`.

Coverage matrix (Task #314 / G7.1-T2 acceptance criteria):

* Happy path: POST creates a row + history row + 201; GET lists own
  tenant; GET/{slug} returns the row; PATCH updates + appends a
  history row; DELETE removes the row + appends a history row +
  204; GET/{slug}/history returns chronological list (newest
  first).
* Tenant isolation: tenant A cannot list / show / patch / delete /
  history tenant B's conventions (404 across the board, no
  existence leak).
* RBAC: ``operator`` reads OK, writes 403; ``read_only`` reads
  blocked (operator-minimum); ``tenant_admin`` everywhere OK.
* 409 on duplicate ``(tenant_id, slug)`` create.
* 422 on a single ``operational`` body exceeding the preamble
  budget; same body as ``workflow`` / ``reference`` accepted
  (preamble-unbound kinds are exempt).
* PATCH body change against an oversize ``operational`` row trips
  the same 422.
* ``priority`` round-trips through create + show.
* Every PATCH and DELETE writes one history row + one audit row
  per call. The history row's ``audit_id`` matches the audit
  row's ``id`` -- the contextvar-binding contract this Task adds
  to the AuditMiddleware.
* History list returns newest first.

The tests drive the production ``meho_backplane.main:app`` so the
real middleware chain (RequestContext → BroadcastDetail → Audit →
router) is exercised. DB is the autouse ``_default_database_url``
fixture's per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.conventions.schemas import (
    DEFAULT_MAX_PREAMBLE_TOKENS,
    TOKEN_CHAR_RATIO,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    Tenant,
    TenantConvention,
    TenantConventionHistory,
)
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
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


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
    )


async def _seed_tenants() -> None:
    """Insert the two test tenants if they don't already exist."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            existing = await session.execute(select(Tenant).where(Tenant.id == tid))
            if existing.scalar_one_or_none() is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _fetch_conventions(tenant_id: UUID) -> list[TenantConvention]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConvention)
            .where(TenantConvention.tenant_id == tenant_id)
            .order_by(TenantConvention.created_at),
        )
        return list(result.scalars().all())


async def _fetch_history(convention_id: UUID) -> list[TenantConventionHistory]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(TenantConventionHistory)
            .where(TenantConventionHistory.convention_id == convention_id)
            .order_by(TenantConventionHistory.ts),
        )
        return list(result.scalars().all())


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _post_convention(
    client: TestClient,
    token: str,
    *,
    slug: str = "rbac-canonical",
    title: str = "Vault is canonical",
    body: str = "RBAC and secrets land in Vault, not 1Password.",
    kind: str = "operational",
    priority: int | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "slug": slug,
        "title": title,
        "body": body,
        "kind": kind,
    }
    if priority is not None:
        payload["priority"] = priority
    return client.post(
        "/api/v1/conventions",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# Happy path -- POST + GET + PATCH + DELETE + history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_convention_returns_201(client: TestClient) -> None:
    """``POST /api/v1/conventions`` returns 201 + the new row body."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-post")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token, priority=5)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "rbac-canonical"
    assert body["title"] == "Vault is canonical"
    assert body["body"] == "RBAC and secrets land in Vault, not 1Password."
    assert body["kind"] == "operational"
    assert body["priority"] == 5
    assert body["created_by_sub"] == "op-admin"
    assert UUID(body["id"])
    rows = await _fetch_conventions(_TENANT_A)
    assert len(rows) == 1
    # The CREATE event also wrote one history row with body_before=NULL.
    history = await _fetch_history(rows[0].id)
    assert len(history) == 1
    assert history[0].body_before is None
    assert history[0].body_after == "RBAC and secrets land in Vault, not 1Password."
    assert history[0].actor_sub == "op-admin"


@pytest.mark.asyncio
async def test_priority_defaults_to_zero(client: TestClient) -> None:
    """Omitting ``priority`` round-trips as 0."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-prio-default")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token)
    assert resp.status_code == 201, resp.text
    assert resp.json()["priority"] == 0


@pytest.mark.asyncio
async def test_get_lists_own_tenant_conventions_priority_desc(
    client: TestClient,
) -> None:
    """``GET`` returns the operator's tenant's rows ordered priority DESC."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-get")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="low-prio", priority=1)
        _post_convention(client, token, slug="high-prio", priority=9)
        _post_convention(client, token, slug="mid-prio", priority=5)
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "entries" in body
    slugs = [row["slug"] for row in body["entries"]]
    assert slugs == ["high-prio", "mid-prio", "low-prio"]
    # ``ConventionSummary`` shape (no body field).
    assert "body" not in body["entries"][0]


@pytest.mark.asyncio
async def test_get_filters_by_kind(client: TestClient) -> None:
    """``?kind=operational`` filters to operational rows only."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-filter")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="op-rule", kind="operational")
        _post_convention(client, token, slug="wf-rule", kind="workflow")
        _post_convention(client, token, slug="ref-rule", kind="reference")
        resp = client.get(
            "/api/v1/conventions?kind=operational",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    slugs = {row["slug"] for row in resp.json()["entries"]}
    assert slugs == {"op-rule"}


@pytest.mark.asyncio
async def test_show_returns_full_row(client: TestClient) -> None:
    """``GET /{slug}`` returns the full :class:`Convention` shape including ``body``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-show")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="show-test", body="Show body text.")
        resp = client.get(
            "/api/v1/conventions/show-test",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == "Show body text."


@pytest.mark.asyncio
async def test_show_missing_slug_returns_404(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-show-404")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/conventions/does-not-exist",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "convention_not_found"


@pytest.mark.asyncio
async def test_patch_updates_body_and_writes_history(client: TestClient) -> None:
    """``PATCH`` updates the row + appends a history row with the diff."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-patch")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(
            client,
            token,
            slug="patch-test",
            body="Original body.",
        )
        resp = client.patch(
            "/api/v1/conventions/patch-test",
            json={"body": "Updated body."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == "Updated body."
    rows = await _fetch_conventions(_TENANT_A)
    assert len(rows) == 1
    history = await _fetch_history(rows[0].id)
    # Two history rows: CREATE + UPDATE.
    assert len(history) == 2
    update = history[-1]
    assert update.body_before == "Original body."
    assert update.body_after == "Updated body."


@pytest.mark.asyncio
async def test_patch_only_priority_writes_history(client: TestClient) -> None:
    """Priority-only PATCH still writes a history row (the causal record matters)."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-patch-prio")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="prio-bump", priority=1)
        resp = client.patch(
            "/api/v1/conventions/prio-bump",
            json={"priority": 7},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["priority"] == 7
    rows = await _fetch_conventions(_TENANT_A)
    history = await _fetch_history(rows[0].id)
    # CREATE + PATCH = 2 history rows; the PATCH carries identical
    # body_before / body_after (the operation happened even when the
    # body text didn't move).
    assert len(history) == 2
    assert history[-1].body_before == history[-1].body_after


@pytest.mark.asyncio
async def test_patch_missing_slug_returns_404(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-patch-404")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.patch(
            "/api/v1/conventions/does-not-exist",
            json={"body": "Whatever."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_row_and_writes_history(client: TestClient) -> None:
    """``DELETE`` removes the row + appends a history row with body_after=<final>."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-delete")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(
            client,
            token,
            slug="delete-test",
            body="Final body before delete.",
        )
        # Capture the convention_id before delete.
        rows_before = await _fetch_conventions(_TENANT_A)
        convention_id = rows_before[0].id
        resp = client.delete(
            "/api/v1/conventions/delete-test",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 204
    assert resp.text == ""
    rows_after = await _fetch_conventions(_TENANT_A)
    assert rows_after == []
    history = await _fetch_history(convention_id)
    assert len(history) == 2  # CREATE + DELETE
    delete_row = history[-1]
    assert delete_row.body_after == "Final body before delete."


@pytest.mark.asyncio
async def test_delete_missing_slug_returns_404(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-delete-404")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            "/api/v1/conventions/does-not-exist",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_history_returns_newest_first(client: TestClient) -> None:
    """``GET /{slug}/history`` returns rows ordered by ``ts DESC``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-history")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="hist-test", body="v1")
        client.patch(
            "/api/v1/conventions/hist-test",
            json={"body": "v2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.patch(
            "/api/v1/conventions/hist-test",
            json={"body": "v3"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(
            "/api/v1/conventions/hist-test/history",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    history = resp.json()
    assert len(history) == 3
    # Newest first: body_after on the first item is v3.
    assert history[0]["body_after"] == "v3"
    assert history[-1]["body_after"] == "v1"
    # CREATE row has body_before=None.
    assert history[-1]["body_before"] is None


@pytest.mark.asyncio
async def test_history_missing_slug_returns_404(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-history-404")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/conventions/does-not-exist/history",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_post(
    client: TestClient,
    role: TenantRole,
) -> None:
    """``operator`` and ``read_only`` cannot create conventions."""
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-rbac-post-{role.value}")
    token = _token(key, role=role)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_patch(
    client: TestClient,
    role: TenantRole,
) -> None:
    await _seed_tenants()
    admin_key = make_rsa_keypair(f"kid-admin-for-{role.value}-patch")
    admin_token = _token(admin_key)
    other_key = make_rsa_keypair(f"kid-rbac-patch-{role.value}")
    other_token = _token(other_key, sub=f"op-{role.value}", role=role)
    with respx.mock as r:
        combined_jwks = {
            "keys": [
                public_jwks(admin_key)["keys"][0],
                public_jwks(other_key)["keys"][0],
            ],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, admin_token, slug=f"rbac-{role.value}")
        resp = client.patch(
            f"/api/v1/conventions/rbac-{role.value}",
            json={"body": "Should not write."},
            headers={"Authorization": f"Bearer {other_token}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_delete(
    client: TestClient,
    role: TenantRole,
) -> None:
    await _seed_tenants()
    admin_key = make_rsa_keypair(f"kid-admin-for-{role.value}-del")
    admin_token = _token(admin_key)
    other_key = make_rsa_keypair(f"kid-rbac-del-{role.value}")
    other_token = _token(other_key, sub=f"op-{role.value}", role=role)
    with respx.mock as r:
        combined_jwks = {
            "keys": [
                public_jwks(admin_key)["keys"][0],
                public_jwks(other_key)["keys"][0],
            ],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, admin_token, slug=f"del-rbac-{role.value}")
        resp = client.delete(
            f"/api/v1/conventions/del-rbac-{role.value}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_operator_can_read(client: TestClient) -> None:
    """``operator`` role can list / show / history."""
    await _seed_tenants()
    admin_key = make_rsa_keypair("kid-admin-for-op-read")
    admin_token = _token(admin_key)
    op_key = make_rsa_keypair("kid-operator-read")
    op_token = _token(op_key, sub="op-reader", role=TenantRole.OPERATOR)
    with respx.mock as r:
        combined_jwks = {
            "keys": [
                public_jwks(admin_key)["keys"][0],
                public_jwks(op_key)["keys"][0],
            ],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, admin_token, slug="op-can-see")
        list_resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {op_token}"},
        )
        show_resp = client.get(
            "/api/v1/conventions/op-can-see",
            headers={"Authorization": f"Bearer {op_token}"},
        )
        history_resp = client.get(
            "/api/v1/conventions/op-can-see/history",
            headers={"Authorization": f"Bearer {op_token}"},
        )
    assert list_resp.status_code == 200
    assert show_resp.status_code == 200
    assert history_resp.status_code == 200


@pytest.mark.asyncio
async def test_read_only_role_gets_403_on_list(client: TestClient) -> None:
    """``read_only`` role is below the ``operator`` minimum the read routes require."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-readonly-list")
    token = _token(key, sub="op-readonly", role=TenantRole.READ_ONLY)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 409 on duplicate slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_duplicate_slug_returns_409(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-dup")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        first = _post_convention(client, token, slug="dup-slug")
        assert first.status_code == 201
        second = _post_convention(client, token, slug="dup-slug")
    assert second.status_code == 409
    assert "dup-slug" in second.json()["detail"]
    assert "already exists" in second.json()["detail"]


@pytest.mark.asyncio
async def test_same_slug_different_tenants_both_succeed(client: TestClient) -> None:
    """Two tenants can each have a convention with the same slug."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-dup-a")
    key_b = make_rsa_keypair("kid-dup-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        resp_a = _post_convention(client, token_a, slug="shared")
        resp_b = _post_convention(client, token_b, slug="shared")
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201


# ---------------------------------------------------------------------------
# 422 over-budget write-time validation
# ---------------------------------------------------------------------------


def _over_budget_body() -> str:
    """A body whose token estimate exceeds the preamble budget."""
    # The estimator is ceil(len / 3.3). To trip the gate we need
    # ceil(len / 3.3) > 600 -> len >= 1981. We pad to 2200 for safety
    # margin so a future heuristic tweak doesn't silently invalidate.
    return "x " * 1100  # 2200 chars


@pytest.mark.asyncio
async def test_post_over_budget_operational_returns_422(client: TestClient) -> None:
    """A single ``operational`` body exceeding budget → 422 with named overflow."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-budget")
    token = _token(key)
    body = _over_budget_body()
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(
            client,
            token,
            slug="too-big",
            body=body,
            kind="operational",
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "estimated" in detail
    assert "budget" in detail
    assert str(DEFAULT_MAX_PREAMBLE_TOKENS) in detail
    # Confirm the body actually exceeded the budget per the
    # heuristic the route uses -- guards against the test silently
    # under-padding if TOKEN_CHAR_RATIO changes.
    assert len(body) / TOKEN_CHAR_RATIO > DEFAULT_MAX_PREAMBLE_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workflow", "reference"])
async def test_post_over_budget_workflow_reference_accepted(
    client: TestClient,
    kind: str,
) -> None:
    """A ``workflow`` / ``reference`` convention is not preamble-bound -- 201."""
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-budget-{kind}")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(
            client,
            token,
            slug=f"big-{kind}",
            body=_over_budget_body(),
            kind=kind,
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_patch_body_over_budget_operational_returns_422(
    client: TestClient,
) -> None:
    """PATCH a body that exceeds budget against an existing operational row → 422."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-patch-budget")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(
            client,
            token,
            slug="patch-budget",
            body="Small body.",
            kind="operational",
        )
        resp = client.patch(
            "/api/v1/conventions/patch-budget",
            json={"body": _over_budget_body()},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422, resp.text
    assert "estimated" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_does_not_leak_cross_tenant(client: TestClient) -> None:
    """Tenant A's create is invisible to tenant B's list."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-a")
    key_b = make_rsa_keypair("kid-xt-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        create_resp = _post_convention(client, token_a, slug="tenant-a-only")
        assert create_resp.status_code == 201, create_resp.text
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # T7 #1094: list now carries ``budget_status`` alongside entries.
    # Tenant B's view sees its own empty preamble budget -- tenant A's
    # convention does not bleed across (the cross-tenant assertion this
    # test originated to guard).
    assert body["entries"] == []
    assert body["budget_status"] == {
        "max_tokens": DEFAULT_MAX_PREAMBLE_TOKENS,
        "estimated_tokens": 0,
        "over_budget": False,
        "dropped_slugs": [],
    }


@pytest.mark.asyncio
async def test_show_cross_tenant_returns_404(client: TestClient) -> None:
    """Tenant B's GET on tenant A's slug → 404 (no existence leak)."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-show-a")
    key_b = make_rsa_keypair("kid-xt-show-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, token_a, slug="cross-tenant-secret")
        resp = client.get(
            "/api/v1/conventions/cross-tenant-secret",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "convention_not_found"


@pytest.mark.asyncio
async def test_patch_cross_tenant_returns_404(client: TestClient) -> None:
    """Tenant B's PATCH on tenant A's slug → 404, row unchanged."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-patch-a")
    key_b = make_rsa_keypair("kid-xt-patch-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(
        key_b,
        sub="op-b",
        tenant_id=_TENANT_B,
        role=TenantRole.TENANT_ADMIN,
    )
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, token_a, slug="cross-patch", body="A's body")
        resp = client.patch(
            "/api/v1/conventions/cross-patch",
            json={"body": "B's hijack"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 404
    # Tenant A's row is unchanged.
    rows = await _fetch_conventions(_TENANT_A)
    assert len(rows) == 1
    assert rows[0].body == "A's body"


@pytest.mark.asyncio
async def test_delete_cross_tenant_returns_404(client: TestClient) -> None:
    """Tenant B's DELETE on tenant A's slug → 404, row preserved."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-del-a")
    key_b = make_rsa_keypair("kid-xt-del-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(
        key_b,
        sub="op-b",
        tenant_id=_TENANT_B,
        role=TenantRole.TENANT_ADMIN,
    )
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, token_a, slug="cross-del")
        resp = client.delete(
            "/api/v1/conventions/cross-del",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 404
    rows = await _fetch_conventions(_TENANT_A)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_history_cross_tenant_returns_404(client: TestClient) -> None:
    """Tenant B's history GET on tenant A's slug → 404 (history rows not leaked)."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-hist-a")
    key_b = make_rsa_keypair("kid-xt-hist-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        _post_convention(client, token_a, slug="cross-hist")
        resp = client.get(
            "/api/v1/conventions/cross-hist/history",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Pydantic validation -- 422 on shape errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_invalid_slug_pattern_returns_422(client: TestClient) -> None:
    """Slug must match the URL-safe lowercase-hyphen pattern."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-slug-bad")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token, slug="UPPER!chars")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_invalid_kind_returns_422(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-kind-bad")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token, kind="bogus-kind")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_unknown_field_returns_422(client: TestClient) -> None:
    """``extra="forbid"`` rejects unknown fields (catches client-side typos)."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-extra")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/conventions",
            json={
                "slug": "valid-slug",
                "title": "Title",
                "body": "Body",
                "kind": "operational",
                "bodytext": "typo",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Audit + history correlation -- audit_id soft-FK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_history_audit_id_matches_audit_log_id(
    client: TestClient,
) -> None:
    """Every CREATE writes one history row whose ``audit_id`` matches one ``AuditLog.id``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-create")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(client, token, slug="audit-create")
    assert resp.status_code == 201
    rows = await _fetch_conventions(_TENANT_A)
    history = await _fetch_history(rows[0].id)
    assert len(history) == 1
    audit_id = history[0].audit_id
    assert audit_id is not None
    audit_rows = await _fetch_audit_rows()
    # The middleware writes one row per request; find the POST row.
    post_rows = [r for r in audit_rows if r.method == "POST" and r.path == "/api/v1/conventions"]
    assert len(post_rows) == 1
    assert post_rows[0].id == audit_id


@pytest.mark.asyncio
async def test_patch_writes_one_history_one_audit(client: TestClient) -> None:
    """PATCH produces exactly one new history row + one new audit row."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-patch")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="audit-patch")
        client.patch(
            "/api/v1/conventions/audit-patch",
            json={"body": "Patched body."},
            headers={"Authorization": f"Bearer {token}"},
        )
    rows = await _fetch_conventions(_TENANT_A)
    history = await _fetch_history(rows[0].id)
    # CREATE + PATCH history rows.
    assert len(history) == 2
    patch_history = history[-1]
    audit_rows = await _fetch_audit_rows()
    patch_audit_rows = [r for r in audit_rows if r.method == "PATCH"]
    assert len(patch_audit_rows) == 1
    assert patch_audit_rows[0].id == patch_history.audit_id


@pytest.mark.asyncio
async def test_delete_writes_one_history_one_audit(client: TestClient) -> None:
    """DELETE produces exactly one new history row + one new audit row."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-delete")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="audit-delete")
        rows_before = await _fetch_conventions(_TENANT_A)
        convention_id = rows_before[0].id
        client.delete(
            "/api/v1/conventions/audit-delete",
            headers={"Authorization": f"Bearer {token}"},
        )
    history = await _fetch_history(convention_id)
    assert len(history) == 2  # CREATE + DELETE
    delete_history = history[-1]
    audit_rows = await _fetch_audit_rows()
    delete_audit_rows = [r for r in audit_rows if r.method == "DELETE"]
    assert len(delete_audit_rows) == 1
    assert delete_audit_rows[0].id == delete_history.audit_id


@pytest.mark.asyncio
async def test_audit_payload_carries_op_id_and_slug(client: TestClient) -> None:
    """Audit row payload carries ``op_id`` + ``op_class`` + ``slug``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-payload")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="audit-payload")
    audit_rows = await _fetch_audit_rows()
    post_rows = [r for r in audit_rows if r.method == "POST" and r.path == "/api/v1/conventions"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "conventions.create"
    assert payload["op_class"] == "write"
    assert payload["slug"] == "audit-payload"


# ---------------------------------------------------------------------------
# T7 #1094 -- BudgetStatus surfacing on GET /api/v1/conventions
# ---------------------------------------------------------------------------


def _near_budget_body(target_tokens: int = 400) -> str:
    """A body whose token estimate is close to ``target_tokens``.

    Stays well under :data:`DEFAULT_MAX_PREAMBLE_TOKENS` so the
    per-entry POST 422 gate accepts it (the single-entry overflow
    check at write time is independent of the cumulative pack-
    budget check at preamble-assembly time). Three of these
    together overflow the cumulative budget and exercise the
    packer's drop-lowest-priority-first behaviour.

    Math: ``estimate_tokens`` is ``ceil(len / 3.3)``. For
    ``target_tokens=400`` we need ``len >= 1320``; we use 1400 for
    headroom.
    """
    chars = max(target_tokens * 4, 1400)
    return "x " * (chars // 2)


@pytest.mark.asyncio
async def test_list_budget_status_empty_tenant(client: TestClient) -> None:
    """Empty tenant: ``estimated_tokens=0``, ``over_budget=False``, ``dropped_slugs=[]``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-budget-empty")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entries"] == []
    bs = body["budget_status"]
    assert bs["max_tokens"] == DEFAULT_MAX_PREAMBLE_TOKENS
    assert bs["estimated_tokens"] == 0
    assert bs["over_budget"] is False
    assert bs["dropped_slugs"] == []


@pytest.mark.asyncio
async def test_list_budget_status_fitting_tenant(client: TestClient) -> None:
    """Fitting tenant: ``over_budget=False``, ``dropped_slugs=[]``, ``estimated_tokens > 0``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-budget-fits")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Two small operational conventions -- both fit the 600-
        # token budget with room to spare.
        _post_convention(client, token, slug="rule-one", body="Short rule one.", priority=10)
        _post_convention(client, token, slug="rule-two", body="Short rule two.", priority=5)
        # And a workflow / reference each -- preamble-unbound; should
        # not affect ``budget_status`` since the packer reads only
        # ``operational``.
        _post_convention(client, token, slug="wf", body="Workflow note.", kind="workflow")
        _post_convention(client, token, slug="ref", body="Reference note.", kind="reference")
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["entries"]) == 4  # all four rows
    bs = body["budget_status"]
    assert bs["max_tokens"] == DEFAULT_MAX_PREAMBLE_TOKENS
    assert bs["estimated_tokens"] > 0
    assert bs["estimated_tokens"] < DEFAULT_MAX_PREAMBLE_TOKENS
    assert bs["over_budget"] is False
    assert bs["dropped_slugs"] == []


@pytest.mark.asyncio
async def test_list_budget_status_over_budget_tenant(client: TestClient) -> None:
    """Over-budget tenant: ``over_budget=True``, ``dropped_slugs`` lowest-priority-first."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-budget-over")
    token = _token(key)
    body_near = _near_budget_body(target_tokens=400)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Three operational conventions each ~400 tokens; cumulative
        # ~1200 tokens against a 600 budget -> the lowest-priority
        # entries must drop. Priorities are distinct so the drop
        # order is deterministic.
        _post_convention(client, token, slug="high-prio", body=body_near, priority=10)
        _post_convention(client, token, slug="mid-prio", body=body_near, priority=5)
        _post_convention(client, token, slug="low-prio", body=body_near, priority=1)
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ``entries`` returns the full set -- packing does not filter
    # the list view; budget_status is the signal.
    assert {row["slug"] for row in body["entries"]} == {"high-prio", "mid-prio", "low-prio"}
    bs = body["budget_status"]
    assert bs["over_budget"] is True
    # Lowest priorities drop first (priority DESC order, then
    # created_at ASC; the packer drops once cumulative + next
    # would exceed the budget). The first one to overflow is
    # mid-prio or low-prio depending on header overhead -- both
    # must be in the dropped list and low-prio must precede none
    # of the higher priorities.
    assert "low-prio" in bs["dropped_slugs"]
    # The dropped order respects the packer's iteration: lower
    # priority appears later in iteration, so it lands later in
    # ``dropped_slugs``. Mid-prio (priority=5) ranks above low-
    # prio (priority=1), so any drop list containing mid-prio
    # must have it before low-prio.
    if "mid-prio" in bs["dropped_slugs"]:
        assert bs["dropped_slugs"].index("mid-prio") < bs["dropped_slugs"].index(
            "low-prio",
        )
    # High-prio (priority=10) is the first packed; it must not
    # appear in the dropped list -- the packer fills from highest
    # priority down.
    assert "high-prio" not in bs["dropped_slugs"]


@pytest.mark.asyncio
async def test_list_budget_status_cross_tenant_isolation(client: TestClient) -> None:
    """Tenant A's list call reflects only A's budget; B's conventions don't affect A."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-budget-xt-a")
    key_b = make_rsa_keypair("kid-budget-xt-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    body_near = _near_budget_body(target_tokens=400)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        # Tenant A: one tiny convention -> easily fits.
        _post_convention(client, token_a, slug="a-small", body="Tiny rule.", priority=10)
        # Tenant B: three near-budget conventions -> overflows.
        _post_convention(client, token_b, slug="b-high", body=body_near, priority=10)
        _post_convention(client, token_b, slug="b-mid", body=body_near, priority=5)
        _post_convention(client, token_b, slug="b-low", body=body_near, priority=1)

        resp_a = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        resp_b = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    body_a = resp_a.json()
    body_b = resp_b.json()
    # Tenant A: own entries only + fitting budget. Tenant B's bulk
    # of conventions must not influence A's ``over_budget``.
    assert {row["slug"] for row in body_a["entries"]} == {"a-small"}
    assert body_a["budget_status"]["over_budget"] is False
    assert body_a["budget_status"]["dropped_slugs"] == []
    # Tenant B: own entries + over-budget; A's tiny convention must
    # not have crossed into B's budget arithmetic.
    assert {row["slug"] for row in body_b["entries"]} == {"b-high", "b-mid", "b-low"}
    assert body_b["budget_status"]["over_budget"] is True
    assert "a-small" not in body_b["budget_status"]["dropped_slugs"]
    assert "b-low" in body_b["budget_status"]["dropped_slugs"]


@pytest.mark.asyncio
async def test_list_budget_status_kind_filter_does_not_narrow_budget(
    client: TestClient,
) -> None:
    """``?kind=`` narrows entries only; ``budget_status`` reflects the full operational set."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-budget-kind-filter")
    token = _token(key)
    body_near = _near_budget_body(target_tokens=400)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="op-a", body=body_near, priority=10)
        _post_convention(client, token, slug="op-b", body=body_near, priority=5)
        _post_convention(client, token, slug="op-c", body=body_near, priority=1)
        _post_convention(client, token, slug="wf-x", body="WF.", kind="workflow")
        # Narrow the list with ?kind=workflow -- entries collapse to
        # the single workflow row, but budget_status still reflects
        # the cumulative operational set (which is over budget).
        resp = client.get(
            "/api/v1/conventions?kind=workflow",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {row["slug"] for row in body["entries"]} == {"wf-x"}
    # ``budget_status`` is computed off the full operational set
    # (the packer reads only ``operational`` regardless of the
    # query filter), so a ``--kind=workflow`` list still surfaces
    # the truthful overflow signal -- the operator can't hide an
    # over-budget tenant by narrowing the kind.
    bs = body["budget_status"]
    assert bs["over_budget"] is True
    assert "op-c" in bs["dropped_slugs"]


# ---------------------------------------------------------------------------
# G0.14-T8 #1149 -- preamble_status on POST/PATCH responses (signal 18)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_operational_returns_preamble_status_included(
    client: TestClient,
) -> None:
    """POST an operational convention that fits → ``preamble_status.included=True``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-included")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(
            client,
            token,
            slug="fits-easily",
            body="Short body that fits.",
            kind="operational",
            priority=10,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "preamble_status" in body, "POST operational must surface inclusion"
    ps = body["preamble_status"]
    assert ps["included"] is True
    assert ps["position"] == 1  # only convention in the tenant; takes first slot
    assert ps["token_count"] > 0
    assert ps["would_drop_slugs"] == []


def _sized_body(char_count: int) -> str:
    """Build an ASCII body of exactly *char_count* characters.

    Pairs with the ``ceil(len / 3.3)`` heuristic so callers can
    target a precise estimated-token cost without leaning on
    :func:`_near_budget_body`'s ``max(target*4, 1400)`` floor (which
    over-shoots for small targets).
    """
    return "x" * char_count


@pytest.mark.asyncio
async def test_post_operational_dropped_when_over_budget(
    client: TestClient,
) -> None:
    """POST a convention that doesn't fit → ``included=False`` + slug in ``would_drop_slugs``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-dropped")
    token = _token(key)
    # ~250-token bodies (825 chars → ceil(825/3.3)=250). Two fit
    # comfortably under the 600-token budget; a third pushes the
    # cumulative pack over and the lowest-priority row drops.
    body_mid = _sized_body(825)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Two priority-10 mid-sized rows fill most of the budget...
        _post_convention(client, token, slug="winner-a", body=body_mid, priority=10)
        _post_convention(client, token, slug="winner-b", body=body_mid, priority=10)
        # ...the third lands at priority 1 -- packer drops it on overflow.
        resp = _post_convention(
            client,
            token,
            slug="loser-c",
            body=body_mid,
            priority=1,
        )
    assert resp.status_code == 201, resp.text
    ps = resp.json()["preamble_status"]
    assert ps["included"] is False
    assert ps["position"] is None
    assert ps["token_count"] > 0
    # The just-written slug appears in would_drop_slugs (it was the
    # one the packer dropped); the other two priority-10 rows fit
    # and must not appear.
    assert "loser-c" in ps["would_drop_slugs"]
    assert "winner-a" not in ps["would_drop_slugs"]
    assert "winner-b" not in ps["would_drop_slugs"]


@pytest.mark.asyncio
async def test_post_operational_high_priority_displaces_existing_slugs(
    client: TestClient,
) -> None:
    """A high-prio POST that pushes a lower-prio neighbour out lists it in ``would_drop_slugs``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-displace")
    token = _token(key)
    # Two ~250-token rows fit; a third pushes overflow. The new top-
    # priority row goes in at position 1 and the lowest-priority
    # existing row drops.
    body_mid = _sized_body(825)  # ~250 tokens
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="existing-mid", body=body_mid, priority=5)
        _post_convention(client, token, slug="existing-low", body=body_mid, priority=1)
        # New row at priority 100 -- highest -- takes position 1; the
        # cumulative pack now overflows and the lowest-priority
        # neighbour drops.
        resp = _post_convention(
            client,
            token,
            slug="new-top",
            body=body_mid,
            priority=100,
        )
    assert resp.status_code == 201, resp.text
    ps = resp.json()["preamble_status"]
    assert ps["included"] is True
    assert ps["position"] == 1  # priority=100 wins the top slot
    # ``existing-low`` had priority 1 -- the lowest -- so it's the
    # natural drop on overflow. ``new-top`` itself is NOT in the
    # drop list (it's the included one).
    assert "existing-low" in ps["would_drop_slugs"]
    assert "new-top" not in ps["would_drop_slugs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workflow", "reference"])
async def test_post_workflow_reference_omits_preamble_status(
    client: TestClient,
    kind: str,
) -> None:
    """Non-operational kinds don't enter the preamble → ``preamble_status`` is ``None``."""
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-preamble-{kind}")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(
            client,
            token,
            slug=f"non-op-{kind}",
            body="Workflow / reference text.",
            kind=kind,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The field is present on the schema (``Convention.preamble_status``)
    # but null for preamble-unbound kinds. JSON consumers branching
    # on ``preamble_status is None`` get the right "this write does
    # not affect the preamble" signal.
    assert body["preamble_status"] is None


@pytest.mark.asyncio
async def test_patch_operational_returns_preamble_status(client: TestClient) -> None:
    """PATCH an operational convention's body → response carries fresh ``preamble_status``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-patch")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(
            client,
            token,
            slug="patch-target",
            body="Original short.",
            priority=5,
        )
        resp = client.patch(
            "/api/v1/conventions/patch-target",
            json={"body": "Updated body text."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    ps = resp.json()["preamble_status"]
    assert ps is not None
    assert ps["included"] is True
    assert ps["position"] == 1
    assert ps["would_drop_slugs"] == []


@pytest.mark.asyncio
async def test_patch_priority_only_returns_preamble_status(client: TestClient) -> None:
    """Priority-only PATCH that re-ranks the convention still surfaces ``preamble_status``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-patch-prio")
    token = _token(key)
    body_near = _near_budget_body(target_tokens=400)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Three rows; only two fit. Start the to-be-patched row at
        # priority 1 (definitely dropped) and bump it to 100 via
        # PATCH (should now be included at position 1).
        _post_convention(client, token, slug="patch-prio", body=body_near, priority=1)
        _post_convention(client, token, slug="other-a", body=body_near, priority=5)
        _post_convention(client, token, slug="other-b", body=body_near, priority=5)
        # Pre-PATCH state: patch-prio is dropped.
        resp_pre = client.get(
            "/api/v1/conventions/patch-prio",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_pre.status_code == 200
        # GET-single does NOT carry preamble_status (the aggregate
        # signal lives on the list response's budget_status); the
        # write-time feedback is the only post-mutation signal.
        assert resp_pre.json().get("preamble_status") is None
        # Bump priority to 100 -- now patch-prio takes the top slot.
        resp = client.patch(
            "/api/v1/conventions/patch-prio",
            json={"priority": 100},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    ps = resp.json()["preamble_status"]
    assert ps is not None
    assert ps["included"] is True
    assert ps["position"] == 1


@pytest.mark.asyncio
async def test_get_single_does_not_carry_preamble_status(client: TestClient) -> None:
    """``GET /{slug}`` returns ``preamble_status=None`` -- inclusion signal is write-time only."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-preamble-get")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _post_convention(client, token, slug="get-only")
        resp = client.get(
            "/api/v1/conventions/get-only",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    # GET-single is the natural read shape; ``budget_status`` on the
    # list response is the aggregate-budget signal, so the per-row
    # GET deliberately omits preamble_status (returns None) to keep
    # the read paths' responsibilities clean.
    assert resp.json()["preamble_status"] is None


# ---------------------------------------------------------------------------
# G12.4-T2 #1316 -- runbook priming wire-up on the conventions routes
# ---------------------------------------------------------------------------
#
# The issue body prescribes Tests #7 + #8: route-level coverage that
# the calling operator's runbook priming flows through the conventions
# API's read + write paths. The route response shapes are
# ``ConventionListResponse`` (``entries`` + ``budget_status``) on the
# list endpoint and ``Convention`` (with ``preamble_status``) on POST /
# PATCH -- none of which surfaces the raw assembled preamble text.
# By design, ``budget_status`` is **conventions-only** (the priming
# band has its own implicit cap via ``MAX_PRIMING_BLOCKS`` and is not
# charged to the conventions budget) and ``preamble_status`` is a
# slug-scoped projection (``included`` / ``position`` / ``token_count``
# / ``would_drop_slugs``) over the conventions pack alone (per the
# ``_compute_preamble_status`` docstring).
#
# Per the iter-1 review's M1-alternative: "if the route response shape
# doesn't expose raw preamble text, document the assembler-level
# coverage as maximally-feasible and file a follow-up for end-to-end
# MCP-level route coverage — the deferral has to be explicit."
#
# The maximally-feasible route-level coverage here is verifying the
# **wire-up**: that each route invokes ``assemble_preamble`` /
# ``assemble_preamble_detailed`` with the calling operator's
# ``operator.sub`` as the second positional argument. That is the
# load-bearing T2 claim ("all three call sites updated to pass
# operator.sub"); a future commit that silently drops the sub argument
# (or passes a hard-coded sentinel) would fail these tests even when
# the conventions-only response fields stayed correct.
#
# End-to-end coverage of priming-band content in the assembled wire
# text is delivered through:
#   * the assembler-level tests in
#     ``backend/tests/test_conventions_preamble.py`` (the
#     ``test_one_in_progress_run_appends_priming_after_conventions``
#     family) that exercise the primitive's full byte shape, and
#   * ``backend/tests/test_mcp_initialize_instructions.py``
#     covers the MCP-level wire-up (the load-bearing call site per
#     the issue body).
# The MCP-level path is the user-facing surface for priming text; the
# conventions API surfaces the conventions-only ``budget_status`` /
# ``preamble_status`` projections and does not promise the priming
# text on its own response shape. A v0.2.next initiative may add a
# dedicated ``GET /api/v1/conventions/preview`` route that returns
# the assembled preamble verbatim (the issue body's original test
# wording assumed such a route would exist; it does not, and the
# conventions-only projections are the right shape for the list /
# write feedback responsibility).


@pytest.mark.asyncio
async def test_list_conventions_invokes_assembler_with_operator_sub(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/v1/conventions`` passes ``operator.sub`` to ``assemble_preamble``.

    G12.4-T2 (#1316) wire-up regression guard. The list endpoint
    builds ``budget_status`` by calling ``assemble_preamble`` with
    the operator's tenant + sub; a refactor that silently dropped
    the ``operator.sub`` argument (so every list call would assemble
    priming for a sentinel operator and the calling operator's
    in-progress runs would no longer flow into the preamble band)
    would still produce a correct ``budget_status`` (the conventions
    pack is sub-agnostic) but would silently break the user-visible
    priming. This test asserts the right sub is forwarded so the
    regression is caught at this layer.

    Spy strategy: wrap the real ``assemble_preamble`` import in
    ``meho_backplane.conventions.service`` (where the budget arithmetic
    now lives after the G10.12-T0 #1894 service extraction) and record
    every call's positional args. Assert the second positional matches
    the operator's sub from the JWT.
    """
    from meho_backplane.conventions import service as conventions_module

    await _seed_tenants()
    key = make_rsa_keypair("kid-priming-list-spy")
    op_sub = "op-priming-list-spy"
    token = mint_token(
        key,
        sub=op_sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT_A),
    )

    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_assemble = conventions_module.assemble_preamble

    async def _spy_assemble(*args: Any, **kwargs: Any) -> Any:
        captured.append((args, dict(kwargs)))
        return await real_assemble(*args, **kwargs)

    monkeypatch.setattr(conventions_module, "assemble_preamble", _spy_assemble)

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/conventions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    # The list handler invokes the assembler exactly once per request.
    # Positional args are ``(tenant_id, operator_sub)``; assert the
    # second one is the operator's sub from the JWT, not a sentinel.
    assert len(captured) == 1, captured
    args, _kwargs = captured[0]
    assert len(args) >= 2, args
    assert args[0] == _TENANT_A
    assert args[1] == op_sub
    # The conventions-only ``budget_status`` projection stays correct
    # in the response -- the priming wiring does not corrupt it (an
    # empty operational set still reports ``estimated_tokens=0``).
    body = resp.json()
    assert body["budget_status"]["estimated_tokens"] == 0
    assert body["budget_status"]["over_budget"] is False


@pytest.mark.asyncio
async def test_post_convention_invokes_detailed_assembler_with_operator_sub(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /api/v1/conventions`` passes ``operator.sub`` to ``assemble_preamble_detailed``.

    G12.4-T2 (#1316) wire-up regression guard for the post-write
    inclusion-feedback path. ``_compute_preamble_status`` (the
    helper that builds ``preamble_status`` on POST / PATCH responses)
    delegates to ``assemble_preamble_detailed`` with the operator's
    sub so the assembled preview reflects what the calling operator's
    MCP session will see (priming included). Forgetting to pass the
    sub would leave priming silently absent from the post-write
    preview while ``preamble_status``'s conventions-only projection
    still looked right.

    Spy strategy: wrap the real ``assemble_preamble_detailed`` import
    in ``meho_backplane.conventions.service`` (where
    ``_compute_preamble_status`` now lives after the G10.12-T0 #1894
    service extraction) and assert every call carries the operator's
    sub. Use an operational convention so the helper is actually
    invoked (workflow / reference short-circuit to
    ``preamble_status=None`` per the ``_compute_preamble_status``
    docstring).
    """
    from meho_backplane.conventions import service as conventions_module

    await _seed_tenants()
    key = make_rsa_keypair("kid-priming-post-spy")
    op_sub = "op-priming-post-spy"
    token = mint_token(
        key,
        sub=op_sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT_A),
    )

    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_detailed = conventions_module.assemble_preamble_detailed

    async def _spy_detailed(*args: Any, **kwargs: Any) -> Any:
        captured.append((args, dict(kwargs)))
        return await real_detailed(*args, **kwargs)

    monkeypatch.setattr(
        conventions_module,
        "assemble_preamble_detailed",
        _spy_detailed,
    )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _post_convention(
            client,
            token,
            slug="priming-wired-post",
            body="Body for the post-write priming wire-up check.",
            kind="operational",
            priority=5,
        )
    assert resp.status_code == 201, resp.text
    # ``_compute_preamble_status`` invokes the detailed assembler
    # once per write against an operational kind. Assert the sub
    # is forwarded as the second positional argument.
    assert len(captured) == 1, captured
    args, kwargs = captured[0]
    assert len(args) >= 2, args
    assert args[0] == _TENANT_A
    assert args[1] == op_sub
    # The route still threads the request-scoped session through so
    # the post-write read sees the just-flushed row (the read-your-
    # own-writes invariant ``_compute_preamble_status`` relies on);
    # the spy must observe the kwarg.
    assert "session" in kwargs
    # And the conventions-only ``preamble_status`` projection stays
    # correct -- the just-written slug lands in position 1 of the
    # otherwise empty operational set.
    ps = resp.json()["preamble_status"]
    assert ps is not None
    assert ps["included"] is True
    assert ps["position"] == 1


@pytest.mark.asyncio
async def test_patch_convention_invokes_detailed_assembler_with_operator_sub(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PATCH /api/v1/conventions/{slug}`` passes ``operator.sub`` to the detailed assembler.

    G12.4-T2 (#1316) wire-up regression guard for PATCH -- pairs
    with the POST spy above. ``_compute_preamble_status`` is invoked
    once per write; the PATCH route's call site must forward
    ``operator.sub`` so the post-update preview reflects the calling
    operator's MCP session shape (priming included). Verify across
    the create + update lifecycle so a refactor that dropped the sub
    on either route would fail.

    The detailed assembler is patched on
    ``meho_backplane.conventions.service`` (where
    ``_compute_preamble_status`` now lives after the G10.12-T0 #1894
    service extraction).
    """
    from meho_backplane.conventions import service as conventions_module

    await _seed_tenants()
    key = make_rsa_keypair("kid-priming-patch-spy")
    op_sub = "op-priming-patch-spy"
    token = mint_token(
        key,
        sub=op_sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT_A),
    )

    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_detailed = conventions_module.assemble_preamble_detailed

    async def _spy_detailed(*args: Any, **kwargs: Any) -> Any:
        captured.append((args, dict(kwargs)))
        return await real_detailed(*args, **kwargs)

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Seed an operational row first WITHOUT the spy so the POST
        # call's invocation isn't captured -- the spy is installed
        # after the create.
        create_resp = _post_convention(
            client,
            token,
            slug="priming-wired-patch",
            body="Original body for PATCH wire-up check.",
            kind="operational",
            priority=10,
        )
        assert create_resp.status_code == 201, create_resp.text
        monkeypatch.setattr(
            conventions_module,
            "assemble_preamble_detailed",
            _spy_detailed,
        )
        patch_resp = client.patch(
            "/api/v1/conventions/priming-wired-patch",
            json={"body": "Updated body for PATCH wire-up check."},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert patch_resp.status_code == 200, patch_resp.text
    # Exactly one detailed-assembler call from the PATCH route
    # (the seeding POST predates the spy installation).
    assert len(captured) == 1, captured
    args, kwargs = captured[0]
    assert len(args) >= 2, args
    assert args[0] == _TENANT_A
    assert args[1] == op_sub
    assert "session" in kwargs
    ps = patch_resp.json()["preamble_status"]
    assert ps is not None
    assert ps["included"] is True
    assert ps["position"] == 1
