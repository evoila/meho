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
    assert resp.json() == {"entries": []}


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
