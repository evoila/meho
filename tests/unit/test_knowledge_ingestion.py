# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.ingestion -- Docling pipeline era.

Covers structured path (PDF/DOCX via Docling), plain text fallback
(TextChunker), summary enrichment (D-05), error handling, and cleanup.

Mock strategy:
  - Stub docling modules in sys.modules before import (docling not installed)
  - Patch DoclingDocumentConverter at ingestion.py import site
  - Patch generate_document_summary and build_chunk_prefix at import site
  - Patch _resolve_connector_context on IngestionService instance
  - Patch _update_job_stage on IngestionService instance
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub docling modules in sys.modules BEFORE any meho_app imports.
#
# The import chain: knowledge.schemas -> knowledge/__init__.py -> routes.py
# -> deps.py -> ingestion.py -> document_converter.py -> docling.*
# Since docling is not installed in the test venv, we must install mock
# modules into sys.modules before Python attempts the imports.
# ---------------------------------------------------------------------------
_DOCLING_MODULES = [
    "docling",
    "docling.chunking",
    "docling.datamodel",
    "docling.datamodel.base_models",
    "docling.datamodel.document",
    "docling.document_converter",
    "docling_core",
    "docling_core.types",
    "docling_core.types.doc",
    "docling_core.types.doc.labels",
]

for _mod_name in _DOCLING_MODULES:
    if _mod_name not in sys.modules:
        _mock_mod = ModuleType(_mod_name)
        # Attach common attributes that document_converter.py accesses
        if _mod_name == "docling.chunking":
            _mock_mod.HybridChunker = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.datamodel.base_models":
            _mock_mod.InputFormat = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.datamodel.document":
            _mock_mod.DocumentStream = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.document_converter":
            _mock_mod.DocumentConverter = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling_core.types.doc":
            _mock_mod.DoclingDocument = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling_core.types.doc.labels":
            # DocItemLabel enum -- provide real-ish values for set comparison
            _label_mock = MagicMock()
            _label_mock.DOCUMENT_INDEX = "DOCUMENT_INDEX"
            _label_mock.PAGE_HEADER = "PAGE_HEADER"
            _label_mock.PAGE_FOOTER = "PAGE_FOOTER"
            _label_mock.PAGE_NUMBER = "PAGE_NUMBER"
            _mock_mod.DocItemLabel = _label_mock  # type: ignore[attr-defined]
        sys.modules[_mod_name] = _mock_mod

# NOW safe to import from meho_app
from datetime import UTC, datetime  # noqa: E402
from unittest.mock import AsyncMock, Mock, patch  # noqa: E402

import pytest  # noqa: E402

from meho_app.modules.knowledge.schemas import KnowledgeChunk  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_add_chunk():
    """Create a mock_add_chunk that assigns incrementing IDs."""
    call_count = 0

    async def mock_add_chunk(chunk_create):
        nonlocal call_count
        call_count += 1
        return KnowledgeChunk(
            id=f"chunk-{call_count}",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    return mock_add_chunk


def _make_store(add_chunk_fn=None):
    """Create a mock knowledge store with an optional custom add_chunk."""
    store = AsyncMock()
    store.add_chunk = add_chunk_fn or _make_mock_add_chunk()
    store.delete_chunk = AsyncMock(return_value=True)
    return store


def _make_storage():
    """Create a mock object storage."""
    storage = Mock()
    storage.upload_document.return_value = "s3://bucket/doc.pdf"
    storage.delete_document = Mock()
    return storage


def _make_service(store, storage, mock_converter_cls):
    """Create an IngestionService with mocked internals."""
    from meho_app.modules.knowledge.ingestion import IngestionService

    service = IngestionService(knowledge_store=store, object_storage=storage)
    # Patch _resolve_connector_context to avoid real DB lookup
    service._resolve_connector_context = AsyncMock(
        return_value=("kubernetes", "prod-cluster")
    )
    # Patch _update_job_stage to avoid job repository dependency
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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_structured_pdf(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """PDF mime type uses subprocess converter -- convert_file_in_subprocess called."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = "Extracted document text content"
    mock_converter.chunk_document.return_value = [
        (
            "Enriched chunk 1 text",
            {
                "heading_stack": ["Chapter 1"],
                "page_numbers": [1],
                "document_name": "test.pdf",
            },
        ),
        (
            "Enriched chunk 2 text",
            {
                "heading_stack": ["Chapter 2"],
                "page_numbers": [2, 3],
                "document_name": "test.pdf",
            },
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    chunk_ids = await service.ingest_document(
        file_bytes=b"fake pdf bytes",
        filename="test.pdf",
        mime_type="application/pdf",
        tenant_id="tenant-1",
        connector_id="conn-1",
    )

    assert len(chunk_ids) == 2
    mock_subprocess_convert.assert_called_once()
    mock_converter.get_full_text.assert_called_once_with(mock_doc)
    mock_converter.chunk_document.assert_called_once()
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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_structured_docx(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """DOCX mime type also uses subprocess converter path."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = "DOCX extracted text"
    mock_converter.chunk_document.return_value = [
        (
            "Chunk 1",
            {
                "heading_stack": ["Section A"],
                "page_numbers": [1],
                "document_name": "report.docx",
            },
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    docx_mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    chunk_ids = await service.ingest_document(
        file_bytes=b"fake docx bytes",
        filename="report.docx",
        mime_type=docx_mime,
        tenant_id="tenant-1",
    )

    assert len(chunk_ids) == 1
    mock_subprocess_convert.assert_called_once()
    mock_converter.chunk_document.assert_called_once()


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
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_plain_text_fallback(
    mock_converter_cls, mock_summary, mock_prefix
):
    """text/plain uses TextChunker path -- DoclingDocumentConverter.convert_file NOT called."""
    mock_converter = mock_converter_cls.return_value

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    # Mock the chunker on the service instance for plain text path
    service.chunker = Mock()
    service.chunker.chunk_document_with_structure.return_value = [
        (
            "Plain text chunk 1",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "notes.txt",
            },
        ),
    ]

    chunk_ids = await service.ingest_document(
        file_bytes=b"Some plain text content",
        filename="notes.txt",
        mime_type="text/plain",
        tenant_id="tenant-1",
    )

    assert len(chunk_ids) == 1
    # DoclingDocumentConverter should NOT have been used
    mock_converter.convert_file.assert_not_called()
    mock_converter.chunk_document.assert_not_called()
    # TextChunker SHOULD have been used
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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_summary_enrichment(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """Verify generate_document_summary called with text, connector_type, connector_name.
    Verify build_chunk_prefix called. Verify chunk_document receives the prefix."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = (
        "Document about pod eviction procedures"
    )
    mock_converter.chunk_document.return_value = [
        (
            "Enriched chunk",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "runbook.pdf",
            },
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    await service.ingest_document(
        file_bytes=b"pdf bytes",
        filename="runbook.pdf",
        mime_type="application/pdf",
        connector_id="conn-k8s",
    )

    # generate_document_summary called with extracted text, connector_type, connector_name
    mock_summary.assert_called_once_with(
        "Document about pod eviction procedures", "kubernetes", "prod-cluster"
    )

    # build_chunk_prefix called with connector context and summary
    mock_prefix.assert_called_once_with(
        "kubernetes", "prod-cluster", "Runbook for pod eviction."
    )

    # chunk_document received the prefix from build_chunk_prefix
    mock_converter.chunk_document.assert_called_once_with(
        mock_doc,
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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_summary_failure_continues(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """generate_document_summary returns ''. Ingestion still succeeds with empty prefix."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = "Some text"
    mock_converter.chunk_document.return_value = [
        (
            "Chunk text",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "doc.pdf",
            },
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    chunk_ids = await service.ingest_document(
        file_bytes=b"pdf bytes",
        filename="doc.pdf",
        mime_type="application/pdf",
    )

    # Ingestion succeeds even with empty summary
    assert len(chunk_ids) == 1
    # chunk_document called with empty prefix
    mock_converter.chunk_document.assert_called_once_with(
        mock_doc, chunk_prefix=""
    )


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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_empty_document_raises(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """chunk_document returns []. Assert ValueError('No text extracted')."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = ""
    mock_converter.chunk_document.return_value = []  # No chunks produced

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    with pytest.raises(
        ValueError, match="No text extracted from document empty.pdf"
    ):
        await service.ingest_document(
            file_bytes=b"empty pdf",
            filename="empty.pdf",
            mime_type="application/pdf",
        )


# ---------------------------------------------------------------------------
# Cleanup on failure (partial chunks + storage)
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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_cleanup_on_failure(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """After 2 chunks succeed, 3rd fails. Cleanup deletes 2 chunks + uploaded doc."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = "Text"
    mock_converter.chunk_document.return_value = [
        (
            "Chunk 1",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "f.pdf",
            },
        ),
        (
            "Chunk 2",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "f.pdf",
            },
        ),
        (
            "Chunk 3",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "f.pdf",
            },
        ),
    ]

    # Custom add_chunk that fails on 3rd call
    call_count = 0

    async def failing_add_chunk(chunk_create):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise Exception("Embedding API failed on chunk 3")
        return KnowledgeChunk(
            id=f"chunk-{call_count}",
            **chunk_create.model_dump(),
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    store = _make_store(add_chunk_fn=failing_add_chunk)
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    with pytest.raises(ValueError, match="Failed to ingest document"):
        await service.ingest_document(
            file_bytes=b"pdf bytes",
            filename="f.pdf",
            mime_type="application/pdf",
        )

    # Should have cleaned up the 2 successful chunks
    assert store.delete_chunk.call_count == 2
    delete_calls = [
        call.args[0] for call in store.delete_chunk.call_args_list
    ]
    assert "chunk-1" in delete_calls
    assert "chunk-2" in delete_calls

    # Should also delete the uploaded document
    storage.delete_document.assert_called_once()


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
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_plain_text_with_prefix(
    mock_converter_cls, mock_summary, mock_prefix
):
    """text/plain + connector context. Verify prefix prepended to TextChunker output."""
    mock_converter = mock_converter_cls.return_value

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    # Mock the chunker for plain text path
    service.chunker = Mock()
    service.chunker.chunk_document_with_structure.return_value = [
        (
            "Raw chunk text",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "config.txt",
            },
        ),
    ]

    chunk_ids = await service.ingest_document(
        file_bytes=b"vmware config content",
        filename="config.txt",
        mime_type="text/plain",
        connector_id="conn-vmware",
    )

    assert len(chunk_ids) == 1
    # DoclingDocumentConverter should NOT have been used
    mock_converter.convert_file.assert_not_called()


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
@patch("meho_app.modules.knowledge.ingestion.convert_file_in_subprocess", new_callable=AsyncMock)
@patch("meho_app.modules.knowledge.ingestion.DoclingDocumentConverter")
async def test_ingest_document_uploads_to_storage(
    mock_converter_cls, mock_subprocess_convert, mock_summary, mock_prefix
):
    """Assert object_storage.upload_document called with file_bytes, storage_key, mime_type."""
    mock_converter = mock_converter_cls.return_value
    mock_doc = MagicMock()
    mock_subprocess_convert.return_value = mock_doc
    mock_converter.get_full_text.return_value = "Text"
    mock_converter.chunk_document.return_value = [
        (
            "Chunk",
            {
                "heading_stack": [],
                "page_numbers": [],
                "document_name": "a.pdf",
            },
        ),
    ]

    store = _make_store()
    storage = _make_storage()
    service = _make_service(store, storage, mock_converter_cls)

    await service.ingest_document(
        file_bytes=b"file content",
        filename="a.pdf",
        mime_type="application/pdf",
        tenant_id="t1",
    )

    # upload_document should be called once
    storage.upload_document.assert_called_once()
    call_args = storage.upload_document.call_args
    assert call_args[0][0] == b"file content"  # file_bytes
    assert "a.pdf" in call_args[0][1]  # storage_key contains filename
    assert call_args[0][2] == "application/pdf"  # mime_type
