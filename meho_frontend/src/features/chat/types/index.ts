// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Feature Types
 *
 * Types specific to the chat feature.
 */
import type { Plan } from '@/api/types';
import type { OrchestratorEvent } from '@/api/types/orchestrator';

/** Error severity classification for inline error cards (Phase 59) */
export type ErrorSeverity = 'retryable' | 'fatal' | 'informational';

/** Phase 62: Citation data linking a superscript to a connector data source */
export interface CitationData {
  stepId: string;
  connectorId: string;
  connectorName: string;
  connectorType: string;
  dataRef?: { table: string; session_id: string; row_count: number };
}

/** Phase 62: Parsed structured content from synthesis XML */
export interface StructuredContent {
  summary: string;
  reasoning: string;
  hypotheses: Array<{ text: string; status: string }>;
  connectorSegments: Array<{ connectorName: string; content: string }>;
}

/** Audit trail entry for post-approval/denial display (Phase 5) */
export interface AuditEntry {
  approval_id: string | null;
  tool: string;
  trust_tier: string;
  decision: string;
  outcome_status: string;
  outcome_summary: string;
  connector_name: string;
  timestamp: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  workflowId?: string;
  plan?: Plan;
  status?: string;
  timestamp: Date;
  isProgressUpdate?: boolean;
  // War room sender attribution (Phase 39)
  senderName?: string;
  senderId?: string;
  requestStartTime?: number;
  // Orchestrator events (TASK-181)
  orchestratorEvents?: OrchestratorEvent[];
  // Passthrough metadata (03.1-01: surfaced from synthesis_chunk for Wave 2)
  passthrough?: boolean;
  sourceConnector?: string;
  sourceConnectorId?: string;
  /** Connector source info for multi-connector attribution */
  connectorSources?: Array<{ id: string; name: string; type?: string }>;
  /** Raw data table references for lazy-load (from connector_complete) */
  dataRefs?: Array<{ table: string; session_id: string; row_count: number }>;
  /** Agent pane snapshots for completed messages */
  agentPanes?: Map<string, AgentPaneState>;
  /** Audit entry for approval/denial trail (Phase 5) */
  auditEntry?: AuditEntry;
  /** Error metadata -- present when message is an error card (Phase 59) */
  errorType?: string;
  errorSeverity?: ErrorSeverity;
  errorDetails?: string;
  errorConnector?: string;
  /** Original query for retry functionality */
  retryQuery?: string;
  /** Phase 62: Parsed structured content from synthesis XML */
  structuredContent?: StructuredContent;
  /** Phase 62: Citation map (number string -> data source) */
  citations?: Record<string, CitationData>;
  /** Phase 62: Follow-up suggestion questions */
  followUpSuggestions?: string[];
  /** Phase 63: @mention metadata for connector-targeted messages */
  mentionMetadata?: {
    connectorId: string;
    connectorName: string;
    connectorType: string;
  };
}

export interface StreamEventData {
  [key: string]: unknown;
  type: string;
  message?: string;
  content?: string;
  plan?: Plan;
  workflow_id?: string;
  requires_approval?: boolean;
  approval_id?: string;
  tool?: string;
  danger_level?: string;
  details?: Record<string, unknown>;
  tool_args?: Record<string, unknown>;
  icon?: string;
  result?: string;
  // Agent-scoped event routing (03-02)
  connector_id?: string;
  connector_name?: string;
  passthrough?: boolean;
  source_connector?: string;
  source_connector_id?: string;
  step?: number;
  max_steps?: number;
  data_refs?: Array<{ table: string; session_id: string; row_count: number }>;
  // Budget extension fields (Phase 36: dynamic budget)
  new_max?: number;
  // Connector completion fields
  status?: string;
  // Error event fields (Phase 59: structured error cards)
  error_type?: string;
  severity?: string;
  trace_id?: string;
  // Audit trail fields (Phase 5: audit_entry events)
  trust_tier?: string;
  decision?: string;
  outcome_status?: string;
  outcome_summary?: string;
  timestamp?: string;
  user_id?: string;
  // Token usage summary fields (OBSV-05: usage_summary event)
  total_tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  effective_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  estimated_cost_usd?: number;
  llm_calls?: number;
  // Context usage fields (context_usage event)
  percentage?: number;
  tokens_used?: number;
  tokens_limit?: number;
  // Nested data envelope (orchestrator lifecycle events)
  data?: Record<string, unknown>;
  // Connector list (dispatch_start, iteration_start)
  connectors?: Array<{ id: string; name: string }>;
  iteration?: number;
  // Phase 62: Structured investigation events
  hypothesis_id?: string;
  suggestions?: string[];
  citations?: Record<string, unknown>;
  text?: string;
  // War room event fields (Phase 39)
  sender_id?: string;
  sender_name?: string;
  // Wrapped agent event fields
  agent_source?: {
    connector_id?: string;
    connector_name?: string;
    agent_name?: string;
    iteration?: number;
  };
  inner_event?: {
    type: string;
    data?: Record<string, unknown>;
    timestamp?: string;
    step?: number;
  };
}

/** Agent pane state for parallel agent execution visualization (03-02) */
export interface AgentPaneState {
  connectorId: string;
  connectorName: string;
  status: 'running' | 'complete' | 'error';
  isExpanded: boolean;
  currentStep: number;
  maxSteps: number;
  events: AgentPaneEvent[];
  /** Raw data table references from connector_complete events */
  dataRefs?: Array<{ table: string; session_id: string; row_count: number }>;
}

export interface AgentPaneEvent {
  id: string;
  type: 'thought' | 'action' | 'observation' | 'step_progress' | 'error';
  content: string;
  timestamp: Date;
  toolName?: string;
}

export type ChatSessionStatus = 'idle' | 'processing' | 'error';

/** Token usage summary from orchestrator usage_summary SSE event (OBSV-05) */
export interface UsageSummary {
  total_tokens: number;
  effective_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  estimated_cost_usd: number;
  llm_calls: number;
}

