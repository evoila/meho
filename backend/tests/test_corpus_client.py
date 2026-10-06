# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the backplane→corpus federation client (G4.5-T2 #1520).

Exercises :func:`~meho_backplane.auth.corpus.search_corpus` against an
``httpx.MockTransport`` mounted on the real :class:`httpx.AsyncClient` —
so the request the corpus would actually receive (URL, bearer header,
JSON body) is asserted, and every fail-closed branch (unconfigured,
unreachable, timeout, non-2xx) is shown to collapse to the one typed
:class:`~meho_backplane.auth.corpus.CorpusUnavailable`. The forwarded
operator JWT must never leak into a structlog event or the 503 detail.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import structlog
import structlog.testing

import meho_backplane.auth.corpus as corpus_mod
from meho_backplane.auth.corpus import (
    CorpusAnswerError,
    CorpusChunk,
    CorpusSearchResponse,
    CorpusStatusResponse,
    CorpusUnavailable,
    UpstreamAnswer,
    ask_corpus,
    corpus_status,
    derive_answer_url,
    derive_status_url,
    search_corpus,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import Settings, get_settings

_CORPUS_URL = "https://corpus.test/search"
_JWT = "header.payload.signature-secret"
#: The deployment-configured corpus service credential (#290) — the bearer
#: the transport presents to the corpus, distinct from the operator JWT
#: (which is never forwarded).
_SERVICE_TOKEN = "corpus-service-token-distinct-from-jwt"


def _make_operator(jwt: str = _JWT) -> Operator:
    """Build a minimal :class:`Operator` carrying the forwarded JWT."""
    return Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt=jwt,
        tenant_id="00000000-0000-0000-0000-00000000a0a0",
        tenant_role="operator",
    )


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env every :class:`Settings` field reads, then reset the cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _pin_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """Override ``corpus.get_settings`` with a Settings carrying *overrides*.

    Builds the real Settings from the pinned env, then ``model_copy``-es
    the corpus knobs the test cares about, so each test states its corpus
    config explicitly without touching every other field.
    """
    settings = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(corpus_mod, "get_settings", lambda: settings)
    return settings


def _transport_capturing(
    captured: list[httpx.Request],
    response: httpx.Response,
) -> httpx.MockTransport:
    """A MockTransport that records the request and returns *response*."""

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return httpx.MockTransport(_handler)


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
    captured_timeout: list[httpx.Timeout],
) -> None:
    """Force every ``AsyncClient`` the module builds onto *transport*.

    ``search_corpus`` constructs its own ``AsyncClient`` internally, so we
    wrap the real class to inject ``transport=`` (the documented
    mock-injection seam) and record the ``timeout=`` the client was built
    with — that is how the timeout-is-bounded assertion is made without a
    real slow server.
    """
    real_async_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        timeout = kwargs.get("timeout")
        if isinstance(timeout, httpx.Timeout):
            captured_timeout.append(timeout)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(corpus_mod.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_forwards_configured_service_token_and_posts_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client POSTs with the configured corpus service token, not the JWT (#290)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_service_token=_SERVICE_TOKEN)
    captured: list[httpx.Request] = []
    # MEHO.Knowledge's actual /search wire shape (#1732): a ``results``
    # envelope of chunks whose text/source-link fields are ``text`` /
    # ``source_uri``. The adapter must read real hits from this body.
    response = httpx.Response(
        200,
        json={
            "query": "supervisor cluster",
            "results": [
                {
                    "chunk_id": "c1",
                    "document_id": "d1",
                    "text": "vSphere 9.0 supervisor cluster setup.",
                    "source_uri": "https://docs.example/vsphere",
                    "score": 0.91,
                    "metadata": {"product": "vsphere", "version": "8.0"},
                }
            ],
            "took_ms": 12,
            "score_kind": "cosine",
        },
    )
    transport = _transport_capturing(captured, response)
    _patch_async_client(monkeypatch, transport, [])

    result = await search_corpus(_make_operator(), "supervisor cluster", limit=5)

    assert isinstance(result, CorpusSearchResponse)
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_id == "c1"
    # The corpus's ``text`` / ``source_uri`` map onto the consumed
    # ``content`` / ``source_url`` names downstream callers read.
    assert result.chunks[0].content == "vSphere 9.0 supervisor cluster setup."
    assert result.chunks[0].source_url == "https://docs.example/vsphere"
    assert result.chunks[0].metadata == {"product": "vsphere", "version": "8.0"}

    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == _CORPUS_URL
    # The deployment-configured service token is the bearer — never the
    # caller's operator JWT (#290).
    assert sent.headers["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert _JWT not in sent.headers.get("Authorization", "")
    import json

    body = json.loads(sent.content.decode())
    assert body["query"] == "supervisor cluster"
    # The corpus reads ``top_k``, not ``limit`` (#1732).
    assert body["top_k"] == 5
    assert "limit" not in body


@pytest.mark.asyncio
async def test_operator_jwt_is_never_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's raw operator JWT never rides the corpus request (#290).

    The credential-capture leg: replaying ``operator.raw_jwt`` to a
    tenant-configurable corpus URL leaked a Vault-capable bearer. The
    transport must present only the deployment-configured service token.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_service_token=_SERVICE_TOKEN)
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json={"chunks": []}))
    _patch_async_client(monkeypatch, transport, [])

    secret_jwt = "eyJ.super-secret-vault-capable.bearer"
    await search_corpus(_make_operator(jwt=secret_jwt), "q")

    # The operator JWT appears nowhere in the outbound request — not the
    # Authorization header, not any other header.
    assert secret_jwt not in repr(dict(captured[0].headers))
    assert captured[0].headers["Authorization"] == f"Bearer {_SERVICE_TOKEN}"


@pytest.mark.asyncio
async def test_no_service_token_sends_no_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset service token sends no Authorization header (never the JWT, #290)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_service_token="")
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json={"chunks": []}))
    _patch_async_client(monkeypatch, transport, [])

    await search_corpus(_make_operator(), "q")

    assert "Authorization" not in captured[0].headers


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://corpus.test/search",  # plaintext scheme
        "https://127.0.0.1/search",  # loopback
        "https://169.254.169.254/search",  # cloud metadata
        "https://[::1]/search",  # IPv6 loopback
    ],
)
@pytest.mark.asyncio
async def test_search_screens_endpoint_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """A non-https / non-public corpus endpoint fails closed before any dial (#290).

    No transport is patched: the destination screen must reject the URL
    before the client is built, so a bug that skips the screen would try a
    real dial (and, for the IP literals, reach loopback / metadata). The
    error message is the screen's, distinguishing it from a connect error.
    """
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
    _pin_settings(monkeypatch, corpus_url=bad_url)

    with pytest.raises(CorpusUnavailable) as exc:
        await search_corpus(_make_operator(), "q")
    assert "not an allowed https public destination" in str(exc.value)


@pytest.mark.asyncio
async def test_metadata_filters_and_audience_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given filters + a configured audience, both ride the request body."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_audience="meho-corpus")
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json={"chunks": []}))
    _patch_async_client(monkeypatch, transport, [])

    await search_corpus(
        _make_operator(),
        "q",
        metadata_filters={"product": "vsphere", "version": "8.0"},
    )

    import json

    body = json.loads(captured[0].content.decode())
    assert body["metadata_filters"] == {"product": "vsphere", "version": "8.0"}
    assert body["audience"] == "meho-corpus"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("soft_scope", "expected_body"),
    [
        # No soft scope (None or empty): the body is exactly what it was
        # before the key existed — the corpus refuses unknown request keys.
        (None, {"query": "q", "top_k": 10}),
        ({}, {"query": "q", "top_k": 10}),
        (
            {"product": "vsphere", "version": "8.0.3.00400", "source": "caller"},
            {
                "query": "q",
                "top_k": 10,
                "scope": {"product": "vsphere", "version": "8.0.3.00400", "source": "caller"},
            },
        ),
    ],
)
async def test_soft_scope_rides_the_body_as_scope_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
    soft_scope: dict[str, str] | None,
    expected_body: dict[str, object],
) -> None:
    """``soft_scope`` is sent as the ``scope`` object, unchanged (#3912)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_audience="")
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json={"chunks": []}))
    _patch_async_client(monkeypatch, transport, [])

    await search_corpus(_make_operator(), "q", soft_scope=soft_scope)

    import json

    assert json.loads(captured[0].content.decode()) == expected_body


@pytest.mark.asyncio
async def test_timeout_is_bounded_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AsyncClient is built with the configured corpus timeout."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_timeout_seconds=3.5)
    captured_timeout: list[httpx.Timeout] = []
    transport = _transport_capturing([], httpx.Response(200, json={"chunks": []}))
    _patch_async_client(monkeypatch, transport, captured_timeout)

    await search_corpus(_make_operator(), "q")

    assert captured_timeout, "AsyncClient was not built with an httpx.Timeout"
    # httpx.Timeout(x) sets connect/read/write/pool all to x.
    assert captured_timeout[0].read == 3.5
    assert captured_timeout[0].connect == 3.5


@pytest.mark.asyncio
async def test_slow_corpus_raises_corpus_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout from the transport maps to CorpusUnavailable, never a hang."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("corpus too slow", request=request)

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler), [])

    with pytest.raises(CorpusUnavailable) as exc:
        await search_corpus(_make_operator(), "q")
    assert exc.value.status is None


@pytest.mark.asyncio
async def test_unconfigured_corpus_url_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """corpus_url unset → CorpusUnavailable (fail-closed, not empty)."""
    _pin_settings(monkeypatch, corpus_url="")
    # No transport patch needed — the unconfigured guard fires before any I/O.
    with pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")


@pytest.mark.asyncio
async def test_connect_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable corpus (ConnectError) maps to CorpusUnavailable."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler), [])

    with pytest.raises(CorpusUnavailable) as exc:
        await search_corpus(_make_operator(), "q")
    assert exc.value.status is None


@pytest.mark.asyncio
async def test_non_2xx_carries_status_and_leaks_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx corpus response → CorpusUnavailable(status=...) with no body leak."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    secret_body = "INTERNAL corpus stack trace with leaky-token-abc"
    transport = _transport_capturing([], httpx.Response(502, text=secret_body))
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable) as exc:
        await search_corpus(_make_operator(), "q")
    assert exc.value.status == 502
    assert secret_body not in str(exc.value)


@pytest.mark.asyncio
async def test_non_json_2xx_body_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx with a non-JSON body is a broken contract → CorpusUnavailable."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing([], httpx.Response(200, text="<html>not json</html>"))
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")


@pytest.mark.asyncio
async def test_schema_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumed field of the wrong type fails validation → CorpusUnavailable."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing(
        [],
        # ``content`` is required str; an int violates the contract.
        httpx.Response(200, json={"chunks": [{"chunk_id": "c", "document_id": "d", "content": 7}]}),
    )
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")


@pytest.mark.asyncio
async def test_results_envelope_with_text_fields_returns_real_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populated {results:[…]} 200 returns real hits, not zero (#1732).

    The regression for the original SEV-2: a healthy corpus returning five
    hits under the ``results`` envelope (with ``text`` / ``source_uri``
    fields) was parsed to an empty hit list, so the consumer saw "no docs
    hits" for a populated corpus. The hits must come through with their
    text and source link mapped onto the consumed names.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "query": "NSX edge node sizing",
            "results": [
                {
                    "chunk_id": f"c{i}",
                    "document_id": f"d{i}",
                    "text": f"hit {i} body",
                    "source_uri": f"https://docs.example/{i}",
                    "score": 1.0 - i / 10,
                }
                for i in range(5)
            ],
            "took_ms": 9,
            "score_kind": "cosine",
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "NSX edge node sizing", limit=3)

    assert len(result.chunks) == 5
    assert result.chunks[0].content == "hit 0 body"
    assert result.chunks[0].source_url == "https://docs.example/0"


@pytest.mark.asyncio
async def test_document_id_threads_through_from_results_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populated ``document_id`` arrives on the chunk (#2004).

    The contract names the field ``document_id`` and MEHO.Knowledge speaks
    that exact key (unlike ``content``/``source_url``, it has no second wire
    name to alias), so a non-blank value must thread straight through the
    ``results`` envelope onto the consumed ``document_id``.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "document_id": "d-042",
                    "text": "owning-doc body",
                    "source_uri": "https://docs.example/d-042",
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].document_id == "d-042"


@pytest.mark.asyncio
async def test_blank_document_id_normalises_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``document_id`` is honestly typed as ``None`` (#2004).

    MEHO.Knowledge returns ``document_id: ""`` for a chunk with no owning-
    document concept. ``document_id`` is ``str | None``; a blank-after-strip
    value normalises to ``None`` rather than threading a misleading ``""``,
    so the citation-label fallback (``title -> document_id -> filename ->
    URL``) skips a cleanly-``None`` rung. The blank must NOT fail parse —
    ``document_id`` is a label fallback, not a grounding key.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "document_id": "",
                    "text": "no owning doc",
                    "source_uri": "https://docs.example/c1",
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].document_id is None
    # The chunk still parses and carries its other consumed fields.
    assert result.chunks[0].chunk_id == "c1"
    assert result.chunks[0].content == "no owning doc"


@pytest.mark.asyncio
async def test_top_level_title_threads_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-level ``title`` threads onto the chunk (#2475).

    An upstream corpus that supplies a human-legible per-chunk title is the
    only place a title can originate (MEHO has no doc-ingest path). A
    top-level ``title`` must reach the consumed ``CorpusChunk.title`` so the
    downstream citation-label chain can prefer it.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "document_id": "d-042",
                    "title": "Some KB title",
                    "text": "titled body",
                    "source_uri": "https://docs.example/d-042",
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].title == "Some KB title"


@pytest.mark.asyncio
async def test_metadata_title_is_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``metadata["title"]`` is used when no top-level title is present (#2475).

    A corpus may nest the title under the per-chunk ``metadata`` bag rather
    than emit it top-level; that value is the fallback so either wire shape
    yields a human-legible label.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "document_id": "d-042",
                    "text": "titled body",
                    "source_uri": "https://docs.example/d-042",
                    "metadata": {"title": "Some KB title", "product": "vsphere"},
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].title == "Some KB title"


@pytest.mark.asyncio
async def test_top_level_title_wins_over_metadata_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-level ``title`` wins over a ``metadata["title"]`` (#2475)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "title": "Top level title",
                    "text": "titled body",
                    "source_uri": "https://docs.example/c1",
                    "metadata": {"title": "Nested title"},
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].title == "Top level title"


@pytest.mark.asyncio
async def test_absent_title_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk with no title anywhere parses to ``title is None`` (#2475).

    Today's corpus emits no per-chunk title; that must not fail parse and
    must leave ``title`` cleanly ``None`` so no behaviour changes until a
    title is supplied.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "text": "untitled body",
                    "source_uri": "https://docs.example/c1",
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].title is None


@pytest.mark.asyncio
async def test_blank_title_normalises_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank-after-strip ``title`` normalises to ``None`` (#2475, #2004 pattern).

    An empty ``title: ""`` must not thread onto the label chain as an empty
    preferred label; it normalises to ``None`` so the chain falls through to
    ``document_id`` / filename / URL.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "title": "   ",
                    "document_id": "d-042",
                    "text": "blank-titled body",
                    "source_uri": "https://docs.example/d-042",
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    result = await search_corpus(_make_operator(), "q")

    assert result.chunks[0].title is None


@pytest.mark.asyncio
async def test_page_identity_score_kind_and_upstream_link_thread_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit's page identity, ``score_kind`` and upstream link parse (#3913).

    ``filename`` / ``breadcrumb`` / ``heading_path`` are what a citation title
    is derived from; ``score_kind`` names the score's direction;
    ``upstream_url`` / ``upstream_page`` are the backend's public link to the
    source and the page in the whole source document.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    link = "https://docs.example/guides/widget-guide.pdf#page=693"
    response = httpx.Response(
        200,
        json={
            "results": [
                {
                    "chunk_id": "c1",
                    "text": "Pools are capped at 64 per cluster.",
                    "source_uri": "gs://example-bucket/docs/widget-guide-part02of05.pdf",
                    "score": 0.31,
                    "score_kind": "distance",
                    "filename": "widget-guide-part02of05.pdf",
                    "breadcrumb": "Widget Guide > Planning",
                    "heading_path": ["Planning", "Pool limits"],
                    "upstream_url": link,
                    "upstream_page": 693,
                }
            ],
        },
    )
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    (chunk,) = (await search_corpus(_make_operator(), "q")).chunks

    assert chunk.filename == "widget-guide-part02of05.pdf"
    assert chunk.breadcrumb == "Widget Guide > Planning"
    assert chunk.heading_path == ["Planning", "Pool limits"]
    assert chunk.score_kind == "distance"
    assert chunk.upstream_url == link
    assert chunk.upstream_page == 693


def test_absent_page_identity_and_link_fields_read_as_absent() -> None:
    """A hit without the #3913 fields parses with empty / ``None`` values.

    ``score_kind`` stays ``None`` (never defaulted to a direction), so a
    caller cannot read a guessed direction into the score.
    """
    chunk = CorpusChunk.model_validate({"chunk_id": "c1", "text": "body"})

    assert (chunk.filename, chunk.breadcrumb, chunk.heading_path) == ("", "", [])
    assert chunk.score_kind is None
    assert chunk.upstream_url is None
    assert chunk.upstream_page is None


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        # upstream_url: only an http(s) URL naming a host is a link.
        ("upstream_url", "javascript:alert(1)", None),
        ("upstream_url", "gs://example-bucket/docs/guide.pdf", None),
        ("upstream_url", "ftp://docs.example/guide.pdf", None),
        ("upstream_url", "https://", None),
        ("upstream_url", "/relative/guide.pdf", None),
        ("upstream_url", "https://docs.example/a b.pdf", None),
        ("upstream_url", "https://docs.example/a\nb.pdf", None),
        ("upstream_url", "https://docs.example/" + "x" * 2100, None),
        ("upstream_url", "", None),
        ("upstream_url", 42, None),
        ("upstream_url", "  https://docs.example/kb/1  ", "https://docs.example/kb/1"),
        ("upstream_url", "http://docs.example/kb/1", "http://docs.example/kb/1"),
        # upstream_page: a whole number of at least 1.
        ("upstream_page", 0, None),
        ("upstream_page", -3, None),
        ("upstream_page", True, None),
        ("upstream_page", "12", None),
        ("upstream_page", 1.5, None),
        ("upstream_page", 1, 1),
        # score_kind: one of the two known directions.
        ("score_kind", "cosine", None),
        ("score_kind", "", None),
        ("score_kind", ["distance"], None),
        ("score_kind", "similarity", "similarity"),
        # Page identity: strings and string lists only.
        ("filename", 7, ""),
        ("breadcrumb", None, ""),
        ("heading_path", "Planning > Pool limits", []),
        ("heading_path", ["Planning", 3, None, "Pool limits"], ["Planning", "Pool limits"]),
    ],
)
def test_unusable_identity_and_link_values_read_as_absent(
    field: str, value: object, expected: object
) -> None:
    """An unusable optional value reads as absent instead of failing the parse.

    These fields label and link a hit; they never decide whether it is
    grounded, so a bad value must not turn a good search into a 503.
    """
    chunk = CorpusChunk.model_validate({"chunk_id": "c1", "text": "body", field: value})

    assert getattr(chunk, field) == expected


@pytest.mark.asyncio
async def test_unrecognized_envelope_fails_loud_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx whose body names neither ``chunks`` nor ``results`` fails loud (#1732).

    The dangerous silent-zero the old ``chunks: [] = default`` shape
    produced: a successful response carrying an unrecognised envelope must
    raise :class:`CorpusUnavailable` (→ 503), never parse to an empty hit
    list that reads as "no docs hits" from a healthy corpus.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing(
        [],
        # A 200 with hits under an unrecognised key — the exact silent-zero
        # shape #1732 is about.
        httpx.Response(200, json={"query": "q", "hits": [{"chunk_id": "c"}], "took_ms": 3}),
    )
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")


@pytest.mark.asyncio
async def test_operator_jwt_and_service_token_never_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the operator JWT nor the corpus service token appears in logs (#290).

    Capture surface (#1254 pattern, see ``docs/codebase/backend.md``): we
    bind a private :class:`structlog.testing.LogCapture` onto a
    freshly-wrapped logger and monkeypatch the subject module's
    module-level ``_log`` instead of using
    :func:`structlog.testing.capture_logs`. Production sets
    ``cache_logger_on_first_use=True`` (``logging.configure_logging``);
    once another test on the same xdist worker warms ``corpus._log`` and
    a later ``structlog.configure(...)`` replaces the processor-list
    instance, the cached ``BoundLogger`` is orphaned and ``capture_logs``
    silently misses every event — which would let the JWT-absence check
    below pass vacuously against an empty list. The private capture is
    process-local and contextvar-free, so it is immune to any concurrent
    ``configure`` regardless of xdist scheduling.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_service_token=_SERVICE_TOKEN)
    transport = _transport_capturing([], httpx.Response(503, text="down"))
    _patch_async_client(monkeypatch, transport, [])

    # Bind a private LogCapture onto a freshly-wrapped logger and patch the
    # subject module's ``_log`` rather than using
    # :func:`structlog.testing.capture_logs`. ``capture_logs`` only swaps the
    # process-global processors *list* — it leaves ``wrapper_class`` and the
    # ``cache_logger_on_first_use`` machinery untouched. Production
    # :func:`~meho_backplane.logging.configure_logging` (run by the FastAPI
    # lifespan in every app-booting test) sets ``cache_logger_on_first_use=True``,
    # which caches ``corpus._log``'s bound logger against the *then-current*
    # processors-list object; a later same-worker ``structlog.reset_defaults()``
    # / ``structlog.configure(...)`` (the observability / api_* per-file
    # fixtures) rebinds that list to a new object, orphaning the cache so
    # ``capture_logs`` can no longer intercept this module's events. Under
    # ``pytest-xdist --dist loadscope`` that co-location is order-dependent, so
    # the ``status==503`` capture here flaked whenever an app-booting module
    # shared the corpus worker. The private-logger pattern is process-local,
    # contextvar-free, and auto-restored on teardown — the same xdist-safe
    # capture shape already used in ``test_connector_registration.py`` /
    # ``test_operations_register_ingested.py``.
    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(structlog.PrintLogger(), processors=[capture])
    monkeypatch.setattr(corpus_mod, "_log", private_log)

    with pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")

    logs = capture.entries
    serialised = repr(logs)
    assert _JWT not in serialised
    assert _SERVICE_TOKEN not in serialised
    # The failure is still observable by status — this canary fails loudly
    # if the capture ever misses, so the secret-absence checks above cannot
    # pass vacuously against an empty list.
    assert any(event.get("status") == 503 for event in logs)


# ---------------------------------------------------------------------------
# corpus_status (T6 #1555 readiness transport)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("search_url", "expected"),
    [
        # The readiness URL is the search URL's host root + /readyz (#1732):
        # MEHO.Knowledge exposes /readyz, not a /status sibling.
        ("https://corpus.test/v1/search", "https://corpus.test/readyz"),
        ("https://corpus.test/v1/search/", "https://corpus.test/readyz"),
        ("https://corpus.test/corpus", "https://corpus.test/readyz"),
        ("https://corpus.test/search?x=1", "https://corpus.test/readyz"),
    ],
)
def test_derive_status_url(search_url: str, expected: str) -> None:
    """The readiness URL is the search URL's host root plus /readyz."""
    assert derive_status_url(search_url) == expected


@pytest.mark.asyncio
async def test_corpus_status_gets_status_url_with_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """corpus_status GETs the derived /readyz URL with the service token (#290)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_service_token=_SERVICE_TOKEN)
    captured: list[httpx.Request] = []
    response = httpx.Response(
        200,
        json={
            "index_built": True,
            "doc_count": 17000,
            "last_ingested_at": "2026-06-01T12:00:00Z",
        },
    )
    transport = _transport_capturing(captured, response)
    _patch_async_client(monkeypatch, transport, [])

    result = await corpus_status(_make_operator())

    assert isinstance(result, CorpusStatusResponse)
    assert result.index_built is True
    assert result.doc_count == 17000
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert str(captured[0].url) == derive_status_url(_CORPUS_URL)
    # The configured service token, not the operator JWT (#290).
    assert captured[0].headers["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert _JWT not in captured[0].headers.get("Authorization", "")


@pytest.mark.asyncio
async def test_corpus_status_readyz_200_without_flag_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare /readyz 200 (no readiness flag) reads as index_built=True (#1732).

    MEHO.Knowledge's /readyz returns a HealthResponse whose 200 *is* the
    ready signal; it need not carry an ``index_built`` field. The adapter
    must treat such a body as answerable rather than failing parse.
    """
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing([], httpx.Response(200, json={"status": "ok"}))
    _patch_async_client(monkeypatch, transport, [])

    result = await corpus_status(_make_operator())

    assert result.index_built is True
    assert result.doc_count is None


@pytest.mark.asyncio
async def test_corpus_status_readyz_ready_alias_false_is_not_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /readyz body advertising ``ready: false`` maps to index_built=False."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing([], httpx.Response(200, json={"ready": False}))
    _patch_async_client(monkeypatch, transport, [])

    result = await corpus_status(_make_operator())

    assert result.index_built is False


@pytest.mark.asyncio
async def test_corpus_status_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty corpus_url is unavailable, not silently 'no readiness'."""
    _pin_settings(monkeypatch, corpus_url="")
    with pytest.raises(CorpusUnavailable):
        await corpus_status(_make_operator())


@pytest.mark.asyncio
async def test_corpus_status_screens_endpoint_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness dial screens the endpoint the same way search does (#290)."""
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
    _pin_settings(monkeypatch, corpus_url="https://169.254.169.254/v1/search")

    with pytest.raises(CorpusUnavailable) as exc:
        await corpus_status(_make_operator())
    assert "not an allowed https public destination" in str(exc.value)


@pytest.mark.asyncio
async def test_corpus_status_non_2xx_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-2xx status collapses to CorpusUnavailable carrying the status."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing([], httpx.Response(500, text="boom"))
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable) as exc:
        await corpus_status(_make_operator())
    assert exc.value.status == 500
    # The corpus error body never leaks through the typed error.
    assert "boom" not in str(exc.value)


@pytest.mark.asyncio
async def test_corpus_status_malformed_body_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx body with a wrong-typed consumed field fails parse → unavailable."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    # ``doc_count`` is an optional int; a non-numeric string violates the
    # contract and must fail closed rather than silently degrade.
    transport = _transport_capturing([], httpx.Response(200, json={"doc_count": "lots"}))
    _patch_async_client(monkeypatch, transport, [])

    with pytest.raises(CorpusUnavailable):
        await corpus_status(_make_operator())


# ---------------------------------------------------------------------------
# ask_corpus (#3911 upstream grounded-answer transport)
# ---------------------------------------------------------------------------

_ANSWER_URL = "https://corpus.test/ask"

#: A minimal valid ``POST /ask?include=hits`` body (synthetic text).
_ANSWER_BODY: dict[str, object] = {
    "query": "q",
    "answer": "Widgets are pooled [0].",
    "citations": [{"chunk_index": 0, "chunk_id": "c1", "quote": "pooled"}],
    "timing": {"total_ms": 1200.0, "llm_ms": 800.0},
    "hits": [
        {
            "chunk_id": "c1",
            "document_id": "",
            "chunk_index": 4,
            "text": "Widgets are pooled per cluster.",
            "source_uri": "gs://example-bucket/docs/widgets.html",
            "filename": "widgets.html",
            "score": 0.42,
        }
    ],
}


@pytest.mark.parametrize(
    ("search_url", "expected"),
    [
        ("https://corpus.test/search", "https://corpus.test/ask"),
        ("https://corpus.test/v1/search", "https://corpus.test/v1/ask"),
        ("https://corpus.test/v1/search/", "https://corpus.test/v1/ask"),
        ("https://corpus.test:9443/search?x=1", "https://corpus.test:9443/ask"),
        ("https://corpus.test", "https://corpus.test/ask"),
    ],
)
def test_derive_answer_url(search_url: str, expected: str) -> None:
    """The answer URL replaces the search URL's last path segment with ``ask``."""
    assert derive_answer_url(search_url) == expected


@pytest.mark.asyncio
async def test_ask_corpus_posts_query_and_top_k_with_include_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer call carries ``{query, top_k}`` + ``include=hits`` and the service token.

    No ``with_rerank`` (ranking policy is the backend's) and, when the caller
    passes none, no ``filters`` / ``scope`` (the per-collection gates decide
    those, #3912) ride the body; the operator JWT is never forwarded.
    """
    _pin_settings(monkeypatch, corpus_service_token=_SERVICE_TOKEN)
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json=_ANSWER_BODY))
    _patch_async_client(monkeypatch, transport, [])

    result = await ask_corpus(_make_operator(), "q", limit=7, answer_url=_ANSWER_URL)

    assert isinstance(result, UpstreamAnswer)
    assert result.citations[0].chunk_id == "c1"
    assert result.hits[0].content == "Widgets are pooled per cluster."
    assert result.hits[0].filename == "widgets.html"
    assert result.timing.total_ms == 1200.0

    (request,) = captured
    assert request.method == "POST"
    assert request.url.path == "/ask"
    assert request.url.params.get("include") == "hits"
    import json

    body = json.loads(request.content.decode())
    assert body == {"query": "q", "top_k": 7}
    assert request.headers["authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert _JWT not in str(request.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "soft_scope", "raw_body"),
    [
        # Both gates off (None or empty): byte-for-byte the #3911 body, so a
        # backend that refuses unknown request keys sees what it always saw.
        pytest.param(None, None, b'{"query":"q","top_k":7}', id="off"),
        pytest.param({}, {}, b'{"query":"q","top_k":7}', id="off-empty"),
        pytest.param(
            {"product": "vsphere", "version": "8.0"},
            None,
            b'{"query":"q","top_k":7,"filters":{"product":"vsphere","version":"8.0"}}',
            id="filters",
        ),
        pytest.param(
            None,
            {"product": "vsphere", "version": "8.0.3.00400", "source": "caller"},
            b'{"query":"q","top_k":7,'
            b'"scope":{"product":"vsphere","version":"8.0.3.00400","source":"caller"}}',
            id="soft",
        ),
    ],
)
async def test_ask_corpus_sends_filters_and_soft_scope_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
    filters: dict[str, str] | None,
    soft_scope: dict[str, str] | None,
    raw_body: bytes,
) -> None:
    """``filters`` / ``soft_scope`` ride the answer body as ``filters`` / ``scope`` (#3912).

    Each is omitted when ``None`` or empty; the values are passed through
    unchanged. The raw request bytes are pinned, so the gate-off body is
    exactly the one the answer call sent before the gates were wired.
    """
    _pin_settings(monkeypatch, corpus_audience="")
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json=_ANSWER_BODY))
    _patch_async_client(monkeypatch, transport, [])

    await ask_corpus(
        _make_operator(),
        "q",
        filters=filters,
        soft_scope=soft_scope,
        limit=7,
        answer_url=_ANSWER_URL,
    )

    (request,) = captured
    assert request.content == raw_body
    assert request.url.params.get("include") == "hits"


@pytest.mark.asyncio
async def test_ask_corpus_forwards_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured audience rides the body, as on search."""
    _pin_settings(monkeypatch, corpus_audience="meho-corpus")
    captured: list[httpx.Request] = []
    transport = _transport_capturing(captured, httpx.Response(200, json=_ANSWER_BODY))
    _patch_async_client(monkeypatch, transport, [])

    await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)

    import json

    assert json.loads(captured[0].content.decode())["audience"] == "meho-corpus"


def test_answer_timeout_setting_defaults_to_60(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CORPUS_ANSWER_TIMEOUT_SECONDS`` defaults to 60 s, independent of search's 10 s."""
    monkeypatch.delenv("CORPUS_ANSWER_TIMEOUT_SECONDS", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.corpus_answer_timeout_seconds == 60.0
    assert settings.corpus_timeout_seconds == 10.0

    monkeypatch.setenv("CORPUS_ANSWER_TIMEOUT_SECONDS", "45")
    get_settings.cache_clear()
    assert get_settings().corpus_answer_timeout_seconds == 45.0


@pytest.mark.asyncio
async def test_ask_corpus_timeout_is_the_answer_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer client is bounded by the answer timeout, not the search one."""
    _pin_settings(monkeypatch, corpus_timeout_seconds=10.0, corpus_answer_timeout_seconds=42.0)
    captured_timeout: list[httpx.Timeout] = []
    transport = _transport_capturing([], httpx.Response(200, json=_ANSWER_BODY))
    _patch_async_client(monkeypatch, transport, captured_timeout)

    await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)

    assert captured_timeout[0].read == 42.0
    assert captured_timeout[0].connect == 42.0


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://corpus.test/ask",  # plaintext
        "https://127.0.0.1/ask",  # loopback
        "https://169.254.169.254/ask",  # cloud metadata
    ],
)
@pytest.mark.asyncio
async def test_ask_corpus_screens_endpoint_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """The answer dial runs the same SSRF screen as search, before any request."""
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
    _pin_settings(monkeypatch)

    with pytest.raises(CorpusUnavailable) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=bad_url)
    assert "not an allowed https public destination" in str(exc.value)


@pytest.mark.asyncio
async def test_ask_corpus_unconfigured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No answer URL is the unavailable arm, never an empty answer."""
    _pin_settings(monkeypatch)
    with pytest.raises(CorpusUnavailable):
        await ask_corpus(_make_operator(), "q", answer_url=None)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("too slow"),
        httpx.ConnectError("connection refused"),
    ],
)
@pytest.mark.asyncio
async def test_ask_corpus_transport_failure_is_corpus_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: httpx.HTTPError
) -> None:
    """A timeout or connect failure is the transport arm (``CorpusUnavailable``)."""
    _pin_settings(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        raise error

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler), [])

    with pytest.raises(CorpusUnavailable) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)
    assert exc.value.status is None


@pytest.mark.parametrize(
    ("response", "kind", "retry_after"),
    [
        (
            httpx.Response(503, json={"error": {"code": "llm_unavailable", "message": "x"}}),
            CorpusAnswerError.KIND_ANSWER_UNAVAILABLE,
            None,
        ),
        (
            httpx.Response(
                503,
                json={"error": {"code": "llm_rate_limited", "message": "x"}},
                headers={"Retry-After": "5"},
            ),
            CorpusAnswerError.KIND_RATE_LIMITED,
            5,
        ),
        (
            httpx.Response(429, text="slow down", headers={"Retry-After": "3"}),
            CorpusAnswerError.KIND_RATE_LIMITED,
            3,
        ),
        (httpx.Response(503, text="proxy unavailable"), CorpusAnswerError.KIND_SERVER_ERROR, None),
        (httpx.Response(500, json={"detail": "boom"}), CorpusAnswerError.KIND_SERVER_ERROR, None),
        (httpx.Response(422, json={"detail": "bad"}), CorpusAnswerError.KIND_REJECTED, None),
        (httpx.Response(401, text="no"), CorpusAnswerError.KIND_REJECTED, None),
        (httpx.Response(404, text="no such route"), CorpusAnswerError.KIND_REJECTED, None),
    ],
)
@pytest.mark.asyncio
async def test_ask_corpus_non_2xx_is_typed_by_kind(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    kind: str,
    retry_after: int | None,
) -> None:
    """Each non-2xx outcome is a typed ``CorpusAnswerError`` with status + kind."""
    _pin_settings(monkeypatch)
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    with pytest.raises(CorpusAnswerError) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)
    assert exc.value.kind == kind
    assert exc.value.status == response.status_code
    assert exc.value.retry_after == retry_after


@pytest.mark.asyncio
async def test_ask_corpus_error_never_echoes_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the upstream body nor an unknown error code reaches the error.

    Logs are captured through a private :class:`structlog.testing.LogCapture`
    patched onto ``corpus._log`` (the #1254 pattern, see
    ``test_operator_jwt_and_service_token_never_logged``), never a bare
    ``capture_logs``, which misses a cached, orphaned module logger and would
    let the absence check pass against an empty list.
    """
    _pin_settings(monkeypatch)
    secret = "INTERNAL stack trace leaky-token-abc"
    response = httpx.Response(503, json={"error": {"code": secret, "message": secret}})
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])
    capture = structlog.testing.LogCapture()
    private_log = structlog.wrap_logger(structlog.PrintLogger(), processors=[capture])
    monkeypatch.setattr(corpus_mod, "_log", private_log)

    with pytest.raises(CorpusAnswerError) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)
    logs = capture.entries
    assert secret not in str(exc.value)
    assert secret not in repr(logs)
    # Canary: the failure is logged (by status + kind, not by body), so the
    # absence check above cannot pass vacuously against an empty capture.
    failed = [e for e in logs if e["event"] == "corpus_answer_request_failed"]
    assert len(failed) == 1
    assert failed[0]["status"] == 503
    assert failed[0]["kind"] == CorpusAnswerError.KIND_SERVER_ERROR


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"answer": "x", "citations": []}),  # no hits
        httpx.Response(200, json={"answer": "", "citations": [], "hits": []}),  # blank answer
        httpx.Response(200, json={"answer": "x", "hits": [{"chunk_id": "c"}]}),  # hit w/o text
    ],
)
@pytest.mark.asyncio
async def test_ask_corpus_malformed_2xx_is_typed_malformed(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    """A 2xx body that does not match the answer shape is ``malformed``, not empty."""
    _pin_settings(monkeypatch)
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    with pytest.raises(CorpusAnswerError) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)
    assert exc.value.kind == CorpusAnswerError.KIND_MALFORMED


def test_retry_after_http_date_is_converted_to_seconds() -> None:
    """An HTTP-date ``Retry-After`` becomes the seconds remaining (never negative)."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=120), usegmt=True)
    assert 100 <= (corpus_mod._retry_after_seconds(future) or 0) <= 120
    assert corpus_mod._retry_after_seconds(past) == 0
    assert corpus_mod._retry_after_seconds("soon") is None
    assert corpus_mod._retry_after_seconds(None) is None


def test_retry_after_http_date_is_capped() -> None:
    """An HTTP-date far ahead clamps to the cap; one that overflows is ``None``."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    far = format_datetime(datetime.now(UTC) + timedelta(days=2), usegmt=True)
    assert corpus_mod._retry_after_seconds(far) == corpus_mod._RETRY_AFTER_MAX_S == 3600
    # A year beyond a C long overflows ``datetime``: dropped, never raised.
    assert corpus_mod._retry_after_seconds("Mon, 01 Jan 99999999999999999999 00:00:00 GMT") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5),
        (" 7 ", 7),
        ("0", 0),
        ("3600", 3600),
        ("3601", 3600),  # over the cap: clamped
        ("9" * 4000, 3600),  # within int()'s digit limit, still clamped
        ("9" * 5000, 3600),  # beyond int()'s 4300-digit limit: clamped, never raised
        ("0" * 5000 + "5", 5),  # leading zeros are not magnitude
        ("\u00b2", None),  # superscript two: isdigit() but not an int() digit
        ("\uff11\uff12", None),  # fullwidth digits: not delta-seconds (ASCII by grammar)
        ("-5", None),
        ("+5", None),
        ("1.5", None),
        ("", None),
        ("   ", None),
    ],
    ids=[
        "five",
        "padded",
        "zero",
        "at-cap",
        "over-cap",
        "4000-digits",
        "5000-digits",
        "leading-zeros",
        "superscript-two",
        "fullwidth",
        "negative",
        "signed",
        "fraction",
        "empty",
        "blank",
    ],
)
def test_retry_after_delta_seconds_is_ascii_bounded_and_capped(
    raw: str, expected: int | None
) -> None:
    """delta-seconds: ASCII digits only, capped at 3600 s; anything else is ``None``."""
    assert corpus_mod._retry_after_seconds(raw) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"\xb2", None),  # httpx decodes the raw byte as Latin-1 U+00B2 (superscript two)
        (b"9" * 5000, 3600),
        (b"86400", 3600),
    ],
    ids=["raw-0xB2", "5000-digits", "over-cap"],
)
@pytest.mark.asyncio
async def test_ask_corpus_garbled_retry_after_stays_typed_rate_limited(
    monkeypatch: pytest.MonkeyPatch, header: bytes, expected: int | None
) -> None:
    """A garbled or huge ``Retry-After`` on a 429 keeps the typed rate-limited error.

    The header is parsed inside the error classification, so a parse that
    raised would surface as an unclassified 500 / ``-32603`` instead of the
    typed 503 ``upstream_rate_limited``. It is dropped (``None``) or clamped.
    """
    _pin_settings(monkeypatch)
    response = httpx.Response(429, text="slow down", headers=[(b"retry-after", header)])
    _patch_async_client(monkeypatch, _transport_capturing([], response), [])

    with pytest.raises(CorpusAnswerError) as exc:
        await ask_corpus(_make_operator(), "q", answer_url=_ANSWER_URL)
    assert exc.value.kind == CorpusAnswerError.KIND_RATE_LIMITED
    assert exc.value.status == 429
    assert exc.value.retry_after == expected
