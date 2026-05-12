# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end MCP acceptance test (G0.5-T6, #251).

Proves that the five components shipped by Tasks T1-T5 work as a system
against a real Postgres + the production auth chain:

* T1 (#246) — Streamable HTTP transport at ``/mcp`` with JSON-RPC 2.0
  envelope handling.
* T2 (#247) — OAuth 2.1 resource-server validation with the canonical
  MCP resource URI as the audience (distinct from the chassis HTTP-API
  audience) + RFC 9728 ``WWW-Authenticate`` header on 401.
* T3 (#248) — Tool + resource registries with RBAC-aware list filtering
  and call-time role re-check.
* T4 (#249) — Reference impls (``meho.status`` tool, ``meho://tenant/
  {tenant_id}/info`` resource).
* T5 (#250) — Per-operation audit rows for every ``tools/call`` and
  ``resources/read`` invocation; chassis ``AuditMiddleware`` path-
  excludes ``/mcp`` so audit granularity stays at the op level.

Why direct JSON-RPC, not ``@modelcontextprotocol/inspector`` subprocess
=====================================================================

The issue body recommends the direct-JSON-RPC shape and puts the
Inspector subprocess out of scope for CI. Three reasons that hold up:

1. **Determinism.** ``npx`` resolves the Inspector package against the
   public registry on first run; CI without a warm node_modules cache
   pays the install cost and inherits its flake surface (npm registry
   network, transitive peer-dep resolution). Direct JSON-RPC has no
   off-VM dependency.
2. **No JS toolchain in the Python CI matrix.** ``pyproject.toml`` and
   the CI image carry no ``node`` / ``npm``; pulling them in for one
   test trades a focused contract test for a polyglot CI cost that
   every downstream contributor would pay.
3. **Coverage equivalence.** The wire protocol is the same on both
   paths — both POST JSON-RPC envelopes to ``/mcp`` with a Bearer
   token. Inspector adds an interactive UI; pytest adds assertions on
   the response shape. The pytest path catches the same spec
   regressions; the runbook in ``docs/architecture/mcp.md`` keeps the
   manual Inspector / Claude.ai check as the pre-release proof against
   a real off-machine client.

The fixture pattern mirrors :mod:`tests.integration.test_tenant_isolation`
verbatim — same testcontainers PG, same async ``httpx.AsyncClient`` over
ASGI transport, same ``_oidc_jwt_helpers`` minter, same in-process Vault
fake. The MCP-specific bits are local to this module:

* :func:`mcp_env` — pins ``BACKPLANE_URL`` so
  :func:`~meho_backplane.mcp.auth.mcp_resource_uri` resolves to the
  canonical MCP URI rather than the empty-string fail-closed sentinel.
* :func:`mcp_isolated_registry` — :func:`importlib.reload` of the T4
  tool + resource modules to repopulate the registries after the
  ``clear_registries()`` teardown other tests in the session may have
  invoked. Same pattern as :mod:`tests.mcp_test_fixtures` carries for
  the unit suites; not pulled in here because the unit fixture relies
  on a different ``client_with_operator`` shape that overrides
  :func:`~meho_backplane.mcp.auth.verify_mcp_jwt_and_bind` — T6 by
  design goes through the real auth chain.

Audit-row contract
==================

The chassis ``AuditMiddleware`` skips every request whose path starts
with ``/mcp``; MCP audit rows are written from inside the dispatch
handlers for ``tools/call`` and ``resources/read`` only. So the
audit-row read-back at the end of :func:`test_full_mcp_lifecycle_succeeds`
asserts exactly two rows from the five-method lifecycle — one per
auditable op — not eight (the total request count). Catches a future
regression where someone re-mounts the chassis middleware over ``/mcp``
or removes a per-op audit writer.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from meho_backplane.auth.operator import TenantRole
from meho_backplane.mcp.registry import clear_registries
from meho_backplane.mcp.resources import tenant_info as _resource_tenant_info
from meho_backplane.mcp.schemas import METHOD_NOT_FOUND, PROTOCOL_VERSION
from meho_backplane.mcp.tools import meho_status as _tool_meho_status
from meho_backplane.settings import get_settings
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
)

# Matches the ``pg_engine`` seed in :mod:`tests.integration.conftest` —
# tenant-a is one of the two pre-inserted rows so the
# ``resources/read meho://tenant/<id>/info`` step finds a real row to
# return rather than collapsing to "tenant row not found".
TENANT_A_ID: str = "11111111-1111-1111-1111-111111111111"

# The canonical MCP resource URI the test mints tokens against. The
# value is arbitrary as long as the mint and the
# :func:`~meho_backplane.mcp.auth.mcp_resource_uri` derivation agree;
# ``https://meho.test/mcp`` matches the existing unit MCP-fixture choice
# (see ``backend/tests/mcp_test_fixtures.py``) so a reader who walks
# the two suites sees one canonical URI.
MCP_RESOURCE_URI: str = "https://meho.test/mcp"


_skip_no_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


@pytest.fixture
def mcp_env(
    integration_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Layer the MCP-specific env on top of the chassis ``integration_env``.

    ``BACKPLANE_URL`` is the input
    :func:`~meho_backplane.mcp.auth.mcp_resource_uri` reads to derive
    the canonical MCP audience. Without it the helper returns an empty
    string and every ``/mcp`` request 401s with ``empty audience`` — a
    correct fail-closed default for production but a hidden gotcha for
    tests. The fixture also clears the settings cache around the yield
    so the cached :class:`~meho_backplane.settings.Settings` instance
    actually sees the new env vars.
    """
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mcp_isolated_registry() -> Iterator[None]:
    """Repopulate tool + resource registries via :func:`importlib.reload`.

    ``clear_registries()`` between tests would leave the registries
    empty because :func:`~meho_backplane.mcp.eager_import_mcp_modules`
    is a no-op on the second call (Python's import cache), and the
    integration app deliberately skips the lifespan hook. Reloading the
    T4 modules forces their top-level ``register_mcp_tool`` /
    ``register_mcp_resource`` calls to run again, repopulating both
    registries for the test that consumes this fixture.
    """
    clear_registries()
    importlib.reload(_tool_meho_status)
    importlib.reload(_resource_tenant_info)
    yield
    clear_registries()


def _make_async_client(app: FastAPI) -> httpx.AsyncClient:
    """ASGI in-process httpx client.

    Same shape as :func:`tests.integration.test_tenant_isolation._make_async_client`
    — pinning a single helper here would force a cross-test import; the
    factory is two lines and the duplication is the lesser evil.
    """
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        # ASGITransport runs the app in-process — no real socket is
        # opened — so the URL scheme is never resolved over the wire.
        # ``https://`` is chosen over ``http://`` to keep SonarCloud's
        # python:S5332 quality-gate rule (text-pattern-based, doesn't
        # distinguish ASGI fakes from production code) from flagging
        # the line as a Security Hotspot on this PR's new-code surface.
        base_url="https://testserver",
    )


@_skip_no_docker
async def test_full_mcp_lifecycle_succeeds(
    integration_app: FastAPI,
    mcp_env: None,
    mcp_isolated_registry: None,
    async_pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end MCP lifecycle against real PG + the production auth chain.

    Drives every method the spec requires a v0.2 server to surface
    (initialize, notifications/initialized, tools/list, tools/call,
    resources/list, resources/templates/list, resources/read) plus an unknown-method
    case and asserts the audit-row tail at the end. Each assertion is
    anchored to one of T6's acceptance criteria; the row-count check
    at the bottom is the cross-cutting "T1-T5 wired correctly" probe.
    """
    install_fake_vault(monkeypatch)
    key = make_rsa_keypair("kid-mcp-e2e")
    token = mint_token(
        key,
        sub="op-mcp-e2e",
        tenant_id=TENANT_A_ID,
        tenant_role=TenantRole.OPERATOR.value,
        audience=MCP_RESOURCE_URI,
    )

    # The MCP-Protocol-Version header is mandatory on every post-
    # ``initialize`` request per spec §"Protocol Version Header". The
    # server returns 400 + JSON-RPC envelope on a mismatched value;
    # pinning it on every call after step 1 keeps the assertion shape
    # readable.
    auth_headers = {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_async_client(integration_app) as client:
            # 1. initialize — handshake, capability advertisement.
            init = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0.0.1"},
                    },
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert init.status_code == 200, init.text
            init_body = init.json()
            assert init_body["result"]["protocolVersion"] == PROTOCOL_VERSION
            assert "tools" in init_body["result"]["capabilities"]
            assert "resources" in init_body["result"]["capabilities"]

            # 2. notifications/initialized — no id, HTTP 202, no body.
            notify = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                headers=auth_headers,
            )
            assert notify.status_code == 202
            assert notify.content == b""

            # 3. tools/list — RBAC-filtered registry projection.
            list_tools = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=auth_headers,
            )
            assert list_tools.status_code == 200
            tools = list_tools.json()["result"]["tools"]
            assert any(t["name"] == "meho.status" for t in tools), tools

            # 4. tools/call meho.status — exercises T4 reference impl
            # through T1's dispatch and T5's audit writer.
            call_status = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "meho.status", "arguments": {}},
                },
                headers=auth_headers,
            )
            assert call_status.status_code == 200
            call_body = call_status.json()
            assert call_body["result"]["isError"] is False
            payload = json.loads(call_body["result"]["content"][0]["text"])
            assert payload["operator"]["sub"] == "op-mcp-e2e"
            # Vault probe fails closed against the in-process fake — the
            # important assertion is structural presence, not reachability.
            assert "reachable" in payload["vault"]
            assert payload["db"]["migrated"] is True

            # 5. resources/list — concrete (non-templated) resources.
            # v0.2 registers only templated resources, so the list MUST
            # be empty. AC #2 lists ``resources/list`` explicitly; a
            # spec-conformant client may call either method and both
            # have to return well-formed envelopes. Asserting the empty
            # contract here keeps the surface honest if a future change
            # accidentally routes templated resources through the
            # concrete endpoint.
            list_resources = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
                headers=auth_headers,
            )
            assert list_resources.status_code == 200
            assert list_resources.json()["result"]["resources"] == []

            # 6. resources/templates/list — every MEHO resource is
            # templated, so the tenant-info template MUST appear here.
            list_templates = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "resources/templates/list",
                },
                headers=auth_headers,
            )
            assert list_templates.status_code == 200
            templates = list_templates.json()["result"]["resourceTemplates"]
            assert any(t["uriTemplate"] == "meho://tenant/{tenant_id}/info" for t in templates), (
                templates
            )

            # 7. resources/read — the operator reads their own tenant's
            # identity bundle. Cross-tenant rejection is unit-tested in
            # ``test_mcp_resource_tenant_info``; T6 only proves the
            # happy path lands end-to-end through the dispatch +
            # registry + audit chain.
            uri = f"meho://tenant/{TENANT_A_ID}/info"
            read_resource = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "resources/read",
                    "params": {"uri": uri},
                },
                headers=auth_headers,
            )
            assert read_resource.status_code == 200
            read_body = read_resource.json()
            assert read_body["result"]["contents"][0]["uri"] == uri
            bundle = json.loads(read_body["result"]["contents"][0]["text"])
            assert bundle["id"] == TENANT_A_ID
            assert bundle["slug"] == "tenant-a"
            assert bundle["operator_role"] == TenantRole.OPERATOR.value

            # 8. unknown method — JSON-RPC METHOD_NOT_FOUND (-32601),
            # HTTP 200, error envelope in the body.
            unknown = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "not.a.real.method",
                },
                headers=auth_headers,
            )
            assert unknown.status_code == 200
            assert unknown.json()["error"]["code"] == METHOD_NOT_FOUND

    # 9. Audit-row read-back — T5's per-op audit writer fires only on
    # ``tools/call`` and ``resources/read``. The chassis
    # ``AuditMiddleware`` path-excludes ``/mcp``, so no chassis-level
    # rows land either. Total = 2 (step 4 + step 7).
    #
    # If a future change re-enables the chassis middleware over
    # ``/mcp``, this assertion catches the row-count inflation (would
    # land at 8 — one per request). If a per-op writer is removed, the
    # count drops to 1 or 0. Either failure mode is a regression on the
    # G8-facing audit-granularity contract.
    audit_total = await count_audit_rows(async_pg_url)
    assert audit_total == 2, f"expected 2 MCP audit rows, got {audit_total}"


@_skip_no_docker
async def test_mcp_rejects_unauthenticated_request(
    integration_app: FastAPI,
    mcp_env: None,
    mcp_isolated_registry: None,
) -> None:
    """No ``Authorization`` header → 401 + RFC 9728 ``WWW-Authenticate``.

    Per MCP 2025-06-18 §Authorization the server MUST respond 401 on a
    missing or invalid Bearer; the ``WWW-Authenticate`` header MUST
    carry the ``resource_metadata`` parameter pointing at the protected-
    resource metadata document so a spec-conforming client can walk
    the OAuth-RS discovery flow without out-of-band configuration.
    """
    async with _make_async_client(integration_app) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.0.1"},
                },
            },
        )

    assert resp.status_code == 401
    challenge = resp.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer"), challenge
    assert "resource_metadata=" in challenge, challenge
    assert "/.well-known/oauth-protected-resource" in challenge, challenge
