# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Search utilities for knowledge base queries.

This module provides helper functions for:
- Detecting metadata filters from queries
- Detecting example requests
- Boosting code-containing chunks
- Estimating data sizes
- Formatting results for LLM consumption
"""

import json
import sys
from typing import Any


def detect_metadata_filters(query: str) -> dict[str, Any] | None:
    """
    Detect metadata filters to apply based on query intent.

    Analyzes the query for keywords that suggest specific resource types,
    content types, or other metadata that should be used to filter search results.

    Args:
        query: The search query string

    Returns:
        Dictionary of metadata filters or None if no specific filters detected

    Examples:
        "Show me an example" -> {"has_json_example": True}
        "Give me a code snippet" -> {"has_code_example": True}
    """
    query_lower = query.lower()
    filters: dict[str, Any] = {}

    # Resource type detection
    # DISABLED: Too aggressive - filters out relevant chunks due to metadata extraction imperfections
    # Example: Chunk about "Get the Roles" API was classified as resource_type="users" because
    # it's in a user management section, so searching "VCF roles" would miss it!
    #
    # Better approach: Let semantic search handle relevance, use metadata filters only
    # when user is VERY specific (e.g., "show me clusters with X" not just "what about clusters")

    # Content type detection
    # Only apply content filters if user EXPLICITLY asks for examples/code
    # Don't filter on "example" alone as it might be part of "endpoint example"
    if any(
        phrase in query_lower
        for phrase in [
            "show me an example",
            "give me an example",
            "json example",
            "response example",
            "sample response",
        ]
    ):
        filters["has_json_example"] = True
    elif any(phrase in query_lower for phrase in ["code example", "code snippet", "show me code"]):
        filters["has_code_example"] = True

    return filters if filters else None


def build_metadata_filters(queries: list[str]) -> dict[str, Any] | None:
    """
    Automatically build metadata filters from user queries.

    Extracts resource types, content types, endpoints, and other metadata
    to enable filtered search. Uses intent detection to identify what the
    user is looking for.

    Args:
        queries: List of search queries

    Returns:
        Dict of metadata filters for enhanced search, or None if no filters detected
    """
    # Combine all queries and detect filters
    combined_query = " ".join(queries)
    filters = detect_metadata_filters(combined_query)

    return filters


def is_example_request(queries: list[str]) -> bool:
    """
    Detect if user is asking for examples, samples, or response formats.

    Args:
        queries: List of search queries

    Returns:
        True if requesting examples/samples
    """
    combined = " ".join(queries).lower()
    indicators = [
        "example",
        "sample",
        "response",
        "payload",
        "json",
        "format",
        "output",
        "returns",
        "show me",
        "what does",
        "looks like",
    ]
    return any(indicator in combined for indicator in indicators)


def boost_code_containing_chunks(chunks: list[Any]) -> list[Any]:
    """
    Reorder chunks to prioritize those containing code/JSON examples.

    Chunks with code content are moved to the front of the list
    while maintaining relative order within each group.

    Args:
        chunks: List of knowledge chunks

    Returns:
        Reordered list with code chunks first
    """
    code_chunks: list[Any] = []
    other_chunks: list[Any] = []

    for chunk in chunks:
        text = chunk.text if hasattr(chunk, "text") else str(chunk)

        # Check if chunk contains code indicators
        has_code = (
            (
                "{" in text
                and "}" in text  # JSON-like content
                and ('"' in text or "'" in text)  # With strings
            )
            or (
                "elements" in text.lower() and ":" in text  # API response pattern
            )
            or (
                "```" in text  # Markdown code block
            )
            or (
                text.count("\n") > 10  # Multi-line
                and (text.count("{") > 2 or text.count("[") > 2)  # Multiple objects/arrays
            )
        )

        if has_code:
            code_chunks.append(chunk)
        else:
            other_chunks.append(chunk)

    # Return code chunks first, then others
    return code_chunks + other_chunks


def estimate_size(obj: Any) -> int:
    """
    Estimate size of object in bytes.

    Args:
        obj: Any Python object

    Returns:
        Estimated size in bytes
    """
    try:
        json_str = json.dumps(obj, default=str)
        return sys.getsizeof(json_str)
    except Exception:
        return sys.getsizeof(obj)


def format_result(result: dict[str, Any]) -> str:
    """
    Format a result dictionary for LLM consumption.

    Args:
        result: Result dictionary from knowledge search or API call

    Returns:
        Formatted string for LLM prompt
    """
    if "text" in result:
        # Knowledge chunk
        return f"Knowledge: {result['text']}"
    elif "data" in result:
        # API response
        return f"API Data: {json.dumps(result['data'], indent=2)}"
    else:
        # Generic result
        return json.dumps(result, indent=2)
