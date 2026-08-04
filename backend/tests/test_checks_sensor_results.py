# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tick evidence retention + trend query -- repository / service / REST (#2756).

Initiative #2780 (parent goal #221), Task #2756. Coverage matrix:

* **Repository** -- ``record_sensor_result(record_history=...)`` appends one
  ``sensor_results`` row per non-stale evaluation in the projection's
  transaction (two ticks => two rows), writes nothing when disabled, appends no
  duplicate on a stale timestamp, and rolls back with the projection;
  ``list_sensor_results`` filters + orders + keyset-paginates.
* **Service** -- ``list_results`` scopes to the tenant (cross-tenant => None),
  round-trips the keyset cursor, and rejects a malformed cursor.
* **REST** -- the ``{items, next_cursor}`` envelope in ``evaluated_at ASC``
  order, an unknown query param rejected 422 (no aggregation knobs), a
  cross-tenant read 404, a state filter, and a malformed cursor 422.
* **Settings** -- the ``CHECKS_EVIDENCE_*`` env round-trips through
  ``get_settings()`` (the #2737 discipline), incl. the ``0``-disables default.

Runs on the SQLite engine from :mod:`tests.conftest`; REST via
:class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.checks.repository import list_sensor_results, record_sensor_result
from meho_backplane.checks.schemas import SensorCreate, SensorResultsQuery
from meho_backplane.checks.service import SensorAdminService, SensorResultsCursorError
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Sensor, SensorResult, Tenant
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

_SAFE_CONNECTOR = "vmware-rest-9.0"
_SAFE_OP = "vmware.vm.list"

_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


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
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _seed_tenant(tenant_id: UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        if existing.scalar_one_or_none() is None:
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
            await session.commit()


async def _seed_descriptor(op_id: str = _SAFE_OP) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.op_id == op_id,
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                EndpointDescriptor(
                    product="vmware",
                    version="9.0",
                    impl_id="vmware-rest",
                    op_id=op_id,
                    source_kind="ingested",
                    method="GET",
                    path=f"/{op_id}",
                    parameter_schema={"type": "object", "properties": {}},
                    safety_level="safe",
                )
            )
            await session.commit()


def _create_payload(name: str = "disk-space") -> SensorCreate:
    return SensorCreate.model_validate(
        {
            "name": name,
            "connector_id": _SAFE_CONNECTOR,
            "op_id": _SAFE_OP,
            "assertion": _ASSERTION,
            "cadence_kind": "interval",
            "interval_seconds": 60,
        }
    )


async def _seed_sensor(tenant_id: UUID = _TENANT_A, *, name: str = "disk-space") -> UUID:
    """Seed a tenant + safe descriptor + one sensor; return the sensor id."""
    await _seed_tenant(tenant_id, slug=str(tenant_id)[:8])
    await _seed_descriptor()
    service = SensorAdminService()
    created = await service.create(
        tenant_id=tenant_id, created_by_sub="op-admin", payload=_create_payload(name)
    )
    return created.id


async def _append_results(
    sensor_id: UUID,
    *,
    count: int,
    base: datetime,
    step_seconds: int = 60,
    state: str = "ok",
) -> None:
    """Append *count* evidence rows via the runner's persist seam."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for i in range(count):
            await record_sensor_result(
                session,
                sensor_id=sensor_id,
                state=state,  # type: ignore[arg-type]
                value=i,
                evidence={"observed": i},
                evaluated_at=base + timedelta(seconds=i * step_seconds),
                record_history=True,
            )
        await session.commit()


async def _count_results(sensor_id: UUID) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        total = await session.execute(
            select(func.count())
            .select_from(SensorResult)
            .where(SensorResult.sensor_id == sensor_id)
        )
        return int(total.scalar_one())


# ---------------------------------------------------------------------------
# Repository: record_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_history_appends_one_row_per_tick() -> None:
    """Two non-stale ticks => two rows; the projection reflects the latest."""
    sensor_id = await _seed_sensor()
    t0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    await _append_results(sensor_id, count=2, base=t0)

    assert await _count_results(sensor_id) == 2
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(SensorResult)
                    .where(SensorResult.sensor_id == sensor_id)
                    .order_by(SensorResult.evaluated_at.asc())
                )
            ).scalars()
        )
    assert [r.value for r in rows] == [0, 1]


@pytest.mark.asyncio
async def test_record_history_disabled_writes_no_rows() -> None:
    """``record_history=False`` (retention disabled) appends nothing."""
    sensor_id = await _seed_sensor()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await record_sensor_result(
            session,
            sensor_id=sensor_id,
            state="ok",
            value=1,
            evidence={"observed": 1},
            evaluated_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
            record_history=False,
        )
        await session.commit()
    assert await _count_results(sensor_id) == 0


@pytest.mark.asyncio
async def test_record_history_stale_result_appends_no_duplicate() -> None:
    """A non-newer ``evaluated_at`` is ignored: no projection change, no extra row."""
    sensor_id = await _seed_sensor()
    t0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        assert (
            await record_sensor_result(
                session,
                sensor_id=sensor_id,
                state="ok",
                value=1,
                evidence={},
                evaluated_at=t0,
                record_history=True,
            )
            is True
        )
        # Same timestamp again -> stale/idempotent, must not append a 2nd row.
        assert (
            await record_sensor_result(
                session,
                sensor_id=sensor_id,
                state="critical",
                value=9,
                evidence={},
                evaluated_at=t0,
                record_history=True,
            )
            is False
        )
        await session.commit()
    assert await _count_results(sensor_id) == 1


@pytest.mark.asyncio
async def test_record_history_appends_row_for_unconfirmed_soft_reading() -> None:
    """A #2799 soft/pending reading (unconfirmed) still lands in history.

    The #2756 x #2799 collision invariant the initiative called out: the
    evidence append rides *before* the confirmation gate, so a reading held as
    a soft state (no commit, projection unchanged) still produces exactly one
    evidence row -- history records the observed outcome, not just committed
    transitions.
    """
    sensor_id = await _seed_sensor()
    # Give the sensor a confirmation window (#2799); there is no update route,
    # so set it directly on the row.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(Sensor, sensor_id)
        assert row is not None
        row.retry_times = 2
        await session.commit()

    async with sessionmaker() as session:
        committed = await record_sensor_result(
            session,
            sensor_id=sensor_id,
            state="critical",
            value=9,
            evidence={"reason": "breach"},
            evaluated_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
            record_history=True,
        )
        await session.commit()

    # retry_times=2 holds the first differing reading soft: nothing commits...
    assert committed is False
    read = await SensorAdminService().get(_TENANT_A, sensor_id)
    assert read is not None
    assert read.last_state == "unknown"
    # ...but the unconfirmed reading is still in the evidence history.
    assert await _count_results(sensor_id) == 1


@pytest.mark.asyncio
async def test_record_history_rolls_back_with_projection() -> None:
    """A rollback discards both the history row and the projection update."""
    sensor_id = await _seed_sensor()
    t0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await record_sensor_result(
            session,
            sensor_id=sensor_id,
            state="critical",
            value=99,
            evidence={"observed": 99},
            evaluated_at=t0,
            record_history=True,
        )
        # Simulate a downstream persist failure before the caller commits.
        await session.rollback()

    # Neither the row nor the projection change survived the aborted txn.
    assert await _count_results(sensor_id) == 0
    read = await SensorAdminService().get(_TENANT_A, sensor_id)
    assert read is not None
    assert read.last_state == "unknown"
    assert read.last_evaluated_at is None


# ---------------------------------------------------------------------------
# Repository: list_sensor_results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sensor_results_filters_window_and_state() -> None:
    """From/to window + state filter narrow the rows; order is evaluated_at ASC."""
    sensor_id = await _seed_sensor()
    base = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    # 5 ok rows at 0..4 min, then 3 critical rows at 5..7 min.
    await _append_results(sensor_id, count=5, base=base, state="ok")
    await _append_results(sensor_id, count=3, base=base + timedelta(minutes=5), state="critical")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows, nxt = await list_sensor_results(
            session,
            sensor_id=sensor_id,
            from_ts=base + timedelta(minutes=5),
            to_ts=None,
            state="critical",
            limit=100,
            after=None,
        )
    assert nxt is None
    assert [r.state for r in rows] == ["critical", "critical", "critical"]
    # ascending order
    assert rows == sorted(rows, key=lambda r: r.evaluated_at)


@pytest.mark.asyncio
async def test_list_sensor_results_keyset_pagination() -> None:
    """A full page returns a next cursor; the next page continues after it."""
    sensor_id = await _seed_sensor()
    base = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    await _append_results(sensor_id, count=5, base=base)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        page1, nxt = await list_sensor_results(
            session,
            sensor_id=sensor_id,
            from_ts=None,
            to_ts=None,
            state=None,
            limit=2,
            after=None,
        )
        assert len(page1) == 2
        assert nxt is not None
        page2, nxt2 = await list_sensor_results(
            session,
            sensor_id=sensor_id,
            from_ts=None,
            to_ts=None,
            state=None,
            limit=2,
            after=nxt,
        )
    assert [r.value for r in page1] == [0, 1]
    assert [r.value for r in page2] == [2, 3]
    assert nxt2 is not None  # still one more row (index 4)


# ---------------------------------------------------------------------------
# Service: list_results (tenant scope + cursor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_results_cross_tenant_returns_none() -> None:
    """A caller in tenant B cannot read tenant A's sensor history."""
    sensor_id = await _seed_sensor(_TENANT_A)
    await _append_results(sensor_id, count=1, base=datetime(2026, 8, 1, tzinfo=UTC))
    await _seed_tenant(_TENANT_B, slug="tenant-b")
    page = await SensorAdminService().list_results(_TENANT_B, sensor_id, SensorResultsQuery())
    assert page is None


@pytest.mark.asyncio
async def test_service_list_results_cursor_round_trip() -> None:
    """The opaque cursor pages through the full series without gaps/dupes."""
    sensor_id = await _seed_sensor()
    await _append_results(sensor_id, count=3, base=datetime(2026, 8, 1, tzinfo=UTC))
    service = SensorAdminService()
    page1 = await service.list_results(_TENANT_A, sensor_id, SensorResultsQuery(limit=2))
    assert page1 is not None
    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    page2 = await service.list_results(
        _TENANT_A, sensor_id, SensorResultsQuery(limit=2, cursor=page1.next_cursor)
    )
    assert page2 is not None
    assert len(page2.items) == 1
    assert page2.next_cursor is None


@pytest.mark.asyncio
async def test_service_list_results_invalid_cursor_raises() -> None:
    """A malformed cursor raises the typed error the boundary maps to 422."""
    sensor_id = await _seed_sensor()
    with pytest.raises(SensorResultsCursorError):
        await SensorAdminService().list_results(
            _TENANT_A, sensor_id, SensorResultsQuery(cursor="!!not-base64!!")
        )


@pytest.mark.asyncio
async def test_service_list_results_empty_when_no_rows() -> None:
    """A sensor with no history returns an empty page (not None)."""
    sensor_id = await _seed_sensor()
    page = await SensorAdminService().list_results(_TENANT_A, sensor_id, SensorResultsQuery())
    assert page is not None
    assert page.items == []
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_results_envelope_and_order(client: TestClient) -> None:
    """GET returns the {items, next_cursor} envelope, evaluated_at ASC."""
    sensor_id = await _seed_sensor()
    await _append_results(sensor_id, count=3, base=datetime(2026, 8, 1, tzinfo=UTC))
    key = make_rsa_keypair("kid-results")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/sensors/{sensor_id}/results",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"items", "next_cursor"}
    values = [item["value"] for item in body["items"]]
    assert values == [0, 1, 2]
    assert all(item["sensor_id"] == str(sensor_id) for item in body["items"])


@pytest.mark.asyncio
async def test_rest_results_unknown_param_returns_422(client: TestClient) -> None:
    """An unknown filter param is rejected 422 -- pins 'no aggregation knobs'."""
    sensor_id = await _seed_sensor()
    key = make_rsa_keypair("kid-results")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/sensors/{sensor_id}/results",
            params={"smoothing": "ewma"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rest_results_state_filter(client: TestClient) -> None:
    """The state filter returns only matching rows."""
    sensor_id = await _seed_sensor()
    base = datetime(2026, 8, 1, tzinfo=UTC)
    await _append_results(sensor_id, count=2, base=base, state="ok")
    await _append_results(sensor_id, count=2, base=base + timedelta(minutes=5), state="critical")
    key = make_rsa_keypair("kid-results")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/sensors/{sensor_id}/results",
            params={"state": "critical"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    assert all(item["state"] == "critical" for item in items)


@pytest.mark.asyncio
async def test_rest_results_cross_tenant_returns_404(client: TestClient) -> None:
    """A tenant-B caller reading a tenant-A sensor's history gets 404, not 403."""
    sensor_id = await _seed_sensor(_TENANT_A)
    await _append_results(sensor_id, count=1, base=datetime(2026, 8, 1, tzinfo=UTC))
    key = make_rsa_keypair("kid-results")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/sensors/{sensor_id}/results",
            headers={"Authorization": f"Bearer {_token(key, tenant_id=_TENANT_B)}"},
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "sensor_not_found"


@pytest.mark.asyncio
async def test_rest_results_invalid_cursor_returns_422(client: TestClient) -> None:
    """A malformed cursor is a 422 sensor_results_invalid_cursor."""
    sensor_id = await _seed_sensor()
    key = make_rsa_keypair("kid-results")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/sensors/{sensor_id}/results",
            params={"cursor": "!!bad!!"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "sensor_results_invalid_cursor"


# ---------------------------------------------------------------------------
# Settings round-trip (#2737 discipline)
# ---------------------------------------------------------------------------


def test_settings_evidence_env_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CHECKS_EVIDENCE_* env round-trips through get_settings()."""
    monkeypatch.setenv("CHECKS_EVIDENCE_RETENTION_DAYS", "14")
    monkeypatch.setenv("CHECKS_EVIDENCE_PRUNE_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("CHECKS_EVIDENCE_PRUNE_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.checks_evidence_retention_days == 14
    assert settings.checks_evidence_prune_interval_seconds == 3600
    assert settings.checks_evidence_prune_enabled is False


def test_settings_evidence_defaults() -> None:
    """Defaults: 7-day retention, weekly cadence, prune enabled."""
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.checks_evidence_retention_days == 7
    assert settings.checks_evidence_prune_interval_seconds == 604800
    assert settings.checks_evidence_prune_enabled is True
