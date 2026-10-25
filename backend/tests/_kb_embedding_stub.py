# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A fake embedding service for kb tests that never loads or downloads a model.

kb writes embed the head and every passage in one ``encode`` call and
count tokens with :meth:`EmbeddingService.passage_tokenizer` (#3761).
:func:`fake_embedding_service` answers both: ``encode`` returns one
fixed vector per input text, and the token counter counts white-space
separated words plus two special tokens, with a 512-token window.
Patch it in at ``meho_backplane.retrieval.indexer.get_embedding_service``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

FAKE_VECTOR: list[float] = [0.1] * 384


class StubPassageTokenizer:
    """Word count plus two special tokens; no model files needed."""

    def __init__(self, window: int = 512) -> None:
        self.window = window

    def count(self, text: str) -> int:
        return len(text.split()) + 2


def fake_embedding_service(vector: list[float] | None = None, window: int = 512) -> AsyncMock:
    """Return an ``AsyncMock`` embedding service with real-shaped answers."""
    vec = FAKE_VECTOR if vector is None else vector
    fake = AsyncMock()
    fake.encode_one.return_value = vec
    fake.encode.side_effect = lambda texts: [list(vec) for _ in texts]
    fake.passage_tokenizer.return_value = StubPassageTokenizer(window)
    fake.dimension = 384
    return fake
