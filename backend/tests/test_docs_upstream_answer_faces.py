# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``ask_docs`` faces on an upstream-answer collection (#3911).

The REST route, the MCP tool and the ``/ui/corpus`` Ask mode all answer
through the one answer seam. For a collection that opted in
(``backend.ref["answer"] = "upstream"``) this pins, per face:

* **Parity** -- the same stubbed upstream body gives the same answer and
  citations on all three faces, each making exactly one answer call and no
  search call.
* **Error table** -- every upstream outcome maps to its ``(leg, cause)`` and
  status: REST status + ``Retry-After`` pass-through (driven end to end
  through the real transport with ``respx``), MCP ``-32603`` +
  ``data.retry_after``, and the UI banner with its one-search fallback.
* **MCP envelope** -- the upstream ``answer`` and every citation ``content``
  are wrapped as untrusted text.
* **Audit** -- the REST ``meho.docs.ask`` row is unchanged.
* **Scope gates** (#3912) -- the REST and MCP faces pass ``product`` /
  ``version`` through, and the answer call carries the soft ``scope``, the
  hard ``filters`` or neither, per the collection's gates. The UI Ask form
  has no product / version fields, so it never sends either.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.ask_docs import _compute_query_hash
from meho_backplane.api.v1.ask_docs import router as ask_docs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.corpus import (
    CorpusAnswerError,
    CorpusChunk,
    CorpusSearchResponse,
    CorpusUnavailable,
    UpstreamAnswer,
)
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog, DocCollection, Tenant
from meho_backplane.docs_search.answer_errors import (
    CAUSE_CORPUS_UNAVAILABLE,
    CAUSE_SYNTHESIS_PARSE,
    CAUSE_UPSTREAM_ANSWER_UNAVAILABLE,
    CAUSE_UPSTREAM_ERROR,
    CAUSE_UPSTREAM_RATE_LIMITED,
    CAUSE_UPSTREAM_REJECTED,
    LEG_CORPUS,
    LEG_MODEL,
    LEG_SYNTHESIS,
)
from meho_backplane.main import app as main_app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import clear_discovery_cache, reset_verifier_store_for_testing
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from meho_backplane.untrusted_text import BLOCK_START, wrap_untrusted_text
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
)

from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_ASK_SEAM = "meho_backplane.docs_search.backends.corpus_http.ask_corpus"
_SEARCH_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"
_RESOLVE_OPERATOR = "meho_backplane.ui.routes.corpus.routes._resolve_operator"

_FIXTURE = Path(__file__).parent / "fixtures" / "docs" / "upstream_ask_include_hits.json"
_ENTITLED = frozenset({"meho-docs", "meho-docs:vmware"})
_UPSTREAM_BACKEND: dict[str, Any] = {
    "type": "corpus-http",
    "ref": {"endpoint": "https://corpus.test/search", "answer": "upstream"},
}
_QUERY = "What is new in Example Platform 2.1.1?"

#: The fixture body mapped into the backplane's shape (see the seam tests).
_EXPECTED_ANSWER = (
    "Example Platform 2.1.1 adds pooled widget storage [1]. "
    "Upgrading from 2.1.0 needs no downtime [2]. "
    "The pooling limits are listed in the release notes [1]."
)
_EXPECTED_CITATION_IDS = ["chunk-rn-2-1-1-0004", "chunk-upgrade-0011"]

_SEARCH_CHUNK = CorpusChunk(
    chunk_id="search-fallback-0001",
    content="Fallback chunk from one plain search.",
    source_url="https://docs.example.test/fallback",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis, MCP and BFF env every face needs, with clean caches."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("CORPUS_URL", "")
    resets = (
        get_settings.cache_clear,
        clear_jwks_cache,
        reset_fernet_cache_for_testing,
        reset_verifier_store_for_testing,
        reset_templating_for_testing,
        clear_discovery_cache,
        reset_engine_for_testing,
    )
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


async def _seed_async(backend: dict[str, Any] | None = None) -> None:
    """Seed the global ``vmware`` collection (opted in by default) + the UI tenant."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-test", name="Op Test"))
        session.add(
            DocCollection(
                tenant_id=None,
                collection_key="vmware",
                vendor="VMware by Broadcom",
                products=["vsphere"],
                description="VMware docs.",
                when_to_use="Vendor product questions.",
                backend=backend if backend is not None else _UPSTREAM_BACKEND,
                status="ready",
            ),
        )


def _seed(backend: dict[str, Any] | None = None) -> None:
    asyncio.run(_seed_async(backend))


class _FakeAsk:
    """A recording ``ask_corpus`` stand-in: the fixture body, or a raised error."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, operator: Operator, query: str, **kwargs: Any) -> UpstreamAnswer:
        self.calls += 1
        self.kwargs.append(kwargs)
        if self._error is not None:
            raise self._error
        return UpstreamAnswer.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


class _FakeSearch:
    """A recording ``search_corpus`` stand-in (the UI's fallback search)."""

    def __init__(self, *, down: bool = False) -> None:
        self._down = down
        self.calls = 0

    async def __call__(self, operator: Operator, query: str, **kwargs: Any) -> CorpusSearchResponse:
        self.calls += 1
        if self._down:
            raise CorpusUnavailable("corpus unreachable: ConnectError")
        return CorpusSearchResponse(chunks=[_SEARCH_CHUNK])


def _operator() -> Operator:
    return Operator(
        sub="op-test",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        capabilities=_ENTITLED,
    )


# ---------------------------------------------------------------------------
# The three faces
# ---------------------------------------------------------------------------


def _rest_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(ask_docs_router)
    return app


def _rest_post(router: respx.MockRouter, query: str = _QUERY, **refinements: str) -> httpx.Response:
    """POST ``/api/v1/ask_docs`` with a minted, entitled JWT (JWKS on *router*)."""
    key = make_rsa_keypair("kid-A")
    mock_discovery_and_jwks(router, public_jwks(key))
    token = mint_token(key, sub="op-rest", capabilities=sorted(_ENTITLED))
    return TestClient(_rest_app()).post(
        "/api/v1/ask_docs",
        json={"query": query, "collection": "vmware", **refinements},
        headers={"Authorization": f"Bearer {token}"},
    )


def _mcp_call(**refinements: str) -> dict[str, Any]:
    """Call the MCP ``ask_docs`` tool as an entitled operator; return the JSON-RPC body."""

    async def _verify() -> Operator:
        return _operator()

    main_app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify
    try:
        with TestClient(main_app) as client:
            response = post_mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "ask_docs",
                        "arguments": {"query": _QUERY, "collection": "vmware", **refinements},
                    },
                },
            )
    finally:
        main_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _ui_post() -> str:
    """Submit the ``/ui/corpus`` Ask form as an entitled session; return the HTML."""

    async def _session() -> uuid.UUID:
        async with get_sessionmaker()() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="op-test",
                tenant_id=OPERATOR_TENANT_ID,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    session_id = asyncio.run(_session())
    csrf = mint_csrf_token(str(session_id))
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount("/ui/static", StaticFiles(directory=str(static_root_dir()), check_dir=False))
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    with respx.mock(assert_all_called=False):
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=_operator()):
            response = client.post(
                "/ui/corpus/search",
                data={"collection": "vmware", "q": _QUERY, "mode": "ask"},
                headers={CSRF_HEADER_NAME: csrf},
            )
    assert response.status_code == 200, response.text
    return response.text


def _ui_cited_ids(html: str) -> list[str]:
    """The chunk ids of the rendered citation cards, in order (internal view links)."""
    return re.findall(r'href="/ui/corpus/chunks/vmware/([^"]+)"', html)


# ---------------------------------------------------------------------------
# Parity: one upstream body, three faces, one answer
# ---------------------------------------------------------------------------


def test_three_faces_return_the_same_upstream_answer_and_citations() -> None:
    """REST, MCP and UI map one upstream body to the same answer + citations.

    Each face makes exactly one answer call and no search call.
    """
    _seed()

    rest_ask, rest_search = _FakeAsk(), _FakeSearch()
    with (
        respx.mock(assert_all_called=False) as router,
        patch(_ASK_SEAM, new=rest_ask),
        patch(_SEARCH_SEAM, new=rest_search),
    ):
        rest = _rest_post(router)
    assert rest.status_code == 200, rest.text
    rest_body = rest.json()

    mcp_ask, mcp_search = _FakeAsk(), _FakeSearch()
    with patch(_ASK_SEAM, new=mcp_ask), patch(_SEARCH_SEAM, new=mcp_search):
        mcp_body = _mcp_call()
    mcp_payload = json.loads(mcp_body["result"]["content"][0]["text"])

    ui_ask, ui_search = _FakeAsk(), _FakeSearch()
    with patch(_ASK_SEAM, new=ui_ask), patch(_SEARCH_SEAM, new=ui_search):
        html = _ui_post()

    for ask, search in ((rest_ask, rest_search), (mcp_ask, mcp_search), (ui_ask, ui_search)):
        assert (ask.calls, search.calls) == (1, 0)

    # REST: the mapped answer and citations, in the response shape of today.
    assert rest_body["answer"] == _EXPECTED_ANSWER
    assert [c["chunk_id"] for c in rest_body["citations"]] == _EXPECTED_CITATION_IDS
    assert set(rest_body) == {"answer", "citations"}
    # MCP: the same answer (wrapped) and the same citations.
    assert mcp_payload["answer"] == wrap_untrusted_text(rest_body["answer"])
    assert [c["chunk_id"] for c in mcp_payload["citations"]] == _EXPECTED_CITATION_IDS
    assert [c["content"] for c in mcp_payload["citations"]] == [
        wrap_untrusted_text(c["content"]) for c in rest_body["citations"]
    ]
    assert [c["source_url"] for c in mcp_payload["citations"]] == [
        c["source_url"] for c in rest_body["citations"]
    ]
    # UI: the same answer text, the same citation cards, numbered [1], [2].
    assert _EXPECTED_ANSWER in html
    assert _ui_cited_ids(html) == _EXPECTED_CITATION_IDS
    assert 'aria-label="Citation 1">[1]</span>' in html
    assert 'aria-label="Citation 2">[2]</span>' in html


def test_local_collection_cards_are_not_numbered() -> None:
    """Without the opt-in the UI answer renders as today: no citation numbers."""
    _seed(backend={"type": "corpus-http"})
    from meho_backplane.docs_search import DocsAnswer, DocsChunk
    from meho_backplane.docs_search.answer import AskPipelineOutcome

    chunk = DocsChunk(chunk_id="c-1", content="Local.", source_url="https://d.test/x")
    outcome = AskPipelineOutcome(
        answer=DocsAnswer(answer="Local answer.", citations=[chunk]), retrieved_chunks=[chunk]
    )
    run = "meho_backplane.ui.routes.corpus.routes.run_ask_pipeline_capturing_retrieval"
    with patch(run, new_callable=AsyncMock, return_value=outcome):
        html = _ui_post()
    assert "Local answer." in html
    assert "Citation 1" not in html


@pytest.mark.asyncio
async def test_rest_audit_row_unchanged_on_upstream_path() -> None:
    """The ``meho.docs.ask`` audit row binds the same identity on the upstream path."""
    await _seed_async()
    with (
        respx.mock(assert_all_called=False) as router,
        patch(_ASK_SEAM, new=_FakeAsk()),
        patch(_SEARCH_SEAM, new=_FakeSearch()),
    ):
        response = _rest_post(router)
    assert response.status_code == 200

    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/ask_docs")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.ask"
    assert payload["op_class"] == "read"
    assert payload["query_hash"] == _compute_query_hash(_QUERY)
    assert payload["collection"] == "vmware"
    assert payload["hit_count"] == len(_EXPECTED_CITATION_IDS)
    assert _QUERY not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Error table -- REST, end to end through the real transport
# ---------------------------------------------------------------------------

_SECRET_BODY = "INTERNAL upstream trace leaky-token-abc"


@pytest.mark.parametrize(
    ("upstream", "status", "leg", "cause", "extra"),
    [
        (httpx.ConnectError("refused"), 503, LEG_CORPUS, CAUSE_CORPUS_UNAVAILABLE, {}),
        (httpx.ReadTimeout("slow"), 503, LEG_CORPUS, CAUSE_CORPUS_UNAVAILABLE, {}),
        (
            httpx.Response(
                503, json={"error": {"code": "llm_unavailable", "message": _SECRET_BODY}}
            ),
            503,
            LEG_MODEL,
            CAUSE_UPSTREAM_ANSWER_UNAVAILABLE,
            {},
        ),
        (
            httpx.Response(
                503,
                json={"error": {"code": "llm_rate_limited", "message": _SECRET_BODY}},
                headers={"Retry-After": "5"},
            ),
            503,
            LEG_MODEL,
            CAUSE_UPSTREAM_RATE_LIMITED,
            {"retry_after": 5},
        ),
        (
            httpx.Response(502, text=_SECRET_BODY),
            503,
            LEG_CORPUS,
            CAUSE_UPSTREAM_ERROR,
            {"upstream_status": 502},
        ),
        (
            httpx.Response(422, json={"detail": _SECRET_BODY}),
            502,
            LEG_CORPUS,
            CAUSE_UPSTREAM_REJECTED,
            {"upstream_status": 422},
        ),
        (httpx.Response(200, text=_SECRET_BODY), 502, LEG_SYNTHESIS, CAUSE_SYNTHESIS_PARSE, {}),
    ],
    ids=["transport", "timeout", "no-model", "rate-limited", "5xx", "4xx", "malformed"],
)
def test_rest_error_table(
    upstream: httpx.Response | Exception,
    status: int,
    leg: str,
    cause: str,
    extra: dict[str, Any],
) -> None:
    """Each upstream outcome -> its REST status + leg / cause, body never echoed."""
    _seed()
    with respx.mock(assert_all_called=False) as router:
        ask_route = router.post(host="corpus.test", path="/ask")
        if isinstance(upstream, Exception):
            ask_route.mock(side_effect=upstream)
        else:
            ask_route.mock(return_value=upstream)
        search_route = router.post(host="corpus.test", path="/search")
        response = _rest_post(router)

    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert (detail["leg"], detail["cause"]) == (leg, cause)
    for key, value in extra.items():
        assert detail[key] == value
    if "retry_after" in extra:
        assert response.headers["retry-after"] == str(extra["retry_after"])
    else:
        assert "retry-after" not in response.headers
    assert _SECRET_BODY not in response.text
    assert ask_route.call_count == 1
    assert ask_route.calls.last.request.url.params["include"] == "hits"
    assert search_route.call_count == 0


# ---------------------------------------------------------------------------
# Error table -- MCP
# ---------------------------------------------------------------------------

_TYPED_FAILURES = [
    (CorpusUnavailable("corpus unreachable: ConnectError"), LEG_CORPUS, CAUSE_CORPUS_UNAVAILABLE),
    (
        CorpusAnswerError("no model", kind=CorpusAnswerError.KIND_ANSWER_UNAVAILABLE, status=503),
        LEG_MODEL,
        CAUSE_UPSTREAM_ANSWER_UNAVAILABLE,
    ),
    (
        CorpusAnswerError(
            "rate limited", kind=CorpusAnswerError.KIND_RATE_LIMITED, status=503, retry_after=5
        ),
        LEG_MODEL,
        CAUSE_UPSTREAM_RATE_LIMITED,
    ),
    (
        CorpusAnswerError("5xx", kind=CorpusAnswerError.KIND_SERVER_ERROR, status=500),
        LEG_CORPUS,
        CAUSE_UPSTREAM_ERROR,
    ),
    (
        CorpusAnswerError("4xx", kind=CorpusAnswerError.KIND_REJECTED, status=404),
        LEG_CORPUS,
        CAUSE_UPSTREAM_REJECTED,
    ),
    (
        CorpusAnswerError("bad body", kind=CorpusAnswerError.KIND_MALFORMED, status=200),
        LEG_SYNTHESIS,
        CAUSE_SYNTHESIS_PARSE,
    ),
]
_TYPED_IDS = ["transport", "no-model", "rate-limited", "5xx", "4xx", "malformed"]


@pytest.mark.parametrize(("error", "leg", "cause"), _TYPED_FAILURES, ids=_TYPED_IDS)
def test_mcp_error_table(error: Exception, leg: str, cause: str) -> None:
    """Each upstream outcome -> MCP ``-32603`` with the leg / cause on ``error.data``."""
    _seed()
    fake_ask, fake_search = _FakeAsk(error), _FakeSearch()
    with patch(_ASK_SEAM, new=fake_ask), patch(_SEARCH_SEAM, new=fake_search):
        body = _mcp_call()

    assert body["error"]["code"] == INTERNAL_ERROR
    data = body["error"]["data"]
    assert (data["leg"], data["cause"]) == (leg, cause)
    if cause == CAUSE_UPSTREAM_RATE_LIMITED:
        assert data["retry_after"] == 5
    if cause == CAUSE_UPSTREAM_REJECTED:
        assert data["upstream_status"] == 404
    assert (fake_ask.calls, fake_search.calls) == (1, 0)


def test_mcp_wraps_upstream_answer_and_citation_content() -> None:
    """The upstream ``answer`` and each citation ``content`` sit inside the envelope."""
    _seed()
    with patch(_ASK_SEAM, new=_FakeAsk()), patch(_SEARCH_SEAM, new=_FakeSearch()):
        body = _mcp_call()
    payload = json.loads(body["result"]["content"][0]["text"])

    assert payload["answer"] == wrap_untrusted_text(_EXPECTED_ANSWER)
    assert payload["answer"].startswith(BLOCK_START)
    assert payload["citations"]
    assert all(c["content"].startswith(BLOCK_START) for c in payload["citations"])


# ---------------------------------------------------------------------------
# Error table -- UI: banner + one plain search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("error", "leg", "cause"), _TYPED_FAILURES, ids=_TYPED_IDS)
def test_ui_upstream_failure_shows_banner_and_one_search(
    error: Exception, leg: str, cause: str
) -> None:
    """An upstream failure renders the leg banner over one plain search's chunks."""
    _seed()
    fake_ask, fake_search = _FakeAsk(error), _FakeSearch()
    with patch(_ASK_SEAM, new=fake_ask), patch(_SEARCH_SEAM, new=fake_search):
        html = _ui_post()

    assert "The grounded answer could not be composed" in html
    assert f'<code class="font-mono">{leg}</code>' in html
    assert f'<code class="font-mono">{cause}</code>' in html
    assert "Fallback chunk from one plain search." in html
    assert (fake_ask.calls, fake_search.calls) == (1, 1)


def test_ui_upstream_failure_with_search_down_shows_banner_alone() -> None:
    """Best effort: when the fallback search fails too, the banner stands alone."""
    _seed()
    error = CorpusAnswerError("4xx", kind=CorpusAnswerError.KIND_REJECTED, status=404)
    fake_ask, fake_search = _FakeAsk(error), _FakeSearch(down=True)
    with patch(_ASK_SEAM, new=fake_ask), patch(_SEARCH_SEAM, new=fake_search):
        html = _ui_post()

    assert "The grounded answer could not be composed" in html
    assert CAUSE_UPSTREAM_REJECTED in html
    assert "Fallback chunk" not in html
    assert (fake_ask.calls, fake_search.calls) == (1, 1)


# ---------------------------------------------------------------------------
# Scope gates on the answer call, per face (#3912)
# ---------------------------------------------------------------------------

_REFINEMENTS: dict[str, str] = {"product": "vsphere", "version": "8.0.3.00400"}
_SOFT: dict[str, str] = {**_REFINEMENTS, "source": "caller"}

_FACE_GATE_CASES = [
    # (ref gate keys, expected filters, expected soft scope)
    pytest.param({}, None, None, id="off"),
    pytest.param({"scope_filters": True}, _REFINEMENTS, None, id="filters"),
    pytest.param({"scope": "soft"}, None, _SOFT, id="soft"),
    pytest.param({"scope": "soft", "scope_filters": True}, None, _SOFT, id="both-soft-wins"),
]


def _gated_backend(gates: dict[str, Any]) -> dict[str, Any]:
    return {"type": "corpus-http", "ref": {**_UPSTREAM_BACKEND["ref"], **gates}}


@pytest.mark.parametrize(("gates", "expected_filters", "expected_scope"), _FACE_GATE_CASES)
@pytest.mark.asyncio
async def test_rest_answer_call_follows_the_scope_gates(
    gates: dict[str, Any],
    expected_filters: dict[str, str] | None,
    expected_scope: dict[str, str] | None,
) -> None:
    """REST ``ask_docs`` with product/version: the answer call is gated; the audit is not."""
    await _seed_async(_gated_backend(gates))
    ask, search = _FakeAsk(), _FakeSearch()
    with (
        respx.mock(assert_all_called=False) as router,
        patch(_ASK_SEAM, new=ask),
        patch(_SEARCH_SEAM, new=search),
    ):
        response = _rest_post(router, **_REFINEMENTS)
    assert response.status_code == 200, response.text
    assert (ask.calls, search.calls) == (1, 0)
    assert ask.kwargs[0]["filters"] == expected_filters
    assert ask.kwargs[0]["soft_scope"] == expected_scope

    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/ask_docs")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload["product"] == "vsphere"
    assert rows[0].payload["version"] == "8.0.3.00400"


@pytest.mark.parametrize(("gates", "expected_filters", "expected_scope"), _FACE_GATE_CASES)
def test_mcp_answer_call_follows_the_scope_gates(
    gates: dict[str, Any],
    expected_filters: dict[str, str] | None,
    expected_scope: dict[str, str] | None,
) -> None:
    """MCP ``ask_docs`` with product/version: the answer call is gated (#3912)."""
    _seed(_gated_backend(gates))
    ask, search = _FakeAsk(), _FakeSearch()
    with patch(_ASK_SEAM, new=ask), patch(_SEARCH_SEAM, new=search):
        body = _mcp_call(**_REFINEMENTS)
    assert "result" in body, body
    assert (ask.calls, search.calls) == (1, 0)
    assert ask.kwargs[0]["filters"] == expected_filters
    assert ask.kwargs[0]["soft_scope"] == expected_scope


def test_ui_answer_call_sends_no_refinements_whatever_the_gates() -> None:
    """The UI Ask form has no product / version, so nothing is forwarded (#3912)."""
    _seed(_gated_backend({"scope": "soft", "scope_filters": True}))
    ask, search = _FakeAsk(), _FakeSearch()
    with patch(_ASK_SEAM, new=ask), patch(_SEARCH_SEAM, new=search):
        html = _ui_post()
    assert _EXPECTED_ANSWER in html
    assert (ask.calls, search.calls) == (1, 0)
    assert ask.kwargs[0]["filters"] is None
    assert ask.kwargs[0]["soft_scope"] is None
