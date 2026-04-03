# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for specialist agent observation compressor.

Tests tool-aware compression of observations before they enter the scratchpad.
Phase 33 (v1.69 Token Optimization): COMP-01 through COMP-04.

Wave 0: Tests written before implementation (TDD RED).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from meho_app.modules.agents.react_agent.tools.call_operation import (
    CallOperationOutput,
)
from meho_app.modules.agents.react_agent.tools.reduce_data import (
    ReduceDataOutput,
)
from meho_app.modules.agents.react_agent.tools.search_knowledge import (
    KnowledgeResult,
    SearchKnowledgeOutput,
)
from meho_app.modules.agents.react_agent.tools.search_operations import (
    OperationInfo,
    SearchOperationsOutput,
)
from meho_app.modules.agents.specialist_agent.compressor import (
    compress_observation,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cached_call_operation_output() -> CallOperationOutput:
    """44 VMs cached in DuckDB -- the big win for compression."""
    results = [
        {
            "id": f"vm-{i}",
            "name": f"web-{i}.prod.internal",
            "cpu": 4 + (i % 8),
            "memory": 8192 + (i * 256),
            "status": "running" if i % 3 != 0 else "stopped",
            "cluster": f"cluster-{i % 4}",
            "datacenter": "dc-west",
        }
        for i in range(44)
    ]
    return CallOperationOutput(
        results=results,
        data_available=True,
        table="virtual_machines",
        row_count=44,
        columns=["id", "name", "cpu", "memory", "status", "cluster", "datacenter"],
        success=True,
    )


@pytest.fixture
def non_cacheable_call_operation_output() -> CallOperationOutput:
    """Single inline result, not cached to DuckDB."""
    return CallOperationOutput(
        results=[{"status": "ok", "version": "7.0.3"}],
        data_available=False,
        table=None,
        row_count=None,
        columns=None,
        success=True,
    )


@pytest.fixture
def failed_call_operation_output() -> CallOperationOutput:
    """Connection error from vCenter."""
    return CallOperationOutput(
        results=[],
        data_available=False,
        success=False,
        error="Connection refused: vCenter at 10.0.0.1:443",
    )


@pytest.fixture
def search_operations_output() -> SearchOperationsOutput:
    """8 operations with long descriptions to test truncation."""
    operations = [
        OperationInfo(
            operation_id=f"op-{i}",
            name=f"get_virtual_machines_{i}",
            description=(
                f"Retrieves a comprehensive list of all virtual machines "
                f"deployed across the vSphere infrastructure cluster {i} "
                f"including their current power state, resource allocation, "
                f"and configuration details for monitoring purposes"
            ),
            category="compute",
        )
        for i in range(8)
    ]
    return SearchOperationsOutput(
        operations=operations,
        total_found=8,
    )


@pytest.fixture
def search_knowledge_output() -> SearchKnowledgeOutput:
    """3 knowledge docs with realistic troubleshooting content."""
    results = [
        KnowledgeResult(
            content=(
                "When Kubernetes pods enter CrashLoopBackOff, the most common "
                "causes are: 1) Application startup failures due to missing "
                "environment variables or config maps. 2) Resource limits being "
                "too restrictive, causing OOMKill. 3) Health check probes failing "
                "because the application needs more time to initialize. Check "
                "pod events with kubectl describe pod, review container logs with "
                "kubectl logs, and verify resource requests vs limits in the "
                "deployment spec. For persistent issues, examine the node's "
                "resource capacity and consider increasing the pod's memory limit."
            ),
            source="docs/k8s-troubleshooting.md",
        ),
        KnowledgeResult(
            content=(
                "VMware vSphere HA admission control reserves resources to "
                "guarantee failover capacity. When VMs fail to power on with "
                "'insufficient resources' errors, check the HA admission control "
                "policy. Percentage-based policies reserve a fixed percentage of "
                "cluster resources. Slot-based policies calculate a slot size "
                "from the largest VM reservation. Consider switching to percentage "
                "mode if slot sizes are inflated by a single large VM. Also "
                "verify that DRS is enabled to balance workloads across hosts "
                "before and after failover events occur."
            ),
            source="docs/vsphere-ha-guide.md",
        ),
        KnowledgeResult(
            content=(
                "Prometheus alerting rules for infrastructure monitoring should "
                "follow these best practices: Use recording rules for expensive "
                "queries to pre-compute aggregations. Set appropriate evaluation "
                "intervals -- 15s for critical alerts, 60s for warnings. Always "
                "include runbook_url annotations pointing to remediation docs. "
                "Group related alerts using alert groups to reduce notification "
                "noise. Use inhibition rules to suppress downstream alerts when "
                "a root cause is already firing. Keep alerting thresholds slightly "
                "above normal operating ranges to avoid flapping."
            ),
            source="docs/prometheus-alerting.md",
        ),
    ]
    return SearchKnowledgeOutput(results=results, total_found=3)


@pytest.fixture
def reduce_data_output() -> ReduceDataOutput:
    """5 rows, 4 columns -- typical SQL query result."""
    return ReduceDataOutput(
        rows=[
            {"name": "web-0", "cpu": 4, "memory": 8192, "status": "running"},
            {"name": "web-1", "cpu": 8, "memory": 16384, "status": "running"},
            {"name": "db-0", "cpu": 16, "memory": 32768, "status": "running"},
            {"name": "db-1", "cpu": 16, "memory": 32768, "status": "stopped"},
            {"name": "cache-0", "cpu": 2, "memory": 4096, "status": "running"},
        ],
        columns=["name", "cpu", "memory", "status"],
        row_count=5,
        success=True,
    )


@pytest.fixture
def wide_reduce_data_output() -> ReduceDataOutput:
    """30 columns, 3 rows -- tests column trimming at 25."""
    columns = [f"col_{i}" for i in range(30)]
    rows = [{col: f"val_{r}_{c}" for c, col in enumerate(columns)} for r in range(3)]
    return ReduceDataOutput(
        rows=rows,
        columns=columns,
        row_count=3,
        success=True,
    )


@pytest.fixture
def failed_reduce_data_output() -> ReduceDataOutput:
    """SQL error with available tables hint."""
    return ReduceDataOutput(
        rows=[],
        columns=[],
        row_count=0,
        success=False,
        error="no such table: foo",
        available_tables=["virtual_machines", "pods"],
    )


@pytest.fixture
def mock_infer():
    """Mock the infer() function used by knowledge compression."""
    with patch(
        "meho_app.modules.agents.specialist_agent.compressor.infer",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = "Synthesized summary from Haiku."
        yield mock


# ─────────────────────────────────────────────────────────────────────────────
# Test Classes
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestCompressObservationDispatch:
    """COMP-01: compress_observation() dispatches to correct compressor."""

    @pytest.mark.asyncio
    async def test_dispatches_call_operation(
        self, cached_call_operation_output: CallOperationOutput
    ):
        result = await compress_observation(cached_call_operation_output, "call_operation")
        assert isinstance(result, str)
        assert "CallOperationOutput(" not in result

    @pytest.mark.asyncio
    async def test_dispatches_search_operations(
        self, search_operations_output: SearchOperationsOutput
    ):
        result = await compress_observation(search_operations_output, "search_operations")
        assert "Found" in result
        assert "op-0" in result

    @pytest.mark.asyncio
    async def test_dispatches_search_knowledge(
        self,
        search_knowledge_output: SearchKnowledgeOutput,
        mock_infer,
    ):
        result = await compress_observation(
            search_knowledge_output,
            "search_knowledge",
            thought="Why are pods crashing?",
        )
        assert "Knowledge (" in result

    @pytest.mark.asyncio
    async def test_dispatches_reduce_data(self, reduce_data_output: ReduceDataOutput):
        result = await compress_observation(reduce_data_output, "reduce_data")
        assert "|" in result

    @pytest.mark.asyncio
    async def test_string_passthrough(self):
        result = await compress_observation("Error: something went wrong", "call_operation")
        assert result == "Error: something went wrong"

    @pytest.mark.asyncio
    async def test_unknown_model_fallback(self):

        class RandomModel(BaseModel):
            foo: str = "bar"

        result = await compress_observation(RandomModel(), "unknown_tool")
        assert "bar" in result


@pytest.mark.unit
class TestCallOperationCompression:
    """COMP-02: call_operation compression strips raw data."""

    @pytest.mark.asyncio
    async def test_cached_result_metadata_only(
        self, cached_call_operation_output: CallOperationOutput
    ):
        result = await compress_observation(cached_call_operation_output, "call_operation")
        assert "Cached table 'virtual_machines': 44 rows" in result
        assert "Columns:" in result
        assert "(Use reduce_data to query this table)" in result
        # Must NOT contain raw data values
        assert "vm-0" not in result
        assert "web-0" not in result

    @pytest.mark.asyncio
    async def test_non_cacheable_result(
        self, non_cacheable_call_operation_output: CallOperationOutput
    ):
        result = await compress_observation(non_cacheable_call_operation_output, "call_operation")
        assert "1 result(s)" in result
        assert "non-cacheable" in result

    @pytest.mark.asyncio
    async def test_error_passthrough(self, failed_call_operation_output: CallOperationOutput):
        result = await compress_observation(failed_call_operation_output, "call_operation")
        assert result.startswith("Error:")
        assert "Connection refused" in result

    @pytest.mark.asyncio
    async def test_compression_ratio(self, cached_call_operation_output: CallOperationOutput):
        result = await compress_observation(cached_call_operation_output, "call_operation")
        original = str(cached_call_operation_output)
        assert len(result) < len(original) / 5


@pytest.mark.unit
class TestSearchOperationsCompression:
    """COMP-03: search_operations compression produces name+description list."""

    @pytest.mark.asyncio
    async def test_format_name_description_list(
        self, search_operations_output: SearchOperationsOutput
    ):
        result = await compress_observation(search_operations_output, "search_operations")
        assert result.startswith("Found 8 operations:")
        assert "- op-0:" in result

    @pytest.mark.asyncio
    async def test_description_truncation(self, search_operations_output: SearchOperationsOutput):
        result = await compress_observation(search_operations_output, "search_operations")
        for line in result.split("\n"):
            if line.startswith("- "):
                assert len(line) <= 120  # op_id + truncated desc + margin

    @pytest.mark.asyncio
    async def test_empty_operations(self):
        empty = SearchOperationsOutput(operations=[], total_found=0)
        result = await compress_observation(empty, "search_operations")
        assert "No operations found" in result


@pytest.mark.unit
class TestSearchKnowledgeCompression:
    """COMP-03: search_knowledge uses Haiku synthesis with fallback."""

    @pytest.mark.asyncio
    async def test_synthesized_summary_with_header(
        self,
        search_knowledge_output: SearchKnowledgeOutput,
        mock_infer,
    ):
        result = await compress_observation(
            search_knowledge_output,
            "search_knowledge",
            thought="Why are pods crashing?",
        )
        assert "Knowledge (3 docs:" in result
        assert "k8s-troubleshooting.md" in result
        assert "Synthesized summary from Haiku." in result

    @pytest.mark.asyncio
    async def test_haiku_failure_fallback(
        self,
        search_knowledge_output: SearchKnowledgeOutput,
    ):
        with patch(
            "meho_app.modules.agents.specialist_agent.compressor.infer",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await compress_observation(
                search_knowledge_output,
                "search_knowledge",
                thought="Why are pods crashing?",
            )
        assert "Knowledge (3 docs:" in result
        # Fallback: excerpt-based (first ~200 chars of each doc)
        assert "Kubernetes pods" in result

    @pytest.mark.asyncio
    async def test_empty_results(self):
        empty = SearchKnowledgeOutput(results=[], total_found=0)
        result = await compress_observation(empty, "search_knowledge")
        assert "No knowledge documents found" in result


@pytest.mark.unit
class TestReduceDataCompression:
    """COMP-04: reduce_data renders markdown pipe tables."""

    @pytest.mark.asyncio
    async def test_markdown_pipe_table(self, reduce_data_output: ReduceDataOutput):
        result = await compress_observation(reduce_data_output, "reduce_data")
        assert "| name | cpu | memory | status |" in result
        assert "| --- |" in result
        # Check data rows are pipe-delimited
        assert "| web-0 |" in result

    @pytest.mark.asyncio
    async def test_row_count_footer(self, reduce_data_output: ReduceDataOutput):
        result = await compress_observation(reduce_data_output, "reduce_data")
        assert "(5 rows)" in result

    @pytest.mark.asyncio
    async def test_no_pydantic_wrapper(self, reduce_data_output: ReduceDataOutput):
        result = await compress_observation(reduce_data_output, "reduce_data")
        assert "ReduceDataOutput(" not in result
        assert "success=" not in result
        assert "error=None" not in result
        assert "available_tables=" not in result

    @pytest.mark.asyncio
    async def test_column_trimming(self, wide_reduce_data_output: ReduceDataOutput):
        result = await compress_observation(wide_reduce_data_output, "reduce_data")
        # Only first 25 columns should appear
        assert "col_0" in result
        assert "col_24" in result
        assert "col_25" not in result.split("\n")[0]  # Not in header
        # Footer should mention omitted columns
        assert "25 of 30 columns" in result

    @pytest.mark.asyncio
    async def test_error_with_available_tables(self, failed_reduce_data_output: ReduceDataOutput):
        result = await compress_observation(failed_reduce_data_output, "reduce_data")
        assert "SQL error:" in result or "no such table" in result
        assert "Available tables:" in result

    @pytest.mark.asyncio
    async def test_zero_rows(self):
        empty = ReduceDataOutput(
            rows=[],
            columns=["name", "cpu"],
            row_count=0,
            success=True,
        )
        result = await compress_observation(empty, "reduce_data")
        assert "Query returned 0 rows" in result


@pytest.mark.unit
class TestCompressionRatio:
    """Verify compression achieves meaningful size reduction."""

    @pytest.mark.asyncio
    async def test_call_operation_ratio(self, cached_call_operation_output: CallOperationOutput):
        result = await compress_observation(cached_call_operation_output, "call_operation")
        original = str(cached_call_operation_output)
        ratio = len(result) / len(original)
        assert ratio < 0.10, f"call_operation compression ratio {ratio:.2f} exceeds 0.10"

    @pytest.mark.asyncio
    async def test_search_operations_ratio(self, search_operations_output: SearchOperationsOutput):
        result = await compress_observation(search_operations_output, "search_operations")
        original = str(search_operations_output)
        ratio = len(result) / len(original)
        assert ratio < 0.50, f"search_operations compression ratio {ratio:.2f} exceeds 0.50"

    @pytest.mark.asyncio
    async def test_reduce_data_ratio(self, reduce_data_output: ReduceDataOutput):
        result = await compress_observation(reduce_data_output, "reduce_data")
        original = str(reduce_data_output)
        ratio = len(result) / len(original)
        assert ratio < 0.70, f"reduce_data compression ratio {ratio:.2f} exceeds 0.70"
