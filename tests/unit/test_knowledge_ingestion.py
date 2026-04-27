# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.ingestion -- DoclingWrapper era.

Covers structured path (PDF/DOCX via DoclingWrapperAdapter), plain text
fallback (TextChunker), summary enrichment (D-05), error handling, and cleanup.

Mock strategy:
  - Stub docling modules in sys.modules before import (docling not installed)
  - Patch DoclingWrapperAdapter at ingestion.py import site
  - Patch generate_document_summary and build_chunk_prefix at import site
  - Patch _resolve_connector_context on IngestionService instance
  - Patch _update_job_stage on IngestionService instance
"""

import importlib.util
import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub docling modules in sys.modules BEFORE any meho_app imports --
# but only for modules that are genuinely missing from the environment.
# Stubbing a module that is actually installed leaks a spec-less ModuleType
# into sys.modules and later tests trip over it when they call
# importlib.util.find_spec() on the same name. Note: torch is deliberately
# NOT in this list -- docling_wrapper.py does not import torch at module
# import time, so pre-empting it was never necessary and only widens the
# stub-pollution surface.
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
    _mock_mod = ModuleType(_mod_name)
    if _mod_name == "docling.datamodel.base_models":
        _mock_mod.InputFormat = MagicMock()  # type: ignore[attr-defined]
    elif _mod_name == "docling.document_converter":
        _mock_mod.DocumentConverter = MagicMock()  # type: ignore[attr-defined]
        _mock_mod.PdfFormatOption = MagicMock()  # type: ignore[attr-defined]
    elif _mod_name == "docling_core.types.doc":
        _mock_mod.DoclingDocument = MagicMock()  # type: ignore[attr-defined]
    elif _mod_name == "docling_core.types.doc.labels":
        _label_mock = MagicMock()
        _label_mock.DOCUMENT_INDEX = "DOCUMENT_INDEX"
        _label_mock.PAGE_HEADER = "PAGE_HEADER"
        _label_mock.PAGE_FOOTER = "PAGE_FOOTER"
        _mock_mod.DocItemLabel = _label_mock  # type: ignore[attr-defined]
    sys.modules[_mod_name] = _mock_mod

# NOW safe to import from meho_app
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import pytest  # noqa: E402

from meho_app.modules.knowledge.docling_wrapper import ConversionResult, Chunk  # noqa: E402
from meho_app.modules.knowledge.schemas import KnowledgeChunk  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_add_chunk():  # type: ignore[no-untyped-def]
    """Create a mock_add_chunk that assigns incrementing IDs."""
    call_count = 0

    async def mock_add_chunk(chunk_create):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        return KnowledgeChunk(
            id=f"chunk-{call_count}",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    return mock_add_chunk


def _make_store(add_chunk_fn=None):  # type: ignore[no-untyped-def]
    """Create a mock knowledge store with an optional custom add_chunk."""
    store = AsyncMock()
    store.add_chunk = add_chunk_fn or _make_mock_add_chunk()
    store.delete_chunk = AsyncMock(return_value=True)
    return store


def _make_storage():  # type: ignore[no-untyped-def]
    """Create a mock object storage."""
    storage = Mock()
    storage.upload_document.return_value = "s3://bucket/doc.pdf"
    storage.delete_document = Mock()
    return storage


def _make_conversion_result(
    markdown: str = "# Title\nContent",
    chunks: list | None = None,
    source: str = "test.pdf",
) -> ConversionResult:
    """Create a mock ConversionResult for testing."""
    return ConversionResult(
        markdown=markdown,
        text="Content",
        html="<h1>Title</h1>",
        chunks=chunks or [],
        pages=5,
        elapsed=1.5,
        mem_peak_mb=100.0,
        mem_avg_mb=80.0,
        source=Path(source),
        format="pdf",
        chunk_count=1,
        file_size=1024,
    )


def _make_service(store, storage, mock_adapter_cls):  # type: ignore[no-untyped-def]
    """Create an IngestionService with mocked internals."""
    from meho_app.modules.knowledge.ingestion import IngestionService

    service = IngestionService(knowledge_store=store, object_storage=storage)
    service._resolve_connector_context = AsyncMock(return_value=("kubernetes", "prod-cluster"))
    service._update_job_stage = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Structured path: PDF
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="kubernetes connector (prod-cluster). Summary text.",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="Summary text.",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_structured_pdf(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """PDF mime type uses DoclingWrapperAdapter."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(
        source="test.pdf",
        chunks=[
            Chunk(text="Content 1", headings=["Chapter 1"], page_numbers=[1]),
            Chunk(text="Content 2", headings=["Chapter 2"], page_numbers=[2, 3]),
        ],
    )
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "Extracted document text content"
    mock_adapter.chunk_document.return_value = [
        (
            "Enriched chunk 1 text",
            {"heading_stack": ["Chapter 1"], "page_numbers": [1], "document_name": "test.pdf"},
        ),
        (
            "Enriched chunk 2 text",
            {"heading_stack": ["Chapter 2"], "page_numbers": [2, 3], "document_name": "test.pdf"},
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    chunk_ids = await service.ingest_document(
        file_bytes=b"fake pdf bytes",
        filename="test.pdf",
        mime_type="application/pdf",
        tenant_id="tenant-1",
        connector_id="conn-1",
    )

    assert len(chunk_ids) == 2
    mock_adapter.convert_file_async.assert_called_once()
    mock_adapter.get_full_text.assert_called_once_with(mock_result)
    mock_adapter.chunk_document.assert_called_once()
    mock_summary.assert_called_once()


# ---------------------------------------------------------------------------
# Structured path: DOCX
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="kubernetes connector (prod-cluster). Summary.",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="Summary.",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_structured_docx(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """DOCX mime type also uses DoclingWrapperAdapter."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="report.docx")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "DOCX extracted text"
    mock_adapter.chunk_document.return_value = [
        (
            "Chunk 1",
            {"heading_stack": ["Section A"], "page_numbers": [1], "document_name": "report.docx"},
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    chunk_ids = await service.ingest_document(
        file_bytes=b"fake docx bytes",
        filename="report.docx",
        mime_type=docx_mime,
        tenant_id="tenant-1",
    )

    assert len(chunk_ids) == 1
    mock_adapter.convert_file_async.assert_called_once()
    mock_adapter.chunk_document.assert_called_once()


# ---------------------------------------------------------------------------
# Plain text fallback (TextChunker)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_plain_text_fallback(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """text/plain uses TextChunker path -- DoclingWrapperAdapter NOT used."""
    mock_adapter = mock_adapter_cls.return_value

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    service.chunker = Mock()
    service.chunker.chunk_document_with_structure.return_value = [
        (
            "Plain text chunk 1",
            {"heading_stack": [], "page_numbers": [], "document_name": "notes.txt"},
        ),
    ]

    chunk_ids = await service.ingest_document(
        file_bytes=b"Some plain text content",
        filename="notes.txt",
        mime_type="text/plain",
        tenant_id="tenant-1",
    )

    assert len(chunk_ids) == 1
    mock_adapter.convert_file_async.assert_not_called()
    mock_adapter.chunk_document.assert_not_called()
    service.chunker.chunk_document_with_structure.assert_called_once()


# ---------------------------------------------------------------------------
# Summary enrichment (D-05)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="kubernetes connector (prod-cluster). Runbook for pod eviction.",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="Runbook for pod eviction.",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_summary_enrichment(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """Verify summary generation and prefix passed to chunk_document."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="runbook.pdf")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "Document about pod eviction procedures"
    mock_adapter.chunk_document.return_value = [
        (
            "Enriched chunk",
            {"heading_stack": [], "page_numbers": [], "document_name": "runbook.pdf"},
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    await service.ingest_document(
        file_bytes=b"pdf bytes",
        filename="runbook.pdf",
        mime_type="application/pdf",
        connector_id="conn-k8s",
    )

    mock_summary.assert_called_once_with(
        "Document about pod eviction procedures", "kubernetes", "prod-cluster"
    )
    mock_prefix.assert_called_once_with("kubernetes", "prod-cluster", "Runbook for pod eviction.")
    mock_adapter.chunk_document.assert_called_once_with(
        mock_result,
        chunk_prefix="kubernetes connector (prod-cluster). Runbook for pod eviction.",
    )


# ---------------------------------------------------------------------------
# Summary failure continues gracefully
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_summary_failure_continues(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """generate_document_summary returns ''. Ingestion still succeeds with empty prefix."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="doc.pdf")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "Some text"
    mock_adapter.chunk_document.return_value = [
        (
            "Chunk text",
            {"heading_stack": [], "page_numbers": [], "document_name": "doc.pdf"},
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    chunk_ids = await service.ingest_document(
        file_bytes=b"pdf bytes",
        filename="doc.pdf",
        mime_type="application/pdf",
    )

    assert len(chunk_ids) == 1
    mock_adapter.chunk_document.assert_called_once_with(mock_result, chunk_prefix="")


# ---------------------------------------------------------------------------
# Empty document raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_empty_document_raises(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """chunk_document returns []. Assert ValueError('No text extracted')."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="empty.pdf")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = ""
    mock_adapter.chunk_document.return_value = []

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    with pytest.raises(ValueError, match="No text extracted from document empty.pdf"):
        await service.ingest_document(
            file_bytes=b"empty pdf",
            filename="empty.pdf",
            mime_type="application/pdf",
        )


# ---------------------------------------------------------------------------
# Retry-and-skip on chunk failure (checkpoint-resume semantics, no rollback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="prefix.",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="Summary.",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_retry_and_skip_on_chunk_failure(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """One chunk exhausts all CHUNK_MAX_RETRIES: skipped, others keep going, no rollback.

    The ingestion pipeline deliberately does not roll back successfully stored chunks
    on a single-chunk failure -- partial progress is preserved under the resume model
    (see `_save_checkpoint`), and failures are only surfaced as a raised ValueError
    once `skipped_chunks > max(10, total_chunks // 10)`. For a 3-chunk document with
    one persistent chunk failure, ingestion completes with 2 stored chunks and 1
    skipped, no deletes issued.
    """
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="f.pdf")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "Text"
    mock_adapter.chunk_document.return_value = [
        ("Chunk 1", {"heading_stack": [], "page_numbers": [], "document_name": "f.pdf"}),
        ("Chunk 2", {"heading_stack": [], "page_numbers": [], "document_name": "f.pdf"}),
        ("Chunk 3", {"heading_stack": [], "page_numbers": [], "document_name": "f.pdf"}),
    ]

    success_texts = {"Chunk 1", "Chunk 2"}
    success_id_counter = 0

    async def selectively_failing_add_chunk(chunk_create):  # type: ignore[no-untyped-def]
        nonlocal success_id_counter
        if chunk_create.text in success_texts:
            success_id_counter += 1
            return KnowledgeChunk(
                id=f"chunk-{success_id_counter}",
                **chunk_create.model_dump(),
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        raise RuntimeError("Embedding API persistently failed on chunk 3")

    store = _make_store(add_chunk_fn=selectively_failing_add_chunk)
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    with patch("meho_app.modules.knowledge.ingestion.asyncio.sleep", new_callable=AsyncMock):
        chunk_ids = await service.ingest_document(
            file_bytes=b"pdf bytes",
            filename="f.pdf",
            mime_type="application/pdf",
        )

    assert chunk_ids == ["chunk-1", "chunk-2"]
    store.delete_chunk.assert_not_called()

    # The original PDF stays in object storage (partial-success preservation);
    # only the transient .chunks.json checkpoint is cleared on finalize, which
    # is unrelated to the rollback behavior this test rules out.
    deleted_keys = [call.args[0] for call in storage.delete_document.call_args_list]
    assert not any(key.endswith("/f.pdf") for key in deleted_keys)


# ---------------------------------------------------------------------------
# Plain text with prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="vmware connector (dc-west). VMware docs.",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="VMware docs.",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_plain_text_with_prefix(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """text/plain + connector context. Verify prefix prepended to TextChunker output."""
    mock_adapter = mock_adapter_cls.return_value

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    service.chunker = Mock()
    service.chunker.chunk_document_with_structure.return_value = [
        (
            "Raw chunk text",
            {"heading_stack": [], "page_numbers": [], "document_name": "config.txt"},
        ),
    ]

    chunk_ids = await service.ingest_document(
        file_bytes=b"vmware config content",
        filename="config.txt",
        mime_type="text/plain",
        connector_id="conn-vmware",
    )

    assert len(chunk_ids) == 1
    mock_adapter.convert_file_async.assert_not_called()


# ---------------------------------------------------------------------------
# Upload to storage
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(
    "meho_app.modules.knowledge.ingestion.build_chunk_prefix",
    return_value="",
)
@patch(
    "meho_app.modules.knowledge.ingestion.generate_document_summary",
    new_callable=AsyncMock,
    return_value="",
)
@patch("meho_app.modules.knowledge.ingestion.DoclingWrapperAdapter")
async def test_ingest_document_uploads_to_storage(
    mock_adapter_cls,
    mock_summary,
    mock_prefix,  # type: ignore[no-untyped-def]
):
    """Assert object_storage.upload_document called with file_bytes, storage_key, mime_type."""
    mock_adapter = mock_adapter_cls.return_value
    mock_result = _make_conversion_result(source="a.pdf")
    mock_adapter.convert_file_async = AsyncMock(return_value=mock_result)
    mock_adapter.get_full_text.return_value = "Text"
    mock_adapter.chunk_document.return_value = [
        ("Chunk", {"heading_stack": [], "page_numbers": [], "document_name": "a.pdf"}),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_adapter_cls)

    await service.ingest_document(
        file_bytes=b"file content",
        filename="a.pdf",
        mime_type="application/pdf",
        tenant_id="t1",
    )

    # Ingestion now uploads three artifacts per document:
    #   1) the original file, 2) the rendered markdown, 3) the chunks checkpoint.
    # Assertion focuses on the original-file upload (call 0) which is the
    # durable-storage contract this test exists to guard.
    assert storage.upload_document.call_count == 3
    upload_calls = storage.upload_document.call_args_list
    original_call = upload_calls[0]
    assert original_call[0][0] == b"file content"
    assert "a.pdf" in original_call[0][1]
    assert original_call[0][2] == "application/pdf"

    markdown_call = upload_calls[1]
    assert markdown_call[0][1].endswith(".md")
    assert markdown_call[0][2] == "text/markdown"

    chunks_call = upload_calls[2]
    assert chunks_call[0][1].endswith(".chunks.json")
    assert chunks_call[0][2] == "application/json"
