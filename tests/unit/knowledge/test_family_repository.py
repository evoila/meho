# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for DocumentFamilyRepository uniqueness checks.

These tests mock the async SQLAlchemy session to verify that:
- `has_version` returns True when a matching non-deleted job exists
- `has_hash` returns True when a matching non-deleted job exists
- Both return False when the lookup yields no rows
- Both gracefully handle invalid family_id strings
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.knowledge.family_repository import DocumentFamilyRepository


def _make_repo_with_result(first_row: object | None) -> DocumentFamilyRepository:
    """Build a repo whose session.execute() returns a result yielding `first_row`."""
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.first.return_value = first_row
    session.execute = AsyncMock(return_value=exec_result)
    return DocumentFamilyRepository(session=session)


class TestHasVersion:
    """Tests for `has_version` duplicate-version detection."""

    @pytest.mark.asyncio
    async def test_existing_version_returns_true(self) -> None:
        repo = _make_repo_with_result(first_row=(uuid.uuid4(),))
        assert await repo.has_version(uuid.uuid4(), "v9") is True

    @pytest.mark.asyncio
    async def test_missing_version_returns_false(self) -> None:
        repo = _make_repo_with_result(first_row=None)
        assert await repo.has_version(uuid.uuid4(), "v9") is False

    @pytest.mark.asyncio
    async def test_invalid_family_id_returns_false(self) -> None:
        repo = _make_repo_with_result(first_row=(uuid.uuid4(),))
        assert await repo.has_version("not-a-uuid", "v9") is False

    @pytest.mark.asyncio
    async def test_string_family_id_is_converted(self) -> None:
        family_id = str(uuid.uuid4())
        repo = _make_repo_with_result(first_row=None)
        result = await repo.has_version(family_id, "1.0.0")
        assert result is False
        assert repo.session.execute.await_count == 1


class TestHasHash:
    """Tests for `has_hash` duplicate-content detection."""

    @pytest.mark.asyncio
    async def test_existing_hash_returns_true(self) -> None:
        repo = _make_repo_with_result(first_row=(uuid.uuid4(),))
        assert await repo.has_hash(uuid.uuid4(), "abc123") is True

    @pytest.mark.asyncio
    async def test_missing_hash_returns_false(self) -> None:
        repo = _make_repo_with_result(first_row=None)
        assert await repo.has_hash(uuid.uuid4(), "deadbeef") is False

    @pytest.mark.asyncio
    async def test_invalid_family_id_returns_false(self) -> None:
        repo = _make_repo_with_result(first_row=(uuid.uuid4(),))
        assert await repo.has_hash("bogus", "abc123") is False


class TestListVersions:
    """Tests for list_versions bulk retrieval."""

    @pytest.mark.asyncio
    async def test_invalid_family_id_returns_empty(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        repo = DocumentFamilyRepository(session=session)
        assert await repo.list_versions("not-a-uuid") == []
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_scalars_all_as_list(self) -> None:
        session = MagicMock()
        scalar_result = MagicMock()
        scalar_result.all.return_value = ["job1", "job2"]
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalar_result
        session.execute = AsyncMock(return_value=exec_result)

        repo = DocumentFamilyRepository(session=session)
        versions = await repo.list_versions(uuid.uuid4())
        assert versions == ["job1", "job2"]


class TestGetFamily:
    """Tests for get_family lookup."""

    @pytest.mark.asyncio
    async def test_invalid_id_returns_none(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        repo = DocumentFamilyRepository(session=session)
        assert await repo.get_family("not-a-uuid") is None
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_scalar_one_or_none(self) -> None:
        session = MagicMock()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = "family-model"
        session.execute = AsyncMock(return_value=exec_result)

        repo = DocumentFamilyRepository(session=session)
        family = await repo.get_family(uuid.uuid4())
        assert family == "family-model"
