# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Recipe System for MEHO.

Recipes are captured Q&A interactions that can be saved, shared, and replayed.
When a user successfully gets an answer to their question, they can "save as recipe"
to create a reusable automation.

Key Concepts:
- Recipe: A saved Q&A pattern with variable parameters
- RecipeParameter: User-configurable inputs (e.g., "region", "threshold")
- RecipeExecution: A single run of a recipe with specific parameter values
- RecipeTemplate: The query pattern that gets executed

Example Flow:
1. User asks: "Show me clusters in us-east with memory > 80%"
2. LLM generates query, returns results
3. User clicks "Save as Recipe"
4. System captures: Question pattern, DataQuery, parameters (region, threshold)
5. Recipe is saved as "High Memory Clusters by Region"
6. User can later run: recipe.execute(region="eu-west", threshold=70)
"""

from meho_app.modules.agents.recipes.capture import RecipeCaptureService
from meho_app.modules.agents.recipes.executor import RecipeExecutor
from meho_app.modules.agents.recipes.models import (
    Recipe,
    RecipeExecution,
    RecipeExecutionStatus,
    RecipeParameter,
    RecipeParameterType,
    RecipeQueryTemplate,
)
from meho_app.modules.agents.recipes.repository import RecipeRepository

__all__ = [
    # Models
    "Recipe",
    # Services
    "RecipeCaptureService",
    "RecipeExecution",
    "RecipeExecutionStatus",
    "RecipeExecutor",
    "RecipeParameter",
    "RecipeParameterType",
    "RecipeQueryTemplate",
    # Repository
    "RecipeRepository",
]
