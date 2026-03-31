// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator Event Types
 *
 * TypeScript types for orchestrator SSE events.
 * The orchestrator dispatches to multiple connectors in parallel and streams
 * events as agents complete for TTFUR (Time to First Useful Response).
 */

/**
 * Source information for wrapped agent events.
 * Identifies which connector/agent produced the event.
 */
export interface AgentSource {
  /** Display name for SSE attribution (e.g., "generic_agent_k8s_prod") */
  agent_name: string;
  /** Connector ID that produced this event */
  connector_id: string;
  /** Human-readable connector name */
  connector_name: string;
  /** Which iteration this event belongs to */
  iteration: number;
}

/**
 * Wrapped event from a child agent.
 * The orchestrator wraps events from connector-specific agents with source metadata.
 */
export interface WrappedAgentEvent {
  type: 'agent_event';
  /** Source metadata identifying which agent produced this event */
  agent_source: AgentSource;
  /** The original event from the child agent */
  inner_event: {
    type: string;
    data: Record<string, unknown>;
    timestamp?: string;
  };
  /** Frontend-captured arrival time (Date.now() when event was received via SSE) */
  _arrivalTime?: number;
}

/**
 * Emitted when orchestrator begins processing a user query.
 */
export interface OrchestratorStartEvent {
  type: 'orchestrator_start';
  agent: string;
  data: {
    goal: string;
  };
  session_id?: string;
}

/**
 * Phase 99: Emitted after routing LLM classifies query and before dispatch_start.
 * Shows the investigation plan (classification, reasoning, strategy, systems).
 */
export interface PlannedSystem {
  /** Connector ID */
  id: string;
  /** Human-readable connector name */
  name: string;
  /** Why this system is being queried */
  reason: string;
  /** Dispatch priority (1 = immediate, 2 = conditional) */
  priority: number;
  /** Whether dispatch depends on priority-1 findings */
  conditional: boolean;
}

export interface OrchestratorPlanEvent {
  type: 'orchestrator_plan';
  agent: string;
  data: {
    /** Query complexity classification */
    classification: 'quick' | 'standard' | 'deep';
    /** LLM's explanation of investigation strategy */
    reasoning: string;
    /** Dispatch strategy: direct (1 system), progressive (1 then maybe 2), parallel (all at once) */
    strategy: 'direct' | 'progressive' | 'parallel';
    /** Systems planned for investigation */
    planned_systems: PlannedSystem[];
    /** Estimated total tool calls across all specialists */
    estimated_calls: number;
  };
  session_id?: string;
}

/**
 * Emitted at the start of each iteration in the orchestrator loop.
 */
export interface IterationStartEvent {
  type: 'iteration_start';
  agent: string;
  data: {
    iteration: number;
  };
  session_id?: string;
}

/**
 * Emitted when dispatching to connectors.
 * Lists which connectors will be queried in this iteration.
 */
export interface DispatchStartEvent {
  type: 'dispatch_start';
  agent: string;
  data: {
    iteration: number;
    connectors: Array<{ id: string; name: string }>;
  };
  session_id?: string;
}

/**
 * Emitted when a connector agent completes (success, failure, or timeout).
 */
export interface ConnectorCompleteEvent {
  type: 'connector_complete';
  agent: string;
  data: {
    connector_id: string;
    connector_name: string;
    status: 'success' | 'partial' | 'failed' | 'timeout' | 'cancelled';
    findings_preview?: string | null;
  };
  session_id?: string;
}

/**
 * Emitted as agents complete for TTFUR optimization.
 * Allows frontend to show progress before all agents finish.
 */
export interface EarlyFindingsEvent {
  type: 'early_findings';
  agent: string;
  data: {
    connector_id: string;
    connector_name: string;
    findings_preview: string | null;
    status: string;
    /** How many connectors are still running */
    remaining_count: number;
  };
  session_id?: string;
}

/**
 * Emitted at the end of each iteration.
 */
export interface IterationCompleteEvent {
  type: 'iteration_complete';
  agent: string;
  data: {
    iteration: number;
    findings_count: number;
    total_findings: number;
  };
  session_id?: string;
}

/**
 * Emitted when synthesis begins.
 */
export interface SynthesisStartEvent {
  type: 'synthesis_start';
  agent: string;
  data: {
    /** True if synthesizing partial results due to errors/timeouts */
    partial?: boolean;
  };
  session_id?: string;
}

/**
 * Emitted with each chunk of the streaming synthesis response.
 * Frontend should accumulate chunks for progressive text rendering.
 */
export interface SynthesisChunkEvent {
  type: 'synthesis_chunk';
  agent: string;
  data: {
    /** The new chunk of text to append */
    content: string;
    /** Length of accumulated text so far (for progress tracking) */
    accumulated_length: number;
    /** True if this is a single-connector passthrough (not re-summarized) (03-02) */
    passthrough?: boolean;
    /** Source connector name when passthrough is true (03-02) */
    source_connector?: string;
    /** Source connector ID when passthrough is true (03-02) */
    source_connector_id?: string;
  };
  session_id?: string;
}

/**
 * Emitted with the final synthesized answer.
 */
export interface FinalAnswerEvent {
  type: 'final_answer';
  agent: string;
  data: {
    content: string;
    iterations: number;
    connectors_queried: string[];
    total_time_ms: number;
    /** True if this is a partial answer due to errors/timeouts */
    partial?: boolean;
    /** Error message if partial due to error */
    error?: string | null;
  };
  session_id?: string;
}

/**
 * Emitted when orchestrator completes (success or failure).
 */
export interface OrchestratorCompleteEvent {
  type: 'orchestrator_complete';
  agent: string;
  data: {
    success: boolean;
    iterations: number;
    total_time_ms: number;
    /** True if this was a partial/degraded response */
    partial?: boolean;
  };
  session_id?: string;
}

/**
 * Emitted on error.
 */
export interface OrchestratorErrorEvent {
  type: 'error';
  agent: string;
  data: {
    message: string;
    /** Number of findings collected before error */
    findings_so_far?: number;
    /** Whether the error is recoverable (will still try to synthesize) */
    recoverable?: boolean;
  };
  session_id?: string;
}

/**
 * Connector status for UI tracking.
 */
export type ConnectorStatus = 'pending' | 'running' | 'success' | 'partial' | 'failed' | 'timeout' | 'cancelled';

/**
 * Connector state for tracking in the UI.
 */
export interface ConnectorState {
  id: string;
  name: string;
  status: ConnectorStatus;
  findings?: string | null;
  error?: string;
  events: WrappedAgentEvent[];
}

/**
 * Base type with arrival time for all events captured from SSE.
 * Added by frontend when event is received.
 */
export interface WithArrivalTime {
  /** Frontend-captured arrival time (Date.now() when event was received via SSE) */
  _arrivalTime: number;
}

/**
 * Orchestrator event with arrival time (for UI timing calculations).
 */
export type TimedOrchestratorEvent = OrchestratorEvent & WithArrivalTime;

/**
 * Union type of all orchestrator events.
 */
export type OrchestratorEvent =
  | OrchestratorStartEvent
  | OrchestratorPlanEvent
  | IterationStartEvent
  | DispatchStartEvent
  | WrappedAgentEvent
  | ConnectorCompleteEvent
  | EarlyFindingsEvent
  | IterationCompleteEvent
  | SynthesisStartEvent
  | SynthesisChunkEvent
  | FinalAnswerEvent
  | OrchestratorCompleteEvent
  | OrchestratorErrorEvent;

/**
 * Type guard to check if an event is an orchestrator event.
 */
export function isOrchestratorEvent(event: { type: string }): event is OrchestratorEvent {
  return [
    'orchestrator_start',
    'orchestrator_plan',
    'iteration_start',
    'dispatch_start',
    'agent_event',
    'connector_complete',
    'early_findings',
    'iteration_complete',
    'synthesis_start',
    'synthesis_chunk',
    'final_answer',
    'orchestrator_complete',
    'error',
  ].includes(event.type);
}

/**
 * Check if an event indicates orchestrator mode (vs legacy agent).
 */
export function isOrchestratorMode(event: { type: string }): boolean {
  return event.type === 'orchestrator_start';
}
