// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Scheduled Task Types
 *
 * Types for scheduled task management: CRUD, toggle, run-now, run history,
 * NL-to-cron conversion, and cron validation.
 * Phase 45 - Scheduled Tasks
 */

export interface ScheduledTask {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  cron_expression: string;
  timezone: string;
  prompt: string;
  is_enabled: boolean;
  next_run_at: string | null;
  total_runs: number;
  last_run_at: string | null;
  last_run_status: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  // Identity model (Phase 74)
  created_by_user_id?: string | null;
  allowed_connector_ids?: string[] | null;
  delegate_credentials?: boolean;
  delegation_active?: boolean;
  // Notification targets (Phase 75)
  notification_targets: Array<{ connector_id: string; contact: string }> | null;
}

export interface ScheduledTaskRun {
  id: string;
  task_id: string;
  status: 'running' | 'success' | 'failed';
  session_id: string | null;
  error_message: string | null;
  prompt_snapshot: string;
  started_at: string;
  completed_at: string | null;
  duration_seconds: number | null;
}

export interface CreateScheduledTaskRequest {
  name: string;
  description?: string;
  cron_expression: string;
  timezone: string;
  prompt: string;
  allowed_connector_ids?: string[] | null;
  delegate_credentials?: boolean;
  notification_targets?: Array<{ connector_id: string; contact: string }> | null;
}

export interface UpdateScheduledTaskRequest {
  name?: string;
  description?: string;
  cron_expression?: string;
  timezone?: string;
  prompt?: string;
  is_enabled?: boolean;
  allowed_connector_ids?: string[] | null;
  delegate_credentials?: boolean;
  notification_targets?: Array<{ connector_id: string; contact: string }> | null;
}

export interface ParseScheduleResponse {
  cron_expression: string;
  next_runs: string[];
  human_readable: string | null;
}

export interface ValidateCronResponse {
  is_valid: boolean;
  cron_expression: string | null;
  next_runs: string[];
  error: string | null;
}
