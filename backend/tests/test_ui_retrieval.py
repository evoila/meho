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
from meho_backplane.db.models import Tenant
from meho_backplane.retrieval.retriever import RetrievalHit
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

#: The route-module symbol patched so the handlers get an operator without a
#: live JWKS round-trip.
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.retrieval.routes._resolve_operator"

#: The route-module symbol the diagnostics handler calls; mocked to control the
#: returned hit list / raised failure without a live corpus + embedder.
_RETRIEVE = "meho_backplane.ui.routes.retrieval.routes.retrieve"

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
    # Tab strip present with all four tabs; Diagnostics is the default-active panel.
    assert "Diagnostics" in body
    assert "Usage Analytics" in body
    assert "Eval Quality" in body
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
