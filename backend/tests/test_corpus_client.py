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

import meho_backplane.auth.corpus as corpus_mod
from meho_backplane.auth.corpus import (
    CorpusSearchResponse,
    CorpusUnavailable,
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
    response = httpx.Response(
        200,
        json={
            "chunks": [
                {
                    "chunk_id": "c1",
                    "document_id": "d1",
                    "content": "vSphere 9.0 supervisor cluster setup.",
                    "source_url": "https://docs.example/vsphere",
                    "score": 0.91,
                    "metadata": {"product": "vmware", "version": "9.0"},
                }
            ]
        },
    )
    transport = _transport_capturing(captured, response)
    _patch_async_client(monkeypatch, transport, [])

    result = await search_corpus(_make_operator(), "supervisor cluster", limit=5)

    assert isinstance(result, CorpusSearchResponse)
    assert len(result.chunks) == 1
    assert result.chunks[0].chunk_id == "c1"
    assert result.chunks[0].metadata == {"product": "vmware", "version": "9.0"}

    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == _CORPUS_URL
    assert sent.headers["Authorization"] == f"Bearer {_JWT}"
    import json

    body = json.loads(sent.content.decode())
    assert body["query"] == "supervisor cluster"
    assert body["limit"] == 5


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
async def test_forwarded_jwt_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forwarded operator JWT must not appear in any structlog event."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    transport = _transport_capturing([], httpx.Response(503, text="down"))
    _patch_async_client(monkeypatch, transport, [])

    with structlog.testing.capture_logs() as logs, pytest.raises(CorpusUnavailable):
        await search_corpus(_make_operator(), "q")

    serialised = repr(logs)
    assert _JWT not in serialised
    # The failure is still observable by status.
    assert any(event.get("status") == 503 for event in logs)
