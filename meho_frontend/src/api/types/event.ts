// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Event Types
 *
 * Types for event management: registration CRUD, event history, and test pipeline.
 * Phase 94 - Events System + Response Channels
 */

export interface EventRegistration {
  id: string;
  name: string;
  event_url: string;
  prompt_template: string;
  rate_limit_per_hour: number;
  is_active: boolean;
  require_signature: boolean;
  total_events_received: number;
  total_events_processed: number;
  total_events_deduplicated: number;
  events_today: number;
  last_event_at: string | null;
  created_at: string;
  updated_at: string;
  // Identity model (Phase 74)
  created_by_user_id?: string | null;
  allowed_connector_ids?: string[] | null;
  delegation_active?: boolean;
  // Notification targets (Phase 75)
  notification_targets: Array<{ connector_id: string; contact: string }> | null;
  // Response channel config (Phase 94)
  response_config?: {
    connector_id: string;
    operation_id: string;
    parameter_mapping: Record<string, string>;
  } | null;
}

export interface EventCreateResponse extends EventRegistration {
  secret: string;
}

export interface EventHistoryEntry {
  id: string;
  status: 'processed' | 'deduplicated' | 'rate_limited' | 'failed' | 'test';
  payload_hash: string;
  payload_size_bytes: number;
  session_id: string | null;
  error_message: string | null;
  created_at: string;
}

export interface EventHistoryResponse {
  events: EventHistoryEntry[];
  total: number;
  has_more: boolean;
}

export interface EventTestStep {
  step: string;
  status: 'success' | 'failed';
  detail?: string;
}

export interface EventTestResponse {
  steps: EventTestStep[];
  status: 'success' | 'failed';
  session_id?: string;
  rendered_prompt?: string;
  error?: string;
}
