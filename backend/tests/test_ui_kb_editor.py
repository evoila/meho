# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the KB UI editor modal + mobile reflow.

Initiative #339 (G10.2 Knowledge base UI), Task #872 (T3). Acceptance
criteria on issue #872:

* The editor modal template renders with CodeMirror 6 wiring and live
  server-rendered preview via ``POST /ui/kb/editor-preview``.
* ``POST /ui/kb/new`` saves to the KB service; the new entry appears in the
  list; ``tenant_admin`` RBAC enforced (operator role → 403).
* Entry-view Markdown reflows at narrow widths — asserted via a
  render/snapshot check that the mobile-reflow CSS rules are present in
  the detail page.
* Cross-tenant isolation: preview and save are tenant-scoped.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto backend/tests/test_ui_kb_editor.py``
  passes.

Suite shape:

* :func:`_build_app` is the same minimal FastAPI wiring as
  :mod:`tests.test_ui_kb_search._build_app` (shared helper via local
  import to avoid circular deps).
* :func:`_seed_session_sync` creates a session with the fake access token
  used across all UI unit tests.
* For the ``POST /ui/kb/new`` RBAC gate, ``_require_tenant_admin`` calls
  :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`; tests patch it
  so the fake "access-token-plaintext" session token does not trip the real
  JWKS chain.
* The preview endpoint (``POST /ui/kb/editor-preview``) is read-only; no
  mocking needed beyond the standard session setup.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.kb.schemas import KbEntry
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test in this suite.

    Same baseline as :mod:`tests.test_ui_kb_search._bff_env`.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for KB UI editor tests.

    Mirrors the production wiring: StaticFiles at ``/ui/static``, BFF
    auth router, UI surface router (which includes the KB routes ahead of
    the stubs), ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
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
    """Insert one ``tenant`` row so Document FK constraints resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_kb_entry(
    *,
    tenant_id: uuid.UUID,
    slug: str,
    body: str = "# Hello\n\nWorld.",
    metadata: dict[str, object] | None = None,
) -> KbEntry:
    """Insert a kb entry via the KbService and return the KbEntry.

    Patches out the embedding service so no ONNX / fastembed is needed.
    """
    from meho_backplane.kb.service import KbService

    fake_service = AsyncMock()
    fake_service.encode_one.return_value = [0.1] * 384
    fake_service.encode.return_value = [[0.1] * 384]
    fake_service.dimension = 384

    created_entry: KbEntry | None = None

    async def _do() -> None:
        nonlocal created_entry
        with patch(
            "meho_backplane.retrieval.indexer.get_embedding_service",
            return_value=fake_service,
        ):
            service = KbService()
            created_entry = await service.create_entry(
                tenant_id,
                slug,
                body,
                metadata=metadata or {},
            )

    asyncio.run(_do())
    assert created_entry is not None
    return created_entry


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str = "op-42",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row and return its UUID (bypasses OAuth)."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return uuid.UUID(str(decrypted.id))

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _csrf_token(session_id: uuid.UUID) -> str:
    """Return a valid CSRF token for *session_id*."""
    return str(mint_csrf_token(str(session_id)))


def _make_fake_admin_operator(tenant_id: uuid.UUID) -> Operator:
    """Return a fake :class:`Operator` with ``TENANT_ADMIN`` role."""
    return MagicMock(
        spec=Operator,
        sub="op-admin",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


def _make_fake_operator_operator(tenant_id: uuid.UUID) -> Operator:
    """Return a fake :class:`Operator` with plain ``OPERATOR`` role."""
    return MagicMock(
        spec=Operator,
        sub="op-regular",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# KB index renders "New entry" button
# ---------------------------------------------------------------------------


def test_kb_index_renders_new_entry_button() -> None:
    """``GET /ui/kb`` includes a "New entry" button that opens the editor modal."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb")
    assert response.status_code == 200, response.text
    body = response.text
    assert "New entry" in body
    assert "kb-editor-modal" in body
    # CodeMirror bundle script tag present.
    assert "codemirror-bundle.min.js" in body


# ---------------------------------------------------------------------------
# POST /ui/kb/editor-preview — live preview partial
# ---------------------------------------------------------------------------


def test_editor_preview_renders_markdown() -> None:
    """``POST /ui/kb/editor-preview`` renders Markdown body to HTML."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    md_body = "# My Heading\n\nSome **bold** text.\n\n```python\ndef foo(): pass\n```\n"

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/editor-preview",
            data={"body": md_body},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 200, response.text
    html = response.text
    # Heading rendered as <h1>.
    assert "<h1>" in html
    assert "My Heading" in html
    # Bold text rendered as <strong>.
    assert "<strong>bold</strong>" in html
    # Code block rendered with pygments class.
    assert "kb-code" in html
    # Fragment does not include the base-shell chrome.
    assert "<!doctype html>" not in html.lower()


def test_editor_preview_empty_body_renders_safely() -> None:
    """Empty body preview returns an empty-but-valid HTML fragment (no error)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/editor-preview",
            data={"body": ""},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 200, response.text


def test_editor_preview_requires_session() -> None:
    """``POST /ui/kb/editor-preview`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        csrf = "x" * 64
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/editor-preview",
            data={"body": "hello"},
            headers={CSRF_HEADER_NAME: csrf},
        )
    assert response.status_code == 302
    assert "/ui/auth/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# POST /ui/kb/new — editor save (tenant_admin required)
# ---------------------------------------------------------------------------


def test_editor_save_creates_entry_and_redirects() -> None:
    """``POST /ui/kb/new`` with tenant_admin role creates the entry and returns HX-Redirect."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    fake_entry = MagicMock(spec=KbEntry)
    fake_entry.slug = "new-entry-slug"

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(
                "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
                new_callable=AsyncMock,
                return_value=_make_fake_admin_operator(_TENANT_A),
            ),
            patch(
                "meho_backplane.ui.routes.kb.routes.KbService.create_entry",
                new_callable=AsyncMock,
                return_value=fake_entry,
            ),
        ):
            response = client.post(
                "/ui/kb/new",
                data={"slug": "new-entry-slug", "body": "# Hello", "tags": "ops"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/kb/new-entry-slug"


def test_editor_save_operator_role_returns_403() -> None:
    """``POST /ui/kb/new`` with plain operator role returns 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(
            "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
            new_callable=AsyncMock,
            return_value=_make_fake_operator_operator(_TENANT_A),
        ):
            response = client.post(
                "/ui/kb/new",
                data={"slug": "some-slug", "body": "body text", "tags": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 403, response.text


def test_editor_save_invalid_slug_returns_422_with_error() -> None:
    """``POST /ui/kb/new`` with an invalid slug re-renders the modal with an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    from meho_backplane.kb.schemas import InvalidKbSlugError

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(
                "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
                new_callable=AsyncMock,
                return_value=_make_fake_admin_operator(_TENANT_A),
            ),
            patch(
                "meho_backplane.ui.routes.kb.routes.KbService.create_entry",
                new_callable=AsyncMock,
                side_effect=InvalidKbSlugError("invalid slug: 'BAD SLUG'"),
            ),
        ):
            response = client.post(
                "/ui/kb/new",
                data={"slug": "BAD SLUG", "body": "body text", "tags": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 422, response.text
    assert "BAD SLUG" in response.text or "invalid slug" in response.text.lower()
    # Re-renders the editor modal (not full page).
    assert "kb-editor-modal" in response.text
    assert "<!doctype html>" not in response.text.lower()


def test_editor_save_requires_session() -> None:
    """``POST /ui/kb/new`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        csrf = "x" * 64
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/new",
            data={"slug": "some-slug", "body": "body", "tags": ""},
            headers={CSRF_HEADER_NAME: csrf},
        )
    assert response.status_code == 302
    assert "/ui/auth/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_editor_save_is_tenant_scoped() -> None:
    """Save uses the session's tenant, not a user-supplied value."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id_a = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id_a)

    captured_tenant: list[uuid.UUID] = []

    async def _capture_create(tenant_id: object, slug: str, body: str, **_: object) -> KbEntry:
        captured_tenant.append(tenant_id)  # type: ignore[arg-type]
        entry = MagicMock(spec=KbEntry)
        entry.slug = slug
        return entry

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id_a)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(
                "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
                new_callable=AsyncMock,
                return_value=_make_fake_admin_operator(_TENANT_A),
            ),
            patch(
                "meho_backplane.ui.routes.kb.routes.KbService.create_entry",
                side_effect=_capture_create,
            ),
        ):
            response = client.post(
                "/ui/kb/new",
                data={"slug": "cross-tenant-test", "body": "body", "tags": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 204, response.text
    assert len(captured_tenant) == 1
    # Tenant comes from the session (tenant A), not user input.
    assert captured_tenant[0] == _TENANT_A


# ---------------------------------------------------------------------------
# Mobile reflow CSS — detail page
# ---------------------------------------------------------------------------


def test_detail_page_has_mobile_reflow_css() -> None:
    """Entry detail page includes the mobile-reflow CSS rules for kb-body."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(
        tenant_id=_TENANT_A,
        slug="mobile-test-entry",
        body=(
            "# Long heading that might overflow\n\n"
            "A paragraph with a very long URL: https://example.com/very/long/path/that/does/not/break\n\n"
            "```python\n"
            "x = 'a' * 200  # very long line that exercises horizontal scroll\n"
            "```\n\n"
            "| Col A | Col B | Col C |\n|-------|-------|-------|\n| val 1 | val 2 | val 3 |\n"
        ),
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/mobile-test-entry")

    assert response.status_code == 200, response.text
    html = response.text
    # Mobile reflow rules present in the injected <style> block.
    assert "overflow-wrap: break-word" in html
    assert "word-break: break-word" in html
    # Code blocks scroll horizontally inside their own box.
    assert "overflow-x: auto" in html
    # Table overflow handling.
    assert "display: block" in html


def test_detail_page_kb_body_class_present() -> None:
    """The Markdown prose container carries the ``kb-body`` CSS hook."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="reflow-check")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/reflow-check")

    assert response.status_code == 200, response.text
    assert 'class="prose prose-sm max-w-none kb-body"' in response.text


# ---------------------------------------------------------------------------
# B1 — CSRF cookie refreshed on 422 re-render (fix verification)
# ---------------------------------------------------------------------------


def test_editor_save_422_sets_fresh_csrf_cookie() -> None:
    """422 re-render must set a fresh CSRF cookie so subsequent POSTs succeed.

    Without the fix, ``kb_editor_save`` minted a fresh token for the
    template context but never called ``response.set_cookie``.  The browser
    continued to send the *old* cookie value, which no longer matched the
    new token embedded in the re-rendered modal's ``hx-headers``, causing
    every follow-up HTMX POST to receive 403 from CSRFMiddleware.

    This test asserts:
    1. The 422 response carries a ``Set-Cookie`` header for ``csrf_token``.
    2. The cookie value is a non-empty string (a freshly minted token).
    3. A subsequent ``POST /ui/kb/editor-preview`` using the new token
       succeeds (200), proving the refreshed cookie round-trips correctly.
    """
    from meho_backplane.kb.schemas import InvalidKbSlugError

    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)

        # --- Step 1: trigger a 422 ---
        with (
            patch(
                "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
                new_callable=AsyncMock,
                return_value=_make_fake_admin_operator(_TENANT_A),
            ),
            patch(
                "meho_backplane.ui.routes.kb.routes.KbService.create_entry",
                new_callable=AsyncMock,
                side_effect=InvalidKbSlugError("invalid slug: 'BAD'"),
            ),
        ):
            r422 = client.post(
                "/ui/kb/new",
                data={"slug": "BAD", "body": "body text", "tags": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert r422.status_code == 422, r422.text

    # The response must set a fresh CSRF cookie.
    new_csrf = r422.cookies.get(CSRF_COOKIE_NAME)
    assert new_csrf is not None, "422 response did not set a fresh CSRF cookie"
    assert len(new_csrf) > 0

    # --- Step 2: use the new token for a follow-up preview POST ---
    with respx.mock(assert_all_called=False):
        client2 = _authenticated_client(session_id)
        client2.cookies.set(CSRF_COOKIE_NAME, new_csrf)
        r_preview = client2.post(
            "/ui/kb/editor-preview",
            data={"body": "## Hello"},
            headers={CSRF_HEADER_NAME: new_csrf},
        )

    assert r_preview.status_code == 200, r_preview.text


# ---------------------------------------------------------------------------
# B2 — Re-rendered modal contains editor anchor and fresh CSRF token
# ---------------------------------------------------------------------------


def test_editor_save_422_rerenders_modal_with_editor_anchor_and_csrf() -> None:
    """422 fragment must contain ``#kb-editor-cm`` and a non-empty CSRF token.

    This verifies the server side of the B2 fix: after a failed save the
    re-rendered ``_editor_modal.html`` fragment must include the CodeMirror
    mount point (``id="kb-editor-cm"``) and a non-empty ``csrf_token`` in
    the ``hx-headers`` attributes so the JS ``htmx:afterSwap`` handler can
    destroy the stale view and mount a fresh one.
    """
    from meho_backplane.kb.schemas import InvalidKbSlugError

    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(
                "meho_backplane.ui.routes.kb.routes.verify_jwt_for_audience",
                new_callable=AsyncMock,
                return_value=_make_fake_admin_operator(_TENANT_A),
            ),
            patch(
                "meho_backplane.ui.routes.kb.routes.KbService.create_entry",
                new_callable=AsyncMock,
                side_effect=InvalidKbSlugError("invalid slug: 'BAD'"),
            ),
        ):
            response = client.post(
                "/ui/kb/new",
                data={"slug": "BAD", "body": "some body", "tags": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 422, response.text
    html = response.text

    # Modal root element present (HTMX outerHTML swap target).
    assert 'id="kb-editor-modal"' in html
    # CodeMirror mount point present — JS needs this to re-mount the editor.
    assert 'id="kb-editor-cm"' in html
    # A non-placeholder CSRF token is embedded in the hx-headers attributes.
    # The token is rendered by Jinja2 into the two hx-headers strings;
    # it must be non-empty so the re-mounted editor can POST successfully.
    new_csrf_cookie = response.cookies.get(CSRF_COOKIE_NAME)
    assert new_csrf_cookie, "422 fragment must carry a fresh CSRF cookie"
    assert new_csrf_cookie in html, (
        "Fresh CSRF token must appear inside the re-rendered modal fragment"
    )
