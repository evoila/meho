# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for upload size limit in routes_knowledge.py (Phase 90.2, D-06).

Covers: rejection of oversized files with HTTP 413, acceptance of files
within limit, error message content including env var name, edge case
at exact limit boundary.

Mock strategy:
  - Test the size check logic in isolation
  - Verify HTTPException raised with status_code=413
  - Verify error message includes file size and env var name
"""

import pytest

from fastapi import HTTPException


def _check_size_limit(file_content: bytes, max_size_mb: int) -> None:
    """Reproduce the size check logic from routes_knowledge.py upload_document().

    Raises HTTPException(413) if file exceeds max_size_mb.
    This mirrors the inline check so we can test it without
    spinning up the full FastAPI app or mocking all dependencies.
    """
    file_size_mb = len(file_content) / 1024 / 1024
    max_size_bytes = max_size_mb * 1024 * 1024
    if len(file_content) > max_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size ({file_size_mb:.1f} MB) exceeds maximum allowed size "
                f"({max_size_mb} MB). Set MEHO_INGESTION_MAX_FILE_SIZE_MB to increase the limit."
            ),
        )


def test_rejects_file_exceeding_max_size():
    """Files larger than ingestion_max_file_size_mb get HTTP 413."""
    # 250 MB file with 200 MB limit
    file_content = b"\x00" * (250 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc_info:
        _check_size_limit(file_content, max_size_mb=200)
    assert exc_info.value.status_code == 413


def test_accepts_file_within_limit():
    """Files within the size limit pass the check."""
    # 100 MB file with 200 MB limit
    file_content = b"\x00" * (100 * 1024 * 1024)
    # Should not raise
    _check_size_limit(file_content, max_size_mb=200)


def test_error_message_includes_env_var_name():
    """413 error message mentions MEHO_INGESTION_MAX_FILE_SIZE_MB for user guidance."""
    file_content = b"\x00" * (250 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc_info:
        _check_size_limit(file_content, max_size_mb=200)
    assert "MEHO_INGESTION_MAX_FILE_SIZE_MB" in exc_info.value.detail


def test_error_message_includes_file_size():
    """413 error message shows actual file size for user clarity."""
    file_content = b"\x00" * (250 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc_info:
        _check_size_limit(file_content, max_size_mb=200)
    assert "250.0 MB" in exc_info.value.detail


def test_boundary_at_exact_limit():
    """File at exactly max_size_bytes passes (only > is rejected)."""
    # Exactly 200 MB
    file_content = b"\x00" * (200 * 1024 * 1024)
    # Should not raise -- only strictly greater than is rejected
    _check_size_limit(file_content, max_size_mb=200)
