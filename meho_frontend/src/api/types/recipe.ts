// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Recipe Types
 * 
 * Types for saved recipes and executions (Session 80 - Unified Execution Architecture).
 */

export interface RecipeParameter {
  name: string;
  description: string;
  type: 'string' | 'number' | 'integer' | 'boolean' | 'array';
  required: boolean;
  default?: string | number | boolean;
  enum?: string[];
  min_value?: number;
  max_value?: number;
}

export interface Recipe {
  id: string;
  name: string;
  description?: string;
  tenant_id: string;
  connector_id?: string;
  endpoint_id?: string;
  original_question: string;
  tags: string[];
  parameters: RecipeParameter[];
  is_public: boolean;
  created_at: string;
  execution_count: number;
  last_executed_at?: string;
}

export interface RecipeExecution {
  id: string;
  recipe_id: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  parameters: Record<string, unknown>;
  results?: unknown;
  error?: string;
  started_at: string;
  finished_at?: string;
}

