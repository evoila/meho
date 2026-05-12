# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.retrieval.embedding`.

Coverage matrix (G0.4-T2 / Task #259 acceptance criteria):

* :class:`EmbeddingService` is the right shape - model name + cache
  dir constructor args, ``dimension`` property pinned to 384, lazy
  load until first ``encode`` call.
* :func:`get_embedding_service` returns a singleton bound to the
  current :class:`~meho_backplane.settings.Settings`; env-var override
  via ``RETRIEVAL_EMBEDDING_MODEL`` / ``RETRIEVAL_MODEL_CACHE_DIR``
  surfaces on a fresh resolution after the cache is cleared.
* Real-model coverage - load ``BAAI/bge-small-en-v1.5``, encode a
  sentence, assert the output shape (384-dim ``list[float]``) and
  determinism (same input → identical output). Marked ``slow`` so the
  always-on suite skips the ~1-2 s model load; CI's slow lane runs it.
* Event-loop responsiveness - concurrent ``encode_one`` + another
  awaitable coroutine via :func:`asyncio.gather` proves the
  :func:`asyncio.to_thread` wrap keeps the loop unblocked.

The non-``slow`` tests fully stub :class:`fastembed.TextEmbedding` so
they run in the always-on suite without paying the model-load cost or
hitting the network for ONNX weights. The ``slow``-marked tests
exercise the real model and gate on the fastembed package being
importable (it is - it's in ``[project.dependencies]``); they're tagged
``slow`` purely for runtime cost, not for an optional dependency.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from meho_backplane.retrieval.embedding import (
    EMBEDDING_DIMENSION,
    EmbeddingService,
    get_embedding_service,
    reset_embedding_service_for_testing,
)
from meho_backplane.settings import get_settings

#: Opt-in gate for the slow real-model test. CI's slow lane sets
#: ``MEHO_RUN_SLOW_TESTS=1`` (matching the convention the chassis
#: testcontainers tests use with ``MEHO_TEST_PGVECTOR_IMAGE``); the
#: always-on suite leaves it unset and skips the ~10-30 s weight
#: download + ~1-2 s model load. Strict-truthy parsing: only the
#: canonical truthy spellings enable the gate, so a typo like
#: ``MEHO_RUN_SLOW_TESTS=0`` evaluates to False rather than the
#: non-empty-string-is-truthy default Python's ``bool(str)`` would
#: produce.
_RUN_SLOW_TESTS: bool = os.environ.get("MEHO_RUN_SLOW_TESTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module.

    Mirrors :func:`tests.test_db_documents._required_settings_env` -
    the autouse ``_default_database_url`` fixture in
    :mod:`tests.conftest` only pins ``DATABASE_URL``; Keycloak / Vault
    knobs come from each test file. Both :func:`get_settings` and
    :func:`get_embedding_service` caches are cleared around the yield
    so a stale instance from a previous test cannot leak in.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_embedding_service_for_testing()
    yield
    get_settings.cache_clear()
    reset_embedding_service_for_testing()


# ---------------------------------------------------------------------------
# Constructor + properties
# ---------------------------------------------------------------------------


def test_embedding_service_does_not_load_model_on_construction(tmp_path: Path) -> None:
    """Constructing :class:`EmbeddingService` does not import fastembed.

    The fastembed import is local to :meth:`_ensure_loaded` so that
    instantiating the service (which the lifespan does at startup,
    and which test fixtures may do to stub the model) doesn't pull
    fastembed + onnxruntime into the import graph.

    Catches a regression where someone moves the ``from fastembed
    import TextEmbedding`` line to module-level (or to the constructor)
    - the assertion that ``self._model is None`` after construction is
    the load-bearing contract.
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path / "meho-test-cache"),
    )
    # Internal attribute access in a unit test is acceptable because
    # the lazy-load contract is what we're locking in. Public callers
    # don't (and shouldn't) inspect ``_model`` directly.
    assert service._model is None
    assert service.model_name == "BAAI/bge-small-en-v1.5"
    assert service.dimension == EMBEDDING_DIMENSION == 384


def test_embedding_service_cache_dir_expands_tilde() -> None:
    """``~`` in the cache dir resolves at construction time.

    Dev/test overrides commonly set ``RETRIEVAL_MODEL_CACHE_DIR`` to
    ``~/.cache/fastembed``; the constructor must expand it eagerly so
    every subsequent fastembed call sees an absolute path. Production
    values are absolute (``/var/cache/fastembed`` per the chart's PVC
    mount) and this expansion is a no-op there.
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir="~/.cache/fastembed-test",
    )
    assert not service.cache_dir.startswith("~"), (
        f"Cache dir must expand ~ at construction; got: {service.cache_dir}"
    )
    assert service.cache_dir.endswith(".cache/fastembed-test")


# ---------------------------------------------------------------------------
# Singleton factory + settings binding
# ---------------------------------------------------------------------------


def test_get_embedding_service_returns_singleton() -> None:
    """Two calls to :func:`get_embedding_service` return the same instance.

    The ``@lru_cache(maxsize=1)`` on the factory is what makes the
    lifespan's eager-load amortise across the pod lifetime: T3's
    ``index_document`` and T4's ``retrieve`` both call
    :func:`get_embedding_service` and get the **same** preloaded
    model. A regression where the factory loses the cache (e.g. drops
    ``@lru_cache``) would surface as a 1-2 s load on every request.
    """
    first = get_embedding_service()
    second = get_embedding_service()
    assert first is second


def test_get_embedding_service_binds_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``RETRIEVAL_EMBEDDING_MODEL`` / ``RETRIEVAL_MODEL_CACHE_DIR`` env vars surface."""
    custom_cache = str(tmp_path / "meho-custom-cache")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    monkeypatch.setenv("RETRIEVAL_MODEL_CACHE_DIR", custom_cache)
    get_settings.cache_clear()
    reset_embedding_service_for_testing()

    service = get_embedding_service()
    assert service.model_name == "BAAI/bge-base-en-v1.5"
    assert service.cache_dir == custom_cache


def test_reset_embedding_service_for_testing_clears_cache() -> None:
    """:func:`reset_embedding_service_for_testing` lets tests swap the singleton.

    Without this helper, a test that mutates ``RETRIEVAL_EMBEDDING_MODEL``
    after the first :func:`get_embedding_service` call would still see
    the cached instance - the same hazard ``get_settings.cache_clear()``
    addresses for chassis settings.
    """
    first = get_embedding_service()
    reset_embedding_service_for_testing()
    second = get_embedding_service()
    assert first is not second


# ---------------------------------------------------------------------------
# Stubbed encode - proves the wiring without paying the model-load cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encode_one_returns_384_dim_list_with_stubbed_model(tmp_path: Path) -> None:
    """A stubbed ``TextEmbedding.embed`` round-trips through ``encode_one``.

    Asserts the contract :func:`EmbeddingService.encode_one` exposes
    to T3/T4 callers: ``list[float]`` (not numpy), length 384, ordered
    same as input. The fastembed import path itself is patched so the
    test runs without loading the real ONNX model - fast enough for
    the always-on suite.
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path / "meho-test-cache"),
    )

    stub_model = MagicMock()
    # fastembed's real ``embed`` returns an iterator of numpy arrays;
    # the stub returns Python lists pre-cast to floats so the
    # ``float(x)`` inside ``encode`` is exercised against ordinary
    # iterable types (numpy is not in the test dependency set).
    stub_model.embed.return_value = iter([[0.01 * i for i in range(384)]])

    # Bypass the lazy fastembed import: directly inject the stub onto
    # the private attribute so ``_ensure_loaded`` returns it unchanged.
    service._model = stub_model

    vec = await service.encode_one("hello world")

    assert isinstance(vec, list)
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)
    # The stub returned indices 0.00, 0.01, 0.02, … 3.83.
    assert vec[0] == pytest.approx(0.0)
    assert vec[1] == pytest.approx(0.01)
    assert vec[383] == pytest.approx(3.83)


@pytest.mark.asyncio
async def test_encode_batches_preserve_order(tmp_path: Path) -> None:
    """``encode([t1, t2, t3])`` returns three vectors in input order.

    Catches a regression where a future refactor maps the embed call
    over a non-ordered iterable (set, dict.values without dict order
    guarantees). Determinism on input order is the load-bearing
    contract; T3's ``index_document`` relies on the 1:1 input ↔ output
    correspondence for single-text calls but a future bulk indexer
    would consume the full ordered batch.
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path / "meho-test-cache"),
    )

    # Three distinct outputs so the order assertion is meaningful.
    def fake_embed(texts: Iterable[str]) -> Iterator[list[float]]:
        text_list = list(texts)
        # Map each text to a sentinel vector keyed by index so the
        # test can prove ordering survived ``asyncio.to_thread``.
        for i, _ in enumerate(text_list):
            yield [float(i)] * 384

    stub_model = MagicMock()
    stub_model.embed.side_effect = fake_embed
    service._model = stub_model

    vecs = await service.encode(["first", "second", "third"])

    assert len(vecs) == 3
    # ``pytest.approx`` keeps the assertion style consistent with the
    # sibling vector test at line ~210 even though the stub returns
    # exact ``float(i)`` values; Sonar's ``python:S1244`` rule flags
    # equality on floats unconditionally.
    assert vecs[0][0] == pytest.approx(0.0)
    assert vecs[1][0] == pytest.approx(1.0)
    assert vecs[2][0] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Lazy-load trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_encode_triggers_fastembed_import(tmp_path: Path) -> None:
    """First :meth:`encode` call imports :class:`fastembed.TextEmbedding`.

    Patches ``fastembed.TextEmbedding`` at the module level so the
    test proves the constructor is called exactly once on first
    encode and never again on subsequent encodes (the singleton
    contract).
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path / "meho-test-cache"),
    )

    constructor_calls = 0
    stub_model = MagicMock()
    # ``side_effect`` instead of ``return_value`` so each call to
    # ``model.embed(texts)`` gets a fresh iterator. The real fastembed
    # API returns a new generator on every invocation; using
    # ``return_value=iter(...)`` would exhaust on the first call and
    # the second ``encode_one`` here would IndexError.
    stub_model.embed.side_effect = lambda texts: iter([[0.0] * 384 for _ in texts])

    def fake_text_embedding(**kwargs: Any) -> MagicMock:
        nonlocal constructor_calls
        constructor_calls += 1
        return stub_model

    with patch("fastembed.TextEmbedding", side_effect=fake_text_embedding):
        await service.encode_one("first")
        assert constructor_calls == 1
        await service.encode_one("second")
        # Constructor must NOT be called again - model is cached on the instance.
        assert constructor_calls == 1


# ---------------------------------------------------------------------------
# Event-loop responsiveness (asyncio.to_thread wrap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encode_does_not_block_event_loop(tmp_path: Path) -> None:
    """Concurrent :meth:`encode_one` + a fast coroutine return out of order.

    The :func:`asyncio.to_thread` wrap is what lets retrieval queries
    not stall the FastAPI event loop. The proof: a deliberately-slow
    embed (sleeps for 200 ms inside the sync function) runs
    concurrently with a fast :func:`asyncio.sleep(0.01)`, and the
    fast sleep finishes first. Without ``to_thread``, the slow embed
    would block the loop and both coroutines would resolve in
    submission order.
    """
    import time

    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path / "meho-test-cache"),
    )

    def slow_embed(texts: Iterable[str]) -> Iterator[list[float]]:
        time.sleep(0.2)
        for _ in texts:
            yield [0.0] * 384

    stub_model = MagicMock()
    stub_model.embed.side_effect = slow_embed
    service._model = stub_model

    results: list[str] = []

    async def fast_task() -> None:
        await asyncio.sleep(0.01)
        results.append("fast")

    async def slow_task() -> None:
        await service.encode_one("slow")
        results.append("slow")

    await asyncio.gather(slow_task(), fast_task())

    # "fast" must arrive before "slow" - proves the loop wasn't stalled
    # by the synchronous embed call.
    assert results == ["fast", "slow"], (
        f"asyncio.to_thread wrap should let fast task finish first; got: {results}"
    )


# ---------------------------------------------------------------------------
# Real-model coverage - slow, opt-in via -m slow
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not _RUN_SLOW_TESTS,
    reason="Real-model load skipped unless MEHO_RUN_SLOW_TESTS=1 (CI slow lane).",
)
@pytest.mark.asyncio
async def test_real_model_loads_and_returns_384_dim_vector(tmp_path: Any) -> None:
    """Real ``BAAI/bge-small-en-v1.5`` produces 384-dim deterministic vectors.

    Smoke-tests the whole stack end-to-end against the actual fastembed
    package. Marked ``slow`` because the first run downloads the
    ~120 MB ONNX weights (~10-30 s on CI) and the model load itself
    is ~1-2 s. The cache_dir points at a temp path so the test doesn't
    pollute / depend on a shared cache.
    """
    service = EmbeddingService(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=str(tmp_path),
    )
    vec_a1 = await service.encode_one("kubernetes ingress troubleshooting")
    vec_a2 = await service.encode_one("kubernetes ingress troubleshooting")
    vec_b = await service.encode_one("completely different sentence about wine")

    assert isinstance(vec_a1, list)
    assert len(vec_a1) == EMBEDDING_DIMENSION
    assert all(isinstance(x, float) for x in vec_a1)

    # Determinism: same input → identical vector.
    assert vec_a1 == vec_a2

    # Different input → different vector (the model is doing *something*).
    assert vec_a1 != vec_b


# ---------------------------------------------------------------------------
# Mypy-cast spot - keep the linter quiet about the deliberate private access
# ---------------------------------------------------------------------------


def test_typing_cast_helpers_compile() -> None:
    """A no-op test that imports :func:`cast` so the cast helper stays in scope.

    Some test files use :func:`typing.cast` to soften narrow type
    hints; this empty test keeps the import alive even when mypy strict
    is happy about unused imports.
    """
    _ = cast
