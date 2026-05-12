# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broad-spectrum per-tenant isolation integration test (G0.1-T6 / #236).

This module is the **end-to-end** proof that the G0.1 tenancy chain
(T1 schema → T2 JWT extraction → T3 contextvar binding + AuditMiddleware
→ T4 RBAC) holds together as a system. Each predecessor Task has unit
tests for its narrow invariant; T6 exists to catch the class of bugs
that span the seams — e.g. a contextvar that leaks between concurrent
requests, an audit row that lands with the wrong ``tenant_id`` under
load, a future cross-tenant query helper missing its ``WHERE`` clause.

Coverage matrix (mirrors the five tests called out in the issue body):

1. ``test_audit_rows_correctly_scoped_per_tenant`` — two operators in
   two tenants generate distinct counts of audit rows; per-tenant
   query returns exactly the requestor's rows.
2. ``test_jwt_with_unknown_tenant_id_succeeds_but_isolates`` — v0.2's
   trust model is "the JWT issuer's tenant_id claim is authoritative".
   A signature-valid token carrying a tenant_id that has no
   :class:`Tenant` row still authenticates (no DB lookup), and its
   audit row lands under that bogus UUID — not under any pre-existing
   tenant. Documents the trust boundary so a future tightening
   (Goal #11.next: per-request tenant existence check) has an
   intentional spot to land.
3. ``test_per_tenant_audit_query_returns_only_requesting_tenant`` —
   forward-compat for G8 audit-query API: the naive helper
   :func:`fetch_audit_rows_for_tenant` returns only rows whose
   ``tenant_id`` matches. G8 will replace it with a full query API;
   T6 only pins the boundary contract.
4. ``test_read_only_role_on_operator_route_returns_403`` — sanity:
   T4's RBAC primitive integrates correctly through the auth
   dependency graph (read_only JWT → 403 on a route gated at
   ``require_role(OPERATOR)``).
5. ``test_concurrent_requests_do_not_leak_contextvars`` — the
   highest-value test. Two operators in two tenants run interleaved
   under :func:`asyncio.gather`; each request's audit row must carry
   the operator's own ``tenant_id``, never the sibling's. The bug
   class this catches — a structlog contextvar bound in one task
   bleeding into a concurrently-executing task — is almost impossible
   to spot by reading the middleware code, easy to spot by running
   this test.

Why real Postgres rather than SQLite:

The issue body calls it out explicitly. v0.2 production runs
PostgreSQL; SQLite happens to work for the unit suites' per-test
``alembic upgrade head`` shape but doesn't replicate JSONB / UUID /
index behaviour cleanly. The integration suite uses ``testcontainers``
+ ``postgres:16-alpine`` (same image pin as
:mod:`tests.test_migration_rollback` and
:mod:`tests.test_db_engine`); see :mod:`tests.integration.conftest`
for the fixture wiring. CI runners have Docker provisioned and run
the full class; agent sandboxes without Docker skip.

Why fake JWTs rather than a real Keycloak:

A real Keycloak roundtrip per test would add ~5s of container boot
plus realm seeding. The post-JWT-validation behaviour is what T6
exercises — audit + RBAC + tenancy. The JWT-validation path itself
is exhaustively covered in :mod:`tests.test_auth_jwt` /
:mod:`tests.test_auth_failures`. Fake JWTs minted by
:func:`tests._oidc_jwt_helpers.mint_token` against a stub JWKS keep
the focus on the seams T6 is meant to prove.

Why every PG-driven test body is ``async def``:

``backend/pyproject.toml`` pins ``asyncio_mode = "auto"`` for
pytest-asyncio, so plain ``async def`` test bodies (and ``async``
fixtures) all run on the single pytest-asyncio-managed event loop.
The :func:`tests.integration.conftest.pg_engine` async fixture binds
an asyncpg connection pool to that loop; calling ``asyncio.run()``
from inside a sync test body would spawn a *fresh* loop per call,
crossing loop boundaries on every pool checkout and tripping
SQLAlchemy's ``pool_pre_ping`` with
``RuntimeError: ... attached to a different loop``. Keeping every
PG-touching test ``async def`` (and using a single
:class:`httpx.AsyncClient` per test, awaited directly) is what keeps
the asyncpg pool, the request, and the read-back queries all on the
same loop.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from meho_backplane.auth.operator import TenantRole
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._vault_fakes import install_fake_vault
from tests.integration.conftest import (
    DOCKER_AVAILABLE,
    SKIP_REASON,
    count_audit_rows,
    fetch_audit_rows_for_tenant,
)

# ---------------------------------------------------------------------------
# Fixture: two operator identities pinned to distinct tenants
# ---------------------------------------------------------------------------


# Stable test-only tenant UUIDs. Pinned so failure diffs stay readable.
# Must match the seed rows inserted by ``pg_engine`` in the conftest —
# the seed there uses these exact UUIDs as string literals so this
# module doesn't have to import test-symbol state into the fixture.
TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"


# Class-level skip rather than module-level so the cheap import smoke at
# the bottom (``test_module_imports_cleanly``) still runs on no-Docker
# sandboxes — that's where a renamed fixture or removed export would
# bite first if it bypassed the Docker-gated suite.
_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    """Construct an :class:`httpx.AsyncClient` driving *app* via ASGI in-process.

    ASGITransport runs the FastAPI app inline in the same event loop
    the test awaits on — no real socket, no separate thread — which
    keeps the concurrent-request test deterministic. Hand-rolled
    rather than using :class:`fastapi.testclient.TestClient` because
    TestClient is sync; the concurrent-isolation test (case 5) needs
    the real async path through the middleware stack so that
    :func:`asyncio.gather` can actually interleave the requests across
    ``await`` boundaries.

    Reused across tests as a small factory so each test owns its own
    client lifecycle (``async with ...``) on the pytest-asyncio loop —
    which is the same loop the :func:`pg_engine` fixture's asyncpg
    pool was created on.
    """
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# Test 1 — audit rows are correctly tenant-scoped
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_audit_rows_correctly_scoped_per_tenant(
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: a missing ``WHERE tenant_id`` on a future audit-list
    helper, or an audit middleware that writes the wrong tenant_id.

    Operator A (in tenant A) makes 5 authenticated requests; operator
    B (in tenant B) makes 3. Per-tenant counts must match the issuance
    counts exactly and must not bleed across tenants.

    The issue body specifies the 5-vs-3 split; pinning those exact
    numbers keeps the assertion shape diff-friendly for reviewers.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-iso")

    token_a = mint_token(key, sub="op-a", tenant_id=TENANT_A_ID)
    token_b = mint_token(key, sub="op-b", tenant_id=TENANT_B_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            for _ in range(5):
                response = await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                assert response.status_code == 200, response.text
            for _ in range(3):
                response = await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_b}"},
                )
                assert response.status_code == 200, response.text

    rows_a = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_A_ID)
    rows_b = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_B_ID)

    assert len(rows_a) == 5, f"tenant A row leak/loss: {rows_a!r}"
    assert len(rows_b) == 3, f"tenant B row leak/loss: {rows_b!r}"
    # Cross-pollination guard — every row in A's slice must be A's
    # operator and every row in B's slice must be B's. A bug that
    # wrote A's tenant_id onto B's rows (or vice versa) would fail
    # this even if the count assertions above happened to pass.
    assert all(row["operator_sub"] == "op-a" for row in rows_a)
    assert all(row["operator_sub"] == "op-b" for row in rows_b)
    # Global row count matches the sum — no rows landed under a third,
    # unexpected tenant_id (e.g. a default UUID from an unbound
    # contextvar).
    total = await count_audit_rows(async_pg_url)
    assert total == 8, f"unexpected extra audit rows: total={total}"


# ---------------------------------------------------------------------------
# Test 2 — v0.2 trust model: signature-valid JWT with unknown tenant_id
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_jwt_with_unknown_tenant_id_succeeds_but_isolates(
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: a future change that quietly adds a tenant-existence
    check to ``verify_jwt`` without updating this contract test, OR a
    bug that maps unknown tenant_ids to the issuer's first known
    tenant (a confused-deputy class).

    v0.2 trusts the JWT issuer's ``tenant_id`` claim verbatim — no DB
    lookup against the ``tenant`` table. A token signed with a
    well-formed UUID that doesn't match any real tenant row still
    authenticates; its audit row carries that UUID. The pre-seeded
    tenant A row in the same DB must NOT inherit the request.

    When v0.2.next adds the per-request tenant lookup, this test's
    expected verdict flips to "401 unknown_tenant" — that change is a
    deliberate trust-model tightening and belongs in a separate Task,
    not a stealth edit to the integration suite.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-unknown")

    bogus_tenant_id = "99999999-9999-9999-9999-999999999999"
    token = mint_token(key, sub="op-bogus", tenant_id=bogus_tenant_id)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            response = await client.get(
                "/api/v1/health",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200, response.text

    bogus_rows = await fetch_audit_rows_for_tenant(async_pg_url, bogus_tenant_id)
    assert len(bogus_rows) == 1, f"expected one row under bogus tenant, got {bogus_rows!r}"
    # Critical isolation property: no row leaked to TENANT_A_ID. If a
    # future bug routed unknown tenant_ids to a fallback tenant
    # (operator misconfig, default-tenant feature), this assertion
    # catches it. The ``tenant`` table holds two real seed rows
    # (tenant-a, tenant-b) injected by the ``pg_engine`` fixture, so
    # this assertion has actual contrast to fail against.
    a_rows = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_A_ID)
    assert a_rows == []


# ---------------------------------------------------------------------------
# Test 3 — per-tenant audit-query helper returns only requesting tenant
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_per_tenant_audit_query_returns_only_requesting_tenant(
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: G8's future audit-query API forgetting a WHERE clause.

    Forward-compat probe for the G8 audit-query API. Today
    :func:`fetch_audit_rows_for_tenant` is a minimal SELECT in the
    test conftest; G8 will replace it with a full handler. This test
    pins the contract — "the per-tenant query returns only that
    tenant's rows" — so a missing ``WHERE tenant_id`` on the future
    handler fails the integration suite, not a customer demo.

    Two operators in two tenants issue requests interleaved
    sequentially; the per-tenant helper must return exactly each
    tenant's slice. Equivalent to Test 1 in spirit but framed around
    the query helper rather than the count.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-query")

    token_a = mint_token(key, sub="op-q-a", tenant_id=TENANT_A_ID)
    token_b = mint_token(key, sub="op-q-b", tenant_id=TENANT_B_ID)

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            # Interleave so a query that ignored tenant_id and returned
            # "the last N rows" would visibly mix operators.
            responses = [
                await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_a}"},
                ),
                await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_b}"},
                ),
                await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_a}"},
                ),
                await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token_b}"},
                ),
            ]
        # Catch a "request silently 500'd, audit row never landed"
        # failure mode before it bleeds into the row-count assertion
        # below — the count check would still flag it, but the status
        # assertion gives a clearer error message in CI logs.
        assert all(r.status_code == 200 for r in responses)

    rows_a = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_A_ID)
    rows_b = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_B_ID)

    # Explicit row-count assertions — without these a silent row-drop
    # (e.g. an INSERT that swallowed an error) would false-pass the
    # operator-sub set assertions below: ``{"op-q-a"}`` happily
    # matches a list of length one.
    assert len(rows_a) == 2, f"tenant A row leak/loss: {rows_a!r}"
    assert len(rows_b) == 2, f"tenant B row leak/loss: {rows_b!r}"
    assert {row["operator_sub"] for row in rows_a} == {"op-q-a"}
    assert {row["operator_sub"] for row in rows_b} == {"op-q-b"}
    # Each tenant got their own rows, no overlap.
    a_ids = {row["id"] for row in rows_a}
    b_ids = {row["id"] for row in rows_b}
    assert a_ids.isdisjoint(b_ids)


# ---------------------------------------------------------------------------
# Test 4 — read_only role on a route gated at OPERATOR returns 403
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_read_only_role_on_operator_route_returns_403(
    integration_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: a regression in :func:`require_role` that softens the
    gate, or a mis-wired dependency chain that silently skips RBAC.

    The fresh integration app mounts ``/api/v1/rbac-test/operator``
    unconditionally (the production app's ``MEHO_ENABLE_RBAC_TEST_ROUTE``
    env-var gate is bypassed for this suite — see
    :func:`tests.integration.conftest.build_integration_app`). A
    read_only operator's request must surface as 403 ``insufficient_role``.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-rbac")

    token = mint_token(
        key,
        sub="op-readonly",
        tenant_id=TENANT_A_ID,
        tenant_role=TenantRole.READ_ONLY.value,
    )

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            response = await client.get(
                "/api/v1/rbac-test/operator",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 403
    assert response.json() == {"detail": "insufficient_role"}


# ---------------------------------------------------------------------------
# Test 5 — contextvar isolation across concurrent requests
# ---------------------------------------------------------------------------


@_skip_no_docker
async def test_concurrent_requests_do_not_leak_contextvars(
    integration_app: FastAPI,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: structlog contextvars leaking across concurrent
    asyncio tasks.

    The highest-value test in T6 per the issue body. Four operators
    spread across two tenants (2x tenant A, 2x tenant B) fire requests
    concurrently via :func:`asyncio.gather`. After the gather resolves,
    each operator's audit row must carry their own tenant_id — never
    a sibling's. The plan deliberately interleaves
    tenant-A/B/A/B/A/B/A/B (eight requests) so a leak that occurred
    only after the first context switch would still surface.

    A single :class:`httpx.AsyncClient` is reused across the gather so
    the underlying ASGITransport's task-creation pattern matches what
    a real reverse-proxy fan-out would do — separate clients per
    request would serialise inside httpx's connection pool.

    The contract that must hold: every request opens its own ASGI
    scope, :class:`RequestContextMiddleware` calls
    :func:`structlog.contextvars.clear_contextvars` at scope entry,
    and :func:`verify_jwt_and_bind` binds ``operator_sub`` /
    ``tenant_id`` only for that scope. A bug that, for example,
    mutated a shared dict instead of using contextvars would race
    here and the wrong tenant_id would land on at least one row.

    The four-request lower bound stated in the issue's AC list is
    raised to eight here — concurrency bugs are racy, more samples
    make a flake-free pass more meaningful.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-concurrent")

    token_a1 = mint_token(key, sub="op-conc-a1", tenant_id=TENANT_A_ID)
    token_b1 = mint_token(key, sub="op-conc-b1", tenant_id=TENANT_B_ID)
    token_a2 = mint_token(key, sub="op-conc-a2", tenant_id=TENANT_A_ID)
    token_b2 = mint_token(key, sub="op-conc-b2", tenant_id=TENANT_B_ID)

    request_plan: list[tuple[str, str]] = [
        (token_a1, TENANT_A_ID),
        (token_b1, TENANT_B_ID),
        (token_a2, TENANT_A_ID),
        (token_b2, TENANT_B_ID),
        (token_a1, TENANT_A_ID),
        (token_b1, TENANT_B_ID),
        (token_a2, TENANT_A_ID),
        (token_b2, TENANT_B_ID),
    ]

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            coros = [
                client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
                for token, _expected in request_plan
            ]
            responses = await asyncio.gather(*coros)

    # Every request returned 200 — concurrency itself didn't break
    # the auth or audit paths.
    for idx, response in enumerate(responses):
        assert response.status_code == 200, (
            f"request {idx} ({request_plan[idx]!r}) returned {response.status_code}: "
            f"{response.text!r}"
        )

    # Every operator's audit rows carry that operator's expected
    # tenant_id — no cross-contamination. Per-operator counts must
    # match the gather plan (each op appears twice; eight requests
    # total).
    plan_summary: dict[str, dict[str, str | int]] = {}
    for token_sub, expected_tenant in (
        ("op-conc-a1", TENANT_A_ID),
        ("op-conc-a2", TENANT_A_ID),
        ("op-conc-b1", TENANT_B_ID),
        ("op-conc-b2", TENANT_B_ID),
    ):
        plan_summary[token_sub] = {
            "expected_tenant": expected_tenant,
            "expected_count": 2,
        }

    rows_a = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_A_ID)
    rows_b = await fetch_audit_rows_for_tenant(async_pg_url, TENANT_B_ID)
    assert len(rows_a) == 4, f"tenant A got {len(rows_a)} rows, expected 4: {rows_a!r}"
    assert len(rows_b) == 4, f"tenant B got {len(rows_b)} rows, expected 4: {rows_b!r}"

    # No row from tenant A carries a B-operator sub, and vice versa.
    # A contextvar leak would manifest here as a row whose
    # ``operator_sub`` and ``tenant_id`` disagree — the leak's signature
    # is "this row is attributed to the wrong tenant".
    for row in rows_a:
        assert row["operator_sub"] in {"op-conc-a1", "op-conc-a2"}, (
            f"tenant A row attributed to wrong operator (contextvar leak?): {row!r}"
        )
    for row in rows_b:
        assert row["operator_sub"] in {"op-conc-b1", "op-conc-b2"}, (
            f"tenant B row attributed to wrong operator (contextvar leak?): {row!r}"
        )

    # Per-operator row counts — each operator fired exactly twice.
    # Failing this assertion means a request dropped silently or a
    # response was attributed to the wrong operator's audit row,
    # both of which are contextvar-isolation bugs.
    for sub, plan in plan_summary.items():
        sub_count = sum(
            1
            for row in (rows_a if plan["expected_tenant"] == TENANT_A_ID else rows_b)
            if row["operator_sub"] == sub
        )
        assert sub_count == plan["expected_count"], (
            f"operator {sub}: got {sub_count} rows, expected {plan['expected_count']}"
        )


# ---------------------------------------------------------------------------
# Module-level smoke: keep import-time errors visible even on no-Docker
# sandboxes where the skip mark fires above. Mirrors the same idiom
# used by tests/test_migration_rollback.py.
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """Soft contract: every public symbol the suite reaches for exists.

    The body references the symbols verbatim so a removed export or a
    renamed helper surfaces here at collection time even when the
    Docker-gated tests above are skipped. Cheap (no Docker, no PG,
    no event loop), catches the failure mode "someone renamed a
    fixture and the integration tests fail to collect on the runner
    that *does* have Docker".
    """
    assert callable(make_rsa_keypair)
    assert callable(mint_token)
    assert callable(mock_discovery_and_jwks)
    assert callable(public_jwks)
    assert callable(install_fake_vault)
    assert callable(fetch_audit_rows_for_tenant)
    assert callable(count_audit_rows)
    # Pin the test-only tenant UUIDs so a future edit can't silently
    # change them and degrade diff readability across reviewers.
    uuid.UUID(TENANT_A_ID)
    uuid.UUID(TENANT_B_ID)
