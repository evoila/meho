# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pytest fixtures for the acceptance suite.

The acceptance tests verify shipped substrates against the consumer's
real input (e.g. the G0.7 vSphere canary feeds the actual vCenter
OpenAPI specs through the ingestion pipeline). Most of the chassis
plumbing they need — a real PostgreSQL container with pgvector + FTS,
the production audit middleware, the JWKS / Keycloak mock — already
exists in :mod:`tests.integration.conftest`.

Three pieces are re-exported verbatim:

* ``async_pg_url`` — module-scoped Postgres container with the
  migration tree applied.
* ``integration_env`` — function-scoped env pinning that overrides
  ``DATABASE_URL`` at the testcontainer URL.

One piece is parallel rather than re-exported:

* ``pg_engine`` — the integration suite's per-test TRUNCATE statement
  hard-codes ``TRUNCATE TABLE audit_log, documents, tenant`` (lifted
  pre-G6.3 / G7.1 / G9.1, all of which added ``tenant_id REFERENCES
  tenant(id)`` foreign keys via migrations 0007 and 0008). Running it
  against the head schema fails with ``Table "graph_node" references
  "tenant"`` because PG demands the multi-table TRUNCATE list every
  referring table or use CASCADE.

  The pragmatic fix is the acceptance-local fixture below, which
  TRUNCATES every chassis table that holds a tenant-scoped row (FK
  or soft) — superset of the integration list, future-proof against
  the next migration. The integration suite's bug is filed as a
  separate follow-up; this conftest works around it for the canary
  without modifying the integration conftest.

Why not just re-export everything: the rename / re-export pattern is
fine for pure data fixtures (URLs, env), but stateful resource
fixtures (engine binding + table truncation) benefit from being
parallel so the acceptance suite's lifecycle is auditable from its
own conftest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import (
    create_engine_for_url,
    dispose_engine,
    reset_engine_for_testing,
)
from meho_backplane.settings import get_settings
from tests.acceptance._canary_fixtures import (
    CANARY_CONNECTOR_ID,
    CANARY_OPERATOR_TENANT,
    IngestedCanaryVcsim,
    acceptance_operator,
    ingested_canary_vcsim,
    prewarmed_embeddings,
)
from tests.acceptance._harbor_canary_fixtures import (
    HARBOR_CANARY_OPERATOR_TENANT,
    IngestedHarborCanary,
    harbor_acceptance_operator,
    ingested_harbor_canary,
)
from tests.acceptance._nsx_canary_fixtures import (
    NSX_CANARY_OPERATOR_TENANT,
    IngestedNsxCanary,
    ingested_nsx_canary,
    nsx_acceptance_operator,
)
from tests.acceptance._robot_canary_fixtures import (
    ROBOT_CANARY_OPERATOR_TENANT,
    IngestedRobotCanary,
    ingested_robot_canary,
    ingested_robot_canary_sandbox,
    robot_acceptance_operator,
)
from tests.acceptance._sddc_canary_fixtures import (
    SDDC_CANARY_OPERATOR_TENANT,
    IngestedSddcCanary,
    ingested_sddc_canary,
    sddc_acceptance_operator,
)
from tests.acceptance._vcsim import (
    DEFAULT_VCSIM_TOPOLOGY,
    VcsimEndpoint,
    VcsimTopology,
    resolve_vcsim_endpoint,
)
from tests.acceptance._vrops_canary_fixtures import (
    VROPS_CANARY_OPERATOR_TENANT,
    IngestedVropsCanary,
    ingested_vrops_canary,
    vrops_acceptance_operator,
)
from tests.integration.conftest import (
    _CHASSIS_ENV,
    DOCKER_AVAILABLE,
    SKIP_REASON,
    async_pg_url,
    integration_env,
)

__all__ = [
    "CANARY_CONNECTOR_ID",
    "CANARY_OPERATOR_TENANT",
    "DEFAULT_VCSIM_TOPOLOGY",
    "DOCKER_AVAILABLE",
    "HARBOR_CANARY_OPERATOR_TENANT",
    "NSX_CANARY_OPERATOR_TENANT",
    "ROBOT_CANARY_OPERATOR_TENANT",
    "SDDC_CANARY_OPERATOR_TENANT",
    "SKIP_REASON",
    "VROPS_CANARY_OPERATOR_TENANT",
    "IngestedCanaryVcsim",
    "IngestedHarborCanary",
    "IngestedNsxCanary",
    "IngestedRobotCanary",
    "IngestedSddcCanary",
    "IngestedVropsCanary",
    "VcsimEndpoint",
    "VcsimTopology",
    "acceptance_operator",
    "async_pg_url",
    "harbor_acceptance_operator",
    "ingested_canary_vcsim",
    "ingested_harbor_canary",
    "ingested_nsx_canary",
    "ingested_robot_canary",
    "ingested_robot_canary_sandbox",
    "ingested_sddc_canary",
    "ingested_vrops_canary",
    "integration_env",
    "nsx_acceptance_operator",
    "pg_engine",
    "prewarmed_embeddings",
    "robot_acceptance_operator",
    "sddc_acceptance_operator",
    "vcsim_endpoint",
    "vrops_acceptance_operator",
]


#: Tables that hold tenant-scoped rows (FK to ``tenant(id)`` or a soft
#: ``tenant_id`` column). The TRUNCATE statement lists every entry so
#: PG's FK-referenced-by-X check passes for each tenant row removed.
#:
#: Maintenance: when a future migration adds another tenant-scoped
#: table, list it here. The acceptance fixture's behaviour is "wipe
#: every chassis-managed table that could carry per-test residue
#: between runs"; a missing entry yields cross-test pollution rather
#: than the FK-truncate error the integration conftest hits today.
#:
#: Order does not matter for ``TRUNCATE TABLE a, b, c`` — PG resolves
#: all the FK checks together. Keeping the list alphabetical so a
#: diff reads cleanly.
_TRUNCATE_TABLES: tuple[str, ...] = (
    # ``agent_definition`` carries a real FK ``tenant(id)`` per migration
    # ``0016`` (#809 G11.1-T2). Listed here so the per-test TRUNCATE stays
    # non-cascading once that table exists.
    "agent_definition",
    # ``agent_permission.tenant_id`` is a real ``REFERENCES tenant(id)`` FK
    # from migration ``0022`` (G11.2-T3 #820). Same rule as the siblings
    # below: PG rejects truncating ``tenant`` unless every referencing
    # table is listed in the same statement, so ``agent_permission`` must
    # appear here or every PG-backed acceptance test errors at setup with
    # ``cannot truncate a table referenced in a foreign key constraint``.
    "agent_permission",
    # ``agent_principal.tenant_id`` is a real ``REFERENCES tenant(id)`` FK
    # from migration ``0018`` (G11.2-T1 #815). PG rejects truncating
    # ``tenant`` unless every referencing table is listed in the same
    # statement, so ``agent_principal`` must appear here or every PG-backed
    # acceptance test errors at setup with ``cannot truncate a table
    # referenced in a foreign key constraint``.
    "agent_principal",
    # ``agent_run.tenant_id`` is a real ``REFERENCES tenant(id)`` FK from
    # migration ``0017`` (G11.1-T6 #813). PG rejects truncating ``tenant``
    # unless every referencing table is listed in the same statement, so
    # ``agent_run`` must appear here or every PG-backed acceptance test
    # errors at setup with ``cannot truncate a table referenced in a
    # foreign key constraint``.
    "agent_run",
    # ``approval_request.tenant_id`` is a real ``REFERENCES tenant(id)`` FK
    # from migration ``0023`` (G11.2-T4 #817). Same rule: PG rejects
    # truncating ``tenant`` unless every referencing table is listed in
    # the same statement.
    "approval_request",
    "audit_log",
    "broadcast_override",
    "documents",
    "endpoint_descriptor",
    # ``event_outbox.tenant_id`` is a real ``REFERENCES tenant(id)`` FK from
    # migration ``0027`` (G11.3-T3 #824). PG rejects truncating ``tenant``
    # unless every referencing table is listed in the same statement, so
    # ``event_outbox`` must appear here or every PG-backed acceptance test
    # errors at setup with ``cannot truncate a table referenced in a
    # foreign key constraint`` (the recurring fixture gotcha #1064 / #1065).
    "event_outbox",
    # ``graph_edge_history`` carries a real FK ``graph_edge(id) ON DELETE
    # SET NULL`` per migration ``0012`` (#856 T1); the same applies to
    # ``graph_node_history``. PG rejects a TRUNCATE on the parent table
    # unless the referencing tables are truncated in the same statement
    # (``cannot truncate a table referenced in a foreign key constraint``).
    # Without these two entries the per-test TRUNCATE the acceptance
    # fixture runs raises ``FeatureNotSupportedError`` and every
    # PG-backed acceptance test errors at setup. The integration conftest's
    # ``_TRUNCATE_TABLES`` already includes both; the acceptance side was
    # missed when T1 landed and is repaired here together with T2's hook.
    "graph_edge",
    "graph_edge_history",
    "graph_node",
    "graph_node_history",
    # ``identity_budget.tenant_id`` is a real ``REFERENCES tenant(id)`` FK
    # from migration ``0031`` (G11.5-T5 #1079). Same rule: PG rejects
    # truncating ``tenant`` unless every referencing table is listed in
    # the same statement, so ``identity_budget`` must appear here or every
    # PG-backed acceptance test errors at setup with ``cannot truncate a
    # table referenced in a foreign key constraint``.
    "identity_budget",
    "operation_group",
    # ``runner_assignments.tenant_id`` and ``runner_check_results.tenant_id``
    # are real ``REFERENCES tenant(id)`` FKs from migration ``0059``
    # (Initiative #2415 T3, #2499). Same rule as ``runner_principal`` below:
    # PG rejects truncating ``tenant`` unless every referencing table is listed
    # in the same statement, so both must appear here.
    "runner_assignments",
    "runner_check_results",
    # ``runner_principal.tenant_id`` is a real ``REFERENCES tenant(id)`` FK
    # from migration ``0058`` (Initiative #2415 T6, #2502). Same rule: PG
    # rejects truncating ``tenant`` unless every referencing table is listed
    # in the same statement, so ``runner_principal`` must appear here or
    # every PG-backed acceptance test errors at setup with ``cannot truncate
    # a table referenced in a foreign key constraint``.
    "runner_principal",
    # ``scheduled_trigger.tenant_id`` and ``.agent_definition_id`` are real
    # ``REFERENCES`` FKs from migration ``0020`` (G11.3-T1 #822); the table
    # must be listed so the non-cascading multi-table TRUNCATE can drop
    # ``tenant`` / ``agent_definition`` without a FK-constraint error.
    "scheduled_trigger",
    "targets",
    "tenant",
)


@pytest.fixture(autouse=True)
def _acceptance_default_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Pin chassis env vars Settings() requires, for every acceptance test.

    The autouse ``_integration_default_env`` fixture that does this for
    the integration suite lives in :mod:`tests.integration.conftest`,
    so pytest scopes it to ``tests/integration/`` only — it does **not**
    apply to ``tests/acceptance/``. Acceptance tests that load
    :class:`Settings` without depending on a fixture that transitively
    pins the env (e.g. ``test_g81_audit_query_acceptance`` via
    ``pg_engine`` → ``integration_env``, which only overrides
    ``DATABASE_URL``) therefore die with
    ``KeyError: 'KEYCLOAK_ISSUER_URL'`` at ``settings.py``'s eager
    ``os.environ["..."]`` access.

    Reuses :data:`tests.integration.conftest._CHASSIS_ENV` verbatim so
    the two suites cannot drift. Mirrors the autouse-for-invariants
    discipline of ``_integration_default_env`` (#679) and the unit-level
    ``_default_database_url`` fixture; brackets the yield with
    ``get_settings.cache_clear()`` / ``clear_jwks_cache()`` so neither
    cache bleeds between tests.
    """
    for key, value in _CHASSIS_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()

    yield

    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
async def pg_engine(integration_env: None, async_pg_url: str) -> AsyncIterator[None]:
    """Inject the testcontainer engine + truncate every chassis table per test.

    Differs from :func:`tests.integration.conftest.pg_engine` in one
    bounded way: the TRUNCATE statement lists every tenant-scoped
    table the production schema knows about (see
    :data:`_TRUNCATE_TABLES`) rather than just the three the
    integration suite listed before G6.3 / G9.1 landed their FK
    columns. This is the workaround the canary needs to run locally;
    the integration conftest itself stays untouched (the bug there
    is filed as a follow-up).

    Other behaviour matches the integration conftest verbatim:
    truncate before yield, re-seed the two pinned tenant rows
    (``TENANT_A_ID`` / ``TENANT_B_ID``) so any consumer test
    expecting them as table state still passes, dispose the engine
    on teardown to release the pool.
    """
    reset_engine_for_testing()
    eng = create_engine_for_url(async_pg_url, pool_size=5, pool_timeout=10.0)
    engine_module._engine = eng

    async with eng.connect() as conn:
        truncate_sql = "TRUNCATE TABLE " + ", ".join(_TRUNCATE_TABLES)
        await conn.execute(text(truncate_sql))
        await conn.execute(
            text(
                "INSERT INTO tenant (id, slug, name) VALUES "
                "('11111111-1111-1111-1111-111111111111', 'tenant-a', 'Tenant A'), "
                "('22222222-2222-2222-2222-222222222222', 'tenant-b', 'Tenant B')"
            )
        )
        await conn.commit()

    try:
        yield
    finally:
        await dispose_engine()
        reset_engine_for_testing()


@pytest.fixture(scope="session")
def vcsim_endpoint() -> Iterator[VcsimEndpoint]:
    """Yield a session-scoped :class:`VcsimEndpoint` for vcsim-backed tests.

    Boots ``vmware/vcsim`` once per pytest invocation (or returns a
    cached endpoint when ``MEHO_VCSIM_URL`` overrides) and tears the
    container down on session teardown. Session scope amortises the
    ~3-second boot across every dispatch / handle / agent-flow test
    in the acceptance suite; per-test isolation comes from the seed
    topology being immutable (read-only ops only).

    Mirrors the ``pg_engine`` skip pattern: missing Docker socket
    plus missing env-override yields a clean ``pytest.skip`` from
    inside :func:`resolve_vcsim_endpoint`, so the consuming test is
    marked SKIPPED rather than failed.

    The 50-VM / 3-host / 1-cluster / 2-datastore / 2-folder topology
    in :data:`DEFAULT_VCSIM_TOPOLOGY` matches the issue body's
    canary fixture; tests that need a different seed boot their own
    container with :func:`resolve_vcsim_endpoint` directly.
    """
    with resolve_vcsim_endpoint(DEFAULT_VCSIM_TOPOLOGY) as endpoint:
        yield endpoint
