# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pytest fixtures and helpers for targets integration tests.

Import into test modules that exercise the /api/v1/targets router:

    from ._targets_helpers import (
        _settings_env,       # noqa: F401  (autouse fixture)
        _isolated_jwks_cache,  # noqa: F401  (autouse fixture)
        _empty_connector_registry,  # noqa: F401  (autouse fixture)
        _build_app,
        _admin_token,
        _operator_token,
        _insert_target,
    )

The three autouse fixtures must be imported at module level so pytest
registers them as autouse for every test in the importing module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
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


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=tenant_id
    )


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id
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
        "port": None,
        "fqdn": None,
        "secret_ref": None,
        "auth_model": "shared_service_account",
        "vpn_required": False,
        "extras": {},
        "notes": None,
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
