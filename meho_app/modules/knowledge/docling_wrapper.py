# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
#!/usr/bin/env python3
"""DoclingWrapper -- Production-hardened wrapper for IBM Docling document conversion.

This module provides a single-class interface to IBM Docling that addresses
reliability and memory-related issues observed when processing large PDFs at
scale.  It is designed to be dropped into any project as a standalone file.

WHY THIS EXISTS
---------------
IBM Docling (https://github.com/DS4SD/docling) is a powerful document
conversion library, but its PDF processing pipeline has structural
limitations that surface in production workloads:

1. NATIVE MEMORY GROWTH UNDER SUSTAINED USE:
   ``docling-parse`` (v5.8.0) is a C++ library exposed to Python via a
   compiled pybind11 extension (``pdf_parsers.cpython-*.so``).  It performs
   PDF text extraction, layout segmentation, and page decoding in native
   code.  While Docling's pipeline does call ``unload()`` on page backends
   after processing, and pybind11 will invoke C++ destructors when Python
   objects are garbage-collected, in practice RSS climbs steadily when
   converting multiple PDFs (or very large PDFs) in the same process and
   does not return to baseline between conversions.

   Benchmark evidence (M2 Max, 36 GB RAM, docling 2.85.0): converting 5
   PDFs (137 pages total) in a single process grew RSS from 454 MB to
   1,532 MB (+1,078 MB).  ``gc.collect()`` recovered near-zero after
   each conversion.  Repeating the same 6-page PDF 10 times showed ~3 MB
   growth per iteration.  With subprocess isolation, the parent RSS
   stayed flat at ~2,838 MB across all conversions.

   We have not traced the exact root cause inside ``docling-parse``'s
   C++ code, but the observed behavior is consistent with one or more of:
   - Internal C++ state (caches, arena allocators, or retained document
     structures) inside ``pdf_parser`` that persists across individual
     ``unload_document()`` / ``unload_pages()`` calls.
   - The ``StandardPdfPipeline`` uses multi-threaded stages that can
     abandon worker threads after a 15-second timeout if they are stuck in
     blocking native calls (model inference, PDF backend ``load_page``).
     Abandoned threads continue holding native resources indefinitely
     (the pipeline itself logs "resources may leak" in this case).
   - Python's ``gc`` cannot reach native heap allocations; even if Python
     wrappers are collected, fragmented native memory may not be returned
     to the OS by the C runtime's ``malloc`` implementation.

   Regardless of which factor dominates, the practical effect is the same:
   processing many PDFs in a single Python process leads to steadily
   growing RSS that is not recovered by Python-side cleanup.

2. UNBOUNDED RSS ON LARGE DOCUMENTS:
   A single large PDF (300+ pages) can push a process to multi-GB RSS.
   In our experience this has led to segfaults and OOM kills, though the
   exact threshold depends on the document.  ``docling-parse`` loads the full PDF
   byte stream into native memory at ``parser.load()`` time.  Although
   page *decoding* is lazy (pages are parsed on first access and cached
   in a Python dict), the underlying native ``pdf_parser`` object retains
   the complete document structure.  Layout analysis models (loaded via
   ``torch``) add further per-page GPU/CPU memory.  For very large
   documents, the cumulative native state of parsed pages, model
   activations, and the PDF structure itself can exceed physical memory.

   Benchmark evidence: converting subsets of an 8,284-page PDF in a
   single process showed increasing memory growth -- 50 pages = +217 MB,
   100 = +318 MB, 200 = +477 MB, 400 = +1,402 MB.  Note these runs
   were sequential in the same process, so later runs include residual
   state from earlier ones.  Extrapolating from this trend, the full
   document would likely exceed typical container memory limits.

   Splitting a large PDF into smaller chunks with pikepdf and processing
   each in a separate subprocess bounds the peak RSS per process.  The
   benchmark confirmed: 50-page chunks cap subprocess peak at ~1.5 GB,
   100-page at ~1.8 GB, 200-page at ~2.2 GB.

   IMPORTANT NOTE ON SPLIT PDF FILE SIZES: A naive PDF split can produce
   chunk files almost as large as the original.  This is a well-known
   property of the PDF format, not a bug in the splitter.  PDFs commonly
   store fonts, images, and other resources in a single shared
   ``/Resources`` dictionary that all pages point to.  When you copy 25
   pages into a new PDF, the new file must include every resource that
   those pages' content streams reference -- but if the shared dictionary
   maps the *same font names* (``/F1``, ``/F2``, ...) across all pages,
   then ``remove_unreferenced_resources()`` sees those names *are* used
   in the chunk's content streams and keeps them.  The result: a 25-page
   chunk from a 140 MB, 8000-page PDF can easily be 80-90 MB because it
   carries the same set of embedded font programs.

   This does NOT mean the chunk is as *expensive to process* as the
   original.  The memory problem is not about file size on disk -- it is
   about the **cumulative native state** that builds up inside a single
   process as pages are decoded sequentially.  Each page adds to the C++
   parser's heap, layout model activations accumulate, and parsed pages
   are cached in Python.  None of this is released until the process
   exits.  A 25-page chunk may be 80 MB on disk but it only causes
   ``docling-parse`` to build state for 25 pages, not 8000.  By
   processing each chunk in its own subprocess, no single process ever
   accumulates state for more than N pages, and the OS reclaims
   everything when the subprocess exits.

   Note: an earlier attempt using PyMuPDF (``fitz``) for splitting
   produced even larger chunk files -- ``fitz`` has no equivalent of
   ``remove_unreferenced_resources()`` and copies more of the source
   document's object tree.  More importantly, splitting alone (without
   subprocess isolation) does not solve the problem: feeding pre-split
   files to a single long-lived process still accumulates native state
   across conversions.  The subprocess-per-chunk design (W1) is what
   actually bounds memory; the splitting (W2) just controls how many
   pages each subprocess sees.

3. NO PROGRESS REPORTING:
   Stock Docling (as of 2.85.0) has no ``progress_callback`` or equivalent
   mechanism.  There is no way to know, from outside the conversion call,
   which page is currently being processed.  PR #3042
   (https://github.com/docling-project/docling/pull/3042) adds a
   ``progress_callback`` parameter but has not been merged as of this
   writing.

   When conversion runs in a subprocess (to fix problems 1 and 2), the
   lack of progress reporting becomes even worse -- the parent process has
   no visibility at all into what the child is doing.

   This wrapper solves it with a runtime monkey patch (W5) applied inside
   each subprocess.  The patch adds ``progress_callback`` support to
   ``DocumentConverter``, replicating the essential behavior from PR #3042.
   A polling thread inside ``BasePipeline.execute`` monitors page
   completion and fires lightweight events.  The subprocess writes these
   to a progress file (W4), which the parent polls to drive its own
   progress UI.

This wrapper addresses all three by running each PDF conversion (or page-
range chunk) in an isolated subprocess, splitting large PDFs with pikepdf,
monkey-patching stock Docling for progress reporting, bridging progress
across the process boundary via filesystem polling, and adding production
guardrails around worker hangs and memory measurement.

WORKAROUNDS
-----------
Eight core architectural workarounds are embedded in this module:

W1  Subprocess isolation      -- each PDF chunk in a fresh process; when the
                                 process exits, the OS reclaims ALL native
                                 memory regardless of C++ cleanup behavior
W2  PDF page-splitting        -- pikepdf splits large PDFs into N-page chunks
W3  Bounded queue             -- at most ``max_workers + 1`` chunk files on disk
W4  Progress file polling     -- child writes an atomic progress snapshot, parent polls
W5  Monkey patch for progress -- patches DocumentConverter + BasePipeline at
                                 runtime to support progress_callback (from
                                 unmerged PR #3042); uses page-count polling
                                 inside _build_document rather than copying
                                 pipeline internals, for resilience across
                                 minor Docling version changes
W6  RSS monitoring            -- parent reads child RSS via psutil; worker samples its own peak RSS
W7  remove_unreferenced_resources -- strips unused font/image refs from split chunks
W8  Eager temp cleanup        -- shutil.rmtree per chunk immediately after use

W1-W4 and W6-W8 are structural patterns that work around Docling from the
outside.  W5 is the only runtime monkey patch; it is applied once per
subprocess and does not modify files on disk.  Beyond those core
workarounds, the wrapper also enforces worker timeout/stall guardrails and
propagates splitter/worker failures without hanging the parent process.

VERSIONING
----------
Built and tested against these exact package versions::

    docling            2.85.0
    docling-core       2.72.0
    docling-ibm-models 3.13.0
    docling-parse      5.8.0
    torch              2.11.0
    pypdfium2          5.6.0

These versions are pinned because the workarounds in this wrapper are
tailored to specific observed behaviors in ``docling-parse`` and the
threading model of ``StandardPdfPipeline`` as of docling 2.85.0.  Different
versions may change memory characteristics or pipeline behavior.  The
wrapper will likely still work with minor version differences, but the
memory numbers documented here may not hold.  A runtime warning is emitted
if the installed ``docling`` version does not match.

USAGE
-----
Minimal example (2-3 lines)::

    from docling_wrapper import DoclingWrapper

    converter = DoclingWrapper(device="mps")
    result = converter.parse("report.pdf")
    print(result.markdown)

With structural chunking for RAG (tokenization is left to your embedding
model or API)::

    converter = DoclingWrapper(device="mps", chunking=True)
    result = converter.parse("report.pdf")
    for chunk in result.chunks:
        # chunk.text, chunk.headings, chunk.page_numbers
        ingest_into_vector_db(chunk.text, metadata=chunk.headings)
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pypdfium2 as pdfium

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------

_PINNED_DOCLING_VERSION = "2.85.0"
_CONFIG_ENV_KEY = "_DOCLING_CONVERT_CFG"


def _check_version() -> None:
    """Emit a warning if the installed docling version differs from the
    pinned version this wrapper was built against.
    """
    try:
        installed = importlib.metadata.version("docling")
    except importlib.metadata.PackageNotFoundError:
        warnings.warn(
            "docling is not installed. DoclingWrapper requires docling=="
            f"{_PINNED_DOCLING_VERSION}.",
            stacklevel=3,
        )
        return
    if installed != _PINNED_DOCLING_VERSION:
        warnings.warn(
            f"docling {installed} is installed, but DoclingWrapper was built "
            f"and tested against docling=={_PINNED_DOCLING_VERSION}. The "
            f"subprocess-isolation workarounds target specific behaviors in "
            f"docling-parse's C internals and StandardPdfPipeline's threading "
            f"model. Mismatched versions may cause incorrect memory handling, "
            f"callback shape changes, or pipeline regressions that this "
            f"wrapper cannot repair. Pin to docling=={_PINNED_DOCLING_VERSION} "
            f"for guaranteed behavior.",
            stacklevel=3,
        )


_check_version()


# ---------------------------------------------------------------------------
# Monkey patch: progress_callback for stock docling  (W5)
# ---------------------------------------------------------------------------
# Stock docling (as of 2.85.0) has no progress_callback support.  PR #3042
# (https://github.com/docling-project/docling/pull/3042) adds it but has not
# been merged.  This patch replicates the essential behavior at runtime:
# per-page progress events emitted during PDF conversion, delivered to a
# caller-supplied callback.
#
# The patch is applied once per subprocess by _subprocess_worker() before
# any Docling objects are created.  It touches three things:
#
#   1. DocumentConverter.__init__  -- accept & store ``progress_callback``
#   2. DocumentConverter._execute_pipeline -- forward it to pipeline.execute()
#   3. BasePipeline.execute -- run _build_document in a background thread,
#      poll ``conv_res.pages`` growth at 100 ms intervals, and fire the
#      callback with a lightweight _PageProgressEvent for each new page.
#
# Approach (3) uses page-count polling rather than patching the deep internals
# of StandardPdfPipeline._build_document or PaginatedPipeline._build_document.
# This makes the patch resilient to changes in pipeline internals across minor
# Docling versions -- it only depends on conv_res.pages being populated as
# pages complete, which is a stable external contract.
#
# NOTE: For StandardPdfPipeline (the threaded path), conv_res.pages is
# populated with placeholder Page objects *before* processing starts.
# Actual completion is tracked differently (proc.pages inside
# _build_document).  To handle this, the polling thread monitors
# pages that have been fully processed by checking page.size is not None
# (pages start as placeholders with size=None and get their size set
# during the initialize_page stage).

_PATCH_APPLIED = False


class _PageProgressEvent:
    """Minimal progress event for the monkey patch.  Matches the interface
    our _on_page callback in _subprocess_worker expects (page_no, total_pages).
    """

    __slots__ = ("page_no", "total_pages")

    def __init__(self, page_no: int, total_pages: int):
        self.page_no = page_no
        self.total_pages = total_pages


@dataclass
class _ProgressSnapshot:
    page: int = 0
    total_pages: int = 0
    updated_at: float = 0.0


def _write_progress_snapshot(path: Path, page: int, total_pages: int) -> None:
    """Atomically replace the child progress snapshot on disk."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "page": max(page, 0),
                    "total_pages": max(total_pages, 0),
                    "updated_at": time.time(),
                },
                fh,
            )
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _read_progress_snapshot(path: Path) -> _ProgressSnapshot:
    """Read a child progress snapshot, supporting the legacy ``page/total`` format."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return _ProgressSnapshot()

    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            return _ProgressSnapshot()
        return _ProgressSnapshot(
            page=int(data.get("page", 0) or 0),
            total_pages=int(data.get("total_pages", 0) or 0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )

    if "/" not in raw:
        return _ProgressSnapshot()

    page_s, total_s = raw.split("/", 1)
    return _ProgressSnapshot(page=int(page_s), total_pages=int(total_s))


def _apply_progress_patch() -> None:
    """Monkey-patch stock Docling to support ``progress_callback``.

    Must be called exactly once, in the subprocess worker, before creating
    any ``DocumentConverter`` instance.  Safe to call multiple times (no-op
    after the first).
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return
    _PATCH_APPLIED = True

    # If a future Docling version merges PR #3042, DocumentConverter will
    # natively accept progress_callback.  Detect this and skip patching.
    import inspect

    from docling.document_converter import DocumentConverter
    from docling.pipeline.base_pipeline import BasePipeline

    sig = inspect.signature(DocumentConverter.__init__)
    if "progress_callback" in sig.parameters:
        logger.info("Docling natively supports progress_callback; skipping W5 patch.")
        return

    # --- 1. Patch DocumentConverter.__init__ ---
    _orig_dc_init = DocumentConverter.__init__

    def _patched_dc_init(self: Any, *args: Any, **kwargs: Any) -> None:
        cb = kwargs.pop("progress_callback", None)
        _orig_dc_init(self, *args, **kwargs)
        self._progress_callback = cb

    DocumentConverter.__init__ = _patched_dc_init  # type: ignore[method-assign]  # runtime monkey-patch for docling<=2.84 (progress_callback)

    # --- 2. Patch DocumentConverter._execute_pipeline ---
    _orig_exec = DocumentConverter._execute_pipeline

    def _patched_exec(self: Any, in_doc: Any, raises_on_error: bool) -> Any:
        cb = getattr(self, "_progress_callback", None)
        if cb is not None and hasattr(in_doc, "format"):
            pipeline = self._get_pipeline(in_doc.format)
            if pipeline is not None:
                pipeline._progress_callback = cb
        return _orig_exec(self, in_doc, raises_on_error)

    DocumentConverter._execute_pipeline = _patched_exec  # type: ignore[method-assign]  # runtime monkey-patch

    # --- 3. Patch BasePipeline.execute ---
    # The original execute() creates a ConversionResult internally, calls
    # _build_document(conv_res), then _assemble_document, _enrich_document.
    # We wrap _build_document on the *instance* (not the class) so we can
    # capture conv_res and poll it for page completion while it runs.
    _orig_execute = BasePipeline.execute

    def _patched_pipeline_execute(
        self: Any, in_doc: Any, raises_on_error: bool, **kwargs: Any
    ) -> Any:
        cb = getattr(self, "_progress_callback", None)
        if cb is None:
            return _orig_execute(self, in_doc, raises_on_error)

        total_pages = getattr(in_doc, "page_count", 0) or 0
        if total_pages <= 0:
            return _orig_execute(self, in_doc, raises_on_error)

        orig_build = self._build_document

        def _build_with_polling(conv_res: Any) -> Any:
            completed = [0]
            stop_event = threading.Event()
            callback_failed = False

            def _emit(event: _PageProgressEvent) -> None:
                nonlocal callback_failed
                if callback_failed:
                    return
                try:
                    cb(event)
                except Exception:
                    callback_failed = True
                    logger.exception(
                        "Docling progress callback failed inside patched pipeline; "
                        "disabling further progress events for this conversion."
                    )

            def _poll() -> None:
                while not stop_event.is_set():
                    n = sum(1 for p in conv_res.pages if getattr(p, "size", None) is not None)
                    if n > completed[0]:
                        completed[0] = n
                        _emit(
                            _PageProgressEvent(
                                page_no=completed[0],
                                total_pages=total_pages,
                            )
                        )
                    stop_event.wait(0.1)

            poller = threading.Thread(target=_poll, daemon=True)
            poller.start()
            try:
                result = orig_build(conv_res)
            finally:
                stop_event.set()
                poller.join(timeout=2.0)

            if completed[0] < total_pages and total_pages > 0:
                _emit(
                    _PageProgressEvent(
                        page_no=total_pages,
                        total_pages=total_pages,
                    )
                )

            return result

        self._build_document = _build_with_polling
        try:
            return _orig_execute(self, in_doc, raises_on_error)
        finally:
            self._build_document = orig_build

    BasePipeline.execute = _patched_pipeline_execute  # type: ignore[method-assign]  # runtime monkey-patch


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".md": "md",
    ".markdown": "md",
    ".adoc": "asciidoc",
    ".asciidoc": "asciidoc",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".tif": "image",
    ".bmp": "image",
    ".gif": "image",
    ".webp": "image",
}


def _resolve_device(device: str) -> str:
    """Resolve ``'auto'`` to the best available accelerator.

    Args:
        device: One of ``'auto'``, ``'mps'``, ``'cuda'``, or ``'cpu'``.

    Returns:
        A concrete device string suitable for ``AcceleratorOptions.device``.
    """
    if device != "auto":
        return device
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception as exc:  # noqa: BLE001 - torch may be missing or probe may fail
        logger.debug("torch_device_probe_failed", exc_info=exc)
    return "cpu"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """A single RAG-ready text chunk produced by Docling's chunking pipeline.

    Chunks are split on document structure (headings, sections, tables) using
    ``HierarchicalChunker``.  No tokenization is performed here -- that is
    left to the downstream embedding model or API (OpenAI, Cohere, etc.).

    Attributes:
        text: The chunk's textual content.
        headings: Document headings that scope this chunk (outermost first).
        page_numbers: Original page numbers this chunk spans.
    """

    text: str = ""
    headings: list[str] = field(default_factory=list)
    page_numbers: list[int] = field(default_factory=list)


@dataclass
class ConversionResult:
    """Result of converting a single document.

    Attributes:
        markdown: Full document exported as Markdown.
        text: Full document exported as plain text.
        html: Full document exported as HTML.
        chunks: RAG chunks (empty list when chunker is ``None``).
        pages: Total page count of the source document.
        elapsed: Wall-clock seconds for conversion.
        mem_peak_mb: Peak RSS (in MB) of the conversion subprocess(es).
        mem_avg_mb: Average memory delta (in MB) per chunk subprocess.
        source: Path to the original source file.
        format: Detected format string (e.g. ``'pdf'``, ``'docx'``).
        chunk_count: Number of PDF page-split chunks used (1 if no splitting).
        file_size: Source file size in bytes.
    """

    markdown: str = ""
    text: str = ""
    html: str = ""
    chunks: list[Chunk] = field(default_factory=list)  # empty when chunking=False
    pages: int = 0
    elapsed: float = 0.0
    mem_peak_mb: float = 0.0
    mem_avg_mb: float = 0.0
    source: Path = field(default_factory=lambda: Path("."))
    format: str = ""
    chunk_count: int = 1
    file_size: int = 0


@dataclass
class ProgressEvent:
    """Payload delivered to the ``on_progress`` callback.

    Not all fields are populated for every event.  For PDF page-splitting,
    ``chunk_index`` / ``total_chunks`` track which chunk subprocess is
    active, while ``page`` / ``total_pages`` track individual page progress
    within that chunk.  ``rss_bytes`` is the child process's current RSS
    as observed by the parent via psutil.

    Attributes:
        page: Current page number (1-based) within the active chunk.
        total_pages: Total pages in the active chunk.
        chunk_index: Index (0-based) of the current page-split chunk.
        total_chunks: Total number of page-split chunks for this PDF.
        rss_bytes: Current RSS of the worker subprocess in bytes.
        source: Path to the file being converted.
    """

    page: int = 0
    total_pages: int = 0
    chunk_index: int = 0
    total_chunks: int = 1
    rss_bytes: int = 0
    source: Path = field(default_factory=lambda: Path("."))


# ---------------------------------------------------------------------------
# Internal dataclass (subprocess results)
# ---------------------------------------------------------------------------


@dataclass
class _ChunkResult:
    pages: int = 0
    elapsed: float = 0.0
    md_chars: int = 0
    mem_used: int = 0
    peak_rss: int = 0
    markdown: str = ""
    chunks: list[Chunk] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DoclingWrapper
# ---------------------------------------------------------------------------


class DoclingWrapper:
    """Production wrapper around IBM Docling for document-to-Markdown conversion.

    Handles PDF memory isolation via subprocesses, large-PDF page-splitting,
    automatic format detection, optional RAG chunking, and device selection.

    Args:
        ocr: Enable OCR for scanned pages.  Default ``False``.
        table_structure: Enable table-structure recognition.  Default ``False``.
        formula_enrichment: Enable formula extraction.  Default ``False``.
        code_enrichment: Enable code-block extraction.  Default ``False``.
        images_scale: Scale factor for page images.  Default ``1.0``.
        device: Accelerator device.  ``'auto'`` probes MPS then CUDA then
            CPU.  Also accepts ``'mps'``, ``'cuda'``, ``'cpu'`` directly.
        num_threads: Thread count for Docling's ``AcceleratorOptions``.
        pdf_chunk_pages: Maximum pages per subprocess when splitting large
            PDFs.  Set to ``0`` to disable splitting (the entire PDF will be
            processed in a single subprocess).
        max_workers: Number of parallel subprocess workers for PDF page-
            splitting.  Only relevant when ``pdf_chunk_pages > 0`` and the
            PDF exceeds that limit.
        progress_poll_interval: Parent poll interval, in seconds, for child
            progress and RSS snapshots.
        worker_timeout_s: Hard wall-clock timeout for a single worker
            subprocess.  ``None`` disables this guardrail.
        worker_idle_timeout_s: Maximum idle time, in seconds, before a worker
            subprocess is treated as stalled.  ``None`` disables this guardrail.
        chunking: Enable structural document chunking for RAG.  Uses
            Docling's ``HierarchicalChunker`` which splits on headings,
            sections, and tables.  No tokenization is performed -- that is
            left to the downstream embedding model or API.  Default ``False``.
        output_dir: If set, write Markdown output files to this directory.
        on_progress: Optional callback invoked with :class:`ProgressEvent`
            during conversion.  Has no Rich dependency -- wire any UI you like.

    Example::

        converter = DoclingWrapper(device="mps", chunking=True)
        result = converter.parse("path/to/file.pdf")
        print(result.markdown)
        for chunk in result.chunks:
            print(chunk.headings, chunk.text[:80])
    """

    def __init__(
        self,
        *,
        ocr: bool = False,
        table_structure: bool = False,
        formula_enrichment: bool = False,
        code_enrichment: bool = False,
        images_scale: float = 1.0,
        device: str = "auto",
        num_threads: int = 12,
        pdf_chunk_pages: int = 100,
        max_workers: int = 4,
        progress_poll_interval: float = 0.2,
        worker_timeout_s: float | None = 3600.0,
        worker_idle_timeout_s: float | None = 300.0,
        chunking: bool = False,
        output_dir: Path | str | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        if progress_poll_interval <= 0:
            raise ValueError("progress_poll_interval must be > 0")
        if worker_timeout_s is not None and worker_timeout_s <= 0:
            raise ValueError("worker_timeout_s must be > 0 when set")
        if worker_idle_timeout_s is not None and worker_idle_timeout_s <= 0:
            raise ValueError("worker_idle_timeout_s must be > 0 when set")

        self.ocr = ocr
        self.table_structure = table_structure
        self.formula_enrichment = formula_enrichment
        self.code_enrichment = code_enrichment
        self.images_scale = images_scale
        self.device = _resolve_device(device)
        self.num_threads = num_threads
        self.pdf_chunk_pages = pdf_chunk_pages
        self.max_workers = max_workers
        self.progress_poll_interval = progress_poll_interval
        self.worker_timeout_s = worker_timeout_s
        self.worker_idle_timeout_s = worker_idle_timeout_s
        self.chunking = chunking
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.on_progress = on_progress
        self._progress_callback_failed = False
        self._progress_error_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, source: Path | str) -> ConversionResult:
        """Convert a document to Markdown (and optionally chunk it for RAG).

        Automatically detects the file format from the extension and routes
        to the appropriate conversion path:

        - **PDF**: subprocess-isolated conversion with optional page-splitting
          for memory bounding (workarounds W1-W8).
        - **Everything else** (DOCX, PPTX, HTML, XLSX, CSV, Markdown, images):
          direct in-process conversion via Docling's ``SimplePipeline``.

        After conversion, if ``chunking=True`` was set at init time, the
        document is run through Docling's ``HierarchicalChunker`` and the
        resulting structural chunks are attached to the result.

        Args:
            source: Path to the file to convert.

        Returns:
            A :class:`ConversionResult` with markdown, text, html, chunks,
            and performance metrics.

        Raises:
            FileNotFoundError: If *source* does not exist.
            ValueError: If the file extension is not supported.
        """
        self._progress_callback_failed = False
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(f"File not found: {source}")

        ext = source.suffix.lower()
        fmt = _SUPPORTED_EXTENSIONS.get(ext)
        if fmt is None:
            raise ValueError(
                f"Unsupported file extension {ext!r}. Supported: "
                f"{', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
            )

        result = self._parse_pdf(source) if fmt == "pdf" else self._parse_direct(source, fmt)

        result.source = source
        result.format = fmt
        result.file_size = source.stat().st_size

        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.output_dir / source.with_suffix(".md").name
            out_path.write_text(result.markdown, encoding="utf-8")

        return result

    def parse_batch(self, sources: list[Path | str]) -> list[ConversionResult]:
        """Convert multiple documents sequentially.

        Each file is converted via :meth:`parse`.  Results are returned in
        the same order as *sources*.

        Args:
            sources: List of file paths to convert.

        Returns:
            List of :class:`ConversionResult`, one per source.
        """
        return [self.parse(s) for s in sources]

    def warmup(self) -> None:
        """Pre-import heavy modules so the first ``parse()`` call is fast.

        Imports ``torch`` and ``docling``.  This is optional -- modules are
        imported lazily on first use regardless.
        """
        with contextlib.suppress(ImportError):
            import torch  # noqa: F401
        with contextlib.suppress(ImportError):
            from docling.document_converter import DocumentConverter  # noqa: F401

    @property
    def config_summary(self) -> dict[str, Any]:
        """Return a dict of all current configuration values.

        Useful for display/logging in CLI tools.
        """
        return {
            "ocr": self.ocr,
            "table_structure": self.table_structure,
            "formula_enrichment": self.formula_enrichment,
            "code_enrichment": self.code_enrichment,
            "images_scale": self.images_scale,
            "device": self.device,
            "num_threads": self.num_threads,
            "pdf_chunk_pages": self.pdf_chunk_pages,
            "max_workers": self.max_workers,
            "progress_poll_interval": self.progress_poll_interval,
            "worker_timeout_s": self.worker_timeout_s,
            "worker_idle_timeout_s": self.worker_idle_timeout_s,
            "chunking": self.chunking,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "docling_version": _PINNED_DOCLING_VERSION,
        }

    def _notify_progress(self, event: ProgressEvent) -> None:
        """Deliver progress without letting callback failures abort conversion."""
        if self.on_progress is None or self._progress_callback_failed:
            return
        try:
            self.on_progress(event)
        except Exception:
            with self._progress_error_lock:
                if self._progress_callback_failed:
                    return
                self._progress_callback_failed = True
            logger.exception(
                "DoclingWrapper progress callback failed for %s; disabling "
                "further progress events for this parse.",
                event.source,
            )

    # ------------------------------------------------------------------
    # PDF path: subprocess isolation (W1) + page-splitting (W2)
    # ------------------------------------------------------------------

    def _parse_pdf(self, pdf_path: Path) -> ConversionResult:
        """Convert a PDF using subprocess isolation.

        Small PDFs (fewer pages than ``pdf_chunk_pages``) run in a single
        subprocess.  Larger PDFs are split with pikepdf and converted in
        parallel subprocesses, then the Markdown is stitched back together.
        """
        pages = _page_count(pdf_path)
        needs_splitting = self.pdf_chunk_pages > 0 and pages > self.pdf_chunk_pages

        if not needs_splitting:
            return self._convert_single_pdf(pdf_path, pages)
        return self._split_and_convert_pdf(pdf_path, pages)

    def _convert_single_pdf(self, pdf_path: Path, pages: int) -> ConversionResult:
        """Convert an entire PDF in one subprocess (no page-splitting)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            md_file = tmp_dir / "out.md"
            stats_file = tmp_dir / "stats.json"

            cr = self._run_chunk(
                pdf_path,
                pages,
                md_file,
                stats_file,
                chunk_index=0,
                total_chunks=1,
            )

            return ConversionResult(
                markdown=cr.markdown,
                pages=pages,
                elapsed=cr.elapsed,
                mem_peak_mb=cr.peak_rss / (1024 * 1024),
                mem_avg_mb=cr.mem_used / (1024 * 1024),
                chunk_count=1,
                chunks=cr.chunks,
            )

    def _split_and_convert_pdf(self, pdf_path: Path, pages: int) -> ConversionResult:
        """Split a large PDF and convert chunks in parallel subprocesses.

        A splitter thread feeds a bounded queue (W3) so at most
        ``max_workers + 1`` chunk files exist on disk at any time.  Worker
        threads consume chunks, run subprocess conversions, and eagerly
        delete temp files (W8).
        """
        import pikepdf

        chunk_size = self.pdf_chunk_pages
        num_chunks = (pages + chunk_size - 1) // chunk_size
        workers = min(self.max_workers, num_chunks)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            # -- Bounded producer: split pages on-demand (W2, W3, W7) --
            chunk_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.max_workers + 1)
            stop_event = threading.Event()
            splitter_done = threading.Event()
            first_error: list[Exception] = []
            error_lock = threading.Lock()

            def _record_error(exc: Exception) -> None:
                with error_lock:
                    if first_error:
                        return
                    first_error.append(exc)
                stop_event.set()

            def _splitter() -> None:
                src = None
                try:
                    src = pikepdf.open(str(pdf_path))
                    for i, batch_start in enumerate(range(0, pages, chunk_size)):
                        if stop_event.is_set():
                            break

                        batch_end = min(batch_start + chunk_size, pages)
                        chunk_dir = tmp_dir / f"chunk_{i}"
                        chunk_dir.mkdir()
                        chunk_pdf = chunk_dir / "chunk.pdf"

                        dst = pikepdf.new()
                        try:
                            for pg in range(batch_start, batch_end):
                                dst.pages.append(src.pages[pg])

                            # W7: PDFs commonly use a shared /Resources dictionary
                            # across all pages.  When pages are copied, the new PDF
                            # inherits references to resources (fonts, images) used
                            # by other pages too.  remove_unreferenced_resources()
                            # strips /Resources entries whose names are not invoked
                            # in the chunk's content streams.  This helps but does
                            # not eliminate bloat: if the original uses the same font
                            # names on every page, the fonts are "referenced" and
                            # kept.  Chunk files can still be surprisingly large
                            # (e.g. 80 MB for a 25-page chunk from a 140 MB PDF).
                            # This is a known property of the PDF format, not a bug.
                            # The file size does not matter for the memory fix --
                            # what matters is that docling-parse only builds native
                            # state for N pages, not the full document.
                            dst.remove_unreferenced_resources()
                            dst.save(str(chunk_pdf), compress_streams=True)
                        finally:
                            dst.close()

                        item = {
                            "idx": i,
                            "chunk_pdf": chunk_pdf,
                            "chunk_dir": chunk_dir,
                            "batch_pages": batch_end - batch_start,
                            "md_file": chunk_dir / "out.md",
                            "stats_file": chunk_dir / "stats.json",
                        }
                        while not stop_event.is_set():
                            try:
                                chunk_queue.put(item, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                except Exception as exc:
                    _record_error(exc)
                finally:
                    if src is not None:
                        src.close()
                    splitter_done.set()

            splitter_thread = threading.Thread(target=_splitter, daemon=True)
            splitter_thread.start()

            # -- Parallel consumers --
            results: dict[int, _ChunkResult] = {}
            results_lock = threading.Lock()
            wall_t0 = time.perf_counter()

            def _worker_loop() -> None:
                while True:
                    try:
                        ci = chunk_queue.get(timeout=0.1)
                    except queue.Empty:
                        if splitter_done.is_set():
                            return
                        continue

                    try:
                        if stop_event.is_set():
                            return
                        cr = self._run_chunk(
                            ci["chunk_pdf"],
                            ci["batch_pages"],
                            ci["md_file"],
                            ci["stats_file"],
                            chunk_index=ci["idx"],
                            total_chunks=num_chunks,
                        )
                        with results_lock:
                            results[ci["idx"]] = cr
                    except Exception as exc:
                        _record_error(exc)
                        return
                    finally:
                        shutil.rmtree(ci["chunk_dir"], ignore_errors=True)  # W8

            threads = [threading.Thread(target=_worker_loop, daemon=True) for _ in range(workers)]
            for t in threads:
                t.start()
            splitter_thread.join()
            for t in threads:
                t.join()
            if first_error:
                raise first_error[0]
            if len(results) != num_chunks:
                raise RuntimeError(
                    f"PDF split conversion for {pdf_path} completed with "
                    f"{len(results)}/{num_chunks} chunks."
                )

            wall_elapsed = time.perf_counter() - wall_t0

        md_parts = [results[i].markdown for i in range(num_chunks)]
        all_chunks: list[Chunk] = []
        for i in range(num_chunks):
            page_offset = i * chunk_size
            for chunk in results[i].chunks:
                if chunk.page_numbers:
                    chunk.page_numbers = [
                        min(page_no + page_offset, pages) if page_no > 0 else 0
                        for page_no in chunk.page_numbers
                    ]
                all_chunks.append(chunk)
        max_peak = max(r.peak_rss for r in results.values())
        total_mem_used = sum(r.mem_used for r in results.values())
        avg_mem = total_mem_used // max(num_chunks, 1)

        return ConversionResult(
            markdown="\n\n".join(md_parts),
            pages=pages,
            elapsed=wall_elapsed,
            mem_peak_mb=max_peak / (1024 * 1024),
            mem_avg_mb=avg_mem / (1024 * 1024),
            chunk_count=num_chunks,
            chunks=all_chunks,
        )

    def _run_chunk(
        self,
        chunk_pdf: Path,
        chunk_pages: int,
        md_file: Path,
        stats_file: Path,
        *,
        chunk_index: int,
        total_chunks: int,
    ) -> _ChunkResult:
        """Spawn a subprocess worker and poll progress + RSS until it exits.

        This is the parent-side half of workarounds W1 (subprocess isolation),
        W4 (progress file polling), and W6 (external RSS monitoring).
        """
        import psutil

        env = os.environ.copy()
        env[_CONFIG_ENV_KEY] = json.dumps(self._build_config_dict())

        stderr_file = stats_file.with_suffix(".stderr")
        progress_file = stats_file.with_suffix(".progress")
        progress_file.unlink(missing_ok=True)

        with open(stderr_file, "w") as err_fh:
            proc = subprocess.Popen(  # noqa: S603 - fully-controlled argv (sys.executable + __file__ + path strings)
                [
                    sys.executable,
                    __file__,
                    "--worker",
                    str(chunk_pdf),
                    str(md_file),
                    str(stats_file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=err_fh,
                env=env,
            )

        ps = psutil.Process(proc.pid)
        start_t = time.monotonic()
        last_activity_t = start_t
        last_progress_page = 0
        last_progress_total = chunk_pages
        last_progress_update = 0.0
        last_rss = -1
        peak_rss = 0

        while proc.poll() is None:
            now = time.monotonic()
            rss = 0
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                rss = ps.memory_info().rss  # W6
            if rss > peak_rss:
                peak_rss = rss
            if rss != last_rss:
                last_rss = rss
                last_activity_t = now

            current_page = 0
            try:
                if progress_file.exists():  # W4 read
                    snapshot = _read_progress_snapshot(progress_file)
                    current_page = snapshot.page
                    snapshot_total = snapshot.total_pages or last_progress_total
                    if (
                        snapshot.page > last_progress_page
                        or snapshot_total != last_progress_total
                        or snapshot.updated_at > last_progress_update
                    ):
                        last_progress_page = max(last_progress_page, snapshot.page)
                        last_progress_total = max(last_progress_total, snapshot_total)
                        last_progress_update = max(last_progress_update, snapshot.updated_at)
                        last_activity_t = now
            except (ValueError, OSError, TypeError, json.JSONDecodeError):
                pass

            self._notify_progress(
                ProgressEvent(
                    page=current_page,
                    total_pages=last_progress_total or chunk_pages,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    rss_bytes=rss,
                    source=chunk_pdf,
                )
            )

            if self.worker_timeout_s is not None and now - start_t > self.worker_timeout_s:
                self._terminate_worker(proc)
                err_text = self._read_worker_stderr(stderr_file)
                raise RuntimeError(
                    f"Subprocess worker timed out after {self.worker_timeout_s:.1f}s "
                    f"converting {chunk_pdf}.\n{err_text}"
                )
            if (
                self.worker_idle_timeout_s is not None
                and now - last_activity_t > self.worker_idle_timeout_s
            ):
                self._terminate_worker(proc)
                err_text = self._read_worker_stderr(stderr_file)
                raise RuntimeError(
                    f"Subprocess worker stalled for {self.worker_idle_timeout_s:.1f}s "
                    f"while converting {chunk_pdf}.\n{err_text}"
                )

            time.sleep(self.progress_poll_interval)

        if proc.returncode != 0:
            err_text = self._read_worker_stderr(stderr_file)
            raise RuntimeError(
                f"Subprocess worker failed (exit {proc.returncode}) converting "
                f"{chunk_pdf}:\n{err_text}"
            )

        if not stats_file.exists():
            raise RuntimeError(f"Subprocess worker produced no stats file for {chunk_pdf}")

        d = json.loads(stats_file.read_text(encoding="utf-8"))
        markdown = md_file.read_text(encoding="utf-8") if md_file.exists() else ""
        progress_file.unlink(missing_ok=True)
        peak_rss = max(peak_rss, int(d.get("peak_rss", 0) or 0))

        self._notify_progress(
            ProgressEvent(
                page=last_progress_total or chunk_pages,
                total_pages=last_progress_total or chunk_pages,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                rss_bytes=peak_rss,
                source=chunk_pdf,
            )
        )

        chunks: list[Chunk] = []
        chunks_file = md_file.with_suffix(".chunks.json")
        if chunks_file.exists():
            chunks.extend(
                Chunk(
                    text=raw["text"],
                    headings=raw.get("headings", []),
                    page_numbers=raw.get("page_numbers", []),
                )
                for raw in json.loads(chunks_file.read_text(encoding="utf-8"))
            )

        return _ChunkResult(
            pages=d["pages"],
            elapsed=d["elapsed"],
            md_chars=d["md_chars"],
            mem_used=d["mem_used"],
            peak_rss=peak_rss,
            markdown=markdown,
            chunks=chunks,
        )

    # ------------------------------------------------------------------
    # Non-PDF path: in-process conversion
    # ------------------------------------------------------------------

    def _parse_direct(self, source: Path, fmt: str) -> ConversionResult:
        """Convert a non-PDF document directly in-process.

        Non-PDF formats use Docling's ``SimplePipeline`` which has no native
        memory issues, so subprocess isolation is unnecessary.
        """
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter

        fmt_enum = InputFormat(fmt)

        t0 = time.perf_counter()
        converter = DocumentConverter(allowed_formats=[fmt_enum])
        conv = converter.convert(source)
        elapsed = time.perf_counter() - t0

        doc = conv.document
        md = doc.export_to_markdown()
        text = doc.export_to_text()
        html = doc.export_to_html()
        pages = getattr(conv.input, "page_count", 1)

        self._notify_progress(
            ProgressEvent(
                page=pages,
                total_pages=pages,
                chunk_index=0,
                total_chunks=1,
                rss_bytes=0,
                source=source,
            )
        )

        chunks = self._chunk_document(doc) if self.chunking else []

        return ConversionResult(
            markdown=md,
            text=text,
            html=html,
            chunks=chunks,
            pages=pages,
            elapsed=elapsed,
        )

    # ------------------------------------------------------------------
    # RAG chunking
    # ------------------------------------------------------------------

    def _chunk_document(self, doc: Any) -> list[Chunk]:
        """Run a ``DoclingDocument`` through ``HierarchicalChunker``.

        Splits the document on its structural hierarchy (headings, sections,
        tables) without any tokenization.  Downstream consumers can apply
        their own token-aware splitting using whichever tokenizer matches
        their embedding model or API.

        Args:
            doc: A ``DoclingDocument`` instance (from ``docling_core``).

        Returns:
            List of :class:`Chunk` with text, headings, and page numbers.
        """
        from docling.chunking import HierarchicalChunker

        chunker = HierarchicalChunker()

        result_chunks: list[Chunk] = []
        for dc in chunker.chunk(doc):
            page_nums: list[int] = []
            if hasattr(dc, "meta") and hasattr(dc.meta, "doc_items"):
                for item in dc.meta.doc_items:
                    if hasattr(item, "prov"):
                        for prov in item.prov:
                            if hasattr(prov, "page_no") and prov.page_no not in page_nums:
                                page_nums.append(prov.page_no)

            headings: list[str] = []
            if hasattr(dc, "meta") and hasattr(dc.meta, "headings") and dc.meta.headings:
                headings = list(dc.meta.headings)

            text = dc.text if hasattr(dc, "text") else str(dc)

            result_chunks.append(
                Chunk(
                    text=text,
                    headings=headings,
                    page_numbers=sorted(page_nums),
                )
            )

        return result_chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_config_dict(self) -> dict:
        """Serialize pipeline configuration for passing to subprocess workers
        via the ``_DOCLING_CONVERT_CFG`` environment variable.
        """
        return {
            "do_ocr": self.ocr,
            "do_table_structure": self.table_structure,
            "do_formula_enrichment": self.formula_enrichment,
            "do_code_enrichment": self.code_enrichment,
            "images_scale": self.images_scale,
            "num_threads": self.num_threads,
            "device": self.device,
            "chunking": self.chunking,
        }

    def _terminate_worker(self, proc: subprocess.Popen[Any]) -> None:
        """Terminate a stuck child process without leaving it running forever."""
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

    def _read_worker_stderr(self, stderr_file: Path) -> str:
        if not stderr_file.exists():
            return ""
        return stderr_file.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------


def _page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF using pypdfium2 (fast, no Docling)."""
    doc = pdfium.PdfDocument(str(pdf_path))
    n = len(doc)
    doc.close()
    return n


# ---------------------------------------------------------------------------
# Subprocess worker entry point (W1, W4-write, W5)
# ---------------------------------------------------------------------------


def _subprocess_worker(pdf_path: str, md_out_path: str, stats_out_path: str) -> None:
    """Child-process entry point: convert a PDF and write results to disk.

    This function runs in an isolated subprocess spawned by
    :meth:`DoclingWrapper._run_chunk`.  It imports Docling (heavy), runs
    the conversion, and writes:
      - Markdown to *md_out_path*
      - JSON stats to *stats_out_path*
      - Live page progress to ``<stats_out_path>.progress`` (W4-write)

    Before importing Docling, this function calls ``_apply_progress_patch()``
    (W5) to monkey-patch stock Docling with ``progress_callback`` support.
    The patch is necessary because stock Docling has no progress reporting
    mechanism; it replicates the essential behavior from unmerged PR #3042.

    When the process exits, all native memory from ``docling-parse`` is
    reclaimed by the OS (W1).
    """
    _apply_progress_patch()

    import psutil
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorOptions,
        PdfPipelineOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    cfg = json.loads(os.environ[_CONFIG_ENV_KEY])

    progress_file = Path(stats_out_path).with_suffix(".progress")

    def _on_page(event: Any) -> None:
        # W5: The _apply_progress_patch() monkey patch emits
        # _PageProgressEvent objects with page_no / total_pages attributes.
        # The hasattr guard protects against unexpected event shapes if the
        # patch behavior changes or Docling adds its own progress events in
        # a future version.
        if hasattr(event, "page_no"):
            _write_progress_snapshot(
                progress_file,
                page=int(getattr(event, "page_no", 0) or 0),
                total_pages=int(getattr(event, "total_pages", 0) or 0),
            )

    pipeline_options = PdfPipelineOptions(
        do_ocr=cfg["do_ocr"],
        do_table_structure=cfg["do_table_structure"],
        do_formula_enrichment=cfg["do_formula_enrichment"],
        do_code_enrichment=cfg["do_code_enrichment"],
        images_scale=cfg["images_scale"],
        accelerator_options=AcceleratorOptions(
            num_threads=cfg["num_threads"],
            device=cfg["device"],
        ),
    )
    converter = DocumentConverter(  # type: ignore[call-arg]  # progress_callback added by _apply_progress_patch() at module load (see W5 monkey-patch)
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        },
        progress_callback=_on_page,
    )

    doc = pdfium.PdfDocument(pdf_path)
    chunk_pages = len(doc)
    doc.close()

    proc = psutil.Process(os.getpid())
    mem_before = proc.memory_info().rss
    peak_rss = mem_before
    stop_sampling = threading.Event()

    def _sample_peak_rss() -> None:
        nonlocal peak_rss
        while not stop_sampling.is_set():
            try:
                peak_rss = max(peak_rss, proc.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return
            stop_sampling.wait(0.1)

    sampler = threading.Thread(target=_sample_peak_rss, daemon=True)
    sampler.start()

    t0 = time.perf_counter()
    try:
        conv_result = converter.convert(pdf_path)
        doc = conv_result.document
        md = doc.export_to_markdown()
        elapsed = time.perf_counter() - t0
    finally:
        stop_sampling.set()
        sampler.join(timeout=1.0)

    mem_after = proc.memory_info().rss
    peak_rss = max(peak_rss, mem_after)

    Path(md_out_path).write_text(md, encoding="utf-8")

    if cfg.get("chunking", False):
        from docling.chunking import HierarchicalChunker

        chunks_data = []
        for dc in HierarchicalChunker().chunk(doc):
            page_nums = []
            if hasattr(dc, "meta") and hasattr(dc.meta, "doc_items"):
                for item in dc.meta.doc_items:
                    if hasattr(item, "prov"):
                        for prov in item.prov:
                            if hasattr(prov, "page_no") and prov.page_no not in page_nums:
                                page_nums.append(prov.page_no)
            headings = []
            if hasattr(dc, "meta") and hasattr(dc.meta, "headings") and dc.meta.headings:
                headings = list(dc.meta.headings)
            text = dc.text if hasattr(dc, "text") else str(dc)
            chunks_data.append(
                {
                    "text": text,
                    "headings": headings,
                    "page_numbers": sorted(page_nums),
                }
            )

        chunks_file = Path(md_out_path).with_suffix(".chunks.json")
        chunks_file.write_text(json.dumps(chunks_data), encoding="utf-8")

    _write_progress_snapshot(progress_file, page=chunk_pages, total_pages=chunk_pages)

    Path(stats_out_path).write_text(
        json.dumps(
            {
                "pages": chunk_pages,
                "elapsed": round(elapsed, 2),
                "md_chars": len(md),
                "mem_used": mem_after - mem_before,
                "peak_rss": peak_rss,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Script entry point -- routes --worker to the subprocess worker
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--worker":
        _subprocess_worker(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(
            "This module is not meant to be run directly. Import and use\n"
            "DoclingWrapper in your code, or run main.py for the CLI.\n\n"
            "  from docling_wrapper import DoclingWrapper\n"
            "  converter = DoclingWrapper()\n"
            "  result = converter.parse('file.pdf')\n"
        )
        sys.exit(1)
