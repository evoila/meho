# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In-process embedding pipeline backed by fastembed (ONNX runtime).

G0.4-T2 (#259) of Initiative #225. The :class:`EmbeddingService` is
the only surface in the backplane that turns text into a dense vector;
both :func:`~meho_backplane.retrieval.indexer.index_document` (T3) and
:func:`~meho_backplane.retrieval.retriever.retrieve` (T4) route through
its singleton.

Design choices (locked by the Initiative body):

* **fastembed, not sentence-transformers.** v0.1-spec L391 chose
  fastembed because it ships an ONNX runtime (~120 MB) instead of a
  full PyTorch dependency (~1.5 GB). The backplane is single-replica
  in v0.2; memory footprint matters.
* **Default model: ``BAAI/bge-small-en-v1.5`` (384-dim, Apache-2.0,
  English).** Matches the ``vector(384)`` column type that migration
  ``0003`` (G0.4-T1) installed. A future model swap with different
  dimensionality is a re-embed-everything migration, deferred to a
  separate ticket per the Initiative's out-of-scope list.
* **Lazy model loading inside :meth:`_ensure_loaded`.** The fastembed
  import itself is local to the method body, not module-level: unit
  tests that never instantiate the service avoid paying the fastembed
  import cost (and avoid downloading ONNX weights via the package's
  on-import probe). The lifespan hook in
  :mod:`meho_backplane.main` calls :meth:`encode_one` once at startup
  so the load amortises across the pod lifetime rather than being
  paid by the first real request.
* **``asyncio.to_thread`` wrap.** fastembed's ``TextEmbedding.embed``
  is sync (ONNX runtime is itself sync). Without the thread offload,
  every embedding call would block the FastAPI event loop for ~10-50
  ms per text - saturating the worker on bursty traffic. Wrapping
  every batch in :func:`asyncio.to_thread` keeps the loop responsive;
  the thread-pool default (cpu_count + 4) is plenty for the v0.2
  request shape (one embed per indexing call, one per retrieval
  query).
* **Model cache directory.** The shipped default model is baked into
  the image at :data:`BAKED_MODEL_CACHE_DIR` (``/opt/meho/model-cache``)
  by ``backend/Dockerfile``'s ``meho_backplane.retrieval.warm`` step,
  so first boot loads it offline + version-locked with no runtime
  HuggingFace download and no PVC dependency (evoila/meho#574; also
  closes the air-gap half of #572). The settings field
  ``retrieval_model_cache_dir`` defaults to that baked path; dev/test
  override it (e.g. ``$HOME/.cache/fastembed`` so SQLite tests reuse a
  developer's existing cache), and operators who override the model to
  a non-default identifier point it at the opt-in
  ``retrieval.modelCache`` PVC so the runtime-fetched weights survive
  pod restarts.

Out of scope here (other tasks / out-of-scope per Initiative):

* GPU / CUDA acceleration - CPU ONNX is fast enough for the v0.2
  corpus size (single-tenant, hundreds to low thousands of documents).
* Multi-replica cache-sharing - operators provide an RWX PVC if they
  need multi-replica (v0.2 is single-replica anyway).
* Per-query embedding cache (LRU) - every retrieval re-embeds the
  query; v0.2.next can add an LRU once cache-hit ratios are measured.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

import structlog

__all__ = [
    "BAKED_MODEL_CACHE_DIR",
    "DEFAULT_EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
    "EmbeddingService",
    "get_embedding_service",
    "reset_embedding_service_for_testing",
]

#: The default fastembed model the backplane ships **and bakes into the
#: image** (``backend/Dockerfile`` runtime stage runs
#: ``python -m meho_backplane.retrieval.warm``, which downloads exactly
#: this). Single source of truth: :attr:`Settings.retrieval_embedding_model`'s
#: Field default *and* its env-loader fallback both reference this
#: constant, so the literal lives in exactly one place and the baked
#: artifact can never silently disagree with the runtime default.
#: Changing it requires re-baking the image; a change in output
#: dimensionality additionally requires a re-embed migration (see
#: :data:`EMBEDDING_DIMENSION`).
DEFAULT_EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"

#: Filesystem path the image bakes the default model into at build time
#: and the default ``cache_dir`` the backplane reads at runtime. This is
#: an **image layer**, not a mounted PVC: first boot is therefore
#: offline + version-locked (closes the air-gap half of evoila/meho#572)
#: and is immune to the persistent-PVC partial/symlink-corruption
#: failure mode that deterministically CrashLoops every fresh pod
#: (evoila/meho#574 — fastembed treats an existing snapshot dir as
#: "present" and will not re-fetch, so one broken populate poisons the
#: PVC forever). The optional ``retrieval.modelCache`` PVC is now an
#: opt-in optimisation for operator-overridden *non-default* models, not
#: a correctness dependency for the shipped default.
BAKED_MODEL_CACHE_DIR: str = "/opt/meho/model-cache"

#: Embedding dimensionality the backplane commits to in v0.2 - must
#: match the ``vector(384)`` column type in migration ``0003`` and the
#: ``Document.embedding`` SQLAlchemy mapping. Operators swapping
#: :class:`Settings.retrieval_embedding_model` to a model with a
#: different output dimensionality require a re-embed-everything
#: migration; the constant exists as the load-bearing contract callers
#: assert against.
EMBEDDING_DIMENSION: int = 384


class EmbeddingService:
    """Async wrapper around fastembed's :class:`TextEmbedding`.

    Holds one ONNX model in process memory and serves batched encode
    requests against it. The wrapper is single-instance per process
    via :func:`get_embedding_service`; do not instantiate directly in
    application code except for tests that need a custom model name
    or cache directory.

    Lifecycle:

    1. Constructor pins ``model_name`` + ``cache_dir`` but does **not**
       load the model - that happens lazily on first :meth:`encode` /
       :meth:`encode_one` call. The lifespan hook in
       :mod:`meho_backplane.main` triggers this load at startup so
       the cost amortises across the pod's lifetime.
    2. :meth:`_ensure_loaded` imports :class:`fastembed.TextEmbedding`
       and constructs the model bound to the cache directory. The
       import lives inside the method body so module import of
       :mod:`meho_backplane.retrieval.embedding` stays fast and the
       fastembed package isn't pulled until an embedding is actually
       requested.
    3. :meth:`encode` wraps the sync ``model.embed`` call in
       :func:`asyncio.to_thread` so the event loop stays responsive
       under load.

    The class deliberately ships with no helper methods beyond
    ``encode`` / ``encode_one``. Token estimation, batch sizing, and
    similar concerns live in the callers (T3's ``index_document``
    estimates tokens; future bulk-index helpers in v0.2.next decide
    batch sizes).
    """

    def __init__(self, model_name: str, cache_dir: str) -> None:
        self._model_name = model_name
        # ``cache_dir`` is expanded eagerly so a literal ``~`` in the
        # settings value (typical dev override) resolves at construction
        # time rather than per-encode call. Production values are
        # absolute paths from the chart's PVC mount; the expansion is a
        # no-op there.
        self._cache_dir = os.path.expanduser(cache_dir)
        self._model: Any | None = None
        # Thread lock guarding the lazy ``_ensure_loaded`` initialisation.
        # Production callers go through ``asyncio.to_thread`` (one
        # encode per thread-pool worker); without the lock two
        # concurrent first-use calls can both observe
        # ``self._model is None`` and race to instantiate
        # ``TextEmbedding`` -- the second instantiation silently
        # discards the first. The lifespan preload normally wins this
        # race before any real request lands, but the contract should
        # not depend on lifespan ordering.
        self._model_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        """The fastembed model identifier this service is bound to."""
        return self._model_name

    @property
    def cache_dir(self) -> str:
        """Absolute path to the ONNX-weight cache directory."""
        return self._cache_dir

    @property
    def dimension(self) -> int:
        """Output vector dimensionality.

        Pinned to :data:`EMBEDDING_DIMENSION` in v0.2 because the
        ``Document.embedding`` column type is ``vector(384)`` and a
        model with different output dimensionality requires a re-embed-
        everything migration. Callers use this constant to assert the
        contract (see :func:`tests.test_retrieval_embedding`).
        """
        return EMBEDDING_DIMENSION

    def _ensure_loaded(self) -> Any:
        """Load the ONNX model if it isn't yet, return the bound instance.

        The fastembed import is local to keep the package off the
        SQLite-only test import path (and to keep module-import time
        for :mod:`meho_backplane.retrieval.embedding` itself fast -
        importing fastembed transitively imports onnxruntime, which
        does its own startup probing). The construction is logged so
        operator-side observability sees the load event with model
        name + cache directory; first-request latency operators see
        on cold pods correlates against this log line.

        :func:`structlog.get_logger` is resolved per-call (rather than
        from a module-level proxy) so the :class:`PrintLogger` it
        eventually resolves to binds against the **current**
        ``sys.stdout``. Without this, a worker thread spawned via
        :func:`asyncio.to_thread` can fall through to a stdout that
        pytest's capture machinery has already closed - the failure
        mode is ``ValueError: I/O operation on closed file`` deep
        inside the structlog processor chain.

        Double-checked locking around the lazy assignment: the first
        ``if self._model is None`` is the fast path (single attribute
        read, no lock acquisition once the model is loaded). Two
        concurrent ``encode()`` calls dispatched to the thread pool
        can both pass the unsynchronised check on a cold service, so
        the second check inside the lock guards against double-
        instantiation -- without it the second thread would silently
        re-construct ``TextEmbedding`` and overwrite the first's
        cached instance.
        """
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from fastembed import TextEmbedding

            log = structlog.get_logger()
            log.info(
                "embedding_model_loading",
                model=self._model_name,
                cache_dir=self._cache_dir,
            )
            try:
                self._model = TextEmbedding(
                    model_name=self._model_name,
                    cache_dir=self._cache_dir,
                )
            except Exception as exc:
                # fastembed builds the onnxruntime InferenceSession in
                # the TextEmbedding constructor, so a partial/corrupt
                # cache surfaces here as a raw
                # ``onnxruntime ... NO_SUCHFILE: ... model_optimized.onnx
                # ... File doesn't exist`` ten frames deep — opaque at
                # the "Application startup failed" line operators
                # actually see. Re-raise with the operator-actionable
                # diagnosis: the model+fastembed pair is fine; the cache
                # is the suspect, and fastembed will NOT self-heal a
                # populated-but-broken snapshot dir (evoila/meho#574).
                raise RuntimeError(
                    f"embedding model {self._model_name!r} failed to "
                    f"load from cache_dir {self._cache_dir!r}: "
                    f"{type(exc).__name__}: {exc}. The shipped default "
                    f"({DEFAULT_EMBEDDING_MODEL!r}) is baked into the "
                    f"image at {BAKED_MODEL_CACHE_DIR!r} and loads "
                    f"offline; this almost always means a partial or "
                    f"symlink-broken cache (a dangling HF symlink or a "
                    f"truncated *.onnx blob from an interrupted first "
                    f"download) — fastembed treats an existing snapshot "
                    f"directory as 'present' and will not re-fetch it. "
                    f"Fix: clear {self._cache_dir!r} and let it "
                    f"re-download, or unset RETRIEVAL_MODEL_CACHE_DIR / "
                    f"RETRIEVAL_EMBEDDING_MODEL to use the baked default "
                    f"(evoila/meho#574)."
                ) from exc
            log.info(
                "embedding_model_loaded",
                model=self._model_name,
                dimension=self.dimension,
            )
        return self._model

    async def encode(self, texts: Iterable[str]) -> list[list[float]]:
        """Encode a batch of texts into 384-dim dense vectors.

        The sync ``model.embed`` call is wrapped in
        :func:`asyncio.to_thread` so the FastAPI event loop stays
        responsive: a 50-text batch takes ~500 ms of CPU time on the
        v0.1 baseline pod, which would stall every concurrent request
        if it ran inline.

        Returns a list whose order matches the input iterable, with
        each element a list of ``float`` (not numpy array - pgvector's
        SQLAlchemy adapter binds ``list[float]`` directly, and the
        SQLite TypeDecorator path in
        :class:`~meho_backplane.db.models._PortableVector384`
        JSON-encodes the same shape).
        """
        text_list = list(texts)

        def _sync_encode() -> list[list[float]]:
            model = self._ensure_loaded()
            # fastembed's ``embed`` returns an iterator of numpy arrays;
            # collecting eagerly inside the thread keeps the event-loop
            # side single-call rather than dragging the iterator across
            # threads. ``float()`` on each element forces the numpy
            # ``float32`` to a Python ``float`` so the downstream
            # pgvector adapter sees a plain ``list[float]`` (numpy
            # types bind too, but the explicit cast removes a class of
            # mypy-side surprises).
            vectors = [[float(x) for x in vec] for vec in model.embed(text_list)]
            # Dimension-drift guard: surface a model misconfiguration
            # (operator swapped to a non-384-dim model) at the
            # retrieval layer with a clear error, rather than letting
            # the wrong-shape vector reach the DB and trip a pgvector
            # bind-time error whose message wouldn't mention the
            # model. The check is per-vector because a future
            # multi-model A/B path could in principle return mixed
            # shapes; the loop bounds are the batch size which is
            # ~50-100 in the v0.2 retrieval shape, well under any
            # perf concern.
            for index, vec in enumerate(vectors):
                if len(vec) != EMBEDDING_DIMENSION:
                    raise ValueError(
                        f"Embedding dimension mismatch from model "
                        f"{self._model_name!r}: expected "
                        f"{EMBEDDING_DIMENSION}, got {len(vec)} at index "
                        f"{index} of a {len(vectors)}-vector batch. "
                        f"Check RETRIEVAL_EMBEDDING_MODEL -- "
                        f"documents.embedding is hard-pinned to "
                        f"vector(384) by migration 0003."
                    )
            return vectors

        return await asyncio.to_thread(_sync_encode)

    async def encode_one(self, text: str) -> list[float]:
        """Encode a single text - convenience wrapper around :meth:`encode`.

        Used by T3's ``index_document`` (one body per call) and T4's
        ``retrieve`` (one query per call). Both paths could call
        :meth:`encode` directly with a one-element list; the named
        wrapper just reads more cleanly at the call site.
        """
        result = await self.encode([text])
        return result[0]


@lru_cache(maxsize=1)
def get_embedding_service() -> EmbeddingService:
    """Return the process-wide :class:`EmbeddingService` singleton.

    Reads :class:`~meho_backplane.settings.Settings` on first call;
    subsequent calls return the cached instance. Tests that need to
    swap the model name or cache directory call
    :func:`reset_embedding_service_for_testing` after mutating env
    vars.

    The function deliberately resolves :func:`get_settings` lazily
    (inside the body rather than at module import) so importing
    :mod:`meho_backplane.retrieval.embedding` does not require every
    chassis env var to be pinned - the test suite imports the module
    before the autouse fixtures pin Keycloak / Vault env vars.
    """
    # Local import: keeps the embedding module importable in test
    # contexts that haven't pinned the full chassis settings surface.
    from meho_backplane.settings import get_settings

    settings = get_settings()
    return EmbeddingService(
        model_name=settings.retrieval_embedding_model,
        cache_dir=settings.retrieval_model_cache_dir,
    )


def reset_embedding_service_for_testing() -> None:
    """Clear the :func:`get_embedding_service` cache.

    Test-only helper - production callers should never reach for it.
    The chassis equivalents (``get_settings.cache_clear()``,
    :func:`~meho_backplane.db.engine.reset_engine_for_testing`) follow
    the same pattern so test isolation across env-var mutations stays
    uniform.
    """
    get_embedding_service.cache_clear()
