"""
Recipe API Routes.

REST API for managing and executing recipes -
reusable Q&A patterns that can be saved and replayed.

Session 80: Added database persistence via SQLAlchemy.
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Header, Body
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_agent.recipes import (
    Recipe,
    RecipeParameter,
    RecipeParameterType,
    RecipeExecution,
    RecipeExecutionStatus,
    RecipeQueryTemplate,
    RecipeCaptureService,
    RecipeExecutor,
)
from meho_agent.recipes.repository import RecipeRepository
from meho_agent.data_reduction import DataQuery
from meho_api.database import get_agent_session
from meho_core.auth_context import UserContext
from meho_api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])

# =============================================================================
# Dependency injection
# =============================================================================

async def get_repository(
    session: AsyncSession = Depends(get_agent_session)
) -> RecipeRepository:
    """Get a RecipeRepository instance with database session."""
    return RecipeRepository(session)


def get_capture_service() -> RecipeCaptureService:
    """Get the capture service singleton."""
    return RecipeCaptureService()


def get_executor() -> RecipeExecutor:
    """Get the executor singleton."""
    return RecipeExecutor()


# =============================================================================
# Request/Response Models
# =============================================================================

class RecipeParameterSchema(BaseModel):
    """Schema for recipe parameter."""
    
    name: str
    display_name: str
    description: Optional[str] = None
    param_type: str
    default_value: Optional[Any] = None
    required: bool = True
    allowed_values: Optional[list[Any]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None


class RecipeCreateRequest(BaseModel):
    """Request to create a recipe from a Q&A interaction."""
    
    name: str = Field(description="Recipe name")
    description: Optional[str] = Field(default=None, description="Recipe description")
    connector_id: UUID = Field(description="Connector used")
    endpoint_id: Optional[UUID] = Field(default=None, description="Specific endpoint")
    original_question: str = Field(description="The original question")
    
    # The query that was generated
    query: dict[str, Any] = Field(description="The DataQuery as dict")
    
    # Optional: manually specify parameters
    parameters: Optional[list[RecipeParameterSchema]] = None
    
    tags: list[str] = Field(default_factory=list)


class RecipeUpdateRequest(BaseModel):
    """Request to update a recipe."""
    
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    is_public: Optional[bool] = None


class RecipeExecuteRequest(BaseModel):
    """Request to execute a recipe."""
    
    parameter_values: dict[str, Any] = Field(
        default_factory=dict,
        description="Values for recipe parameters"
    )
    api_response: dict[str, Any] = Field(
        description="API response data to process"
    )


class RecipeResponse(BaseModel):
    """Response containing a recipe."""
    
    id: UUID
    name: str
    description: Optional[str]
    connector_id: UUID
    endpoint_id: Optional[UUID]
    original_question: str
    parameters: list[RecipeParameterSchema]
    tags: list[str]
    execution_count: int
    last_executed_at: Optional[str]
    is_public: bool
    created_at: str
    
    @classmethod
    def from_recipe(cls, recipe: Recipe) -> "RecipeResponse":
        """Convert Recipe to response."""
        return cls(
            id=recipe.id,
            name=recipe.name,
            description=recipe.description,
            connector_id=recipe.connector_id,
            endpoint_id=recipe.endpoint_id,
            original_question=recipe.original_question,
            parameters=[
                RecipeParameterSchema(
                    name=p.name,
                    display_name=p.display_name,
                    description=p.description,
                    param_type=p.param_type,
                    default_value=p.default_value,
                    required=p.required,
                    allowed_values=p.allowed_values,
                    min_value=p.min_value,
                    max_value=p.max_value,
                )
                for p in recipe.parameters
            ],
            tags=recipe.tags,
            execution_count=recipe.execution_count,
            last_executed_at=recipe.last_executed_at.isoformat() if recipe.last_executed_at else None,
            is_public=recipe.is_public,
            created_at=recipe.created_at.isoformat(),
        )


class RecipeListResponse(BaseModel):
    """Response containing list of recipes."""
    
    recipes: list[RecipeResponse]
    total: int


class ExecutionResponse(BaseModel):
    """Response from recipe execution."""
    
    id: UUID
    recipe_id: UUID
    status: str
    error_message: Optional[str]
    result_count: Optional[int]
    result_summary: Optional[str]
    aggregates: dict[str, Any]
    duration_ms: Optional[float]
    
    @classmethod
    def from_execution(cls, execution: RecipeExecution) -> "ExecutionResponse":
        """Convert RecipeExecution to response."""
        return cls(
            id=execution.id,
            recipe_id=execution.recipe_id,
            status=execution.status,
            error_message=execution.error_message,
            result_count=execution.result_count,
            result_summary=execution.result_summary,
            aggregates=execution.aggregates,
            duration_ms=execution.duration_ms,
        )


# =============================================================================
# Recipe CRUD Endpoints
# =============================================================================

@router.post("", response_model=RecipeResponse)
async def create_recipe(
    request: RecipeCreateRequest,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> RecipeResponse:
    """
    Create a new recipe from a Q&A interaction.
    
    This captures a successful question/query pair and turns it
    into a reusable recipe with extracted parameters.
    """
    capture = get_capture_service()
    
    try:
        # Convert query dict to DataQuery
        query = DataQuery(**request.query)
        
        # Capture as recipe
        recipe = await capture.capture(
            question=request.original_question,
            query=query,
            connector_id=request.connector_id,
            tenant_id=user.tenant_id,
            endpoint_id=request.endpoint_id,
            name=request.name,
            description=request.description,
        )
        
        # Override parameters if specified
        if request.parameters:
            recipe.parameters = [
                RecipeParameter(
                    name=p.name,
                    display_name=p.display_name,
                    description=p.description,
                    param_type=RecipeParameterType(p.param_type),
                    default_value=p.default_value,
                    required=p.required,
                    allowed_values=p.allowed_values,
                    min_value=p.min_value,
                    max_value=p.max_value,
                )
                for p in request.parameters
            ]
        
        # Add tags
        recipe.tags.extend(request.tags)
        
        # Save
        recipe = await repo.create_recipe(recipe)
        await session.commit()
        
        return RecipeResponse.from_recipe(recipe)
        
    except Exception as e:
        await session.rollback()
        logger.exception(f"Failed to create recipe: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=RecipeListResponse)
async def list_recipes(
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    connector_id: Optional[UUID] = Query(None),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> RecipeListResponse:
    """List recipes for the tenant."""
    tag_list = tags.split(",") if tags else None
    
    recipes = await repo.list_recipes(
        tenant_id=user.tenant_id,
        connector_id=connector_id,
        tags=tag_list,
        limit=limit,
        offset=offset,
    )
    
    return RecipeListResponse(
        recipes=[RecipeResponse.from_recipe(r) for r in recipes],
        total=len(recipes),
    )


@router.get("/search", response_model=RecipeListResponse)
async def search_recipes(
    q: str = Query(..., description="Search query"),
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    limit: int = Query(20, ge=1, le=100),
) -> RecipeListResponse:
    """Search recipes by name, description, and original question."""
    recipes = await repo.search_recipes(
        tenant_id=user.tenant_id,
        query=q,
        limit=limit,
    )
    
    return RecipeListResponse(
        recipes=[RecipeResponse.from_recipe(r) for r in recipes],
        total=len(recipes),
    )


@router.get("/popular", response_model=RecipeListResponse)
async def get_popular_recipes(
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    limit: int = Query(10, ge=1, le=50),
) -> RecipeListResponse:
    """Get most frequently executed recipes."""
    recipes = await repo.get_popular_recipes(
        tenant_id=user.tenant_id,
        limit=limit,
    )
    
    return RecipeListResponse(
        recipes=[RecipeResponse.from_recipe(r) for r in recipes],
        total=len(recipes),
    )


@router.get("/recent", response_model=RecipeListResponse)
async def get_recent_recipes(
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    limit: int = Query(10, ge=1, le=50),
) -> RecipeListResponse:
    """Get most recently executed recipes."""
    recipes = await repo.get_recent_recipes(
        tenant_id=user.tenant_id,
        limit=limit,
    )
    
    return RecipeListResponse(
        recipes=[RecipeResponse.from_recipe(r) for r in recipes],
        total=len(recipes),
    )


@router.get("/{recipe_id}", response_model=RecipeResponse)
async def get_recipe(
    recipe_id: UUID,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
) -> RecipeResponse:
    """Get a recipe by ID."""
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id and not recipe.is_public:
        raise HTTPException(status_code=403, detail="Access denied")
    
    return RecipeResponse.from_recipe(recipe)


@router.patch("/{recipe_id}", response_model=RecipeResponse)
async def update_recipe(
    recipe_id: UUID,
    request: RecipeUpdateRequest,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> RecipeResponse:
    """Update a recipe."""
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update fields
    if request.name is not None:
        recipe.name = request.name
    if request.description is not None:
        recipe.description = request.description
    if request.tags is not None:
        recipe.tags = request.tags
    if request.is_public is not None:
        recipe.is_public = request.is_public
    
    recipe = await repo.update_recipe(recipe)
    await session.commit()
    
    return RecipeResponse.from_recipe(recipe)


@router.delete("/{recipe_id}")
async def delete_recipe(
    recipe_id: UUID,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> dict:
    """Delete a recipe."""
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    await repo.delete_recipe(recipe_id)
    await session.commit()
    
    return {"status": "deleted", "id": str(recipe_id)}


# =============================================================================
# Recipe Execution Endpoints
# =============================================================================

@router.post("/{recipe_id}/execute", response_model=ExecutionResponse)
async def execute_recipe(
    recipe_id: UUID,
    request: RecipeExecuteRequest,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    session: AsyncSession = Depends(get_agent_session),
) -> ExecutionResponse:
    """
    Execute a recipe with the given parameters.
    
    The API response data must be provided - this endpoint
    processes the data according to the recipe's query template.
    """
    executor = get_executor()
    
    # Get the recipe
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id and not recipe.is_public:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Execute
    execution = await executor.execute(
        recipe=recipe,
        parameter_values=request.parameter_values,
        api_response=request.api_response,
        triggered_by=user.user_id,
    )
    
    # Save execution
    await repo.create_execution(execution)
    
    # Update recipe stats
    await repo.increment_execution_count(recipe_id)
    await session.commit()
    
    return ExecutionResponse.from_execution(execution)


@router.post("/{recipe_id}/preview")
async def preview_recipe(
    recipe_id: UUID,
    parameter_values: dict[str, Any] = Body(default_factory=dict),
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
) -> dict:
    """
    Preview the query that would be executed.
    
    Returns the rendered DataQuery without executing it.
    """
    executor = get_executor()
    
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id and not recipe.is_public:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        query = executor.preview(recipe, parameter_values)
        return {
            "recipe_id": str(recipe_id),
            "recipe_name": recipe.name,
            "parameters_used": parameter_values,
            "query": query.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{recipe_id}/executions", response_model=list[ExecutionResponse])
async def list_executions(
    recipe_id: UUID,
    user: UserContext = Depends(get_current_user),
    repo: RecipeRepository = Depends(get_repository),
    limit: int = Query(50, ge=1, le=200),
) -> list[ExecutionResponse]:
    """List execution history for a recipe."""
    recipe = await repo.get_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    
    if recipe.tenant_id != user.tenant_id and not recipe.is_public:
        raise HTTPException(status_code=403, detail="Access denied")
    
    executions = await repo.list_executions(recipe_id, limit=limit)
    
    return [ExecutionResponse.from_execution(e) for e in executions]
