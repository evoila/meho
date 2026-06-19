# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the docs-corpus UI surface.

Initiative #1775 (G10.7 Docs-corpus console surface), Task #1777.
Acceptance criteria on issue #1777:

* ``GET /ui/corpus`` renders a "Docs Corpus" page; the collection
  ``<select>`` is populated from the entitled, tenant-scoped catalogue and
  is **pre-selected when exactly one collection is entitled**.
* Submitting a query ``POST /ui/corpus/search`` calls the in-process
  ``search_docs`` service and swaps ``corpus/_results.html`` into
  ``#corpus-results``: one card per chunk with content + ``source_url``
  link + formatted score (+ ``collection`` tag when present).
* An empty hit list renders a "no results" state (not an error); a 403 /
  409 / 503 / 422 from ``search_docs`` renders a typed error card.
* The page is operator-gated (unauthenticated -> ``/ui/auth/login``); the
  CSRF token is minted on load and the ``meho_csrf`` cookie is set; the
  search fragment reuses the live token without rotating the cookie.
* ``/ui/corpus`` appears in the sidebar nav + the dashboard surface-tile
  grid.

Suite shape:

* :func:`_build_app` mirrors the production wiring + the kb / topology UI
  tests: StaticFiles, BFF auth router, UI surface router (corpus ahead of
  stubs), ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
* The doc-collection catalogue is seeded into the autouse SQLite engine
  (same as the ``search_docs`` route test). The operator-reconstruction
  seam (:func:`~meho_backplane.ui.routes.corpus.routes._resolve_operator`)
  is patched to return a constructed :class:`Operator` so the tests do not
  need a live Keycloak / JWKS round-trip; the actual ``search_docs``
  backend call is mocked at the route's ``search_docs`` import so the tests
  focus on the UI layer (the backend round-trip is exercised in
  ``tests/test_search_docs_route.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection, Tenant
from meho_backplane.docs_search import DocsChunk, DocsSearchResult
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

#: The route-module symbol patched so the handlers get an entitled operator
#: without a live JWKS round-trip.
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.corpus.routes._resolve_operator"

#: The route-module symbol the search handler calls; mocked to control the
#: returned chunk list / raised failure without a live corpus.
_SEARCH_DOCS = "meho_backplane.ui.routes.corpus.routes.search_docs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the kb UI suite)."""
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
    """Minimal FastAPI app wired for corpus UI tests.

    Mirrors production + the chassis smoke test: StaticFiles at
    ``/ui/static``, BFF auth router, UI surface router (corpus ahead of
    stubs), ``UISessionMiddleware`` outermost + ``CSRFMiddleware`` next.
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
    """Insert one ``tenant`` row so session + collection FKs resolve."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_collection(
    *,
    collection_key: str,
    status_value: str = "ready",
    tenant_id: uuid.UUID | None = None,
    vendor: str = "VMware by Broadcom",
) -> None:
    """Insert a doc collection row (global by default) for the catalogue."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                DocCollection(
                    tenant_id=tenant_id,
                    collection_key=collection_key,
                    vendor=vendor,
                    products=["vsphere"],
                    description=f"{vendor} docs.",
                    when_to_use="Vendor product questions.",
                    backend={"type": "corpus-http"},
                    status=status_value,
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
    capabilities: frozenset[str],
    sub: str = "op-42",
) -> Operator:
    """Build an :class:`Operator` the patched ``_resolve_operator`` returns."""
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        capabilities=capabilities,
    )


def _chunk(
    *,
    chunk_id: str = "c-1",
    document_id: str = "vsphere-guide",
    content: str = "Snapshots quiesce the guest before capture.",
    source_url: str | None = "https://docs.vmware.test/snapshots",
    score: float | None = 0.87,
    collection: str | None = None,
) -> DocsChunk:
    """Build a minimal :class:`DocsChunk` for mocked search returns."""
    return DocsChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source_url=source_url,
        score=score,
        collection=collection,
    )


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_corpus_index_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/corpus`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/corpus")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_corpus_search_unauthenticated_redirects_to_login() -> None:
    """``POST /ui/corpus/search`` without a session is bounced before the handler.

    The CSRF middleware rejects a state-changing request with no session
    cookie at 403; either way an unauthenticated caller never reaches the
    handler. (Authenticated CSRF behaviour is covered below.)
    """
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.post("/ui/corpus/search", data={"collection": "vmware", "q": "x"})
    assert response.status_code in (302, 403)


# ---------------------------------------------------------------------------
# GET /ui/corpus -- page render + collection selector
# ---------------------------------------------------------------------------


def test_corpus_index_renders_page_and_collections() -> None:
    """``GET /ui/corpus`` renders the page with the entitled collections."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    _seed_collection(collection_key="netapp", vendor="NetApp")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware", "meho-docs:netapp"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Docs Corpus" in body
    assert "Docs Corpus" in body
    # Both entitled collections appear as options.
    assert 'value="vmware"' in body
    assert 'value="netapp"' in body
    # Sidebar nav link + active highlight.
    assert 'href="/ui/corpus"' in body
    # CSRF cookie is set, and the form carries the token in hx-headers.
    assert CSRF_COOKIE_NAME in response.cookies
    assert "X-CSRF-Token" in body
    assert 'hx-post="/ui/corpus/search"' in body


def test_corpus_index_default_selects_sole_collection() -> None:
    """Exactly one entitled collection is pre-selected in the ``<select>``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    # A second collection exists but the operator is NOT entitled to it, so
    # the entitled set is a single collection -> default-select.
    _seed_collection(collection_key="netapp", vendor="NetApp")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    # The sole entitled option is rendered + carries the ``selected`` attr.
    # Normalise whitespace so the assertion is robust to template indentation.
    normalised = " ".join(body.split())
    assert 'value="vmware" selected' in normalised
    # The non-entitled collection is absent from the picker.
    assert 'value="netapp"' not in body
    # No "Select a collection" placeholder option when single-entitled.
    assert "Select a collection" not in body


def test_corpus_index_multiple_collections_no_default_selection() -> None:
    """With two entitled collections, none is pre-selected (placeholder shown)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    _seed_collection(collection_key="netapp", vendor="NetApp")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware", "meho-docs:netapp"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    normalised = " ".join(body.split())
    # The disabled placeholder is rendered + selected; no real collection
    # option carries the ``selected`` attribute.
    assert "Select a collection" in body
    assert 'value="" disabled selected' in normalised
    assert 'value="vmware" selected' not in normalised
    assert 'value="netapp" selected' not in normalised


def test_corpus_index_empty_state_when_no_entitled_collections() -> None:
    """A collection exists but the identity is unentitled -> diagnosable empty state.

    This is the reported #1802 symptom: a ``vmware`` collection is attached
    (and searchable via MCP) but the operator's session identity lacks
    ``meho-docs:vmware``. The page must name the missing capability + the
    identity it checked, NOT the opaque "No doc collections available".
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    # No ``meho-docs:vmware`` capability -> empty entitled set, but a
    # collection the operator can't see DOES exist.
    operator = _operator(tenant_id=_TENANT_A, capabilities=frozenset(), sub="op-unentitled")

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    # The actionable diagnostic names the missing capability + the identity.
    assert "meho-docs:vmware" in body
    assert "op-unentitled" in body
    assert str(_TENANT_A) in body
    assert "not entitled" in body.lower()
    # It is NOT the generic unprovisioned copy (a corpus DOES exist).
    assert "No doc collections available" not in body
    # The search form is not rendered when there is nothing to search.
    assert 'hx-post="/ui/corpus/search"' not in body


def test_corpus_index_unprovisioned_state_when_no_collections_exist() -> None:
    """A tenant with NO collections at all sees the generic unprovisioned copy.

    Distinct from the diagnosable missing-capability state above: when the
    catalogue holds nothing the operator could be entitled to, there is no
    concrete ``meho-docs:<key>`` to name, so the page keeps the plain "ask an
    administrator to register and entitle a collection" copy and does NOT
    fabricate a capability name.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    # No collection seeded at all.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=frozenset())

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    assert "No doc collections available" in body
    # No collection exists, so no missing-capability diagnostic is rendered.
    assert "meho-docs:" not in body
    assert 'hx-post="/ui/corpus/search"' not in body


# ---------------------------------------------------------------------------
# POST /ui/corpus/search -- successful fragment
# ---------------------------------------------------------------------------


def test_corpus_search_renders_cited_chunks() -> None:
    """A successful search swaps the fragment with one card per chunk."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    result = DocsSearchResult(
        chunks=[
            _chunk(
                chunk_id="c-1",
                document_id="vsphere-snapshots",
                content="Snapshots quiesce the guest before capture.",
                source_url="https://docs.vmware.test/snapshots",
                score=0.873,
            ),
            _chunk(
                chunk_id="c-2",
                document_id="nsx-overview",
                content="NSX overlays segment east-west traffic.",
                source_url=None,
                score=None,
            ),
        ]
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot quiesce"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="corpus-results"' in body
    assert "<!doctype html>" not in body.lower()
    # Both chunks rendered with content.
    assert "Snapshots quiesce the guest" in body
    assert "NSX overlays segment" in body
    # source_url link present for the first chunk; formatted score shown.
    assert 'href="https://docs.vmware.test/snapshots"' in body
    assert "0.873" in body
    # 2 cited chunks heading.
    assert "2 cited chunks" in body


def test_corpus_search_resolves_gs_kb_source_to_canonical_link() -> None:
    """A KB ``gs://`` chunk renders a clickable Broadcom KB link (#1919).

    The raw ``gs://`` object path an operator cannot open is resolved to the
    canonical ``knowledge.broadcom.com`` article URL; the rendered anchor never
    carries a broken ``gs://`` href.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    result = DocsSearchResult(
        chunks=[
            _chunk(
                chunk_id="kb-1",
                document_id="broadcom-kb-414551",
                content="vCenter scaling maximums.",
                source_url="gs://meho-knowledge-vmware-corpus/kb/broadcom-kb/articles/41/414551.html",
                score=0.9,
            ),
        ]
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "vcenter maximums"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Clickable canonical KB link, with the human label (document id fallback)
    # as link text.
    assert 'href="https://knowledge.broadcom.com/external/article/414551"' in body
    assert "broadcom-kb-414551" in body
    # The broken gs:// path is never rendered as an anchor href.
    assert 'href="gs://' not in body


def test_corpus_search_degrades_community_gs_source_to_non_clickable() -> None:
    """A community ``gs://`` chunk degrades to a non-clickable label (#1919).

    The mirror path carries no recoverable original post URL, so the citation
    renders title + path text rather than a broken ``gs://`` anchor.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    community_url = (
        "gs://meho-knowledge-vmware-corpus/community/williamlam/blog/quiesce-snapshots.md"
    )
    result = DocsSearchResult(
        chunks=[
            _chunk(
                chunk_id="comm-1",
                document_id="williamlam-quiesce",
                content="Community guidance on quiescing snapshots.",
                source_url=community_url,
                score=0.7,
            ),
        ]
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "quiesce snapshots"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # No broken gs:// anchor anywhere.
    assert 'href="gs://' not in body
    # The label is rendered (document id fallback) and the raw path is shown
    # as provenance text (not a link).
    assert "williamlam-quiesce" in body
    assert community_url in body


def test_corpus_search_renders_collection_tag_on_fanout_chunk() -> None:
    """A chunk carrying a ``collection`` provenance tag renders the tag."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    result = DocsSearchResult(chunks=[_chunk(collection="vmware")])

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # The collection provenance tag renders as a badge.
    assert "vmware" in response.text


def test_corpus_search_forwards_operator_and_collection() -> None:
    """The search forwards the reconstructed operator + selected collection."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    search_mock = AsyncMock(return_value=DocsSearchResult(chunks=[_chunk()]))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, search_mock),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot quiesce"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    search_mock.assert_awaited_once()
    call = search_mock.await_args
    # search_docs(operator, query, *, scope=, collection=, limit=)
    assert call.args[0] is operator
    assert call.args[1] == "snapshot quiesce"
    assert call.kwargs["scope"].collection_key == "vmware"


# ---------------------------------------------------------------------------
# POST /ui/corpus/search -- empty / error states
# ---------------------------------------------------------------------------


def test_corpus_search_empty_results_renders_no_results_state() -> None:
    """A search returning zero chunks renders the no-results state, not an error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=DocsSearchResult(chunks=[])),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "nonexistent term"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "No cited chunks for" in body
    assert "nonexistent term" in body
    # The no-results state is NOT an error alert.
    assert 'role="alert"' not in body


def test_corpus_search_backend_unavailable_renders_503_card() -> None:
    """A :class:`CorpusUnavailable` from search renders a typed 503 error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(
                _SEARCH_DOCS,
                new_callable=AsyncMock,
                side_effect=CorpusUnavailable("corpus backend unreachable", status=502),
            ),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    # The handler maps CorpusUnavailable to a 503 error card rendered in a
    # 200 fragment swap (the swap target is #corpus-results, not an error page).
    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "503" in body
    assert "Temporarily unavailable" in body


def test_corpus_search_disabled_collection_renders_403_card() -> None:
    """Searching a disabled collection renders a typed 403 error card.

    The collection is seeded ``disabled``; the shared resolve gate the
    handler calls (un-mocked here) raises ``CollectionDisabledError`` ->
    the handler maps it to a 403 card.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", status_value="disabled")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "403" in body
    assert "Not permitted" in body


def test_corpus_search_not_entitled_renders_403_card_naming_capability() -> None:
    """Searching a collection the identity isn't entitled to names the missing claim.

    The collection is seeded ``ready`` and the operator passes the static
    ``meho-docs`` add-on gate but lacks the per-collection
    ``meho-docs:vmware`` capability. The un-mocked resolve gate raises
    ``CollectionForbiddenError``; the handler maps it to a 403 card whose
    detail names the missing capability + the identity it checked (T2 #1802),
    not just "Not permitted".
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    # Base add-on capability only -> visible tool, but not entitled to vmware.
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs"}),
        sub="op-nopriv",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "403" in body
    assert "Not permitted" in body
    # The actionable diagnostic: the missing capability + the identity.
    assert "meho-docs:vmware" in body
    assert "op-nopriv" in body
    assert str(_TENANT_A) in body


def test_corpus_search_not_ready_collection_renders_409_card() -> None:
    """Searching a provisioning collection renders a typed 409 error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", status_value="provisioning")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "409" in body


def test_corpus_search_unknown_collection_renders_422_card() -> None:
    """Searching an unknown collection key renders a typed 422 error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:ghost"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "ghost", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "422" in body


def test_corpus_search_missing_collection_renders_422_card() -> None:
    """A search with a blank collection is the mandatory-scope 422 error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "422" in body


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_corpus_search_rejected_without_csrf_token() -> None:
    """A search POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        # No X-CSRF-Token header -> the double-submit pair is incomplete.
        response = client.post(
            "/ui/corpus/search",
            data={"collection": "vmware", "q": "snapshot"},
        )

    assert response.status_code == 403


def test_corpus_search_reuses_live_csrf_cookie_without_rotation() -> None:
    """The search fragment reuses the live CSRF cookie and does NOT rotate it.

    Rotating the cookie out from under the un-swapped search form would
    desync the form's ``hx-headers`` echo from the cookie (#1754 class); the
    handler reuses the validated live token and emits no ``Set-Cookie``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = _csrf_token(session_id)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=DocsSearchResult(chunks=[])),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # No Set-Cookie for meho_csrf on the fragment response (live token reused).
    set_cookie = response.headers.get("set-cookie", "")
    assert CSRF_COOKIE_NAME not in set_cookie


def test_corpus_search_session_gone_propagates_401() -> None:
    """A 401 from the operator-reconstruction seam surfaces as 401.

    When the session has been revoked between the middleware check and the
    handler, ``_resolve_operator`` raises 401; the handler does not swallow
    it into an error card (it is an auth condition, not a search failure).
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
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
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Dashboard surface tile + nav registration
# ---------------------------------------------------------------------------


def test_dashboard_surface_grid_includes_corpus_tile() -> None:
    """The dashboard surface-tile grid links to ``/ui/corpus``."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/")

    assert response.status_code == 200, response.text
    body = response.text
    assert 'href="/ui/corpus"' in body
    assert "Docs Corpus" in body
