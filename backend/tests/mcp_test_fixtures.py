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
from meho_backplane.db.models import DocCollection, Tenant
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
    "seed_doc_collection",
    "seeded_operator_tenant",
]


async def seed_doc_collection(
    *,
    collection_key: str = "vmware",
    status: str = "ready",
    backend: dict[str, Any] | None = None,
    tenant_id: UUID | None = None,
) -> None:
    """Insert a global :class:`DocCollection` row for collection-scoped tests.

    Defaults to a ``ready`` ``vmware`` collection bound to the
    ``corpus-http`` backend (no ``ref`` → the legacy global corpus URL the
    transport mock stands in for). Pass ``status="rebuilding"`` to exercise
    the not-ready arm, or a ``tenant_id`` to make it tenant-curated. The
    ``backend`` record is NOT NULL with no default, so a valid ``{type}``
    is always supplied.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            DocCollection(
                tenant_id=tenant_id,
                collection_key=collection_key,
                vendor="VMware by Broadcom",
                products=["vsphere", "nsx"],
                description="VMware vendor docs.",
                when_to_use="VMware product questions.",
                backend=backend if backend is not None else {"type": "corpus-http"},
                status=status,
            ),
        )


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

    Reloading ``meho_status`` and both resource modules unconditionally
    is harmless even for tool-only / resource-only test files: the
    tests assert specific entries exist, not that the registry is
    minimal. Folding the reloads here keeps the autouse fixture
    single-shape across test files (the duplication driver Sonar
    flagged pre-T5). ``tenant_feed`` (G6.1-T6a, #312) joins the
    reload list for the same reason — without it, this fixture's
    ``clear_registries()`` would leave the feed resource unregistered
    in any test file that imports the fixture after the first one
    runs. The G4.1-T3 kb meta-tools (``mcp.tools.knowledge``) and the
    matching ``meho://kb/{slug}`` resource (``mcp.resources.kb``) join
    the list for the same reason. The G0.9.1-T6 manual-seed admin tool
    (``mcp.tools.topology_create_node`` -- ``meho.topology.create_node``)
    lives in a separate module so the older
    ``mcp.tools.topology`` file does not grow further past the
    600-line code-quality guidance; it joins the reload list
    explicitly. The G9.1-T7 topology meta-tools
    (``mcp.tools.topology`` — ``query_topology`` + ``list_targets``)
    join the list for the same reason. The G5.1-T3 memory meta-tools
    (``mcp.tools.memory`` — ``search_memory`` + ``add_to_memory``) and
    the matching ``meho://memory/{scope}/{slug}`` resource
    (``mcp.resources.memory``) join for the same reason.
    """
    from meho_backplane.mcp.resources import docs as docs_resource
    from meho_backplane.mcp.resources import kb as kb_resource
    from meho_backplane.mcp.resources import memory as memory_resource
    from meho_backplane.mcp.resources import (
        tenant_conventions as tenant_conventions_resource,
    )
    from meho_backplane.mcp.resources import tenant_feed, tenant_info
    from meho_backplane.mcp.tools import (
        agent_grants,
        agent_runs,
        agents,
        approvals,
        audit,
        broadcast_overrides,
        connector_admin,
        connector_ingest,
        docs,
        knowledge,
        meho_status,
        operations,
        result_query,
        runbook_runs,
        runbooks,
        topology,
        topology_create_node,
    )
    from meho_backplane.mcp.tools import broadcast as broadcast_tools
    from meho_backplane.mcp.tools import (
        doc_collections as doc_collections_tools,
    )
    from meho_backplane.mcp.tools import memory as memory_tools
    from meho_backplane.mcp.tools import memory_promote as memory_promote_tool

    clear_registries()
    importlib.reload(meho_status)
    importlib.reload(operations)
    # G0.20-T7 (#1507): the JSONFlux handle read-back tool
    # (``result_query``) joins the reload list for the same reason every
    # other tool module does -- the autouse ``clear_registries()`` above
    # would otherwise leave it unregistered in any test file that imports
    # this fixture after the first one runs in the process.
    importlib.reload(result_query)
    importlib.reload(connector_admin)
    # G3.5-T2 (#1531): the connector-ingest MCP tools
    # (``meho.connector.ingest`` + ``meho.connector.ingest_status``) split
    # out of ``connector_admin`` into their own module; they join the
    # reload list for the same reason every other tool module does -- the
    # autouse ``clear_registries()`` above would otherwise leave them
    # unregistered in any test file that imports this fixture after the
    # first one runs in the process.
    importlib.reload(connector_ingest)
    importlib.reload(audit)
    importlib.reload(broadcast_overrides)
    # G6.4-T1 (#1091): meho.broadcast.recent (agent-facing read of recent
    # broadcast events). T2 (announce, #1092) and T3 (watch, #1093) will
    # land additional tools in this module; the single reload covers all
    # three because they share one file by design.
    importlib.reload(broadcast_tools)
    importlib.reload(knowledge)
    # G4.5-T4 (#1523): the capability-gated ``search_docs`` MCP tool +
    # its companion ``meho://docs/{product}/{version}/{chunk_id}``
    # resource join the reload list for the same reason every other MCP
    # module does -- the autouse ``clear_registries()`` above would
    # otherwise leave them unregistered in any test file that imports
    # this fixture after the first one runs in the process.
    importlib.reload(docs)
    # G4.6-T4 (#1553): the capability-gated ``list_doc_collections``
    # catalogue tool joins the reload list for the same reason every other
    # MCP tool module does -- the autouse ``clear_registries()`` above would
    # otherwise leave it unregistered in any test file that imports this
    # fixture after the first one runs in the process.
    importlib.reload(doc_collections_tools)
    importlib.reload(topology)
    importlib.reload(topology_create_node)
    # G12.2-T4 (#1298): the runbook template MCP tools
    # (``runbook_*_template`` x 6) join the reload list for the same
    # reason every other tool module does -- the autouse
    # ``clear_registries()`` above would otherwise leave them
    # unregistered in any test file that imports this fixture after
    # the first one runs in the process.
    importlib.reload(runbooks)
    # G12.3-T6 (#1313): the runbook *run* MCP tools (``runbook_start``
    # / ``runbook_next`` / ``runbook_abort`` / ``runbook_reassign`` /
    # ``runbook_list_runs``) join the reload list for the same reason
    # as the template-side module above. The five tools are imported
    # but unused here -- pytest collects them via the side-effect
    # ``register_mcp_tool`` calls in the module body, not by name.
    importlib.reload(runbook_runs)
    importlib.reload(memory_tools)
    importlib.reload(memory_promote_tool)
    # G11.1-T2 (#809): the agent-definition MCP tools join the reload
    # list for the same reason every other tool module does -- the
    # autouse clear_registries() above would otherwise leave them
    # unregistered in any test file that imports this fixture after the
    # first one runs in the process.
    importlib.reload(agents)
    # G11.1-T4 (#811): the agent invocation MCP tools (meho.agents.run +
    # meho.agents.run_status) join the reload list for the same reason.
    importlib.reload(agent_runs)
    # G11.2-T5 (#818) approvals + T6 (#819) agent grants: the MCP tool
    # modules (``meho.approvals.*`` and ``meho.agents.grant.*``) join
    # the reload list for the same reason every other tool module
    # does -- the autouse ``clear_registries()`` above would otherwise
    # leave them unregistered in any test file that imports this
    # fixture after the first one runs in the process. The negative
    # RBAC suites (G11.2 follow-up #1113) rely on this entry to
    # exercise ``tools/list`` filter + ``tools/call`` re-check gates.
    importlib.reload(agent_grants)
    importlib.reload(approvals)
    importlib.reload(tenant_info)
    importlib.reload(tenant_feed)
    importlib.reload(kb_resource)
    importlib.reload(docs_resource)
    importlib.reload(memory_resource)
    # G7.1-T4 (#316): the tenant-conventions per-slug resource
    # (``meho://tenant/{tenant_id}/conventions/{slug}``) joins the
    # reload list for the same reason every other resource module
    # does -- the autouse clear_registries() above would otherwise
    # leave it unregistered in any test file that imports this
    # fixture after the first one runs in the process.
    importlib.reload(tenant_conventions_resource)
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


def post_mcp(
    client: TestClient,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    """POST a JSON-RPC envelope to ``/mcp`` and return the ``Response``.

    ``headers`` lets a test exercise transport-level header handling
    (e.g. ``Mcp-Session-Id`` capture, G8.2-T2 #1010) without bypassing
    the shared helper.
    """
    return client.post("/mcp", json=body, headers=headers)
