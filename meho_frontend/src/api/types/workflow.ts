// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Workflow Types
 * 
 * Types for workflow execution and plan management.
 */

export interface Workflow {
  id: string;
  tenant_id: string;
  user_id: string;
  goal: string;
  status: 'PLANNING' | 'WAITING_APPROVAL' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
  plan: Plan | null;
  result: ExecutionResult | string | null;
  created_at: string;
  updated_at: string;
}

export interface Plan {
  goal: string;
  steps: PlanStep[];
  notes?: string;
}

export interface PlanStep {
  id: string;
  description: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  depends_on: string[];
}

export interface ExecutionResult {
  step_results: Record<string, unknown>;
  step_errors: Record<string, string>;
  completed_steps: string[];
  failed_steps: string[];
  answer?: string;
  response?: string;
  text?: string;
}

