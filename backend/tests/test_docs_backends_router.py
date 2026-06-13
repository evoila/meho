# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the backend-agnostic search router (G4.6-T2 #1551).

Three things are proven here:

1. **The router is type-agnostic.** A fake backend registered under a
   novel ``backend_type`` is selected purely by ``collection.backend.type``
   — the router never special-cases the shipped adapter (AC4).
2. **Fail-closed routing.** An unknown / malformed ``backend.type`` →
   :class:`~meho_backplane.auth.corpus.CorpusUnavailable` (the existing
   503 arm, no new taxonomy) (AC1). ``resolve_backend_or_label`` returns
   the non-raising ``(impl, label, msg)`` sibling shape.
3. **The re-homed adapter is behaviourally identical to today's
   ``search_corpus``.** ``CorpusHttpBackend`` forwards the operator JWT,
   bounds the timeout, fails closed, and reads its endpoint / audience
   from the collection's ``backend.ref`` (legacy ``corpus_url`` fallback)
   (AC2). The exhaustive transport assertions stay in
   ``test_corpus_client``; here we assert the adapter is a faithful,
   ref-aware delegate.

The ``search_docs`` seam routing (``collection`` → ``resolve_backend`` →
``backend.search``) is covered at the bottom: a collection with a fake
backend makes ``search_docs`` return that backend's chunks, and the
backend id never appears in the projected result (AC3).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

import meho_backplane.auth.corpus as corpus_mod
from meho_backplane.auth.corpus import (
    CorpusChunk,
    CorpusSearchResponse,
    CorpusUnavailable,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.docs_collections import DocCollection
from meho_backplane.docs_search import resolve_backend, resolve_backend_or_label, search_docs
from meho_backplane.docs_search.backends import (
    CORPUS_HTTP_BACKEND_TYPE,
    CorpusHttpBackend,
    SearchBackend,
    all_backends,
    get_backend,
    register_backend,
)
from meho_backplane.docs_search.backends import registry as registry_mod
from meho_backplane.docs_search.service import DocsScope
from meho_backplane.settings import Settings, get_settings

_JWT = "header.payload.signature-secret"
_CORPUS_URL = "https://corpus.test/search"


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


def _make_collection(
    *,
    backend: dict[str, Any],
    collection_key: str = "vmware",
) -> DocCollection:
    """Build a frozen :class:`DocCollection` read shape with *backend*.

    Only ``backend`` and ``collection_key`` matter for routing; the rest
    are filled with valid placeholders so the frozen model validates.
    """
    now = datetime.now(UTC)
    return DocCollection(
        id=uuid4(),
        tenant_id=None,
        collection_key=collection_key,
        vendor="vmware",
        products=("vsphere",),
        description=None,
        when_to_use=None,
        backend=backend,
        status="ready",
        last_ingested_at=None,
        doc_count=None,
        readiness=None,
        extras={},
        created_at=now,
        updated_at=now,
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


@pytest.fixture
def _restore_registry() -> Iterator[None]:
    """Snapshot the backend registry and restore it after the test.

    Tests that register a fake backend must not leak it into the
    process-wide registry the other tests (and the seam) read.
    """
    snapshot = all_backends()
    yield
    registry_mod._BACKENDS.clear()
    registry_mod._BACKENDS.update(snapshot)


def _pin_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """Override ``corpus.get_settings`` with a Settings carrying *overrides*."""
    settings = get_settings().model_copy(update=overrides)
    monkeypatch.setattr(corpus_mod, "get_settings", lambda: settings)
    return settings


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
    captured_timeout: list[httpx.Timeout] | None = None,
) -> None:
    """Force every ``AsyncClient`` the corpus module builds onto *transport*."""
    real_async_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        timeout = kwargs.get("timeout")
        if captured_timeout is not None and isinstance(timeout, httpx.Timeout):
            captured_timeout.append(timeout)
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(corpus_mod.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------
# The shipped adapter is registered (AC2 / AC4 baseline)
# ---------------------------------------------------------------------------


def test_corpus_http_adapter_is_registered_by_default() -> None:
    """The ``corpus-http`` adapter self-registers at import time."""
    impl = get_backend(CORPUS_HTTP_BACKEND_TYPE)
    assert isinstance(impl, CorpusHttpBackend)
    assert impl.backend_type == CORPUS_HTTP_BACKEND_TYPE


def test_registry_rejects_duplicate_type(_restore_registry: None) -> None:
    """Re-registering a type is a programming bug → RuntimeError."""
    with pytest.raises(RuntimeError):
        register_backend(CORPUS_HTTP_BACKEND_TYPE, CorpusHttpBackend())


def test_registry_rejects_mismatched_advertised_type(_restore_registry: None) -> None:
    """An impl whose ``backend_type`` disagrees with the key is rejected."""
    with pytest.raises(TypeError):
        register_backend("some-other-type", CorpusHttpBackend())


# ---------------------------------------------------------------------------
# Routing is type-agnostic (AC4)
# ---------------------------------------------------------------------------


class _FakeBackend(SearchBackend):
    """A stand-in backend selected purely by its registered type."""

    backend_type = "fake-rag"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        operator: Operator,
        query: str,
        *,
        backend_ref: Any = None,
        metadata_filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        self.calls.append(
            {
                "operator": operator,
                "query": query,
                "backend_ref": backend_ref,
                "metadata_filters": metadata_filters,
                "limit": limit,
            }
        )
        return CorpusSearchResponse(
            chunks=[
                CorpusChunk(chunk_id="f1", document_id="fd1", content="the fake answer"),
            ]
        )


def test_router_selects_backend_purely_by_type(_restore_registry: None) -> None:
    """AC4: a fake backend is chosen solely by ``collection.backend.type``."""
    fake = _FakeBackend()
    register_backend(_FakeBackend.backend_type, fake)

    collection = _make_collection(
        backend={"type": "fake-rag", "ref": {"endpoint": "https://fake.test"}},
    )
    resolved = resolve_backend(collection)

    assert resolved.backend is fake
    assert resolved.ref == {"endpoint": "https://fake.test"}


def test_router_label_form_routes(_restore_registry: None) -> None:
    """The non-raising ``(impl, label, msg)`` shape returns the adapter."""
    fake = _FakeBackend()
    register_backend(_FakeBackend.backend_type, fake)
    collection = _make_collection(backend={"type": "fake-rag", "ref": None})

    impl, label, msg = resolve_backend_or_label(collection)

    assert impl is not None
    assert impl.backend is fake
    assert label is None
    assert msg is None


def test_legacy_none_collection_routes_to_corpus_http() -> None:
    """``collection=None`` (unmigrated deploy) → the corpus-http adapter, no ref."""
    resolved = resolve_backend(None)
    assert isinstance(resolved.backend, CorpusHttpBackend)
    assert resolved.ref is None


# ---------------------------------------------------------------------------
# Fail-closed routing (AC1)
# ---------------------------------------------------------------------------


def test_unknown_backend_type_raises_corpus_unavailable() -> None:
    """AC1: an unregistered ``backend.type`` → CorpusUnavailable (503 arm)."""
    collection = _make_collection(backend={"type": "no-such-backend", "ref": None})
    with pytest.raises(CorpusUnavailable):
        resolve_backend(collection)


def test_unknown_backend_type_label_form() -> None:
    """The label form reports ``unknown_backend`` without raising."""
    collection = _make_collection(backend={"type": "no-such-backend", "ref": None})
    impl, label, msg = resolve_backend_or_label(collection)
    assert impl is None
    assert label == "unknown_backend"
    assert msg is not None and "no-such-backend" in msg


def test_missing_backend_type_raises_corpus_unavailable() -> None:
    """A routing record without a ``type`` is unroutable → CorpusUnavailable."""
    collection = _make_collection(backend={"ref": {"endpoint": "https://x.test"}})
    with pytest.raises(CorpusUnavailable):
        resolve_backend(collection)


def test_blank_backend_type_is_unroutable() -> None:
    """An empty-string ``type`` is treated as missing (not a registry key)."""
    collection = _make_collection(backend={"type": "", "ref": None})
    impl, label, _msg = resolve_backend_or_label(collection)
    assert impl is None
    assert label == "unknown_backend"


# ---------------------------------------------------------------------------
# The re-homed adapter is behaviourally identical to search_corpus (AC2)
# ---------------------------------------------------------------------------


async def test_adapter_forwards_jwt_and_uses_backend_ref_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the adapter forwards the JWT and POSTs to the ref's endpoint."""
    # Legacy global is a different URL; the ref must win.
    _pin_settings(monkeypatch, corpus_url="https://legacy.test/search")
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"chunks": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler))

    adapter = CorpusHttpBackend()
    await adapter.search(
        _make_operator(),
        "supervisor cluster",
        backend_ref={"endpoint": _CORPUS_URL, "audience": "meho-corpus"},
        metadata_filters={"product": "vmware", "version": "9.0"},
        limit=5,
    )

    sent = captured[0]
    assert str(sent.url) == _CORPUS_URL  # ref endpoint, not the legacy global
    assert sent.headers["Authorization"] == f"Bearer {_JWT}"
    import json

    body = json.loads(sent.content.decode())
    assert body["query"] == "supervisor cluster"
    # The corpus reads ``top_k``, not ``limit`` (#1732).
    assert body["top_k"] == 5
    assert body["metadata_filters"] == {"product": "vmware", "version": "9.0"}
    assert body["audience"] == "meho-corpus"


async def test_adapter_falls_back_to_legacy_corpus_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ref without an endpoint falls back to the legacy ``corpus_url``."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_audience="legacy-aud")
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"chunks": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler))

    adapter = CorpusHttpBackend()
    await adapter.search(_make_operator(), "q", backend_ref=None)

    sent = captured[0]
    assert str(sent.url) == _CORPUS_URL
    import json

    body = json.loads(sent.content.decode())
    assert body["audience"] == "legacy-aud"


async def test_adapter_unconfigured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ref endpoint AND no legacy corpus_url → CorpusUnavailable (fail-closed)."""
    _pin_settings(monkeypatch, corpus_url="")
    adapter = CorpusHttpBackend()
    with pytest.raises(CorpusUnavailable):
        await adapter.search(_make_operator(), "q", backend_ref=None)


async def test_adapter_bounds_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter inherits the bounded corpus timeout (no unbounded hang)."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL, corpus_timeout_seconds=3.5)
    captured_timeout: list[httpx.Timeout] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chunks": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler), captured_timeout)

    await CorpusHttpBackend().search(_make_operator(), "q", backend_ref=None)

    assert captured_timeout
    assert captured_timeout[0].read == 3.5


async def test_adapter_blank_ref_endpoint_uses_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ref carrying an empty endpoint does not mask the legacy fallback."""
    _pin_settings(monkeypatch, corpus_url=_CORPUS_URL)
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"chunks": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler))

    await CorpusHttpBackend().search(_make_operator(), "q", backend_ref={"endpoint": "   "})

    assert str(captured[0].url) == _CORPUS_URL


async def test_base_probe_seam_fails_loudly_when_unimplemented() -> None:
    """An adapter that does not override ``probe`` raises, never claims ready.

    T6 (#1555) implements ``probe`` on ``CorpusHttpBackend``; the base
    seam still fails loudly for an adapter (here ``_FakeBackend``) that
    has not gained a liveness check, so it can never silently report
    "ready" — the contract the base default guards.
    """
    with pytest.raises(NotImplementedError):
        await _FakeBackend().probe(_make_operator())


# ---------------------------------------------------------------------------
# The search_docs seam routes through the backend (AC3)
# ---------------------------------------------------------------------------


async def test_search_docs_routes_through_resolved_backend(_restore_registry: None) -> None:
    """AC3: search_docs with a collection returns the routed backend's chunks.

    The backend id never appears in the projected result — the seam holds
    the backend-agnostic contract.
    """
    fake = _FakeBackend()
    register_backend(_FakeBackend.backend_type, fake)
    collection = _make_collection(
        backend={"type": "fake-rag", "ref": {"endpoint": "https://fake.test"}},
    )

    result = await search_docs(
        _make_operator(),
        "how do I configure NSX",
        scope=DocsScope(collection_key="vmware", product="vmware", version="9.0"),
        limit=7,
        collection=collection,
    )

    # Routed to the fake backend with the collection's ref + scope filters.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["backend_ref"] == {"endpoint": "https://fake.test"}
    assert call["metadata_filters"] == {"product": "vmware", "version": "9.0"}
    assert call["limit"] == 7

    # The projected result carries the fake backend's chunk, and neither
    # the backend type nor the routing record leaks into the DocsChunk
    # surface (the backend-agnostic contract).
    assert len(result.chunks) == 1
    assert result.chunks[0].content == "the fake answer"
    serialised = result.model_dump_json()
    assert "fake-rag" not in serialised
    assert "fake.test" not in serialised
    assert "backend_type" not in serialised
    assert set(result.chunks[0].model_dump()) == {
        "chunk_id",
        "document_id",
        "content",
        "source_url",
        "score",
        "collection",
    }
    # The single-collection path leaves the provenance tag unset (the
    # collection is already implied by the request scope); it is only
    # populated on the cross-collection fan-out path (T5 #1554).
    assert result.chunks[0].collection is None


async def test_search_docs_unroutable_collection_raises(_restore_registry: None) -> None:
    """A collection routing to an unregistered backend → CorpusUnavailable (503)."""
    collection = _make_collection(backend={"type": "ghost-backend", "ref": None})
    with pytest.raises(CorpusUnavailable):
        await search_docs(
            _make_operator(),
            "q",
            scope=DocsScope(collection_key="vmware", product="vmware", version="9.0"),
            collection=collection,
        )
