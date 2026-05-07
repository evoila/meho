# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for document_converter shared utilities.

Tests cover SUPPORTED_MIME_TYPES, build_chunk_prefix, and
generate_document_summary used by the lightweight converter ingestion path.
"""

from __future__ import annotations

import pytest

from meho_app.modules.knowledge.document_converter import (
    SUPPORTED_MIME_TYPES,
    build_chunk_prefix,
)


# ---------------------------------------------------------------------------
# Tests: SUPPORTED_MIME_TYPES
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_supported_mime_types_contains_pdf() -> None:
    """PDF MIME type is in the supported set."""
    assert "application/pdf" in SUPPORTED_MIME_TYPES


@pytest.mark.unit
def test_supported_mime_types_contains_docx() -> None:
    """DOCX MIME type is in the supported set."""
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert docx in SUPPORTED_MIME_TYPES


@pytest.mark.unit
def test_supported_mime_types_contains_html() -> None:
    """HTML MIME type is in the supported set."""
    assert "text/html" in SUPPORTED_MIME_TYPES


@pytest.mark.unit
def test_supported_mime_types_is_frozenset() -> None:
    """SUPPORTED_MIME_TYPES is immutable."""
    assert isinstance(SUPPORTED_MIME_TYPES, frozenset)


# ---------------------------------------------------------------------------
# Tests: build_chunk_prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_chunk_prefix_full() -> None:
    """Full args: type + name + summary."""
    result = build_chunk_prefix("kubernetes", "prod-eu", "Runbook for K8s.")
    assert result == "kubernetes connector (prod-eu). Runbook for K8s."


@pytest.mark.unit
def test_build_chunk_prefix_type_and_name() -> None:
    """Type and name, no summary."""
    result = build_chunk_prefix("vmware", "dc-west")
    assert result == "vmware connector (dc-west)."


@pytest.mark.unit
def test_build_chunk_prefix_type_only() -> None:
    """Type only, no name, no summary."""
    result = build_chunk_prefix("kubernetes")
    assert result == "kubernetes connector."


@pytest.mark.unit
def test_build_chunk_prefix_summary_only() -> None:
    """Summary only, no type or name."""
    result = build_chunk_prefix(document_summary="A summary.")
    assert result == "A summary."


@pytest.mark.unit
def test_build_chunk_prefix_empty() -> None:
    """No args returns empty string."""
    result = build_chunk_prefix()
    assert result == ""
