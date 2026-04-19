# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_knowledge/text_validation.py

Tests text validation for embedding token limits.
"""

from meho_app.modules.knowledge.text_validation import (
    MAX_EMBEDDING_TOKENS,
    count_tokens,
    truncate_text_to_token_limit,
    validate_text_for_embedding,
)


class TestValidateTextForEmbedding:
    """Tests for validate_text_for_embedding function"""

    def test_validate_short_text_passes(self):
        """Test that short text passes validation"""
        text = "This is a short text."

        is_valid, error_msg, token_count = validate_text_for_embedding(text)

        assert is_valid is True
        assert error_msg == ""
        assert token_count > 0
        assert token_count < MAX_EMBEDDING_TOKENS

    def test_validate_empty_text(self):
        """Test that empty text passes validation"""
        text = ""

        is_valid, error_msg, token_count = validate_text_for_embedding(text)

        assert is_valid is True
        assert error_msg == ""
        assert token_count == 0

    def test_validate_text_at_limit(self):
        """Test text exactly at token limit"""
        # Create text with approximately MAX_EMBEDDING_TOKENS tokens
        # Average ~4 characters per token
        text = "word " * (MAX_EMBEDDING_TOKENS - 1)

        is_valid, error_msg, token_count = validate_text_for_embedding(text)

        assert is_valid is True
        assert error_msg == ""
        assert token_count <= MAX_EMBEDDING_TOKENS

    def test_validate_text_exceeds_limit(self):
        """Test that text exceeding limit fails validation"""
        # Create text with more than MAX_EMBEDDING_TOKENS tokens
        # Generate a long string that will exceed the limit
        text = "word " * (MAX_EMBEDDING_TOKENS + 1000)

        is_valid, error_msg, token_count = validate_text_for_embedding(text)

        assert is_valid is False
        assert "exceeds embedding token limit" in error_msg
        assert str(token_count) in error_msg
        assert str(MAX_EMBEDDING_TOKENS) in error_msg
        assert token_count > MAX_EMBEDDING_TOKENS

    def test_validate_with_special_characters(self):
        """Test validation with special characters and unicode"""
        text = "Hello 世界! 🌍 Ñoño café résumé"

        is_valid, error_msg, token_count = validate_text_for_embedding(text)

        assert is_valid is True
        assert error_msg == ""
        assert token_count > 0

    def test_validate_with_custom_encoding(self):
        """Test validation with custom encoding"""
        text = "This is a test text."

        is_valid, _error_msg, token_count = validate_text_for_embedding(
            text, encoding_name="cl100k_base"
        )

        assert is_valid is True
        assert token_count > 0


class TestTruncateTextToTokenLimit:
    """Tests for truncate_text_to_token_limit function"""

    def test_truncate_short_text_unchanged(self):
        """Test that short text is not truncated"""
        text = "This is a short text."

        result = truncate_text_to_token_limit(text)

        assert result == text
        assert "[... truncated" not in result

    def test_truncate_text_at_limit(self):
        """Test text at limit is not truncated"""
        # Create text just under the limit
        text = "word " * (MAX_EMBEDDING_TOKENS // 2)

        result = truncate_text_to_token_limit(text)

        # Should not be truncated if under limit
        assert "[... truncated" not in result

    def test_truncate_long_text(self):
        """Test that long text is truncated"""
        # Create text that definitely exceeds limit
        text = "word " * (MAX_EMBEDDING_TOKENS + 5000)

        result = truncate_text_to_token_limit(text)

        # Should be truncated
        assert "[... truncated to fit embedding token limit]" in result
        assert len(result) < len(text)

        # Verify truncated text is within limit
        _is_valid, _, token_count = validate_text_for_embedding(result)
        # Allow some overhead for the truncation message
        assert token_count <= MAX_EMBEDDING_TOKENS + 20

    def test_truncate_with_custom_max_tokens(self):
        """Test truncation with custom max_tokens"""
        text = "word " * 200  # ~200 tokens
        max_tokens = 50

        result = truncate_text_to_token_limit(text, max_tokens=max_tokens)

        # Should be truncated
        assert "[... truncated to fit embedding token limit]" in result

        # Count tokens in truncated result (excluding message)
        token_count = count_tokens(result)
        # Should be close to max_tokens (with some overhead for message)
        assert token_count <= max_tokens + 20

    def test_truncate_with_unicode(self):
        """Test truncation with unicode characters"""
        text = "Hello 世界! " * 2000

        result = truncate_text_to_token_limit(text, max_tokens=100)

        # Should be truncated and still valid unicode
        assert "[... truncated" in result
        # Ensure it's valid string (no encoding errors)
        assert isinstance(result, str)

    def test_truncate_empty_text(self):
        """Test truncating empty text"""
        text = ""

        result = truncate_text_to_token_limit(text)

        assert result == ""
        assert "[... truncated" not in result

    def test_truncate_preserves_structure(self):
        """Test that truncation preserves text structure"""
        text = "Line 1\nLine 2\nLine 3\n" * 3000

        result = truncate_text_to_token_limit(text, max_tokens=50)

        # Should be truncated
        assert "[... truncated" in result
        # Should still be a valid string
        assert isinstance(result, str)


class TestCountTokens:
    """Tests for count_tokens function"""

    def test_count_tokens_simple_text(self):
        """Test counting tokens in simple text"""
        text = "Hello world"

        token_count = count_tokens(text)

        assert token_count > 0
        assert isinstance(token_count, int)
        # "Hello world" is typically 2 tokens
        assert token_count == 2

    def test_count_tokens_empty_text(self):
        """Test counting tokens in empty text"""
        text = ""

        token_count = count_tokens(text)

        assert token_count == 0

    def test_count_tokens_long_text(self):
        """Test counting tokens in long text"""
        text = "word " * 1000

        token_count = count_tokens(text)

        assert token_count > 0
        # Should be approximately 1000 tokens (one per word)
        assert 900 < token_count < 1100

    def test_count_tokens_with_special_characters(self):
        """Test counting tokens with special characters"""
        text = "Hello! How are you? I'm fine. 😀"

        token_count = count_tokens(text)

        assert token_count > 0
        assert isinstance(token_count, int)

    def test_count_tokens_with_unicode(self):
        """Test counting tokens with unicode"""
        text = "世界 Ñoño café"

        token_count = count_tokens(text)

        assert token_count > 0

    def test_count_tokens_with_custom_encoding(self):
        """Test counting tokens with custom encoding"""
        text = "This is a test."

        token_count = count_tokens(text, encoding_name="cl100k_base")

        assert token_count > 0
        assert isinstance(token_count, int)


class TestIntegration:
    """Integration tests for text validation workflow"""

    def test_validate_and_truncate_workflow(self):
        """Test full workflow: validate -> truncate if needed"""
        # Create text that exceeds limit
        long_text = "word " * (MAX_EMBEDDING_TOKENS + 1000)

        # First, validate
        is_valid, _error_msg, token_count = validate_text_for_embedding(long_text)
        assert is_valid is False
        assert token_count > MAX_EMBEDDING_TOKENS

        # Then truncate
        truncated = truncate_text_to_token_limit(long_text)

        # Validate truncated text
        _is_valid_after, _, token_count_after = validate_text_for_embedding(truncated)
        # Allow some overhead for truncation message
        assert token_count_after <= MAX_EMBEDDING_TOKENS + 20

    def test_count_matches_validate(self):
        """Test that count_tokens matches token count from validate"""
        text = "This is a test text with several words."

        # Count directly
        direct_count = count_tokens(text)

        # Count from validate
        _, _, validate_count = validate_text_for_embedding(text)

        assert direct_count == validate_count
