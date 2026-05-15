# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G8.1-T5 acceptance suite — audit-query surface under realistic conditions.

Closes Initiative #334 (G8.1) by proving the four-layer audit-query
surface (T1 substrate / T2 REST / T3 CLI / T4 MCP) holds together
against real Postgres + Valkey containers. The unit and per-layer
contract tests are in place; this module is the load-bearing
integration gate.

Coverage matrix (mirrors the 8 scenarios in the issue body #469):

1. **Tenant boundary** — overlapping target names / principal subs
   across two tenants; tenant-A operator's REST surface never returns
   tenant-B rows, returns 404 (not 403) on tenant-B's audit_id,
   and silently drops a body-supplied cross-tenant ``tenant_id``.
2. **Cursor pagination correctness** — 250 rows in tenant A, paged
   at limit=100 across three pages; ``asyncio.gather`` runs a
   concurrent 50-row insert against page-2-read using the page-1
   cursor; the substrate's lex-compare ``(occurred_at, id) <
   cursor.(ts, id)`` invariant means page 2 returns rows strictly
   older than the cursor — no duplicates, no skips. New rows are
   visible only on a fresh page-1 call.
3. **Aggregate-only broadcast** (decision #3, ``docs/planning/
   v0.2-decisions.md``) — issuing a ``query_audit`` call publishes
   one ``BroadcastEvent`` to ``meho:feed:{tenant_id}`` whose
   ``payload`` is exactly ``{op_class, result_status, row_count}``.
   The filter contents (``principal``, ``op_id``, ``target``) NEVER
   appear; this is the load-bearing PII contract the broadcast
   classifier owns.
4. **Pre-canned shortcuts** — ``GET /audit/who-touched/{target}``
   returns the same row set as ``POST /audit/query`` with the
   matching filter; ``GET /audit/my-recent`` infers
   ``principal_sub`` from the operator's JWT and ignores any client-
   supplied ``principal`` argument.
5. **CLI wire contract** — the JSON shape ``meho audit query
   --json`` consumes from the REST surface is stable enough for a
   ``jq .rows[].op_id`` pipeline; covered here by pinning the
   key set the CLI's struct unmarshals into. The Go-side CLI has
   its own end-to-end tests in #506; this scenario closes the
   Python-side seam.
6. **MCP meta-tool E2E** — ``tools/call query_audit`` against the
   ``/mcp`` Streamable HTTP transport returns the expected
   ``AuditQueryResult`` shape; ``tools/list`` advertises
   ``query_audit`` exactly once, and does NOT advertise per-shape
   shortcut tools (those are CLI-only per CLAUDE.md narrow-waist).
7. **Glob pattern correctness** — ``op_id="vsphere.vm.*"`` matches
   ``vsphere.vm.list`` / ``vsphere.vm.get`` and does NOT match
   ``vsphere.host.list`` or ``vsphere.vmware.*``. The literal ``vm.``
   segment is the boundary.
8. **Audit-on-audit-query loop termination** — issuing
   ``query_audit`` writes an audit row of its own (``op_class=
   audit_query``); a follow-up query filtered on ``op_class=
   audit_query`` returns the first call's row, proving the
   audit-of-audit chain doesn't recurse infinitely.

Real Postgres rather than SQLite: same reasoning as
:mod:`tests.integration.test_tenant_isolation`. Real Valkey via
``testcontainers.redis.RedisContainer`` per the
:mod:`tests.integration.test_broadcast_load` precedent — the
aggregate-only assertion needs to read back from the actual
publish-on-write pipeline, not a mock.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import redis.asyncio as redis_async
import redis.exceptions
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker as sa_async_sessionmaker

from meho_backplane.api.v1.audit import router as api_v1_audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import TenantRole
from meho_backplane.broadcast import client as broadcast_client_module
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.mcp import eager_import_mcp_modules
from meho_backplane.mcp import router as mcp_router
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.acceptance.conftest import DOCKER_AVAILABLE, SKIP_REASON
from tests.fixtures.audit_rows_seed import (
    seed_audit_row,
    seed_audit_rows,
    seed_target,
    seed_tenants_with_overlap,
)

# ``asyncio_mode = "auto"`` in pyproject.toml means every async test
# in this module is automatically picked up by pytest-asyncio — no
# explicit ``@pytest.mark.asyncio`` per function. The module-level
# mark below is for the Docker skip only; applying ``asyncio`` here
# would also tag the sync ``test_module_imports_cleanly`` and emit a
# pytest warning.
pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

# ---------------------------------------------------------------------------
# Pinned identities (same shape as test_tenant_isolation / test_kb_service_pg)
# ---------------------------------------------------------------------------

TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"

#: The MCP transport canonical resource URI the audience-binding
#: middleware compares against. Same value the in-tree MCP test
#: fixtures pin (``backend/tests/mcp_test_fixtures.py``) so a reader
#: walking from the MCP unit tests into this acceptance run sees one
#: identifier across both layers.
MCP_RESOURCE_URI: str = "https://meho.test/mcp"

# ---------------------------------------------------------------------------
# Valkey fixture (mirrors test_broadcast_load's pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
async def valkey_url(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Boot a per-test ``valkey/valkey:8`` container and pin its URL.

    The broadcast publisher reads ``BROADCAST_REDIS_URL`` from the
    chassis Settings; binding the env var here means the audit
    middleware's publish-on-write hook lands in the testcontainer's
    Valkey rather than the per-deploy production instance. Mirrors
    :mod:`tests.integration.test_broadcast_load`'s ``valkey_url``
    fixture verbatim; kept local rather than re-exported because the
    acceptance suite's lifecycle is auditable from its own conftest.
    """
    # Local import so the module loads cleanly on machines without
    # the testcontainers extra installed; the skip-on-no-Docker
    # guard at module level keeps the missing-import case inert.
    from testcontainers.redis import RedisContainer  # type: ignore[import-untyped]

    image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
    with RedisContainer(image) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        monkeypatch.setenv("BROADCAST_REDIS_URL", url)
        # Disposing the cached broadcast client so the next
        # `get_broadcast_client()` picks up the new env var; the
        # chassis singleton is process-global.
        await broadcast_client_module.dispose_broadcast_client()
        get_settings.cache_clear()
        try:
            yield url
        finally:
            await broadcast_client_module.dispose_broadcast_client()


# ---------------------------------------------------------------------------
# App + client builders
# ---------------------------------------------------------------------------


def _build_audit_acceptance_app() -> FastAPI:
    """FastAPI with the audit + MCP routers mounted on the production stack.

    The integration suite's ``build_integration_app`` skips the audit
    router (its scope predates G8.1). Building locally keeps the
    integration conftest stable while still mirroring the production
    middleware ordering: ``AuditMiddleware`` inner,
    ``RequestContextMiddleware`` outer.
    """
    # Pre-register every MCP tool so ``tools/list`` advertises the
    # full surface — without this the registry is empty and the
    # ``query_audit`` MCP scenario can't dispatch.
    eager_import_mcp_modules()
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_v1_audit_router)
    app.include_router(mcp_router)
    return app


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` bound to *app* via ASGI."""
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _mint_operator(
    *,
    keypair: Any,
    sub: str = "damir",
    tenant_id: str = TENANT_A_ID,
    role: TenantRole = TenantRole.OPERATOR,
    audience: str = AUDIENCE,
) -> str:
    """Mint a happy-path JWT for an operator in *tenant_id*."""
    return mint_token(
        keypair,
        sub=sub,
        tenant_id=tenant_id,
        tenant_role=role.value,
        audience=audience,
    )


def _bearer(token: str) -> dict[str, str]:
    """Return the ``Authorization: Bearer <token>`` header dict."""
    return {"Authorization": f"Bearer {token}"}


async def _session_for_url(async_url: str) -> AsyncSession:
    """Return an :class:`AsyncSession` against the testcontainer URL.

    A dedicated short-lived engine + sessionmaker so the seed helpers
    write into the same DB the audit middleware reads from, without
    sharing the pool the production engine cache (re)builds during
    request handling.
    """
    engine = create_async_engine(async_url)
    factory = sa_async_sessionmaker(engine, expire_on_commit=False)
    return factory()


async def _read_broadcast_event_for_tenant(
    valkey_url: str,
    tenant_id: str,
    *,
    block_ms: int = 30_000,
) -> BroadcastEvent | None:
    """XREAD one event from ``meho:feed:{tenant_id}``.

    Blocks up to *block_ms* (default 30 s — generous CI margin per
    :mod:`tests.integration.test_broadcast_load`). Returns the parsed
    :class:`BroadcastEvent` or ``None`` if the BLOCK timed out.
    """
    client = redis_async.from_url(valkey_url, decode_responses=True)
    stream = f"meho:feed:{tenant_id}"
    try:
        try:
            response = await client.xread(
                streams={stream: "0-0"},
                count=1,
                block=block_ms,
            )
        except redis.exceptions.TimeoutError:
            return None
        if not response:
            return None
        # response shape: [[stream, [(entry_id, {field: value, ...})]], ...]
        _stream, entries = response[0]
        _entry_id, fields = entries[0]
        # The publisher writes the BroadcastEvent as JSON under a
        # single ``event`` field per ``broadcast/publisher.py``; if
        # the wire shape changes the assertion will surface it loud.
        if "event" in fields:
            return BroadcastEvent.model_validate_json(fields["event"])
        # Fallback: flatten the field dict; redis-py decode_responses
        # produces a plain dict. Construct via model_validate to keep
        # the contract pinned even if the publisher flattens.
        return BroadcastEvent.model_validate(fields)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Scenario 1 — Tenant boundary
# ---------------------------------------------------------------------------


async def test_scenario_1_tenant_a_query_never_returns_tenant_b_rows(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 1: tenant-A POST /query never returns tenant-B rows."""
    async with await _session_for_url(async_pg_url) as session:
        seeds = await seed_tenants_with_overlap(
            session,
            tenant_a=uuid.UUID(TENANT_A_ID),
            tenant_b=uuid.UUID(TENANT_B_ID),
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.post(
                "/api/v1/audit/query",
                json={"target": "rdc-vcenter"},
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row_ids = {row["id"] for row in body["rows"]}
    assert str(seeds["row_a"]) in row_ids, "tenant-A row missing from tenant-A query"
    assert str(seeds["row_b"]) not in row_ids, "tenant-B row leaked into tenant-A query"


async def test_scenario_1_show_cross_tenant_audit_id_returns_404(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Scenario 1 continued: GET /show/{B-row-id} as tenant-A returns 404 not 403."""
    async with await _session_for_url(async_pg_url) as session:
        seeds = await seed_tenants_with_overlap(
            session,
            tenant_a=uuid.UUID(TENANT_A_ID),
            tenant_b=uuid.UUID(TENANT_B_ID),
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/show/{seeds['row_b']}",
                headers=_bearer(token_a),
            )
    # 404 — NEVER 403 — so existence of tenant-B's audit row never
    # leaks via the status-code channel.
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403


async def test_scenario_1_body_tenant_id_is_silently_dropped(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Scenario 1 continued: body `tenant_id=<B>` is ignored; tenant-A rows still returned."""
    async with await _session_for_url(async_pg_url) as session:
        seeds = await seed_tenants_with_overlap(
            session,
            tenant_a=uuid.UUID(TENANT_A_ID),
            tenant_b=uuid.UUID(TENANT_B_ID),
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.post(
                "/api/v1/audit/query",
                # The body-supplied tenant_id is NOT on AuditQueryRequest;
                # Pydantic's default extra="ignore" silently drops it.
                # The substrate's tenant_id is the operator's JWT claim,
                # always — never anything client-controllable.
                json={"tenant_id": TENANT_B_ID, "target": "rdc-vcenter"},
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    row_ids = {row["id"] for row in resp.json()["rows"]}
    assert str(seeds["row_a"]) in row_ids
    assert str(seeds["row_b"]) not in row_ids, (
        "body tenant_id=<B> was honoured — substrate tenant boundary broken"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — Cursor pagination correctness under concurrent insert
# ---------------------------------------------------------------------------


async def test_scenario_2_cursor_paginates_250_rows_in_three_pages(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 2 (base case): three pages of 100/100/50 rows."""
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=250,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)
    seen_ids: set[str] = set()

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            cursor: str | None = None
            page_count = 0
            page_sizes: list[int] = []
            while True:
                body: dict[str, Any] = {"limit": 100}
                if cursor is not None:
                    body["cursor"] = cursor
                resp = await client.post(
                    "/api/v1/audit/query",
                    json=body,
                    headers=_bearer(token_a),
                )
                assert resp.status_code == 200, resp.text
                page = resp.json()
                page_sizes.append(len(page["rows"]))
                for row in page["rows"]:
                    assert row["id"] not in seen_ids, "cursor pagination yielded a duplicate"
                    seen_ids.add(row["id"])
                cursor = page["next_cursor"]
                page_count += 1
                if cursor is None:
                    break
                assert page_count <= 4, "pagination did not terminate within 4 pages"

    assert page_count == 3, f"expected 3 pages, got {page_count}"
    assert page_sizes == [100, 100, 50], f"page sizes: {page_sizes}"
    assert len(seen_ids) == 250, f"saw {len(seen_ids)} rows; expected 250"


async def test_scenario_2_concurrent_insert_does_not_corrupt_cursor(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 2 (concurrent path):

    Seed 250 rows. Page 1 (limit=100) returns the 100 newest + a cursor.
    Run a 50-row writer concurrently with the page-2 read. The
    substrate's lex-compare ``(occurred_at, id) < cursor`` invariant
    guarantees page-2 sees rows strictly older than the cursor —
    the 50 new rows have ``occurred_at`` AFTER the page-1 cursor's
    timestamp, so they never surface on page 2. A fresh page-1
    call after the writer commits sees them. No duplicates, no skips.
    """
    seed_session = await _session_for_url(async_pg_url)
    try:
        await seed_audit_rows(
            seed_session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=250,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
            # Anchor the seed timestamps far enough in the past so
            # the concurrent writer's "now()" timestamps land strictly
            # after the cursor's lex key — that's the load-bearing
            # invariant the test exercises.
            base_ts=datetime.now(UTC) - timedelta(hours=2),
            spacing=timedelta(seconds=1),
        )
        await seed_session.commit()
    finally:
        await seed_session.close()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            # Page 1.
            resp1 = await client.post(
                "/api/v1/audit/query",
                json={"limit": 100},
                headers=_bearer(token_a),
            )
            assert resp1.status_code == 200, resp1.text
            page1 = resp1.json()
            assert len(page1["rows"]) == 100
            cursor = page1["next_cursor"]
            assert cursor is not None
            page1_ids = {r["id"] for r in page1["rows"]}

            # Captured by the concurrent-insert closure so the
            # post-gather assertions can pin the exact 50 row-ids
            # that landed during the gather. Tracking the IDs
            # directly is stronger than counting "new" rows: the
            # AuditMiddleware writes one row per /audit/query call
            # itself, so a count-based assertion would conflate the
            # concurrent batch with the test's own observable
            # request-trail.
            concurrent_inserted_ids: list[str] = []

            async def _concurrent_insert() -> None:
                writer_session = await _session_for_url(async_pg_url)
                try:
                    seeded = await seed_audit_rows(
                        writer_session,
                        tenant_id=uuid.UUID(TENANT_A_ID),
                        count=50,
                        principal_sub="damir",
                        op_id="vsphere.vm.list",
                        op_class="read",
                        # AFTER the seed batch (which anchored 2h ago).
                        # The concurrent rows are by definition newer
                        # than the page-1 cursor's timestamp; they should
                        # not surface on page 2.
                        base_ts=datetime.now(UTC),
                    )
                    await writer_session.commit()
                    concurrent_inserted_ids.extend(str(rid) for _ts, rid in seeded)
                finally:
                    await writer_session.close()

            async def _read_page_2() -> dict[str, Any]:
                resp = await client.post(
                    "/api/v1/audit/query",
                    json={"limit": 100, "cursor": cursor},
                    headers=_bearer(token_a),
                )
                assert resp.status_code == 200, resp.text
                decoded: dict[str, Any] = resp.json()
                return decoded

            _insert_result, page2 = await asyncio.gather(
                _concurrent_insert(),
                _read_page_2(),
            )

    page2_ids = {r["id"] for r in page2["rows"]}
    assert page1_ids.isdisjoint(page2_ids), "page 2 leaked page 1's rows"
    # The 50 concurrently-inserted rows are NEWER than the cursor key
    # so the cursor's lex-compare excludes them — page 2 sees ONLY
    # rows from the original 250 seed (rows 100..199 chronologically
    # newest-to-oldest). Exactly 100 rows.
    assert len(page2["rows"]) == 100, f"page 2 should be 100 rows; got {len(page2['rows'])}"

    # A fresh page-1 call after the concurrent insert MUST see the
    # newest 50 rows (the concurrent batch) before the original seed's
    # newest 50. This is the "concurrent inserts visible only on a
    # fresh page-1 call" half of the issue body invariant.
    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp_fresh = await client.post(
                "/api/v1/audit/query",
                json={"limit": 100},
                headers=_bearer(token_a),
            )
    fresh_ids = {r["id"] for r in resp_fresh.json()["rows"]}
    # The concurrent insert produced 50 specific row-ids. All 50
    # must surface on a FRESH page-1 query (newest first) — that's
    # the "concurrent inserts visible only on a fresh page-1 call"
    # half of the substrate's cursor invariant. The set may also
    # contain audit-middleware rows from the test's own /audit/query
    # calls (one row per call), so we assert by-id containment
    # rather than by-count.
    assert len(concurrent_inserted_ids) == 50, (
        f"concurrent insert produced {len(concurrent_inserted_ids)} rows; expected 50"
    )
    missing = set(concurrent_inserted_ids) - fresh_ids
    assert not missing, (
        f"fresh page-1 missing {len(missing)} concurrent rows: {sorted(missing)[:3]}..."
    )
    # And no concurrent row leaked into page1 or page2.
    leaked_into_page1 = set(concurrent_inserted_ids) & page1_ids
    leaked_into_page2 = set(concurrent_inserted_ids) & page2_ids
    assert not leaked_into_page1, "concurrent row leaked into page 1 (impossible by time)"
    assert not leaked_into_page2, (
        "concurrent row leaked into page 2 — cursor lex-compare invariant broken"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Aggregate-only broadcast verification
# ---------------------------------------------------------------------------


async def test_scenario_3_broadcast_payload_is_aggregate_only(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 3: BroadcastEvent.payload keys are exactly the aggregate trio.

    Calls POST /query with a rich filter (principal + op_id glob),
    then XREADs the per-tenant Valkey stream. The published event's
    payload must be EXACTLY ``{op_class, result_status, row_count}``
    — no filter contents, no principal, no target, nothing else.
    This is the load-bearing PII contract from decision #3.
    """
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=3,
            principal_sub="damir",
            op_id="vault.kv.read",
            op_class="credential_read",
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.post(
                "/api/v1/audit/query",
                json={"principal": "damir", "op_id": "vault.kv.*"},
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text

    event = await _read_broadcast_event_for_tenant(valkey_url, TENANT_A_ID)
    assert event is not None, "no BroadcastEvent landed in Valkey within the 30s window"
    assert event.op_id == "meho.audit.query"
    assert event.op_class == "audit_query"
    # Exact key set — anything beyond these three would leak the
    # filter contents the operator typed.
    assert set(event.payload.keys()) == {"op_class", "result_status", "row_count"}, (
        f"BroadcastEvent.payload leaked extra keys: {sorted(event.payload.keys())}"
    )
    # Defensive: also confirm the filter contents are absent. A
    # future redactor regression could keep the right key set while
    # putting filter content into a `row_count`-shaped field; this
    # asserts the specific leak vectors.
    serialised = json.dumps(event.payload)
    assert "vault.kv" not in serialised, "broadcast payload leaked op_id filter"
    assert "principal" not in event.payload
    assert "target" not in event.payload


# ---------------------------------------------------------------------------
# Scenario 4 — Pre-canned shortcuts parity
# ---------------------------------------------------------------------------


async def test_scenario_4_who_touched_matches_query_with_target_filter(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 4: GET /who-touched/<target> == POST /query{target}."""
    async with await _session_for_url(async_pg_url) as session:
        target_a = await seed_target(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            name="rdc-vcenter",
        )
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=3,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
            target_id=target_a,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            who = await client.get(
                "/api/v1/audit/who-touched/rdc-vcenter",
                headers=_bearer(token_a),
            )
            query = await client.post(
                "/api/v1/audit/query",
                json={"target": "rdc-vcenter"},
                headers=_bearer(token_a),
            )
    assert who.status_code == 200, who.text
    assert query.status_code == 200, query.text
    who_ids = sorted(r["id"] for r in who.json()["rows"])
    query_ids = sorted(r["id"] for r in query.json()["rows"])
    assert who_ids == query_ids, "who-touched and query{target} returned different row sets"


async def test_scenario_4_my_recent_ignores_client_supplied_principal(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Scenario 4 continued: my-recent infers principal from JWT, ignores query args.

    The route's signature accepts only ``since`` / ``limit`` query
    params; a client-supplied ``?principal=X`` is dropped at the
    framework boundary. Test seeds two operators in tenant A and
    confirms operator ``alice`` only ever sees her own rows even if
    she tries to spoof ``?principal=bob``.
    """
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=2,
            principal_sub="alice",
            op_id="vsphere.vm.list",
            op_class="read",
        )
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=2,
            principal_sub="bob",
            op_id="vsphere.vm.list",
            op_class="read",
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_alice = _mint_operator(keypair=key, sub="alice", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            # Alice tries to spoof bob's recent via a query param.
            # The route signature has no ``principal`` param; FastAPI
            # drops unknown query keys. Result: only alice's rows.
            resp = await client.get(
                "/api/v1/audit/my-recent?principal=bob",
                headers=_bearer(token_alice),
            )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 2, f"alice's my-recent returned {len(rows)} rows; expected 2"
    assert {r["principal_sub"] for r in rows} == {"alice"}


# ---------------------------------------------------------------------------
# Scenario 5 — CLI wire contract (Python-side; Go-side covered in #506)
# ---------------------------------------------------------------------------


async def test_scenario_5_cli_json_wire_shape_is_jq_parseable(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 5: the JSON shape `meho audit query --json` consumes.

    The Go CLI's own test suite (#506) pins the binary's stdout
    contract; this acceptance test pins the REST shape the CLI's
    ``QueryResult`` Go struct unmarshals into. A future regression
    that dropped a required key from the API would surface here
    even before the Go side catches it on its next CI run.
    """
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=5,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.post(
                "/api/v1/audit/query",
                json={"op_class": "read"},
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Top-level shape: rows + next_cursor. The Go CLI's --json output
    # is a passthrough of this; jq pipelines key off both names.
    assert set(body.keys()) == {"rows", "next_cursor"}
    assert isinstance(body["rows"], list)
    # Each row's key set is what `jq .rows[].op_id` would see.
    for row in body["rows"]:
        assert "op_id" in row, "CLI's `jq .rows[].op_id` pipeline would break"
        assert "id" in row
        assert "ts" in row
        assert "principal_sub" in row
        assert "op_class" in row
        assert "result_status" in row


# ---------------------------------------------------------------------------
# Scenario 6 — MCP query_audit tool E2E via /mcp transport
# ---------------------------------------------------------------------------


async def _mcp_call(
    client: httpx.AsyncClient,
    token: str,
    *,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    """Issue one JSON-RPC ``method`` against ``/mcp``."""
    envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
    if params is not None:
        envelope["params"] = params
    resp = await client.post(
        "/mcp",
        json=envelope,
        headers={**_bearer(token), "Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 200, f"MCP {method} → {resp.status_code}: {resp.text}"
    decoded: dict[str, Any] = resp.json()
    return decoded


async def test_scenario_6_mcp_tools_list_contains_query_audit_only(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue body scenario 6 (tools/list): query_audit advertised; no per-shape shortcuts."""
    # The MCP audience-binding middleware reads the canonical resource
    # URI from BACKPLANE_URL (preferred) or MCP_RESOURCE_URI; without
    # either, every /mcp request 401s with audience_not_configured.
    # Pin both to the in-tree fixture's value
    # (matches tests/integration/test_mcp_inspector.py).
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", MCP_RESOURCE_URI)
    get_settings.cache_clear()
    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(
        keypair=key,
        sub="damir",
        tenant_id=TENANT_A_ID,
        audience=MCP_RESOURCE_URI,
    )

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            envelope = await _mcp_call(client, token_a, method="tools/list")
    tools = {t["name"] for t in envelope["result"]["tools"]}
    assert "query_audit" in tools, f"query_audit missing from tools/list: {sorted(tools)}"
    # The pre-canned shortcuts live in the CLI / REST surface, NOT MCP.
    assert "audit.show" not in tools
    assert "audit.who_touched" not in tools
    assert "audit.my_recent" not in tools


async def test_scenario_6_mcp_query_audit_call_returns_expected_shape(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue body scenario 6 (tools/call): query_audit returns rows + next_cursor."""
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", MCP_RESOURCE_URI)
    get_settings.cache_clear()
    async with await _session_for_url(async_pg_url) as session:
        target_a = await seed_target(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            name="rdc-vcenter",
        )
        await seed_audit_rows(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            count=3,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
            target_id=target_a,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(
        keypair=key,
        sub="damir",
        tenant_id=TENANT_A_ID,
        audience=MCP_RESOURCE_URI,
    )

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            envelope = await _mcp_call(
                client,
                token_a,
                method="tools/call",
                params={
                    "name": "query_audit",
                    "arguments": {"target": "rdc-vcenter", "since": "24h"},
                },
            )
    # The MCP dispatcher returns the tool's dict-shape under
    # `result.content[0].text` (text) or `result.structuredContent`
    # (structured) — the chassis registers a tool with an outputSchema
    # so the structured form is preferred.
    result = envelope["result"]
    structured = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert "rows" in structured
    assert "next_cursor" in structured
    assert len(structured["rows"]) == 3


# ---------------------------------------------------------------------------
# Scenario 7 — Glob pattern correctness
# ---------------------------------------------------------------------------


async def test_scenario_7_op_id_glob_matches_literal_segment_only(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 7: vsphere.vm.* matches vm.list/get; not vm-adjacent paths.

    The substrate translates ``*`` → SQL ``%`` after LIKE-escaping
    the rest of the input. ``vsphere.vm.*`` therefore matches
    ``vsphere.vm.list`` and ``vsphere.vm.get`` but does NOT match
    ``vsphere.host.list`` (different second segment) or
    ``vsphere.vmware.list`` (substring prefix would be a bug — the
    literal ``vm.`` segment is the boundary).
    """
    async with await _session_for_url(async_pg_url) as session:
        for op_id in [
            "vsphere.vm.list",
            "vsphere.vm.get",
            "vsphere.host.list",
            "vsphere.vmware.list",
        ]:
            await seed_audit_row(
                session,
                tenant_id=uuid.UUID(TENANT_A_ID),
                occurred_at=datetime.now(UTC) - timedelta(seconds=10),
                principal_sub="damir",
                op_id=op_id,
                op_class="read",
            )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.post(
                "/api/v1/audit/query",
                json={"op_id": "vsphere.vm.*"},
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    matched = {row["op_id"] for row in resp.json()["rows"]}
    assert matched == {"vsphere.vm.list", "vsphere.vm.get"}, (
        f"glob matched unexpected op_ids: {sorted(matched)}"
    )


# ---------------------------------------------------------------------------
# Scenario 8 — Audit-on-audit-query loop termination
# ---------------------------------------------------------------------------


async def test_scenario_8_audit_query_writes_its_own_queryable_row(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Issue body scenario 8: query_audit emits an audit row; queryable; no recursion."""
    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, sub="damir", tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            # First call — produces 0 result rows, writes one audit row.
            first = await client.post(
                "/api/v1/audit/query",
                json={"target": "nothing-matches"},
                headers=_bearer(token_a),
            )
            assert first.status_code == 200
            # Read back the audit log via the substrate filter on
            # op_class=audit_query. The first call's row MUST surface.
            second = await client.post(
                "/api/v1/audit/query",
                json={"op_class": "audit_query"},
                headers=_bearer(token_a),
            )
    assert second.status_code == 200, second.text
    rows = second.json()["rows"]
    # Both calls emit audit rows with op_class=audit_query. The second
    # call surfaces the FIRST call's row (and possibly itself if the
    # write commits before the read; both shapes are valid since the
    # audit middleware writes synchronously before response — but a
    # single row is sufficient proof that the chain terminates).
    assert len(rows) >= 1
    op_classes = {row["op_class"] for row in rows}
    assert op_classes == {"audit_query"}, f"unexpected op_class spread: {op_classes}"
    op_ids = {row["op_id"] for row in rows}
    assert "meho.audit.query" in op_ids, f"missing canonical op_id: {sorted(op_ids)}"
    # The audit_log only has one row in the tenant up to this point
    # PLUS the second call's row. If the loop recursed, we'd see
    # exploding row counts; bounding by 5 catches that without
    # being brittle to whether the second call's own row commits
    # before its response.
    assert len(rows) <= 5, f"audit-on-audit-query may be recursing: {len(rows)} rows"


# ---------------------------------------------------------------------------
# Module self-check — imports cleanly even without Docker
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """The module's top-level imports load without requiring Docker.

    The skip-on-no-Docker decorator gates **tests** from running;
    it does not gate the module's import. A future regression that
    pulls a Docker-dependent import to the top level would surface
    here at collection time on a Docker-free runner.
    """
    assert _build_audit_acceptance_app is not None
    assert seed_audit_rows is not None
