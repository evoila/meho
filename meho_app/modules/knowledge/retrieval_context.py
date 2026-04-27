# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Helpers for building retrieval-oriented text context.

These helpers intentionally mirror the retrieval text shape used in Farseer so
that MEHO can embed and rerank the same textual representation of a chunk.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote, urlparse

from meho_app.modules.knowledge.schemas import ChunkMetadata


def clean_heading_path(heading_path: Sequence[str] | None) -> list[str]:
    """Normalize a heading path by dropping empty segments."""
    if not heading_path:
        return []
    return [part.strip() for part in heading_path if part and part.strip()]


def format_heading_path(heading_path: Sequence[str] | None) -> str:
    """Render a heading path for display or retrieval context."""
    return " > ".join(clean_heading_path(heading_path))


def format_page_range(
    *,
    page_number: int = 0,
    page_start: int = 0,
    page_end: int = 0,
) -> str:
    """Render a compact page label."""
    start = page_start or page_number
    end = page_end or page_start or page_number
    if start <= 0 and end <= 0:
        return ""
    if start > 0 and end > 0 and end != start:
        return f"p.{start}-{end}"
    return f"p.{start or end}"


def extract_filename(source_uri: str | None, document_name: str = "") -> str:
    """Extract a user-facing filename from stored metadata or source URI."""
    if document_name.strip():
        return document_name.strip()
    if not source_uri:
        return ""

    raw_source = source_uri.split("#", 1)[0]
    parsed = urlparse(raw_source)
    candidate = parsed.path or raw_source
    if not candidate:
        return ""

    return unquote(candidate.rsplit("/", 1)[-1]).strip()


def build_retrieval_text(
    text: str,
    *,
    filename: str = "",
    section_header: str = "",
    heading_path: Sequence[str] | None = None,
    page_number: int = 0,
    page_start: int = 0,
    page_end: int = 0,
) -> str:
    """Build a retrieval-friendly text block without mutating raw chunk text."""
    lines: list[str] = []

    if filename:
        lines.append(f"File: {filename}")

    path = format_heading_path(heading_path)
    if not path and section_header.strip():
        path = section_header.strip()
    if path:
        lines.append(f"Section path: {path}")

    page_label = format_page_range(
        page_number=page_number,
        page_start=page_start,
        page_end=page_end,
    )
    if page_label:
        lines.append(f"Pages: {page_label}")

    body = text.strip()
    if body:
        lines.append(body)

    return "\n".join(lines)


def _metadata_value(
    metadata: ChunkMetadata | dict[str, Any] | None,
    key: str,
) -> Any:
    """Read a retrieval field from either a schema object or a plain dict."""
    if metadata is None:
        return None
    if isinstance(metadata, dict):
        return metadata.get(key)
    return getattr(metadata, key, None)


def _normalize_page_numbers(page_numbers: Sequence[Any] | None) -> list[int]:
    """Normalize page numbers to a sorted list of positive integers."""
    if not page_numbers:
        return []

    normalized: list[int] = []
    for value in page_numbers:
        try:
            page_number = int(value)
        except (TypeError, ValueError):
            continue
        if page_number > 0:
            normalized.append(page_number)

    return sorted(set(normalized))


def build_retrieval_text_from_metadata(
    *,
    text: str,
    source_uri: str | None,
    metadata: ChunkMetadata | dict[str, Any] | None,
) -> str:
    """Build retrieval text from stored chunk text plus MEHO metadata."""
    heading_path = clean_heading_path(
        _metadata_value(metadata, "heading_hierarchy")
        or _metadata_value(metadata, "heading_stack")
        or []
    )
    section_header = str(
        _metadata_value(metadata, "section")
        or _metadata_value(metadata, "chapter")
        or (heading_path[-1] if heading_path else "")
    )
    page_numbers = _normalize_page_numbers(_metadata_value(metadata, "page_numbers"))
    page_number = int(_metadata_value(metadata, "page_number") or 0)
    page_start = int(_metadata_value(metadata, "page_start") or 0)
    page_end = int(_metadata_value(metadata, "page_end") or 0)

    if page_numbers:
        if page_number <= 0:
            page_number = page_numbers[0]
        if page_start <= 0:
            page_start = page_numbers[0]
        if page_end <= 0:
            page_end = page_numbers[-1]

    filename = extract_filename(
        source_uri=source_uri,
        document_name=str(_metadata_value(metadata, "document_name") or ""),
    )

    return build_retrieval_text(
        text,
        filename=filename,
        section_header=section_header,
        heading_path=heading_path,
        page_number=page_number,
        page_start=page_start,
        page_end=page_end,
    )
