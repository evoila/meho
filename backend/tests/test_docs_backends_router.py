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
backend id never appears in the projected result (AC3). The per-collection
scope gates (#3912) are covered there too: product/version reach the
backend as a soft ``scope`` only when the collection's
``backend.ref["scope"]`` is ``"soft"``, as ``metadata_filters`` only when
its ``backend.ref["scope_filters"]`` is ``true`` (the soft gate wins when
both are set), and not at all otherwise, down to the exact corpus request
body; the ``docs_search_completed`` log keeps the requested values and
names how they were sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import structlog

import meho_backplane.auth.corpus as corpus_mod
import meho_backplane.docs_search.service as service_mod
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
from meho_backplane.docs_search.service import DocsScope, ForwardedScope, forwarded_scope
from meho_backplane.settings import Settings, get_settings

_JWT = "header.payload.signature-secret"
_CORPUS_URL = "https://corpus.test/search"
#: Deployment-configured corpus service credential (#290) — the bearer the
#: adapter presents, never the caller's operator JWT.
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
        soft_scope: dict[str, str] | None = None,
        limit: int = 10,
    ) -> CorpusSearchResponse:
        self.calls.append(
            {
                "operator": operator,
                "query": query,
                "backend_ref": backend_ref,
                "metadata_filters": metadata_filters,
                "soft_scope": soft_scope,
                "limit": limit,
            }
        )
        return CorpusSearchResponse(
            chunks=[
                CorpusChunk(
                    chunk_id="f1",
                    document_id="fd1",
                    title="The Fake Answer Guide",
                    content="the fake answer",
                ),
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


async def test_adapter_uses_service_token_and_backend_ref_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: the adapter presents the configured service token (never the JWT, #290)
    and POSTs to the ref's endpoint."""
    # Legacy global is a different URL; the ref must win.
    _pin_settings(
        monkeypatch,
        corpus_url="https://legacy.test/search",
        corpus_service_token=_SERVICE_TOKEN,
    )
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
    # The deployment-configured service token is the bearer, not the JWT (#290).
    assert sent.headers["Authorization"] == f"Bearer {_SERVICE_TOKEN}"
    assert _JWT not in sent.headers.get("Authorization", "")
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
    ref = {"endpoint": "https://fake.test", "scope_filters": True}
    collection = _make_collection(backend={"type": "fake-rag", "ref": ref})

    result = await search_docs(
        _make_operator(),
        "how do I configure NSX",
        scope=DocsScope(collection_key="vmware", product="nsx", version="9.0"),
        limit=7,
        collection=collection,
    )

    # Routed to the fake backend with the collection's ref + scope filters
    # (the collection enables them, #3912).
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["backend_ref"] == ref
    assert call["metadata_filters"] == {"product": "nsx", "version": "9.0"}
    assert call["soft_scope"] is None
    assert call["limit"] == 7

    # The projected result carries the fake backend's chunk, and neither
    # the backend type nor the routing record leaks into the DocsChunk
    # surface (the backend-agnostic contract).
    assert len(result.chunks) == 1
    assert result.chunks[0].content == "the fake answer"
    # #2475: an upstream chunk title threads through the projection.
    assert result.chunks[0].title == "The Fake Answer Guide"
    serialised = result.model_dump_json()
    assert "fake-rag" not in serialised
    assert "fake.test" not in serialised
    assert "backend_type" not in serialised
    assert set(result.chunks[0].model_dump()) == {
        "chunk_id",
        "document_id",
        "title",
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


# ---------------------------------------------------------------------------
# The per-collection scope gates (#3912)
# ---------------------------------------------------------------------------

#: A full release, sent unchanged by both gates (normalising it is the
#: backend's job).
_SCOPE = DocsScope(collection_key="vmware", product="vsphere", version="8.0.3.00400")
_REQUESTED = {"product": "vsphere", "version": "8.0.3.00400"}
_SOFT = {"product": "vsphere", "version": "8.0.3.00400", "source": "caller"}


@pytest.mark.parametrize(
    "ref",
    [
        None,
        {},
        {"endpoint": "https://fake.test"},
        {"scope_filters": False},
        # Only the JSON boolean enables the filter gate; a truthy look-alike does not.
        {"scope_filters": "true"},
        {"scope_filters": 1},
        # Only the exact string "soft" enables the soft gate.
        {"scope": "SOFT"},
        {"scope": " soft"},
        {"scope": "hard"},
        {"scope": True},
        {"scope": {"mode": "soft"}},
        {"scope": None},
    ],
)
def test_forwarded_scope_off_unless_a_gate_is_on(ref: dict[str, Any] | None) -> None:
    """Absent or look-alike gate values forward nothing: neither scope nor filters."""
    assert forwarded_scope(_SCOPE, ref) == ForwardedScope(mode="none", filters={}, soft_scope={})


def test_forwarded_scope_filters_gate_forwards_refinements_unchanged() -> None:
    """``scope_filters: true`` (no ``scope`` key) forwards ``as_filters()`` verbatim."""
    ref = {"endpoint": "https://fake.test", "scope_filters": True}
    assert forwarded_scope(_SCOPE, ref) == ForwardedScope(mode="filters", filters=_REQUESTED)


def test_forwarded_scope_soft_gate_sends_values_unchanged_and_no_filters() -> None:
    """``scope: "soft"`` sends the scope object with the values as given, no filters."""
    ref = {"endpoint": "https://fake.test", "scope": "soft"}
    forwarded = forwarded_scope(_SCOPE, ref)
    assert forwarded == ForwardedScope(mode="soft", soft_scope=_SOFT)
    assert forwarded.filters == {}


def test_forwarded_scope_soft_wins_when_both_gates_are_set() -> None:
    """Both gates set: the soft scope is sent and the filters are not."""
    ref = {"scope": "soft", "scope_filters": True}
    assert forwarded_scope(_SCOPE, ref) == ForwardedScope(mode="soft", soft_scope=_SOFT)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (
            DocsScope(collection_key="vmware", product="vsphere"),
            {"product": "vsphere", "source": "caller"},
        ),
        (
            DocsScope(collection_key="vmware", version="9.1.1"),
            {"version": "9.1.1", "source": "caller"},
        ),
        (
            DocsScope(collection_key="vmware", version="8.0 U3"),
            {"version": "8.0 U3", "source": "caller"},
        ),
    ],
)
def test_forwarded_scope_soft_omits_keys_not_given(
    scope: DocsScope, expected: dict[str, str]
) -> None:
    """The soft scope carries only the keys the caller gave, unchanged."""
    assert forwarded_scope(scope, {"scope": "soft"}).soft_scope == expected


@pytest.mark.parametrize(
    "ref",
    [{"scope": "soft"}, {"scope_filters": True}, {"scope": "soft", "scope_filters": True}],
)
def test_forwarded_scope_nothing_requested_sends_nothing(ref: dict[str, Any]) -> None:
    """No product/version requested → nothing forwarded, whatever the gates."""
    assert forwarded_scope(DocsScope(collection_key="vmware"), ref) == ForwardedScope()


_GATE_CASES = [
    # (ref, expected metadata_filters, expected soft_scope, scope_forwarded)
    pytest.param({"endpoint": "https://fake.test"}, None, None, "none", id="off"),
    pytest.param(
        {"endpoint": "https://fake.test", "scope_filters": True},
        _REQUESTED,
        None,
        "filters",
        id="filters",
    ),
    pytest.param(
        {"endpoint": "https://fake.test", "scope": "soft"}, None, _SOFT, "soft", id="soft"
    ),
    pytest.param(
        {"endpoint": "https://fake.test", "scope": "soft", "scope_filters": True},
        None,
        _SOFT,
        "soft",
        id="both-soft-wins",
    ),
]


@pytest.mark.parametrize(
    ("ref", "expected_filters", "expected_scope", "expected_mode"), _GATE_CASES
)
async def test_search_docs_gates_control_forwarding_and_log_keeps_request(
    _restore_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    ref: dict[str, Any],
    expected_filters: dict[str, str] | None,
    expected_scope: dict[str, str] | None,
    expected_mode: str,
) -> None:
    """The gates decide what reaches the backend; the log records the request.

    Gate off: the backend gets neither ``metadata_filters`` nor a soft
    scope. ``scope_filters`` on: the refinements as filters. ``scope``
    ``"soft"`` on (alone or with ``scope_filters``): the soft scope and no
    filters. Either way ``docs_search_completed`` carries the requested
    product/version, plus ``scope_forwarded`` naming how they were sent.
    """
    # Rebind a fresh proxy so ``capture_logs`` sees the event whatever an
    # earlier test in this worker did to the cached module logger (the
    # ``cache_logger_on_first_use`` hazard; see test_operations_ingest_jobs).
    monkeypatch.setattr(service_mod, "_log", structlog.get_logger(service_mod.__name__))
    fake = _FakeBackend()
    register_backend(_FakeBackend.backend_type, fake)
    collection = _make_collection(backend={"type": "fake-rag", "ref": ref})

    with structlog.testing.capture_logs() as logs:
        await search_docs(_make_operator(), "q", scope=_SCOPE, collection=collection)

    assert fake.calls[0]["metadata_filters"] == expected_filters
    assert fake.calls[0]["soft_scope"] == expected_scope
    completed = [e for e in logs if e["event"] == "docs_search_completed"]
    assert len(completed) == 1
    assert completed[0]["product"] == "vsphere"
    assert completed[0]["version"] == "8.0.3.00400"
    assert completed[0]["scope_forwarded"] == expected_mode
    assert "scope_filters_forwarded" not in completed[0]


@pytest.mark.parametrize(
    ("ref_extra", "extra_body", "raw_body"),
    [
        pytest.param({}, {}, b'{"query":"snapshot depth","top_k":10}', id="off"),
        pytest.param(
            {"scope_filters": True},
            {"metadata_filters": _REQUESTED},
            b'{"query":"snapshot depth","top_k":10,'
            b'"metadata_filters":{"product":"vsphere","version":"8.0.3.00400"}}',
            id="filters",
        ),
        pytest.param(
            {"scope": "soft"},
            {"scope": _SOFT},
            b'{"query":"snapshot depth","top_k":10,'
            b'"scope":{"product":"vsphere","version":"8.0.3.00400","source":"caller"}}',
            id="soft",
        ),
        pytest.param(
            {"scope": "soft", "scope_filters": True},
            {"scope": _SOFT},
            b'{"query":"snapshot depth","top_k":10,'
            b'"scope":{"product":"vsphere","version":"8.0.3.00400","source":"caller"}}',
            id="both-soft-wins",
        ),
    ],
)
async def test_search_docs_corpus_request_body_per_gate(
    monkeypatch: pytest.MonkeyPatch,
    ref_extra: dict[str, Any],
    extra_body: dict[str, Any],
    raw_body: bytes,
) -> None:
    """The exact body the ``corpus-http`` backend receives, per gate.

    The corpus refuses unknown request keys, so with the soft gate off the
    body must be byte-for-byte what it was before the gate existed: the
    gate-off case pins the raw request bytes to ``{"query", "top_k"}`` as
    httpx serialises them. With a gate on, the one added key is
    ``metadata_filters`` or ``scope``, never both. Each case asserts the
    parsed body and the raw bytes.
    """
    _pin_settings(monkeypatch, corpus_service_token=_SERVICE_TOKEN, corpus_audience="")
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(_handler))
    collection = _make_collection(
        backend={"type": CORPUS_HTTP_BACKEND_TYPE, "ref": {"endpoint": _CORPUS_URL, **ref_extra}},
    )

    await search_docs(_make_operator(), "snapshot depth", scope=_SCOPE, collection=collection)

    import json

    assert len(captured) == 1
    assert json.loads(captured[0].content.decode()) == {
        "query": "snapshot depth",
        "top_k": 10,
        **extra_body,
    }
    assert captured[0].content == raw_body
