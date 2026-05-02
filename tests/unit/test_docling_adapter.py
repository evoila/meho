# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for DoclingWrapperAdapter.

Tests cover chunk translation (heading path enrichment, metadata mapping),
temp file management, progress callback bridging, unsupported MIME handling,
and empty chunk filtering.

All DoclingWrapper calls are mocked -- no real Docling/PyTorch needed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Inject fake docling modules so docling_wrapper.py can be imported without
# the real docling package -- but only for modules that are genuinely
# missing. Pre-empting an actually-installed module leaks the MagicMock
# into sys.modules and breaks downstream tests. Note: torch is deliberately
# NOT in this list -- docling_wrapper.py does not import torch at module
# import time, so pre-empting it was never necessary.
# ---------------------------------------------------------------------------

_DOCLING_MODULES = [
    "docling",
    "docling.chunking",
    "docling.datamodel",
    "docling.datamodel.base_models",
    "docling.datamodel.document",
    "docling.datamodel.pipeline_options",
    "docling.document_converter",
    "docling.pipeline",
    "docling.pipeline.base_pipeline",
    "docling_core",
    "docling_core.types",
    "docling_core.types.doc",
    "docling_core.types.doc.labels",
    "pypdfium2",
    "psutil",
    "pikepdf",
]


def _module_is_real(name: str) -> bool:
    """Return True when ``name`` resolves to an actual installed package."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


for _mod_name in _DOCLING_MODULES:
    if _mod_name in sys.modules or _module_is_real(_mod_name):
        continue
    sys.modules[_mod_name] = MagicMock()

# Force reimport of wrapper and adapter with mocked docling
for _mod in [
    "meho_app.modules.knowledge.docling_wrapper",
    "meho_app.modules.knowledge.docling_adapter",
]:
    if _mod in sys.modules:
        del sys.modules[_mod]

from meho_app.modules.knowledge.docling_wrapper import (
    Chunk,
    ConversionResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    chunks: list[Chunk] | None = None,
    markdown: str = "# Test\nContent",
    source: str = "test.pdf",
    pages: int = 5,
) -> ConversionResult:
    """Create a ConversionResult for testing."""
    return ConversionResult(
        markdown=markdown,
        text="Test content",
        html="<h1>Test</h1>",
        chunks=chunks or [],
        pages=pages,
        elapsed=1.5,
        mem_peak_mb=100.0,
        mem_avg_mb=80.0,
        source=Path(source),
        format="pdf",
        chunk_count=1,
        file_size=1024,
    )


# ---------------------------------------------------------------------------
# Tests: chunk_document -- heading path enrichment
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_document_keeps_raw_text() -> None:
    """Chunk text stays raw; heading context is carried in metadata."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(
                text="VLAN configuration steps",
                headings=["Chapter 3", "Section 3.2", "3.2.1 VLANs"],
                page_numbers=[12, 13],
            )
        ],
        source="network_guide.pdf",
    )

    chunks = adapter.chunk_document(result)

    assert len(chunks) == 1
    text, ctx = chunks[0]
    assert text == "VLAN configuration steps"
    assert ctx["heading_stack"] == ["Chapter 3", "Section 3.2", "3.2.1 VLANs"]


@pytest.mark.unit
def test_chunk_document_no_headings() -> None:
    """Chunks without headings have no heading path prefix."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(
                text="Some content without headings",
                headings=[],
                page_numbers=[1],
            )
        ]
    )

    chunks = adapter.chunk_document(result)

    assert len(chunks) == 1
    text, _ = chunks[0]
    assert text == "Some content without headings"


# ---------------------------------------------------------------------------
# Tests: chunk_document -- metadata mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_document_metadata_mapping() -> None:
    """Context dict maps wrapper Chunk fields to MEHO expected keys."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(
                text="Content here",
                headings=["Chapter 1", "Section 1.1"],
                page_numbers=[5, 6],
            )
        ],
        source="manual.pdf",
    )

    chunks = adapter.chunk_document(result)

    assert len(chunks) == 1
    _, ctx = chunks[0]
    assert ctx["heading_stack"] == ["Chapter 1", "Section 1.1"]
    assert ctx["page_numbers"] == [5, 6]
    assert ctx["document_name"] == "manual.pdf"


# ---------------------------------------------------------------------------
# Tests: chunk_document -- prefix prepending
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_document_with_prefix() -> None:
    """chunk_prefix no longer mutates the stored chunk body."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(
                text="Pod lifecycle description",
                headings=["Kubernetes Basics"],
                page_numbers=[3],
            )
        ]
    )

    chunks = adapter.chunk_document(result, chunk_prefix="kubernetes connector (prod).")

    assert len(chunks) == 1
    text, _ = chunks[0]
    assert text == "Pod lifecycle description"


@pytest.mark.unit
def test_chunk_document_without_prefix() -> None:
    """Empty chunk_prefix leaves the raw chunk body unchanged."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(
                text="Raw content",
                headings=["Intro"],
                page_numbers=[1],
            )
        ]
    )

    chunks = adapter.chunk_document(result, chunk_prefix="")

    assert len(chunks) == 1
    text, _ = chunks[0]
    assert text == "Raw content"


# ---------------------------------------------------------------------------
# Tests: chunk_document -- empty chunk filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_document_filters_empty_chunks() -> None:
    """Chunks with empty or whitespace-only text are filtered out."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(text="", headings=["Empty"], page_numbers=[1]),
            Chunk(text="   \n  ", headings=["Whitespace"], page_numbers=[2]),
            Chunk(text="Real content", headings=["Valid"], page_numbers=[3]),
        ]
    )

    chunks = adapter.chunk_document(result)

    assert len(chunks) == 1
    text, _ = chunks[0]
    assert "Real content" in text


# ---------------------------------------------------------------------------
# Tests: get_full_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_full_text() -> None:
    """get_full_text returns the markdown from ConversionResult."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(markdown="# Title\nContent here")

    text = adapter.get_full_text(result)

    assert text == "# Title\nContent here"


# ---------------------------------------------------------------------------
# Tests: convert_file -- MIME type handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_convert_file_unsupported_mime() -> None:
    """convert_file raises ValueError for unsupported MIME types."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    with pytest.raises(ValueError, match="Unsupported MIME type"):
        adapter.convert_file(b"data", "test.xyz", "application/unknown")


@pytest.mark.unit
def test_convert_file_calls_wrapper_parse() -> None:
    """convert_file writes temp file and calls wrapper.parse()."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    mock_wrapper = MagicMock()
    mock_wrapper.parse.return_value = _make_result()
    adapter._wrapper = mock_wrapper

    result = adapter.convert_file(b"fake-pdf-bytes", "test.pdf", "application/pdf")

    assert mock_wrapper.parse.call_count == 1
    assert result.markdown == "# Test\nContent"


@pytest.mark.unit
def test_convert_file_cleans_up_temp_file() -> None:
    """Temp file is deleted after conversion, even on success."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    mock_wrapper = MagicMock()
    mock_wrapper.parse.return_value = _make_result()
    adapter._wrapper = mock_wrapper

    adapter.convert_file(b"fake-pdf-bytes", "test.pdf", "application/pdf")

    call_args = mock_wrapper.parse.call_args[0][0]
    tmp_path = Path(str(call_args))
    assert not tmp_path.exists(), "Temp file should be cleaned up"


@pytest.mark.unit
def test_convert_file_cleans_up_on_error() -> None:
    """Temp file is deleted even when wrapper.parse() raises."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    mock_wrapper = MagicMock()
    mock_wrapper.parse.side_effect = RuntimeError("conversion failed")
    adapter._wrapper = mock_wrapper

    with pytest.raises(ValueError, match="Failed to convert"):
        adapter.convert_file(b"bad-data", "test.pdf", "application/pdf")


# ---------------------------------------------------------------------------
# Tests: progress callback bridging
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_progress_callback_setter() -> None:
    """on_progress property delegates to wrapper."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    mock_wrapper = MagicMock()
    adapter._wrapper = mock_wrapper

    callback = MagicMock()
    adapter.on_progress = callback

    assert mock_wrapper.on_progress == callback


# ---------------------------------------------------------------------------
# Tests: multiple chunks ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_document_preserves_order() -> None:
    """Chunks are returned in the same order as the wrapper produces them."""
    from meho_app.modules.knowledge.docling_adapter import DoclingWrapperAdapter

    adapter = DoclingWrapperAdapter.__new__(DoclingWrapperAdapter)
    adapter._wrapper = MagicMock()

    result = _make_result(
        chunks=[
            Chunk(text="First chunk", headings=["Ch 1"], page_numbers=[1]),
            Chunk(text="Second chunk", headings=["Ch 2"], page_numbers=[2]),
            Chunk(text="Third chunk", headings=["Ch 3"], page_numbers=[3]),
        ]
    )

    chunks = adapter.chunk_document(result)

    assert len(chunks) == 3
    assert "First chunk" in chunks[0][0]
    assert "Second chunk" in chunks[1][0]
    assert "Third chunk" in chunks[2][0]
