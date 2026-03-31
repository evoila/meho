// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator Skills API Types
 *
 * Types for orchestrator skills CRUD and LLM-assisted generation.
 * API methods are on MEHOAPIClient (lib/api-client.ts), matching the
 * existing pattern for scheduled tasks, recipes, etc.
 *
 * Phase 52 - Orchestrator Skills Frontend
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface OrchestratorSkillSummary {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
}

export interface OrchestratorSkill {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  content: string;
  summary: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateSkillRequest {
  name: string;
  description?: string;
  content: string;
}

export interface UpdateSkillRequest {
  name?: string;
  description?: string;
  content?: string;
  is_active?: boolean;
}

export interface GenerateSkillRequest {
  user_description: string;
}

export interface GenerateSkillResponse {
  content: string;
}
