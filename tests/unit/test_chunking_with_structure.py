# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for document chunking with heading-aware structure."""

from meho_app.modules.knowledge.chunking import TextChunker


def test_chunk_document_with_structure() -> None:
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

    assert len(chunks_with_context) > 0
    for chunk_text, context in chunks_with_context:
        assert isinstance(chunk_text, str)
        assert isinstance(context, dict)
        assert "heading_stack" in context
        assert context["document_name"] == "test.md"

    assert any(
        "Roles Management" in context.get("heading_stack", []) for _, context in chunks_with_context
    )


def test_chunk_document_without_headings() -> None:
    chunker = TextChunker()
    text = "This is plain text without any headings. Just regular content."
    chunks_with_context = chunker.chunk_document_with_structure(
        text=text, document_name="test.md", detect_headings=True
    )
    assert len(chunks_with_context) > 0
    for _, context in chunks_with_context:
        assert context.get("heading_stack") == []


def test_nested_heading_hierarchy() -> None:
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
    chunks = chunker.chunk_document_with_structure(text, document_name="t.md")
    paths = {tuple(ctx["heading_stack"]) for _, ctx in chunks}
    assert ("Top Level",) in paths
    assert ("Top Level", "Second Level") in paths
    assert ("Top Level", "Second Level", "Third Level") in paths
    assert ("Top Level", "Back to Second") in paths


def test_chunk_document_with_structure_disabled() -> None:
    chunker = TextChunker()
    text = "# Heading\n\nContent here.\n"
    chunks_with_context = chunker.chunk_document_with_structure(
        text=text, document_name="test.md", detect_headings=False
    )
    assert len(chunks_with_context) > 0
    for _, context in chunks_with_context:
        assert context.get("heading_stack") == []
