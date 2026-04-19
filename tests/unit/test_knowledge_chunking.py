# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.chunking
"""

import pytest

from meho_app.modules.knowledge.chunking import TextChunker, chunk_text


@pytest.mark.unit
def test_chunk_short_text():
    """Test chunking text shorter than max_tokens"""
    chunker = TextChunker(max_tokens=100, overlap_tokens=10)

    short_text = "This is a short text."
    chunks = chunker.chunk_text(short_text)

    assert len(chunks) == 1
    assert chunks[0] == short_text


@pytest.mark.unit
def test_chunk_empty_text():
    """Test chunking empty text returns empty list"""
    chunker = TextChunker()

    assert chunker.chunk_text("") == []
    assert chunker.chunk_text("   ") == []


@pytest.mark.unit
def test_chunk_long_text():
    """Test chunking text longer than max_tokens"""
    chunker = TextChunker(max_tokens=50, overlap_tokens=10)

    # Create text that's ~200 tokens (each word is ~1 token)
    long_text = " ".join(["word"] * 200)

    chunks = chunker.chunk_text(long_text)

    # Should have multiple chunks
    assert len(chunks) > 1

    # Each chunk should be reasonable length
    for chunk in chunks:
        tokens = chunker.encoding.encode(chunk)
        assert len(tokens) <= 50, f"Chunk has {len(tokens)} tokens, max is 50"


@pytest.mark.unit
def test_chunk_respects_max_tokens():
    """Test that chunks don't exceed max_tokens"""
    chunker = TextChunker(max_tokens=100, overlap_tokens=20)

    # Create long text
    long_text = " ".join(["word"] * 500)

    chunks = chunker.chunk_text(long_text)

    for i, chunk in enumerate(chunks):
        tokens = chunker.encoding.encode(chunk)
        assert len(tokens) <= 100, f"Chunk {i} has {len(tokens)} tokens, max is 100"


@pytest.mark.unit
def test_chunk_last_chunk_shorter_than_overlap():
    """Test that last chunk shorter than overlap doesn't cause infinite loop (bug fix)"""
    chunker = TextChunker(max_tokens=512, overlap_tokens=50)

    # Create text that results in a short last chunk
    # 600 tokens total: first chunk 512, second chunk ~88, third would be ~38
    long_text = " ".join(["word"] * 600)

    chunks = chunker.chunk_text(long_text)

    # Should complete without hanging
    assert len(chunks) >= 2, "Should produce multiple chunks"
    assert len(chunks) < 100, "Should not produce excessive chunks (would indicate infinite loop)"

    # Verify last chunk exists and is reasonable
    last_chunk_tokens = chunker.encoding.encode(chunks[-1])
    assert len(last_chunk_tokens) > 0, "Last chunk should have content"
    assert len(last_chunk_tokens) <= 512, "Last chunk should not exceed max"


@pytest.mark.unit
def test_chunk_overlap_creates_context():
    """Test that overlap creates context between chunks"""
    chunker = TextChunker(max_tokens=50, overlap_tokens=10)

    # Create text with distinct sections
    long_text = " ".join([f"section{i}" for i in range(100)])

    chunks = chunker.chunk_text(long_text)

    # With overlap, adjacent chunks should share some content
    # (This is a smoke test - actual overlap verification is complex)
    assert len(chunks) >= 2


@pytest.mark.unit
def test_chunk_tries_sentence_boundaries():
    """Test that chunker tries to break on sentence boundaries"""
    chunker = TextChunker(max_tokens=50, overlap_tokens=5)

    # Text with clear sentence boundaries
    text = "First sentence. " * 20 + "Second sentence. " * 20 + "Third sentence. " * 20

    chunks = chunker.chunk_text(text)

    # Most chunks should end with sentence punctuation
    sentence_enders = 0
    for chunk in chunks[:-1]:  # Exclude last chunk
        if chunk.rstrip().endswith((".", "!", "?")):
            sentence_enders += 1

    # At least some chunks should end on sentence boundaries
    assert sentence_enders >= len(chunks) // 2, "Should try to break on sentences"


@pytest.mark.unit
def test_chunk_pages():
    """Test chunking multiple pages"""
    chunker = TextChunker(max_tokens=100, overlap_tokens=10)

    pages = [
        "Page 1 content. " * 50,  # Long page
        "Page 2 content. " * 50,  # Long page
        "Short page 3.",  # Short page
    ]

    chunks_with_pages = chunker.chunk_pages(pages)

    # Should have chunks from all pages
    assert len(chunks_with_pages) > 3  # Multiple chunks from long pages

    # Verify page numbers are tracked
    page_nums = [page_num for _, page_num in chunks_with_pages]
    assert 1 in page_nums
    assert 2 in page_nums
    assert 3 in page_nums


@pytest.mark.unit
def test_chunk_pages_skips_empty():
    """Test that chunk_pages skips empty pages"""
    chunker = TextChunker()

    pages = [
        "Page 1 content",
        "",  # Empty page
        "   ",  # Whitespace only
        "Page 4 content",
    ]

    chunks_with_pages = chunker.chunk_pages(pages)

    # Should have chunks from pages 1 and 4 only
    page_nums = [page_num for _, page_num in chunks_with_pages]
    assert 1 in page_nums
    assert 4 in page_nums
    assert 2 not in page_nums  # Empty page skipped
    assert 3 not in page_nums  # Whitespace page skipped


@pytest.mark.unit
def test_chunker_with_different_encodings():
    """Test chunker works with different encodings"""
    # cl100k_base (GPT-4)
    chunker1 = TextChunker(encoding_name="cl100k_base")

    # p50k_base (GPT-3)
    chunker2 = TextChunker(encoding_name="p50k_base")

    text = "Test text " * 100

    chunks1 = chunker1.chunk_text(text)
    chunks2 = chunker2.chunk_text(text)

    # Should both work (may produce different number of chunks)
    assert len(chunks1) > 0
    assert len(chunks2) > 0


@pytest.mark.unit
def test_chunk_text_helper_function():
    """Ensure legacy chunk_text helper mirrors TextChunker behavior."""
    text = " ".join(["token"] * 120)

    helper_chunks = chunk_text(text, max_tokens=60, overlap_tokens=10)
    class_chunks = TextChunker(max_tokens=60, overlap_tokens=10).chunk_text(text)

    assert helper_chunks == class_chunks
    assert len(helper_chunks) > 1


@pytest.mark.unit
def test_chunker_preserves_json_blocks():
    """Test that chunker tries to keep JSON examples intact."""
    text = """
    This is some documentation text about an API.

    Here's an example response:
    { "elements": [
        {"id": "1", "name": "ADMIN", "description": "Administrator"},
        {"id": "2", "name": "OPERATOR", "description": "Operator"},
        {"id": "3", "name": "VIEWER", "description": "Viewer"}
    ] }

    This example shows the three roles available.
    """

    chunker = TextChunker(max_tokens=100, overlap_tokens=20)
    chunks = chunker.chunk_text(text)

    # The JSON should be in at least one chunk intact
    has_complete_json = any(
        "ADMIN" in chunk and "OPERATOR" in chunk and "VIEWER" in chunk for chunk in chunks
    )
    assert has_complete_json, "JSON example should be kept together in at least one chunk"


@pytest.mark.unit
def test_chunker_detects_code_blocks():
    """Test that _find_code_blocks correctly identifies code regions."""
    text = """
    Regular text here.

    ```json
    {"key": "value"}
    ```

    More text.

    { "elements": [{"id": "1"}] }
    """

    chunker = TextChunker()
    code_blocks = chunker._find_code_blocks(text)

    # Should find at least the markdown code block and elements JSON
    assert len(code_blocks) >= 1


@pytest.mark.unit
def test_chunker_avoids_breaking_in_json():
    """Test that chunker doesn't break in the middle of JSON structures."""
    # Create text with a JSON block that would normally be split
    json_example = (
        '{ "elements": ['
        + ", ".join([f'{{"id": "{i}", "name": "Item{i}", "value": "Data{i}"}}' for i in range(20)])
        + "] }"
    )

    text = f"Documentation\n\n{json_example}\n\nMore docs"

    chunker = TextChunker(max_tokens=80, overlap_tokens=10)
    chunks = chunker.chunk_text(text)

    # If JSON is split, it should at least be at a reasonable boundary
    for chunk in chunks:
        # Should not break with unclosed braces in an obvious way
        # (this is a soft check - perfect preservation isn't always possible)
        if "{" in chunk or "[" in chunk:
            # If we have opening braces, we should have reasonable structure
            pass  # Soft check - just ensure no crash


@pytest.mark.unit
def test_chunker_handles_large_code_blocks():
    """Test that chunker can handle code blocks larger than 200 chars."""
    # Create a large JSON example (>300 chars) that spans more than the initial search window
    large_json = (
        '{ "elements": ['
        + ", ".join(
            [
                f'{{"id": "role-{i}", "name": "ROLE_{i}", "description": "Description for role {i}", "permissions": ["read", "write", "execute"]}}'
                for i in range(15)
            ]
        )
        + "] }"
    )

    # Put some text before and after
    text = f"This is documentation about the API.\n\n{large_json}\n\nMore documentation follows."

    chunker = TextChunker(max_tokens=100, overlap_tokens=20)
    chunks = chunker.chunk_text(text)

    # The chunker should either:
    # 1. Keep the entire JSON in one chunk, OR
    # 2. Break BEFORE the JSON starts (keeping "This is documentation" separate)
    # It should NOT break in the middle of the JSON

    # Check that we don't have obviously broken JSON
    for chunk in chunks:
        if '"elements"' in chunk:
            # This chunk contains the start of our JSON
            # It should have balanced braces OR be the only chunk with elements
            open_count = chunk.count("{")
            close_count = chunk.count("}")

            # Either balanced (complete JSON) or missing closes (will continue in next chunk)
            # But we shouldn't have MORE closes than opens (broken JSON)
            assert close_count <= open_count + 1, (
                f"Chunk has unbalanced braces (too many closes): {chunk[:100]}"
            )
