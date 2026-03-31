# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for JSONFlux aggregation module (TASK-195 Phase 1 + Phase 3).

Tests cover:
- _infer_table_name(): deterministic heuristic (no mocks)
- generate_data_preview(): real QueryEngine (no mocks)
- _count_table_rows(): row counting from formatted output
- jsonflux_aggregate(): mock _generate_sql, real QueryEngine
- _generate_sql(): mock infer_structured
- Phase 3: GENERIC_NAMES constant, multi-table engine integration
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from meho_app.jsonflux import QueryEngine
from meho_app.modules.agents.base.jsonflux_aggregate import (
    GENERIC_NAMES,
    AggregationResult,
    _count_table_rows,
    _infer_table_name,
    _SQLGenerationResult,
    generate_data_preview,
    jsonflux_aggregate,
)

# Module path for patching
AGGREGATE_MODULE = "meho_app.modules.agents.base.jsonflux_aggregate"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def engine() -> QueryEngine:
    """Create a fresh QueryEngine for each test."""
    eng = QueryEngine()
    yield eng
    eng.close()


@pytest.fixture
def pods_data() -> list[dict]:
    """Sample K8s pod data."""
    return [
        {
            "metadata": {"name": "api-server-1", "namespace": "production"},
            "status": {"phase": "Running"},
            "kind": "Pod",
        },
        {
            "metadata": {"name": "api-server-2", "namespace": "production"},
            "status": {"phase": "CrashLoopBackOff"},
            "kind": "Pod",
        },
        {
            "metadata": {"name": "worker-1", "namespace": "staging"},
            "status": {"phase": "Running"},
            "kind": "Pod",
        },
    ]


@pytest.fixture
def namespaces_data() -> list[dict]:
    """Sample K8s namespace data."""
    return [
        {"metadata": {"name": "production"}, "status": {"phase": "Active"}},
        {"metadata": {"name": "staging"}, "status": {"phase": "Active"}},
        {"metadata": {"name": "kube-system"}, "status": {"phase": "Active"}},
    ]


# ============================================================================
# _infer_table_name() tests -- pure, no mocks
# ============================================================================


class TestInferTableName:
    """Tests for _infer_table_name heuristic."""

    def test_k8s_list_with_kind_field(self) -> None:
        """List of dicts with 'kind' field -> pluralized kind."""
        data = [{"kind": "Pod", "metadata": {"name": "pod-1"}}]
        assert _infer_table_name(data) == "pods"

    def test_k8s_kind_already_plural(self) -> None:
        """Kind already ending in 's' stays as-is."""
        data = [{"kind": "Endpoints", "metadata": {"name": "ep-1"}}]
        assert _infer_table_name(data) == "endpoints"

    def test_k8s_pluralization_simple(self) -> None:
        """Simple +s pluralization for kinds not ending in 's'."""
        data = [{"kind": "Deployment", "metadata": {"name": "dep-1"}}]
        assert _infer_table_name(data, fallback="items") == "deployments"

    def test_k8s_kind_ending_in_s_no_double(self) -> None:
        """Kind ending in 's' (like Address) is not double-pluralized."""
        data = [{"kind": "Address", "metadata": {"name": "addr-1"}}]
        assert _infer_table_name(data, fallback="items") == "address"

    def test_single_key_dict_with_list(self) -> None:
        """Dict with one key containing a list -> that key."""
        data = {"namespaces": [{"name": "ns1"}, {"name": "ns2"}]}
        assert _infer_table_name(data) == "namespaces"

    def test_single_key_dict_uppercase(self) -> None:
        """Single-key dict is lowercased."""
        data = {"Nodes": [{"name": "n1"}]}
        assert _infer_table_name(data) == "nodes"

    def test_k8s_list_wrapper_with_kind(self) -> None:
        """K8s list wrapper: {"kind": "PodList", "items": [...]} -> 'pods'."""
        data = {
            "kind": "PodList",
            "items": [{"kind": "Pod", "metadata": {"name": "p1"}}],
        }
        assert _infer_table_name(data) == "pods"

    def test_k8s_list_wrapper_item_kind_fallback(self) -> None:
        """K8s list wrapper without top-level kind -> use item's kind."""
        data = {
            "apiVersion": "v1",
            "items": [{"kind": "Deployment", "metadata": {"name": "d1"}}],
        }
        assert _infer_table_name(data) == "deployments"

    def test_empty_list_returns_fallback(self) -> None:
        """Empty list -> fallback."""
        assert _infer_table_name([], fallback="resources") == "resources"

    def test_empty_dict_returns_fallback(self) -> None:
        """Empty dict -> fallback."""
        assert _infer_table_name({}, fallback="data") == "data"

    def test_multi_key_dict_returns_fallback(self) -> None:
        """Dict with multiple keys and no 'items' -> fallback."""
        data = {"name": "test", "value": 42}
        assert _infer_table_name(data, fallback="misc") == "misc"

    def test_default_fallback(self) -> None:
        """Default fallback is 'data'."""
        assert _infer_table_name([]) == "data"

    def test_list_of_primitives_returns_fallback(self) -> None:
        """List of non-dicts -> fallback."""
        assert _infer_table_name([1, 2, 3], fallback="numbers") == "numbers"

    def test_k8s_list_wrapper_empty_items(self) -> None:
        """K8s list wrapper with empty items and kind -> use kind."""
        data = {"kind": "ServiceList", "items": []}
        assert _infer_table_name(data) == "services"


# ============================================================================
# generate_data_preview() tests -- real QueryEngine, no mocks
# ============================================================================


class TestGenerateDataPreview:
    """Tests for generate_data_preview using real QueryEngine."""

    def test_single_table_preview(self, engine: QueryEngine, pods_data: list) -> None:
        """Single table -> preview includes table name and row count."""
        engine.register("pods", pods_data)
        preview = generate_data_preview(engine)
        assert "pods" in preview
        assert "3" in preview  # row count

    def test_multiple_tables_preview(
        self,
        engine: QueryEngine,
        pods_data: list,
        namespaces_data: list,
    ) -> None:
        """Multiple tables -> preview includes both."""
        engine.register("pods", pods_data)
        engine.register("namespaces", namespaces_data)
        preview = generate_data_preview(engine)
        assert "pods" in preview
        assert "namespaces" in preview

    def test_empty_engine_preview(self, engine: QueryEngine) -> None:
        """No registered tables -> empty or minimal output."""
        preview = generate_data_preview(engine)
        # Should not crash; result is empty or minimal
        assert isinstance(preview, str)


# ============================================================================
# _count_table_rows() tests
# ============================================================================


class TestCountTableRows:
    """Tests for _count_table_rows helper."""

    def test_markdown_table(self) -> None:
        """Standard markdown table with 2 data rows."""
        table = "| name | phase |\n|------|-------|\n| pod-1 | Running |\n| pod-2 | Failed |\n"
        assert _count_table_rows(table) == 2

    def test_empty_string(self) -> None:
        """Empty string -> 0 rows."""
        assert _count_table_rows("") == 0

    def test_no_data_rows(self) -> None:
        """Header only (no data rows) -> 0 rows."""
        table = "| name | phase |\n|------|-------|\n"
        assert _count_table_rows(table) == 0

    def test_non_table_output(self) -> None:
        """Non-table output -> 0 rows."""
        assert _count_table_rows("Some plain text output") == 0


# ============================================================================
# jsonflux_aggregate() tests -- mock _generate_sql, real QueryEngine
# ============================================================================


class TestJsonfluxAggregate:
    """Tests for the main jsonflux_aggregate function."""

    @pytest.mark.asyncio
    async def test_success_first_attempt(self, engine: QueryEngine, pods_data: list) -> None:
        """LLM generates correct SQL on first try -> success."""
        engine.register("pods", pods_data)

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            new_callable=AsyncMock,
            return_value="SELECT metadata.name, status.phase FROM pods",
        ):
            result = await jsonflux_aggregate(engine, "Show all pod names and phases")

        assert result.success is True
        assert result.markdown != ""
        assert result.sql == "SELECT metadata.name, status.phase FROM pods"
        assert result.row_count == 3
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_retry_on_error(self, engine: QueryEngine, pods_data: list) -> None:
        """First SQL fails, second succeeds -> returns second attempt."""
        engine.register("pods", pods_data)

        mock_generate = AsyncMock(
            side_effect=[
                "SELECT nonexistent_col FROM pods",  # Will fail
                "SELECT metadata.name FROM pods",  # Will succeed
            ]
        )

        with patch(f"{AGGREGATE_MODULE}._generate_sql", mock_generate):
            result = await jsonflux_aggregate(engine, "Show pod names")

        assert result.success is True
        assert result.markdown != ""
        assert result.sql == "SELECT metadata.name FROM pods"
        assert mock_generate.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, engine: QueryEngine, pods_data: list) -> None:
        """All SQL attempts fail -> returns error."""
        engine.register("pods", pods_data)

        mock_generate = AsyncMock(return_value="SELECT bad_col FROM nonexistent_table")

        with patch(f"{AGGREGATE_MODULE}._generate_sql", mock_generate):
            result = await jsonflux_aggregate(engine, "Show something", max_retries=2)

        assert result.success is False
        assert result.error != ""
        assert "ERROR" in result.error
        assert mock_generate.call_count == 2

    @pytest.mark.asyncio
    async def test_multi_table(
        self,
        engine: QueryEngine,
        pods_data: list,
        namespaces_data: list,
    ) -> None:
        """Multiple registered tables -> SQL can reference any table."""
        engine.register("pods", pods_data)
        engine.register("namespaces", namespaces_data)

        sql = "SELECT metadata.name FROM namespaces"

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            new_callable=AsyncMock,
            return_value=sql,
        ):
            result = await jsonflux_aggregate(engine, "List all namespace names")

        assert result.success is True
        assert "production" in result.markdown
        assert "staging" in result.markdown
        assert result.row_count == 3

    @pytest.mark.asyncio
    async def test_nested_data(self, engine: QueryEngine) -> None:
        """Deeply nested JSON -> querying top-level fields works."""
        nested_data = [
            {"name": "srv-1", "config": {"port": 8080, "tls": {"enabled": True}}},
            {"name": "srv-2", "config": {"port": 9090, "tls": {"enabled": False}}},
        ]
        engine.register("services", nested_data)

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            new_callable=AsyncMock,
            return_value="SELECT name FROM services",
        ):
            result = await jsonflux_aggregate(engine, "List service names")

        assert result.success is True
        assert "srv-1" in result.markdown
        assert "srv-2" in result.markdown

    @pytest.mark.asyncio
    async def test_error_context_passed_on_retry(
        self, engine: QueryEngine, pods_data: list
    ) -> None:
        """On retry, _generate_sql receives the previous error."""
        engine.register("pods", pods_data)

        call_args_list: list[tuple] = []

        async def tracking_generate(
            system_prompt: str,
            nlq: str,
            last_error: str | None = None,
        ) -> str:
            call_args_list.append((system_prompt, nlq, last_error))
            # Always return bad SQL to force retries
            return "SELECT bad FROM nowhere"

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            side_effect=tracking_generate,
        ):
            result = await jsonflux_aggregate(engine, "test query", max_retries=2)

        assert result.success is False
        # First call: no error context
        assert call_args_list[0][2] is None
        # Second call: has error context from first failure
        assert call_args_list[1][2] is not None
        assert "ERROR" in call_args_list[1][2]

    @pytest.mark.asyncio
    async def test_format_parameter_passed(self, engine: QueryEngine, pods_data: list) -> None:
        """The format parameter is passed to engine.format_query."""
        engine.register("pods", pods_data)

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            new_callable=AsyncMock,
            return_value="SELECT metadata.name FROM pods",
        ):
            result = await jsonflux_aggregate(engine, "Show pod names", format="csv")

        assert result.success is True
        # CSV output should contain comma-separated values
        assert "," in result.markdown or "name" in result.markdown


# ============================================================================
# _generate_sql() tests -- mock infer_structured
# ============================================================================


class TestGenerateSQL:
    """Tests for _generate_sql private helper."""

    @pytest.mark.asyncio
    async def test_returns_sql_from_llm(self) -> None:
        """LLM returns SQL string via structured output."""
        from meho_app.modules.agents.base.jsonflux_aggregate import _generate_sql

        mock_result = _SQLGenerationResult(sql="SELECT * FROM pods")

        with patch(
            "meho_app.modules.agents.base.inference.infer_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_infer:
            sql = await _generate_sql(
                system_prompt="You are a SQL expert.",
                natural_language_query="Show all pods",
            )

        assert sql == "SELECT * FROM pods"
        mock_infer.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_instructions_to_infer(self) -> None:
        """System prompt is passed as 'instructions' to infer_structured."""
        from meho_app.modules.agents.base.jsonflux_aggregate import _generate_sql

        mock_result = _SQLGenerationResult(sql="SELECT 1")
        system_prompt = "You are a DuckDB expert. Tables: pods, namespaces."

        with patch(
            "meho_app.modules.agents.base.inference.infer_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_infer:
            await _generate_sql(
                system_prompt=system_prompt,
                natural_language_query="test",
            )

        call_kwargs = mock_infer.call_args.kwargs
        assert call_kwargs["instructions"] == system_prompt

    @pytest.mark.asyncio
    async def test_includes_error_context_on_retry(self) -> None:
        """When last_error is provided, it appears in the prompt."""
        from meho_app.modules.agents.base.jsonflux_aggregate import _generate_sql

        mock_result = _SQLGenerationResult(sql="SELECT 1")
        error_msg = "ERROR: column 'bad_col' not found"

        with patch(
            "meho_app.modules.agents.base.inference.infer_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_infer:
            await _generate_sql(
                system_prompt="SQL expert.",
                natural_language_query="Show pods",
                last_error=error_msg,
            )

        # The prompt should contain the error message
        call_args = mock_infer.call_args
        prompt = call_args.kwargs.get("prompt") or call_args.args[0]
        assert error_msg in prompt
        assert "fix" in prompt.lower()

    @pytest.mark.asyncio
    async def test_uses_temperature_zero(self) -> None:
        """SQL generation uses temperature=0.0 for deterministic output."""
        from meho_app.modules.agents.base.jsonflux_aggregate import _generate_sql

        mock_result = _SQLGenerationResult(sql="SELECT 1")

        with patch(
            "meho_app.modules.agents.base.inference.infer_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_infer:
            await _generate_sql(
                system_prompt="SQL expert.",
                natural_language_query="test",
            )

        call_kwargs = mock_infer.call_args.kwargs
        assert call_kwargs["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_uses_correct_response_model(self) -> None:
        """infer_structured is called with _SQLGenerationResult model."""
        from meho_app.modules.agents.base.jsonflux_aggregate import _generate_sql

        mock_result = _SQLGenerationResult(sql="SELECT 1")

        with patch(
            "meho_app.modules.agents.base.inference.infer_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_infer:
            await _generate_sql(
                system_prompt="SQL expert.",
                natural_language_query="test",
            )

        call_kwargs = mock_infer.call_args.kwargs
        assert call_kwargs.get("response_model") is _SQLGenerationResult


# ============================================================================
# AggregationResult tests
# ============================================================================


class TestAggregationResult:
    """Tests for AggregationResult dataclass."""

    def test_success_defaults(self) -> None:
        """Success result has correct defaults."""
        result = AggregationResult(success=True, markdown="| col |\n", sql="SELECT 1")
        assert result.success is True
        assert result.error == ""
        assert result.row_count == 0

    def test_failure_defaults(self) -> None:
        """Failure result has correct defaults."""
        result = AggregationResult(success=False, error="SQL error")
        assert result.success is False
        assert result.markdown == ""
        assert result.sql == ""


# ============================================================================
# Phase 3: GENERIC_NAMES and multi-table engine tests
# ============================================================================


class TestGenericNames:
    """Tests for the GENERIC_NAMES constant (TASK-195 Phase 3)."""

    def test_contains_expected_entries(self) -> None:
        """GENERIC_NAMES includes common generic table names."""
        expected = {"data", "items", "resources", "results", "records", "entries", "objects"}
        for name in expected:
            assert name in GENERIC_NAMES, f"'{name}' missing from GENERIC_NAMES"

    def test_is_frozenset(self) -> None:
        """GENERIC_NAMES is immutable (frozenset)."""
        assert isinstance(GENERIC_NAMES, frozenset)

    def test_does_not_contain_meaningful_names(self) -> None:
        """Meaningful table names like 'pods' are NOT in GENERIC_NAMES."""
        meaningful = ["pods", "nodes", "namespaces", "deployments", "services", "virtual_machines"]
        for name in meaningful:
            assert name not in GENERIC_NAMES, f"'{name}' should not be in GENERIC_NAMES"


class TestMultiTableEngine:
    """Tests for multi-table QueryEngine integration (TASK-195 Phase 3).

    These use a real QueryEngine (no mocks) to verify that multiple
    tables can be registered and queried together.
    """

    def test_multi_table_preview_includes_all(
        self,
        engine: QueryEngine,
        pods_data: list,
        namespaces_data: list,
    ) -> None:
        """Preview includes schema info for all registered tables."""
        engine.register("pods", pods_data)
        engine.register("namespaces", namespaces_data)
        preview = generate_data_preview(engine)

        # Both tables mentioned in preview
        assert "pods" in preview
        assert "namespaces" in preview
        # Row counts mentioned
        assert "3" in preview  # both have 3 rows

    def test_multi_table_prompt_includes_all_schemas(
        self,
        engine: QueryEngine,
        pods_data: list,
        namespaces_data: list,
    ) -> None:
        """generate_prompt() includes schemas for all registered tables."""
        engine.register("pods", pods_data)
        engine.register("namespaces", namespaces_data)
        prompt = engine.generate_prompt()

        # Both table names in the system prompt
        assert "pods" in prompt
        assert "namespaces" in prompt

    @pytest.mark.asyncio
    async def test_cross_table_query_executes(
        self,
        engine: QueryEngine,
    ) -> None:
        """A query referencing multiple tables can execute successfully."""
        # Register two related tables
        employees = [
            {"id": 1, "name": "Alice", "dept_id": 10},
            {"id": 2, "name": "Bob", "dept_id": 20},
        ]
        departments = [
            {"id": 10, "name": "Engineering"},
            {"id": 20, "name": "Marketing"},
        ]
        engine.register("employees", employees)
        engine.register("departments", departments)

        # Cross-table JOIN SQL
        sql = (
            "SELECT e.name AS employee, d.name AS department "
            "FROM employees e JOIN departments d ON e.dept_id = d.id"
        )

        with patch(
            f"{AGGREGATE_MODULE}._generate_sql",
            new_callable=AsyncMock,
            return_value=sql,
        ):
            result = await jsonflux_aggregate(engine, "Show employees with their departments")

        assert result.success is True
        assert "Alice" in result.markdown
        assert "Engineering" in result.markdown
        assert "Bob" in result.markdown
        assert "Marketing" in result.markdown
        assert result.row_count == 2

    def test_three_tables_registered(self, engine: QueryEngine) -> None:
        """Three tables can be registered and appear in the prompt."""
        engine.register("pods", [{"name": "p1"}])
        engine.register("nodes", [{"name": "n1"}])
        engine.register("services", [{"name": "s1"}])

        prompt = engine.generate_prompt()
        assert "pods" in prompt
        assert "nodes" in prompt
        assert "services" in prompt

    def test_generic_name_resolved_before_registration(self, engine: QueryEngine) -> None:
        """Demonstrate that _infer_table_name fixes generic names."""
        # Data that would be named "items" generically
        k8s_data = [{"kind": "Node", "metadata": {"name": "node-1"}}]

        # If name is generic, infer a better one
        name = "items"
        if name in GENERIC_NAMES:
            name = _infer_table_name(k8s_data, fallback=name)

        engine.register(name, k8s_data)
        prompt = engine.generate_prompt()

        # Should use "nodes" not "items"
        assert "nodes" in prompt
