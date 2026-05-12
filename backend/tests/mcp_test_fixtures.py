# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pytest fixtures + helpers for the MCP test suite.

Every MCP-touching test file
(:mod:`tests.test_mcp_tool_meho_status`,
:mod:`tests.test_mcp_resource_tenant_info`,
:mod:`tests.test_mcp_audit`) needs the same constellation: a fixture
:class:`Operator` pinned to a known tenant, the Keycloak / Vault /
backplane env vars the chassis settings require, a
:class:`TestClient` whose ``verify_mcp_jwt_and_bind`` dependency is
overridden to that operator, and a registry-isolation fixture that
:func:`importlib.reload`s the production MCP tool / resource modules
so each test starts from a freshly-registered state.

Pre-T5 each file carried its own copy of these fixtures; SonarCloud
flagged the cross-file duplication as a Quality Gate failure on
``new_duplicated_lines_density > 3%``. Extracting the canonical
versions here and re-exporting via explicit imports from each test
module keeps the duplication count below the gate.

This module is intentionally **not** named ``conftest.py``: pytest's
conftest auto-discovery would otherwise pull these MCP-specific
autouse fixtures into every other test in :mod:`tests/`. Test files
that need them import explicitly with::

    from tests.mcp_test_fixtures import (
        client_with_operator,
        isolated_registry,
        operator_tenant_id,
        seeded_operator_tenant,
    )

The ``# noqa: F401`` markers on those imports silence ruff's "imported
but unused" check — pytest collects fixtures by name from the test
module's namespace, so the imports are load-bearing even though no
explicit call site exists.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import clear_registries
from meho_backplane.settings import get_settings

__all__ = [
    "OPERATOR_TENANT_ID",
    "build_operator",
    "client_with_operator",
    "isolated_registry",
    "post_mcp",
    "required_settings_env",
    "seeded_operator_tenant",
]


#: UUID pinned for the fixture operator's ``tenant_id``. Used by every
#: test that exercises tenant-bound MCP paths (resource handlers,
#: audit-row attribution).
OPERATOR_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-00000000a0a0")


def build_operator(role: TenantRole = TenantRole.READ_ONLY) -> Operator:
    """Build a fixture :class:`Operator` pinned to :data:`OPERATOR_TENANT_ID`."""
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
    )


@pytest.fixture(autouse=True)
def required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin Keycloak / Vault / backplane env vars every MCP test needs.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``. Helper-level
    tests (those that don't enter the FastAPI ``TestClient``) still
    call into ``get_settings()`` via :func:`get_sessionmaker` and
    would explode on a missing Keycloak knob otherwise. Pinning here
    makes every MCP test independently runnable regardless of fixture
    composition.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    """Reset the tool / resource registries then re-register production modules.

    Python's import cache makes the lifespan's
    :func:`~meho_backplane.mcp.registry.eager_import_mcp_modules` a
    no-op on the 2nd+ test in the same process — the top-level
    ``register_mcp_tool`` / ``register_mcp_resource`` calls only run
    on first import. :func:`importlib.reload` forces the module body
    to re-execute so each test starts from a known registered state
    regardless of cross-file ordering.

    Reloading both ``meho_status`` and ``tenant_info`` unconditionally
    is harmless even for tool-only / resource-only test files: the
    tests assert specific entries exist, not that the registry is
    minimal. Folding both reloads here keeps the autouse fixture
    single-shape across test files (the duplication driver Sonar
    flagged pre-T5).
    """
    from meho_backplane.mcp.resources import tenant_info
    from meho_backplane.mcp.tools import meho_status

    clear_registries()
    importlib.reload(meho_status)
    importlib.reload(tenant_info)
    yield
    clear_registries()


@pytest.fixture
def client_with_operator(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` with ``verify_mcp_jwt_and_bind`` overridden to a fixture operator.

    Enters ``TestClient(app)`` as a context manager so Starlette runs
    the FastAPI lifespan startup (and shutdown). Without the ``with``,
    ``TestClient.__init__`` would set up the transport but skip
    lifespan — meaning
    :func:`~meho_backplane.mcp.registry.eager_import_mcp_modules` would
    never fire, and a regression in lifespan-driven discovery would
    slip past every fixture-driven test.

    Operator role parameterisation: tests can request a non-default
    role via ``@pytest.mark.parametrize("client_with_operator", [TenantRole.X], indirect=True)``.
    Default is :class:`~meho_backplane.auth.operator.TenantRole.READ_ONLY`.
    """
    role: TenantRole = getattr(request, "param", TenantRole.READ_ONLY)
    op = build_operator(role)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@pytest.fixture
async def seeded_operator_tenant() -> None:
    """Insert a :class:`Tenant` row matching :data:`OPERATOR_TENANT_ID`.

    The autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` materialises the ``tenant`` table via
    ``alembic upgrade head``; this fixture populates the operator's
    row so resource handlers that query ``Tenant`` resolve a real
    record rather than ``None``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            Tenant(
                id=OPERATOR_TENANT_ID,
                slug="op-test-tenant",
                name="Operator Test Tenant",
            ),
        )


def post_mcp(client: TestClient, body: Any) -> Any:
    """POST a JSON-RPC envelope to ``/mcp`` and return the ``Response``."""
    return client.post("/mcp", json=body)
