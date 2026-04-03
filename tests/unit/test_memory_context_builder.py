# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for semantic memory context builder (MEM-RELEVANCE).

Tests build_relevant_memory_context() and build_relevant_memory_summary()
which use semantic search with token budget enforcement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.memory.schemas import MemoryResponse, MemorySearchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    *,
    id: str = "mem-1",
    title: str = "Test memory",
    body: str = "Test body",
    confidence_level: str = "auto_extracted",
    memory_type: str = "entity",
) -> MemoryResponse:
    """Create a MemoryResponse for testing."""
    now = datetime.now(tz=UTC)
    return MemoryResponse(
        id=id,
        tenant_id="t1",
        connector_id="c1",
        title=title,
        body=body,
        memory_type=memory_type,
        tags=[],
        confidence_level=confidence_level,
        source_type="extraction",
        created_by="system",
        provenance_trail=[],
        occurrence_count=1,
        last_accessed=now,
        last_seen=now,
        created_at=now,
        updated_at=now,
        merged=False,
    )


def _make_search_result(
    memory: MemoryResponse,
    similarity: float = 0.9,
    final_score: float = 0.85,
) -> MemorySearchResult:
    """Create a MemorySearchResult wrapping a MemoryResponse."""
    return MemorySearchResult(
        memory=memory,
        similarity=similarity,
        final_score=final_score,
    )


def _setup_patches(
    operator_memories: list[MemoryResponse] | None = None,
    search_results: list[MemorySearchResult] | None = None,
):
    """Create mock service and session patches.

    Returns a context manager that patches create_openapi_session_maker
    and get_memory_service at their source modules.
    """
    svc = AsyncMock()
    svc.list_memories = AsyncMock(return_value=operator_memories or [])
    svc.search = AsyncMock(return_value=search_results or [])

    # Mock session context manager
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_maker = MagicMock(return_value=session_cm)

    return session_maker, svc


# ---------------------------------------------------------------------------
# Tests for build_relevant_memory_context
# ---------------------------------------------------------------------------


class TestBuildRelevantMemoryContext:
    """Tests for the semantic memory context builder."""

    @pytest.mark.asyncio
    async def test_empty_memories_returns_empty_string(self):
        """When no memories exist for the connector, return empty string."""
        session_maker, svc = _setup_patches()

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
            )
            assert result == ""

    @pytest.mark.asyncio
    async def test_operator_memories_always_included(self):
        """Operator memories are included even when they exceed the token budget."""
        op_mem1 = _make_memory(
            id="op-1",
            title="Critical operator note",
            body="This is a very important operator-provided memory " * 20,
            confidence_level="operator",
        )
        op_mem2 = _make_memory(
            id="op-2",
            title="Another operator note",
            body="Another important note " * 20,
            confidence_level="operator",
        )

        session_maker, svc = _setup_patches(
            operator_memories=[op_mem1, op_mem2],
            search_results=[],
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            # Budget of 100 tokens -- operator memories should STILL be included
            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
                token_budget=100,
            )
            assert "Critical operator note" in result
            assert "Another operator note" in result

    @pytest.mark.asyncio
    async def test_auto_extracted_fills_budget(self):
        """Auto-extracted memories fill the budget in relevance order."""
        memories = []
        search_results = []
        for i in range(10):
            mem = _make_memory(
                id=f"auto-{i}",
                title=f"Auto memory {i}",
                body=f"Content for memory {i}",
                confidence_level="auto_extracted",
            )
            memories.append(mem)
            search_results.append(_make_search_result(mem, final_score=0.9 - i * 0.05))

        session_maker, svc = _setup_patches(
            operator_memories=[],
            search_results=search_results,
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
                token_budget=5000,
            )
            # The highest-scored memory should be included
            assert "Auto memory 0" in result

    @pytest.mark.asyncio
    async def test_no_duplicate_operator_in_semantic_results(self):
        """Operator memories appearing in semantic results are not duplicated."""
        op_mem = _make_memory(
            id="op-dup",
            title="Shared operator memory",
            body="This appears in both tiers",
            confidence_level="operator",
        )

        session_maker, svc = _setup_patches(
            operator_memories=[op_mem],
            search_results=[_make_search_result(op_mem, final_score=0.95)],
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
            )
            # Count occurrences -- title should appear exactly once
            assert result.count("Shared operator memory") == 1

    @pytest.mark.asyncio
    async def test_budget_skip_and_continue(self):
        """A large memory is skipped but smaller ones after it are still included."""
        small_mem1 = _make_memory(
            id="small-1",
            title="Small 1",
            body="Small content",
            confidence_level="auto_extracted",
        )
        # Large memory: ~500 tokens
        large_mem = _make_memory(
            id="large-1",
            title="Large memory",
            body="x " * 500,
            confidence_level="auto_extracted",
        )
        small_mem2 = _make_memory(
            id="small-2",
            title="Small 2",
            body="Also small content",
            confidence_level="auto_extracted",
        )

        # Relevance order: small_mem1 > large_mem > small_mem2
        search_results = [
            _make_search_result(small_mem1, final_score=0.95),
            _make_search_result(large_mem, final_score=0.90),
            _make_search_result(small_mem2, final_score=0.85),
        ]

        session_maker, svc = _setup_patches(
            operator_memories=[],
            search_results=search_results,
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            # Budget is 50 tokens -- small memories fit, large one doesn't
            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
                token_budget=50,
            )
            assert "Small 1" in result
            assert "Small 2" in result
            # Large memory should be skipped (exceeds budget)
            assert "Large memory" not in result

    @pytest.mark.asyncio
    async def test_relevance_ordering_preserved(self):
        """Memories appear in final_score descending order."""
        mem_low = _make_memory(
            id="low",
            title="Low relevance",
            body="Content",
            confidence_level="auto_extracted",
        )
        mem_high = _make_memory(
            id="high",
            title="High relevance",
            body="Content",
            confidence_level="auto_extracted",
        )

        # High relevance first in results (already sorted by search)
        search_results = [
            _make_search_result(mem_high, final_score=0.95),
            _make_search_result(mem_low, final_score=0.60),
        ]

        session_maker, svc = _setup_patches(
            operator_memories=[],
            search_results=search_results,
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_context,
            )

            result = await build_relevant_memory_context(
                query="check pod status",
                connector_id="c1",
                tenant_id="t1",
            )
            # Both should be present
            assert "High relevance" in result
            assert "Low relevance" in result
            # High relevance should appear before low relevance
            high_pos = result.index("High relevance")
            low_pos = result.index("Low relevance")
            assert high_pos < low_pos


# ---------------------------------------------------------------------------
# Tests for build_relevant_memory_summary
# ---------------------------------------------------------------------------


class TestBuildRelevantMemorySummary:
    """Tests for the semantic memory summary builder (orchestrator synthesis)."""

    @pytest.mark.asyncio
    async def test_summary_uses_search_not_list(self):
        """build_relevant_memory_summary calls svc.search(), not list_memories(limit=1000)."""
        mem = _make_memory(id="s-1", title="Summarizable", body="Content")
        search_results = [_make_search_result(mem, final_score=0.9)]

        session_maker, svc = _setup_patches(
            operator_memories=[],
            search_results=search_results,
        )

        with (
            patch("meho_app.api.database.get_session_maker", return_value=session_maker),
            patch("meho_app.modules.memory.service.get_memory_service", return_value=svc),
        ):
            from meho_app.modules.memory.context_builder import (
                build_relevant_memory_summary,
            )

            result = await build_relevant_memory_summary(
                query="investigate kubernetes",
                connector_id="c1",
                tenant_id="t1",
            )
            # search should have been called (for semantic relevance)
            svc.search.assert_called_once()
            # Result should contain the memory title
            assert "Summarizable" in result
