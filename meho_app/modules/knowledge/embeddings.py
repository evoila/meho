# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Embedding generation for knowledge chunks.

Embeddings are produced in-process by `fastembed <https://qdrant.github.io/fastembed/>`_,
which runs quantized ONNX models on the CPU. No PyTorch, no transformers, no
GPU. The default model is the 384-dim multilingual MiniLM
(`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`); the model is
pulled from Hugging Face on first use and cached on disk under
``config.fastembed_cache_dir``.

This is the preview path. When MEHO.Knowledge takes over remote
embedding/reranking, swap :class:`FastEmbedEmbeddings` for an HTTP client
implementation of the same Protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    dimension: int

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding vector for a single text."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        ...


class FastEmbedEmbeddings:
    """In-process fastembed (ONNX) embedding provider."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_dir: str | None = None,
        dimension: int = 384,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.dimension = dimension
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> Any:
        """Lazy-load the ONNX model on first use, single-flight via the lock."""
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                logger.info(
                    "fastembed_loading_model",
                    model=self.model_name,
                    cache_dir=self.cache_dir,
                )
                self._model = await asyncio.to_thread(self._build_model)
                logger.info("fastembed_model_loaded", model=self.model_name)
        return self._model

    def _build_model(self) -> Any:
        # Imported lazily so the dependency is only paid when an embedding is
        # actually requested (e.g. tests mocking the provider don't trigger
        # an ONNX download).
        from fastembed import TextEmbedding

        kwargs: dict[str, Any] = {"model_name": self.model_name}
        if self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
        return TextEmbedding(**kwargs)

    async def embed_text(self, text: str, **_kwargs: Any) -> list[float]:
        model = await self._ensure_loaded()
        # fastembed's `.embed()` returns a generator of numpy arrays; we coerce
        # to a plain list[float] for JSON / pgvector compatibility.
        result = await asyncio.to_thread(lambda: list(model.embed([text])))
        return [float(x) for x in result[0]]

    async def embed_batch(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
        if not texts:
            return []
        model = await self._ensure_loaded()
        result = await asyncio.to_thread(lambda: list(model.embed(texts)))
        return [[float(x) for x in vec] for vec in result]


_embedding_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Get the embedding provider singleton (in-process fastembed)."""
    global _embedding_provider

    if _embedding_provider is None:
        config = get_config()
        _embedding_provider = cast(
            "EmbeddingProvider",
            FastEmbedEmbeddings(
                model_name=config.fastembed_embedding_model,
                cache_dir=config.fastembed_cache_dir,
            ),
        )

    return _embedding_provider


def reset_embedding_provider() -> None:
    """Reset embedding provider singleton (for testing)."""
    global _embedding_provider
    _embedding_provider = None
