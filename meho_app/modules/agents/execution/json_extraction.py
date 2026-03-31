# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
JSON extraction utilities for extracting verbatim examples from knowledge chunks.

This module provides functions to:
- Detect if a user is asking for example responses
- Extract preferred keywords from queries for matching
- Find and score JSON snippets in text
- Select the most relevant snippet based on context
"""

import re
from typing import Any


def requires_verbatim_example(context: str | None, question: str | None) -> bool:
    """
    Detect if the user explicitly requested an example/sample response/payload.

    Args:
        context: The conversation context
        question: The specific question asked

    Returns:
        True if user is explicitly asking for an example/sample
    """
    combined = " ".join(filter(None, [context or "", question or ""])).lower()
    keywords = ["example", "sample", "payload", "response", "snippet"]
    return any(keyword in combined for keyword in keywords)


def extract_preferred_keywords(context: str | None, question: str | None) -> list[str]:
    """
    Extract preferred keywords from user query for snippet matching.

    Prioritizes keywords that help identify the right JSON example:
    1. URL paths and endpoints (highest priority)
    2. Quoted strings (explicit mentions)
    3. Technical terms in uppercase
    4. Resource names near key phrases

    Args:
        context: The conversation context
        question: The specific question asked

    Returns:
        List of keywords ordered by relevance
    """
    combined = " ".join(filter(None, [context or "", question or ""]))
    keywords: list[str] = []

    # Priority 1: Extract URL paths/endpoints (e.g., /v1/roles, /api/users)
    # These are the most specific identifiers
    url_pattern = re.compile(r"(/[\w/-]+(?:/[\w/-]+)*)")
    for match in url_pattern.finditer(combined):
        path = match.group(1)
        if "/" in path and len(path) > 1:  # Must be actual path, not just /
            keywords.append(path)
            # Also add the resource name from the path (e.g., "roles" from "/v1/roles")
            parts = path.strip("/").split("/")
            if parts:
                resource = parts[-1]  # Last part is usually the resource
                if resource and len(resource) > 2:  # Avoid v1, v2, etc.
                    keywords.append(resource)

    # Priority 2: Extract quoted strings
    # User explicitly mentioned these terms
    quoted_pattern = re.compile(r'["\']([^"\']+)["\']')
    for match in quoted_pattern.finditer(combined):
        term = match.group(1)
        keywords.append(f'"{term}"')
        # Also add without quotes for broader matching
        if len(term) > 2:
            keywords.append(term)

    # Priority 3: Extract HTTP methods with endpoints (GET /endpoint, POST /api/...)
    method_pattern = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[\w/-]+)", re.IGNORECASE)
    for match in method_pattern.finditer(combined):
        method = match.group(1).upper()
        endpoint = match.group(2)
        keywords.append(f"{method} {endpoint}")
        keywords.append(endpoint)

    # Priority 4: Extract uppercase technical terms (API names, constants)
    uppercase_pattern = re.compile(r"\b[A-Z]{2,}\b")
    for match in uppercase_pattern.finditer(combined):
        term = match.group(0)
        # Skip common words that aren't useful
        if term not in {
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "HTTP",
            "API",
            "REST",
            "JSON",
            "XML",
            "VCF",
            "ID",
        }:
            keywords.append(term)

    # Priority 5: Extract resource names near "endpoint", "API", "response"
    # e.g., "roles endpoint" -> extract "roles"
    context_pattern = re.compile(
        r"\b(\w+)\s+(?:endpoint|api|response|request|resource)\b", re.IGNORECASE
    )
    for match in context_pattern.finditer(combined):
        resource = match.group(1)
        if len(resource) > 3 and resource.lower() not in {"that", "this", "what", "which", "show"}:
            keywords.append(resource)

    return keywords


def find_json_snippets(text: str) -> list[tuple[str, str]]:
    """
    Extract all JSON blocks from text with surrounding context.

    Searches for:
    1. JSON with "elements" array (common API response pattern)
    2. JSON arrays with objects
    3. Single JSON objects with typical entity fields

    Args:
        text: The text to search for JSON snippets

    Returns:
        List of (snippet, context) tuples where context is ~200 chars around the snippet
    """
    snippets: list[tuple[str, str]] = []

    # Pattern 1: JSON with "elements" array (common API response)
    elements_pattern = re.compile(r'\{\s*"elements"\s*:\s*\[[\s\S]+?\]\s*\}', re.IGNORECASE)
    for match in elements_pattern.finditer(text):
        snippet = match.group(0)
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 200)
        context = text[start:end]
        snippets.append((snippet, context))

    # Pattern 2: JSON arrays with objects (e.g., [{"id": "...", "name": "..."}])
    array_pattern = re.compile(r"\[\s*\{[\s\S]+?\}\s*\]", re.IGNORECASE)
    for match in array_pattern.finditer(text):
        snippet = match.group(0)
        # Skip if already found as part of elements pattern
        if not any(snippet in existing for existing, _ in snippets):
            start = max(0, match.start() - 200)
            end = min(len(text), match.end() + 200)
            context = text[start:end]
            if len(snippet) > 30:  # Skip tiny arrays
                snippets.append((snippet, context))

    # Pattern 3: Single JSON objects that look like responses
    # (must have id or name fields to avoid matching random JSON)
    object_pattern = re.compile(
        r'\{\s*(?:"[^"]+"\s*:\s*[^,}]+\s*,\s*)+?"(?:id|name|type|status)"\s*:[\s\S]+?\}',
        re.IGNORECASE,
    )
    for match in object_pattern.finditer(text):
        snippet = match.group(0)
        # Skip if already found
        if not any(snippet in existing for existing, _ in snippets):
            start = max(0, match.start() - 200)
            end = min(len(text), match.end() + 200)
            context = text[start:end]
            if len(snippet) > 40:  # Skip tiny objects
                snippets.append((snippet, context))

    return snippets


def score_snippet(snippet: str, context: str, keywords: list[str]) -> int:
    """
    Score a JSON snippet based on relevance indicators.

    Scoring factors:
    - Keyword matches in snippet (high value)
    - Keyword matches in context (medium value)
    - Context indicators ("example response", "sample:", etc.)
    - Snippet quality indicators (common patterns)

    Args:
        snippet: The JSON snippet text
        context: Surrounding text context
        keywords: Preferred keywords to match

    Returns:
        Relevance score (higher is better)
    """
    score = 0

    # Base score for having a snippet
    score += 10

    # Keyword matches in snippet (high value)
    for keyword in keywords:
        if keyword.lower() in snippet.lower():
            # Exact matches worth more
            if keyword in snippet:
                score += 50
            else:
                score += 30

    # Keyword matches in context (medium value)
    context_lower = context.lower()
    for keyword in keywords:
        if keyword.lower() in context_lower:
            score += 15

    # Context indicators (snippet is near helpful text)
    context_indicators = [
        ("example response", 40),
        ("sample response", 40),
        ("response example", 40),
        ("example:", 30),
        ("sample:", 30),
        ("response:", 25),
        ("returns", 20),
        ("output", 15),
    ]

    for indicator, points in context_indicators:
        if indicator in context_lower:
            score += points

    # Snippet quality indicators
    if '"elements"' in snippet:
        score += 25  # Common API response pattern
    if '"id"' in snippet and '"name"' in snippet:
        score += 20  # Typical entity structure
    if len(snippet) > 100:
        score += 10  # Prefer more complete examples

    return score


def extract_verbatim_snippet(
    results: list[dict[str, Any]], preferred_keywords: list[str] | None = None
) -> str | None:
    """
    Attempt to pull a JSON snippet directly from knowledge search results.

    Uses smart scoring to select the most relevant snippet based on:
    - Keyword matches (higher score for more matches)
    - Context indicators (near "example", "response", etc.)
    - Snippet quality (size, structure)

    Args:
        results: Knowledge search results containing text
        preferred_keywords: Optional keywords to prefer when selecting snippets

    Returns:
        Best matching JSON snippet if found, None otherwise
    """
    if preferred_keywords is None:
        preferred_keywords = []

    best_snippet: str | None = None
    best_score = 0

    for result in results:
        candidate_entries: list[Any]
        candidate_entries = result if isinstance(result, list) else [result]

        for entry in candidate_entries:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not text or not isinstance(text, str):
                continue

            # Try to find JSON snippets
            snippets = find_json_snippets(text)

            for snippet, context in snippets:
                # Score this snippet
                snippet_score = score_snippet(snippet, context, preferred_keywords)

                if snippet_score > best_score:
                    best_score = snippet_score
                    best_snippet = snippet

    return best_snippet
