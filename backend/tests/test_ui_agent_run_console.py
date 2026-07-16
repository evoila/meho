# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Agent run console UI surface.

Initiative #1824 (G10.8 Agents console), Task #1829 (T2). The acceptance
criteria on issue #1829 are:

* Streaming console renders frames incrementally (SSE buffering #1389 is
  fixed): the bridge proxies ``invoker.stream_events`` per-frame.
* The SSE bridge is session-cookie-authed and tenant-isolated -- a
  cross-tenant / cross-session run handle yields no stream; the
  isolation comes from the lifted operator + the session-bound run
  token, not a hand-rolled key or a request parameter.
* The Run button surfaces 404 / 409 / 429-budget as actionable inline
  messages; CSRF double-submit gates the run POST.
* No Stop affordance (documented as T9 follow-up).

Suite shape mirrors :mod:`backend.tests.test_ui_agents`: a minimal
FastAPI app wired with the chassis middlewares + the BFF auth router +
the UI router; a ``web_session`` row seeded with a real Keycloak-minted
access token so ``resolve_run_operator_or_403`` can re-verify the token
and pick up the right :class:`TenantRole`. The agent invoker is replaced
with a deterministic fake via ``reset_agent_invoker_for_testing`` so the
SSE bridge streams known frames without a live model / DB run row.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentRunNotFoundError,
    BudgetExceededError,
    reset_agent_invoker_for_testing,
)
from meho_backplane.agent.run import AgentRunEvent, AgentRunEventKind
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AgentDefinition, Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.agents.run_token import mint_run_token
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OP_A = "op-alice"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the agents suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    reset_agent_invoker_for_testing(None)


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the run-console tests."""
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
    """Insert one ``tenant`` row so the agent-definition FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_agent(
    *,
    tenant_id: uuid.UUID,
    name: str,
    enabled: bool = True,
    turn_budget: int = 12,
) -> None:
    """Persist one ``agent_definition`` row directly (bypasses the service)."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentDefinition(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    identity_ref="agent-bot",
                    model_tier="standard",
                    system_prompt="You are a helpful ops agent.",
                    toolset={},
                    turn_budget=turn_budget,
                    output_schema=None,
                    enabled=enabled,
                    created_by_sub=_OP_A,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-agent-run-test-kid")
    return keypair, _public_jwks(keypair)


def _operator_token(keypair: Any, *, role: TenantRole = TenantRole.OPERATOR) -> str:
    """Mint an access token for the standard operator at *role*."""
    return _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=role.value,
    )


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + an open respx mock + a CSRF token."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    if with_csrf:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers a state-changing HTMX request carries (token + HX-Request)."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


class _FakeInvoker:
    """Deterministic stand-in for :class:`AgentInvoker`.

    ``ensure_runnable`` raises the configured error (or passes); the
    streaming generator yields a fixed sequence of run events so the SSE
    bridge emits known frames without a model / DB round-trip. The
    ``operator`` arg is captured so a test can assert the bridge passed
    the lifted (tenant-scoped) operator rather than a request parameter.
    """

    def __init__(
        self,
        *,
        ensure_error: Exception | None = None,
        events: list[AgentRunEvent] | None = None,
        run_id: uuid.UUID | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self._ensure_error = ensure_error
        self._events = events or []
        self._run_id = run_id or uuid.uuid4()
        self._cancel_error = cancel_error
        self.stream_calls: list[tuple[str, str]] = []
        self.stream_operators: list[Operator] = []
        self.cancel_calls: list[tuple[uuid.UUID, Operator]] = []

    async def ensure_runnable(self, operator: Operator, name: str) -> None:
        if self._ensure_error is not None:
            raise self._ensure_error

    async def stream_events(
        self,
        operator: Operator,
        name: str,
        inputs: str,
    ) -> AsyncIterator[tuple[uuid.UUID, AgentRunEvent]]:
        self.stream_calls.append((name, inputs))
        self.stream_operators.append(operator)
        for event in self._events:
            yield self._run_id, event

    async def cancel(self, operator: Operator, run_id: uuid.UUID) -> object:
        """Record the cancel + raise the configured error, else return a sentinel.

        The BFF cancel route discards the returned summary (it replies
        204), so a non-error path only has to not raise.
        """
        self.cancel_calls.append((run_id, operator))
        if self._cancel_error is not None:
            raise self._cancel_error
        return object()


# ---------------------------------------------------------------------------
# Console page (read)
# ---------------------------------------------------------------------------


def test_run_console_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents/{name}/run`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/agents/a1/run")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_run_console_renders_form_and_budget() -> None:
    """The console renders the run form, the turn budget, and no Stop control."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1", turn_budget=7)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/a1/run")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="agent-run-console"' in body
    assert 'name="input"' in body
    assert 'name="work_ref"' in body
    # The turn budget is surfaced so the operator sees the cost ceiling.
    assert "Turn budget" in body and ">7<" in body
    assert 'src="/ui/static/src/app/agent-run-console.js"' in body
    # No Stop affordance ships in this Task (T9 #1833 adds it).
    assert 'data-action="stop"' not in body.lower()


def test_run_console_missing_agent_returns_404() -> None:
    """An absent agent name renders 404, not the console form."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/ghost/run")
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Run POST (CSRF + runnable pre-check)
# ---------------------------------------------------------------------------


def test_run_post_without_csrf_is_403() -> None:
    """The run POST is CSRF-gated -- a submit without the token is 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "do the thing"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_run_post_read_only_operator_is_403() -> None:
    """A read_only operator cannot run an agent (operator-floor gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair, role=TenantRole.READ_ONLY)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "do the thing"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_run_post_returns_transcript_with_stream_token() -> None:
    """A valid run authorise returns the transcript fragment wired to the bridge."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "summarise the topology", "work_ref": "gh:evoila/meho#9"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="agent-run-transcript-live"' in body
    # The fragment carries an EventSource pointed at the cookie-authed
    # bridge with a run token (not the raw prompt) in the query string.
    assert "/ui/agents/a1/run/stream?token=" in body
    assert "summarise the topology" not in body  # prompt rides in the token, not the URL.


def test_run_transcript_stop_dialog_opts_out_of_autopen() -> None:
    """The Stop-confirm dialog must not auto-open when the transcript swaps in.

    The run transcript is swapped over ``#agent-run-transcript`` on Run submit;
    the app-shell modal controller auto-opens freshly swapped-in
    ``dialog.modal`` fragments on ``htmx:afterSwap``. The Stop dialog is
    button-driven, so it carries ``data-auto-open="false"`` to opt out of that
    sweep -- otherwise the confirm pops the instant a run starts and hides the
    live transcript (#2347).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "go"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-role="stop-confirm"' in body
    assert 'data-auto-open="false"' in body


def test_run_post_disabled_agent_renders_409_inline() -> None:
    """A disabled agent renders an inline 409 alert, not a torn stream."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1", enabled=False)
    reset_agent_invoker_for_testing(_FakeInvoker(ensure_error=AgentDisabledError("a1")))
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "run anyway"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 409, response.text
    body = response.text
    assert 'data-role="run-error"' in body
    assert "disabled" in body.lower()


def test_run_post_budget_exceeded_renders_429_inline() -> None:
    """A budget-refused run renders an inline 429 with the reason."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(
        _FakeInvoker(ensure_error=BudgetExceededError("per_tenant_cap"))
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "expensive run"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 429, response.text
    body = response.text
    assert 'data-role="run-error"' in body
    assert "budget" in body.lower()
    assert "per_tenant_cap" in body


def test_run_post_empty_prompt_renders_422_inline() -> None:
    """An empty prompt re-renders the form with an inline 422, never a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "   "},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-role="run-error"' in response.text


# ---------------------------------------------------------------------------
# SSE bridge (cookie auth + token isolation + streaming)
# ---------------------------------------------------------------------------


def test_stream_bridge_streams_frames_incrementally() -> None:
    """The bridge proxies stream_events, emitting one SSE frame per event."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    run_id = uuid.uuid4()
    fake = _FakeInvoker(
        events=[
            AgentRunEvent(kind=AgentRunEventKind.TURN, data={}),
            AgentRunEvent(
                kind=AgentRunEventKind.TOOL_CALL,
                data={"tool_name": "topology.query", "args": {"q": "all"}},
            ),
            AgentRunEvent(kind=AgentRunEventKind.FINAL, data={"output": {"ok": True}}),
        ],
        run_id=run_id,
    )
    reset_agent_invoker_for_testing(fake)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    run_token = mint_run_token(session_id=str(session_id), name="a1", input_="go", work_ref=None)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/a1/run/stream?token={run_token}")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Accel-Buffering"] == "no"
    text = response.text
    assert "event: turn" in text
    assert "event: tool_call" in text
    assert "event: final" in text
    assert "topology.query" in text
    assert str(run_id) in text
    # The bridge ran the prompt the token carried, for the lifted operator.
    assert fake.stream_calls == [("a1", "go")]
    assert fake.stream_operators[0].tenant_id == _TENANT_A


def test_stream_bridge_rejects_cross_session_token() -> None:
    """A token minted for one session is 403 when presented under another.

    Tenant / session isolation: the run a token authorised cannot be
    driven by a different session's cookie, so a leaked / replayed token
    yields no stream.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    # Token bound to a DIFFERENT (random) session id than the cookie's.
    foreign_token = mint_run_token(
        session_id=str(uuid.uuid4()), name="a1", input_="go", work_ref=None
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/a1/run/stream?token={foreign_token}")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_stream_bridge_rejects_token_name_mismatch() -> None:
    """A token minted for agent X cannot drive a stream for agent Y."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    _seed_agent(tenant_id=_TENANT_A, name="a2")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    run_token = mint_run_token(session_id=str(session_id), name="a1", input_="go", work_ref=None)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        # Present a1's token on the a2 stream path.
        response = client.get(f"/ui/agents/a2/run/stream?token={run_token}")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_stream_bridge_missing_token_is_403() -> None:
    """A bridge GET with no token streams nothing (no run starts)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    fake = _FakeInvoker(events=[AgentRunEvent(kind=AgentRunEventKind.TURN, data={})])
    reset_agent_invoker_for_testing(fake)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/a1/run/stream")
    finally:
        mock.stop()
    assert response.status_code == 403
    # No run was driven.
    assert fake.stream_calls == []


def test_stream_bridge_disabled_between_authorise_and_connect_is_409() -> None:
    """A definition disabled in the authorise->connect window surfaces 409.

    The bridge re-runs ``ensure_runnable`` before opening the stream so a
    race (disable / delete after the POST authorised the run) is a clean
    HTTP status, not a torn text/event-stream.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker(ensure_error=AgentDisabledError("a1")))
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    run_token = mint_run_token(session_id=str(session_id), name="a1", input_="go", work_ref=None)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/a1/run/stream?token={run_token}")
    finally:
        mock.stop()
    assert response.status_code == 409


def test_stream_bridge_not_found_is_404() -> None:
    """A token-named agent that no longer exists surfaces 404 before streaming."""
    _seed_tenant(_TENANT_A, "tenant-a")
    reset_agent_invoker_for_testing(_FakeInvoker(ensure_error=AgentNotFoundError("a1")))
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    run_token = mint_run_token(session_id=str(session_id), name="a1", input_="go", work_ref=None)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/a1/run/stream?token={run_token}")
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cancel proxy (Stop button BFF -- T9 #1833)
# ---------------------------------------------------------------------------


def test_run_cancel_without_csrf_is_403() -> None:
    """The cancel proxy is a state-changing POST -- a submit without CSRF is 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    handle = uuid.uuid4()
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_run_cancel_read_only_operator_is_403() -> None:
    """A read_only operator cannot cancel a run (operator-floor gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair, role=TenantRole.READ_ONLY)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    handle = uuid.uuid4()
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_run_cancel_success_returns_204() -> None:
    """A valid cancel drives invoker.cancel for the lifted operator and 204s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    fake = _FakeInvoker()
    reset_agent_invoker_for_testing(fake)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    handle = uuid.uuid4()
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    # The cancel ran for exactly this handle, scoped to the lifted operator.
    assert len(fake.cancel_calls) == 1
    cancelled_handle, cancel_operator = fake.cancel_calls[0]
    assert cancelled_handle == handle
    assert cancel_operator.tenant_id == _TENANT_A


def test_run_cancel_already_terminal_is_409() -> None:
    """An already-terminal run surfaces 409 (agent_run_not_cancellable)."""
    from meho_backplane.db.models import AgentRunStatus
    from meho_backplane.operations.agent_run import IllegalTransitionError

    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(
        _FakeInvoker(
            cancel_error=IllegalTransitionError(
                from_status=AgentRunStatus.SUCCEEDED,
                to_status=AgentRunStatus.CANCELLED,
            )
        )
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    handle = uuid.uuid4()
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "agent_run_not_cancellable"


def test_run_cancel_unknown_handle_is_404() -> None:
    """An unknown / cross-tenant handle surfaces 404 (existence not leaked)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    handle = uuid.uuid4()
    reset_agent_invoker_for_testing(_FakeInvoker(cancel_error=AgentRunNotFoundError(handle)))
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "agent_run_not_found"


def test_run_cancel_role_backstop_is_403() -> None:
    """The service's own role check (UnauthorizedCancellationError) maps to 403."""
    from meho_backplane.operations.agent_run import UnauthorizedCancellationError

    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(
        _FakeInvoker(
            cancel_error=UnauthorizedCancellationError(operator_sub=_OP_A, role=TenantRole.OPERATOR)
        )
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    handle = uuid.uuid4()
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/a1/run/{handle}/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "agent_run_cancel_forbidden"


def test_run_cancel_non_uuid_handle_is_422() -> None:
    """A non-UUID handle 422s at the path boundary (never reaches the invoker)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    fake = _FakeInvoker()
    reset_agent_invoker_for_testing(fake)
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run/not-a-uuid/cancel",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    assert fake.cancel_calls == []


def test_run_transcript_carries_stop_wiring() -> None:
    """The transcript fragment threads the cancel URL + CSRF token to Alpine."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_agent(tenant_id=_TENANT_A, name="a1")
    reset_agent_invoker_for_testing(_FakeInvoker())
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/a1/run",
            data={"input": "do the thing"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The Stop button + native <dialog> confirm + cancel-URL placeholder
    # all ship in the live transcript fragment.
    assert 'data-action="stop"' in body
    assert 'data-role="stop-confirm"' in body
    assert "/run/__RUN_ID__/cancel" in body
    assert "cancelUrlTemplate" in body


def test_can_stop_stays_available_after_stream_drop() -> None:
    """A dropped SSE stream must not disqualify a still-executing run from Stop.

    The transcript controller's ``canStop()`` gates the Stop button. The SSE
    bridge dying (``streamErrored``) does *not* terminate the backend run, so a
    run with a known ``runId`` and no terminal frame must stay cancellable after
    a stream drop -- otherwise the operator loses the only way to stop a run
    that is still burning budget. ``canStop()`` therefore gates on
    ``runId``/``finalStatus``/``cancelled``/``cancelUrlTemplate`` only, and must
    not reference ``streamErrored``. The controller is client-side Alpine state
    with no JS test runner in this repo, so we assert the source contract: the
    ``streamErrored`` flag is still tracked for transcript messaging but is
    absent from the ``canStop()`` predicate.
    """
    source = (static_root_dir() / "src" / "app" / "agent-run-console.js").read_text(
        encoding="utf-8"
    )

    # The drop flag is still part of the controller (it drives the dropped /
    # error transcript hints) -- this guards against asserting on a typo.
    assert "streamErrored" in source

    can_stop_match = re.search(r"canStop\(\)\s*\{(?P<body>.*?)\}", source, flags=re.DOTALL)
    assert can_stop_match is not None, "canStop() not found in controller source"
    can_stop_body = can_stop_match.group("body")

    assert "streamErrored" not in can_stop_body, (
        "canStop() must not gate on streamErrored: a dropped stream leaves a "
        "non-terminal backend run cancellable (review M1, PR #1878)."
    )
    # The genuine non-terminal gates remain.
    assert "this.runId" in can_stop_body
    assert "this.finalStatus" in can_stop_body
    assert "this.cancelled" in can_stop_body
    assert "this.cancelUrlTemplate" in can_stop_body
