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
from meho_backplane.retrieval.eval.result_models import EvalResult, SurfaceResult
from meho_backplane.retrieval.retire import (
    CriterionResult,
    RetireChecklistReport,
    SurfaceChecklist,
)
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

#: The route-module symbol the Eval tab calls; mocked to return a constructed
#: :class:`EvalResult` without a live corpus + embedder run (#1889).
_EVAL_ALL = "meho_backplane.ui.routes.retrieval.routes.eval_all"

#: The route-module symbol the Retire tab calls; mocked to return a constructed
#: :class:`RetireChecklistReport` without a live audit-log + eval run (#1890).
_COMPUTE_RETIRE = "meho_backplane.ui.routes.retrieval.routes.compute_retire_checklist"

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
    platform_admin: bool = False,
) -> Operator:
    """Build an :class:`Operator` the patched ``_resolve_operator`` returns."""
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        capabilities=frozenset(),
        platform_admin=platform_admin,
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
# Eval Quality tab (#1889)
# ===========================================================================


def _surface_result(
    *,
    surface: str = "kb",
    precision_at_5: float = 0.8,
    mrr: float = 0.75,
    coverage: float = 0.9,
    verdict: str = "green",
    query_count: int = 10,
) -> SurfaceResult:
    """Build a :class:`SurfaceResult` for mocked ``eval_all`` returns."""
    return SurfaceResult(
        surface=surface,  # type: ignore[arg-type]
        query_count=query_count,
        precision_at_5=precision_at_5,
        mrr=mrr,
        coverage=coverage,
        verdict=verdict,  # type: ignore[arg-type]
    )


def _eval_result(
    *,
    surfaces: list[SurfaceResult] | None = None,
    overall_verdict: str = "green",
) -> EvalResult:
    """Build an :class:`EvalResult` the patched ``eval_all`` returns."""
    return EvalResult(
        ran_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        surfaces=surfaces if surfaces is not None else [_surface_result()],
        overall_verdict=overall_verdict,  # type: ignore[arg-type]
    )


def test_eval_renders_metrics_and_verdict_pills() -> None:
    """The Eval fragment renders per-surface metrics + a verdict pill + the overall.

    Acceptance criterion: a stubbed ``eval_all`` returning a ``red`` surface
    renders the red pill (``data-verdict="red"``) AND a red ``overall_verdict``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    result = _eval_result(
        surfaces=[
            _surface_result(
                surface="kb",
                precision_at_5=0.2,
                mrr=0.15,
                coverage=0.3,
                verdict="red",
            )
        ],
        overall_verdict="red",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_EVAL_ALL, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/retrieval/eval",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="retrieval-eval-results"' in body
    assert "<!doctype html>" not in body.lower()
    # Per-surface metrics rendered.
    assert "0.200" in body  # precision@5
    assert "0.150" in body  # mrr
    assert "0.300" in body  # coverage
    # The verdict token is rendered verbatim with its mapped color class.
    assert 'data-verdict="red"' in body
    assert "badge-error" in body
    # The overall verdict is red too (worst-of every surface).
    assert body.count('data-verdict="red"') >= 2


def test_eval_renders_green_for_empty_corpus_surface_verbatim() -> None:
    """An empty-corpus surface's ``green`` verdict is rendered verbatim, not recomputed."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    result = _eval_result(
        surfaces=[
            _surface_result(
                surface="memory",
                precision_at_5=0.0,
                mrr=0.0,
                coverage=0.0,
                verdict="green",
                query_count=0,
            )
        ],
        overall_verdict="green",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_EVAL_ALL, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/retrieval/eval",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-verdict="green"' in body
    assert "badge-success" in body


def test_eval_own_tenant_only_no_baseline() -> None:
    """The Eval handler calls ``eval_all`` own-tenant with no baseline argument.

    Acceptance criterion: the 501 baseline path is never reached -- ``eval_all``
    is invoked with ``tenant_id`` only (no ``baseline*`` kwarg).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    eval_mock = AsyncMock(return_value=_eval_result())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_EVAL_ALL, eval_mock),
        ):
            response = client.post(
                "/ui/retrieval/eval",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    eval_mock.assert_awaited_once()
    kwargs = eval_mock.await_args.kwargs
    assert kwargs["tenant_id"] == _TENANT_A
    # No baseline argument is ever passed -- the 501 server-side baseline path
    # cannot be reached from this surface.
    assert not any("baseline" in key for key in kwargs)
    assert eval_mock.await_args.args == ()


def test_eval_rejected_without_csrf_token() -> None:
    """An eval POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post("/ui/retrieval/eval", data={})

    assert response.status_code == 403


# ===========================================================================
# Tab panes + template wiring (#1889)
# ===========================================================================


def test_index_renders_usage_and_eval_forms_no_baseline_field() -> None:
    """``GET /ui/retrieval`` wires the live Usage + Eval tab forms (no baseline field).

    Acceptance criteria: the Eval tab offers no baseline toggle (assert no
    ``baseline`` form field), and both tabs are own-tenant only (no
    ``tenant_filter`` form field).
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
    # Both tab forms post to their fragment routes.
    assert 'hx-post="/ui/retrieval/usage"' in body
    assert 'hx-post="/ui/retrieval/eval"' in body
    # Both results regions present for the initial (empty) render.
    assert 'id="retrieval-usage-results"' in body
    assert 'id="retrieval-eval-results"' in body
    # No baseline toggle anywhere -- the 501 baseline path must be unreachable.
    assert 'name="baseline"' not in body
    assert ">baseline<" not in body.lower()
    # Own-tenant only -- no cross-tenant selector on either tab (T3 owns that).
    assert 'name="tenant_filter"' not in body


def test_retrieval_usage_eval_literals_before_any_param_route() -> None:
    """No ``/ui/retrieval/{param}`` route precedes the literal usage / eval POSTs."""
    from meho_backplane.ui.routes.retrieval import build_retrieval_router

    router = build_retrieval_router()
    retrieval_paths = [
        route.path  # type: ignore[attr-defined]
        for route in router.routes
        if getattr(route, "path", "").startswith("/ui/retrieval")
    ]
    for literal in ("/ui/retrieval/usage", "/ui/retrieval/eval"):
        assert literal in retrieval_paths
        idx = retrieval_paths.index(literal)
        for path in retrieval_paths[:idx]:
            assert "{" not in path, f"param route {path!r} precedes the literal {literal!r}"


# ===========================================================================
# Retire Checklist tab (#1890)
# ===========================================================================


def _criterion(
    *,
    name: str = "daily_use_duration",
    verdict: str = "green",
    observed_value: str = "45 days since first use",
    threshold_summary: str = ">= 30 days",
    notes: str | None = None,
) -> CriterionResult:
    """Build a :class:`CriterionResult` for a stubbed surface checklist."""
    return CriterionResult(
        name=name,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        observed_value=observed_value,
        threshold_summary=threshold_summary,
        notes=notes,
    )


def _full_criteria(*, c4_yellow: bool = True) -> list[CriterionResult]:
    """Build the five canonical criteria; criterion 4 yellow by default (UI ceiling)."""
    return [
        _criterion(name="daily_use_duration", verdict="green"),
        _criterion(
            name="operator_breadth",
            verdict="green",
            observed_value="3 qualified operators",
            threshold_summary=">= 3 operators",
        ),
        _criterion(
            name="eval_precision",
            verdict="green",
            observed_value="precision@5 = 0.840",
            threshold_summary=">= 0.80",
        ),
        _criterion(
            name="meho_vs_baseline",
            verdict="yellow" if c4_yellow else "green",
            observed_value="baseline did not run" if c4_yellow else "every metric >= baseline",
            threshold_summary="every metric >= baseline",
            notes=("no baseline corpus configured for this surface in v0.2" if c4_yellow else None),
        ),
        _criterion(
            name="open_blockers",
            verdict="green",
            observed_value="0 open",
            threshold_summary="== 0 open blockers",
        ),
    ]


def _surface_checklist(
    *,
    surface: str = "kb",
    verdict: str = "REVIEW MANUALLY",
    criteria: list[CriterionResult] | None = None,
) -> SurfaceChecklist:
    """Build a :class:`SurfaceChecklist` for a stubbed retire report."""
    return SurfaceChecklist(
        surface=surface,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        criteria=criteria if criteria is not None else _full_criteria(),
    )


def _retire_report(
    *,
    surfaces: list[SurfaceChecklist] | None = None,
    overall_verdict: str = "REVIEW MANUALLY",
    tenant_id: uuid.UUID = _TENANT_A,
) -> RetireChecklistReport:
    """Build a :class:`RetireChecklistReport` the patched service returns."""
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    return RetireChecklistReport(
        ran_at=now,
        tenant_id=tenant_id,
        since=now - timedelta(days=90),
        until=now,
        surfaces=surfaces if surfaces is not None else [_surface_checklist()],
        overall_verdict=overall_verdict,  # type: ignore[arg-type]
    )


def test_retire_renders_three_distinct_verdict_states_not_binary() -> None:
    """The Retire fragment renders all three verdict states with distinct pills.

    Acceptance criterion: a report whose surfaces carry "READY TO RETIRE",
    "REVIEW MANUALLY", and "NOT YET" must render each as a distinct verdict pill
    (no collapse to a binary retire / hold).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    report = _retire_report(
        surfaces=[
            _surface_checklist(surface="kb", verdict="READY TO RETIRE"),
            _surface_checklist(surface="memory", verdict="REVIEW MANUALLY"),
            _surface_checklist(surface="operations", verdict="NOT YET"),
        ],
        overall_verdict="NOT YET",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="retrieval-retire-results"' in body
    assert "<!doctype html>" not in body.lower()
    # All three states render as distinct pills, verbatim.
    assert 'data-verdict="READY TO RETIRE"' in body
    assert 'data-verdict="REVIEW MANUALLY"' in body
    assert 'data-verdict="NOT YET"' in body
    # Each maps to its own daisyUI color (success/warning/error) -- not collapsed.
    assert "badge-success" in body
    assert "badge-warning" in body
    assert "badge-error" in body
    # The overall verdict pill heads the fragment (worst-of -> NOT YET here).
    assert "Overall" in body
    assert body.count('data-verdict="NOT YET"') >= 2


def test_retire_renders_five_criteria_verbatim_with_c4_yellow_note() -> None:
    """Each surface renders its five criteria verbatim + the criterion-4 yellow note.

    Acceptance criterion: the five criterion names render with their
    verdict/observed/threshold/notes verbatim, and the criterion-4 yellow honesty
    note copy is present when ``meho_vs_baseline.verdict == "yellow"``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    report = _retire_report(
        surfaces=[_surface_checklist(surface="kb", criteria=_full_criteria(c4_yellow=True))]
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # All five criterion names render verbatim.
    for name in (
        "daily_use_duration",
        "operator_breadth",
        "eval_precision",
        "meho_vs_baseline",
        "open_blockers",
    ):
        assert name in body
    # Observed/threshold fields render verbatim.
    assert "precision@5 = 0.840" in body
    assert "3 qualified operators" in body
    # The criterion-4 yellow honesty note is surfaced (baseline is CLI-only in v0.2).
    assert "Baseline is CLI-only in v0.2" in body
    # A green/yellow/red dot is rendered per criterion (the yellow c4 dot here).
    assert 'data-band="yellow"' in body
    assert 'data-band="green"' in body


def test_retire_c4_yellow_note_absent_when_criterion_green() -> None:
    """The criterion-4 honesty note is NOT shown when ``meho_vs_baseline`` is green."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    report = _retire_report(
        surfaces=[
            _surface_checklist(
                surface="kb",
                verdict="READY TO RETIRE",
                criteria=_full_criteria(c4_yellow=False),
            )
        ],
        overall_verdict="READY TO RETIRE",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "Baseline is CLI-only in v0.2" not in body


def test_retire_tenant_filter_selector_hidden_for_non_platform_admin() -> None:
    """The ``tenant_filter`` selector is absent for a non-platform-admin operator.

    Acceptance criterion: a non-platform-admin sees no cross-tenant selector and
    is own-tenant scoped.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=False)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    # The retire form posts to the read-only fragment route...
    assert 'hx-post="/ui/retrieval/retire-checklist"' in body
    # ...but the cross-tenant selector is hidden for a non-platform-admin.
    assert 'name="tenant_filter"' not in body


def test_retire_tenant_filter_selector_shown_for_platform_admin() -> None:
    """The ``tenant_filter`` selector renders for a platform-admin operator.

    Acceptance criterion: a platform admin sees the cross-tenant selector.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=True)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'name="tenant_filter"' in body


def test_retire_platform_admin_forwards_tenant_filter() -> None:
    """A platform admin's ``tenant_filter`` is authorized + forwarded to the service."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=True)
    retire_mock = AsyncMock(return_value=_retire_report(tenant_id=_TENANT_B))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, retire_mock),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={"tenant_filter": str(_TENANT_B)},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    retire_mock.assert_awaited_once()
    kwargs = retire_mock.await_args.kwargs
    # The cross-tenant claim is authorized (platform admin) and forwarded.
    assert kwargs["tenant_id"] == _TENANT_B
    # Every supported surface is requested, in order.
    assert list(kwargs["surfaces"]) == ["kb", "memory", "operations"]
    # The UI sends no CLI-fill-only body fields.
    assert "blocker_counts" not in kwargs
    assert "baseline_overrides" not in kwargs


def test_retire_own_tenant_when_no_tenant_filter() -> None:
    """A blank ``tenant_filter`` scopes the run to the operator's own tenant."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=False)
    retire_mock = AsyncMock(return_value=_retire_report())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, retire_mock),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    retire_mock.assert_awaited_once()
    kwargs = retire_mock.await_args.kwargs
    assert kwargs["tenant_id"] == _TENANT_A


def test_retire_forged_cross_tenant_filter_renders_403_card_not_500() -> None:
    """A non-platform-admin's forged ``tenant_filter`` surfaces the 403 inline, not a 500.

    Acceptance criterion: a non-platform-admin POST carrying a foreign
    ``tenant_filter`` surfaces the backend ``cross_tenant_requires_platform_admin``
    403 as an inline error card -- ``compute_retire_checklist`` is never reached.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=False)
    retire_mock = AsyncMock(return_value=_retire_report())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, retire_mock),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={"tenant_filter": str(_TENANT_B)},
                headers={CSRF_HEADER_NAME: csrf},
            )

    # Not a 500 -- the cross-tenant denial is caught and rendered inline.
    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "cross_tenant_requires_platform_admin" in body
    # The cross-tenant gate short-circuits before the service runs.
    retire_mock.assert_not_awaited()


def test_retire_malformed_tenant_filter_renders_error_card_not_500() -> None:
    """A malformed (non-UUID) ``tenant_filter`` renders an inline error card, not a 500."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=True)
    retire_mock = AsyncMock(return_value=_retire_report())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, retire_mock),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={"tenant_filter": "not-a-uuid"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "Invalid tenant filter" in body
    retire_mock.assert_not_awaited()


def test_retire_renders_rest_excluded_honesty_explainer() -> None:
    """The fragment surfaces the ``rest_excluded`` / ``counted_surfaces`` explainer."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)
    report = _retire_report()

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, new_callable=AsyncMock, return_value=report),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # rest_excluded defaults True on the model, so the explainer renders.
    assert "REST excluded" in body
    assert "mcp:search_knowledge" in body


def test_retire_no_write_affordance_read_only() -> None:
    """The retire surface is read-only: the only POST is the checklist run.

    Acceptance criterion: no purge / dry-run / execute-retirement affordance --
    no write form/button beyond the read-only ``hx-post`` to the checklist route.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, platform_admin=True)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/retrieval")

    assert response.status_code == 200, response.text
    body = response.text
    # The only retire POST is the read-only checklist run.
    assert 'hx-post="/ui/retrieval/retire-checklist"' in body
    # No purge / dry-run / execute-retirement affordance anywhere.
    for forbidden in ("purge", "dry-run", "dry run", "execute-retirement", "execute retirement"):
        assert forbidden not in body.lower()
    # The UI never offers a baseline-entry form for criterion 4.
    assert 'name="baseline"' not in body
    assert 'name="blocker_counts"' not in body
    assert 'name="baseline_overrides"' not in body


def test_retire_rejected_without_csrf_token() -> None:
    """A retire POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = client.post("/ui/retrieval/retire-checklist", data={})

    assert response.status_code == 403


def test_retire_reuses_live_csrf_cookie_without_rotation() -> None:
    """The retire fragment reuses the live CSRF cookie and does NOT rotate it."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_COMPUTE_RETIRE, new_callable=AsyncMock, return_value=_retire_report()),
        ):
            response = client.post(
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME not in set_cookie


def test_retire_session_gone_propagates_401() -> None:
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
                "/ui/retrieval/retire-checklist",
                data={},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 401


def test_retire_literal_before_any_param_route() -> None:
    """No ``/ui/retrieval/{param}`` route precedes the literal ``retire-checklist`` POST."""
    from meho_backplane.ui.routes.retrieval import build_retrieval_router

    router = build_retrieval_router()
    retrieval_paths = [
        route.path  # type: ignore[attr-defined]
        for route in router.routes
        if getattr(route, "path", "").startswith("/ui/retrieval")
    ]
    assert "/ui/retrieval/retire-checklist" in retrieval_paths
    idx = retrieval_paths.index("/ui/retrieval/retire-checklist")
    for path in retrieval_paths[:idx]:
        assert "{" not in path, f"param route {path!r} precedes the literal retire-checklist route"
