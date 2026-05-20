# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end acceptance for the G6.3 PII opt-in/opt-out surfaces (#383).

This module is the **meta-test** closing Initiative #376. T1-T5 have
unit + behavioural coverage of their own slices; T6 (this file)
exercises the two override surfaces end-to-end through the production
``meho_backplane.main:app`` middleware stack against a real
:class:`PostgresContainer` (via the package-level ``pg_engine``
fixture) and a real :class:`RedisContainer` (Valkey 8-compatible,
booted module-scoped here so the seven scenarios amortise the
~3-second start cost).

Scenarios (one ``async def`` per Initiative-DoD AC):

1. ``test_per_call_opt_in_upgrades_audit_query_to_full`` -- operator
   POSTs ``/api/v1/audit/query`` with ``X-Broadcast-Detail: full``;
   the audit row's ``broadcast_detail_origin = "request_override"``
   and the Valkey stream entry's payload carries the full filter
   shape (the upgrade path from decision #3's aggregate-only-by-
   default for ``audit_query``).
2. ``test_tenant_rule_downgrades_read_op_to_aggregate`` -- tenant
   admin creates a rule downgrading ``meho.broadcast.overrides.list``
   to aggregate; the next GET broadcasts an aggregate payload tagged
   ``broadcast_detail_origin = "tenant_rule:<uuid>"``.
3. ``test_scoped_rule_scope_miss_falls_through_to_default`` -- the
   rule from #2 with a non-matching ``scope_field=namespace,
   scope_value=kube-system`` no longer applies because the route
   never binds ``namespace`` into the audit payload; origin reverts
   to ``"default"``.
4. ``test_delete_rule_invalidates_resolver_cache`` -- after the
   downgrade rule is DELETEd, the next GET returns to the default
   full-detail broadcast; verifies the cache invalidation hook T4
   wires runs end-to-end.
5. ``test_origin_tagging_records_all_three_branches`` -- a single
   suite of three requests (header opt-in / scoped rule / default)
   each lands the matching ``broadcast_detail_origin`` /
   ``broadcast_detail_effective`` pair on the audit row.
6. ``test_rbac_blocks_non_admin_on_rest_and_mcp`` -- an operator-
   role JWT (not tenant_admin) hitting ``POST
   /api/v1/broadcast/overrides`` returns 403, and ``tools/call
   meho.broadcast.overrides.set`` over the MCP transport returns
   JSON-RPC ``-32602`` with a ``forbidden`` detail token.
7. ``test_tenant_isolation_rule_does_not_leak`` -- tenant A's
   downgrade rule on ``meho.broadcast.overrides.list`` does not
   affect tenant B's identical GET; B still gets the default full
   detail.

Why ``main:app`` and not ``build_integration_app``
==================================================

The package conftest's ``build_integration_app`` mounts only health,
rbac-test, retrieve, MCP, and well-known routes -- it deliberately
omits the audit + broadcast-overrides routers and (crucially) the
:class:`BroadcastDetailMiddleware` that parses ``X-Broadcast-Detail``.
T6 needs all three, so this module imports ``meho_backplane.main.app``
directly. The ``pg_engine`` fixture's engine swap is read by the
production app's session factory lazily on the first request, so the
test-installed engine is the one that handles every audit insert and
every override CRUD call.

Why ``httpx.AsyncClient`` and not ``TestClient``
================================================

``fastapi.testclient.TestClient`` spawns a fresh event loop per
request via :mod:`anyio`, which crosses loop boundaries with the
``pg_engine`` fixture's asyncpg pool and trips SQLAlchemy's
``RuntimeError: attached to a different loop`` on teardown. Every
PG-driven integration test in this suite uses
:class:`httpx.AsyncClient` + :class:`ASGITransport` so the request
and the asyncpg pool stay on the same pytest-asyncio-managed loop --
exactly the pattern :mod:`tests.integration.test_tenant_isolation`
established.

Real Valkey vs. mocked publish_event
====================================

T1-T5 mock :func:`~meho_backplane.broadcast.publisher.publish_event`
via :func:`monkeypatch.setattr` because their behavioural shape is
"resolver said X, the event we'd have published reflects X". T6 is
the cross-surface acceptance check; it XADDs into a real Valkey
stream and reads back via ``XRANGE`` so the wire shape that an SSE
or MCP-resource subscriber actually receives is what's asserted.
The container costs ~3 s to boot once per module; with seven scenarios
the amortised overhead is < 0.5 s/test.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import redis.asyncio as redis_async
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.broadcast.client import (
    dispose_broadcast_client,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.overrides import reset_overrides_cache_for_testing
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._vault_fakes import install_fake_vault

# Pinned tenants -- match the integration conftest's ``pg_engine``
# re-seed so every scenario can rely on their existence without
# re-inserting.
_TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B: UUID = UUID("22222222-2222-2222-2222-222222222222")

# Pattern targeting the ``GET /api/v1/broadcast/overrides`` route's
# bound op_id (``_OP_ID_LIST`` in
# :mod:`~meho_backplane.api.v1.broadcast_overrides`). Used by the
# downgrade scenarios because it is a sensitivity-class=``read`` op
# in the production wiring -- the default detail is full, so a rule
# saying ``detail=aggregate`` produces an observable downgrade in
# the broadcast event.
_LIST_OP_PATTERN: str = "meho.broadcast.overrides.list"


def _docker_socket_present() -> bool:
    """Heuristic: Docker usable when the unix socket is present.

    Mirrors :func:`tests.integration.conftest._docker_socket_present`
    so the skip condition stays uniform across all integration
    suites that boot a testcontainer.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE: bool = _docker_socket_present()
_DOCKER_SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


# ---------------------------------------------------------------------------
# Module-scoped Valkey -- one container, seven tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def valkey_url() -> Iterator[str]:
    """Boot a Valkey 8 container, yield the ``redis://host:port`` URL.

    Module scope amortises the ~3-second container start across every
    test in this file. ``MEHO_TEST_VALKEY_IMAGE`` honours the same
    registry-mirror override knob used by
    :mod:`tests.integration.test_broadcast_load` so a Docker Hub
    rate-limit on the public image can be sidestepped by CI.
    """
    if not _DOCKER_AVAILABLE:
        pytest.skip(_DOCKER_SKIP_REASON)
    from testcontainers.redis import RedisContainer

    image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
    with RedisContainer(image) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}"


@pytest.fixture
async def broadcast_runtime(
    valkey_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[str]:
    """Pin ``BROADCAST_REDIS_URL``, reset the per-process client cache.

    The production app reads the URL once per worker on the first
    :func:`get_broadcast_client` call; tests must clear the cache so
    the testcontainer's URL replaces whatever the autouse env fixture
    pinned. Yielding the URL lets the test issue ``XRANGE`` directly
    against the same stream the app's ``publish_event`` XADD-ed into.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", valkey_url)
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    try:
        yield valkey_url
    finally:
        await dispose_broadcast_client()
        reset_broadcast_client_for_testing()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Per-test resolver-cache reset + JWKS cache reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_resolver_cache() -> Iterator[None]:
    """Wipe T2's per-tenant override cache so rules don't bleed."""
    reset_overrides_cache_for_testing()
    yield
    reset_overrides_cache_for_testing()


@pytest.fixture(autouse=True)
def _jwks_cache_reset() -> Iterator[None]:
    """Per-test JWKS cache wipe; each scenario mints its own keypair."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the in-process Vault fake for code paths that read secrets."""
    install_fake_vault(monkeypatch)


@pytest.fixture
def production_app(
    pg_engine: None,
    broadcast_runtime: str,
    fake_vault: None,
) -> FastAPI:
    """Hand back the production ``main:app`` after fixtures wire the env.

    ``pg_engine`` truncates the DB and re-seeds the two pinned tenants;
    ``broadcast_runtime`` pins the Valkey URL and clears the client
    cache; ``fake_vault`` short-circuits any transitive Vault read.
    The app's middleware stack + routers are the production wiring --
    Phase 7 verification runs against the exact same shape an operator
    would hit in production.
    """
    return app


def _make_async_client(target_app: FastAPI) -> httpx.AsyncClient:
    """Construct an :class:`httpx.AsyncClient` driving *target_app* in-process.

    ASGITransport runs the FastAPI app inline on the same event loop
    the test awaits on -- no real socket, no separate thread -- which
    keeps the ``pg_engine``-bound asyncpg pool and the request handler
    on the same loop. Matches the pattern
    :mod:`tests.integration.test_tenant_isolation` established.
    """
    return httpx.AsyncClient(
        transport=ASGITransport(app=target_app),
        base_url="http://test",
    )


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    """Mint a JWT carrying *role* + *tenant_id* via the shared helpers."""
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Valkey + audit read-back helpers
# ---------------------------------------------------------------------------


async def _drain_stream(url: str, tenant_id: UUID) -> list[dict[str, Any]]:
    """Read every entry in ``meho:feed:<tenant>`` and return the parsed events.

    A short-lived client per call rather than reusing
    :func:`~meho_backplane.broadcast.client.get_broadcast_client`'s
    singleton so each scenario's assertion sees a fresh connection
    independent of whatever pool state the production app left
    behind. ``XRANGE`` reads the full stream in order; entries are
    capped by ``MAXLEN ~ 10000`` on the publish side so reading the
    whole stream is fast even after many scenarios share the same
    tenant.
    """
    client = redis_async.from_url(url, decode_responses=True)
    try:
        entries = await client.xrange(f"meho:feed:{tenant_id}", min="-", max="+")
    finally:
        await client.aclose()
    parsed: list[dict[str, Any]] = []
    for _entry_id, fields in entries:
        blob = fields.get("event")
        if blob is None:
            continue
        parsed.append(json.loads(blob))
    return parsed


async def _audit_rows_for_tenant(tenant_id: UUID) -> list[AuditLog]:
    """Read audit rows for *tenant_id*, ordered by ``occurred_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


def _audit_query_body() -> dict[str, Any]:
    """Smallest body the ``POST /api/v1/audit/query`` route accepts."""
    return {"since": "24h", "limit": 10}


async def _create_override(
    client: httpx.AsyncClient,
    token: str,
    *,
    op_id_pattern: str,
    detail: str,
    scope_field: str | None = None,
    scope_value: str | None = None,
) -> str:
    """POST a rule, return its id. Fails the test on a non-201 response."""
    body: dict[str, Any] = {"op_id_pattern": op_id_pattern, "detail": detail}
    if scope_field is not None:
        body["scope_field"] = scope_field
        body["scope_value"] = scope_value
    resp = await client.post("/api/v1/broadcast/overrides", json=body, headers=_bearer(token))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _delete_override(client: httpx.AsyncClient, token: str, override_id: str) -> None:
    resp = await client.delete(f"/api/v1/broadcast/overrides/{override_id}", headers=_bearer(token))
    assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# Scenario 1 -- per-call opt-in upgrades audit_query to full
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_per_call_opt_in_upgrades_audit_query_to_full(
    production_app: FastAPI, broadcast_runtime: str
) -> None:
    """``X-Broadcast-Detail: full`` flips an audit_query event to full detail.

    The audit-query op classifies as ``audit_query`` (sensitive,
    decision #3 default aggregate). The header drives
    :class:`~meho_backplane.middleware.BroadcastDetailMiddleware` to
    bind the contextvar; the audit middleware's resolver sees
    ``request_override="full"`` and upgrades. The Valkey stream
    entry must include the ``params`` key (full-detail shape from
    :func:`~meho_backplane.broadcast.events.redact_payload`).
    """
    key = make_rsa_keypair("kid-opt-in")
    token = _token(key, role=TenantRole.OPERATOR)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = await client.post(
            "/api/v1/audit/query",
            json=_audit_query_body(),
            headers={**_bearer(token), "X-Broadcast-Detail": "full"},
        )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows_for_tenant(_TENANT_A)
    audit_query_rows = [row for row in rows if row.path == "/api/v1/audit/query"]
    assert len(audit_query_rows) == 1
    audit_row = audit_query_rows[0]
    assert audit_row.payload["broadcast_detail_origin"] == "request_override"
    assert audit_row.payload["broadcast_detail_effective"] == "full"

    events = await _drain_stream(broadcast_runtime, _TENANT_A)
    audit_events = [e for e in events if e["op_id"] == "meho.audit.query"]
    assert len(audit_events) == 1
    payload = audit_events[0]["payload"]
    # Full-detail shape from ``redact_payload``: includes ``params``.
    assert "params" in payload, payload


# ---------------------------------------------------------------------------
# Scenario 2 -- tenant rule downgrades a read op to aggregate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_tenant_rule_downgrades_read_op_to_aggregate(
    production_app: FastAPI, broadcast_runtime: str
) -> None:
    """An op-wide downgrade rule changes the broadcast event's detail.

    Setup: tenant-admin creates a rule on
    ``meho.broadcast.overrides.list`` with ``detail=aggregate``.
    Action: hit ``GET /api/v1/broadcast/overrides`` (which binds
    ``op_id`` to the same pattern + ``op_class=read``).
    Assert: the resolver picks the tenant rule; the audit row's
    ``broadcast_detail_origin`` becomes ``"tenant_rule:<rule_id>"``
    and the published event payload is the aggregate shape
    ``{op_class, result_status}``.
    """
    key = make_rsa_keypair("kid-downgrade")
    admin_token = _token(key, role=TenantRole.TENANT_ADMIN)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rule_id = await _create_override(
            client, admin_token, op_id_pattern=_LIST_OP_PATTERN, detail="aggregate"
        )
        # First GET after rule creation -- the cache was invalidated
        # by the POST so the resolver hydrates from DB and sees the
        # rule on this call's lookup.
        list_resp = await client.get("/api/v1/broadcast/overrides", headers=_bearer(admin_token))
    assert list_resp.status_code == 200, list_resp.text

    rows = await _audit_rows_for_tenant(_TENANT_A)
    list_rows = [
        row for row in rows if row.method == "GET" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(list_rows) == 1
    list_row = list_rows[0]
    assert list_row.payload["broadcast_detail_origin"] == f"tenant_rule:{rule_id}"
    assert list_row.payload["broadcast_detail_effective"] == "aggregate"

    events = await _drain_stream(broadcast_runtime, _TENANT_A)
    list_events = [e for e in events if e["op_id"] == _LIST_OP_PATTERN]
    assert list_events, "no broadcast event recorded for the list call"
    aggregated = list_events[-1]["payload"]
    assert aggregated == {"op_class": "read", "result_status": "ok"}


# ---------------------------------------------------------------------------
# Scenario 3 -- scoped rule scope-miss falls through to default
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_scoped_rule_scope_miss_falls_through_to_default(
    production_app: FastAPI,
) -> None:
    """A scoped rule whose ``namespace`` isn't in raw_params doesn't match.

    Set a rule with ``scope_field=namespace, scope_value=kube-system``
    against the list op. The route never binds ``audit_namespace``
    so the resolver's raw_params dict has no ``namespace`` key;
    :func:`~meho_backplane.broadcast.overrides._match_scope` therefore
    returns False, no rule matches, and the audit row's origin is
    ``"default"``.
    """
    key = make_rsa_keypair("kid-scope-miss")
    admin_token = _token(key, role=TenantRole.TENANT_ADMIN)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await _create_override(
            client,
            admin_token,
            op_id_pattern=_LIST_OP_PATTERN,
            detail="aggregate",
            scope_field="namespace",
            scope_value="kube-system",
        )
        list_resp = await client.get("/api/v1/broadcast/overrides", headers=_bearer(admin_token))
    assert list_resp.status_code == 200, list_resp.text

    rows = await _audit_rows_for_tenant(_TENANT_A)
    list_rows = [
        row for row in rows if row.method == "GET" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(list_rows) == 1
    list_row = list_rows[0]
    assert list_row.payload["broadcast_detail_origin"] == "default"
    assert list_row.payload["broadcast_detail_effective"] == "full"


# ---------------------------------------------------------------------------
# Scenario 4 -- DELETE invalidates the resolver cache
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_delete_rule_invalidates_resolver_cache(
    production_app: FastAPI,
) -> None:
    """After DELETE, the next publish sees the rule gone, not stale-cached.

    Stages: create rule → list call (aggregate) → DELETE rule → list
    call (default full). The two list-call audit rows must carry
    different origins; the second's must be ``"default"`` so the
    cache invalidation hook in
    :func:`~meho_backplane.api.v1.broadcast_overrides.delete_override_impl`
    is exercised through to the next request's resolver lookup.
    """
    key = make_rsa_keypair("kid-cache-bust")
    admin_token = _token(key, role=TenantRole.TENANT_ADMIN)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rule_id = await _create_override(
            client, admin_token, op_id_pattern=_LIST_OP_PATTERN, detail="aggregate"
        )
        list_resp_pre = await client.get(
            "/api/v1/broadcast/overrides", headers=_bearer(admin_token)
        )
        await _delete_override(client, admin_token, rule_id)
        list_resp_post = await client.get(
            "/api/v1/broadcast/overrides", headers=_bearer(admin_token)
        )
    assert list_resp_pre.status_code == 200
    assert list_resp_post.status_code == 200

    rows = await _audit_rows_for_tenant(_TENANT_A)
    list_rows = [
        row for row in rows if row.method == "GET" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(list_rows) == 2
    pre_row, post_row = list_rows
    assert pre_row.payload["broadcast_detail_origin"] == f"tenant_rule:{rule_id}"
    assert pre_row.payload["broadcast_detail_effective"] == "aggregate"
    assert post_row.payload["broadcast_detail_origin"] == "default"
    assert post_row.payload["broadcast_detail_effective"] == "full"


# ---------------------------------------------------------------------------
# Scenario 5 -- origin tagging records all three branches in one pass
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_origin_tagging_records_all_three_branches(
    production_app: FastAPI,
) -> None:
    """One pass exercising request_override / tenant_rule / default origins.

    Three back-to-back calls sharing the same JWT key + respx mock:

    1. ``POST /api/v1/audit/query`` + ``X-Broadcast-Detail: full`` →
       origin ``request_override``, effective ``full``.
    2. ``GET /api/v1/broadcast/overrides`` after seeding a downgrade
       rule → origin ``tenant_rule:<id>``, effective ``aggregate``.
    3. ``GET /api/v1/broadcast/overrides`` after deleting the rule →
       origin ``default``, effective ``full``.

    The single test ties the three branches together so a regression
    in any one branch is visible against the same JWT + tenant baseline.
    """
    key = make_rsa_keypair("kid-origins")
    operator_token = _token(key, role=TenantRole.OPERATOR)
    admin_token = _token(key, sub="op-admin", role=TenantRole.TENANT_ADMIN)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        opt_in_resp = await client.post(
            "/api/v1/audit/query",
            json=_audit_query_body(),
            headers={**_bearer(operator_token), "X-Broadcast-Detail": "full"},
        )
        rule_id = await _create_override(
            client, admin_token, op_id_pattern=_LIST_OP_PATTERN, detail="aggregate"
        )
        list_under_rule = await client.get(
            "/api/v1/broadcast/overrides",
            headers=_bearer(admin_token),
        )
        await _delete_override(client, admin_token, rule_id)
        list_after_delete = await client.get(
            "/api/v1/broadcast/overrides",
            headers=_bearer(admin_token),
        )
    assert opt_in_resp.status_code == 200
    assert list_under_rule.status_code == 200
    assert list_after_delete.status_code == 200

    rows = await _audit_rows_for_tenant(_TENANT_A)
    opt_in_row = next(r for r in rows if r.path == "/api/v1/audit/query")
    list_rows = [
        row for row in rows if row.method == "GET" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(list_rows) == 2
    pre_row, post_row = list_rows
    assert opt_in_row.payload["broadcast_detail_origin"] == "request_override"
    assert opt_in_row.payload["broadcast_detail_effective"] == "full"
    assert pre_row.payload["broadcast_detail_origin"] == f"tenant_rule:{rule_id}"
    assert pre_row.payload["broadcast_detail_effective"] == "aggregate"
    assert post_row.payload["broadcast_detail_origin"] == "default"
    assert post_row.payload["broadcast_detail_effective"] == "full"


# ---------------------------------------------------------------------------
# Scenario 6 -- RBAC blocks non-admin on REST and MCP
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_rbac_blocks_non_admin_on_rest_and_mcp(
    production_app: FastAPI,
) -> None:
    """Operator role gets 403 on REST + JSON-RPC ``-32602`` on MCP.

    REST: ``POST /api/v1/broadcast/overrides`` from an ``operator``
    role token returns 403 + ``insufficient_role`` detail (T4's RBAC
    gate, exercised end-to-end through ``require_role``).

    MCP: ``tools/call meho.broadcast.overrides.set`` from the same
    operator token returns either a JSON-RPC ``-32602`` envelope
    whose ``error.message`` contains the ``forbidden`` token, or an
    HTTP 401/403 if the MCP audience guard rejects the operator's
    token first. The "tool ran" path is the failure mode the
    Initiative DoD forbids; both rejection paths are acceptable.
    """
    key = make_rsa_keypair("kid-rbac")
    op_token = _token(key, role=TenantRole.OPERATOR)
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rest_resp = await client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers=_bearer(op_token),
        )
        assert rest_resp.status_code == 403
        assert rest_resp.json()["detail"] == "insufficient_role"
        mcp_resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "rbac-check",
                "method": "tools/call",
                "params": {
                    "name": "meho.broadcast.overrides.set",
                    "arguments": {
                        "op_id_pattern": "vault.kv.*",
                        "detail": "aggregate",
                    },
                },
            },
            headers=_bearer(op_token),
        )
        assert mcp_resp.status_code in (200, 401, 403)
        if mcp_resp.status_code == 200:
            body = mcp_resp.json()
            assert "error" in body, body
            assert body["error"]["code"] == -32602, body["error"]
            assert "forbidden" in body["error"]["message"].lower(), body["error"]


# ---------------------------------------------------------------------------
# Scenario 7 -- tenant A's rule does not affect tenant B
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_DOCKER_SKIP_REASON)
async def test_tenant_isolation_rule_does_not_leak(production_app: FastAPI) -> None:
    """Tenant A's downgrade rule does not change tenant B's broadcast detail.

    Two parallel JWT chains (one key per tenant). Tenant A's admin
    creates a downgrade rule on ``meho.broadcast.overrides.list``;
    tenant B's admin then does a list call. The audit row for tenant
    B's call must carry ``broadcast_detail_origin = "default"`` --
    tenant A's rule lives only in tenant A's row set (verified by the
    resolver's ``WHERE tenant_id`` filter) and the per-tenant cache
    is keyed by tenant_id so cross-pollination is impossible.
    """
    key_a = make_rsa_keypair("kid-iso-a")
    key_b = make_rsa_keypair("kid-iso-b")
    admin_a = _token(key_a, sub="op-a", role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_A)
    admin_b = _token(key_b, sub="op-b", role=TenantRole.TENANT_ADMIN, tenant_id=_TENANT_B)
    combined_jwks = {
        "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
    }
    async with _make_async_client(production_app) as client, respx.mock() as r:
        mock_discovery_and_jwks(r, combined_jwks)
        await _create_override(client, admin_a, op_id_pattern=_LIST_OP_PATTERN, detail="aggregate")
        b_resp = await client.get("/api/v1/broadcast/overrides", headers=_bearer(admin_b))
    assert b_resp.status_code == 200, b_resp.text

    b_rows = await _audit_rows_for_tenant(_TENANT_B)
    b_list_rows = [
        row for row in b_rows if row.method == "GET" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(b_list_rows) == 1
    b_row = b_list_rows[0]
    assert b_row.payload["broadcast_detail_origin"] == "default"
    assert b_row.payload["broadcast_detail_effective"] == "full"


# Smoke import check -- catches syntax + top-level binding errors in
# the lane that lacks Docker (the per-scenario tests would otherwise
# skip silently with no signal that the module itself is healthy).
def test_module_imports_cleanly() -> None:
    assert callable(_drain_stream)
    assert callable(_audit_rows_for_tenant)
    assert hasattr(redis_async, "from_url")
    assert _TENANT_A != _TENANT_B
