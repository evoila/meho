# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the KB UI read surface.

Initiative #339 (G10.2 Knowledge base UI), Task #870 (T1). Acceptance
criteria on issue #870:

* ``GET /ui/kb`` lists operator tenant's entries (paginated); empty state
  when none.
* Search returns ranked results from ``/api/v1/retrieve`` (source=kb);
  cards show fused + BM25 + cosine signals; debounced HTMX keyup.
* ``/ui/kb/<slug>`` renders Markdown server-side (headings/lists/code
  with syntax highlight/links); sidebar shows source/kind/indexed_at/
  body_hash.
* Hover-preview shows the matched snippet with query terms highlighted
  (server-side markup).
* ``operator`` role can read+search (RBAC); cross-tenant entries never
  returned.
* ``ruff`` + ``mypy`` clean; ``pytest -n auto backend/tests/test_ui_kb_search.py``
  passes.

Suite shape:

* :func:`_build_app` constructs a minimal FastAPI app wired the same way
  as :mod:`backend.tests.test_ui_topology_table._build_app`.
* :func:`_seed_kb_entry` inserts a kb entry bypassing the embedding
  pipeline (uses a patched ``get_embedding_service`` so no ONNX is
  needed).
* Test cases cover: auth boundary, full-page render, empty-state, HTMX
  fragment, entry detail, Markdown render, syntax highlight, sidebar
  metadata, hover-preview with term highlighting, cross-tenant isolation.

The suite uses SQLite (same as every other UI unit test). Real
BM25+cosine search ranking is exercised in
``tests/integration/test_kb_routes_pg.py``; here we mock
``KbService.search_entries`` to control the return shape so the tests
focus on the UI layer.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.kb.schemas import KbEntry, KbEntrySearchHit
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
    """Pin chassis + BFF env vars for every test.

    Mirrors the topology table test's autouse env fixture so the same
    Keycloak / Vault / DB / encryption-key baseline applies.
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
    """Minimal FastAPI app wired for KB UI tests.

    Mirrors the production wiring + chassis smoke test: StaticFiles at
    ``/ui/static``, BFF auth router, UI surface router (which now
    includes the KB routes ahead of the stubs),
    ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
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

    Patches out the embedding service (same pattern as test_kb_service.py)
    so no ONNX / fastembed is needed. Returns the created KbEntry.
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
            created_entry, _ = await service.create_entry(
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
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session cookie pre-set."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _csrf_token(session_id: uuid.UUID) -> str:
    """Return a valid CSRF token for *session_id* (for POST requests in tests)."""
    return mint_csrf_token(str(session_id))


def _make_search_hit(
    slug: str,
    body: str = "Snippet text.",
    fused: float = 0.5,
    bm25: float | None = 0.3,
    cosine: float | None = 0.8,
) -> KbEntrySearchHit:
    """Build a minimal :class:`KbEntrySearchHit` for mock returns."""
    return KbEntrySearchHit(
        slug=slug,
        snippet=body[:200],
        metadata={},
        fused_score=fused,
        bm25_score=bm25,
        cosine_score=cosine,
        bm25_rank=1,
        cosine_rank=1,
    )


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_kb_index_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/kb`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/kb")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_kb_detail_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/kb/<slug>`` without a session 302s to login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/kb/some-entry")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# GET /ui/kb -- full-page render with seeded entries
# ---------------------------------------------------------------------------


def test_kb_index_renders_seeded_entries() -> None:
    """``GET /ui/kb`` lists the operator tenant's entries in the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="vcenter-8.0-snapshots")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="linux-kernel-tuning")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Knowledge" in body
    assert "vcenter-8.0-snapshots" in body
    assert "linux-kernel-tuning" in body
    # Sidebar KB link points to /ui/kb (not /ui/knowledge).
    assert 'href="/ui/kb"' in body
    # CSRF cookie is set.
    assert CSRF_COOKIE_NAME in response.cookies


def test_kb_index_empty_state_when_no_entries() -> None:
    """Empty tenant KB renders the empty-state message."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb")

    assert response.status_code == 200, response.text
    assert "No knowledge entries yet" in response.text


# ---------------------------------------------------------------------------
# GET /ui/kb -- HTMX fragment request
# ---------------------------------------------------------------------------


def test_kb_index_htmx_returns_fragment_only() -> None:
    """``GET /ui/kb`` with ``HX-Request: true`` returns the fragment, not full page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="my-entry")

    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb", headers={"HX-Request": "true"})

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment has the results div but NOT the full-page shell.
    assert 'id="kb-results"' in body
    assert "my-entry" in body
    # Full-page chrome should not be present in the fragment.
    assert "<!doctype html>" not in body.lower()


# ---------------------------------------------------------------------------
# POST /ui/kb/search -- HTMX search partial
# ---------------------------------------------------------------------------


def test_kb_search_post_returns_hits() -> None:
    """``POST /ui/kb/search`` with a query returns ranked result cards."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    mock_hits = [
        _make_search_hit("vcenter-snapshots", fused=0.9, bm25=0.7, cosine=0.95),
        _make_search_hit("linux-perf", fused=0.5, bm25=None, cosine=0.6),
    ]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(
            "meho_backplane.ui.routes.kb.routes.KbService.search_entries",
            new_callable=AsyncMock,
            return_value=mock_hits,
        ):
            response = client.post(
                "/ui/kb/search",
                data={"q": "vcenter"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Both hits rendered.
    assert "vcenter-snapshots" in body
    assert "linux-perf" in body
    # Score pills.
    assert "fused" in body
    assert "0.900" in body
    # BM25 pill present for first hit only.
    assert "bm25" in body
    # Cosine pill present.
    assert "cos" in body


def test_kb_search_post_empty_query_returns_list() -> None:
    """``POST /ui/kb/search`` with empty query returns the entry list view."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="some-entry")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/search",
            data={"q": ""},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 200, response.text
    assert "some-entry" in response.text


def test_kb_search_returns_empty_state_for_no_hits() -> None:
    """Search with no results renders the "no entries matched" empty state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(
            "meho_backplane.ui.routes.kb.routes.KbService.search_entries",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.post(
                "/ui/kb/search",
                data={"q": "nonexistent"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    assert "No entries matched" in response.text
    assert "nonexistent" in response.text


# ---------------------------------------------------------------------------
# GET /ui/kb/<slug> -- entry detail
# ---------------------------------------------------------------------------


def test_kb_detail_renders_markdown() -> None:
    """``GET /ui/kb/<slug>`` renders the entry body as server-side HTML."""
    _seed_tenant(_TENANT_A, "tenant-a")
    md_body = (
        "# My Heading\n\n"
        "Some **bold** and *italic* text.\n\n"
        "```python\ndef greet(name: str) -> str:\n    return f'Hello {name}'\n```\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n"
    )
    _seed_kb_entry(tenant_id=_TENANT_A, slug="my-entry", body=md_body)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/my-entry")

    assert response.status_code == 200, response.text
    body = response.text
    # Markdown rendered to HTML.
    assert "<h1>My Heading</h1>" in body
    assert "<strong>bold</strong>" in body
    assert "<em>italic</em>" in body
    # Table rendered.
    assert "<table>" in body
    assert "<th>A</th>" in body
    # Pygments-highlighted code block (spans inside code).
    assert 'class="language-python"' in body
    assert "<span" in body
    # Pygments CSS injected inline.
    assert "kb-code" in body
    assert "<style>" in body


def test_kb_detail_shows_sidebar_metadata() -> None:
    """``/ui/kb/<slug>`` sidebar shows slug, kind, indexed_at, body_hash, source."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(
        tenant_id=_TENANT_A,
        slug="vcenter-8.0-guide",
        body="Guide content.",
        metadata={
            "path": "kb/vcenter-8.0-guide.md",
            "body_hash": "abcdef1234567890abcdef1234567890",
        },
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/vcenter-8.0-guide")

    assert response.status_code == 200, response.text
    body = response.text
    # Sidebar metadata fields.
    assert "vcenter-8.0-guide" in body
    assert "kb/vcenter-8.0-guide.md" in body
    # body_hash truncated to 12 chars.
    assert "abcdef123456" in body


def test_kb_detail_404_for_missing_slug() -> None:
    """``GET /ui/kb/<unknown-slug>`` returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/does-not-exist")

    assert response.status_code == 404


def test_kb_detail_strikethrough_rendered() -> None:
    """GFM strikethrough ``~~text~~`` renders as ``<s>text</s>`` (markdown-it-py convention)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="strike-test", body="~~deprecated~~")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/strike-test")

    assert response.status_code == 200, response.text
    assert "<s>deprecated</s>" in response.text


# ---------------------------------------------------------------------------
# GET /ui/kb/<slug>/preview -- hover preview partial
# ---------------------------------------------------------------------------


def test_kb_preview_returns_snippet() -> None:
    """``GET /ui/kb/<slug>/preview`` returns the _preview.html fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(
        tenant_id=_TENANT_A,
        slug="preview-entry",
        body="This is the sample content for the entry.",
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/preview-entry/preview?q=sample")

    assert response.status_code == 200, response.text
    body = response.text
    # Snippet text is in the preview.
    assert "This is the" in body
    assert "content for the entry" in body
    # Query terms highlighted with <mark>.
    assert "<mark" in body
    assert "sample" in body
    # View full entry link present.
    assert "View full entry" in body
    assert "/ui/kb/preview-entry" in body


def test_kb_preview_highlights_query_terms() -> None:
    """Query terms in the preview snippet are wrapped in ``<mark class="kb-term">``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_kb_entry(
        tenant_id=_TENANT_A,
        slug="highlight-test",
        body="The quick brown fox jumps over the lazy dog.",
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/highlight-test/preview?q=fox+dog")

    assert response.status_code == 200, response.text
    body = response.text
    # Both terms highlighted.
    assert 'class="kb-term"' in body
    assert "fox" in body
    assert "dog" in body


def test_kb_preview_404_for_missing_slug() -> None:
    """``GET /ui/kb/<unknown>/preview`` returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/does-not-exist/preview")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


def test_cross_tenant_entry_not_visible() -> None:
    """Tenant B's entry does not appear in Tenant A's KB list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_kb_entry(tenant_id=_TENANT_A, slug="tenant-a-entry")
    _seed_kb_entry(tenant_id=_TENANT_B, slug="tenant-b-entry")

    # Authenticate as Tenant A.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb")

    assert response.status_code == 200, response.text
    body = response.text
    assert "tenant-a-entry" in body
    assert "tenant-b-entry" not in body


def test_cross_tenant_detail_returns_404() -> None:
    """Accessing Tenant B's slug as Tenant A yields 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_kb_entry(tenant_id=_TENANT_B, slug="secret-entry")

    # Authenticate as Tenant A.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/kb/secret-entry")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# B1 — render.py _highlight_code: lang attribute escaping (XSS fix)
# ---------------------------------------------------------------------------


def test_highlight_code_escapes_lang_attribute() -> None:
    """``_highlight_code`` HTML-escapes *lang* before injecting into class attr."""
    from meho_backplane.ui.routes.kb.render import _highlight_code

    malicious_lang = '"><script>alert(1)</script><span class="'
    html = _highlight_code("x = 1", malicious_lang, "")
    # The raw script tag must not appear unescaped in the output.
    assert "<script>" not in html
    # The lang is attribute-escaped; the value should contain &quot; or &gt;
    assert "&quot;" in html or "&#34;" in html or "&gt;" in html


# ---------------------------------------------------------------------------
# B2 — routes.py POST /ui/kb/search: empty-query pagination (has_more)
# ---------------------------------------------------------------------------


def test_kb_search_post_empty_query_has_more_computed() -> None:
    """POST /ui/kb/search with empty query and >_DEFAULT_PAGE_LIMIT entries
    returns has_more=True and trims entries to the page limit.

    Seeds _DEFAULT_PAGE_LIMIT + 1 fake entries via mocked list_entries so
    the handler's +1-fetch / trim / has_more logic is exercised.
    """
    from datetime import datetime

    from meho_backplane.kb.schemas import KbEntry as _KbEntry
    from meho_backplane.ui.routes.kb.routes import _DEFAULT_PAGE_LIMIT

    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    _now = datetime(2026, 1, 1, tzinfo=UTC)
    fake_entries = [
        _KbEntry(
            id=uuid.UUID(int=i + 1),
            slug=f"entry-{i:03d}",
            body="body",
            metadata={},
            tenant_id=_TENANT_A,
            created_at=_now,
            updated_at=_now,
        )
        for i in range(_DEFAULT_PAGE_LIMIT + 1)
    ]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(
            "meho_backplane.ui.routes.kb.routes.KbService.list_entries",
            new_callable=AsyncMock,
            return_value=fake_entries,
        ):
            response = client.post(
                "/ui/kb/search",
                data={"q": ""},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # Pagination "Next" control should appear since has_more=True.
    assert "Next" in response.text or "offset=" in response.text


# ---------------------------------------------------------------------------
# M1 — routes.py POST /ui/kb/search: max_length guard on q
# ---------------------------------------------------------------------------


def test_kb_search_post_query_too_long_returns_422() -> None:
    """POST /ui/kb/search with q exceeding _MAX_QUERY_LENGTH returns 422."""
    from meho_backplane.ui.routes.kb.routes import _MAX_QUERY_LENGTH

    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    oversized_query = "a" * (_MAX_QUERY_LENGTH + 1)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post(
            "/ui/kb/search",
            data={"q": oversized_query},
            headers={CSRF_HEADER_NAME: csrf},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# M2 — routes.py _highlight_query_terms: single-pass (no double-mark)
# ---------------------------------------------------------------------------


def test_highlight_query_terms_no_double_mark() -> None:
    """``_highlight_query_terms`` single alternating-union pass never nests
    ``<mark>`` tags even when a term appears within the markup itself.
    """
    from meho_backplane.ui.routes.kb.routes import _highlight_query_terms

    snippet = "The mark tag wraps a word."
    result = _highlight_query_terms(snippet, "mark word")
    result_str = str(result)
    assert "<mark" in result_str
    # Opening and closing tag counts must be equal (no nesting).
    assert result_str.count("<mark") == result_str.count("</mark>")


# ---------------------------------------------------------------------------
# Blocker — render.py: raw HTML in the entry body is escaped, not rendered
# ---------------------------------------------------------------------------


def test_render_markdown_escapes_raw_html_body() -> None:
    """``render_markdown`` must escape raw HTML in the kb body (html=False).

    The ``commonmark`` preset enables ``html`` (CommonMark permits raw HTML
    passthrough); ``_build_md`` overrides it to ``html=False``. A body
    carrying ``<script>`` / ``<img onerror>`` must render as inert escaped
    text, never as live tags — otherwise a stored kb entry is a stored-XSS
    vector on the operator console.
    """
    from meho_backplane.ui.routes.kb.render import render_markdown

    rendered = str(
        render_markdown(
            "Intro\n\n<script>alert(document.cookie)</script>\n\n"
            '<img src=x onerror="alert(1)"> and <a href="javascript:alert(1)">x</a>'
        )
    )
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "onerror" not in rendered or "&lt;img" in rendered
    # The payload survives as escaped text so the content is still visible.
    assert "&lt;script&gt;" in rendered
    # Legitimate Markdown still renders (sanity: the escape didn't nuke output).
    assert "<p>" in rendered


def test_highlight_query_terms_prefers_longest_term() -> None:
    """Overlapping terms: the longest alternative wins (``python`` over ``py``).

    Leftmost-alternation would mark only ``py`` inside ``python``; terms are
    deduped + sorted by descending length so the full word is highlighted.
    """
    from meho_backplane.ui.routes.kb.routes import _highlight_query_terms

    result = str(_highlight_query_terms("learn python and py basics", "py python"))
    assert '<mark class="kb-term">python</mark>' in result
    # The standalone ``py`` token is also marked, but ``python`` is not
    # left as a bare ``py``+``thon`` fragment.
    assert "py</mark>thon" not in result


def test_highlight_query_terms_marks_special_char_term() -> None:
    """A query term containing HTML metacharacters still highlights.

    The snippet is escaped before matching, so the alternation branches are
    built from escaped terms — a term like ``<b>`` matches ``&lt;b&gt;`` in
    the escaped snippet and is wrapped without double-encoding.
    """
    from meho_backplane.ui.routes.kb.routes import _highlight_query_terms

    result = str(_highlight_query_terms("the <b> tag is bold", "<b>"))
    assert '<mark class="kb-term">&lt;b&gt;</mark>' in result
    # No double-encoding of the ampersand entity.
    assert "&amp;lt;" not in result
