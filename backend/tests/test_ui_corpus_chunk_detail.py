# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/corpus`` cited-source view affordance (#2462).

Initiative #2495 (G0.33 operator-console hardening), Task #2462. The
``/ui/corpus`` citation cards (#1919) rendered two kinds of source: a
public-URL chunk got a clickable outbound anchor, but a chunk whose source
normalizes to an opaque ``meho://docs/<collection>/<chunk_id>`` ref (#132)
was plain, non-clickable text -- a dead end. This task adds a consistent
view-source affordance: a ``meho://``-ref citation click-throughs to an
internal cited-source detail view, while public-URL citations keep their
outbound link (#1919 AC 2 preserved), and Retrieve + Ask render the identical
affordance for the identical doc (parity via the shared ``_cited_chunks``
seam).

Acceptance criteria on issue #2462:

* an Ask-mode ``meho://``-ref citation exposes an actionable view-source
  affordance opening an internal detail view -- no dead links
  (``test_ask_meho_ref_citation_click_throughs_to_internal_detail``);
* Retrieve + Ask render the same affordance for the same doc
  (``test_retrieve_and_ask_render_identical_view_source_href``);
* a public-URL citation keeps its outbound link, unregressed
  (``test_search_public_url_citation_keeps_outbound_link``);
* the internal detail view resolves a chunk id to a readable view, with
  404 / permission paths handled
  (``test_chunk_detail_*``).

Harness mirrors :mod:`backend.tests.test_ui_corpus`: the same BFF session +
CSRF + ``_resolve_operator`` patch, with ``search_docs`` /
``run_ask_pipeline_capturing_retrieval`` mocked at the route so the tests
focus on the UI layer.
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
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.api.v1.ask_docs import AskPipelineOutcome
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection, Tenant
from meho_backplane.docs_search import DocsAnswer, DocsChunk, DocsSearchResult
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

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

#: The search-router operator seam (patched for the search / ask fragment tests).
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.corpus.routes._resolve_operator"
#: The chunk-detail router imports ``_resolve_operator`` into its own namespace,
#: so the detail-route tests must patch the name bound THERE, not in ``routes``.
_RESOLVE_OPERATOR_DETAIL = "meho_backplane.ui.routes.corpus.chunk_detail._resolve_operator"
_SEARCH_DOCS = "meho_backplane.ui.routes.corpus.routes.search_docs"
_RUN_ASK = "meho_backplane.ui.routes.corpus.routes.run_ask_pipeline_capturing_retrieval"

#: A ``meho://`` ref citation: no public URL, so the card cannot link outbound.
_MEHO_REF = "meho://docs/vmware/c-2"
#: The internal detail href the ref maps to.
_INTERNAL_HREF = "/ui/corpus/chunks/vmware/c-2"


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


def _build_app() -> FastAPI:
    """Minimal FastAPI app wired for corpus UI tests (mirrors test_ui_corpus)."""
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


def _seed_collection(
    *,
    collection_key: str,
    status_value: str = "ready",
    tenant_id: uuid.UUID | None = None,
    vendor: str = "VMware by Broadcom",
    backend: dict[str, object] | None = None,
) -> None:
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
                    backend=backend if backend is not None else {"type": "corpus-http"},
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
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _csrf_token(session_id: uuid.UUID) -> str:
    return mint_csrf_token(str(session_id))


def _operator(
    *,
    tenant_id: uuid.UUID,
    capabilities: frozenset[str],
    sub: str = "op-42",
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        capabilities=capabilities,
    )


def _chunk(
    *,
    chunk_id: str = "c-2",
    document_id: str | None = "nsx-overview",
    content: str = "NSX overlays segment east-west traffic.",
    source_url: str | None = _MEHO_REF,
    score: float | None = 0.71,
    collection: str | None = None,
    read_handle: str | None = None,
) -> DocsChunk:
    return DocsChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source_url=source_url,
        score=score,
        collection=collection,
        read_handle=read_handle,
    )


def _post_search(
    session_id: uuid.UUID,
    operator: Operator,
    *,
    result: DocsSearchResult,
) -> str:
    """Drive ``POST /ui/corpus/search`` in retrieve mode; return the body."""
    csrf = _csrf_token(session_id)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_SEARCH_DOCS, new_callable=AsyncMock, return_value=result),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "nsx overlay"},
                headers={CSRF_HEADER_NAME: csrf},
            )
    assert response.status_code == 200, response.text
    return response.text


# ---------------------------------------------------------------------------
# Affordance in the retrieve (search) fragment
# ---------------------------------------------------------------------------


def test_search_meho_ref_citation_click_throughs_to_internal_detail() -> None:
    """A ``meho://``-ref citation renders an internal view-source link, not a dead end."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    body = _post_search(
        session_id, operator, result=DocsSearchResult(chunks=[_chunk(source_url=_MEHO_REF)])
    )

    # The title click-throughs to the internal cited-source detail view.
    assert f'href="{_INTERNAL_HREF}"' in body
    # No dead link: the opaque ``meho://`` ref is never an anchor href.
    assert 'href="meho://' not in body
    # The raw ref still surfaces as provenance text below the title (#1919).
    assert _MEHO_REF in body


def test_search_public_url_citation_keeps_outbound_link() -> None:
    """A public-URL citation keeps its outbound link, unregressed (#1919 AC 2)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    body = _post_search(
        session_id,
        operator,
        result=DocsSearchResult(chunks=[_chunk(source_url="https://docs.vmware.test/snapshots")]),
    )

    # Outbound anchor preserved (opens in a new tab).
    assert 'href="https://docs.vmware.test/snapshots"' in body
    assert 'target="_blank"' in body
    # A public-URL citation is NOT rewritten to the internal detail route.
    assert "/ui/corpus/chunks/" not in body


def test_search_no_source_citation_stays_plain_text() -> None:
    """A citation with no source at all gets neither an outbound nor an internal link."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    body = _post_search(
        session_id, operator, result=DocsSearchResult(chunks=[_chunk(source_url=None)])
    )

    assert "/ui/corpus/chunks/" not in body
    assert 'href="meho://' not in body
    # The label (document-id fallback) still renders as text.
    assert "nsx-overview" in body


# ---------------------------------------------------------------------------
# Ask-mode parity
# ---------------------------------------------------------------------------


def _post_ask(session_id: uuid.UUID, operator: Operator, *, outcome: AskPipelineOutcome) -> str:
    """Drive ``POST /ui/corpus/search`` in ask mode; return the body."""
    csrf = _csrf_token(session_id)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, new_callable=AsyncMock, return_value=outcome),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "nsx overlay", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )
    assert response.status_code == 200, response.text
    return response.text


def test_ask_meho_ref_citation_click_throughs_to_internal_detail() -> None:
    """An Ask-mode ``meho://``-ref citation opens the internal detail view -- no dead link."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    cited = _chunk(source_url=_MEHO_REF)
    outcome = AskPipelineOutcome(
        answer=DocsAnswer(answer="NSX segments east-west traffic.", citations=[cited]),
        retrieved_chunks=[cited],
    )
    body = _post_ask(session_id, operator, outcome=outcome)

    assert "NSX segments east-west traffic." in body
    assert f'href="{_INTERNAL_HREF}"' in body
    assert 'href="meho://' not in body


def test_retrieve_and_ask_render_identical_view_source_href() -> None:
    """Retrieve + Ask render the SAME view-source href for the SAME doc (parity)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )
    cited = _chunk(source_url=_MEHO_REF)

    retrieve_body = _post_search(session_id, operator, result=DocsSearchResult(chunks=[cited]))
    ask_body = _post_ask(
        session_id,
        operator,
        outcome=AskPipelineOutcome(
            answer=DocsAnswer(answer="ok", citations=[cited]),
            retrieved_chunks=[cited],
        ),
    )

    # The identical affordance (same internal href) appears in both modes.
    assert f'href="{_INTERNAL_HREF}"' in retrieve_body
    assert f'href="{_INTERNAL_HREF}"' in ask_body


# ---------------------------------------------------------------------------
# GET /ui/corpus/chunks/{collection_key}/{chunk_id} -- the internal detail view
# ---------------------------------------------------------------------------


def test_chunk_detail_renders_for_entitled_operator() -> None:
    """The detail view resolves the chunk id to a readable provenance page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:vmware"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR_DETAIL, new_callable=AsyncMock, return_value=operator):
            response = client.get(_INTERNAL_HREF)

    assert response.status_code == 200, response.text
    body = response.text
    assert "Cited source" in body
    # The chunk id + its stable reference are shown.
    assert "c-2" in body
    assert _MEHO_REF in body
    # It links onward to the collection detail page + names the vendor.
    assert 'href="/ui/corpus/collections/vmware"' in body
    assert "VMware by Broadcom" in body


def test_chunk_detail_forbidden_when_not_entitled() -> None:
    """An identity missing the per-collection capability is denied 403, named."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    # Base add-on only -> the collection resolves, but the identity is not
    # entitled to inspect its citations.
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs"}),
        sub="op-nopriv",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR_DETAIL, new_callable=AsyncMock, return_value=operator):
            response = client.get(_INTERNAL_HREF)

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["required_capability"] == "meho-docs:vmware"
    assert detail["operator_sub"] == "op-nopriv"
    assert detail["collection"] == "vmware"


def test_chunk_detail_unknown_collection_returns_404() -> None:
    """An unknown collection key resolves to 404 before the entitlement check."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No collection seeded.
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(
        tenant_id=_TENANT_A,
        capabilities=frozenset({"meho-docs", "meho-docs:ghost"}),
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR_DETAIL, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus/chunks/ghost/c-9")

    assert response.status_code == 404, response.text


def test_chunk_detail_unauthenticated_redirects_to_login() -> None:
    """The detail view is operator-gated: no session -> BFF login redirect."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get(_INTERNAL_HREF)

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# Read around the hit (#3948)
# ---------------------------------------------------------------------------

#: The ``corpus-http`` adapter's read transport seam.
_READ_SEAM = "meho_backplane.docs_search.backends.corpus_http.read_corpus"
_READ_ON: dict[str, object] = {"type": "corpus-http", "ref": {"read": "upstream"}}
_HANDLE = "eyJ2IjoxfQ.dWktaGFuZGxl"
_ENTITLED = frozenset({"meho-docs", "meho-docs:vmware"})


class _FakeRead:
    """A recording ``read_corpus`` stand-in."""

    def __init__(self, result: dict[str, object] | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def __call__(self, operator: Operator, read_handle: str, **kwargs: object) -> object:
        from meho_backplane.auth.corpus import UpstreamRead

        self.calls.append({"read_handle": read_handle, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return UpstreamRead.model_validate(self._result)


_READ_REPLY: dict[str, object] = {
    "mode": "around",
    "text": "Before the hit.\n<script>alert(1)</script> The hit.\nAfter the hit.",
    "title": "NSX overlay guide",
    "source_uri": "gs://private-bucket/docs/nsx.html",
    "upstream_url": "https://docs.vmware.test/nsx-overlay",
    "disclosure": "full",
    "truncated": True,
    "next": "next-cursor-1",
}


def _post_read(
    session_id: uuid.UUID,
    operator: Operator,
    fake: _FakeRead,
    *,
    data: dict[str, str],
    with_csrf: bool = True,
) -> object:
    csrf = _csrf_token(session_id)
    form = dict(data)
    if with_csrf:
        form["csrf_token"] = csrf
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR_DETAIL, new_callable=AsyncMock, return_value=operator),
            patch(_READ_SEAM, new=fake),
        ):
            return client.post(_INTERNAL_HREF, data=form)


def test_result_card_offers_read_form_only_with_a_read_handle() -> None:
    """A card whose chunk carries a handle posts it in the form body, never the URL."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    body = _post_search(
        session_id,
        operator,
        result=DocsSearchResult(
            chunks=[
                _chunk(chunk_id="c-2", read_handle=_HANDLE),
                _chunk(chunk_id="c-3", source_url="https://docs.vmware.test/x"),
            ]
        ),
    )
    assert f'action="{_INTERNAL_HREF}"' in body
    assert f'name="read_handle" value="{_HANDLE}"' in body
    assert 'name="csrf_token"' in body
    assert "Read around this hit" in body
    # One form only: the chunk without a handle gets none.
    assert body.count("Read around this hit</button>") == 1
    # The handle appears once, in the hidden form field: never in a URL.
    assert body.count(_HANDLE) == 1


def test_read_post_shows_escaped_text_and_more_form() -> None:
    """A read-enabled collection shows the text (escaped) and a "Show more" form."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(_READ_REPLY)

    response = _post_read(session_id, operator, fake, data={"read_handle": _HANDLE})

    assert response.status_code == 200, response.text
    body = response.text
    assert "Text around this hit" in body
    assert "The hit." in body
    # HTML-escaped, never rendered as markup.
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert 'href="https://docs.vmware.test/nsx-overlay"' in body
    assert "gs://" not in body
    assert "cut at the size limit" in body
    # "Show more" posts the same handle with the next cursor.
    assert 'name="cursor" value="next-cursor-1"' in body
    assert f'name="read_handle" value="{_HANDLE}"' in body
    assert "Show more" in body
    # Provenance stays on the page.
    assert _MEHO_REF in body
    (call,) = fake.calls
    assert call["read_handle"] == _HANDLE
    assert call["cursor"] is None


def test_read_post_sends_the_cursor_for_show_more() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead({**_READ_REPLY, "next": None})

    response = _post_read(
        session_id, operator, fake, data={"read_handle": _HANDLE, "cursor": "next-cursor-1"}
    )

    assert response.status_code == 200, response.text
    assert fake.calls[0]["cursor"] == "next-cursor-1"
    assert "Show more" not in response.text


def test_read_post_without_read_shows_provenance_and_a_neutral_note() -> None:
    """A collection without read keeps today's provenance view, plus one note."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(_READ_REPLY)

    response = _post_read(session_id, operator, fake, data={"read_handle": _HANDLE})

    assert response.status_code == 200, response.text
    assert "The text around this hit is not available." in response.text
    assert _MEHO_REF in response.text
    assert fake.calls == []


@pytest.mark.parametrize(
    ("kind", "retry_after", "note"),
    [
        ("not_found", None, "The text around this hit is not available."),
        ("search_again", None, "Run the search again"),
        ("rate_limited", 5, "Wait 5 seconds and try again."),
    ],
)
def test_read_post_refusals_show_plain_notes(kind: str, retry_after: int | None, note: str) -> None:
    from meho_backplane.auth.corpus import CorpusReadError

    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(CorpusReadError("x", kind=kind, status=400, retry_after=retry_after))

    response = _post_read(session_id, operator, fake, data={"read_handle": _HANDLE})

    assert response.status_code == 200, response.text
    assert note in response.text


def test_read_post_link_only_file_shows_only_the_link() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(
        {**_READ_REPLY, "disclosure": "link", "reason": "link_only", "text": "hidden words"}
    )

    response = _post_read(session_id, operator, fake, data={"read_handle": _HANDLE})

    body = response.text
    assert "allows only its link" in body
    assert "hidden words" not in body
    assert 'href="https://docs.vmware.test/nsx-overlay"' in body


def test_read_post_requires_csrf() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(_READ_REPLY)

    response = _post_read(
        session_id, operator, fake, data={"read_handle": _HANDLE}, with_csrf=False
    )

    assert response.status_code == 403
    assert fake.calls == []


def test_read_post_keeps_the_entitlement_gate() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=frozenset({"meho-docs"}))
    fake = _FakeRead(_READ_REPLY)

    response = _post_read(session_id, operator, fake, data={"read_handle": _HANDLE})

    assert response.status_code == 403
    assert fake.calls == []


def test_get_detail_stays_provenance_only_for_a_read_collection() -> None:
    """The GET view does not read: today's provenance-only page."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware", backend=_READ_ON)
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    fake = _FakeRead(_READ_REPLY)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with (
            patch(_RESOLVE_OPERATOR_DETAIL, new_callable=AsyncMock, return_value=operator),
            patch(_READ_SEAM, new=fake),
        ):
            response = client.get(_INTERNAL_HREF)

    assert response.status_code == 200
    assert "Text around this hit" not in response.text
    assert fake.calls == []
