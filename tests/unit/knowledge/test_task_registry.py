# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for :class:`IngestionTaskRegistry`."""

import asyncio

import pytest

from meho_app.modules.knowledge.task_registry import (
    IngestionTaskRegistry,
    get_task_registry,
    reset_task_registry,
)


class TestIngestionTaskRegistry:
    """Basic register / get / pop / auto-cleanup behaviour."""

    async def test_register_stores_task_under_job_id(self) -> None:
        registry = IngestionTaskRegistry()

        async def _work() -> str:
            await asyncio.sleep(0)
            return "done"

        task = registry.register("job-1", _work())

        assert registry.get("job-1") is task
        assert "job-1" in registry
        assert len(registry) == 1

        await task
        # Give the done-callback a chance to fire.
        await asyncio.sleep(0)

    async def test_task_auto_removed_after_completion(self) -> None:
        """M4 regression: the add_done_callback path must actually fire."""
        registry = IngestionTaskRegistry()

        async def _work() -> None:
            await asyncio.sleep(0)

        task = registry.register("job-2", _work())
        await task
        # Yield the loop so the done callback runs.
        await asyncio.sleep(0)

        assert registry.get("job-2") is None
        assert "job-2" not in registry
        assert len(registry) == 0

    async def test_task_auto_removed_after_cancellation(self) -> None:
        registry = IngestionTaskRegistry()

        async def _work() -> None:
            await asyncio.sleep(10)

        task = registry.register("job-3", _work())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert registry.get("job-3") is None

    async def test_task_auto_removed_after_failure(self) -> None:
        registry = IngestionTaskRegistry()

        async def _work() -> None:
            raise RuntimeError("boom")

        task = registry.register("job-4", _work())
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

        assert registry.get("job-4") is None

    async def test_pop_returns_task_and_removes_it(self) -> None:
        registry = IngestionTaskRegistry()

        async def _work() -> None:
            await asyncio.sleep(10)

        task = registry.register("job-5", _work())
        popped = registry.pop("job-5")

        assert popped is task
        assert registry.get("job-5") is None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_get_returns_none_for_unknown_job(self) -> None:
        registry = IngestionTaskRegistry()
        assert registry.get("does-not-exist") is None


class TestGetTaskRegistry:
    """Module-level accessor for the default singleton."""

    def setup_method(self) -> None:
        reset_task_registry()

    def teardown_method(self) -> None:
        reset_task_registry()

    def test_returns_same_instance_across_calls(self) -> None:
        first = get_task_registry()
        second = get_task_registry()
        assert first is second

    def test_reset_forces_new_instance(self) -> None:
        first = get_task_registry()
        reset_task_registry()
        second = get_task_registry()
        assert first is not second
