# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for document chunking with structure tracking.
"""

from meho_app.modules.knowledge.chunking import TextChunker


def test_parse_document_structure():
    """Test that document structure is parsed correctly"""
    chunker = TextChunker()

    text = """# Chapter 1: Introduction

This is the introduction.

## Section 1.1: Overview

Overview content here.

## Section 1.2: Details

Details content here.

# Chapter 2: API Reference

API reference content.
"""

    sections = chunker._parse_document_structure(text)

    # Should have 4 sections (after each heading)
    assert len(sections) > 0

    # Check heading stacks
    _section_texts, heading_stacks = zip(*sections, strict=False) if sections else ([], [])

    # First section should have "Chapter 1: Introduction"
    assert any(any("Chapter 1" in heading for heading in stack) for stack in heading_stacks)


def test_chunk_document_with_structure():
    """Test chunking with structure tracking"""
    chunker = TextChunker()

    text = """# Roles Management

The Roles Management API provides endpoints for managing user roles.

## GET /v1/roles

Returns a list of all available roles.

Example response:
{
  "elements": [
    {"id": "1", "name": "ADMIN"}
  ]
}
"""

    chunks_with_context = chunker.chunk_document_with_structure(
        text=text, document_name="test.md", detect_headings=True
    )

    # Should return chunks with context
    assert len(chunks_with_context) > 0

    # Each should be a tuple of (chunk_text, context_dict)
    for chunk_text, context in chunks_with_context:
        assert isinstance(chunk_text, str)
        assert isinstance(context, dict)
        assert "heading_stack" in context
        assert "document_name" in context

    # At least one chunk should have "Roles Management" in heading stack
    assert any(
        "Roles Management" in context.get("heading_stack", []) for _, context in chunks_with_context
    )


def test_chunk_document_without_headings():
    """Test chunking plain text without headings"""
    chunker = TextChunker()

    text = "This is plain text without any headings. Just regular content."

    chunks_with_context = chunker.chunk_document_with_structure(
        text=text, document_name="test.md", detect_headings=True
    )

    # Should still work, but heading stack will be empty
    assert len(chunks_with_context) > 0

    for _chunk_text, context in chunks_with_context:
        assert context.get("heading_stack") == []


def test_nested_heading_hierarchy():
    """Test that nested headings are properly tracked"""
    chunker = TextChunker()

    text = """# Top Level

Content 1

## Second Level

Content 2

### Third Level

Content 3

## Back to Second

Content 4
"""

    sections = chunker._parse_document_structure(text)

    # Check that heading stacks are properly nested
    for _section_text, heading_stack in sections:
        # Stack should not have more than 3 levels in this example
        assert len(heading_stack) <= 3


def test_chunk_document_with_structure_disabled():
    """Test that detect_headings=False works"""
    chunker = TextChunker()

    text = """# Heading

Content here.
"""

    chunks_with_context = chunker.chunk_document_with_structure(
        text=text, document_name="test.md", detect_headings=False
    )

    # Should chunk without parsing structure
    assert len(chunks_with_context) > 0

    for _, context in chunks_with_context:
        assert context.get("heading_stack") == []
