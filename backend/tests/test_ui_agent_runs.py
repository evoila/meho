# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the agent-runs UI surface (Task #1830, G10.8-T3).

Initiative #1824 (G10.8 Agents console). Acceptance criteria on issue
#1830:

* Status + work_ref filters work and are bookmarkable; empty states per
  filter.
* Detail polls only while non-terminal; terminal runs render statically.
* Timestamps coerce to UTC-aware before relative rendering (so the SQLite
  test path does not ``TypeError``).
* Operator-gated; no writes here. Tenant-scoped (cross-tenant / absent ->
  404 on detail, invisible on list).
* Never leaks ``system_prompt`` / ``toolset`` / approval params.

Harness shape mirrors :mod:`backend.tests.test_ui_scheduler`: a minimal
FastAPI app wired with the UI session + CSRF middlewares, a ``web_session``
row carrying the tenant id (the read path synthesises an ``OPERATOR`` from
the session context -- no JWT round-trip), and seeded ``tenant`` /
``agent_run`` rows in the autouse SQLite engine.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    Tenant,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_OPERATOR = "op-operator"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the scheduler suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()
    reset_engine_for_testing()


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_definition(*, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert an ``agent_definition`` row and return its id (for the soft-FK)."""
    did = uuid.uuid4()

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentDefinition(
                    id=did,
                    tenant_id=tenant_id,
                    name=name,
                    identity_ref=f"agent:{name}",
                    model_tier="standard",
                    system_prompt="You triage incidents.",
                    toolset={},
                    turn_budget=5,
                    output_schema=None,
                    enabled=True,
                    created_by_sub="seed-admin",
                ),
            )
        return did

    return asyncio.run(_do())


def _seed_run(
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    status_value: str = AgentRunStatus.RUNNING.value,
    trigger: str = AgentRunTrigger.DIRECT.value,
    agent_definition_id: uuid.UUID | None = None,
    work_ref: str | None = None,
    provider: str | None = "anthropic",
    model: str | None = "claude-sonnet",
    model_tier: str = "standard",
    turns: int = 2,
    output: dict[str, object] | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> uuid.UUID:
    """Insert an ``agent_run`` row positioned at *status_value*.

    Status / output / timestamps are written directly (not through the
    transition guard) so a test can place a row at any state -- the UI's
    render is what's under test, not the state machine.
    """
    rid = run_id or uuid.uuid4()
    now = datetime.now(UTC)

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentRun(
                    id=rid,
                    agent_definition_id=agent_definition_id,
                    tenant_id=tenant_id,
                    identity_sub=_OP_OPERATOR,
                    identity_act=None,
                    trigger=trigger,
                    model_tier=model_tier,
                    provider=provider,
                    model=model,
                    status=status_value,
                    turns=turns,
                    output=output,
                    error=error,
                    work_ref=work_ref,
                    created_at=now,
                    started_at=started_at,
                    ended_at=ended_at,
                ),
            )
        return rid

    return asyncio.run(_do())


def _seed_session_sync(*, tenant_id: uuid.UUID, operator_sub: str = _OP_OPERATOR) -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="unused-on-the-read-path",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _client_for_tenant(tenant_id: uuid.UUID) -> TestClient:
    """A TestClient carrying a BFF session cookie for *tenant_id*.

    The runs read path synthesises an ``OPERATOR`` operator from the
    session context (no JWT round-trip), so no respx mock / minted token is
    needed -- just a session row with the tenant id.
    """
    session_id = _seed_session_sync(tenant_id=tenant_id)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents/runs`` without a session 302s to the BFF login."""
    client = TestClient(_build_app(), follow_redirects=False)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents/runs/{handle}`` without a session 302s to login."""
    client = TestClient(_build_app(), follow_redirects=False)
    response = client.get(f"/ui/agents/runs/{uuid.uuid4()}")
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# GET /ui/agents/runs -- list
# ---------------------------------------------------------------------------


def test_list_renders_run_row_for_operator() -> None:
    """An operator sees their tenant's run; the row + relative created render."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.SUCCEEDED.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200, response.text
    body = response.text
    assert str(rid)[:8] in body
    assert "succeeded" in body
    # The list never leaks the agent's prompt / toolset.
    assert "system_prompt" not in body
    assert "toolset" not in body


def test_list_renders_agent_name_column() -> None:
    """A run with a live definition shows the agent name in its row (#2472)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    did = _seed_definition(tenant_id=_TENANT_A, name="triage-bot")
    _seed_run(
        tenant_id=_TENANT_A,
        status_value=AgentRunStatus.SUCCEEDED.value,
        agent_definition_id=did,
    )
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200, response.text
    body = response.text
    assert "Agent" in body  # column header
    assert "triage-bot" in body  # resolved per-run agent name


def test_list_renders_dash_for_ad_hoc_run_agent() -> None:
    """A run with no definition (ad-hoc) renders without an agent name, no 500 (#2472)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(
        tenant_id=_TENANT_A,
        status_value=AgentRunStatus.SUCCEEDED.value,
        agent_definition_id=None,
    )
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200, response.text
    assert str(rid)[:8] in response.text


def test_list_renders_back_to_agents_breadcrumb() -> None:
    """The runs list carries a breadcrumb back to ``/ui/agents`` (#2347)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.SUCCEEDED.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'aria-label="Breadcrumb"' in body
    assert '<a href="/ui/agents" class="link link-hover">Agents</a>' in body


def test_list_status_filter_narrows_rows() -> None:
    """``?status=failed`` shows only the failed run, not the running one."""
    _seed_tenant(_TENANT_A, "tenant-a")
    failed = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.FAILED.value)
    running = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.RUNNING.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", params={"status": "failed"})
    assert response.status_code == 200
    body = response.text
    assert str(failed)[:8] in body
    assert str(running)[:8] not in body


def test_list_work_ref_filter_narrows_rows() -> None:
    """``?work_ref=`` (exact) shows only the matching run."""
    _seed_tenant(_TENANT_A, "tenant-a")
    matching = _seed_run(tenant_id=_TENANT_A, work_ref="gh:evoila/meho#11")
    other = _seed_run(tenant_id=_TENANT_A, work_ref="gh:evoila/meho#99")
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", params={"work_ref": "gh:evoila/meho#11"})
    assert response.status_code == 200
    body = response.text
    assert str(matching)[:8] in body
    assert str(other)[:8] not in body


def test_list_empty_state_renders() -> None:
    """A filter matching nothing renders the empty-state row, not an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.SUCCEEDED.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", params={"status": "cancelled"})
    assert response.status_code == 200
    assert "No agent runs match the current filter." in response.text


def test_list_htmx_request_returns_fragment_not_full_page() -> None:
    """An ``HX-Request`` list fetch returns the rows fragment, not the chrome."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_run(tenant_id=_TENANT_A)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert 'id="agent-runs-table-body"' in body
    # The fragment is the <tbody> only -- no <html>/sidebar chrome.
    assert "<html" not in body.lower()


def test_list_invalid_status_filter_is_422() -> None:
    """An out-of-enum ``?status=`` fails at the HTTP boundary with 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", params={"status": "bogus"})
    assert response.status_code == 422


def test_list_empty_status_filter_is_200_unfiltered() -> None:
    """An empty ``?status=`` (the "All" option) returns the unfiltered list at 200.

    The HTMX status ``<select>``'s "All" option carries ``value=""``; picking
    it (or clearing the filter) submits ``?status=``. Without the empty-string
    coercion that empty value fails the ``AgentRunStatus`` enum validation and
    422s, so HTMX never swaps and the control silently no-ops. The unfiltered
    row must come back at 200.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.SUCCEEDED.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs", params={"status": ""})
    assert response.status_code == 200, response.text
    assert str(rid)[:8] in response.text


def test_list_is_tenant_isolated() -> None:
    """Tenant B's runs never appear on tenant A's list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    b_run = _seed_run(tenant_id=_TENANT_B, status_value=AgentRunStatus.RUNNING.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200
    assert str(b_run)[:8] not in response.text
    assert "No agent runs match the current filter." in response.text


def test_list_awaiting_approval_deep_links_to_approvals() -> None:
    """An ``awaiting_approval`` row surfaces a deep-link to /ui/approvals."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.AWAITING_APPROVAL.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs")
    assert response.status_code == 200
    assert 'href="/ui/approvals"' in response.text


# ---------------------------------------------------------------------------
# GET /ui/agents/runs/{handle} -- detail + poll
# ---------------------------------------------------------------------------


def test_detail_renders_terminal_run_statically() -> None:
    """A succeeded run renders its output and does NOT carry a poll directive."""
    _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    rid = _seed_run(
        tenant_id=_TENANT_A,
        status_value=AgentRunStatus.SUCCEEDED.value,
        output={"text": "done-ok"},
        started_at=now - timedelta(seconds=30),
        ended_at=now,
    )
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{rid}")
    assert response.status_code == 200, response.text
    body = response.text
    assert "done-ok" in body
    # Terminal => the status panel carries no self-poll directive. (The
    # full page's chrome carries unrelated hx-trigger directives -- e.g.
    # the approvals bell -- so we assert the panel's own poll target is
    # absent rather than the bare "hx-trigger" substring.)
    assert f'hx-get="/ui/agents/runs/{rid}"' not in body


def test_detail_renders_back_to_agents_breadcrumb() -> None:
    """The run-detail page breadcrumbs back to ``/ui/agents`` (not only the
    runs list), matching the other agents sub-views (#2347)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.SUCCEEDED.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{rid}")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'aria-label="Breadcrumb"' in body
    assert '<a href="/ui/agents" class="link link-hover">Agents</a>' in body
    assert '<a href="/ui/agents/runs" class="link link-hover">Runs</a>' in body


def test_detail_non_terminal_run_self_polls() -> None:
    """A running run renders the poll directive so HTMX keeps refreshing."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.RUNNING.value)
    client = _client_for_tenant(_TENANT_A)
    # Fetch the panel fragment (HX-Request) so the assertion sees the panel
    # alone, not the chrome -- the self-poll directives belong to the panel.
    response = client.get(f"/ui/agents/runs/{rid}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert f'hx-get="/ui/agents/runs/{rid}"' in body
    assert 'hx-trigger="every' in body


def test_detail_htmx_request_returns_status_panel_fragment() -> None:
    """An ``HX-Request`` detail fetch returns the status-panel fragment only."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(tenant_id=_TENANT_A, status_value=AgentRunStatus.RUNNING.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{rid}", headers={"HX-Request": "true"})
    assert response.status_code == 200
    body = response.text
    assert 'id="agent-run-status-panel"' in body
    assert "<html" not in body.lower()


def test_detail_failed_run_shows_error_not_output() -> None:
    """A failed run renders its error reason."""
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _seed_run(
        tenant_id=_TENANT_A,
        status_value=AgentRunStatus.FAILED.value,
        output=None,
        error="ToolError: backend 503",
    )
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{rid}")
    assert response.status_code == 200
    assert "ToolError: backend 503" in response.text


def test_detail_cross_tenant_is_404() -> None:
    """Tenant A polling tenant B's run id gets 404 (existence not leaked)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    b_run = _seed_run(tenant_id=_TENANT_B, status_value=AgentRunStatus.RUNNING.value)
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{b_run}")
    assert response.status_code == 404


def test_detail_absent_run_is_404() -> None:
    """An unknown run id is 404 -- indistinguishable from cross-tenant."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client = _client_for_tenant(_TENANT_A)
    response = client.get(f"/ui/agents/runs/{uuid.uuid4()}")
    assert response.status_code == 404


def test_detail_non_uuid_handle_is_422() -> None:
    """A non-UUID handle 422s at the path-param boundary (not bound as a name)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client = _client_for_tenant(_TENANT_A)
    response = client.get("/ui/agents/runs/not-a-uuid")
    assert response.status_code == 422
