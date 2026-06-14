# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Memory UI surface.

Initiative #341 (G10.4 Memory UI), Task #877 (G10.4-T1). The
acceptance criteria on issue #877 are:

* ``/ui/memory`` lists memories filtered by scope; switching scope
  tabs re-renders the list via HTMX; cards show slug / preview /
  scope-badge / expiry / tags.
* ``/ui/memory/<scope>/<slug>`` renders the body as server-side
  Markdown; edit-in-place saves via PATCH; delete via confirm modal
  + DELETE, list re-renders.
* RBAC: operator can edit own user-scoped; cannot edit tenant-scoped
  without ``tenant_admin`` (assert 403).
* Tag filter + autocomplete (``/ui/memory/tags``) narrows the list.
* Cross-user isolation (A's user-scoped invisible to B) + cross-tenant
  isolation.

Suite shape:

* :func:`_build_app` wires a minimal FastAPI app with the chassis
  middlewares + the BFF auth router + the UI router; mirrors
  :mod:`backend.tests.test_ui_chassis_smoke._build_app` and the
  topology suite's pattern.
* :func:`_seed_session_sync` writes a ``web_session`` row with a
  real Keycloak-minted access token (signed by the test JWKS) so
  the ``resolve_ui_operator`` write dep can re-verify the token and
  pick up the right :class:`TenantRole`.
* Tests cover list (empty + populated + scope-filter + tag-filter),
  detail (200 + Markdown + 404), edit-form (operator vs read-only
  RBAC + cross-user 404), PATCH save (happy + tenant-admin gate +
  body too large), DELETE (happy + re-render), tag autocomplete,
  cross-user isolation, and cross-tenant isolation.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Document, Tenant
from meho_backplane.memory._internal import (
    MEMORY_SOURCE,
    build_metadata,
    encode_source_id,
)
from meho_backplane.memory.schemas import MemoryScope, kind_for_scope
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BACKPLANE_URL = "https://meho.test"

#: Two stable tenant ids for the cross-tenant isolation assertion.
_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

#: Two stable operator subs for the cross-user isolation assertion.
_OP_A = "op-alice"
_OP_B = "op-bob"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test.

    Mirrors :func:`backend.tests.test_ui_topology_table._bff_env`
    + the chassis smoke + auth flow suites so the baseline (issuer,
    audience, vault url, encryption key, BFF client) is identical
    across the UI test surface.
    """
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
    """Construct a minimal FastAPI app wired for the memory UI tests.

    Mirrors the production wiring + the topology suite: StaticFiles
    at ``/ui/static``, BFF auth router + UI surface router (which
    includes the memory routes ahead of the stubs), ``UISessionMiddleware``
    outermost + ``CSRFMiddleware`` next.
    """
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
    """Insert one ``tenant`` row so the document tenant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_memory(
    *,
    tenant_id: uuid.UUID,
    scope: MemoryScope,
    slug: str,
    body: str,
    user_sub: str | None = None,
    target_name: str | None = None,
    tags: list[str] | None = None,
    expires_at: datetime | None = None,
) -> uuid.UUID:
    """Persist one memory row directly via the documents table.

    Bypasses :class:`MemoryService.remember` so the test doesn't need
    to mock the embedding service for the seeded body. The natural
    key encoding mirrors the service layer's
    :func:`encode_source_id` so a subsequent ``recall`` finds the row.

    Returns the seeded :class:`Document.id`.
    """
    metadata = build_metadata(
        caller_metadata={"tags": list(tags)} if tags else None,
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        expires_at=expires_at,
    )
    source_id = encode_source_id(
        scope=scope,
        user_sub=user_sub or "",
        target_name=target_name,
        slug=slug,
    )
    doc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Document(
                    id=doc_id,
                    tenant_id=tenant_id,
                    source=MEMORY_SOURCE,
                    source_id=source_id,
                    kind=kind_for_scope(scope),
                    body=body,
                    body_hash=f"sha256:test:{doc_id}",
                    embedding=[0.0] * 384,
                    doc_metadata=metadata,
                    tokens=len(body.split()),
                ),
            )

    asyncio.run(_do())
    return doc_id


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
    """Mint a stable RSA-2048 keypair + the matching JWKS document.

    Wraps :func:`tests._oidc_jwt_helpers.make_rsa_keypair` with a
    deterministic kid so a respx replay across multiple JWT decodes
    in one test resolves through the same cache entry.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-memory-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + a respx mock + a CSRF token.

    The mock router stays open across the test scope so the
    ``resolve_ui_operator`` dependency can call into the JWKS endpoint
    on each PATCH / DELETE / edit-form request. The caller is
    responsible for entering the mock as a context manager
    (``with mock: ...``).

    ``with_csrf=True`` pre-seeds the CSRF cookie so PATCH / DELETE
    requests can include the matching token without a GET round-trip
    -- the chassis cookie ships with ``secure=True``, which the
    TestClient's default ``http://testserver`` origin drops, so a
    GET-then-PATCH test would otherwise see a missing cookie. The
    third tuple element is the token value the caller passes back
    via the ``X-CSRF-Token`` header (the double-submit pair).
    """
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
    """Return the headers a state-changing HTMX request carries.

    Mirrors the page-level ``hx-headers`` directive that ships the
    CSRF token on every HTMX request from the memory surface.
    """
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/memory`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/memory")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/memory/user/x`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/memory/user/some-slug")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# List view -- full page + HTMX fragment + scope tabs + cards
# ---------------------------------------------------------------------------


def test_list_full_page_renders_with_empty_inventory() -> None:
    """``GET /ui/memory`` with no memories renders the empty state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A,
        access_token="unused",
        operator_sub=_OP_A,
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Memory" in body
    assert 'id="memory-cards"' in body
    assert "No memories match the current scope" in body
    # Scope tabs are rendered for every scope plus All.
    for label in ("User", "Tenant", "Target", "All visible"):
        assert label in body
    # CSRF cookie set by the route.
    assert CSRF_COOKIE_NAME in response.cookies


def test_list_renders_cards_with_preview_and_scope_badge() -> None:
    """Seeded memories render as cards with the slug + scope badge + preview."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="Damir prefers Riesling. " * 20,  # > 200 chars
        user_sub=_OP_A,
        tags=["preference", "wine"],
    )
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-runbook",
        body="On-call rotation lives in the kb.",
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "wine-pref" in body
    assert "team-runbook" in body
    # Scope badges + preview text both rendered.
    assert ">user<" in body
    assert ">tenant<" in body
    assert "Damir prefers Riesling." in body
    # Tag chips.
    assert "preference" in body
    assert "wine" in body
    # 200-char preview truncated -- the body is >400 chars but the
    # preview slice is bounded; verifying the ellipsis suffix is enough.
    assert "..." in body


def test_list_htmx_fragment_returns_cards_partial_only() -> None:
    """HTMX request to ``/ui/memory`` returns the ``_cards.html`` fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(tenant_id=_TENANT_A, scope=MemoryScope.USER, slug="x", body="x", user_sub=_OP_A)
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # Fragment includes ``#memory-cards`` but no full ``<title>`` chrome.
    assert 'id="memory-cards"' in body
    assert "<title>" not in body


def _sets_csrf_cookie(response: Any) -> bool:
    """Return ``True`` iff the response carries a ``meho_csrf`` ``Set-Cookie``.

    Reads every ``Set-Cookie`` header (``get_list`` -- a response can
    carry more than one) rather than the comma-joined ``.get`` shape so
    the assertion is robust if a sibling cookie ever rides the same
    response.
    """
    return any(
        f"{CSRF_COOKIE_NAME}=" in header for header in response.headers.get_list("set-cookie")
    )


def test_list_full_page_render_sets_fresh_csrf_cookie() -> None:
    """A full-page ``GET /ui/memory`` mints + Set-Cookies a fresh ``meho_csrf`` (#1754)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert _sets_csrf_cookie(response), (
        "full-page list render must Set-Cookie meho_csrf so the page's "
        "double-submit pair is established"
    )


def test_list_htmx_poll_does_not_rotate_csrf_cookie() -> None:
    """The ``every 60s`` HTMX poll returns NO ``Set-Cookie: meho_csrf`` (#1754).

    Root cause of the memory-create 403: ``render_index`` used to Set-
    Cookie a freshly-minted ``meho_csrf`` on every render -- including
    the cards fragment's 60-second poll. A poll firing while the create
    modal is open rotated the cookie out from under the modal's echoed
    token (#1693), so the next create POST failed the chassis double-
    submit match with ``403 csrf_token_invalid``. The fix sets the
    cookie on full-page renders only; a poll carrying a valid cookie
    re-echoes that same token and leaves the cookie untouched.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        # The browser holds the cookie the full-page load set; replay it
        # on the poll (the chassis cookie is ``Secure``, which the
        # http-scheme TestClient jar drops, so seed it explicitly).
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.get("/ui/memory?scope=all", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    assert not _sets_csrf_cookie(response), (
        "an HTMX poll carrying a live meho_csrf cookie must NOT rotate it -- "
        "rotating mid-page-stay is exactly the #1754 create-403 regression"
    )


def test_list_htmx_fragment_mints_cookie_when_none_present() -> None:
    """Defensive: an HTMX fragment fetch with no prior cookie still mints one (#1754).

    The poll-no-rotation rule reuses the request's existing ``meho_csrf``
    cookie. A fragment fetched without any prior full-page load (so no
    cookie on the request) must still get a freshly-minted cookie set,
    otherwise the fragment's own bulk-action form would carry an echoed
    token with no matching cookie and 403 on submit.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        # No CSRF cookie seeded -- simulate a fragment hit with no prior
        # full-page render on this client.
        response = client.get("/ui/memory?scope=all", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 200
    assert _sets_csrf_cookie(response), (
        "a fragment fetched without a prior meho_csrf cookie must mint + "
        "Set-Cookie one so its own forms validate"
    )


def test_list_scope_filter_narrows_to_one_scope() -> None:
    """``?scope=tenant`` returns only tenant-scoped memories."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A, scope=MemoryScope.USER, slug="user-1", body="u", user_sub=_OP_A
    )
    _seed_memory(tenant_id=_TENANT_A, scope=MemoryScope.TENANT, slug="tenant-1", body="t")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory?scope=tenant")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "tenant-1" in body
    assert "user-1" not in body


def test_list_invalid_scope_returns_422() -> None:
    """A typoed scope query value 422s rather than collapsing to 'all'."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory?scope=bogus")
    finally:
        mock.stop()
    assert response.status_code == 422


def test_list_tag_filter_narrows_results() -> None:
    """``?tag=wine`` returns only rows whose metadata.tags includes 'wine'."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
        tags=["wine"],
    )
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="food-pref",
        body="pasta",
        user_sub=_OP_A,
        tags=["food"],
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory?tag=wine")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "wine-pref" in body
    assert "food-pref" not in body


def test_tags_endpoint_returns_distinct_sorted_tag_options() -> None:
    """``/ui/memory/tags`` returns the union of tags in distinct sorted order."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="a",
        body="a",
        user_sub=_OP_A,
        tags=["wine", "food"],
    )
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="b",
        body="b",
        tags=["food", "ops"],
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/tags")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    # Three distinct tags, sorted; rendered as <option> elements.
    assert body.index("food") < body.index("ops") < body.index("wine")
    assert body.count("<option ") == 3


def test_list_datalist_overrides_inherited_hx_target() -> None:
    """The tag datalist pins ``hx-target="this"`` against form inheritance.

    Regression for #1695: htmx resolves ``hx-target`` closest-wins up
    the ancestor chain, so without a local override the datalist
    inherits the filter form's ``hx-target="#memory-cards"`` and the
    ``hx-trigger="load"`` options fetch replaces the card grid with
    bare ``<option>`` elements on every page load.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    start = body.index("<datalist")
    datalist_tag = body[start : body.index(">", start)]
    assert 'id="memory-tag-options"' in datalist_tag
    assert 'hx-target="this"' in datalist_tag


# ---------------------------------------------------------------------------
# Detail view -- 200 + Markdown render + 404 + cross-user
# ---------------------------------------------------------------------------


def test_detail_full_page_renders_markdown_body() -> None:
    """Detail page renders body as HTML (Markdown -> tags + escaped HTML)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    body_md = "# Title\n\n*italic* and **bold** and a `code` snippet."
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="readme",
        body=body_md,
        user_sub=_OP_A,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/readme")
    finally:
        mock.stop()
    assert response.status_code == 200
    text = response.text
    # Markdown rendered to HTML -- not raw Markdown.
    assert "<h1>Title</h1>" in text
    assert "<em>italic</em>" in text
    assert "<strong>bold</strong>" in text
    assert "<code>code</code>" in text


def test_detail_strips_attribute_injection_from_fenced_code_lang() -> None:
    """A crafted fenced-code lang token cannot escape the ``class="..."`` attribute.

    Before the allowlist landed, the fenced-code info string was
    interpolated verbatim into ``<code class="language-{lang}">``: a
    body of ```` ```a"onmouseover="alert(1)"x ```` rendered as an HTML
    fragment carrying a live ``onmouseover`` handler. The fix
    restricts the rendered ``lang`` to ``[A-Za-z0-9_+-.]+`` and falls
    back to ``text`` otherwise.
    """
    from meho_backplane.ui.routes.memory.render import render_markdown

    payload = '```a"onmouseover="alert(1)"x\nfoo\n```'
    out = str(render_markdown(payload))
    assert "onmouseover" not in out
    assert 'class="language-text"' in out


def test_render_markdown_linkifies_bare_urls() -> None:
    """``render_markdown`` converts a bare HTTPS URL into an ``<a>`` tag.

    ``linkify=True`` on the ``markdown-it-py`` constructor silently
    no-ops when ``linkify-it-py`` isn't installed. Pinning the test
    here makes the dependency contract failable: a future
    ``pyproject.toml`` edit that drops the ``[linkify]`` extra fails
    this assertion before CI gets to the integration smoke.
    """
    from meho_backplane.ui.routes.memory.render import render_markdown

    out = str(render_markdown("Visit https://example.com"))
    assert '<a href="https://example.com"' in out


def test_detail_strips_inline_html_from_body() -> None:
    """Raw ``<script>`` in a memory body renders as escaped text, not script."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="xss-probe",
        body='<script>alert("x")</script>',
        user_sub=_OP_A,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/xss-probe")
    finally:
        mock.stop()
    assert response.status_code == 200
    text = response.text
    # The body is escaped; no raw script tag survives inside the
    # body article. The DOM detail-page rendering can still embed
    # ``<script>`` tags elsewhere (HTMX, Alpine), so we narrow the
    # assertion to the escaped form.
    assert "&lt;script&gt;" in text


def test_detail_missing_slug_returns_404() -> None:
    """A non-existent slug returns 404 (info-leak-avoidance: no 403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/not-there")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_detail_cross_user_user_scoped_returns_404() -> None:
    """Operator B cannot see Operator A's user-scoped memory (404, not 403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="alice-secret",
        body="for-alice-only",
        user_sub=_OP_A,
    )
    _, jwks = _make_keypair_and_jwks()
    session_id_b = _seed_session_sync(
        tenant_id=_TENANT_A,  # same tenant
        access_token="unused",
        operator_sub=_OP_B,  # different operator
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id_b, jwks=jwks)
    try:
        response = client.get("/ui/memory/user/alice-secret")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_detail_cross_tenant_returns_404() -> None:
    """Operator in tenant B cannot see tenant A's tenant-scoped memory."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="a-runbook",
        body="for-a",
    )
    _, jwks = _make_keypair_and_jwks()
    session_id_b = _seed_session_sync(
        tenant_id=_TENANT_B,
        access_token="unused",
        operator_sub=_OP_A,  # same sub, different tenant
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id_b, jwks=jwks)
    try:
        response = client.get("/ui/memory/tenant/a-runbook")
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Edit-in-place -- RBAC gate on render + save
# ---------------------------------------------------------------------------


def test_edit_form_for_own_user_scoped_renders_textarea() -> None:
    """``GET /ui/memory/user/<slug>/edit`` renders a textarea for own user-scoped."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="riesling",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A,
        access_token=access_token,
        operator_sub=_OP_A,
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(
            "/ui/memory/user/wine-pref/edit",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "<textarea" in body
    assert 'name="body"' in body
    assert "riesling" in body
    # The form is the swap target (id=memory-body) so the cancel/save
    # roundtrip swaps in place.
    assert 'id="memory-body"' in body


def test_edit_form_tenant_scoped_as_operator_returns_403() -> None:
    """Operator cannot edit tenant-scoped without tenant_admin (RBAC=403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/tenant/team-rule/edit")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_edit_form_tenant_scoped_as_admin_renders_textarea() -> None:
    """``tenant_admin`` role unlocks the edit form on tenant-scoped memories."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory/tenant/team-rule/edit")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "<textarea" in response.text


def test_patch_save_persists_new_body_and_returns_rendered_view() -> None:
    """PATCH save updates the body in place and re-renders the body view fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="original",
        user_sub=_OP_A,
        tags=["wine"],
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    # Stub the embedding service: ``index_document``'s re-embed
    # branch will call ``encode_one`` because the body hash
    # changed; we mock it to return a fixed-dim vector so the test
    # doesn't depend on the embedding-model wheel.
    from unittest.mock import AsyncMock
    from unittest.mock import patch as _patch

    try:
        with _patch("meho_backplane.retrieval.indexer.get_embedding_service") as mock_embed_factory:
            mock_embed_factory.return_value = type(
                "_Stub", (), {"encode_one": AsyncMock(return_value=[0.0] * 384)}
            )()
            response = client.request(
                "PATCH",
                "/ui/memory/user/wine-pref",
                data={"body": "edited via UI"},
                headers=_csrf_headers(_csrf),
            )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The response is the body-view fragment with the new content
    # rendered through Markdown.
    assert "edited via UI" in body
    assert 'id="memory-body"' in body


def test_patch_save_tenant_scoped_as_operator_returns_403() -> None:
    """Operator role cannot PATCH a tenant-scoped memory (RBAC=403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "PATCH",
            "/ui/memory/tenant/team-rule",
            data={"body": "edited"},
            headers=_csrf_headers(_csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


def test_patch_save_empty_body_returns_422() -> None:
    """An empty body fails the 'must not be empty' guard."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="wine-pref",
        body="original",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "PATCH",
            "/ui/memory/user/wine-pref",
            data={"body": "   "},
            headers=_csrf_headers(_csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Delete -- happy path + idempotent + RBAC
# ---------------------------------------------------------------------------


def test_delete_removes_row_and_redirects_to_list() -> None:
    """DELETE returns 204 + HX-Redirect so HTMX navigates to the list.

    The detail page's confirm-delete button uses ``hx-target="body"
    hx-swap="outerHTML"`` -- a fragment-only re-render would destroy
    the chassis chrome. ``HX-Redirect`` makes HTMX issue a full client
    GET against ``/ui/memory`` instead, which rebuilds the page from
    the base template.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.USER,
        slug="ephemeral",
        body="goodbye",
        user_sub=_OP_A,
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "DELETE",
            "/ui/memory/user/ephemeral",
            headers=_csrf_headers(_csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/memory"
    assert response.text == ""
    # Follow the redirect and confirm the chassis chrome is intact
    # and the deleted row is absent from the rendered list.
    follow = client.get("/ui/memory")
    assert follow.status_code == 200, follow.text
    follow_body = follow.text
    assert "<html" in follow_body and "</html>" in follow_body
    assert 'id="memory-cards"' in follow_body
    assert "ephemeral" not in follow_body


def test_delete_tenant_scoped_as_operator_returns_403() -> None:
    """Operator role cannot DELETE a tenant-scoped memory."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_memory(
        tenant_id=_TENANT_A,
        scope=MemoryScope.TENANT,
        slug="team-rule",
        body="shared",
    )
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "DELETE",
            "/ui/memory/tenant/team-rule",
            headers=_csrf_headers(_csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_delete_missing_slug_returns_404() -> None:
    """DELETE on a non-existent slug returns 404 (info-leak-avoidance)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, access_token=access_token, operator_sub=_OP_A
    )
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.request(
            "DELETE",
            "/ui/memory/user/never-existed",
            headers=_csrf_headers(_csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stub retirement -- ensure ``/ui/memory`` is no longer a stub
# ---------------------------------------------------------------------------


def test_ui_memory_is_not_a_chassis_stub() -> None:
    """The real memory router replaces the stub."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/memory")
    finally:
        mock.stop()
    assert response.status_code == 200
    # The chassis stub renders "Coming soon" -- the real surface
    # never does.
    assert "Coming soon" not in response.text
