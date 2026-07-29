# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Conventions UI surface.

Initiative #1838 (G10.12 Conventions console), Task #1895 (T1). The
acceptance criteria on issue #1895 are:

* ``GET /ui/conventions`` returns 200 for an authenticated operator and
  renders the always-on preamble token-budget banner; an over-budget
  tenant shows every ``dropped_slug`` in a red / error-styled element.
* The kind filter narrows the table (``?kind=workflow`` shows only
  workflow rows) while the banner still reflects the full operational
  budget.
* ``GET /ui/conventions/<slug>`` renders the full body via the sanitised
  ``render_markdown`` (a body containing ``<script>`` is escaped).
* Routes are session-cookie-authed (anonymous redirects to login) and
  call the in-process ``ConventionsService``, never the Bearer API.
* Route ordering: the literal ``/ui/conventions`` list route is matched
  ahead of ``/ui/conventions/{slug}``.

Suite shape mirrors :mod:`backend.tests.test_ui_memory_list`: a minimal
FastAPI app with the chassis middlewares + the BFF auth router + the UI
router, a seeded ``web_session`` row, and seeded ``tenant_convention``
rows.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.conventions.schemas import ConventionKind
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant, TenantConvention
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
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
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
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_A = "op-alice"

#: ~1500-char operational body. Two of these sum to ~900 estimated
#: tokens (>600), so the lower-priority one drops -- the over-budget
#: fixture.
_BIG_BODY = "Always confirm the change set with the operator before applying. " * 24


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the memory suite)."""
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


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app wired for the conventions UI tests."""
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
    """Insert one ``tenant`` row so the convention tenant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_convention(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    title: str,
    body: str,
    kind: ConventionKind,
    priority: int = 0,
) -> None:
    """Persist one ``tenant_convention`` row directly."""
    now = datetime.now(UTC)

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                TenantConvention(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    slug=slug,
                    title=title,
                    body=body,
                    kind=kind.value,
                    priority=priority,
                    created_by_sub=_OP_A,
                    created_at=now,
                    updated_at=now,
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
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-conventions-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
) -> tuple[TestClient, respx.MockRouter]:
    """Return a TestClient + a respx mock for the JWKS endpoint."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client, mock


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/conventions`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/conventions")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/conventions/<slug>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/conventions/some-rule")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Budget banner -- always-on + over-budget dropped-slugs in red
# ---------------------------------------------------------------------------


def test_list_renders_budget_banner_within_budget() -> None:
    """A within-budget tenant renders the calm banner with the token math."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="confirm-first",
        title="Confirm before applying",
        body="Always confirm before applying a destructive change.",
        kind=ConventionKind.OPERATIONAL,
        priority=10,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Conventions" in body
    # Banner present + the within-budget state + token math rendered.
    assert 'data-budget-state="ok"' in body
    assert "/ 600 tokens" in body


def test_list_over_budget_renders_dropped_slugs_in_error_style() -> None:
    """An over-budget tenant shows dropped slugs inside an error-styled element."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Two large operational bodies -- the sum exceeds the 600-token
    # budget so the lower-priority one is dropped from the preamble.
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="high-priority-rule",
        title="High priority",
        body=_BIG_BODY,
        kind=ConventionKind.OPERATIONAL,
        priority=100,
    )
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="low-priority-rule",
        title="Low priority",
        body=_BIG_BODY,
        kind=ConventionKind.OPERATIONAL,
        priority=1,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # Over-budget banner state.
    assert 'data-budget-state="over"' in body
    assert "alert-error" in body
    # The dropped slug is surfaced in a red / error-styled element.
    assert 'data-dropped-slug="low-priority-rule"' in body
    start = body.index('data-dropped-slug="low-priority-rule"')
    element = body[body.rfind("<", 0, start) : body.index(">", start) + 1]
    assert "badge-error" in element
    # Explicit "agents never see this rule" copy.
    assert "agents never see" in body


# ---------------------------------------------------------------------------
# Kind filter -- table narrows, banner stays full-operational
# ---------------------------------------------------------------------------


def test_list_kind_filter_narrows_table_but_banner_reflects_operational() -> None:
    """``?kind=workflow`` shows only workflow rows; banner still over-budget."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Over-budget operational set (drives the banner).
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="op-high",
        title="Op high",
        body=_BIG_BODY,
        kind=ConventionKind.OPERATIONAL,
        priority=100,
    )
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="op-low",
        title="Op low",
        body=_BIG_BODY,
        kind=ConventionKind.OPERATIONAL,
        priority=1,
    )
    # A workflow row that the kind filter should isolate.
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="wf-deploy",
        title="Deploy workflow",
        body="Run the deploy checklist.",
        kind=ConventionKind.WORKFLOW,
        priority=5,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions?kind=workflow")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # Table shows the workflow row only -- the operational rows are
    # filtered out of the table.
    assert 'data-slug="wf-deploy"' in body
    assert 'data-slug="op-high"' not in body
    # But the banner still reflects the full operational budget (over).
    assert 'data-budget-state="over"' in body
    assert 'data-dropped-slug="op-low"' in body


def test_row_carries_visible_identity_link_and_view_button() -> None:
    """Each convention row offers the converged detail-nav pair (#2463).

    The slug cell links to the detail page with the visible
    ``link link-primary`` styling (not the invisible ``link link-hover``),
    and a trailing actions column carries a ``View`` button to the same
    ``/ui/conventions/<slug>`` URL -- the page-nav list convention.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="confirm-first",
        title="Confirm before applying",
        body="Always confirm before applying a destructive change.",
        kind=ConventionKind.OPERATIONAL,
        priority=10,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # Visible identity link (never the hover-only styling).
    assert 'class="link link-primary"' in body
    assert "link link-hover" not in body
    assert 'href="/ui/conventions/confirm-first"' in body
    # Trailing View button to the same detail URL + its header cell.
    assert 'class="btn btn-ghost btn-xs"' in body
    assert 'aria-label="View convention confirm-first"' in body
    assert 'class="sr-only">Actions</th>' in body


def test_list_invalid_kind_returns_422() -> None:
    """A typoed kind query value 422s rather than collapsing to 'all'."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions?kind=bogus")
    finally:
        mock.stop()
    assert response.status_code == 422


def test_list_htmx_kind_filter_returns_table_fragment_only() -> None:
    """An HTMX kind-tab request returns the ``_table.html`` fragment (no chrome)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="wf-deploy",
        title="Deploy workflow",
        body="checklist",
        kind=ConventionKind.WORKFLOW,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions?kind=workflow", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="conventions-table"' in body
    assert "<title>" not in body


# ---------------------------------------------------------------------------
# Detail view -- sanitised Markdown body + 404
# ---------------------------------------------------------------------------


def test_detail_renders_full_body_markdown() -> None:
    """Detail renders the full body as HTML (Markdown -> tags)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="readme-rule",
        title="Readme rule",
        body="# Heading\n\n*italic* and **bold** and a `code` snippet.",
        kind=ConventionKind.REFERENCE,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/readme-rule")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    text = response.text
    assert "<h1>Heading</h1>" in text
    assert "<em>italic</em>" in text
    assert "<strong>bold</strong>" in text
    assert "<code>code</code>" in text


def test_detail_sanitises_inline_script_in_body() -> None:
    """Raw ``<script>`` in a convention body renders escaped, not executed."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="xss-probe",
        title="XSS probe",
        body='<script>alert("x")</script>',
        kind=ConventionKind.REFERENCE,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/xss-probe")
    finally:
        mock.stop()
    assert response.status_code == 200
    text = response.text
    # The body article shows the escaped form -- no live script tag.
    assert "&lt;script&gt;" in text


def test_detail_missing_slug_returns_404() -> None:
    """A non-existent slug returns 404 (info-leak avoidance: no 403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions/not-there")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_detail_cross_tenant_returns_404() -> None:
    """An operator in tenant B cannot see tenant A's convention (404)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_convention(
        tenant_id=_TENANT_A,
        slug="a-only",
        title="A only",
        body="for tenant a",
        kind=ConventionKind.OPERATIONAL,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id_b = _seed_session_sync(
        tenant_id=_TENANT_B, access_token="unused", operator_sub=_OP_A
    )
    client, mock = _authenticated_client(session_id=session_id_b, jwks=jwks)
    try:
        response = client.get("/ui/conventions/a-only")
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Route ordering + service wiring
# ---------------------------------------------------------------------------


def test_route_order_literal_list_not_swallowed_by_slug() -> None:
    """``/ui/conventions`` resolves the list (not ``{slug}`` with slug='').

    A request to the bare ``/ui/conventions`` must hit the list handler
    -- it renders the budget banner + kind tabs, which the detail
    handler never does. Asserting the banner element is present proves
    the literal list route won the match.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # List-only markers (the detail page has neither).
    assert 'role="tablist"' in body
    assert "data-budget-state=" in body


def test_ui_conventions_is_not_a_chassis_stub() -> None:
    """The real conventions router replaces any stub (no 'Coming soon')."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/conventions")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "Coming soon" not in response.text
