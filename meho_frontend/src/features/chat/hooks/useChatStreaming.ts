// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Streaming Hook
 *
 * Manages SSE streaming, message handling, and investigation tracking
 * via the orchestrator store. Orchestrator event routing via onOrchestratorEvent
 * callback. Agent events are routed to the orchestrator Zustand store directly.
 */
import { useCallback, useRef } from 'react';
import { config } from '@/lib/config';
import { triggerSessionExpired } from '@/lib/api-client';
import { useAuth } from '@/contexts/AuthContext';
import type { Plan } from '@/api/types';
import type { ChatMessage, StreamEventData, UsageSummary, ErrorSeverity, CitationData } from '../types';
import type { ApprovalRequest } from '@/components/ApprovalModal';
import type { AuditEntry } from '@/components/AuditCard';
import type { OrchestratorEvent } from '@/api/types/orchestrator';
import type { Hypothesis } from '../stores/slices/orchestratorSlice';
import { useChatStore } from '../stores';
import { parseTargetEntity } from '../stores/slices/orchestratorSlice';
import { extractOutputSummary } from '../utils/extractOutputSummary';
import { parseSynthesis } from '../utils/parseSynthesis';

/**
 * Classify error severity from message content and event data.
 * Used to determine ErrorCard color coding and retry eligibility.
 */
function classifyErrorSeverity(message: string, data: StreamEventData): ErrorSeverity {
  // Use explicit severity from backend if provided
  if (data.severity === 'transient') return 'retryable';
  if (data.severity === 'informational') return 'informational';

  const msg = message.toLowerCase();

  // Retryable: timeout, rate limit, temporary failures
  if (msg.includes('timeout') || msg.includes('rate limit') || msg.includes('temporarily')
    || msg.includes('429') || msg.includes('503') || msg.includes('504')
    || msg.includes('try again') || msg.includes('processing another request')) {
    return 'retryable';
  }

  // Informational: partial results, connector unavailable
  if (msg.includes('partial') || msg.includes('unavailable') || msg.includes('skipped')) {
    return 'informational';
  }

  // Fatal: everything else (auth failure, internal error, invalid request)
  return 'fatal';
}

interface StreamingOptions {
  sessionId: string;
  message: string;
  /** Phase 63-03: Optional connector_id for @mention direct routing bypass */
  connectorId?: string;
  onThinking: (thinkingId: string) => void;
  onMessage: (message: ChatMessage) => void;
  onUpdateMessage: (id: string, updates: Partial<ChatMessage>) => void;
  onRemoveMessage: (id: string) => void;
  onPlanReady: (plan: Plan, workflowId: string, requiresApproval: boolean) => void;
  onApprovalRequired: (approval: ApprovalRequest, originalMessage: string) => void;
  onComplete: () => void;
  onError: (error: string) => void;
  onStartPolling: (workflowId: string) => void;
  /** Callback for orchestrator events (03.1-01: event routing to ChatPage) */
  onOrchestratorEvent?: (event: OrchestratorEvent) => void;
  /** Callback for audit trail entries (Phase 5: post-approval/denial) */
  onAuditEntry?: (entry: AuditEntry) => void;
  /** Callback for token usage summary (OBSV-05: usage_summary SSE event) */
  onUsageSummary?: (usage: UsageSummary) => void;
}

export function useChatStreaming() {
  const { token, refreshToken } = useAuth();
  const abortControllerRef = useRef<AbortController | null>(null);

  const streamChat = useCallback(async (options: StreamingOptions) => {
    const {
      sessionId,
      message,
      onThinking,
      onMessage,
      onUpdateMessage,
      onRemoveMessage,
      onPlanReady,
      onApprovalRequired,
      onComplete,
      onError,
      onStartPolling,
      onOrchestratorEvent,
      onAuditEntry,
      onUsageSummary,
    } = options;

    // Guard against double onComplete (03.1-01: completedRef)
    const completedRef = { current: false };
    const safeComplete = () => {
      if (completedRef.current) return;
      completedRef.current = true;
      onComplete();
    };

    // Create abort controller for this request
    abortControllerRef.current = new AbortController();

    // Add thinking indicator
    const thinkingId = `thinking-${Date.now()}`;
    onThinking(thinkingId);
    onMessage({
      id: thinkingId,
      role: 'assistant',
      content: 'Thinking...',
      timestamp: new Date(),
    });

    if (!token) {
      onError('Not authenticated');
      return;
    }

    // Phase 65-05: Read session mode from Zustand store for request body
    const sessionMode = useChatStore.getState().sessionMode;

    /** Helper: perform the SSE fetch with a given token */
    const doFetch = (authToken: string) =>
      fetch(`${config.apiURL}/api/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${authToken}`,
        },
        body: JSON.stringify({
          message,
          session_id: sessionId,
          session_mode: sessionMode,
          // Phase 63-03: Include connector_id for @mention direct routing
          ...(options.connectorId && { connector_id: options.connectorId }),
        }),
        signal: abortControllerRef.current?.signal,
      });

    try {
      let response = await doFetch(token);

      // On 401: attempt token refresh and retry once (no hard logout)
      if (response.status === 401) {
        const newToken = await refreshToken(5);
        if (newToken) {
          response = await doFetch(newToken);
        }
        if (response.status === 401) {
          // Refresh failed or second attempt still 401 -- surface via session expired modal
          triggerSessionExpired();  // Phase 66: Surface re-auth modal
          throw new Error('Session expired. Please re-authenticate.');
        }
      }

      // Phase 39: Handle 409 Conflict (agent already processing in group session)
      if (response.status === 409) {
        throw new Error('MEHO is currently processing another request. Please wait.');
      }

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('No response body');
      }

      let workflowId: string | null = null;
      let assistantMessageId: string | null = null;
      let buffer = '';
      // Phase 66: Track whether orchestrator_start was seen in this streaming session.
      // Used to detect @mention bypass (no orchestrator) and auto-start investigation.
      // Local variable (not store state) to avoid race condition (see Research Pitfall 2).
      let seenOrchestratorStart = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data: StreamEventData = JSON.parse(line.slice(6));

              switch (data.type) {
                // ============================================================
                // Orchestrator lifecycle events (03.1-01: route to ChatPage)
                // ============================================================
                case 'orchestrator_start':
                case 'orchestrator_plan':
                case 'iteration_start':
                case 'dispatch_start':
                case 'early_findings':
                case 'iteration_complete':
                case 'synthesis_start':
                case 'orchestrator_complete': {
                  if (onOrchestratorEvent) {
                    onOrchestratorEvent({ ...data, _arrivalTime: Date.now() } as OrchestratorEvent & { _arrivalTime: number });
                  }

                  // Phase 60-02: Route orchestrator lifecycle events to agent pane Zustand store
                  const store = useChatStore.getState();

                  // Phase 64-02: Announce agent status transitions to screen readers (locked decision)
                  if (data.type === 'orchestrator_start') {
                    store.announce('Agent started investigation', 'polite');
                  } else if (data.type === 'dispatch_start') {
                    const dispatchConnectors = data.data?.connectors ?? (data as Record<string, unknown>).connectors;
                    if (Array.isArray(dispatchConnectors) && dispatchConnectors.length) {
                      const names = (dispatchConnectors as Array<{ name: string }>).map((c) => c.name).join(', ');
                      store.announce(`Querying ${names}`, 'polite');
                    }
                  } else if (data.type === 'synthesis_start') {
                    store.announce('Synthesizing findings', 'polite');
                  } else if (data.type === 'orchestrator_complete') {
                    store.announce('Investigation complete', 'polite');
                  }

                  if (data.type === 'orchestrator_start') {
                    seenOrchestratorStart = true;
                    store.startInvestigation(Date.now());
                  } else if (data.type === 'orchestrator_plan') {
                    // Phase 99: Store investigation plan for InvestigationPlan component
                    const planData = data.data ?? data;
                    store.setInvestigationPlan({
                      classification: planData.classification ?? 'standard',
                      reasoning: planData.reasoning ?? '',
                      strategy: planData.strategy ?? 'progressive',
                      plannedSystems: (planData.planned_systems ?? []).map((s: Record<string, unknown>) => ({
                        id: String(s.id ?? ''),
                        name: String(s.name ?? ''),
                        reason: String(s.reason ?? ''),
                        priority: Number(s.priority ?? 1),
                        conditional: Boolean(s.conditional),
                      })),
                      estimatedCalls: Number(planData.estimated_calls ?? 0),
                    });

                    // Announce for screen readers (Phase 64 accessibility)
                    const classLabel = planData.classification === 'quick' ? 'Quick check' :
                      planData.classification === 'deep' ? 'Deep analysis' : 'Investigation';
                    store.announce(`${classLabel}: ${planData.reasoning ?? 'Planning investigation'}`, 'polite');
                  } else if (data.type === 'iteration_start') {
                    const iterNum = data.data?.iteration ?? (data as Record<string, unknown>).iteration;
                    if (typeof iterNum === 'number') {
                      store.addIteration(iterNum);
                    }
                  } else if (data.type === 'dispatch_start') {
                    const connectors = data.data?.connectors ?? (data as Record<string, unknown>).connectors;
                    const iterNum = data.data?.iteration ?? (data as Record<string, unknown>).iteration ?? store.currentIteration;
                    if (Array.isArray(connectors)) {
                      for (const conn of connectors as Array<{ id: string; name: string }>) {
                        store.registerConnector(conn.id, conn.name, iterNum as number);
                      }
                    }
                  }
                  break;
                }

                // Token usage summary (OBSV-05: surfaces token count + cost in chat)
                case 'usage_summary': {
                  if (onUsageSummary) {
                    onUsageSummary({
                      total_tokens: data.total_tokens ?? 0,
                      effective_tokens: data.effective_tokens ?? 0,
                      prompt_tokens: data.prompt_tokens ?? 0,
                      completion_tokens: data.completion_tokens ?? 0,
                      cache_read_tokens: data.cache_read_tokens ?? 0,
                      cache_write_tokens: data.cache_write_tokens ?? 0,
                      estimated_cost_usd: data.estimated_cost_usd ?? 0,
                      llm_calls: data.llm_calls ?? 0,
                    });
                  }
                  break;
                }

                // Phase 63-02: Context usage gauge (percentage of context window consumed)
                case 'context_usage': {
                  const store = useChatStore.getState();
                  store.setContextUsage({
                    percentage: data.percentage ?? 0,
                    tokensUsed: data.tokens_used ?? 0,
                    tokensLimit: data.tokens_limit ?? 200000,
                  });
                  break;
                }

                case 'thinking':
                  onUpdateMessage(thinkingId, { content: data.message || 'Thinking...' });
                  break;

                case 'planning_start':
                case 'workflow_created':
                  onUpdateMessage(thinkingId, { content: data.message || 'Planning...' });
                  if (data.workflow_id) {
                    workflowId = data.workflow_id;
                  }
                  break;

                case 'plan_ready':
                  onRemoveMessage(thinkingId);
                  if (data.plan && data.workflow_id) {
                    const requiresApproval = data.requires_approval ?? true;
                    workflowId = data.workflow_id;

                    const newAssistantId = `assistant-${Date.now()}`;
                    assistantMessageId = newAssistantId;

                    const assistantContent = requiresApproval
                      ? 'I\'ve created a plan for your request. Please review and approve it to proceed.'
                      : 'I\'ve created a plan and will execute it automatically since it only includes safe operations.';

                    onMessage({
                      id: newAssistantId,
                      role: 'assistant',
                      content: assistantContent,
                      workflowId: data.workflow_id,
                      plan: data.plan,
                      status: requiresApproval ? 'WAITING_APPROVAL' : 'RUNNING',
                      timestamp: new Date(),
                    });

                    onPlanReady(data.plan, data.workflow_id, requiresApproval);

                    if (!requiresApproval) {
                      onStartPolling(data.workflow_id);
                    }
                  }
                  break;

                case 'status': {
                  const statusMsg = data.icon ? `${data.icon} ${data.message}` : data.message || '';
                  onMessage({
                    id: `status-${Date.now()}`,
                    role: 'assistant',
                    content: statusMsg,
                    timestamp: new Date(),
                    isProgressUpdate: true,
                  });
                  break;
                }

                case 'auto_executing':
                  if (assistantMessageId) {
                    onUpdateMessage(assistantMessageId, { content: data.message || '' });
                  }
                  break;

                case 'approval_required':
                  if (data.approval_id) {
                    onApprovalRequired({
                      approval_id: data.approval_id,
                      tool: data.tool || '',
                      danger_level: data.danger_level || 'dangerous',
                      details: data.details || {},
                      tool_args: data.tool_args || {},
                      message: data.message || '',
                    }, message);
                  }
                  if (assistantMessageId) {
                    onUpdateMessage(assistantMessageId, { content: data.message || '' });
                  }
                  break;

                case 'step_start':
                case 'step_complete':
                case 'status_update':
                  if (assistantMessageId && data.message) {
                    onUpdateMessage(assistantMessageId, { content: data.message });
                  }
                  break;

                case 'execution_complete':
                  if (workflowId) {
                    const wfId = workflowId;
                    setTimeout(() => onStartPolling(wfId), 500);
                  } else if (data.message) {
                    onRemoveMessage(thinkingId);
                    onMessage({
                      id: `assistant-${Date.now()}`,
                      role: 'assistant',
                      content: data.message,
                      timestamp: new Date(),
                    });
                    safeComplete();
                  }
                  break;

                // ============================================================
                // Agent-scoped event routing
                // ============================================================

                case 'agent_event': {
                  // Phase 68.2: Auto-start investigation for @mention bypass (no orchestrator_start)
                  if (!seenOrchestratorStart) {
                    const store = useChatStore.getState();
                    store.startInvestigation(Date.now());
                    seenOrchestratorStart = true; // Only trigger once per streaming session
                  }

                  const agentSource = data.agent_source;
                  const innerEvent = data.inner_event;
                  const connectorId = agentSource?.connector_id;
                  const connectorName = agentSource?.connector_name || 'Unknown';
                  const arrivalTime = Date.now();

                  // Route to orchestrator so ConnectorCards get timeline data
                  if (onOrchestratorEvent) {
                    onOrchestratorEvent({ ...data, _arrivalTime: arrivalTime } as OrchestratorEvent & { _arrivalTime: number });
                  }

                  if (innerEvent?.type === 'approval_required' && innerEvent.data?.approval_id) {
                    const d = innerEvent.data;
                    const args = d.args as Record<string, unknown> | undefined;
                    onApprovalRequired({
                      approval_id: d.approval_id as string,
                      tool: (d.tool as string) || '',
                      danger_level: (d.danger_level as string) || 'dangerous',
                      details: (d.details as { method?: string; path?: string; description?: string; impact?: string }) || {
                        method: args?.method as string | undefined,
                        path: args?.operation_id as string | undefined,
                        description: d.description as string | undefined,
                      },
                      tool_args: (args as Record<string, unknown>) || {},
                      message: (d.description as string) || (d.message as string) || '',
                    }, message);
                  }

                  // Phase 60-02: Route agent events to Zustand store (investigation tracking)
                  if (connectorId && innerEvent) {
                    const agentStore = useChatStore.getState();
                    const iteration = agentSource?.iteration ?? agentStore.currentIteration;

                    // Ensure connector is registered
                    agentStore.registerConnector(connectorId, connectorName, iteration);

                    const innerType = innerEvent.type;
                    const innerData = innerEvent.data || {};
                    const stepId = `step-${arrivalTime}-${Math.random().toString(36).slice(2, 7)}`;

                    if (innerType === 'thought') {
                      // Reasoning step
                      agentStore.addStep(connectorId, iteration, {
                        id: stepId,
                        type: 'thought',
                        status: 'success',
                        reasoning: String(innerData.content || innerData.message || ''),
                        arrivalTime,
                      });
                    } else if (innerType === 'action' || innerType === 'tool_start') {
                      // Tool call step -- starts running
                      const toolName = String(innerData.tool || innerData.name || 'tool');
                      const args = (innerData.args || {}) as Record<string, unknown>;
                      agentStore.addStep(connectorId, iteration, {
                        id: stepId,
                        type: 'tool_call',
                        toolName,
                        targetEntity: parseTargetEntity(toolName, args),
                        status: 'running',
                        arrivalTime,
                      });
                    } else if (innerType === 'observation' || innerType === 'tool_complete') {
                      // Tool completed -- find and update the matching running step
                      const resultToolName = String(innerData.tool || innerData.name || '');
                      const storeState = useChatStore.getState();
                      // Find the iteration
                      const iterState = storeState.iterations.find((it) => it.iteration === iteration);
                      const connState = iterState?.connectors.get(connectorId);
                      if (connState) {
                        // Find the last running step with matching tool name
                        const runningStep = [...connState.steps].reverse().find(
                          (s) => s.status === 'running' && (!resultToolName || s.toolName === resultToolName),
                        );
                        if (runningStep) {
                          const duration = arrivalTime - runningStep.arrivalTime;
                          const result = innerData.result ?? innerData.content;
                          storeState.updateStepStatus(connectorId, iteration, runningStep.id, 'success', duration);
                          // Update observation fields by adding a new step with the data
                          // Instead, we update the step inline via a targeted set
                          const latestStore = useChatStore.getState();
                          const updatedIterations = latestStore.iterations.map((iter) => {
                            if (iter.iteration !== iteration) return iter;
                            const connectors = new Map(iter.connectors);
                            const conn = connectors.get(connectorId);
                            if (!conn) return iter;
                            connectors.set(connectorId, {
                              ...conn,
                              steps: conn.steps.map((s) =>
                                s.id === runningStep.id
                                  ? {
                                      ...s,
                                      observationSummary: extractOutputSummary(resultToolName, result) || (typeof result === 'string' ? result.slice(0, 100) : undefined),
                                      observationData: result,
                                    }
                                  : s,
                              ),
                            });
                            return { ...iter, connectors };
                          });
                          // Direct set on store for observation data
                          useChatStore.setState({ iterations: updatedIterations });
                        }
                      }
                    } else if (innerType === 'error') {
                      // Error step
                      agentStore.addStep(connectorId, iteration, {
                        id: stepId,
                        type: 'tool_call',
                        toolName: String(innerData.tool || 'error'),
                        status: 'failed',
                        reasoning: String(innerData.message || innerData.error || 'Unknown error'),
                        arrivalTime,
                      });
                    }
                  }
                  break;
                }

                case 'step_progress': {
                  // Step counter events from SpecialistAgent (may arrive unwrapped)
                  // Phase 68.2: Route directly to store instead of onAgentPaneUpdate callback
                  const connectorId = data.connector_id;
                  if (connectorId) {
                    const store = useChatStore.getState();
                    const currentIter = store.currentIteration || 1;
                    store.registerConnector(connectorId, data.connector_name || 'Unknown', currentIter);
                    store.addStep(connectorId, currentIter, {
                      id: `step-progress-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
                      type: 'tool_call',
                      status: 'running',
                      arrivalTime: Date.now(),
                    });
                  }
                  break;
                }

                case 'budget_extended': {
                  // Dynamic budget extension (Phase 36)
                  // Phase 68.2: Route directly to store instead of onAgentPaneUpdate callback
                  const connectorId = data.connector_id;
                  if (connectorId) {
                    const store = useChatStore.getState();
                    const currentIter = store.currentIteration || 1;
                    store.registerConnector(connectorId, data.connector_name || 'Unknown', currentIter);
                  }
                  break;
                }

                case 'connector_complete': {
                  // Dual handling: orchestrator event + store update
                  if (onOrchestratorEvent) {
                    onOrchestratorEvent({ ...data, _arrivalTime: Date.now() } as OrchestratorEvent & { _arrivalTime: number });
                  }

                  // Phase 60-02: Update connector status in Zustand store
                  {
                    const ccId = data.connector_id;
                    if (ccId) {
                      const ccStatus = (data.status || 'success') as 'success' | 'failed' | 'timeout' | 'partial';
                      useChatStore.getState().updateConnectorStatus(ccId, ccStatus);
                    }
                  }
                  break;
                }

                case 'synthesis_chunk': {
                  // Dual handling: orchestrator event + message update (03.1-01)
                  if (onOrchestratorEvent) {
                    onOrchestratorEvent({ ...data, _arrivalTime: Date.now() } as OrchestratorEvent & { _arrivalTime: number });
                  }
                  // Progressive rendering of synthesis text (01-06)
                  // Passthrough metadata surfaced as structured fields (03.1-01)
                  const chunk = data.content || '';
                  if (chunk) {
                    onUpdateMessage(thinkingId, {
                      content: chunk,
                      // Surface passthrough metadata for ChatPage to handle
                      ...(data.passthrough === true && {
                        passthrough: true,
                        sourceConnector: data.source_connector,
                        sourceConnectorId: data.source_connector_id,
                      }),
                    });
                  }
                  break;
                }

                // Phase 5: Audit trail entries after approval/denial
                case 'audit_entry':
                  if (onAuditEntry) {
                    onAuditEntry({
                      approval_id: data.approval_id || null,
                      tool: data.tool || '',
                      trust_tier: data.trust_tier || data.danger_level || 'write',
                      decision: data.decision || 'approved',
                      outcome_status: data.outcome_status || 'success',
                      outcome_summary: data.outcome_summary || '',
                      connector_name: data.connector_name || '',
                      timestamp: data.timestamp || new Date().toISOString(),
                      user_id: data.user_id || '',  // Phase 7.1: user attribution
                    });
                  }
                  break;

                // Phase 62: Hypothesis update from specialist agent reasoning
                case 'hypothesis_update': {
                  const store = useChatStore.getState();
                  store.upsertHypothesis({
                    id: data.hypothesis_id || `h-${Date.now()}`,
                    text: data.text || data.content || '',
                    status: (data.status as Hypothesis['status']) || 'investigating',
                    connectorId: data.connector_id,
                    connectorName: data.connector_name,
                    updatedAt: Date.now(),
                  });
                  break;
                }

                // Phase 62: Follow-up suggestion questions after synthesis
                case 'follow_up_suggestions': {
                  const store = useChatStore.getState();
                  const suggestions = data.suggestions || [];
                  if (Array.isArray(suggestions) && suggestions.length > 0) {
                    store.setFollowUpSuggestions(suggestions);
                  }
                  break;
                }

                // Phase 62: Citation map linking superscripts to connector data sources
                case 'citation_map': {
                  const store = useChatStore.getState();
                  const citations = data.citations || {};
                  // Attach citation map to the current synthesis message
                  const messages = store.messages;
                  const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant');
                  if (lastAssistant) {
                    // Transform backend citation data to frontend CitationData
                    const frontendCitations: Record<string, CitationData> = {};
                    for (const [num, cite] of Object.entries(citations)) {
                      const c = cite as Record<string, unknown>;
                      frontendCitations[num] = {
                        stepId: (c.step_id as string) || '',
                        connectorId: (c.connector_id as string) || '',
                        connectorName: (c.connector_name as string) || '',
                        connectorType: (c.connector_type as string) || 'rest',
                        dataRef: c.data_ref as { table: string; session_id: string; row_count: number } | undefined,
                      };
                    }
                    store.updateMessage(lastAssistant.id, { citations: frontendCitations });
                  }
                  break;
                }

                // SSE keepalive -- ignore, keeps connection alive through proxies
                case 'keepalive':
                  break;

                case 'final_answer': {
                  onRemoveMessage(thinkingId);
                  const finalContent = data.content || '';
                  const finalMsgId = `assistant-${Date.now()}`;
                  const structured = parseSynthesis(finalContent);
                  onMessage({
                    id: finalMsgId,
                    role: 'assistant',
                    content: finalContent,
                    timestamp: new Date(),
                    ...(structured && { structuredContent: structured }),
                  });
                  safeComplete();
                  break;
                }

                case 'done':
                  safeComplete();
                  break;

                case 'error': {
                  onRemoveMessage(thinkingId);
                  const errorMsg = data.message || 'Unknown error';
                  const severity = classifyErrorSeverity(errorMsg, data);
                  onMessage({
                    id: `error-${Date.now()}`,
                    role: 'assistant',
                    content: errorMsg,
                    timestamp: new Date(),
                    errorType: data.error_type || 'Error',
                    errorSeverity: severity,
                    errorDetails: data.trace_id
                      ? `Trace ID: ${data.trace_id}${data.details ? '\n' + JSON.stringify(data.details, null, 2) : ''}`
                      : data.details ? JSON.stringify(data.details, null, 2) : undefined,
                    errorConnector: data.connector_name,
                    retryQuery: message,
                  });
                  safeComplete();
                  break;
                }
              }
            } catch (parseError) {
              console.error('Error parsing SSE message:', parseError);
            }
          }
        }
      }

      safeComplete();
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        return; // Cancelled by user
      }
      console.error('Error in streaming chat:', error);
      onRemoveMessage(thinkingId);
      const errorMsg = (error as Error).message || 'Connection error';
      onMessage({
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: errorMsg,
        timestamp: new Date(),
        errorType: 'Connection Error',
        errorSeverity: 'retryable',
        retryQuery: message,
      });
      safeComplete();
    }
  }, [token, refreshToken]);

  const cancelStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  return {
    streamChat,
    cancelStream,
  };
}
