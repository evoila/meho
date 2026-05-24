# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G8.2-T7 acceptance suite — per-session audit replay end-to-end (#1015).

Closes the replay leg of Goal #219 / Initiative #377: a Postgres-backed
integration gate that proves :func:`~meho_backplane.audit_query.replay_session`
(T3), the REST replay route (T4), the Go CLI wire contract (T5), and the
MCP replay surface (T6) hold together against a real database — not the
SQLite the per-task unit suites run on.

Why real Postgres, not SQLite
=============================

The unit coverage in ``backend/tests/test_audit_replay.py`` (T3) and
``backend/tests/test_mcp_tool_audit_replay.py`` (T6) exercises the
recursive-CTE closure and the tree assembler against SQLite (T3) or a
mocked ``replay_session`` (T4 / T6). The recursive CTE — the first in the
codebase — runs on both engines, but the *acceptance* contract is "does the
whole stack reconstruct a real session on the production database". The
tenant-isolation boundary in particular must be proven against PostgreSQL's
own predicate evaluation, because the unit suite cannot prove the WHERE
clause survives the JIT / planner the production engine uses. This module is
the load-bearing PG gate; it reuses the testcontainer fixtures and JWT mocks
G8.1-T5 (#469) established.

Coverage matrix (the five #1015 integration scenarios + unit edges)
==================================================================

1. **Multi-level session tree.** Seed root → child → grandchild + a
   sibling root (all sharing ``agent_session_id``, linked by
   ``parent_audit_id``). The substrate, the REST route, and the MCP
   ``meho.audit.replay`` tool all reconstruct the same tree: correct
   nesting, each branch ``occurred_at``-ascending, roots chronological.
2. **Tenant isolation.** Tenant-A and tenant-B each own a session under
   the **same** ``agent_session_id`` UUID. Tenant-A's replay (REST + MCP)
   returns only A's rows; B's are unreachable. The boundary is proven to
   *fail loudly* — a direct substrate call with ``tenant_id=B`` returns
   B's distinct row set, so a regression that dropped the tenant predicate
   would surface as cross-tenant leakage here.
3. **Cycle defence.** A self-referential row (``parent_audit_id == id``)
   and a 2-row mutual cycle terminate at ``max_depth`` against the real
   recursive CTE — no infinite recursion, no stack overflow, bounded time.
4. **Session-too-large.** A session with > 10_000 anchor rows returns
   REST 413 ``{"detail": {"detail": "session_too_large", "row_count": n}}``
   and the recursive tree is **not** built for the rejected request (the
   count-first guard runs before ``replay_session``). The Go CLI's 413
   redirect decodes this exact body shape.
5. **Aggregate-only broadcast.** Invoking the REST replay route emits one
   ``BroadcastEvent`` to ``meho:feed:{tenant_id}`` whose payload is exactly
   ``{op_class, result_status, row_count}`` — no ``ReplayNode`` tree, no
   per-node audit content reaches the SSE / Slack feed (decision #3).

End-to-end CLI → REST → substrate
=================================

:func:`test_e2e_cli_wire_contract_replay_envelope_against_seeded_db` drives
the live REST replay route against the seeded PG DB on the exact path the Go
CLI's ``buildReplayPath`` produces, and pins the response JSON to the field
shape the Go ``ReplayResult`` / ``ReplayNode`` structs unmarshal
(``cli/internal/cmd/audit/replay.go``). This mirrors G8.1-T5 scenario 5's
posture: the Go binary keeps its own end-to-end tests (``replay_test.go``),
and this acceptance test closes the Python-side seam so a REST regression
that broke the wire shape surfaces even before the Go CI run catches it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import redis.asyncio as redis_async
import redis.exceptions
import respx
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker as sa_async_sessionmaker

from meho_backplane.api.v1.audit import router as api_v1_audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.audit_query import replay_session
from meho_backplane.auth.operator import TenantRole
from meho_backplane.broadcast import client as broadcast_client_module
from meho_backplane.broadcast.events import BroadcastEvent
from meho_backplane.db.models import AuditLog
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
from tests.fixtures.audit_rows_seed import seed_audit_row

# ``asyncio_mode = "auto"`` (pyproject.toml) auto-collects every async test;
# the module-level mark below is the Docker skip only. Applying ``asyncio``
# here would also tag the sync ``test_module_imports_cleanly`` and emit a
# pytest warning — same stance as test_g81_audit_query_acceptance.
pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)

# ---------------------------------------------------------------------------
# Pinned identities (same shape as test_g81_audit_query_acceptance)
# ---------------------------------------------------------------------------

TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID: str = "22222222-2222-2222-2222-222222222222"

#: The canonical MCP resource URI the audience-binding middleware compares
#: against; pinned to the in-tree fixture value (matches
#: ``tests/integration/test_mcp_inspector.py`` and the G8.1-T5 acceptance).
MCP_RESOURCE_URI: str = "https://meho.test/mcp"

#: The replay row cap mirrored from the REST route's ``_REPLAY_ROW_CAP``
#: and the MCP tool's ``_REPLAY_MAX_ROWS``. The 413 scenario seeds one
#: more than this so the count-first guard fires.
REPLAY_ROW_CAP: int = 10_000

_BASE = datetime(2026, 5, 20, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Valkey fixture (mirrors test_g81_audit_query_acceptance's valkey_url)
# ---------------------------------------------------------------------------


@pytest.fixture
async def valkey_url(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Boot a per-test ``valkey/valkey:8`` container and pin its URL.

    The audit middleware's publish-on-write hook reads
    ``BROADCAST_REDIS_URL`` from Settings; binding it here routes the
    aggregate-only replay broadcast into the testcontainer's Valkey so the
    decision-#3 assertion reads back from the real pipeline rather than a
    mock. Verbatim copy of the G8.1-T5 acceptance fixture, kept local so
    this suite's lifecycle is auditable from one place.
    """
    import os

    from testcontainers.redis import RedisContainer  # type: ignore[import-untyped]

    image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
    with RedisContainer(image) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        monkeypatch.setenv("BROADCAST_REDIS_URL", url)
        await broadcast_client_module.dispose_broadcast_client()
        get_settings.cache_clear()
        try:
            yield url
        finally:
            await broadcast_client_module.dispose_broadcast_client()


# ---------------------------------------------------------------------------
# App + client + auth builders (mirror the G8.1-T5 acceptance helpers)
# ---------------------------------------------------------------------------


def _build_audit_acceptance_app() -> FastAPI:
    """FastAPI with the audit + MCP routers on the production middleware stack.

    ``AuditMiddleware`` inner, ``RequestContextMiddleware`` outer — the
    production ordering. ``eager_import_mcp_modules`` pre-registers every
    MCP tool so ``tools/list`` and ``tools/call`` see ``meho.audit.replay``.
    """
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

    A dedicated short-lived engine + sessionmaker so the seed helpers write
    into the same DB the audit middleware (and the production engine cache)
    reads from. Mirrors the G8.1-T5 acceptance helper.
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
    """XREAD one event from ``meho:feed:{tenant_id}`` (verbatim from G8.1-T5)."""
    client = redis_async.from_url(valkey_url, decode_responses=True)
    stream = f"meho:feed:{tenant_id}"
    try:
        try:
            response = await client.xread(streams={stream: "0-0"}, count=1, block=block_ms)
        except redis.exceptions.TimeoutError:
            return None
        if not response:
            return None
        _stream, entries = response[0]
        _entry_id, fields = entries[0]
        if "event" in fields:
            return BroadcastEvent.model_validate_json(fields["event"])
        return BroadcastEvent.model_validate(fields)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Replay-tree seed shapes
# ---------------------------------------------------------------------------


async def _seed_multi_level_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_session_id: uuid.UUID,
    principal_sub: str = "damir",
) -> dict[str, uuid.UUID]:
    """Seed root → child → grandchild + a later sibling root in one session.

    All four rows share *agent_session_id*; the descendants are linked by
    ``parent_audit_id``. The sibling root has no parent and a later
    timestamp, so it sorts after the first root. Returns the row ids keyed
    by role for the caller's nesting / ordering assertions.
    """
    root = await seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=_BASE,
        principal_sub=principal_sub,
        op_id="vsphere.vm.list",
        op_class="read",
        agent_session_id=agent_session_id,
    )
    child = await seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=_BASE + timedelta(seconds=1),
        principal_sub=principal_sub,
        op_id="vsphere.vm.get",
        op_class="read",
        agent_session_id=agent_session_id,
        parent_audit_id=root,
    )
    grandchild = await seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=_BASE + timedelta(seconds=2),
        principal_sub=principal_sub,
        op_id="vsphere.vm.power.get",
        op_class="read",
        agent_session_id=agent_session_id,
        parent_audit_id=child,
    )
    sibling_root = await seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=_BASE + timedelta(seconds=3),
        principal_sub=principal_sub,
        op_id="vsphere.host.list",
        op_class="read",
        agent_session_id=agent_session_id,
    )
    return {
        "root": root,
        "child": child,
        "grandchild": grandchild,
        "sibling_root": sibling_root,
    }


def _flatten(nodes: list[dict[str, Any]]) -> list[str]:
    """Depth-first list of node ids (strings) across a serialised forest."""
    out: list[str] = []
    for node in nodes:
        out.append(str(node["id"]))
        out.extend(_flatten(node.get("children", [])))
    return out


async def _mcp_call(
    client: httpx.AsyncClient,
    token: str,
    *,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    """Issue one JSON-RPC ``method`` against ``/mcp`` (verbatim from G8.1-T5)."""
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


def _mcp_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the MCP audience env the ``/mcp`` transport requires.

    Without ``BACKPLANE_URL`` / ``MCP_RESOURCE_URI`` every ``/mcp`` request
    401s with ``audience_not_configured``. Same pin the G8.1-T5 MCP
    scenarios use.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("MCP_RESOURCE_URI", MCP_RESOURCE_URI)
    get_settings.cache_clear()


# ===========================================================================
# Scenario 1 — Multi-level session tree (substrate + REST + MCP)
# ===========================================================================


async def test_scenario_1_substrate_reconstructs_multi_level_tree(
    pg_engine: None,
    async_pg_url: str,
) -> None:
    """The substrate reconstructs root → child → grandchild + sibling root on PG."""
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=agent_session_id,
        )
        await session.commit()

    async with await _session_for_url(async_pg_url) as session:
        roots = await replay_session(
            agent_session_id,
            tenant_id=uuid.UUID(TENANT_A_ID),
            session=session,
        )

    # Roots chronological: the original root sorts before the later sibling.
    assert [n.id for n in roots] == [ids["root"], ids["sibling_root"]]
    assert roots[0].depth == 0
    # root → child → grandchild nesting, each at the expected depth.
    assert [n.id for n in roots[0].children] == [ids["child"]]
    assert roots[0].children[0].depth == 1
    assert [n.id for n in roots[0].children[0].children] == [ids["grandchild"]]
    assert roots[0].children[0].children[0].depth == 2
    # The sibling root is a leaf.
    assert roots[1].children == []


async def test_scenario_1_rest_replay_returns_multi_level_tree(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """The REST replay route returns the same multi-level tree as the substrate."""
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=agent_session_id,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{agent_session_id}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"root", "session_id", "tenant_id", "row_count"}
    assert body["session_id"] == str(agent_session_id)
    assert body["tenant_id"] == TENANT_A_ID
    # row_count is the session's anchor-row count (all four share the session).
    assert body["row_count"] == 4
    root_ids = [str(n["id"]) for n in body["root"]]
    assert root_ids == [str(ids["root"]), str(ids["sibling_root"])]
    first = body["root"][0]
    assert [str(c["id"]) for c in first["children"]] == [str(ids["child"])]
    assert [str(c["id"]) for c in first["children"][0]["children"]] == [str(ids["grandchild"])]


async def test_scenario_1_mcp_replay_returns_multi_level_tree(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``meho.audit.replay`` admin tool returns the same tree as REST."""
    _mcp_admin_env(monkeypatch)
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=agent_session_id,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    # The replay MCP tool is tenant_admin-gated.
    token_admin = _mint_operator(
        keypair=key,
        tenant_id=TENANT_A_ID,
        role=TenantRole.TENANT_ADMIN,
        audience=MCP_RESOURCE_URI,
    )

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            envelope = await _mcp_call(
                client,
                token_admin,
                method="tools/call",
                params={
                    "name": "meho.audit.replay",
                    "arguments": {"session_id": str(agent_session_id)},
                },
            )
    result = envelope["result"]
    structured = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert structured["session_id"] == str(agent_session_id)
    assert structured["tenant_id"] == TENANT_A_ID
    # The MCP envelope's row_count is the assembled node count (all 4 nodes).
    assert structured["row_count"] == 4
    root_ids = [str(n["id"]) for n in structured["root"]]
    assert root_ids == [str(ids["root"]), str(ids["sibling_root"])]
    first = structured["root"][0]
    assert [str(c["id"]) for c in first["children"]] == [str(ids["child"])]


# ===========================================================================
# Scenario 2 — Tenant isolation (same agent_session_id on both tenants)
# ===========================================================================


async def test_scenario_2_rest_tenant_isolation_same_session_id(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Tenant-A REST replay returns only A's rows; B shares the same session UUID.

    The session id is identical on both tenants. Tenant-A's replay must see
    exactly A's tree and never B's row — even though the anchor predicate
    matches B's rows too, the tenant clause splits them.
    """
    shared_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        a_ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=shared_session_id,
            principal_sub="alice",
        )
        b_root = await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_B_ID),
            occurred_at=_BASE,
            principal_sub="bob",
            op_id="k8s.pod.list",
            op_class="read",
            agent_session_id=shared_session_id,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{shared_session_id}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    all_ids = set(_flatten(body["root"]))
    assert str(a_ids["root"]) in all_ids, "tenant-A root missing from A's replay"
    assert str(b_root) not in all_ids, "tenant-B row leaked into tenant-A replay"
    # row_count counts A's anchors (4) only — B's anchor under the same
    # session id must not inflate it.
    assert body["row_count"] == 4


async def test_scenario_2_mcp_tenant_isolation_same_session_id(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant-A MCP replay returns only A's rows for a session id B also uses."""
    _mcp_admin_env(monkeypatch)
    shared_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        a_ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=shared_session_id,
            principal_sub="alice",
        )
        b_root = await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_B_ID),
            occurred_at=_BASE,
            principal_sub="bob",
            op_id="k8s.pod.list",
            op_class="read",
            agent_session_id=shared_session_id,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_admin = _mint_operator(
        keypair=key,
        tenant_id=TENANT_A_ID,
        role=TenantRole.TENANT_ADMIN,
        audience=MCP_RESOURCE_URI,
    )

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            envelope = await _mcp_call(
                client,
                token_admin,
                method="tools/call",
                params={
                    "name": "meho.audit.replay",
                    "arguments": {"session_id": str(shared_session_id)},
                },
            )
    result = envelope["result"]
    structured = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    all_ids = set(_flatten(structured["root"]))
    assert str(a_ids["root"]) in all_ids
    assert str(b_root) not in all_ids, "tenant-B row leaked into tenant-A MCP replay"


async def test_scenario_2_isolation_fails_loudly_when_tenant_swapped(
    pg_engine: None,
    async_pg_url: str,
) -> None:
    """The tenant boundary actually guards: B's substrate replay returns B's rows.

    Issue AC: "tenant-isolation scenario fails loudly if the tenant WHERE
    clause is removed." Proven positively at the substrate: the SAME session
    id replayed under ``tenant_id=B`` returns B's distinct row set, not A's.
    A regression that dropped the tenant predicate would make both calls
    return the merged set, breaking the disjointness assertion below.
    """
    shared_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        a_ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=shared_session_id,
            principal_sub="alice",
        )
        b_root = await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_B_ID),
            occurred_at=_BASE,
            principal_sub="bob",
            op_id="k8s.pod.list",
            op_class="read",
            agent_session_id=shared_session_id,
        )
        await session.commit()

    async with await _session_for_url(async_pg_url) as session:
        a_roots = await replay_session(
            shared_session_id,
            tenant_id=uuid.UUID(TENANT_A_ID),
            session=session,
        )
        b_roots = await replay_session(
            shared_session_id,
            tenant_id=uuid.UUID(TENANT_B_ID),
            session=session,
        )

    a_seen = {str(n.id) for n in a_roots} | {
        cid for n in a_roots for cid in _flatten([n.model_dump(mode="json")])
    }
    b_seen = {str(n.id) for n in b_roots}
    assert str(a_ids["root"]) in a_seen
    assert b_seen == {str(b_root)}, "tenant-B replay returned the wrong row set"
    # Disjoint: the two tenants' replays share no row. A dropped WHERE clause
    # would merge them and break this.
    assert a_seen.isdisjoint(b_seen), "tenant boundary leaked between A and B"


# ===========================================================================
# Scenario 3 — Cycle defence (self-ref + 2-row mutual cycle) on real PG
# ===========================================================================


async def test_scenario_3_self_referential_row_terminates(
    pg_engine: None,
    async_pg_url: str,
) -> None:
    """A ``parent_audit_id == id`` row terminates against the real recursive CTE."""
    agent_session_id = uuid.uuid4()
    row_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            occurred_at=_BASE,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
            agent_session_id=agent_session_id,
            parent_audit_id=row_id,
            audit_id=row_id,
        )
        await session.commit()

    async with await _session_for_url(async_pg_url) as session:
        roots = await replay_session(
            agent_session_id,
            tenant_id=uuid.UUID(TENANT_A_ID),
            session=session,
            max_depth=20,
        )
    # The self-loop is treated as a root; no infinite recursion, no children.
    assert [n.id for n in roots] == [row_id]
    assert roots[0].children == []


async def test_scenario_3_mutual_cycle_terminates_at_max_depth(
    pg_engine: None,
    async_pg_url: str,
) -> None:
    """An A↔B mutual cycle terminates against PG; both rows surface exactly once.

    The depth-bounded CTE keeps the SQL closure from looping forever; tree
    assembly's path-set drops the back-edge. The contract: bounded time +
    every fetched row present once.
    """
    agent_session_id = uuid.uuid4()
    a_id = uuid.uuid4()
    b_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            occurred_at=_BASE,
            principal_sub="damir",
            op_id="vsphere.vm.list",
            op_class="read",
            agent_session_id=agent_session_id,
            parent_audit_id=b_id,
            audit_id=a_id,
        )
        await seed_audit_row(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            occurred_at=_BASE + timedelta(seconds=1),
            principal_sub="damir",
            op_id="vsphere.vm.get",
            op_class="read",
            agent_session_id=agent_session_id,
            parent_audit_id=a_id,
            audit_id=b_id,
        )
        await session.commit()

    async with await _session_for_url(async_pg_url) as session:
        roots = await replay_session(
            agent_session_id,
            tenant_id=uuid.UUID(TENANT_A_ID),
            session=session,
            max_depth=20,
        )

    seen: list[str] = []

    def _collect(node: Any) -> None:
        seen.append(str(node.id))
        for child in node.children:
            _collect(child)

    for root in roots:
        _collect(root)
    # Each row appears exactly once — no infinite expansion — and both present.
    assert sorted(seen) == sorted([str(a_id), str(b_id)])
    assert len(seen) == 2


async def test_scenario_3_deep_chain_truncates_at_max_depth(
    pg_engine: None,
    async_pg_url: str,
) -> None:
    """A chain deeper than ``max_depth`` truncates against the real CTE bound.

    The recursive arm's ``depth < max_depth`` predicate is what guarantees
    termination on PG; this proves the cap holds on a deliberately deep
    chain — the capped node keeps its row but has no children.
    """
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        parent: uuid.UUID | None = None
        for i in range(8):
            parent = await seed_audit_row(
                session,
                tenant_id=uuid.UUID(TENANT_A_ID),
                occurred_at=_BASE + timedelta(seconds=i),
                principal_sub="damir",
                op_id="vsphere.vm.list",
                op_class="read",
                agent_session_id=agent_session_id,
                parent_audit_id=parent,
            )
        await session.commit()

    async with await _session_for_url(async_pg_url) as session:
        roots = await replay_session(
            agent_session_id,
            tenant_id=uuid.UUID(TENANT_A_ID),
            session=session,
            max_depth=3,
        )

    node = roots[0]
    depths = [node.depth]
    while node.children:
        node = node.children[0]
        depths.append(node.depth)
    assert depths == [0, 1, 2, 3]
    assert node.children == []  # depth-3 node truncated despite deeper rows


# ===========================================================================
# Scenario 4 — Session-too-large → 413, tree NOT built
# ===========================================================================


async def _bulk_seed_anchor_rows(
    async_url: str,
    *,
    tenant_id: uuid.UUID,
    agent_session_id: uuid.UUID,
    count: int,
) -> None:
    """Insert *count* flat anchor rows via a single Core executemany.

    The 413 guard counts only ``agent_session_id``-anchored rows, so the
    rows need no lineage — a fast bulk insert keeps the >10k seed under a
    couple of seconds rather than 10k ORM round-trips.
    """
    engine = create_async_engine(async_url)
    base = datetime.now(UTC) - timedelta(seconds=count)
    rows = [
        {
            "id": uuid.uuid4(),
            "occurred_at": base + timedelta(milliseconds=i),
            "operator_sub": "damir",
            "method": "POST",
            "path": "/mcp",
            "status_code": 200,
            "request_id": None,
            "duration_ms": Decimal("1.0"),
            "payload": {"op_id": "vsphere.vm.list", "op_class": "read"},
            "tenant_id": tenant_id,
            "target_id": None,
            "agent_session_id": agent_session_id,
            "parent_audit_id": None,
        }
        for i in range(count)
    ]
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.insert(AuditLog), rows)
    finally:
        await engine.dispose()


async def test_scenario_4_over_cap_session_returns_413_without_building_tree(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """A > 10k-anchor session → REST 413 ``session_too_large``; tree never built.

    The count-first guard is proven against the real DB: 10_001 anchor rows
    are seeded and the route must 413 with the exact body shape the Go CLI's
    413 redirect decodes. The body's ``row_count`` echoes the real
    cardinality.
    """
    over_cap_session = uuid.uuid4()
    await _bulk_seed_anchor_rows(
        async_pg_url,
        tenant_id=uuid.UUID(TENANT_A_ID),
        agent_session_id=over_cap_session,
        count=REPLAY_ROW_CAP + 1,
    )

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{over_cap_session}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 413, resp.text
    # FastAPI wraps the route's HTTPException(detail={...}) under a top-level
    # `detail` key — the exact two-level shape the Go CLI's
    # `sessionTooLargeDetail` struct unmarshals.
    detail = resp.json()["detail"]
    assert detail == {"detail": "session_too_large", "row_count": REPLAY_ROW_CAP + 1}


async def test_scenario_4_at_cap_boundary_returns_200(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Exactly ``_REPLAY_ROW_CAP`` anchor rows is at the boundary → 200, not 413.

    Proves the guard is ``count > cap`` (strict), not ``>=``. Seeds exactly
    the cap so the tree IS built and the route returns the envelope.
    """
    at_cap_session = uuid.uuid4()
    await _bulk_seed_anchor_rows(
        async_pg_url,
        tenant_id=uuid.UUID(TENANT_A_ID),
        agent_session_id=at_cap_session,
        count=REPLAY_ROW_CAP,
    )

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{at_cap_session}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == REPLAY_ROW_CAP


# ===========================================================================
# Scenario 5 — Aggregate-only broadcast (no ReplayNode reaches the feed)
# ===========================================================================


async def test_scenario_5_replay_broadcast_is_aggregate_only(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """The REST replay route's broadcast carries no ReplayNode/tree payload.

    Decision #3: invoking ``meho.audit.replay`` emits one BroadcastEvent
    whose payload is exactly ``{op_class, result_status, row_count}`` — never
    the replayed tree or any per-node audit content. Reads back from the
    real Valkey stream the middleware published to.
    """
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=agent_session_id,
        )
        await session.commit()

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{agent_session_id}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 200, resp.text

    event = await _read_broadcast_event_for_tenant(valkey_url, TENANT_A_ID)
    assert event is not None, "no BroadcastEvent landed in Valkey within the 30s window"
    # Distinct replay op_id, aggregate-only class.
    assert event.op_id == "meho.audit.replay"
    assert event.op_class == "audit_query"
    # Exact key set — no `root`, no `children`, no per-node audit content.
    assert set(event.payload.keys()) == {"op_class", "result_status", "row_count"}, (
        f"replay broadcast leaked extra keys: {sorted(event.payload.keys())}"
    )
    assert event.payload["row_count"] == 4
    # Defensive: no ReplayNode field names appear anywhere in the payload.
    serialised = json.dumps(event.payload)
    for leaked in ("root", "children", "depth", "ReplayNode", "vsphere.vm"):
        assert leaked not in serialised, f"replay broadcast leaked '{leaked}'"


async def test_scenario_5_over_cap_rejection_broadcast_is_aggregate_only(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """Even the 413 rejection emits an aggregate-only broadcast with the count.

    The route binds ``audit_row_count`` before raising 413, so the
    audit-on-replay broadcast reports the rejected cardinality — still
    aggregate-only, never the (un-built) tree.
    """
    over_cap_session = uuid.uuid4()
    await _bulk_seed_anchor_rows(
        async_pg_url,
        tenant_id=uuid.UUID(TENANT_A_ID),
        agent_session_id=over_cap_session,
        count=REPLAY_ROW_CAP + 1,
    )

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(
                f"/api/v1/audit/sessions/{over_cap_session}/replay",
                headers=_bearer(token_a),
            )
    assert resp.status_code == 413, resp.text

    event = await _read_broadcast_event_for_tenant(valkey_url, TENANT_A_ID)
    assert event is not None, "no BroadcastEvent landed for the 413-rejected replay"
    assert event.op_id == "meho.audit.replay"
    assert event.op_class == "audit_query"
    assert set(event.payload.keys()) == {"op_class", "result_status", "row_count"}
    assert event.payload["row_count"] == REPLAY_ROW_CAP + 1
    # 413 is a client-error status → result_status is not "ok".
    assert event.payload["result_status"] != "ok"


# ===========================================================================
# End-to-end CLI → REST → substrate (Go-wire-shape seam, G8.1-T5 posture)
# ===========================================================================


async def test_e2e_cli_wire_contract_replay_envelope_against_seeded_db(
    pg_engine: None,
    valkey_url: str,
    async_pg_url: str,
) -> None:
    """`meho audit replay <id>` → live REST route → substrate against seeded PG.

    Drives the exact path the Go CLI's ``buildReplayPath`` produces
    (``/api/v1/audit/sessions/{id}/replay``) against the seeded DB and pins
    the response JSON to the field shape the Go ``ReplayResult`` /
    ``ReplayNode`` structs unmarshal (``cli/internal/cmd/audit/replay.go``):
    the envelope is ``{root, session_id, tenant_id, row_count}`` and each
    node carries ``ts`` / ``op_id`` / ``result_status`` / ``duration_ms`` /
    ``depth`` / ``children``. The Go binary keeps its own end-to-end tests;
    this closes the Python-side seam so a REST regression that broke the
    wire shape surfaces before the Go CI run (mirrors G8.1-T5 scenario 5).
    """
    agent_session_id = uuid.uuid4()
    async with await _session_for_url(async_pg_url) as session:
        ids = await _seed_multi_level_session(
            session,
            tenant_id=uuid.UUID(TENANT_A_ID),
            agent_session_id=agent_session_id,
        )
        await session.commit()

    # The exact path the Go CLI assembles (buildReplayPath in replay.go).
    cli_path = f"/api/v1/audit/sessions/{agent_session_id}/replay"

    app = _build_audit_acceptance_app()
    key = make_rsa_keypair("kid-A")
    token_a = _mint_operator(keypair=key, tenant_id=TENANT_A_ID)

    async with _make_async_client(app) as client:
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            resp = await client.get(cli_path, headers=_bearer(token_a))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Envelope shape the Go ReplayResult struct decodes.
    assert set(body.keys()) == {"root", "session_id", "tenant_id", "row_count"}
    assert isinstance(body["root"], list)
    assert isinstance(body["row_count"], int)

    # Every key the Go ReplayNode struct reads must be present on each node
    # (the verb's --json path re-marshals the raw bytes verbatim, but the
    # human ASCII tree reads these named fields).
    def _assert_node_shape(node: dict[str, Any]) -> None:
        for required in ("ts", "op_id", "result_status", "depth", "children"):
            assert required in node, f"replay node missing CLI field '{required}'"
        # duration_ms is *string|null on the Go side — present as a key.
        assert "duration_ms" in node
        assert isinstance(node["children"], list)
        for child in node["children"]:
            _assert_node_shape(child)

    for node in body["root"]:
        _assert_node_shape(node)

    # The tree the CLI would render: root → child → grandchild, sibling root.
    assert [str(n["id"]) for n in body["root"]] == [
        str(ids["root"]),
        str(ids["sibling_root"]),
    ]


# ===========================================================================
# Unit edges (in-memory; the acceptance gate's fast contract cross-check)
# ===========================================================================
#
# These mirror the per-task SQLite unit coverage in
# ``backend/tests/test_audit_replay.py`` (T3) and the MCP self-session checks
# in ``backend/tests/test_mcp_tool_audit_replay.py`` (T6). They are included
# here so the acceptance module is self-contained for the four edges the
# #1015 issue body names as the in-memory unit layer of this gate; they run
# on the autouse SQLite ``_default_database_url`` fixture in tests/conftest
# (no Docker), so they execute even on a Docker-free runner.


@pytest.fixture
async def sqlite_session() -> AsyncIterator[AsyncSession]:
    """One SQLite-backed :class:`AsyncSession` for the in-memory unit edges.

    The unit edges do NOT request ``pg_engine``, so ``DATABASE_URL`` stays at
    the SQLite default the top-level conftest's autouse
    ``_default_database_url`` fixture provisions (migrated to head, per
    test). The chassis Settings env these calls need is pinned by the
    acceptance conftest's autouse ``_acceptance_default_env`` (the full
    ``_CHASSIS_ENV``), so no per-module env fixture is required here.
    """
    from meho_backplane.db.engine import get_sessionmaker

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _seed_sqlite_anchor(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    second: int,
    agent_session_id: uuid.UUID | None,
    parent_audit_id: uuid.UUID | None = None,
    row_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one minimal SQLite audit row for the in-memory unit edges."""
    return await seed_audit_row(
        s,
        tenant_id=tenant_id,
        occurred_at=_BASE + timedelta(seconds=second),
        principal_sub="op",
        op_id="vsphere.vm.list",
        op_class="read",
        agent_session_id=agent_session_id,
        parent_audit_id=parent_audit_id,
        audit_id=row_id,
    )


@pytest.mark.asyncio
async def test_unit_edge_null_session_rows_unreachable(sqlite_session: AsyncSession) -> None:
    """NULL ``agent_session_id`` rows (chassis HTTP era) are unreachable by id."""
    tenant_id = uuid.uuid4()
    await _seed_sqlite_anchor(sqlite_session, tenant_id=tenant_id, second=0, agent_session_id=None)
    await sqlite_session.commit()
    roots = await replay_session(uuid.uuid4(), tenant_id=tenant_id, session=sqlite_session)
    assert roots == []


@pytest.mark.asyncio
async def test_unit_edge_flat_session_renders_flat_roots(sqlite_session: AsyncSession) -> None:
    """A flat session (no ``parent_audit_id`` links) → a flat list of roots."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    ids = [
        await _seed_sqlite_anchor(
            sqlite_session, tenant_id=tenant_id, second=i, agent_session_id=sess
        )
        for i in range(4)
    ]
    await sqlite_session.commit()
    roots = await replay_session(sess, tenant_id=tenant_id, session=sqlite_session)
    assert [n.id for n in roots] == ids
    assert all(r.depth == 0 and r.children == [] for r in roots)


@pytest.mark.asyncio
async def test_unit_edge_depth_cap_truncates_deep_chain(sqlite_session: AsyncSession) -> None:
    """The depth cap truncates a deliberately deep chain (capped node has no kids)."""
    tenant_id = uuid.uuid4()
    sess = uuid.uuid4()
    parent: uuid.UUID | None = None
    for i in range(6):
        parent = await _seed_sqlite_anchor(
            sqlite_session,
            tenant_id=tenant_id,
            second=i,
            agent_session_id=sess,
            parent_audit_id=parent,
        )
    await sqlite_session.commit()
    roots = await replay_session(sess, tenant_id=tenant_id, session=sqlite_session, max_depth=2)
    node = roots[0]
    depths = [node.depth]
    while node.children:
        node = node.children[0]
        depths.append(node.depth)
    assert depths == [0, 1, 2]
    assert node.children == []


def test_unit_edge_query_audit_tree_foreign_session_returns_invalid_params() -> None:
    """``query_audit(shape="tree")`` self-session-only: a foreign id → -32602.

    The ``shape="tree"`` path on the operator-role ``query_audit`` tool
    replays ONLY the caller's own bound MCP session, enforced by
    :func:`~meho_backplane.mcp.tools.audit._resolve_self_session_id`. A
    request naming a different ``agent_session_id`` than the caller's bound
    MCP session id is rejected with ``McpInvalidParamsError`` (JSON-RPC
    -32602) — even though the flat path would return other in-tenant
    principals' rows. Exercised directly (no DB / Operator) since the
    self-session gate is a pure contextvar + arguments check.
    """
    import structlog

    from meho_backplane.mcp.server import McpInvalidParamsError
    from meho_backplane.mcp.tools.audit import _resolve_self_session_id

    own_session = uuid.uuid4()
    foreign_session = uuid.uuid4()
    # Bind the caller's own MCP session id the way the transport would.
    with (
        structlog.contextvars.bound_contextvars(mcp_session_id=str(own_session)),
        pytest.raises(McpInvalidParamsError, match="own session"),
    ):
        _resolve_self_session_id({"agent_session_id": str(foreign_session)})


# ---------------------------------------------------------------------------
# Module self-check — imports cleanly without Docker
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """The module's top-level imports load without requiring Docker.

    The skip-on-no-Docker decorator gates the PG **tests** from running; it
    does not gate the module import. A future regression pulling a
    Docker-dependent import to the top level surfaces here at collection time
    on a Docker-free runner.
    """
    assert _build_audit_acceptance_app is not None
    assert replay_session is not None
