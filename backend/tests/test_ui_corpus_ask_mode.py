# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``/ui/corpus`` Ask mode (G4.6-T2 #1917).

The retrieve-only ``/ui/corpus`` surface (#1777) gains a second mode: an
**Ask** toggle that composes a grounded, cited *answer* over the ``ask_docs``
pipeline (expand → retrieve → synthesize) instead of returning the raw
chunks. Acceptance criteria on issue #1917:

* ``/ui/corpus`` has an Ask mode rendering the grounded answer + clickable
  citations.
* On a synthesis failure the Ask mode **fails open to chunks** -- it renders
  the retrieved chunks (the #1918 ``corpus_ask_fallback_context`` seam) under
  a banner naming the failed leg -- never an ungrounded answer.

Suite shape mirrors ``tests/test_ui_corpus.py``: the same BFF session +
CSRF + ``_resolve_operator`` patch harness. The ``ask_docs`` pipeline is
mocked at the route's ``run_ask_pipeline`` import (the in-process pipeline
is exercised end-to-end in ``tests/test_ask_docs_route.py``); these tests
focus on the UI layer -- the toggle, the answer render, and the fail-open
branch. Collection-access failures (entitlement / readiness / scope) go
through the un-mocked shared resolve gate, exactly as the search-mode tests
do, since the Ask mode reuses it.
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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import DocCollection, Tenant
from meho_backplane.docs_search import DocsAnswer, DocsChunk
from meho_backplane.docs_search.answer_errors import (
    CAUSE_CLIENT_UNAVAILABLE,
    CAUSE_SYNTHESIS_PARSE,
    LEG_EXPAND,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
)
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

_BACKPLANE_URL = "https://meho.test"
_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

#: Patched so the handlers get an entitled operator without a JWKS round-trip.
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.corpus.routes._resolve_operator"
#: The Ask-mode pipeline the handler calls; mocked to control the answer /
#: raised leg failure without a live corpus / model.
_RUN_ASK = "meho_backplane.ui.routes.corpus.routes.run_ask_pipeline"


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
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
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


def _seed_collection(*, collection_key: str, status_value: str = "ready") -> None:
    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                DocCollection(
                    tenant_id=None,
                    collection_key=collection_key,
                    vendor="VMware by Broadcom",
                    products=["vsphere"],
                    description="VMware docs.",
                    when_to_use="Vendor product questions.",
                    backend={"type": "corpus-http"},
                    status=status_value,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(*, tenant_id: uuid.UUID, operator_sub: str = "op-42") -> uuid.UUID:
    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    return asyncio.run(_do())


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    return client


def _operator(
    *, tenant_id: uuid.UUID, capabilities: frozenset[str], sub: str = "op-42"
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
    chunk_id: str = "c-1",
    document_id: str = "vsphere-snapshots",
    content: str = "Snapshots quiesce the guest before capture.",
    source_url: str | None = "https://docs.vmware.test/snapshots",
    score: float | None = 0.87,
) -> DocsChunk:
    return DocsChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source_url=source_url,
        score=score,
    )


_ENTITLED = frozenset({"meho-docs", "meho-docs:vmware"})


# ---------------------------------------------------------------------------
# GET /ui/corpus -- the mode toggle is rendered
# ---------------------------------------------------------------------------


def test_corpus_index_renders_mode_toggle() -> None:
    """``GET /ui/corpus`` renders the Retrieve / Ask mode toggle (#1917).

    Both radio options ride the search form (so the chosen ``mode`` is
    submitted with the query), and Retrieve is the default-checked option.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get("/ui/corpus")

    assert response.status_code == 200, response.text
    body = response.text
    normalised = " ".join(body.split())
    # Both mode radios are present and ride the search form.
    assert 'name="mode" value="search"' in normalised
    assert 'name="mode" value="ask"' in normalised
    # Retrieve is the default-checked mode on first render.
    assert 'name="mode" value="search" class="sr-only" checked' in normalised
    # The labels are operator-legible.
    assert "Retrieve" in body
    assert "Ask" in body


# ---------------------------------------------------------------------------
# POST /ui/corpus/search mode=ask -- grounded answer success path
# ---------------------------------------------------------------------------


def test_ask_mode_renders_grounded_answer_and_citations() -> None:
    """Ask mode renders the grounded answer prose + clickable citation cards."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    answer = DocsAnswer(
        answer="Snapshots quiesce the guest before capture for app-consistent backups.",
        citations=[_chunk()],
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, new_callable=AsyncMock, return_value=answer),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "do snapshots quiesce", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # Fragment, not full page.
    assert 'id="corpus-results"' in body
    assert "<!doctype html>" not in body.lower()
    # The grounded answer prose renders in its own answer card.
    assert "app-consistent backups" in body
    assert "Answer" in body
    # The cited chunk renders with its clickable resolved link (#1919).
    assert "Snapshots quiesce the guest" in body
    assert 'href="https://docs.vmware.test/snapshots"' in body
    # It is NOT rendered via the plain retrieve-mode heading ("N cited chunks
    # for <query>") -- the answer branch wins, with its own grounding heading.
    assert "cited chunks for" not in body
    assert "Grounded in 1 cited chunk" in body


def test_ask_mode_forwards_query_and_collection_to_pipeline() -> None:
    """Ask mode runs the ask pipeline with the operator + scoped collection."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    run_mock = AsyncMock(return_value=DocsAnswer(answer="ok", citations=[_chunk()]))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, run_mock),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot quiesce", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    run_mock.assert_awaited_once()
    call = run_mock.await_args
    # run_ask_pipeline(operator, query, *, scope=, collection=, limit=)
    assert call.args[0] is operator
    assert call.args[1] == "snapshot quiesce"
    assert call.kwargs["scope"].collection_key == "vmware"


def test_ask_mode_no_grounded_answer_renders_without_citations() -> None:
    """An empty-retrieval "no grounded answer" renders as the answer with no cards."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    answer = DocsAnswer(answer="No grounded answer: the corpus returned no chunks.", citations=[])

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, new_callable=AsyncMock, return_value=answer),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "obscure", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "No grounded answer" in body
    # No citation card list when there are no citations.
    assert 'role="list" aria-label="Cited chunks"' not in body
    # Not an error alert -- a no-grounding answer is a valid 200 result.
    assert 'role="alert"' not in body


# ---------------------------------------------------------------------------
# POST /ui/corpus/search mode=ask -- fail open to chunks on a leg failure
# ---------------------------------------------------------------------------


def test_ask_mode_synthesis_failure_fails_open_with_named_leg() -> None:
    """A synthesis leg failure renders the fail-open banner naming the leg (#1918).

    The Ask mode catches the :class:`AskDocsAnswerError` and renders the
    ``corpus_ask_fallback_context`` banner -- naming ``synthesis_malformed`` /
    ``parse`` -- rather than a bare error or an ungrounded answer.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    leg_error = AskDocsAnswerError(
        leg=LEG_SYNTHESIS,
        cause=CAUSE_SYNTHESIS_PARSE,
        message="synthesis leg failed: non-JSON output",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, new_callable=AsyncMock, side_effect=leg_error),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    # The fail-open banner names the failed leg + sub-cause (#1918).
    assert "Answer unavailable" in body
    assert LEG_SYNTHESIS in body
    assert CAUSE_SYNTHESIS_PARSE in body


def test_ask_mode_expand_leg_failure_renders_banner() -> None:
    """An expand leg failure (no chunks to fail open to) renders the named banner.

    The expand leg fails before retrieval produces usable chunks, so the
    fail-open render is the named-leg banner alone -- still diagnosable, just
    without chunk cards -- never an ungrounded answer.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    leg_error = AskDocsAnswerError(
        leg=LEG_EXPAND,
        cause=CAUSE_CLIENT_UNAVAILABLE,
        message="expand leg failed: no ANTHROPIC_API_KEY configured",
    )

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, new_callable=AsyncMock, side_effect=leg_error),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert "Answer unavailable" in body
    assert LEG_EXPAND in body
    # No chunk cards (the expand leg failed before retrieval).
    assert 'role="list" aria-label="Cited chunks"' not in body


# ---------------------------------------------------------------------------
# Ask mode reuses the search-mode collection-access gate (403 / 409 / 422)
# ---------------------------------------------------------------------------


def test_ask_mode_not_entitled_renders_403_card() -> None:
    """Ask mode on a non-entitled collection renders the same typed 403 card.

    The answer pipeline never runs -- the shared resolve gate rejects the
    collection first, exactly as search mode does.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(
        tenant_id=_TENANT_A, capabilities=frozenset({"meho-docs"}), sub="op-nopriv"
    )
    run_mock = AsyncMock(return_value=DocsAnswer(answer="unused", citations=[]))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, run_mock),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "403" in body
    assert "meho-docs:vmware" in body  # names the missing capability (T2 #1802)
    # The pipeline was never reached -- the collection gate rejected first.
    run_mock.assert_not_awaited()


def test_ask_mode_missing_collection_renders_422_card() -> None:
    """Ask mode with a blank collection is the mandatory-scope 422 error card."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    run_mock = AsyncMock(return_value=DocsAnswer(answer="unused", citations=[]))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, run_mock),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "", "q": "snapshot", "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    body = response.text
    assert 'role="alert"' in body
    assert "422" in body
    run_mock.assert_not_awaited()


def test_unrecognised_mode_falls_back_to_retrieve() -> None:
    """An unrecognised ``mode`` degrades to the safe retrieve-only path.

    A malformed / unknown ``mode`` value must NOT reach the answer pipeline --
    it falls back to ``search``, so the search service is what runs.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))
    operator = _operator(tenant_id=_TENANT_A, capabilities=_ENTITLED)
    ask_mock = AsyncMock(return_value=DocsAnswer(answer="unused", citations=[]))
    search_mock = AsyncMock()
    search_mock.return_value = type("R", (), {"chunks": [_chunk()]})()

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with (
            patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator),
            patch(_RUN_ASK, ask_mock),
            patch("meho_backplane.ui.routes.corpus.routes.search_docs", search_mock),
        ):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": "snapshot", "mode": "garbage"},
                headers={CSRF_HEADER_NAME: csrf},
            )

    assert response.status_code == 200, response.text
    # The retrieve path ran (search_docs), NOT the ask pipeline.
    search_mock.assert_awaited_once()
    ask_mock.assert_not_awaited()


def test_ask_mode_csrf_rejected_without_token() -> None:
    """An Ask-mode POST without the CSRF header is rejected 403 by the middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_collection(collection_key="vmware")
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    csrf = mint_csrf_token(str(session_id))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        # No X-CSRF-Token header -> the double-submit pair is incomplete.
        response = client.post(
            "/ui/corpus/search",
            data={"collection": "vmware", "q": "snapshot", "mode": "ask"},
        )

    assert response.status_code == 403
