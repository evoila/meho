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
    CorpusSearchResponse,
    CorpusStatusResponse,
    CorpusUnavailable,
    corpus_status,
    derive_status_url,
    search_corpus,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.settings import Settings, get_settings

_CORPUS_URL = "https://corpus.test/search"
_JWT = "header.payload.signature-secret"


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
async def test_forwards_bearer_jwt_and_posts_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client POSTs to corpus_url with Authorization: Bearer <raw_jwt>."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
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
                    "metadata": {"product": "vmware", "version": "9.0"},
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
    assert result.chunks[0].metadata == {"product": "vmware", "version": "9.0"}

    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == _CORPUS_URL
    assert sent.headers["Authorization"] == f"Bearer {_JWT}"
    import json

    body = json.loads(sent.content.decode())
    assert body["query"] == "supervisor cluster"
    # The corpus reads ``top_k``, not ``limit`` (#1732).
    assert body["top_k"] == 5
    assert "limit" not in body


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
        metadata_filters={"product": "vmware", "version": "9.0"},
    )

    import json

    body = json.loads(captured[0].content.decode())
    assert body["metadata_filters"] == {"product": "vmware", "version": "9.0"}
    assert body["audience"] == "meho-corpus"


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
async def test_forwarded_jwt_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forwarded operator JWT must not appear in any structlog event."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
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
    # The failure is still observable by status.
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
    """corpus_status GETs the derived /readyz URL forwarding the operator JWT."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
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
    assert captured[0].headers["Authorization"] == f"Bearer {_JWT}"


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
