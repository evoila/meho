# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Text validation utilities for knowledge chunks.

Validates text size to prevent embedding overflow.
The limit targets fastembed MiniLM-L12-v2 (512 WordPiece tokens).
tiktoken cl100k_base is an approximation — BPE tends to produce fewer tokens
than WordPiece, so the guard is slightly conservative.
"""

import tiktoken

# fastembed paraphrase-multilingual-MiniLM-L12-v2 has a 512-token BERT context window.
MAX_EMBEDDING_TOKENS = 512


def validate_text_for_embedding(
    text: str, encoding_name: str = "cl100k_base"
) -> tuple[bool, str, int]:
    """
    Validate that text is within embedding model token limits.

    Args:
        text: Text to validate
        encoding_name: Tiktoken encoding name

    Returns:
        Tuple of (is_valid, error_message, token_count)
    """
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)
    token_count = len(tokens)

    if token_count > MAX_EMBEDDING_TOKENS:
        return (
            False,
            f"Text exceeds embedding token limit: {token_count} tokens (max: {MAX_EMBEDDING_TOKENS}). Please use chunking or reduce text size.",
            token_count,
        )

    return (True, "", token_count)


def truncate_text_to_token_limit(
    text: str, max_tokens: int = MAX_EMBEDDING_TOKENS, encoding_name: str = "cl100k_base"
) -> str:
    """
    Truncate text to fit within token limit.

    Args:
        text: Text to truncate
        max_tokens: Maximum tokens
        encoding_name: Tiktoken encoding

    Returns:
        Truncated text that fits within token limit
    """
    encoding = tiktoken.get_encoding(encoding_name)
    tokens = encoding.encode(text)

    if len(tokens) <= max_tokens:
        return text

    # Truncate tokens and decode back to text
    truncated_tokens = tokens[:max_tokens]
    truncated_text = encoding.decode(truncated_tokens)

    # Add ellipsis to indicate truncation
    return truncated_text + "\n\n[... truncated to fit embedding token limit]"


def count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """
    Count tokens in text.

    Args:
        text: Text to count
        encoding_name: Tiktoken encoding

    Returns:
        Number of tokens
    """
    encoding = tiktoken.get_encoding(encoding_name)
    return len(encoding.encode(text))
