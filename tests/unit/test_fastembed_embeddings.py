# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for the in-process fastembed embedding provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from meho_app.modules.knowledge.embeddings import (
    FastEmbedEmbeddings,
    get_embedding_provider,
    reset_embedding_provider,
)


class _FakeTextEmbedding:
    """Fake fastembed.TextEmbedding stand-in for tests.

    Returns one length-`dim` vector per input, filled with the input length.
    Records call args so we can assert batching + lazy load.
    """

    def __init__(self, *args: Any, dim: int = 384, **kwargs: Any) -> None:
        self.dim = dim
        self.embed_calls: list[list[str]] = []
        self.init_args = args
        self.init_kwargs = kwargs

    def embed(self, texts: list[str]) -> Any:
        self.embed_calls.append(list(texts))
        for t in texts:
            yield [float(len(t))] * self.dim


class TestFastEmbedEmbeddings:
    @pytest.mark.asyncio
    async def test_embed_text_lazy_loads_model(self, tmp_path: Path) -> None:
        cache = str(tmp_path / "x")
        provider = FastEmbedEmbeddings(model_name="dummy-model", cache_dir=cache, dimension=8)
        fake = _FakeTextEmbedding(dim=8)

        with patch("fastembed.TextEmbedding", return_value=fake) as cls:
            assert provider._model is None
            result = await provider.embed_text("hello")
            assert provider._model is fake
            cls.assert_called_once_with(model_name="dummy-model", cache_dir=cache)

        assert len(result) == 8
        assert result[0] == pytest.approx(5.0)
        assert fake.embed_calls == [["hello"]]

    @pytest.mark.asyncio
    async def test_embed_batch_returns_one_vector_per_text(self) -> None:
        provider = FastEmbedEmbeddings(model_name="dummy", dimension=4)
        fake = _FakeTextEmbedding(dim=4)
        with patch("fastembed.TextEmbedding", return_value=fake):
            result = await provider.embed_batch(["a", "bb", "ccc"])
        assert len(result) == 3
        assert all(len(v) == 4 for v in result)
        assert result[0][0] == pytest.approx(1.0)
        assert result[1][0] == pytest.approx(2.0)
        assert result[2][0] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_embed_batch_empty_short_circuits(self) -> None:
        provider = FastEmbedEmbeddings(model_name="dummy")
        # Don't patch fastembed: empty list must not even attempt to load the model.
        result = await provider.embed_batch([])
        assert result == []
        assert provider._model is None

    @pytest.mark.asyncio
    async def test_model_loaded_only_once(self) -> None:
        provider = FastEmbedEmbeddings(model_name="dummy", dimension=4)
        fake = _FakeTextEmbedding(dim=4)
        with patch("fastembed.TextEmbedding", return_value=fake) as cls:
            await provider.embed_text("a")
            await provider.embed_text("b")
            await provider.embed_batch(["c", "d"])
        assert cls.call_count == 1
        assert fake.embed_calls == [["a"], ["b"], ["c", "d"]]

    def test_default_dimension_is_384(self) -> None:
        # 384 must match the pgvector schema set by 0011_embedding_dim_384.
        provider = FastEmbedEmbeddings()
        assert provider.dimension == 384


class TestProviderSingleton:
    def setup_method(self) -> None:
        reset_embedding_provider()

    def teardown_method(self) -> None:
        reset_embedding_provider()

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_factory_returns_fastembed(self, mock_get_config: MagicMock) -> None:
        cfg = MagicMock()
        cfg.fastembed_embedding_model = "model-x"
        cfg.fastembed_cache_dir = "/var/cache/fastembed"
        mock_get_config.return_value = cfg

        provider = get_embedding_provider()
        assert isinstance(provider, FastEmbedEmbeddings)
        assert provider.model_name == "model-x"
        assert provider.cache_dir == "/var/cache/fastembed"

    @patch("meho_app.modules.knowledge.embeddings.get_config")
    def test_factory_is_idempotent(self, mock_get_config: MagicMock, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.fastembed_embedding_model = "model-x"
        cfg.fastembed_cache_dir = str(tmp_path)
        mock_get_config.return_value = cfg

        a = get_embedding_provider()
        b = get_embedding_provider()
        assert a is b
        assert mock_get_config.call_count == 1
