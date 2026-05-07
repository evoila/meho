# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.embeddings.

Covered in detail by tests/unit/test_fastembed_embeddings.py. This file
keeps a tiny smoke check so import-time regressions are surfaced even if
the dedicated suite is skipped.
"""

from unittest.mock import MagicMock, patch

from meho_app.modules.knowledge.embeddings import (
    FastEmbedEmbeddings,
    get_embedding_provider,
    reset_embedding_provider,
)


def setup_function() -> None:
    reset_embedding_provider()


def teardown_function() -> None:
    reset_embedding_provider()


@patch("meho_app.modules.knowledge.embeddings.get_config")
def test_singleton_returns_fastembed_with_config_model(mock_get_config: MagicMock) -> None:
    cfg = MagicMock()
    cfg.fastembed_embedding_model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    cfg.fastembed_cache_dir = "/var/cache/fastembed"
    mock_get_config.return_value = cfg

    provider = get_embedding_provider()
    assert isinstance(provider, FastEmbedEmbeddings)
    assert provider.dimension == 384
