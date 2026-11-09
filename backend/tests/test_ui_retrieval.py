# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the retrieval-diagnostics UI surface.

Initiative #1840 (G10.14 Retrieval diagnostics & quality console), Task #1888.
Acceptance criteria on issue #1888:

* ``GET /ui/retrieval`` returns 200 for an operator session and renders the
  Diagnostics tab active with an empty results region; an anonymous session is
  redirected to login via ``require_ui_session``.
* ``POST /ui/retrieval/diagnostics`` with a query swaps a fragment containing,
  per hit, the ``fused_score`` and a per-signal RRF score/rank breakdown where
  a ``None`` ``bm25_rank`` / ``cosine_rank`` renders an explicit "absent"
  marker, not a blank.
* The diagnostics handler binds ``audit_query_hash`` (SHA-256 of the raw query,
  never the raw query), ``audit_source``, ``audit_kind``, ``audit_hit_count``
  before/after the ``retrieve`` call -- the raw query is absent from the bound
  audit payload.
* CSRF double-submit is enforced on ``POST /ui/retrieval/diagnostics``: a
  request with no/invalid ``meho_csrf`` cookie+header pair is rejected; the
  fragment reuses the live cookie token (cookie untouched).

Suite shape mirrors ``test_ui_corpus.py``: ``_build_app`` wires StaticFiles,
the BFF auth router, the UI surface router (retrieval ahead of stubs),
``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next. The operator-
reconstruction seam (``_resolve_operator``) is patched to return a constructed
:class:`Operator` so the tests do not need a live Keycloak / JWKS round-trip;
the in-process ``retrieve`` is mocked at the route's import so the tests focus
on the UI layer (the substrate is exercised in the retriever's own tests).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Document, Tenant
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.retrieval.usage import UsageReport
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

from ._ui_role_helpers import allow_ui_operator_role


@pytest.fixture(autouse=True)
def _ui_operator_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console pages under test sit behind the ``operator`` role check."""
    allow_ui_operator_role(monkeypatch)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

#: The route-module symbol patched so the handlers get an operator without a
#: live JWKS round-trip.
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.retrieval.routes._resolve_operator"

#: The route-module symbol the diagnostics handler calls; mocked to control the
#: returned hit list / raised failure without a live corpus + embedder.
_RETRIEVE = "meho_backplane.ui.routes.retrieval.routes.retrieve"

#: The route-module symbol the Usage tab calls; mocked to return a
#: constructed :class:`UsageReport` without a live audit_log scan (#1889).
_COMPUTE_USAGE = "meho_backplane.ui.routes.retrieval.routes.compute_usage"

_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

#: The contextvars binder the handler calls; patched to capture the audit
#: payload without a live structlog pipeline.
_BIND_CONTEXTVARS = (
    "meho_backplane.ui.routes.retrieval.routes.structlog.contextvars.bind_contextvars"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the corpus UI suite)."""
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
    """Minimal FastAPI app wired for retrieval UI tests (mirrors corpus)."""
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
    """Insert one ``tenant`` row so the session FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_document(
    *,
    tenant_id: uuid.UUID,
    source: str,
    kind: str,
    source_id: str,
    body: str = "seeded document body",
    user_sub: str | None = None,
) -> None:
    """Insert one ``documents`` row so the datalist DISTINCT query sees it (#2458).

    Seeds the substrate directly (no embedding service) with a fixed zero
    vector; the facet query reads only ``source`` / ``kind`` / ``tenant_id`` /
    ``metadata``. A ``user_sub`` populates ``metadata['user_sub']`` so the
    per-principal memory-visibility predicate can be exercised.
    """
    metadata: dict[str, object] = {}
    if user_sub is not None:
        metadata["user_sub"] = user_sub
    doc_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                Document(
                    id=doc_id,
                    tenant_id=tenant_id,
                    source=source,
                    source_id=source_id,
                    kind=kind,
                    body=body,
                    body_hash=f"sha256:test:{doc_id}",
                    embedding=[0.0] * 384,
                    doc_metadata=metadata,
                    tokens=len(body.split()),
                ),
            )

    asyncio.run(_do())


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
    """Return a valid CSRF token for *session_id*."""
    return mint_csrf_token(str(session_id))


def _operator(
    *,
    tenant_id: uuid.UUID,
    sub: str = "op-42",
) -> Operator:
    """Build an :class:`Operator` the patched ``_resolve_operator`` returns."""
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset(),
        platform_admin=False,
    )


def _hit(
    *,
    source: str = "kb",
    source_id: str = "kb-entry-1",
    kind: str = "kb-entry",
    body: str = "Snapshots quiesce the guest before capture.",
    fused_score: float = 0.0327,
    bm25_score: float | None = 1.234,
    cosine_score: float | None = 0.876,
    bm25_rank: int | None = 1,
    cosine_rank: int | None = 2,
) -> RetrievalHit:
    """Build a :class:`RetrievalHit` for mocked retrieve returns."""
    now = datetime(2026, 6, 19, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=_TENANT_A,
        source=source,
        source_id=source_id,
        kind=kind,
        body=body,
        doc_metadata={},
        created_at=now,
        updated_at=now,
        fused_score=fused_score,
        bm25_score=bm25_score,
        cosine_score=cosine_score,
        bm25_rank=bm25_rank,
        cosine_rank=cosine_rank,
    )


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_retrieval_index_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/retrieval`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/retrieval")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_retrieval_diagnostics_unauthenticated_rejected() -> None:
    """``POST /ui/retrieval/diagnostics`` without a session never reaches the handler."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post("/ui/retrieval/diagnostics", data={"query": "x"})
    assert response.status_code in (302, 403)


# ---------------------------------------------------------------------------
# GET /ui/retrieval -- page render
# ---------------------------------------------------------------------------


def test_retrieval_index_renders_diagnostics_tab_active_and_empty_results() -> None:
    """``GET /ui/retrieval`` renders the Diagnostics tab active + an empty results region."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Retrieval" in body
    # Tab strip present with both tabs; Diagnostics is the default-active panel.
    assert "Diagnostics" in body
    assert "Usage Analytics" in body
    assert "tab: 'diagnostics'" in body
    # Empty results region present, no hits rendered yet.
    assert 'id="retrieval-diagnostics-results"' in body
    assert "Enter a query to run retrieval" in body
    # Sidebar nav link.
    assert 'href="/ui/retrieval"' in body
    # CSRF cookie set + form carries the token.
    assert CSRF_COOKIE_NAME in response.cookies
    assert "X-CSRF-Token" in body
    assert 'hx-post="/ui/retrieval/diagnostics"' in body


def test_run_buttons_carry_in_flight_spinner_and_disable_attrs() -> None:
    """Each multi-second Run form wires hx-indicator + hx-disabled-elt (#2459).

    The Diagnostics form declares ``hx-indicator`` (pointing at an
    ``htmx-indicator`` spinner span) and ``hx-disabled-elt`` targeting its
    own submit button, plus an ``hx-disinherit`` guard so neither attribute
    leaks onto a descendant htmx request in the swapped results region (#2340).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text

    # Each Run form: indicator target + submit-button disable + disinherit guard.
    spinner_id = "retrieval-diagnostics-spinner"
    assert f'hx-indicator="#{spinner_id}"' in body
    # The spinner span exists and carries the htmx-indicator class.
    assert f'id="{spinner_id}"' in body
    assert body.count('hx-disabled-elt="find button[type=submit]"') >= 1
    assert body.count('hx-disinherit="hx-disabled-elt hx-indicator"') >= 1
    assert body.count("htmx-indicator loading loading-spinner") >= 1


# ---------------------------------------------------------------------------
# GET /ui/retrieval -- Source / Kind datalists (#2458)
# ---------------------------------------------------------------------------


def test_index_source_kind_datalists_enumerate_distinct_visible_values() -> None:
    """The Source / Kind inputs are backed by datalists of distinct visible values.

    A ``SELECT DISTINCT source, kind`` over the tenant's retrieval-visible
    ``documents`` rows populates the two ``<datalist>`` elements. Duplicate
    (source, kind) rows collapse to one option, and the Source help text notes
    docs-corpus collections are searched elsewhere.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub="op-42")
    # Two kb rows (distinct source_id) collapse to a single kb / kb-entry option.
    _seed_document(tenant_id=_TENANT_A, source="kb", kind="kb-entry", source_id="kb-1")
    _seed_document(tenant_id=_TENANT_A, source="kb", kind="kb-entry", source_id="kb-2")
    # A user-scoped memory row owned by this principal is visible to them.
    _seed_document(
        tenant_id=_TENANT_A,
        source="memory",
        kind="memory-user",
        source_id="mem-1",
        user_sub="op-42",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    # Inputs are bound to their datalists.
    assert 'list="retrieval-source-options"' in body
    assert 'list="retrieval-kind-options"' in body
    assert 'id="retrieval-source-options"' in body
    assert 'id="retrieval-kind-options"' in body
    # Distinct sources + kinds are offered as options.
    assert '<option value="kb"></option>' in body
    assert '<option value="memory"></option>' in body
    assert '<option value="kb-entry"></option>' in body
    assert '<option value="memory-user"></option>' in body
    # DISTINCT collapses the two kb rows to one source option.
    assert body.count('<option value="kb"></option>') == 1
    # Source help text steers docs-corpus lookups to /ui/corpus, not here.
    assert "/ui/corpus" in body
    assert "Docs-corpus collections are a separate" in body


def test_index_datalists_are_tenant_scoped() -> None:
    """Values seeded under a different tenant never appear in the datalists.

    The DISTINCT query is tenant-scoped, so another tenant's source/kind values
    cannot leak into this operator's suggestions.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub="op-42")
    _seed_document(tenant_id=_TENANT_A, source="kb", kind="kb-entry", source_id="a-1")
    # Foreign-tenant rows with unmistakable source/kind values.
    _seed_document(
        tenant_id=_TENANT_B,
        source="tenant-b-secret-source",
        kind="tenant-b-secret-kind",
        source_id="b-1",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    assert '<option value="kb"></option>' in body
    # No cross-tenant leak.
    assert "tenant-b-secret-source" not in body
    assert "tenant-b-secret-kind" not in body


def test_index_datalists_exclude_other_principals_user_scoped_memory() -> None:
    """A user-scoped memory row another principal owns is not offered (#1797 gate).

    The datalists enumerate *retrieval-visible* rows, so a ``source='memory'``
    user-scoped ``kind`` owned by a different ``user_sub`` is excluded -- the
    operator would get 0 hits typing it, so it must not be suggested.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub="op-42")
    _seed_document(tenant_id=_TENANT_A, source="kb", kind="kb-entry", source_id="kb-1")
    # A user-scoped memory row owned by a DIFFERENT principal -- not visible to op-42.
    _seed_document(
        tenant_id=_TENANT_A,
        source="memory",
        kind="memory-user",
        source_id="mem-intruder",
        user_sub="someone-else",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    assert '<option value="kb"></option>' in body
    # The other principal's user-scoped memory row does not leak into either list.
    assert '<option value="memory"></option>' not in body
    assert '<option value="memory-user"></option>' not in body


# ---------------------------------------------------------------------------
# POST /ui/retrieval/diagnostics -- successful fragment + per-signal breakdown
# ---------------------------------------------------------------------------


def test_diagnostics_renders_fused_score_and_per_signal_breakdown() -> None:
    """A successful run swaps a fragment with the fused_score + per-signal table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    hits = [_hit(fused_score=0.0327, bm25_score=1.234, cosine_score=0.876)]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=hits),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "snapshot quiesce", "source": "", "kind": "", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="retrieval-diagnostics-results"' in body
    assert "<!doctype html>" not in body.lower()
    # Body excerpt + provenance.
    assert "Snapshots quiesce the guest" in body
    assert "kb-entry-1" in body
    # Fused score rendered.
    assert "0.032700" in body
    # Per-signal breakdown: both signals present with ranks.
    assert "BM25" in body
    assert "Cosine" in body
    assert "#1" in body
    assert "#2" in body
    assert "1 hit" in body


def test_diagnostics_clamps_hit_body_with_expand_toggle() -> None:
    """Each hit body is clamped to a snippet with an Alpine expand-on-click toggle (#2456).

    Acceptance criterion: ranked hits render as comparable snippet cards so ten
    hits fit one screen; each card exposes an expand affordance revealing the
    full body. The clamp is a static ``line-clamp-3`` (so it holds with JS
    disabled) that the ``expanded`` binding removes on click.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    hits = [_hit(source_id=f"kb-entry-{i}") for i in range(10)]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=hits),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "snapshot quiesce", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # One clamped snippet + one expand toggle per hit (all ten).
    assert body.count("whitespace-pre-wrap line-clamp-3") == 10
    assert body.count('x-data="{ expanded: false, clamped: false }"') == 10
    assert body.count("expanded = !expanded") == 10
    # The clamp is removed on expand, and the toggle labels both directions.
    assert "{ 'line-clamp-3': !expanded }" in body
    assert "Show more" in body
    assert "Show less" in body


def test_diagnostics_renders_absent_marker_for_none_rank() -> None:
    """A hit absent from a signal's top-N renders an explicit 'absent' marker, not a blank.

    Acceptance criterion: a ``cosine_score=None, cosine_rank=None`` hit must
    render the "absent from this signal's top-50" marker (the document never
    appeared in that signal's candidate list), distinguishing it from a
    zero-score blank.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    hits = [_hit(bm25_score=1.5, bm25_rank=1, cosine_score=None, cosine_rank=None)]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=hits),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "lexical only", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # The explicit absent marker is rendered for the None-rank signal.
    assert "absent from this signal" in body
    assert "top-50" in body
    # The present signal still shows its rank.
    assert "#1" in body


def test_diagnostics_labels_provenance_and_links_kb_source_id() -> None:
    """Provenance is labelled visibly and a kb hit's source_id links to its entry (#2457).

    Acceptance criteria: the provenance row carries a visible ``Source id``
    caption (not only a ``title=`` attribute), and a ``source == "kb"`` hit
    renders its ``source_id`` as an anchor to ``/ui/kb/<source_id>`` (kb
    documents persist ``source_id=slug``, so the id is the KB slug).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    hits = [_hit(source="kb", kind="kb-entry", source_id="quiesce-snapshots")]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=hits),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "snapshot", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Provenance labels are rendered as visible captions, not hover-only.
    assert ">Source</span>" in body
    assert ">Kind</span>" in body
    assert ">Source id</span>" in body
    # A kb hit's source_id is a same-tab anchor to its KB entry detail page.
    assert 'href="/ui/kb/quiesce-snapshots"' in body
    assert ">quiesce-snapshots</a>" in body


def test_diagnostics_non_kb_source_id_is_plain_text_never_a_dead_link() -> None:
    """A non-kb hit keeps its source_id as plain text with no anchor (#2457).

    Acceptance criterion: a memory hit (no resolvable ``/ui/kb`` route) must
    not wrap its source_id in an anchor, and must never emit an ``href`` that
    would 404 against the KB detail route.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    hits = [_hit(source="memory", kind="note", source_id="mem-scope-slug")]

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=hits),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "recall", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # The id is still shown, but as plain selectable text -- never a dead link.
    assert "mem-scope-slug" in body
    assert ">mem-scope-slug</a>" not in body
    assert 'href="/ui/kb/mem-scope-slug"' not in body


def test_diagnostics_empty_results_renders_no_matches_state() -> None:
    """A run returning zero hits renders the no-matches state, not an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=[]),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "nonexistent term", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "No hits for" in body
    assert "nonexistent term" in body
    assert 'role="alert"' not in body


def test_diagnostics_forwards_operator_tenant_and_filters() -> None:
    """The handler forwards the reconstructed operator's tenant + sub + filters."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, sub="op-principal")
    retrieve_mock = AsyncMock(return_value=[_hit()])

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, retrieve_mock),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "vcenter maximums", "source": "kb", "kind": "kb-entry", "limit": 20},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    retrieve_mock.assert_awaited_once()
    kwargs = retrieve_mock.await_args.kwargs
    assert kwargs["tenant_id"] == _TENANT_A
    assert kwargs["query"] == "vcenter maximums"
    assert kwargs["source"] == "kb"
    assert kwargs["kind"] == "kb-entry"
    assert kwargs["limit"] == 20
    # Own-tenant only -- the per-principal predicate is bound from the operator.
    assert kwargs["principal_sub"] == "op-principal"
    # A non-Memory source has no Memory candidates, so no eligibility.
    assert kwargs["memory_eligibility"] is None


def _post_diagnostics_with_mock(
    *, source: str, operator: Operator, retrieve_mock: AsyncMock
) -> None:
    """POST the diagnostics form once with *source* against a mocked retrieve."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, retrieve_mock),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "vcenter maximums", "source": source, "kind": "", "limit": 20},
                headers={CSRF_HEADER_NAME: csrf},
            )
    assert response.status_code == 200, response.text
    retrieve_mock.assert_awaited_once()


@pytest.mark.parametrize("source", ["", "memory"])
def test_diagnostics_passes_memory_eligibility_when_memory_rows_can_rank(
    source: str,
) -> None:
    """No source and ``source=memory`` both pass the Memory eligibility (#3741).

    The UI front must follow the same Memory contract as ``POST
    /api/v1/retrieve`` and the MCP retrieve resource: the frozen eligibility
    carries the operator's tenant, their ``sub``, and every visible Memory
    kind, so the candidate SQL drops rows the operator may not see before
    ranking.
    """
    operator = _operator(tenant_id=_TENANT_A, sub="op-principal")
    retrieve_mock = AsyncMock(return_value=[_hit()])

    _post_diagnostics_with_mock(source=source, operator=operator, retrieve_mock=retrieve_mock)

    kwargs = retrieve_mock.await_args.kwargs
    assert kwargs["source"] == (source or None)
    assert kwargs["principal_sub"] == "op-principal"
    eligibility = kwargs["memory_eligibility"]
    assert eligibility is not None
    assert eligibility.tenant_id == _TENANT_A
    assert eligibility.principal_sub == "op-principal"
    assert len(eligibility.kinds) == 5


# ---------------------------------------------------------------------------
# Audit / privacy binding
# ---------------------------------------------------------------------------


def test_diagnostics_binds_query_hash_not_raw_query() -> None:
    """The handler binds the SHA-256 query hash + source/kind/hit_count, never the raw query."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    raw_query = "a-very-secret-query-string"
    expected_hash = hashlib.sha256(raw_query.encode("utf-8")).hexdigest()

    bound_payloads: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        bound_payloads.append(dict(kwargs))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=[_hit()]),
            patch(_BIND_CONTEXTVARS, side_effect=_capture),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": raw_query, "source": "kb", "kind": "kb-entry", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # Flatten every bound payload.
    merged: dict[str, object] = {}
    for payload in bound_payloads:
        merged.update(payload)
    # The query hash is bound, the raw query is NOT.
    assert merged["audit_query_hash"] == expected_hash
    assert merged["audit_source"] == "kb"
    assert merged["audit_kind"] == "kb-entry"
    assert merged["audit_hit_count"] == 1
    # The raw query never appears in any bound payload (value or key).
    for payload in bound_payloads:
        for value in payload.values():
            assert value != raw_query
        assert raw_query not in payload


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_diagnostics_rejected_without_csrf_token() -> None:
    """A diagnostics POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        # No X-CSRF-Token header -> the double-submit pair is incomplete.
        response = client.post(
            "/ui/retrieval/diagnostics",
            data={"query": "snapshot"},
        )

    assert response.status_code == 403


def test_diagnostics_reuses_live_csrf_cookie_without_rotation() -> None:
    """The diagnostics fragment reuses the live CSRF cookie and does NOT rotate it."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RETRIEVE, new_callable=AsyncMock, return_value=[]),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "snapshot", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # No Set-Cookie for meho_csrf on the fragment response (live token reused).
    set_cookie = response.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME not in set_cookie


def test_diagnostics_session_gone_propagates_401() -> None:
    """A 401 from the operator-reconstruction seam surfaces as 401, not an error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(
            _RESOLVE_OPERATOR,
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="session_expired"
            ),
        ):
            response = client.post(
                "/ui/retrieval/diagnostics",
                data={"query": "snapshot", "limit": 10},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Route ordering + nav registration
# ---------------------------------------------------------------------------


def test_retrieval_router_registered_before_stubs() -> None:
    """The retrieval include precedes the stubs include in build_router."""
    import inspect

    from meho_backplane.ui import routes as routes_module

    source = inspect.getsource(routes_module.build_router)
    retrieval_pos = source.index("build_retrieval_router()")
    stubs_pos = source.index("build_stubs_router()")
    assert retrieval_pos < stubs_pos


def test_retrieval_diagnostics_literal_before_any_param_route() -> None:
    """No ``/ui/retrieval/{param}`` route precedes the literal ``/diagnostics`` POST."""
    from meho_backplane.ui.routes.retrieval import build_retrieval_router

    router = build_retrieval_router()
    retrieval_paths = [
        route.path  # type: ignore[attr-defined]
        for route in router.routes
        if getattr(route, "path", "").startswith("/ui/retrieval")
    ]
    # The literal diagnostics route exists; no param route precedes it.
    assert "/ui/retrieval/diagnostics" in retrieval_paths
    diagnostics_idx = retrieval_paths.index("/ui/retrieval/diagnostics")
    for path in retrieval_paths[:diagnostics_idx]:
        assert "{" not in path, f"param route {path!r} precedes the literal diagnostics route"


def test_dashboard_surface_grid_includes_retrieval_tile() -> None:
    """The dashboard surface-tile grid links to ``/ui/retrieval``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/ui/retrieval"' in body
    assert "Retrieval" in body


# ===========================================================================
# Usage Analytics tab (#1889)
# ===========================================================================


def _usage_report(
    *,
    total_searches: int = 0,
    buckets: list[object] | None = None,
    rest_excluded: bool = True,
) -> UsageReport:
    """Build a :class:`UsageReport` the patched ``compute_usage`` returns.

    ``counted_surfaces`` / ``rest_excluded`` default from the model (so the
    honesty-gap explainer fields are populated as production would emit them).
    """
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    return UsageReport(
        since=now - timedelta(days=30),
        until=now,
        surfaces=["kb", "memory", "operations"],
        tenant_id=_TENANT_A,
        buckets=buckets or [],  # type: ignore[arg-type]
        total_searches=total_searches,
        rest_excluded=rest_excluded,
    )


def test_usage_renders_total_and_rest_excluded_explainer_on_zero() -> None:
    """A ``total_searches=0`` renders the REST-excluded explainer, not "no activity".

    Acceptance criterion: ``compute_usage`` returns a ``UsageReport`` with
    ``total_searches=0, rest_excluded=True``; the fragment must surface the
    "REST excluded" / ``counted_surfaces`` explainer so the zero does not read
    as inactivity.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    report = _usage_report(total_searches=0, rest_excluded=True)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_USAGE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/usage",
                data={"since": "30d"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="retrieval-usage-results"' in body
    assert "<!doctype html>" not in body.lower()
    # The honesty-gap explainer reads as "REST excluded", not "no activity".
    assert "REST excluded" in body
    # At least one counted /mcp surface badge renders so the zero is self-explaining.
    assert "mcp:search_knowledge" in body


def test_usage_renders_per_surface_buckets() -> None:
    """A non-zero report renders ``total_searches`` + the per-(day, surface) table."""
    from meho_backplane.retrieval.usage import DailyUsageBucket

    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    bucket = DailyUsageBucket(
        date=datetime(2026, 6, 18, tzinfo=UTC).date(),
        surface="kb",
        search_count=7,
        distinct_operators=2,
        action_conversion_pct=42.86,
    )
    report = _usage_report(total_searches=7, buckets=[bucket])

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_USAGE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/usage",
                data={"since": "30d"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "7" in body
    assert "2026-06-18" in body
    assert "kb" in body
    assert "42.86" in body
    # A non-zero count does not show the zero-explainer.
    assert "REST excluded" not in body


def test_usage_own_tenant_only_no_tenant_filter() -> None:
    """The Usage handler runs own-tenant: ``compute_usage`` gets ``operator.tenant_id``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    usage_mock = AsyncMock(return_value=_usage_report(total_searches=0))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_USAGE, usage_mock),
        ):
            response = client.post(
                "/ui/retrieval/usage",
                data={"since": "30d"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    usage_mock.assert_awaited_once()
    kwargs = usage_mock.await_args.kwargs
    # Own-tenant only -- the report is scoped to the reconstructed operator's
    # tenant; there is no cross-tenant tenant_filter on this surface.
    assert kwargs["tenant_id"] == _TENANT_A


def test_usage_malformed_since_renders_inline_400_card_not_500() -> None:
    """A malformed ``since`` surfaces the ``SinceValueError`` as an inline error card.

    Acceptance criterion: a ``since`` like ``"banana"`` must render an inline
    400-class error card (the backend ``SinceValueError`` detail), NOT a 500 --
    and ``compute_usage`` must never be reached (the parse fails first).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    usage_mock = AsyncMock(return_value=_usage_report())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_USAGE, usage_mock),
        ):
            response = client.post(
                "/ui/retrieval/usage",
                data={"since": "banana"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    # Not a 500 -- the parser rejection is caught and rendered inline.
    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "Invalid lookback window" in body
    # The substrate was never reached -- the bad window short-circuits the run.
    usage_mock.assert_not_awaited()


def test_usage_rejected_without_csrf_token() -> None:
    """A usage POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post("/ui/retrieval/usage", data={"since": "30d"})

    assert response.status_code == 403


def test_usage_reuses_live_csrf_cookie_without_rotation() -> None:
    """The usage fragment reuses the live CSRF cookie and does NOT rotate it."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_USAGE, new_callable=AsyncMock, return_value=_usage_report()),
        ):
            response = client.post(
                "/ui/retrieval/usage",
                data={"since": "30d"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME not in set_cookie


# ===========================================================================
# Tab panes + template wiring (#1889)
# ===========================================================================


def test_index_renders_only_diagnostics_and_usage_tabs() -> None:
    """``GET /ui/retrieval`` shows only the Diagnostics and Usage tabs.

    The Eval Quality and Retire Checklist tabs are gone, and their routes
    with them. Both tabs are own-tenant only (no ``tenant_filter`` form field).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    assert ">Diagnostics</button>" in body
    assert ">Usage Analytics</button>" in body
    assert 'hx-post="/ui/retrieval/diagnostics"' in body
    assert 'hx-post="/ui/retrieval/usage"' in body
    assert 'id="retrieval-usage-results"' in body
    # The removed tabs leave no trace on the page.
    for gone in ("Eval Quality", "Retire Checklist", "/ui/retrieval/eval", "retire-checklist"):
        assert gone not in body
    # Own-tenant only -- no cross-tenant selector on either tab.
    assert 'name="tenant_filter"' not in body


@pytest.mark.parametrize("path", ["/ui/retrieval/eval", "/ui/retrieval/retire-checklist"])
def test_removed_eval_and_retire_routes_are_gone(path: str) -> None:
    """The Eval Quality and Retire Checklist fragment routes no longer exist."""
    from meho_backplane.ui.routes.retrieval import build_retrieval_router

    paths = {getattr(route, "path", "") for route in build_retrieval_router().routes}
    assert path not in paths


def test_retrieval_usage_literal_before_any_param_route() -> None:
    """No ``/ui/retrieval/{param}`` route precedes the literal usage POST."""
    from meho_backplane.ui.routes.retrieval import build_retrieval_router

    router = build_retrieval_router()
    retrieval_paths = [
        route.path  # type: ignore[attr-defined]
        for route in router.routes
        if getattr(route, "path", "").startswith("/ui/retrieval")
    ]
    for literal in ("/ui/retrieval/usage",):
        assert literal in retrieval_paths
        idx = retrieval_paths.index(literal)
        for path in retrieval_paths[:idx]:
            assert "{" not in path, f"param route {path!r} precedes the literal {literal!r}"
