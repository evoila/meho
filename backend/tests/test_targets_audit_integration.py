# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for G0.3-T4: audit_log.target_id via contextvar.

Coverage matrix:

* :func:`resolve_target` binds ``target_id`` into structlog contextvars on
  exact-name success, alias-match success, and does **not** bind on 404.
* :func:`_resolve_target_id` in :mod:`meho_backplane.audit` correctly parses
  the contextvar (happy path, None slot, malformed value).
* ``GET /api/v1/targets/{name}`` (describe) writes ``audit_log.target_id``
  equal to the resolved target's UUID.
* ``POST /api/v1/targets`` (create) writes ``audit_log.target_id`` equal to
  the newly created target's UUID.
* ``GET /api/v1/targets`` (list) writes ``audit_log.target_id = NULL``
  (no resolve_target call, slot stays ``None``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx
import structlog
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.audit import _resolve_target_id
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.targets.resolver import (
    TargetNotFoundError,
    resolve_target,
)

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_DEFAULT_TENANT_UUID = uuid.UUID(DEFAULT_TENANT_ID)


# ---------------------------------------------------------------------------
# Per-test isolated audit DB
# ---------------------------------------------------------------------------


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = tmp_path / "audit_t4.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_engine(
    _audit_db_url: str,
) -> AsyncIterator[AsyncEngine]:
    reset_engine_for_testing()
    eng = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        reset_engine_for_testing()


async def _fetch_audit_rows(eng: AsyncEngine) -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Unit tests — resolve_target contextvar binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_exact_match_binds_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """Exact-name match binds ``target_id`` into structlog contextvars."""
    t = await _insert_target(name="alpha")

    structlog.contextvars.clear_contextvars()
    sm = get_sessionmaker()
    async with sm() as session:
        returned = await resolve_target(session, _DEFAULT_TENANT_UUID, "alpha")

    assert returned.id == t.id
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") == str(t.id)


@pytest.mark.asyncio
async def test_resolve_target_alias_match_binds_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """Alias-element-equality match also binds ``target_id``."""
    t = await _insert_target(name="beta", aliases=["b", "beta-alias"])

    structlog.contextvars.clear_contextvars()
    sm = get_sessionmaker()
    async with sm() as session:
        returned = await resolve_target(session, _DEFAULT_TENANT_UUID, "beta-alias")

    assert returned.id == t.id
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") == str(t.id)


@pytest.mark.asyncio
async def test_resolve_target_not_found_does_not_bind_target_id(
    isolated_engine: AsyncEngine,
) -> None:
    """TargetNotFoundError is raised without mutating ``target_id`` contextvar."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=None)

    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(TargetNotFoundError):
            await resolve_target(session, _DEFAULT_TENANT_UUID, "no-such-target")

    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("target_id") is None


# ---------------------------------------------------------------------------
# Unit tests — _resolve_target_id helper
# ---------------------------------------------------------------------------


def test_resolve_target_id_returns_none_when_slot_is_none() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=None)
    assert _resolve_target_id() is None


def test_resolve_target_id_returns_none_when_key_absent() -> None:
    structlog.contextvars.clear_contextvars()
    assert _resolve_target_id() is None


def test_resolve_target_id_parses_valid_uuid_string() -> None:
    tid = uuid.uuid4()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id=str(tid))
    assert _resolve_target_id() == tid


def test_resolve_target_id_returns_none_for_malformed_string() -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id="not-a-uuid")
    assert _resolve_target_id() is None


# ---------------------------------------------------------------------------
# Integration tests — audit_log.target_id populated via middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_target_writes_audit_row_with_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets/{name} → audit_log.target_id == resolved target UUID."""
    t = await _insert_target(name="gamma")
    key = make_rsa_keypair("kid-T4-describe")
    token = mint_token(
        key,
        sub="op-t4",
        tenant_role="operator",
    )
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            f"/api/v1/targets/{t.name}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id == t.id


@pytest.mark.asyncio
async def test_list_targets_writes_audit_row_with_null_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets → audit_log.target_id is NULL (no resolve_target)."""
    await _insert_target(name="delta")
    key = make_rsa_keypair("kid-T4-list")
    token = mint_token(key, sub="op-t4-list", tenant_role="operator")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id is None


@pytest.mark.asyncio
async def test_create_target_writes_audit_row_with_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/v1/targets → audit_log.target_id == newly created target UUID."""
    key = make_rsa_keypair("kid-T4-create")
    token = mint_token(key, sub="op-t4-create", tenant_role="tenant_admin")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "epsilon",
                "product": "rke2",
                "host": "10.0.0.5",
                "auth_model": "shared_service_account",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    created_id = uuid.UUID(response.json()["id"])

    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id == created_id


@pytest.mark.asyncio
async def test_describe_nonexistent_target_audit_row_has_null_target_id(
    isolated_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /api/v1/targets/no-such → 404, audit_log.target_id is NULL."""
    key = make_rsa_keypair("kid-T4-404")
    token = mint_token(key, sub="op-t4-404", tenant_role="operator")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        response = client.get(
            "/api/v1/targets/no-such-target",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 404
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    assert rows[0].target_id is None
