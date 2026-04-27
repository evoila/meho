// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Recipes domain client (CRUD, create-from-session, execute).
 *
 * Recipes are parameterized, reusable chat queries scoped to a tenant.
 * This client wraps `/api/recipes/*`, including the LLM-backed
 * `createRecipeFromSession` flow used by the "Save as recipe" UI.
 *
 * Migrated from `lib/api-client.ts` in Phase 4 (#350).
 */
import type { AxiosInstance } from 'axios';
import type { Recipe, RecipeParameter, RecipeExecution } from '../types';
import { getTransport } from './transport';

export function createRecipesClient(transport: AxiosInstance) {
  return {
    async listRecipes(filters?: {
      tag?: string;
      search?: string;
      connector_id?: string;
    }): Promise<Recipe[]> {
      const params = new URLSearchParams();
      if (filters?.tag) params.set('tag', filters.tag);
      if (filters?.search) params.set('search', filters.search);
      if (filters?.connector_id) params.set('connector_id', filters.connector_id);

      const response = await transport.get<{ recipes: Recipe[]; total: number }>(
        `/api/recipes?${params.toString()}`,
      );
      return response.data.recipes;
    },

    async getRecipe(recipeId: string): Promise<Recipe> {
      const response = await transport.get<Recipe>(`/api/recipes/${recipeId}`);
      return response.data;
    },

    async createRecipe(request: {
      name: string;
      description?: string;
      tags?: string[];
      connector_id?: string;
      query_template: string;
      parameters?: RecipeParameter[];
    }): Promise<Recipe> {
      const response = await transport.post<Recipe>('/api/recipes', request);
      return response.data;
    },

    async deleteRecipe(recipeId: string): Promise<void> {
      await transport.delete(`/api/recipes/${recipeId}`);
    },

    /**
     * Analyze a chat session with an LLM and create a recipe draft from it.
     * Used by the "Save as recipe" action on the chat header (Phase 63).
     */
    async createRecipeFromSession(sessionId: string): Promise<Recipe> {
      const response = await transport.post<Recipe>(
        `/api/recipes/create-from-session/${sessionId}`,
      );
      return response.data;
    },

    /**
     * Partial update to name/description/tags/parameters (Phase 63).
     */
    async updateRecipe(
      recipeId: string,
      request: {
        name?: string;
        description?: string;
        tags?: string[];
        parameters?: RecipeParameter[];
      },
    ): Promise<Recipe> {
      const response = await transport.patch<Recipe>(`/api/recipes/${recipeId}`, request);
      return response.data;
    },

    async executeRecipe(
      recipeId: string,
      parameters: Record<string, unknown>,
    ): Promise<RecipeExecution> {
      const response = await transport.post<RecipeExecution>(
        `/api/recipes/${recipeId}/execute`,
        { parameters },
      );
      return response.data;
    },
  };
}

let recipesClient: ReturnType<typeof createRecipesClient> | null = null;

export function getRecipesClient(): ReturnType<typeof createRecipesClient> {
  if (!recipesClient) {
    recipesClient = createRecipesClient(getTransport());
  }
  return recipesClient;
}
