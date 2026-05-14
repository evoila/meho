# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for G0.3-T1.5 (Task #477) schema remediation.

Coverage matrix (#477 acceptance criteria):

* **Migration round-trip** — ``alembic upgrade head`` lands ``0009``;
  ``downgrade -1`` removes both columns; ``upgrade head`` re-applies
  cleanly. Exercised against the SQLite engine the autouse fixture
  bootstraps; the dialect-portability discipline established by the
  prior migrations means the same shape holds against PostgreSQL.
* **ORM round-trip** — :attr:`Target.fingerprint` and
  :attr:`Target.preferred_impl_id` accept ``None`` (default), accept a
  populated dict / string, and round-trip via the ORM's
  ``async_sessionmaker`` against the autouse SQLite engine.
* **Pydantic schema rejection** —
  :class:`~meho_backplane.targets.schemas.TargetCreate` and
  :class:`~meho_backplane.targets.schemas.TargetUpdate` reject the
  ``fingerprint`` field with 422 (``extra='forbid'``); both accept the
  ``preferred_impl_id`` field.
* **Probe-persist round-trip** — ``POST /api/v1/targets/{name}/probe``
  against a mock connector returning a fixed :class:`FingerprintResult`
  writes the JSON-safe ``model_dump(mode='json')`` to
  ``targets.fingerprint``; a subsequent ``GET`` returns the persisted
  value. ``ConfigDict(frozen=True)`` on the input model + the
  ``MappingProxyType`` wrap in :class:`FingerprintResult` mean the
  serialized dict is a snapshot, not a reference to the connector's
  internal state.
* **501 path leaves the row untouched** — when no connector is
  registered for the target's product, the probe route returns 501
  and the ``fingerprint`` column retains whatever it had before
  (``NULL`` in the test setup).
* **Probe-failure path leaves the row untouched** — when the
  connector's ``fingerprint`` method raises, the exception propagates
  through the route, the outer transactional ``get_session`` rolls
  back, and the ``fingerprint`` column retains its prior value.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the auth-related env vars the targets router needs."""
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
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _empty_connector_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(targets_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id)


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=tenant_id
    )


async def _insert_target(**kwargs: Any) -> TargetORM:
    """Insert a TargetORM row directly via the test sessionmaker."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "name": "default-target",
        "product": "ssh",
        "host": "10.0.0.1",
        "aliases": [],
        "vpn_required": False,
        "auth_model": "shared_service_account",
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    t = TargetORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(t)
        await session.commit()
    return t


async def _fetch_target_by_name(tenant_id: uuid.UUID, name: str) -> TargetORM | None:
    """Re-read a target row from the test DB to verify persistence."""
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(TargetORM).where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.name == name,
            )
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Migration round-trip — 0009 up/down/up
# ---------------------------------------------------------------------------


def test_migration_0009_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``alembic upgrade head`` lands 0009; ``downgrade -1`` reverts cleanly.

    The autouse ``_default_database_url`` fixture in conftest already
    runs ``upgrade head`` against a per-test SQLite file; this test
    builds its **own** dedicated DB to exercise the downgrade arrow
    (downgrade against the autouse DB would leave the next test's
    DB-migration probe surprised to see a missing column). The
    isolation also lets the test assert the column-set transition
    deterministically.
    """
    db_path = tmp_path / "migration_roundtrip.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)

    # Upgrade to head — should include migration 0009.
    command.upgrade(cfg, "head")

    # Inspect via the sync engine path Alembic itself uses internally —
    # PRAGMA table_info returns the column set the migration created.
    from sqlalchemy import create_engine, text

    sync_url = url.replace("sqlite+aiosqlite", "sqlite")
    sync_engine = create_engine(sync_url)
    with sync_engine.connect() as conn:
        columns_at_head = {
            row[1] for row in conn.execute(text("PRAGMA table_info(targets)")).fetchall()
        }
    assert "fingerprint" in columns_at_head, "0009 upgrade did not add fingerprint column"
    assert "preferred_impl_id" in columns_at_head, (
        "0009 upgrade did not add preferred_impl_id column"
    )

    # Downgrade one revision — should remove both columns.
    command.downgrade(cfg, "-1")
    with sync_engine.connect() as conn:
        columns_after_downgrade = {
            row[1] for row in conn.execute(text("PRAGMA table_info(targets)")).fetchall()
        }
    assert "fingerprint" not in columns_after_downgrade, (
        "0009 downgrade did not drop fingerprint column"
    )
    assert "preferred_impl_id" not in columns_after_downgrade, (
        "0009 downgrade did not drop preferred_impl_id column"
    )

    # Re-upgrade to head — should be idempotent on the second pass too.
    command.upgrade(cfg, "head")
    with sync_engine.connect() as conn:
        columns_after_reupgrade = {
            row[1] for row in conn.execute(text("PRAGMA table_info(targets)")).fetchall()
        }
    assert "fingerprint" in columns_after_reupgrade
    assert "preferred_impl_id" in columns_after_reupgrade

    sync_engine.dispose()


# ---------------------------------------------------------------------------
# ORM round-trip — Target.fingerprint / Target.preferred_impl_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_round_trip_new_columns() -> None:
    """Both new columns accept None and populated values via the ORM."""
    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)

    # Insert with both fields NULL — the default for additive migrations.
    await _insert_target(
        tenant_id=tenant_id,
        name="rdc-vault",
        product="vault",
        host="vault.corp.internal",
    )
    fetched = await _fetch_target_by_name(tenant_id, "rdc-vault")
    assert fetched is not None
    assert fetched.fingerprint is None
    assert fetched.preferred_impl_id is None

    # Insert with both fields populated — verify round-trip semantics.
    fingerprint_dict = {
        "vendor": "hashicorp",
        "product": "vault",
        "version": "1.15.0",
        "build": None,
        "edition": None,
        "reachable": True,
        "probed_at": "2026-05-14T18:00:00Z",
        "probe_method": "sys-health",
        "extras": {"namespace": "ops"},
    }
    await _insert_target(
        tenant_id=tenant_id,
        name="rdc-k8s",
        product="kubernetes",
        host="kubernetes.corp.internal",
        fingerprint=fingerprint_dict,
        preferred_impl_id="kubernetes-py-1.28",
    )
    fetched = await _fetch_target_by_name(tenant_id, "rdc-k8s")
    assert fetched is not None
    assert fetched.fingerprint == fingerprint_dict
    assert fetched.preferred_impl_id == "kubernetes-py-1.28"
    # Reference identity matters — ORM should hand back a dict the
    # caller can read as ``Mapping[str, Any]`` (no surprise unwrapping).
    assert isinstance(fetched.fingerprint, dict)
    assert fetched.fingerprint["extras"]["namespace"] == "ops"


# ---------------------------------------------------------------------------
# Pydantic schema rejection — TargetCreate / TargetUpdate
# ---------------------------------------------------------------------------


def test_target_create_rejects_fingerprint_field() -> None:
    """``TargetCreate`` is server-managed-fingerprint; sending it raises 422.

    The amendment-driven invariant: clients cannot seed the G0.6 resolver's
    tie-break input with fabricated fingerprint values. ``extra='forbid'``
    on the schema enforces this at the Pydantic layer, which FastAPI
    surfaces as HTTP 422.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        TargetCreate(
            name="t",
            product="vault",
            host="vault.corp.internal",
            fingerprint={"vendor": "fabricated"},  # type: ignore[call-arg]
        )
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("fingerprint",) for e in errors), (
        f"expected an error on the fingerprint field, got: {errors}"
    )
    # ``extra_forbidden`` is the pydantic v2 error type for ``extra='forbid'``.
    assert any(e["type"] == "extra_forbidden" for e in errors), (
        f"expected extra_forbidden error type, got: {[e['type'] for e in errors]}"
    )


def test_target_create_accepts_preferred_impl_id() -> None:
    """``TargetCreate`` accepts the operator-override ``preferred_impl_id`` field."""
    t = TargetCreate(
        name="t",
        product="vault",
        host="vault.corp.internal",
        preferred_impl_id="vault-cli-1.15",
    )
    assert t.preferred_impl_id == "vault-cli-1.15"


def test_target_update_rejects_fingerprint_field() -> None:
    """``TargetUpdate`` is server-managed-fingerprint; PATCHing it raises 422."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        TargetUpdate(fingerprint={"vendor": "fabricated"})  # type: ignore[call-arg]
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("fingerprint",) for e in errors)
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_target_update_accepts_preferred_impl_id() -> None:
    """``TargetUpdate`` accepts the operator-override ``preferred_impl_id`` field."""
    u = TargetUpdate(preferred_impl_id="vault-cli-1.15")
    assert u.preferred_impl_id == "vault-cli-1.15"


# ---------------------------------------------------------------------------
# Probe-persist round-trip via the FastAPI surface
# ---------------------------------------------------------------------------


class _FixedFingerprintConnector(Connector):
    """Test connector that returns a fixed FingerprintResult.

    The fingerprint is constructed lazily so each instance returns a
    fresh ``probed_at`` timestamp matching the test's wall-clock — the
    persisted JSON column reflects the value returned, not a frozen
    module-level constant.
    """

    product = "vault"

    async def probe(self, target: Any) -> ProbeResult:  # pragma: no cover
        raise NotImplementedError

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        return FingerprintResult(
            vendor="hashicorp",
            product="vault",
            version="1.15.0",
            build="2026-04-01",
            reachable=True,
            probed_at=datetime(2026, 5, 14, 18, 0, 0, tzinfo=UTC),
            probe_method="sys-health",
            extras={"cluster_id": "abc-123"},
        )

    async def execute(  # pragma: no cover  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


class _RaisingConnector(Connector):
    """Test connector whose ``fingerprint`` always raises."""

    product = "vault"

    async def probe(self, target: Any) -> ProbeResult:  # pragma: no cover
        raise NotImplementedError

    async def fingerprint(self, target: Any) -> FingerprintResult:  # type: ignore[override]
        raise RuntimeError("connector simulated failure")

    async def execute(  # pragma: no cover  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_probe_persists_fingerprint_to_db(client: TestClient) -> None:
    """Probe → DB row has the connector's FingerprintResult JSON-encoded."""
    register_connector("vault", _FixedFingerprintConnector)

    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
    )

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/prod-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 200
    body = response.json()
    # Response is the FingerprintResult shape.
    assert body["vendor"] == "hashicorp"
    assert body["product"] == "vault"
    assert body["version"] == "1.15.0"
    assert body["reachable"] is True

    # And it landed in the DB row.
    fetched = await _fetch_target_by_name(tenant_id, "prod-vault")
    assert fetched is not None
    assert fetched.fingerprint is not None
    assert fetched.fingerprint["vendor"] == "hashicorp"
    assert fetched.fingerprint["product"] == "vault"
    assert fetched.fingerprint["version"] == "1.15.0"
    assert fetched.fingerprint["reachable"] is True
    # ``model_dump(mode='json')`` produces an ISO-8601 string for the
    # datetime, not a Python ``datetime`` — the explicit assertion
    # makes the JSONB-safety contract a regression-protected property.
    assert fetched.fingerprint["probed_at"] == "2026-05-14T18:00:00Z"
    assert fetched.fingerprint["extras"] == {"cluster_id": "abc-123"}


@pytest.mark.asyncio
async def test_probe_persists_and_describe_returns_it(client: TestClient) -> None:
    """GET /api/v1/targets/{name} surfaces the persisted fingerprint."""
    register_connector("vault", _FixedFingerprintConnector)

    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
        preferred_impl_id="vault-cli-1.15",
    )

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        probe_response = client.post(
            "/api/v1/targets/prod-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
        assert probe_response.status_code == 200
        describe_response = client.get(
            "/api/v1/targets/prod-vault",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
    assert describe_response.status_code == 200
    data = describe_response.json()
    assert data["preferred_impl_id"] == "vault-cli-1.15"
    assert data["fingerprint"] is not None
    assert data["fingerprint"]["vendor"] == "hashicorp"
    assert data["fingerprint"]["reachable"] is True


# ---------------------------------------------------------------------------
# Non-mutation paths — 501 + connector raise leave the DB row untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_501_does_not_mutate_db(client: TestClient) -> None:
    """501 (no connector) path must NOT touch ``targets.fingerprint``."""
    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="orphan-nsx",
        product="nsx",  # No connector registered for this product.
        host="10.0.0.1",
    )

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/orphan-nsx/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 501

    # The DB row must still have NULL fingerprint — the 501 path
    # returns before any write. A transactional rollback would also
    # protect us, but the explicit assertion makes the
    # 501-before-write contract a regression-protected property.
    fetched = await _fetch_target_by_name(tenant_id, "orphan-nsx")
    assert fetched is not None
    assert fetched.fingerprint is None


@pytest.mark.asyncio
async def test_probe_connector_raise_does_not_mutate_db() -> None:
    """A connector that raises must leave ``targets.fingerprint`` untouched.

    The outer ``async with session.begin()`` in
    :func:`~meho_backplane.db.engine.get_session` rolls back on the
    propagating exception; the column retains its prior value.

    ``TestClient`` is constructed with ``raise_server_exceptions=False``
    so FastAPI's default 500 response shape is surfaced as an HTTP
    response rather than re-raised through the test boundary — the
    test's contract is "the DB row is untouched", not "the framework
    converts RuntimeError to 500".
    """
    register_connector("vault", _RaisingConnector)

    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="flaky-vault",
        product="vault",
        host="vault.corp.internal",
    )

    # Build a dedicated TestClient that surfaces server errors as 500
    # rather than re-raising — the default ``raise_server_exceptions=True``
    # mode is useful for debugging but bypasses the framework's
    # transactional-rollback shape we want to exercise here.
    raise_safe_client = TestClient(_build_app(), raise_server_exceptions=False)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = raise_safe_client.post(
            "/api/v1/targets/flaky-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 500

    fetched = await _fetch_target_by_name(tenant_id, "flaky-vault")
    assert fetched is not None
    assert fetched.fingerprint is None


@pytest.mark.asyncio
async def test_probe_overwrites_existing_fingerprint(client: TestClient) -> None:
    """A second successful probe overwrites the cached fingerprint.

    The column always reflects the *last* successful probe; this is
    the explicit contract from the route docstring. Failure modes
    (501, connector raise) preserve prior state; success overwrites.
    """
    register_connector("vault", _FixedFingerprintConnector)

    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
        fingerprint={"vendor": "stale", "product": "vault", "version": "0.0.0"},
    )

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets/prod-vault/probe",
            headers={"Authorization": f"Bearer {_operator_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 200

    fetched = await _fetch_target_by_name(tenant_id, "prod-vault")
    assert fetched is not None
    assert fetched.fingerprint is not None
    assert fetched.fingerprint["vendor"] == "hashicorp", (
        "second successful probe should have overwritten the stale fingerprint"
    )
    assert fetched.fingerprint["version"] == "1.15.0"


# ---------------------------------------------------------------------------
# REST surface — create/update accept preferred_impl_id, reject fingerprint
# ---------------------------------------------------------------------------


def test_create_target_accepts_preferred_impl_id(client: TestClient) -> None:
    """``POST /api/v1/targets`` accepts a ``preferred_impl_id`` body field."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "vault-override",
                "product": "vault",
                "host": "vault.corp.internal",
                "preferred_impl_id": "vault-cli-1.15",
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 201
    data = response.json()
    assert data["preferred_impl_id"] == "vault-cli-1.15"
    assert data["fingerprint"] is None


def test_create_target_rejects_fingerprint_field_422(client: TestClient) -> None:
    """``POST /api/v1/targets`` returns 422 when ``fingerprint`` is supplied."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/targets",
            json={
                "name": "vault-spoofed",
                "product": "vault",
                "host": "vault.corp.internal",
                "fingerprint": {"vendor": "fabricated"},
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 422
    # The Pydantic error must name the offending field.
    body = response.json()
    assert any(
        "fingerprint" in str(item.get("loc", ()))
        for item in (body.get("detail") if isinstance(body.get("detail"), list) else [])
    ), f"expected fingerprint to be flagged in 422 body, got: {body}"


@pytest.mark.asyncio
async def test_update_target_rejects_fingerprint_field_422(client: TestClient) -> None:
    """``PATCH /api/v1/targets/{name}`` returns 422 when ``fingerprint`` is supplied."""
    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/prod-vault",
            json={"fingerprint": {"vendor": "fabricated"}},
            headers={"Authorization": f"Bearer {_admin_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_target_accepts_preferred_impl_id(client: TestClient) -> None:
    """``PATCH /api/v1/targets/{name}`` accepts ``preferred_impl_id``."""
    tenant_id = uuid.UUID(DEFAULT_TENANT_ID)
    await _insert_target(
        tenant_id=tenant_id,
        name="prod-vault",
        product="vault",
        host="vault.corp.internal",
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            "/api/v1/targets/prod-vault",
            json={"preferred_impl_id": "vault-cli-1.15"},
            headers={"Authorization": f"Bearer {_admin_token(key, str(tenant_id))}"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["preferred_impl_id"] == "vault-cli-1.15"
