# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pytest fixtures and helpers for event_source integration tests.

Mirrors :mod:`tests._targets_helpers`. Import into test modules that
exercise the /api/v1/event-sources router:

    from ._event_source_helpers import (
        _settings_env,          # noqa: F401  (autouse fixture)
        _isolated_jwks_cache,   # noqa: F401  (autouse fixture)
        _build_app,
        _admin_token,
        _operator_token,
        _insert_event_source,
    )
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI

from meho_backplane.api.v1.event_source import router as event_source_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EventSource as EventSourceORM
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, mint_token
from ._oidc_jwt_helpers import ISSUER as _ISSUER


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(event_source_router)
    return app


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=tenant_id
    )


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id)


async def _insert_event_source(**kwargs: Any) -> EventSourceORM:
    """Insert an EventSourceORM row directly via the test sessionmaker."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "name": "default-source",
        "slug": "default-source",
        "kind": "alertmanager",
        "auth_strategy": "hmac-sha256",
        "secret_ref": None,
        "status": "active",
        "extras": {},
        "created_by_sub": "admin-1",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    row = EventSourceORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(row)
        await session.commit()
    return row
