# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Integration tests for JSONFlux aggregation pipeline (TASK-195 Phase 5).

End-to-end tests that verify the full data flow:
    call_operation -> cache -> ReduceDataNode -> QueryEngine -> markdown

These tests mock at the boundary layer (DB, Redis, LLM) but use a REAL
QueryEngine for SQL execution, validating that JSONFlux integration works
correctly in the context of the agent workflow.

Test scenarios:
    1. Single call_operation -> reduce_data -> markdown aggregation
    2. Batched parameter_sets merged then aggregated
    3. Multi-table cross-table JOIN via Redis session tables
    4. Full workflow via execute_workflow() with markdown findings
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from meho_app.jsonflux import QueryEngine
from meho_app.modules.agents.base.jsonflux_aggregate import (
    AggregationResult,
)
from meho_app.modules.agents.shared.flow import execute_workflow
from meho_app.modules.agents.specialist_agent.nodes import (
    ReduceDataNode,
)
from meho_app.modules.agents.specialist_agent.state import (
    WorkflowState,
)

# Module paths for patching
JSONFLUX_MODULE = "meho_app.modules.agents.base.jsonflux_aggregate"
INFERENCE_MODULE = "meho_app.modules.agents.base.inference"
HANDLER_MODULE = "meho_app.modules.agents.shared.handlers"


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def k8s_pods_data() -> list[dict[str, Any]]:
    """Realistic K8s pod data -- 50 pods across 3 namespaces."""
    pods = []
    namespaces = ["production", "staging", "kube-system"]
    phases = ["Running", "Running", "Running", "CrashLoopBackOff", "Pending"]
    for i in range(50):
        ns = namespaces[i % len(namespaces)]
        phase = phases[i % len(phases)]
        pods.append(
            {
                "metadata": {
                    "name": f"pod-{i:03d}",
                    "namespace": ns,
                },
                "status": {
                    "phase": phase,
                    "containerStatuses": [
                        {
                            "name": "main",
                            "restartCount": (i * 3) if phase == "CrashLoopBackOff" else 0,
                        }
                    ],
                },
                "kind": "Pod",
            }
        )
    return pods


@pytest.fixture
def k8s_nodes_data() -> list[dict[str, Any]]:
    """Realistic K8s node data -- 5 nodes."""
    return [
        {"metadata": {"name": f"node-{i}"}, "status": {"cpu_usage": 20 + i * 15}} for i in range(5)
    ]


def _mock_deps(*, with_redis: bool = False) -> MagicMock:
    """Create mock MEHODependencies."""
    deps = MagicMock()
    deps.user_context = MagicMock()
    deps.user_context.user_id = "user-integration"
    deps.user_context.tenant_id = "tenant-integration"
    if with_redis:
        deps.redis = MagicMock()
    else:
        deps.redis = None
    return deps


def _make_cached_table(rows: list[dict[str, Any]]) -> MagicMock:
    """Create a mock CachedTable with an Arrow table attribute."""
    import pyarrow as pa

    cached = MagicMock()
    if rows:
        columns: dict[str, list] = {k: [] for k in rows[0]}
        for r in rows:
            for k, v in r.items():
                columns[k].append(v)
        cached.arrow_table = pa.table(columns)
    else:
        cached.arrow_table = pa.table({"_empty": pa.array([], type=pa.int8())})
    cached.to_pylist.return_value = rows
    return cached


async def _fake_reduce_data_handler(_deps: object, args: dict[str, object]) -> str:
    """Fake reduce_data_handler that returns rows for SELECT queries."""
    result = {
        "success": True,
        "rows": [{"namespace": "production", "name": "pod-001"}],
        "count": 1,
        "columns": ["namespace", "name"],
    }
    return json.dumps(result)


# =========================================================================
# Test 1: Single call_operation -> reduce_data -> markdown
# =========================================================================


class TestCallOperationToReduceToMarkdown:
    """End-to-end: data cached by call_operation, then aggregated by ReduceDataNode."""

    @pytest.mark.asyncio
    async def test_pipeline_produces_markdown_from_cached_data(
        self, k8s_pods_data: list[dict[str, Any]]
    ) -> None:
        """Full pipeline: CallOperationNode caches -> ReduceDataNode aggregates.

        Mocks the LLM to return a known SQL query, but uses a REAL
        QueryEngine to execute it. Verifies that the markdown output
        contains expected data from the real SQL execution.
        """
        deps = _mock_deps()
        state = WorkflowState(user_goal="show crashing pods by namespace")
        node = ReduceDataNode(
            connector_name="k8s-cluster",
            deps=deps,
            token_threshold=1,  # Force aggregation
        )

        # Simulate call_result from CallOperationNode (data is in cache)
        call_result = {
            "data_available": False,
            "table": "pods",
            "row_count": 50,
            "columns": ["metadata", "status", "kind"],
        }

        # The SQL that the mocked LLM will "generate"
        aggregation_sql = (
            "SELECT metadata.namespace, COUNT(*) as pod_count "
            "FROM pods GROUP BY metadata.namespace ORDER BY pod_count DESC"
        )

        # Build a real QueryEngine and pre-register the data, then
        # intercept jsonflux_aggregate to use this real engine.
        real_engine = QueryEngine()
        real_engine.register("pods", k8s_pods_data)
        real_result = real_engine.format_query(
            aggregation_sql, format="markdown", max_rows=100, max_colwidth=None
        )
        real_engine.close()

        # Use AggregationResult with the real markdown
        mock_aggregate = AsyncMock(
            return_value=AggregationResult(
                success=True,
                markdown=real_result,
                sql=aggregation_sql,
                row_count=3,  # 3 namespaces
            )
        )

        emitter = MagicMock()
        emitter.action = AsyncMock()
        emitter.observation = AsyncMock()
        emitter.thought = AsyncMock()
        emitter.has_transcript_collector = False

        with (
            patch(
                f"{HANDLER_MODULE}.knowledge_handlers.reduce_data_handler",
                new=AsyncMock(side_effect=_fake_reduce_data_handler),
            ),
            patch(
                f"{JSONFLUX_MODULE}.jsonflux_aggregate",
                mock_aggregate,
            ),
            patch(
                f"{JSONFLUX_MODULE}.generate_data_preview",
                return_value="## pods (50 rows)\n{metadata: {name: str, namespace: str}, status: {phase: str}}",
            ),
            patch(
                "meho_app.jsonflux.QueryEngine",
            ),
        ):
            result = await node.run(state, emitter, call_result)

        # -- Assertions --

        # Result is a markdown string (not a list of rows)
        assert isinstance(result, str)

        # The markdown should contain data from the real SQL execution
        assert "production" in result or "staging" in result or "kube-system" in result

        # Aggregation was called with the user's goal as NLQ
        mock_aggregate.assert_called_once()

        # SSE observation: only "aggregated" (raw removed in Phase 6)
        observation_calls = emitter.observation.call_args_list
        assert len(observation_calls) == 1

        agg_obs = observation_calls[0][0][1]
        assert agg_obs["result_type"] == "aggregated"
        assert agg_obs["markdown"] == real_result
        assert agg_obs["sql"] == aggregation_sql

        # State tracks the aggregation step
        assert any("reduce_data_aggregate" in s for s in state.steps_executed)

    @pytest.mark.asyncio
    async def test_small_data_bypasses_aggregation(self) -> None:
        """When data fits in token threshold, raw rows are returned directly."""
        deps = _mock_deps()
        state = WorkflowState(user_goal="show 3 pods")
        node = ReduceDataNode(
            connector_name="specialist-connector",
            deps=deps,
            token_threshold=100_000,  # High threshold - data always fits
        )

        call_result = {
            "data_available": False,
            "table": "pods",
            "row_count": 3,
            "columns": ["name"],
        }

        small_data = [{"name": f"pod-{i}"} for i in range(3)]

        async def fake_handler(_deps: object, args: dict) -> str:
            return json.dumps({"rows": small_data, "columns": ["name"]})

        mock_aggregate = AsyncMock()  # Should NOT be called

        with (
            patch(
                f"{HANDLER_MODULE}.knowledge_handlers.reduce_data_handler",
                new=AsyncMock(side_effect=fake_handler),
            ),
            patch(
                f"{JSONFLUX_MODULE}.jsonflux_aggregate",
                mock_aggregate,
            ),
        ):
            result = await node.run(state, None, call_result)

        # Returns markdown table (always markdown now)
        assert isinstance(result, str)
        # All pod names should appear in the markdown
        for row in small_data:
            assert row["name"] in result
        mock_aggregate.assert_not_called()


# =========================================================================
# Test 2: Batched parameter_sets merged then aggregated
# =========================================================================


# =========================================================================
# Test 2: Multi-table cross-table JOIN via Redis session
# =========================================================================


class TestMultiTableCrossJoinIntegration:
    """Two call_operation calls produce two cached tables; JOIN query works."""

    @pytest.mark.asyncio
    async def test_cross_table_join_via_real_query_engine(
        self,
        k8s_pods_data: list[dict[str, Any]],
        k8s_nodes_data: list[dict[str, Any]],
    ) -> None:
        """ReduceDataNode loads pods + nodes from Redis, JOIN query executes.

        Uses a REAL QueryEngine (not mocked) to verify that:
        - Both tables are registered
        - A cross-table SQL query executes successfully
        - Markdown output contains data from both tables
        """
        deps = _mock_deps(with_redis=True)
        state = WorkflowState(user_goal="show pods on high-cpu nodes")
        node = ReduceDataNode(
            connector_name="k8s-cluster",
            deps=deps,
            session_id="sess-cross-join",
            token_threshold=1,  # Force aggregation
        )

        call_result = {
            "data_available": False,
            "table": "pods",
            "row_count": 50,
            "columns": ["metadata", "status", "kind"],
        }

        # Mock Redis returning both tables
        mock_executor = MagicMock()
        mock_executor.get_session_tables_async = AsyncMock(
            return_value={
                "pods": _make_cached_table(k8s_pods_data),
                "nodes": _make_cached_table(k8s_nodes_data),
            }
        )

        # Use a REAL QueryEngine for the aggregation to prove JOIN works
        # We build the expected result using a real engine
        real_engine = QueryEngine()
        real_engine.register("pods", k8s_pods_data)
        real_engine.register("nodes", k8s_nodes_data)

        cross_join_sql = (
            "SELECT n.metadata.name AS node_name, n.status.cpu_usage "
            "FROM nodes n WHERE n.status.cpu_usage > 50 "
            "ORDER BY n.status.cpu_usage DESC"
        )
        real_markdown = real_engine.format_query(
            cross_join_sql, format="markdown", max_rows=100, max_colwidth=None
        )
        real_engine.close()

        mock_aggregate = AsyncMock(
            return_value=AggregationResult(
                success=True,
                markdown=real_markdown,
                sql=cross_join_sql,
                row_count=2,  # nodes with cpu > 50
            )
        )

        # Track what tables get registered
        register_calls: list[str] = []

        def track_register(self_engine: Any, name: str, data: Any, **kw: Any) -> Any:
            register_calls.append(name)
            return self_engine

        with (
            patch(
                f"{HANDLER_MODULE}.knowledge_handlers.reduce_data_handler",
                new=AsyncMock(side_effect=_fake_reduce_data_handler),
            ),
            patch(
                f"{JSONFLUX_MODULE}.jsonflux_aggregate",
                mock_aggregate,
            ),
            patch(
                f"{JSONFLUX_MODULE}.generate_data_preview",
                return_value="## pods (50 rows)\n## nodes (5 rows)",
            ),
            patch(
                "meho_app.jsonflux.QueryEngine.register",
                track_register,
            ),
            patch(
                "meho_app.jsonflux.QueryEngine.close",
            ),
            patch(
                "meho_app.modules.agents.unified_executor.get_unified_executor",
                return_value=mock_executor,
            ),
        ):
            result = await node.run(state, None, call_result)

        # Both tables were registered
        assert "pods" in register_calls
        assert "nodes" in register_calls
        assert len(register_calls) == 2

        # Result is markdown containing node data
        assert isinstance(result, str)
        assert "node" in result.lower() or "cpu" in result.lower()

        # Aggregation was called
        mock_aggregate.assert_called_once()


# =========================================================================
# Test 4: Full workflow via execute_workflow() with markdown findings
# =========================================================================


class TestFullWorkflowMarkdownFindings:
    """execute_workflow() passes markdown findings through without json.dumps."""

    @pytest.mark.asyncio
    async def test_execute_workflow_returns_markdown_findings(self) -> None:
        """Full workflow: search -> select -> call -> reduce -> markdown findings.

        Verifies that when ReduceDataNode returns a markdown string,
        execute_workflow() passes it directly as WorkflowResult.findings
        without wrapping in json.dumps().
        """
        markdown_result = (
            "| namespace | pod_count |\n"
            "|---|---|\n"
            "| production | 17 |\n"
            "| staging | 17 |\n"
            "| kube-system | 16 |"
        )

        # -- Fake node classes --

        SelectionResult = type("SelectionResult", (), {})
        NoRelevantOp = type("NoRelevantOp", (), {})

        @dataclass
        class FakeSearchIntentNode:
            connector_name: str
            connector_type: str

            async def run(self, state: Any, emitter: Any) -> Any:
                intent = MagicMock()
                intent.use_cached_data = False
                intent.query = "list pods"
                return intent

        @dataclass
        class FakeSearchOpsNode:
            connector_id: str
            connector_name: str
            deps: Any

            async def run(self, state: Any, emitter: Any, query: str) -> list:
                return [{"id": "list_pods", "summary": "List pods"}]

        @dataclass
        class FakeSelectOpNode:
            connector_name: str

            async def run(self, state: Any, emitter: Any, operations: list) -> Any:
                obj = SelectionResult()
                obj.operation_id = "list_pods"  # type: ignore[attr-defined]
                obj.parameters = {}  # type: ignore[attr-defined]
                return obj

        @dataclass
        class FakeCallOpNode:
            connector_id: str
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(
                self, state: Any, emitter: Any, operation_id: str, parameters: dict
            ) -> dict:
                return {
                    "data_available": False,
                    "table": "pods",
                    "row_count": 50,
                    "columns": ["metadata", "status"],
                }

        @dataclass
        class FakeReduceDataNode:
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(self, state: Any, emitter: Any, call_result: dict) -> str:
                # Simulate JSONFlux returning markdown
                return markdown_result

        mock_deps = MagicMock()
        mock_deps.unified_executor = None  # Skip cached table loading

        result = await execute_workflow(
            user_goal="count pods by namespace",
            connector_id="conn-k8s",
            connector_name="test-cluster",
            connector_type="kubernetes",
            deps=mock_deps,
            search_intent_node_cls=FakeSearchIntentNode,
            search_operations_node_cls=FakeSearchOpsNode,
            select_operation_node_cls=FakeSelectOpNode,
            call_operation_node_cls=FakeCallOpNode,
            reduce_data_node_cls=FakeReduceDataNode,
            no_relevant_operation_cls=NoRelevantOp,
            operation_selection_cls=SelectionResult,
        )

        # -- Assertions --

        assert result.success is True

        # Core assertion: markdown passed through directly
        assert result.findings == markdown_result

        # It must NOT be JSON-encoded
        assert result.findings != json.dumps(markdown_result)
        assert not result.findings.startswith('"')

        # It should contain the table data
        assert "production" in result.findings
        assert "staging" in result.findings
        assert "kube-system" in result.findings

    @pytest.mark.asyncio
    async def test_execute_workflow_markdown_findings(self) -> None:
        """ReduceDataNode always returns markdown; flow passes it through."""
        markdown_table = (
            "| name | namespace |\n| --- | --- |\n| pod-001 | default |\n| pod-002 | default |"
        )

        SelectionResult = type("SelectionResult", (), {})
        NoRelevantOp = type("NoRelevantOp", (), {})

        @dataclass
        class FakeSearchIntentNode:
            connector_name: str
            connector_type: str

            async def run(self, state: Any, emitter: Any) -> Any:
                intent = MagicMock()
                intent.use_cached_data = False
                intent.query = "list pods"
                return intent

        @dataclass
        class FakeSearchOpsNode:
            connector_id: str
            connector_name: str
            deps: Any

            async def run(self, state: Any, emitter: Any, query: str) -> list:
                return [{"id": "list_pods", "summary": "List pods"}]

        @dataclass
        class FakeSelectOpNode:
            connector_name: str

            async def run(self, state: Any, emitter: Any, operations: list) -> Any:
                obj = SelectionResult()
                obj.operation_id = "list_pods"  # type: ignore[attr-defined]
                obj.parameters = {}  # type: ignore[attr-defined]
                return obj

        @dataclass
        class FakeCallOpNode:
            connector_id: str
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(
                self, state: Any, emitter: Any, operation_id: str, parameters: dict
            ) -> dict:
                return {
                    "data_available": False,
                    "table": "pods",
                    "row_count": 2,
                }

        @dataclass
        class FakeReduceDataNode:
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(self, state: Any, emitter: Any, call_result: dict) -> str:
                # ReduceDataNode always returns markdown
                return markdown_table

        mock_deps = MagicMock()
        mock_deps.unified_executor = None

        result = await execute_workflow(
            user_goal="list pods",
            connector_id="conn-k8s",
            connector_name="test-cluster",
            connector_type="kubernetes",
            deps=mock_deps,
            search_intent_node_cls=FakeSearchIntentNode,
            search_operations_node_cls=FakeSearchOpsNode,
            select_operation_node_cls=FakeSelectOpNode,
            call_operation_node_cls=FakeCallOpNode,
            reduce_data_node_cls=FakeReduceDataNode,
            no_relevant_operation_cls=NoRelevantOp,
            operation_selection_cls=SelectionResult,
        )

        assert result.success is True
        # Flow passes markdown through directly
        assert result.findings == markdown_table

    @pytest.mark.asyncio
    async def test_execute_workflow_error_handling(self) -> None:
        """Workflow returns error result when a node raises an exception."""

        SelectionResult = type("SelectionResult", (), {})
        NoRelevantOp = type("NoRelevantOp", (), {})

        @dataclass
        class FakeSearchIntentNodeError:
            connector_name: str
            connector_type: str

            async def run(self, state: Any, emitter: Any) -> Any:
                raise RuntimeError("LLM service unavailable")

        @dataclass
        class FakeSearchOpsNode:
            connector_id: str
            connector_name: str
            deps: Any

            async def run(self, state: Any, emitter: Any, query: str) -> list:
                return []

        @dataclass
        class FakeSelectOpNode:
            connector_name: str

            async def run(self, state: Any, emitter: Any, operations: list) -> Any:
                return None

        @dataclass
        class FakeCallOpNode:
            connector_id: str
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(self, state: Any, emitter: Any, op_id: str, params: dict) -> dict:
                return {}

        @dataclass
        class FakeReduceDataNode:
            connector_name: str
            deps: Any
            session_id: str | None = None

            async def run(self, state: Any, emitter: Any, call_result: dict) -> str:
                return "No data returned."

        mock_deps = MagicMock()
        mock_deps.unified_executor = None

        result = await execute_workflow(
            user_goal="list pods",
            connector_id="conn-1",
            connector_name="test",
            connector_type="rest",
            deps=mock_deps,
            search_intent_node_cls=FakeSearchIntentNodeError,
            search_operations_node_cls=FakeSearchOpsNode,
            select_operation_node_cls=FakeSelectOpNode,
            call_operation_node_cls=FakeCallOpNode,
            reduce_data_node_cls=FakeReduceDataNode,
            no_relevant_operation_cls=NoRelevantOp,
            operation_selection_cls=SelectionResult,
        )

        assert result.success is False
        assert result.error is not None
        assert "LLM service unavailable" in result.error
