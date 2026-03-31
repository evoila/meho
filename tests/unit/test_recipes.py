# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for the Recipe System.

Tests the capture, storage, and execution of reusable Q&A recipes.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from meho_app.modules.agents.data_reduction.query_schema import (
    ComputeField,
    DataQuery,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    SortSpec,
)
from meho_app.modules.agents.recipes import (
    Recipe,
    RecipeCaptureService,
    RecipeExecution,
    RecipeExecutionStatus,
    RecipeExecutor,
    RecipeParameter,
    RecipeParameterType,
    RecipeQueryTemplate,
)

# =============================================================================
# Model Tests
# =============================================================================


class TestRecipeParameter:
    """Tests for RecipeParameter model."""

    def test_string_parameter(self):
        """Test creating a string parameter."""
        param = RecipeParameter(
            name="region",
            display_name="Region",
            description="The region to filter by",
            param_type=RecipeParameterType.STRING,
            default_value="us-east",
        )

        assert param.name == "region"
        assert param.param_type == RecipeParameterType.STRING
        assert param.default_value == "us-east"

    def test_number_parameter_with_range(self):
        """Test creating a number parameter with min/max."""
        param = RecipeParameter(
            name="threshold",
            display_name="Memory Threshold",
            param_type=RecipeParameterType.NUMBER,
            default_value=80.0,
            min_value=0.0,
            max_value=100.0,
        )

        assert param.min_value == 0.0
        assert param.max_value == 100.0

    def test_enum_parameter(self):
        """Test creating an enum parameter."""
        param = RecipeParameter(
            name="status",
            display_name="Status",
            param_type=RecipeParameterType.ENUM,
            default_value="healthy",
            allowed_values=["healthy", "warning", "critical"],
        )

        assert "critical" in param.allowed_values


class TestRecipeQueryTemplate:
    """Tests for RecipeQueryTemplate."""

    def test_render_simple_template(self):
        """Test rendering a simple template."""
        template = RecipeQueryTemplate(
            source_path="clusters",
            select=["name", "status"],
            filter_template={
                "conditions": [{"field": "status", "operator": "=", "value": "{{status}}"}],
                "logic": "and",
            },
            limit=20,
        )

        query = template.render({"status": "critical"})

        assert query.source_path == "clusters"
        assert query.filter is not None
        assert query.filter.conditions[0].value == "critical"
        assert query.limit == 20

    def test_render_with_multiple_parameters(self):
        """Test rendering with multiple parameters."""
        template = RecipeQueryTemplate(
            source_path="clusters",
            filter_template={
                "conditions": [
                    {"field": "region", "operator": "=", "value": "{{region}}"},
                    {"field": "memory_pct", "operator": ">", "value": "{{threshold}}"},
                ],
                "logic": "and",
            },
        )

        query = template.render(
            {
                "region": "eu-west",
                "threshold": 70,
            }
        )

        assert len(query.filter.conditions) == 2
        assert query.filter.conditions[0].value == "eu-west"
        assert query.filter.conditions[1].value == 70

    def test_render_preserves_static_values(self):
        """Test that static values are preserved."""
        template = RecipeQueryTemplate(
            source_path="clusters",
            filter_template={
                "conditions": [
                    {"field": "region", "operator": "=", "value": "{{region}}"},
                    {"field": "enabled", "operator": "=", "value": True},  # Static
                ],
                "logic": "and",
            },
        )

        query = template.render({"region": "us-east"})

        # Static value should be preserved
        assert query.filter.conditions[1].value is True


class TestRecipe:
    """Tests for Recipe model."""

    def test_create_recipe(self):
        """Test creating a complete recipe."""
        recipe = Recipe(
            tenant_id="test-tenant",
            name="High Memory Clusters",
            description="Find clusters with high memory usage",
            connector_id=uuid4(),
            original_question="Show clusters with memory > 80%",
            parameters=[
                RecipeParameter(
                    name="threshold",
                    display_name="Memory Threshold",
                    param_type=RecipeParameterType.NUMBER,
                    default_value=80,
                )
            ],
            query_template=RecipeQueryTemplate(
                source_path="clusters",
                filter_template={
                    "conditions": [
                        {"field": "memory_pct", "operator": ">", "value": "{{threshold}}"}
                    ],
                    "logic": "and",
                },
            ),
        )

        assert recipe.name == "High Memory Clusters"
        assert len(recipe.parameters) == 1
        assert recipe.execution_count == 0


# =============================================================================
# Capture Service Tests
# =============================================================================


class TestRecipeCaptureService:
    """Tests for RecipeCaptureService."""

    @pytest.fixture
    def capture_service(self):
        """Create a capture service."""
        return RecipeCaptureService()

    @pytest.mark.asyncio
    async def test_capture_simple_query(self, capture_service):
        """Test capturing a simple filter query."""
        question = "Show me critical clusters"
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="critical")
                ]
            ),
        )

        recipe = await capture_service.capture(
            question=question,
            query=query,
            connector_id=uuid4(),
            tenant_id="test-tenant",
        )

        assert recipe.original_question == question
        assert len(recipe.parameters) == 1
        assert recipe.parameters[0].name == "status"
        assert recipe.parameters[0].default_value == "critical"

    @pytest.mark.asyncio
    async def test_capture_with_threshold(self, capture_service):
        """Test capturing a query with numeric threshold."""
        question = "Show clusters with memory > 80%"
        query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="memory_pct", expression="memory_used / memory_total * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=80)
                ]
            ),
        )

        recipe = await capture_service.capture(
            question=question,
            query=query,
            connector_id=uuid4(),
            tenant_id="test-tenant",
        )

        # Should extract the threshold as a parameter
        threshold_param = next((p for p in recipe.parameters if "memory_pct" in p.name), None)
        assert threshold_param is not None
        assert threshold_param.default_value == 80
        assert threshold_param.param_type == RecipeParameterType.INTEGER

    @pytest.mark.asyncio
    async def test_capture_with_multiple_conditions(self, capture_service):
        """Test capturing a query with multiple filter conditions."""
        question = "Show critical clusters in us-east"
        query = DataQuery(
            source_path="clusters",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="status", operator=FilterOperator.EQ, value="critical"),
                    FilterCondition(field="region", operator=FilterOperator.EQ, value="us-east"),
                ],
                logic="and",
            ),
        )

        recipe = await capture_service.capture(
            question=question,
            query=query,
            connector_id=uuid4(),
            tenant_id="test-tenant",
        )

        # Should have parameters for both conditions
        assert len(recipe.parameters) == 2
        param_names = {p.name for p in recipe.parameters}
        assert "status" in param_names
        assert "region" in param_names

    @pytest.mark.asyncio
    async def test_capture_generates_name(self, capture_service):
        """Test that capture generates a reasonable name."""
        question = "Show me all failed pods in the default namespace"
        query = DataQuery(
            source_path="pods",
            filter=FilterGroup(
                conditions=[
                    FilterCondition(
                        field="status.phase", operator=FilterOperator.EQ, value="Failed"
                    )
                ]
            ),
        )

        recipe = await capture_service.capture(
            question=question,
            query=query,
            connector_id=uuid4(),
            tenant_id="test-tenant",
        )

        # Name should be derived from question
        assert recipe.name
        assert len(recipe.name) <= 50


# =============================================================================
# Executor Tests
# =============================================================================


class TestRecipeExecutor:
    """Tests for RecipeExecutor."""

    @pytest.fixture
    def executor(self):
        """Create an executor."""
        return RecipeExecutor()

    @pytest.fixture
    def sample_recipe(self):
        """Create a sample recipe for testing."""
        return Recipe(
            tenant_id="test-tenant",
            name="High Memory Clusters",
            connector_id=uuid4(),
            original_question="Show clusters with memory > 80%",
            parameters=[
                RecipeParameter(
                    name="threshold",
                    display_name="Memory Threshold",
                    param_type=RecipeParameterType.NUMBER,
                    default_value=80,
                    min_value=0,
                    max_value=100,
                ),
                RecipeParameter(
                    name="limit",
                    display_name="Result Limit",
                    param_type=RecipeParameterType.INTEGER,
                    default_value=20,
                    required=False,
                ),
            ],
            query_template=RecipeQueryTemplate(
                source_path="clusters",
                compute_expressions=[
                    {"name": "memory_pct", "expression": "memory_used_gb / memory_total_gb * 100"}
                ],
                filter_template={
                    "conditions": [
                        {"field": "memory_pct", "operator": ">", "value": "{{threshold}}"}
                    ],
                    "logic": "and",
                },
                sort_field="memory_pct",
                sort_direction="desc",
                limit=20,
                aggregates=[{"name": "avg_memory", "function": "avg", "field": "memory_pct"}],
            ),
        )

    @pytest.fixture
    def sample_data(self):
        """Sample cluster data."""
        return {
            "clusters": [
                {"name": "cluster-1", "memory_total_gb": 512, "memory_used_gb": 450},  # 87.9%
                {"name": "cluster-2", "memory_total_gb": 256, "memory_used_gb": 200},  # 78.1%
                {"name": "cluster-3", "memory_total_gb": 512, "memory_used_gb": 480},  # 93.7%
                {"name": "cluster-4", "memory_total_gb": 128, "memory_used_gb": 100},  # 78.1%
                {"name": "cluster-5", "memory_total_gb": 256, "memory_used_gb": 230},  # 89.8%
            ]
        }

    @pytest.mark.asyncio
    async def test_execute_with_default_params(self, executor, sample_recipe, sample_data):
        """Test executing recipe with default parameters."""
        execution = await executor.execute(
            recipe=sample_recipe,
            parameter_values={},  # Use defaults
            api_response=sample_data,
        )

        assert execution.status == RecipeExecutionStatus.COMPLETED
        assert execution.result_count is not None
        assert execution.result_count > 0  # Should find some high-memory clusters

    @pytest.mark.asyncio
    async def test_execute_with_custom_params(self, executor, sample_recipe, sample_data):
        """Test executing recipe with custom parameters."""
        execution = await executor.execute(
            recipe=sample_recipe,
            parameter_values={"threshold": 85},  # Higher threshold
            api_response=sample_data,
        )

        assert execution.status == RecipeExecutionStatus.COMPLETED
        # With 85% threshold, should find fewer clusters
        assert execution.result_count <= 3

    @pytest.mark.asyncio
    async def test_execute_validates_range(self, executor, sample_recipe, sample_data):
        """Test that executor validates parameter ranges."""
        execution = await executor.execute(
            recipe=sample_recipe,
            parameter_values={"threshold": 150},  # Out of range
            api_response=sample_data,
        )

        assert execution.status == RecipeExecutionStatus.FAILED
        assert "at most" in execution.error_message.lower()

    @pytest.mark.asyncio
    async def test_execute_tracks_duration(self, executor, sample_recipe, sample_data):
        """Test that execution tracks duration."""
        execution = await executor.execute(
            recipe=sample_recipe,
            parameter_values={},
            api_response=sample_data,
        )

        assert execution.duration_ms is not None
        assert execution.duration_ms >= 0

    def test_preview_query(self, executor, sample_recipe):
        """Test previewing the query without execution."""
        query = executor.preview(
            recipe=sample_recipe,
            parameter_values={"threshold": 75},
        )

        assert isinstance(query, DataQuery)
        assert query.source_path == "clusters"
        # Check the filter has the right value
        assert query.filter.conditions[0].value == 75


class TestRecipeExecution:
    """Tests for RecipeExecution model."""

    def test_execution_default_status(self):
        """Test default execution status."""
        execution = RecipeExecution(
            recipe_id=uuid4(),
            tenant_id="test",
        )

        assert execution.status == RecipeExecutionStatus.PENDING

    def test_execution_with_results(self):
        """Test execution with result data."""
        execution = RecipeExecution(
            recipe_id=uuid4(),
            tenant_id="test",
            status=RecipeExecutionStatus.COMPLETED,
            result_count=15,
            aggregates={"avg_memory": 85.5, "count": 15},
        )

        assert execution.result_count == 15
        assert "avg_memory" in execution.aggregates


# =============================================================================
# Integration Tests
# =============================================================================


class TestRecipeWorkflow:
    """Integration tests for the complete recipe workflow."""

    @pytest.mark.asyncio
    async def test_capture_and_execute_workflow(self):
        """Test the complete workflow: capture → execute."""
        capture_service = RecipeCaptureService()
        executor = RecipeExecutor()

        # Step 1: Original Q&A interaction
        original_question = "Show clusters with memory usage over 80%"
        original_query = DataQuery(
            source_path="clusters",
            compute=[
                ComputeField(name="memory_pct", expression="memory_used_gb / memory_total_gb * 100")
            ],
            filter=FilterGroup(
                conditions=[
                    FilterCondition(field="memory_pct", operator=FilterOperator.GT, value=80)
                ]
            ),
            sort=SortSpec(field="memory_pct", direction="desc"),
            limit=20,
        )

        # Step 2: Capture as recipe
        recipe = await capture_service.capture(
            question=original_question,
            query=original_query,
            connector_id=uuid4(),
            tenant_id="test-tenant",
            name="High Memory Clusters",
        )

        assert recipe.name == "High Memory Clusters"
        assert len(recipe.parameters) >= 1

        # Step 3: Execute with different parameter
        sample_data = {
            "clusters": [
                {"name": f"cluster-{i}", "memory_total_gb": 512, "memory_used_gb": 400 + i * 20}
                for i in range(10)
            ]
        }

        # Run with lower threshold
        execution = await executor.execute(
            recipe=recipe,
            parameter_values={"memory_pct": 90},  # Higher threshold
            api_response=sample_data,
        )

        assert execution.status == RecipeExecutionStatus.COMPLETED
        assert execution.result_count is not None


# =============================================================================
# Repository Tests
# =============================================================================


@pytest.mark.skip(reason="RecipeRepository requires database session - move to integration tests")
class TestRecipeRepository:
    """Tests for RecipeRepository."""

    @pytest.fixture
    def repository(self, mock_session):
        """Create a fresh repository with mock session."""
        from meho_app.modules.agents.recipes.repository import RecipeRepository

        return RecipeRepository(mock_session)

    @pytest.fixture
    def sample_recipe(self):
        """Create a sample recipe."""
        return Recipe(
            tenant_id="test-tenant",
            name="Test Recipe",
            connector_id=uuid4(),
            original_question="Test question",
            parameters=[],
            query_template=RecipeQueryTemplate(
                source_path="items",
            ),
        )

    @pytest.mark.asyncio
    async def test_create_recipe(self, repository, sample_recipe):
        """Test creating a recipe."""
        created = await repository.create_recipe(sample_recipe)

        assert created.id == sample_recipe.id
        assert created.name == "Test Recipe"

    @pytest.mark.asyncio
    async def test_get_recipe(self, repository, sample_recipe):
        """Test getting a recipe by ID."""
        await repository.create_recipe(sample_recipe)

        retrieved = await repository.get_recipe(sample_recipe.id)

        assert retrieved is not None
        assert retrieved.id == sample_recipe.id

    @pytest.mark.asyncio
    async def test_get_recipe_not_found(self, repository):
        """Test getting a non-existent recipe."""
        retrieved = await repository.get_recipe(uuid4())

        assert retrieved is None

    @pytest.mark.asyncio
    async def test_get_recipe_by_name(self, repository, sample_recipe):
        """Test getting a recipe by name."""
        await repository.create_recipe(sample_recipe)

        retrieved = await repository.get_recipe_by_name("Test Recipe", "test-tenant")

        assert retrieved is not None
        assert retrieved.name == "Test Recipe"

    @pytest.mark.asyncio
    async def test_list_recipes_filters_by_tenant(self, repository):
        """Test that list respects tenant filter."""
        for tenant in ["tenant-a", "tenant-b"]:
            for i in range(3):
                recipe = Recipe(
                    tenant_id=tenant,
                    name=f"Recipe {i}",
                    connector_id=uuid4(),
                    original_question=f"Question {i}",
                    query_template=RecipeQueryTemplate(source_path="items"),
                )
                await repository.create_recipe(recipe)

        tenant_a_recipes = await repository.list_recipes("tenant-a")

        assert len(tenant_a_recipes) == 3

    @pytest.mark.asyncio
    async def test_list_recipes_filters_by_tags(self, repository):
        """Test filtering by tags."""
        recipe1 = Recipe(
            tenant_id="test-tenant",
            name="Recipe 1",
            connector_id=uuid4(),
            original_question="Q1",
            tags=["kubernetes", "pods"],
            query_template=RecipeQueryTemplate(source_path="pods"),
        )
        recipe2 = Recipe(
            tenant_id="test-tenant",
            name="Recipe 2",
            connector_id=uuid4(),
            original_question="Q2",
            tags=["vcenter", "vms"],
            query_template=RecipeQueryTemplate(source_path="vms"),
        )
        await repository.create_recipe(recipe1)
        await repository.create_recipe(recipe2)

        k8s_recipes = await repository.list_recipes("test-tenant", tags=["kubernetes"])

        assert len(k8s_recipes) == 1

    @pytest.mark.asyncio
    async def test_update_recipe(self, repository, sample_recipe):
        """Test updating a recipe."""
        await repository.create_recipe(sample_recipe)

        sample_recipe.name = "Updated Name"
        updated = await repository.update_recipe(sample_recipe)

        assert updated.name == "Updated Name"

    @pytest.mark.asyncio
    async def test_delete_recipe(self, repository, sample_recipe):
        """Test deleting a recipe."""
        await repository.create_recipe(sample_recipe)

        deleted = await repository.delete_recipe(sample_recipe.id)

        assert deleted is True
        assert await repository.get_recipe(sample_recipe.id) is None

    @pytest.mark.asyncio
    async def test_increment_execution_count(self, repository, sample_recipe):
        """Test incrementing execution count."""
        await repository.create_recipe(sample_recipe)

        await repository.increment_execution_count(sample_recipe.id)
        await repository.increment_execution_count(sample_recipe.id)

        recipe = await repository.get_recipe(sample_recipe.id)
        assert recipe.execution_count == 2

    @pytest.mark.asyncio
    async def test_search_recipes(self, repository):
        """Test searching recipes."""
        recipe1 = Recipe(
            tenant_id="test-tenant",
            name="Kubernetes Pods",
            connector_id=uuid4(),
            original_question="Show all failed pods",
            query_template=RecipeQueryTemplate(source_path="pods"),
        )
        await repository.create_recipe(recipe1)

        results = await repository.search_recipes("test-tenant", "pods")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_popular_recipes(self, repository):
        """Test getting popular recipes."""
        for i in range(5):
            recipe = Recipe(
                tenant_id="test-tenant",
                name=f"Recipe {i}",
                connector_id=uuid4(),
                original_question=f"Question {i}",
                execution_count=i * 10,
                query_template=RecipeQueryTemplate(source_path="items"),
            )
            await repository.create_recipe(recipe)

        popular = await repository.get_popular_recipes("test-tenant", limit=3)

        assert len(popular) == 3
        assert popular[0].execution_count >= popular[1].execution_count

    @pytest.mark.asyncio
    async def test_get_recent_recipes(self, repository):
        """Test getting recent recipes."""
        now = datetime.now(tz=UTC)
        for i in range(3):
            recipe = Recipe(
                tenant_id="test-tenant",
                name=f"Recipe {i}",
                connector_id=uuid4(),
                original_question=f"Question {i}",
                last_executed_at=now - timedelta(hours=i),
                query_template=RecipeQueryTemplate(source_path="items"),
            )
            await repository.create_recipe(recipe)

        recent = await repository.get_recent_recipes("test-tenant", limit=2)

        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_execution_crud(self, repository, sample_recipe):
        """Test execution CRUD operations."""
        await repository.create_recipe(sample_recipe)

        # Create
        execution = RecipeExecution(
            recipe_id=sample_recipe.id,
            tenant_id="test-tenant",
            started_at=datetime.now(tz=UTC),
        )
        await repository.create_execution(execution)

        # Get
        retrieved = await repository.get_execution(execution.id)
        assert retrieved is not None

        # List
        executions = await repository.list_executions(sample_recipe.id)
        assert len(executions) == 1

        # Update
        execution.status = RecipeExecutionStatus.COMPLETED
        await repository.update_execution(execution)
        updated = await repository.get_execution(execution.id)
        assert updated.status == RecipeExecutionStatus.COMPLETED


# =============================================================================
# Executor Edge Case Tests
# =============================================================================


class TestRecipeExecutorEdgeCases:
    """Additional edge case tests for RecipeExecutor."""

    @pytest.fixture
    def executor(self):
        return RecipeExecutor()

    def test_validate_type_number_invalid(self, executor):
        """Test number type validation with invalid value."""
        from meho_app.modules.agents.recipes.executor import RecipeExecutionError

        with pytest.raises(RecipeExecutionError):
            executor._validate_type("number", "not-a-number", "Test")

    def test_validate_type_integer_invalid(self, executor):
        """Test integer type validation with invalid value."""
        from meho_app.modules.agents.recipes.executor import RecipeExecutionError

        with pytest.raises(RecipeExecutionError):
            executor._validate_type("integer", "42.5", "Test")

    def test_validate_type_boolean_variations(self, executor):
        """Test boolean validation with various inputs."""
        assert executor._validate_type("boolean", "true", "t") is True
        assert executor._validate_type("boolean", "yes", "t") is True
        assert executor._validate_type("boolean", "1", "t") is True
        assert executor._validate_type("boolean", "no", "t") is False

    def test_validate_type_array(self, executor):
        """Test array type validation."""
        assert executor._validate_type("array", "single", "t") == ["single"]
        assert executor._validate_type("array", ["a", "b"], "t") == ["a", "b"]

    @pytest.mark.asyncio
    async def test_execute_enum_validation_fails(self, executor):
        """Test execution fails with invalid enum value."""
        recipe = Recipe(
            tenant_id="test-tenant",
            name="Test",
            connector_id=uuid4(),
            original_question="Test",
            parameters=[
                RecipeParameter(
                    name="status",
                    display_name="Status",
                    param_type=RecipeParameterType.ENUM,
                    allowed_values=["active", "inactive"],
                )
            ],
            query_template=RecipeQueryTemplate(
                source_path="items",
                filter_template={
                    "conditions": [{"field": "status", "operator": "=", "value": "{{status}}"}],
                    "logic": "and",
                },
            ),
        )

        execution = await executor.execute(
            recipe=recipe,
            parameter_values={"status": "invalid"},
            api_response={"items": []},
        )

        assert execution.status == RecipeExecutionStatus.FAILED


# =============================================================================
# Scheduler Tests
# =============================================================================


class TestRecipeScheduler:
    """Tests for RecipeScheduler."""

    @pytest.fixture
    def scheduler(self):
        from meho_app.modules.agents.recipes.executor import RecipeScheduler

        return RecipeScheduler(RecipeExecutor())

    @pytest.mark.asyncio
    async def test_schedule_recipe(self, scheduler):
        """Test scheduling a recipe."""
        recipe_id = uuid4()

        schedule = await scheduler.schedule(recipe_id, "0 * * * *", {"threshold": 80})

        assert schedule["recipe_id"] == recipe_id
        assert schedule["enabled"] is True

    @pytest.mark.asyncio
    async def test_unschedule_recipe(self, scheduler):
        """Test unscheduling a recipe."""
        recipe_id = uuid4()
        await scheduler.schedule(recipe_id, "0 * * * *", {})

        result = await scheduler.unschedule(recipe_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_list_schedules(self, scheduler):
        """Test listing schedules."""
        for i in range(3):
            await scheduler.schedule(uuid4(), f"0 {i} * * *", {})

        schedules = await scheduler.list_schedules()

        assert len(schedules) == 3
