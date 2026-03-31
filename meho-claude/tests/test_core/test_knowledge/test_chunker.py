"""Tests for heading-aware markdown chunker."""

from __future__ import annotations

from meho_claude.core.knowledge.chunker import Chunk, chunk_markdown


class TestChunkMarkdown:
    """Tests for chunk_markdown function."""

    def test_empty_string_returns_empty_list(self):
        """chunk_markdown("") returns empty list."""
        result = chunk_markdown("")
        assert result == []

    def test_whitespace_only_returns_empty_list(self):
        """chunk_markdown with only whitespace returns empty list."""
        result = chunk_markdown("   \n\n  ")
        assert result == []

    def test_single_heading_short_content_returns_one_chunk(self):
        """chunk_markdown with single heading + short content returns one chunk."""
        text = "# Introduction\n\nThis is a short paragraph about the topic."
        result = chunk_markdown(text)
        assert len(result) == 1
        assert result[0].heading == "# Introduction"
        assert "Introduction" in result[0].content
        assert "short paragraph" in result[0].content
        assert result[0].chunk_index == 0

    def test_multiple_headings_splits_at_boundaries(self):
        """chunk_markdown with multiple headings splits at heading boundaries."""
        text = (
            "# Section One\n\n"
            "Content for section one.\n\n"
            "# Section Two\n\n"
            "Content for section two.\n\n"
            "## Subsection\n\n"
            "Content for subsection."
        )
        result = chunk_markdown(text)
        assert len(result) == 3
        assert result[0].heading == "# Section One"
        assert result[1].heading == "# Section Two"
        assert result[2].heading == "## Subsection"

    def test_oversized_section_falls_back_to_paragraph_splitting(self):
        """chunk_markdown with oversized section falls back to paragraph splitting."""
        # Create a section that exceeds max_tokens (use small limit for test)
        long_paragraph1 = "word " * 50  # ~50 words
        long_paragraph2 = "text " * 50  # ~50 words
        text = f"# Big Section\n\n{long_paragraph1}\n\n{long_paragraph2}"
        # With max_tokens=40 (30 words * 1/0.75 = 40 tokens), each paragraph should split
        result = chunk_markdown(text, max_tokens=40)
        assert len(result) > 1
        for chunk in result:
            assert chunk.heading == "# Big Section"

    def test_no_headings_falls_back_to_splitting(self):
        """chunk_markdown with no headings at all falls back to paragraph/fixed splitting."""
        paragraph1 = "First paragraph with enough words to be meaningful content. " * 3
        paragraph2 = "Second paragraph with different content about another topic. " * 3
        text = f"{paragraph1}\n\n{paragraph2}"
        # With small token limit, it should still produce chunks
        result = chunk_markdown(text, max_tokens=30)
        assert len(result) >= 1
        # All chunks should have empty heading since there are no headings
        for chunk in result:
            assert chunk.heading == ""

    def test_chunk_has_required_fields(self):
        """Each Chunk has content, heading, chunk_index, token_estimate fields."""
        text = "# Hello\n\nWorld"
        result = chunk_markdown(text)
        assert len(result) == 1
        chunk = result[0]
        assert isinstance(chunk, Chunk)
        assert isinstance(chunk.content, str)
        assert isinstance(chunk.heading, str)
        assert isinstance(chunk.chunk_index, int)
        assert isinstance(chunk.token_estimate, int)

    def test_token_estimate_approximation(self):
        """Token estimate is approximately len(words) / 0.75."""
        text = "# Test\n\nOne two three four five six seven eight"
        result = chunk_markdown(text)
        assert len(result) == 1
        chunk = result[0]
        # The chunk content includes the heading line "# Test\n" + content
        word_count = len(chunk.content.split())
        expected_tokens = int(word_count / 0.75)
        assert chunk.token_estimate == expected_tokens

    def test_chunk_indexes_are_sequential(self):
        """chunk_index values are sequential starting from 0."""
        text = "# A\n\nContent A\n\n# B\n\nContent B\n\n# C\n\nContent C"
        result = chunk_markdown(text)
        for i, chunk in enumerate(result):
            assert chunk.chunk_index == i

    def test_content_includes_heading_line(self):
        """Each chunk's content includes its heading line."""
        text = "# My Heading\n\nMy content"
        result = chunk_markdown(text)
        assert len(result) == 1
        assert result[0].content.startswith("# My Heading")

    def test_preamble_before_first_heading(self):
        """Content before the first heading is captured as a separate chunk."""
        text = "Some preamble text.\n\n# First Heading\n\nFirst content."
        result = chunk_markdown(text)
        assert len(result) == 2
        assert result[0].heading == ""
        assert "preamble" in result[0].content
        assert result[1].heading == "# First Heading"
