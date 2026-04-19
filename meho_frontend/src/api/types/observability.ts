// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Observability API Types
 *
 * TypeScript types matching backend Pydantic schemas for deep observability.
 * These types are used by the observability feature module.
 */

// ============================================================================
// Token Usage
// ============================================================================

/**
 * Token usage metrics from LLM calls.
 */
export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
}

// ============================================================================
// Event Details
// ============================================================================

/**
 * Flexible container for event-type-specific details.
 * Only the relevant fields are populated based on event type.
 */
export interface EventDetails {
  // LLM fields
  llm_prompt?: string | null;
  llm_messages?: Array<Record<string, unknown>> | null;
  llm_response?: string | null;
  llm_parsed?: Record<string, unknown> | null;
  token_usage?: TokenUsage | null;
  llm_duration_ms?: number | null;
  model?: string | null;

  // HTTP fields
  http_method?: string | null;
  http_url?: string | null;
  http_headers?: Record<string, string> | null;
  http_request_body?: string | null;
  http_response_body?: string | null;
  http_status_code?: number | null;
  http_duration_ms?: number | null;

  // SQL fields
  sql_query?: string | null;
  sql_parameters?: Record<string, unknown> | null;
  sql_row_count?: number | null;
  sql_result_sample?: Array<Record<string, unknown>> | null;
  sql_duration_ms?: number | null;

  // Tool fields
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  tool_output?: unknown | null;
  tool_duration_ms?: number | null;
  tool_error?: string | null;
}

// ============================================================================
// Event Response
// ============================================================================

/**
 * Full event response from the API.
 */
export interface EventResponse {
  id: string;
  timestamp: string;
  type: string;
  summary: string;
  details: EventDetails;
  parent_event_id?: string | null;
  step_number?: number | null;
  node_name?: string | null;
  agent_name?: string | null;
  duration_ms?: number | null;
}

// ============================================================================
// Session Summary
// ============================================================================

/**
 * Summary statistics for a session.
 */
export interface SessionSummary {
  session_id: string;
  total_events: number;
  llm_calls: number;
  operation_calls: number;
  sql_queries: number;
  tool_calls: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
  total_duration_ms: number | null;
  error_count: number;
  start_time: string | null;
  end_time: string | null;
}

// ============================================================================
// Transcript Response
// ============================================================================

/**
 * Full transcript response with events and summary.
 * @deprecated Use MultiTranscriptResponse instead for multi-turn support.
 */
export interface TranscriptResponse {
  session_id: string;
  events: EventResponse[];
  summary: SessionSummary;
}

/**
 * Individual transcript in a multi-transcript session view.
 * Represents one user message/execution within a chat session.
 */
export interface TranscriptItem {
  transcript_id: string;
  user_query?: string | null;
  created_at: string;
  status: string;
  summary: SessionSummary;
  events: EventResponse[];
}

/**
 * Multiple transcripts for a session (multi-turn conversation view).
 * Each transcript represents one user message and its agent execution.
 */
export interface MultiTranscriptResponse {
  session_id: string;
  transcripts: TranscriptItem[];
  total_transcripts: number;
}

// ============================================================================
// Session List
// ============================================================================

/**
 * Session list item with basic info.
 * Matches backend SessionListItem schema from routes_observability.py
 */
export interface SessionListItem {
  session_id: string;
  created_at: string;
  status: string;
  user_query?: string | null;
  total_llm_calls: number;
  total_tokens: number;
  total_duration_ms: number;
}

/**
 * Paginated session list response.
 * Matches backend SessionListResponse schema from routes_observability.py
 */
export interface SessionListResponse {
  sessions: SessionListItem[];
  total: number;
  offset: number;
  limit: number;
}

// ============================================================================
// Search
// ============================================================================

/**
 * Search result item.
 */
export interface SearchResultItem {
  session_id: string;
  event_id: string;
  event_type: string;
  summary: string;
  timestamp: string;
  match_field: string;
  match_snippet: string;
  score: number;
}

/**
 * Search response.
 */
export interface SearchResponse {
  results: SearchResultItem[];
  total: number;
  query: string;
  took_ms: number;
}

// ============================================================================
// API Parameters
// ============================================================================

/**
 * Parameters for session list endpoint.
 * Matches backend query parameters from routes_observability.py
 */
export interface SessionListParams {
  limit?: number;
  offset?: number;
  status?: string;
}

/**
 * Parameters for transcript endpoint.
 */
export interface TranscriptParams {
  event_types?: string[];
  include_details?: boolean;
  limit?: number;
  offset?: number;
}

/**
 * Parameters for LLM calls endpoint.
 */
export interface LLMParams {
  include_messages?: boolean;
  include_response?: boolean;
}

/**
 * Parameters for HTTP calls endpoint.
 */
export interface HTTPParams {
  include_headers?: boolean;
  include_body?: boolean;
  status_filter?: 'all' | 'success' | 'error';
}

/**
 * Parameters for SQL queries endpoint.
 */
export interface SQLParams {
  include_results?: boolean;
  limit?: number;
}

/**
 * Parameters for search endpoint.
 */
export interface SearchParams {
  query: string;
  session_id?: string;
  event_types?: string[];
  from_date?: string;
  to_date?: string;
  limit?: number;
}

// ============================================================================
// Event Types
// ============================================================================

/**
 * Known event types.
 */
export type EventType =
  | 'llm_call'
  | 'http_request'
  | 'sql_query'
  | 'tool_call'
  | 'thought'
  | 'action'
  | 'observation'
  | 'error'
  | 'workflow_start'
  | 'workflow_complete'
  | 'agent_start'
  | 'agent_complete';

/**
 * Check if an event has LLM details.
 */
export function hasLLMDetails(details: EventDetails): boolean {
  return !!(details.llm_prompt || details.llm_response || details.llm_messages);
}

/**
 * Check if an event has HTTP details.
 */
export function hasHTTPDetails(details: EventDetails): boolean {
  return !!details.http_url;
}

/**
 * Check if an event has SQL details.
 */
export function hasSQLDetails(details: EventDetails): boolean {
  return !!details.sql_query;
}

/**
 * Check if an event has tool details.
 */
export function hasToolDetails(details: EventDetails): boolean {
  return !!(details.tool_input || details.tool_name);
}
