# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Resource estimation for ephemeral ingestion workers.

Maps document page counts to resource profiles (memory, CPU, GPU, timeout).
The sizing table is derived from profiling Docling memory consumption across
document sizes -- see Phase 97.1 CONTEXT.md for measured evidence.
"""

from meho_app.worker.backends.protocol import ResourceProfile

# Sizing table: (max_pages, ResourceProfile)
# Each entry applies to documents with page_count <= max_pages.
# The last entry is a catch-all for extremely large documents.
# Calibrated from measured profiling data:
#   - Single page conversion: ~6 GB peak RSS (PyTorch models + layout + table extraction)
#   - Per-batch processing (50 pages): ~6-8 GB with fresh converter per batch
#   - Model loading baseline: ~1.5 GB (torch + docling + layout + tableformer)
#   - Memory NEVER freed within process (PyTorch glibc allocator)
# The worker creates a fresh converter per batch and exits after completion,
# so memory is bounded by single-batch peak, not total document size.
_SIZING_TABLE: list[tuple[int, ResourceProfile]] = [
    (
        10,
        ResourceProfile(
            memory_gb=8,
            cpu=2,
            gpu=False,
            timeout_seconds=120,
            size_category="tiny",
        ),
    ),
    (
        50,
        ResourceProfile(
            memory_gb=8,
            cpu=2,
            gpu=False,
            timeout_seconds=600,
            size_category="small",
        ),
    ),
    (
        500,
        ResourceProfile(
            memory_gb=16,
            cpu=4,
            gpu=False,
            timeout_seconds=3600,
            size_category="medium",
        ),
    ),
    (
        2000,
        ResourceProfile(
            memory_gb=16,
            cpu=4,
            gpu=False,
            timeout_seconds=10800,
            size_category="large",
        ),
    ),
    (
        999_999,
        ResourceProfile(
            memory_gb=32,
            cpu=8,
            gpu=False,
            timeout_seconds=28800,
            size_category="huge",
        ),
    ),
]


def estimate_resources(page_count: int) -> ResourceProfile:
    """Estimate resource requirements based on document page count.

    Iterates the sizing table and returns the first profile where
    page_count <= max_pages. Falls back to the last (largest) entry
    for documents exceeding all thresholds.

    Args:
        page_count: Number of pages in the document.

    Returns:
        ResourceProfile with memory, CPU, GPU, timeout, and size category.
    """
    for max_pages, profile in _SIZING_TABLE:
        if page_count <= max_pages:
            return profile
    # Fallback to the largest profile for extremely large documents
    return _SIZING_TABLE[-1][1]
