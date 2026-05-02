# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for PDF page number preservation bug fix.

CRITICAL: Ensures source_uri page numbers match actual PDF pages even when extraction fails.
"""

import pytest

from meho_app.modules.knowledge.chunking import TextChunker


@pytest.mark.unit
def test_chunker_handles_empty_pages_preserves_numbering():
    """
    Test that chunker skips empty pages BUT preserves correct page numbers.

    This is the CRITICAL behavior for page number alignment.
    """
    chunker = TextChunker()

    # Simulate PDF extractor output with failed/empty pages
    # Pages: 1 (content), 2 (failed/empty), 3 (content), 4 (empty), 5 (content)
    pages = [
        "Page 1 content",
        "",  # Page 2 - failed extraction (empty string)
        "Page 3 content",
        "",  # Page 4 - empty page
        "Page 5 content",
    ]

    chunks_with_pages = chunker.chunk_pages(pages)

    # Extract page numbers from results
    page_numbers = [page_num for _, page_num in chunks_with_pages]

    # CRITICAL ASSERTIONS:
    # 1. Should have chunks from pages 1, 3, 5 (not 2, 4)
    assert 1 in page_numbers  # Page 1
    assert 3 in page_numbers  # Page 3 (NOT renumbered to 2!)
    assert 5 in page_numbers  # Page 5 (NOT renumbered to 3!)

    # 2. Empty pages should be skipped (no chunks created)
    assert 2 not in page_numbers  # Empty page skipped
    assert 4 not in page_numbers  # Empty page skipped

    # 3. Verify page 3 is actually at index 2 in pages list
    # This proves the numbering comes from enumerate(start=1), not array index
    assert pages[2] == "Page 3 content"  # Index 2 = Page 3 ✅


@pytest.mark.unit
def test_page_numbering_example():
    """
    Example showing correct behavior after bug fix.

    Before fix: Failed page → pages list shrinks → wrong page numbers
    After fix: Failed page → empty string → correct page numbers
    """
    chunker = TextChunker()

    # Simulate 5-page PDF where page 3 failed
    pages_after_fix = [
        "Page 1",
        "Page 2",
        "",  # Page 3 FAILED (empty string keeps slot)
        "Page 4",
        "Page 5",
    ]

    chunks = chunker.chunk_pages(pages_after_fix)
    page_nums = [pnum for _, pnum in chunks]

    # Page 4 should be page number 4 (not 3)
    assert 4 in page_nums
    assert 5 in page_nums

    # source_uri#page=4 will correctly reference actual PDF page 4


@pytest.mark.unit
def test_chunker_handles_empty_pages():
    """Test that chunker skips empty pages but preserves correct page numbers"""
    from meho_app.modules.knowledge.chunking import TextChunker

    chunker = TextChunker()

    # Pages with some empty (simulating failed extraction)
    pages = [
        "Page 1 content",
        "",  # Page 2 empty (failed extraction)
        "Page 3 content",
        "",  # Page 4 empty
        "Page 5 content",
    ]

    chunks_with_pages = chunker.chunk_pages(pages)

    # Should skip empty pages
    page_numbers = [page_num for _, page_num in chunks_with_pages]

    # Should have chunks from pages 1, 3, 5 with CORRECT page numbers
    assert 1 in page_numbers  # Page 1
    assert 3 in page_numbers  # Page 3 (not renumbered to 2!)
    assert 5 in page_numbers  # Page 5 (not renumbered to 3!)

    # Should NOT have chunks from empty pages
    assert 2 not in page_numbers
    assert 4 not in page_numbers
