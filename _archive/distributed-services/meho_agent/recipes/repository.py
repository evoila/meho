"""
Recipe Repository - Persistence layer for recipes.

Handles storing and retrieving recipes from the PostgreSQL database.

Session 80: Added database persistence using SQLAlchemy.
"""
# mypy: disable-error-code="arg-type,union-attr,no-any-return,call-overload,attr-defined"

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, update, delete, and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from meho_agent.models import RecipeModel, RecipeExecutionModel, RecipeExecutionStatus as DBStatus
from meho_agent.recipes.models import (
    Recipe,
    RecipeParameter,
    RecipeQueryTemplate,
    RecipeExecution,
    RecipeExecutionStatus,
)

logger = logging.getLogger(__name__)


class RecipeRepository:
    """
    Repository for recipe persistence using SQLAlchemy.
    
    Uses PostgreSQL with JSONB for flexible storage of parameters and templates.
    """
    
    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the repository with a database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self._session = session
    
    # ==========================================================================
    # Conversion helpers
    # ==========================================================================
    
    def _model_to_recipe(self, model: RecipeModel) -> Recipe:
        """Convert SQLAlchemy model to Pydantic Recipe."""
        parameters = [
            RecipeParameter.model_validate(p) for p in (model.parameters or [])
        ]
        query_template = RecipeQueryTemplate.model_validate(model.query_template)
        
        return Recipe(
            id=model.id,
            tenant_id=model.tenant_id,
            name=model.name,
            description=model.description,
            tags=model.tags or [],
            connector_id=model.connector_id,
            endpoint_id=model.endpoint_id,
            original_question=model.original_question,
            parameters=parameters,
            query_template=query_template,
            interpretation_prompt=model.interpretation_prompt,
            created_at=model.created_at,
            updated_at=model.updated_at,
            execution_count=model.execution_count,
            last_executed_at=model.last_executed_at,
            is_public=model.is_public,
            created_by=model.created_by,
        )
    
    def _recipe_to_model(self, recipe: Recipe) -> RecipeModel:
        """Convert Pydantic Recipe to SQLAlchemy model."""
        return RecipeModel(
            id=recipe.id,
            tenant_id=recipe.tenant_id,
            name=recipe.name,
            description=recipe.description,
            tags=recipe.tags,
            connector_id=recipe.connector_id,
            endpoint_id=recipe.endpoint_id,
            original_question=recipe.original_question,
            parameters=[p.model_dump() for p in recipe.parameters],
            query_template=recipe.query_template.model_dump(),
            interpretation_prompt=recipe.interpretation_prompt,
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
            execution_count=recipe.execution_count,
            last_executed_at=recipe.last_executed_at,
            is_public=recipe.is_public,
            created_by=recipe.created_by,
        )
    
    def _model_to_execution(self, model: RecipeExecutionModel) -> RecipeExecution:
        """Convert SQLAlchemy model to Pydantic RecipeExecution."""
        # Map DB enum to Pydantic enum
        status_map = {
            DBStatus.PENDING: RecipeExecutionStatus.PENDING,
            DBStatus.RUNNING: RecipeExecutionStatus.RUNNING,
            DBStatus.COMPLETED: RecipeExecutionStatus.COMPLETED,
            DBStatus.FAILED: RecipeExecutionStatus.FAILED,
        }
        
        return RecipeExecution(
            id=model.id,
            recipe_id=model.recipe_id,
            tenant_id=model.tenant_id,
            parameter_values=model.parameter_values or {},
            status=status_map.get(model.status, RecipeExecutionStatus.PENDING),
            error_message=model.error_message,
            result_count=model.result_count,
            result_summary=model.result_summary,
            aggregates=model.aggregates or {},
            started_at=model.started_at,
            completed_at=model.completed_at,
            duration_ms=float(model.duration_ms) if model.duration_ms else None,
            triggered_by=model.triggered_by,
        )
    
    def _execution_to_model(self, execution: RecipeExecution) -> RecipeExecutionModel:
        """Convert Pydantic RecipeExecution to SQLAlchemy model."""
        # Map Pydantic enum to DB enum
        status_map = {
            RecipeExecutionStatus.PENDING: DBStatus.PENDING,
            RecipeExecutionStatus.RUNNING: DBStatus.RUNNING,
            RecipeExecutionStatus.COMPLETED: DBStatus.COMPLETED,
            RecipeExecutionStatus.FAILED: DBStatus.FAILED,
        }
        
        return RecipeExecutionModel(
            id=execution.id,
            recipe_id=execution.recipe_id,
            tenant_id=execution.tenant_id,
            parameter_values=execution.parameter_values,
            status=status_map.get(execution.status, DBStatus.PENDING),
            error_message=execution.error_message,
            result_count=execution.result_count,
            result_summary=execution.result_summary,
            aggregates=execution.aggregates,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            duration_ms=int(execution.duration_ms) if execution.duration_ms else None,
            triggered_by=execution.triggered_by,
        )
    
    # ==========================================================================
    # Recipe CRUD
    # ==========================================================================
    
    async def create_recipe(self, recipe: Recipe) -> Recipe:
        """
        Create a new recipe.
        
        Args:
            recipe: Recipe to create
            
        Returns:
            Created recipe with ID
        """
        model = self._recipe_to_model(recipe)
        self._session.add(model)
        await self._session.flush()
        logger.info(f"Created recipe: {recipe.id} - {recipe.name}")
        return self._model_to_recipe(model)
    
    async def get_recipe(self, recipe_id: UUID) -> Optional[Recipe]:
        """
        Get a recipe by ID.
        
        Args:
            recipe_id: Recipe ID
            
        Returns:
            Recipe if found, None otherwise
        """
        result = await self._session.execute(
            select(RecipeModel).where(RecipeModel.id == recipe_id)
        )
        model = result.scalar_one_or_none()
        return self._model_to_recipe(model) if model else None
    
    async def get_recipe_by_name(
        self,
        name: str,
        tenant_id: str,
    ) -> Optional[Recipe]:
        """
        Get a recipe by name within a tenant.
        
        Args:
            name: Recipe name
            tenant_id: Tenant ID
            
        Returns:
            Recipe if found, None otherwise
        """
        result = await self._session.execute(
            select(RecipeModel).where(
                and_(
                    RecipeModel.name == name,
                    RecipeModel.tenant_id == tenant_id
                )
            )
        )
        model = result.scalar_one_or_none()
        return self._model_to_recipe(model) if model else None
    
    async def list_recipes(
        self,
        tenant_id: str,
        connector_id: Optional[UUID] = None,
        tags: Optional[list[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Recipe]:
        """
        List recipes for a tenant.
        
        Args:
            tenant_id: Tenant ID
            connector_id: Optional filter by connector
            tags: Optional filter by tags (any match)
            limit: Maximum number to return
            offset: Number to skip
            
        Returns:
            List of matching recipes
        """
        query = select(RecipeModel).where(RecipeModel.tenant_id == tenant_id)
        
        if connector_id:
            query = query.where(RecipeModel.connector_id == connector_id)
        
        # Tag filtering using JSONB containment
        if tags:
            # Check if any tag is in the tags array
            tag_conditions = [
                RecipeModel.tags.contains([tag]) for tag in tags
            ]
            query = query.where(or_(*tag_conditions))
        
        # Sort by last executed (nulls last), then by name
        query = query.order_by(
            RecipeModel.last_executed_at.desc().nullslast(),
            RecipeModel.name
        ).limit(limit).offset(offset)
        
        result = await self._session.execute(query)
        models = result.scalars().all()
        return [self._model_to_recipe(m) for m in models]
    
    async def update_recipe(self, recipe: Recipe) -> Recipe:
        """
        Update an existing recipe.
        
        Args:
            recipe: Recipe with updated fields
            
        Returns:
            Updated recipe
        """
        recipe.updated_at = datetime.utcnow()
        
        await self._session.execute(
            update(RecipeModel)
            .where(RecipeModel.id == recipe.id)
            .values(
                name=recipe.name,
                description=recipe.description,
                tags=recipe.tags,
                parameters=[p.model_dump() for p in recipe.parameters],
                query_template=recipe.query_template.model_dump(),
                interpretation_prompt=recipe.interpretation_prompt,
                updated_at=recipe.updated_at,
                is_public=recipe.is_public,
            )
        )
        logger.info(f"Updated recipe: {recipe.id}")
        return recipe
    
    async def delete_recipe(self, recipe_id: UUID) -> bool:
        """
        Delete a recipe.
        
        Args:
            recipe_id: Recipe ID to delete
            
        Returns:
            True if deleted, False if not found
        """
        result = await self._session.execute(
            delete(RecipeModel).where(RecipeModel.id == recipe_id)
        )
        deleted = result.rowcount > 0
        if deleted:
            logger.info(f"Deleted recipe: {recipe_id}")
        return deleted
    
    async def increment_execution_count(self, recipe_id: UUID) -> None:
        """Increment the execution count for a recipe."""
        await self._session.execute(
            update(RecipeModel)
            .where(RecipeModel.id == recipe_id)
            .values(
                execution_count=RecipeModel.execution_count + 1,
                last_executed_at=datetime.utcnow()
            )
        )
    
    # ==========================================================================
    # Recipe Execution CRUD
    # ==========================================================================
    
    async def create_execution(
        self,
        execution: RecipeExecution,
    ) -> RecipeExecution:
        """
        Create a new execution record.
        
        Args:
            execution: Execution to create
            
        Returns:
            Created execution
        """
        model = self._execution_to_model(execution)
        self._session.add(model)
        await self._session.flush()
        return self._model_to_execution(model)
    
    async def get_execution(
        self,
        execution_id: UUID,
    ) -> Optional[RecipeExecution]:
        """Get an execution by ID."""
        result = await self._session.execute(
            select(RecipeExecutionModel).where(RecipeExecutionModel.id == execution_id)
        )
        model = result.scalar_one_or_none()
        return self._model_to_execution(model) if model else None
    
    async def list_executions(
        self,
        recipe_id: UUID,
        limit: int = 50,
    ) -> list[RecipeExecution]:
        """
        List executions for a recipe.
        
        Args:
            recipe_id: Recipe ID
            limit: Maximum number to return
            
        Returns:
            List of executions, most recent first
        """
        result = await self._session.execute(
            select(RecipeExecutionModel)
            .where(RecipeExecutionModel.recipe_id == recipe_id)
            .order_by(RecipeExecutionModel.started_at.desc().nullslast())
            .limit(limit)
        )
        models = result.scalars().all()
        return [self._model_to_execution(m) for m in models]
    
    async def update_execution(
        self,
        execution: RecipeExecution,
    ) -> RecipeExecution:
        """Update an execution record."""
        # Map Pydantic enum to DB enum
        status_map = {
            RecipeExecutionStatus.PENDING: DBStatus.PENDING,
            RecipeExecutionStatus.RUNNING: DBStatus.RUNNING,
            RecipeExecutionStatus.COMPLETED: DBStatus.COMPLETED,
            RecipeExecutionStatus.FAILED: DBStatus.FAILED,
        }
        
        await self._session.execute(
            update(RecipeExecutionModel)
            .where(RecipeExecutionModel.id == execution.id)
            .values(
                status=status_map.get(execution.status, DBStatus.PENDING),
                error_message=execution.error_message,
                result_count=execution.result_count,
                result_summary=execution.result_summary,
                aggregates=execution.aggregates,
                completed_at=execution.completed_at,
                duration_ms=int(execution.duration_ms) if execution.duration_ms else None,
            )
        )
        return execution
    
    # ==========================================================================
    # Search and Discovery
    # ==========================================================================
    
    async def search_recipes(
        self,
        tenant_id: str,
        query: str,
        limit: int = 20,
    ) -> list[Recipe]:
        """
        Search recipes by name and description.
        
        Args:
            tenant_id: Tenant ID
            query: Search query
            limit: Maximum number to return
            
        Returns:
            Matching recipes
        """
        query_lower = f"%{query.lower()}%"
        
        result = await self._session.execute(
            select(RecipeModel)
            .where(
                and_(
                    RecipeModel.tenant_id == tenant_id,
                    # Search in name, description, and original question
                    func.lower(
                        func.coalesce(RecipeModel.name, '') +
                        func.coalesce(RecipeModel.description, '') +
                        func.coalesce(RecipeModel.original_question, '')
                    ).like(query_lower)
                )
            )
            .limit(limit)
        )
        models = result.scalars().all()
        return [self._model_to_recipe(m) for m in models]
    
    async def get_popular_recipes(
        self,
        tenant_id: str,
        limit: int = 10,
    ) -> list[Recipe]:
        """
        Get most frequently executed recipes.
        
        Args:
            tenant_id: Tenant ID
            limit: Maximum number to return
            
        Returns:
            Most popular recipes
        """
        result = await self._session.execute(
            select(RecipeModel)
            .where(RecipeModel.tenant_id == tenant_id)
            .order_by(RecipeModel.execution_count.desc())
            .limit(limit)
        )
        models = result.scalars().all()
        return [self._model_to_recipe(m) for m in models]
    
    async def get_recent_recipes(
        self,
        tenant_id: str,
        limit: int = 10,
    ) -> list[Recipe]:
        """
        Get most recently executed recipes.
        
        Args:
            tenant_id: Tenant ID
            limit: Maximum number to return
            
        Returns:
            Most recently executed recipes
        """
        result = await self._session.execute(
            select(RecipeModel)
            .where(
                and_(
                    RecipeModel.tenant_id == tenant_id,
                    RecipeModel.last_executed_at.isnot(None)
                )
            )
            .order_by(RecipeModel.last_executed_at.desc())
            .limit(limit)
        )
        models = result.scalars().all()
        return [self._model_to_recipe(m) for m in models]
