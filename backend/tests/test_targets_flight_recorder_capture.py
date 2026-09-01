# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the per-target flight-recorder capture tri-state on the target
PATCH / create routes (#3272).

The ``targets.flight_recorder_capture`` override rides the existing
``PATCH /api/v1/targets/{name}`` and ``POST /api/v1/targets`` routes rather
than a new endpoint. Coverage:

* Tri-state semantics: PATCH sets ``true`` / ``false`` and clears with an
  explicit ``null`` (back to inherit); an absent field leaves the column
  untouched (null-vs-absent gated on ``exclude_unset``).
* Audit: an applied change folds ``flight_recorder_capture`` before / after
  into the request's ``audit_log`` payload (with the ``inherit`` sentinel for
  the tri-state NULL); a no-op / unrelated PATCH binds no capture keys. Create
  with a non-inherit override audits the seeded value.
* Cache invalidation: flipping the override through the route is reflected by
  the next :func:`should_capture` **without** a cache reset -- proving the
  handler evicts the resolver's per-target cache.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    get_sessionmaker,
    reset_engine_for_testing,
)
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AuditLog
from meho_backplane.flight_recorder.config import (
    reset_flight_recorder_config_cache_for_testing,
    should_capture,
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

_DEFAULT_TENANT_UUID = uuid.UUID(DEFAULT_TENANT_ID)


@pytest.fixture
def _audit_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = tmp_path / "fr_capture.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def isolated_engine(_audit_db_url: str) -> AsyncIterator[AsyncEngine]:
    reset_engine_for_testing()
    reset_flight_recorder_config_cache_for_testing()
    eng = create_engine_for_url(_audit_db_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng
    try:
        yield eng
    finally:
        await dispose_engine()
        reset_engine_for_testing()
        reset_flight_recorder_config_cache_for_testing()


async def _fetch_audit_rows(eng: AsyncEngine) -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


def _admin_token(key: object) -> str:
    return mint_token(key, sub="adm-fr", tenant_role="tenant_admin")


# ---------------------------------------------------------------------------
# PATCH tri-state + audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "after_sentinel"),
    [(True, "on"), (False, "off")],
)
async def test_patch_capture_sets_override_and_audits(
    isolated_engine: AsyncEngine, value: bool, after_sentinel: str
) -> None:
    t = await _insert_target(name="fr-set", flight_recorder_capture=None)
    key = make_rsa_keypair(f"kid-cap-{value}")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"flight_recorder_capture": value},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["flight_recorder_capture"] is value
    rows = await _fetch_audit_rows(isolated_engine)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["flight_recorder_capture_changed"] is True
    assert payload["target_id"] == str(t.id)
    assert payload["flight_recorder_capture_before"] == "inherit"
    assert payload["flight_recorder_capture_after"] == after_sentinel
    assert rows[0].target_id == t.id


@pytest.mark.asyncio
async def test_patch_capture_clear_to_null_reverts_to_inherit(
    isolated_engine: AsyncEngine,
) -> None:
    t = await _insert_target(name="fr-clear", flight_recorder_capture=True)
    key = make_rsa_keypair("kid-cap-clear")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"flight_recorder_capture": None},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["flight_recorder_capture"] is None
    payload = (await _fetch_audit_rows(isolated_engine))[0].payload
    assert payload["flight_recorder_capture_before"] == "on"
    assert payload["flight_recorder_capture_after"] == "inherit"


@pytest.mark.asyncio
async def test_patch_without_capture_binds_no_capture_keys(
    isolated_engine: AsyncEngine,
) -> None:
    t = await _insert_target(name="fr-unrelated", flight_recorder_capture=True)
    key = make_rsa_keypair("kid-cap-unrelated")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"notes": "ticket-99"},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    payload = (await _fetch_audit_rows(isolated_engine))[0].payload
    assert "flight_recorder_capture_changed" not in payload
    assert "flight_recorder_capture_before" not in payload


@pytest.mark.asyncio
async def test_patch_capture_noop_resend_binds_no_capture_keys(
    isolated_engine: AsyncEngine,
) -> None:
    """Re-sending the same override value is not a change -> no audit noise."""
    t = await _insert_target(name="fr-noop", flight_recorder_capture=True)
    key = make_rsa_keypair("kid-cap-noop")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"flight_recorder_capture": True},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    payload = (await _fetch_audit_rows(isolated_engine))[0].payload
    assert "flight_recorder_capture_changed" not in payload


@pytest.mark.asyncio
async def test_create_target_with_capture_override_audits(
    isolated_engine: AsyncEngine,
) -> None:
    key = make_rsa_keypair("kid-cap-create")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.post(
            "/api/v1/targets",
            json={
                "name": "fr-created",
                "product": "rke2",
                "host": "10.0.0.9",
                "flight_recorder_capture": True,
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["flight_recorder_capture"] is True
    payload = (await _fetch_audit_rows(isolated_engine))[0].payload
    assert payload["flight_recorder_capture_changed"] is True
    assert payload["flight_recorder_capture_before"] == "inherit"
    assert payload["flight_recorder_capture_after"] == "on"


# ---------------------------------------------------------------------------
# Cache invalidation (the load-bearing property)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_flip_reflected_by_resolver_without_restart(
    isolated_engine: AsyncEngine,
) -> None:
    """Flipping the per-target override through the route is seen by the next
    ``should_capture`` without a cache reset -- proving per-target eviction.

    The target's tenant has no policy row (capture OFF default), so with the
    override at inherit the resolver answers ``False``. After the override flips
    to force-ON, the resolver must answer ``True`` even though the cache was
    primed with the old value.
    """
    t = await _insert_target(name="fr-cache", flight_recorder_capture=None)
    reset_flight_recorder_config_cache_for_testing()
    # Prime: inherit -> tenant default (OFF) -> False.
    assert await should_capture(tenant_id=_DEFAULT_TENANT_UUID, target_id=t.id) is False
    key = make_rsa_keypair("kid-cap-cache")
    client = TestClient(_build_app())
    with respx.mock as mr:
        mock_discovery_and_jwks(mr, public_jwks(key))
        resp = client.patch(
            f"/api/v1/targets/{t.name}",
            json={"flight_recorder_capture": True},
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    # No cache reset -- the route must have invalidated the per-target entry.
    assert await should_capture(tenant_id=_DEFAULT_TENANT_UUID, target_id=t.id) is True
