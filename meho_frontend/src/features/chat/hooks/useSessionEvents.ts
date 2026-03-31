// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Session Events Hook
 *
 * Subscribes to the /{session_id}/events SSE endpoint for group sessions
 * (and private sessions when forceConnect is true).
 *
 * Handles the FULL event stream from Redis pub/sub, including:
 * - Orchestrator lifecycle (start, iterations, dispatch, synthesis, complete)
 * - Agent events (thoughts, actions, observations per connector)
 * - Synthesis chunks (progressive rendering)
 * - Final answer (assistant message creation)
 * - Processing state (started/complete)
 * - War room events (remote user messages)
 *
 * Event replay: The backend replays stored events when a subscriber
 * connects to an active session, so late-joining viewers (e.g. opening
 * an event-triggered session) see the full investigation from the start.
 */
import { useCallback, useRef, useEffect } from 'react';
import { config } from '@/lib/config';
import { useAuth } from '@/contexts/AuthContext';
import { useChatStore } from '../stores';
import { parseTargetEntity } from '../stores/slices/orchestratorSlice';
import { extractOutputSummary } from '../utils/extractOutputSummary';
import { parseSynthesis } from '../utils/parseSynthesis';
import type { ChatMessage, StreamEventData } from '../types';
import type { OrchestratorEvent } from '@/api/types/orchestrator';
import type { Hypothesis } from '../stores/slices/orchestratorSlice';

const INITIAL_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;
const BACKOFF_MULTIPLIER = 2;

function getRetryDelay(retryCount: number): number {
  const baseDelay = Math.min(INITIAL_DELAY_MS * Math.pow(BACKOFF_MULTIPLIER, retryCount), MAX_DELAY_MS);
  return baseDelay + Math.random() * 1000;
}

interface SessionEventsOptions {
  sessionId: string | null;
  visibility: string | undefined;
  currentUserId: string | undefined;
  onRemoteMessage: (message: ChatMessage) => void;
  onProcessingChange: (isProcessing: boolean) => void;
  forceConnect?: boolean;
  onSessionExpired?: () => void;
}

export function useSessionEvents({
  sessionId,
  visibility,
  currentUserId,
  onRemoteMessage,
  onProcessingChange,
  forceConnect = false,
  onSessionExpired,
}: SessionEventsOptions) {
  const { token, refreshToken } = useAuth();
  const abortControllerRef = useRef<AbortController | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryCountRef = useRef(0);
  const isMountedRef = useRef(true);

  const clearReconnectTimeout = useCallback(() => {
    if (reconnectTimeoutRef.current !== null) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  const disconnect = useCallback(() => {
    clearReconnectTimeout();
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, [clearReconnectTimeout]);

  useEffect(() => {
    isMountedRef.current = true;

    const isGroupOrTenant = visibility && visibility !== 'private';
    const shouldConnect = sessionId && token && (isGroupOrTenant || forceConnect);

    if (!shouldConnect) {
      disconnect();
      return;
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    // Synthesis message tracking (local to this connection lifecycle)
    let synthSessionMsgId: string | null = null;
    let seenOrchestratorStart = false;

    const connect = async (authToken: string) => {
      if (!isMountedRef.current || abortController.signal.aborted) return;

      try {
        const response = await fetch(
          `${config.apiURL}/api/chat/${sessionId}/events`,
          {
            headers: {
              'Authorization': `Bearer ${authToken}`,
              'Accept': 'text/event-stream',
            },
            signal: abortController.signal,
          },
        );

        if (response.status === 401) {
          const newToken = await refreshToken(5);
          if (!newToken || !isMountedRef.current) {
            onSessionExpired?.();
            return;
          }
          const retryResponse = await fetch(
            `${config.apiURL}/api/chat/${sessionId}/events`,
            {
              headers: {
                'Authorization': `Bearer ${newToken}`,
                'Accept': 'text/event-stream',
              },
              signal: abortController.signal,
            },
          );
          if (retryResponse.status === 401) {
            onSessionExpired?.();
            return;
          }
          if (!retryResponse.ok || !retryResponse.body) {
            console.warn(`[SessionEvents] Failed to connect after refresh: ${retryResponse.status}`);
            scheduleReconnect();
            return;
          }
          retryCountRef.current = 0;
          await processStream(retryResponse.body.getReader());
          return;
        }

        if (!response.ok || !response.body) {
          console.warn(`[SessionEvents] Failed to connect: ${response.status}`);
          scheduleReconnect();
          return;
        }

        retryCountRef.current = 0;
        await processStream(response.body.getReader());
      } catch (error) {
        if ((error as Error).name === 'AbortError') return;
        console.warn('[SessionEvents] Connection error:', error);
        scheduleReconnect();
      }
    };

    const processStream = async (reader: ReadableStreamDefaultReader<Uint8Array>) => {
      const decoder = new TextDecoder();
      let buffer = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith(':')) continue;

            if (line.startsWith('data: ')) {
              try {
                const data: StreamEventData = JSON.parse(line.slice(6));
                handleEvent(data);
              } catch {
                // Ignore parse errors for non-JSON lines
              }
            }
          }
        }

        if (isMountedRef.current && !abortController.signal.aborted) {
          scheduleReconnect();
        }
      } catch (error) {
        if ((error as Error).name === 'AbortError') return;
        console.warn('[SessionEvents] Stream read error:', error);
        scheduleReconnect();
      }
    };

    /**
     * Route a single SSE event to the appropriate store/callback.
     * Mirrors the event handling in useChatStreaming but operates
     * as a viewer (no POST request, no thinkingId).
     *
     * When the LOCAL user is streaming (isProcessing === true),
     * useChatStreaming already handles orchestrator/synthesis events
     * via the POST /stream response. We must skip them here to
     * avoid double-appending synthesis chunks and duplicate messages.
     */
    const handleEvent = (data: StreamEventData) => {
      const store = useChatStore.getState();

      // Guard: skip orchestrator/synthesis events when the local user
      // owns the active stream — useChatStreaming handles them already.
      if (store.isProcessing) {
        const PASSTHROUGH_TYPES = new Set([
          'user_message',
          'processing_started',
          'processing_complete',
          'session_expired',
          'keepalive',
        ]);
        if (!PASSTHROUGH_TYPES.has(data.type)) {
          return;
        }
      }

      switch (data.type) {
        // ============================================================
        // War room events (original useSessionEvents functionality)
        // ============================================================
        case 'user_message': {
          if (data.sender_id === currentUserId) break;
          onRemoteMessage({
            id: `remote-user-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            role: 'user',
            content: data.content || '',
            senderName: data.sender_name || undefined,
            senderId: data.sender_id || undefined,
            timestamp: new Date(),
          });
          break;
        }

        case 'processing_started': {
          onProcessingChange(true);
          break;
        }

        case 'processing_complete': {
          onProcessingChange(false);
          synthSessionMsgId = null;
          break;
        }

        // ============================================================
        // Orchestrator lifecycle events
        // ============================================================
        case 'orchestrator_start':
        case 'iteration_start':
        case 'dispatch_start':
        case 'early_findings':
        case 'iteration_complete':
        case 'synthesis_start':
        case 'orchestrator_complete': {
          store.addOrchestratorEvent({
            ...data,
            _arrivalTime: Date.now(),
          } as OrchestratorEvent & { _arrivalTime: number });

          if (data.type === 'orchestrator_start') {
            seenOrchestratorStart = true;
            store.startInvestigation(Date.now());
            store.setOrchestratorActive(true);
            store.resetSynthesis();
            store.setRequestStartTime(Date.now());
            store.announce('Agent started investigation', 'polite');
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
              const names = (connectors as Array<{ name: string }>).map((c) => c.name).join(', ');
              store.announce(`Querying ${names}`, 'polite');
            }
          } else if (data.type === 'synthesis_start') {
            store.announce('Synthesizing findings', 'polite');
          } else if (data.type === 'orchestrator_complete') {
            store.announce('Investigation complete', 'polite');
          }
          break;
        }

        // ============================================================
        // Agent-scoped events (investigation tracking)
        // ============================================================
        case 'agent_event': {
          if (!seenOrchestratorStart) {
            store.startInvestigation(Date.now());
            store.setOrchestratorActive(true);
            store.setRequestStartTime(Date.now());
            seenOrchestratorStart = true;
          }

          const agentSource = data.agent_source;
          const innerEvent = data.inner_event;
          const connectorId = agentSource?.connector_id;
          const connectorName = agentSource?.connector_name || 'Unknown';
          const arrivalTime = Date.now();

          store.addOrchestratorEvent({
            ...data,
            _arrivalTime: arrivalTime,
          } as OrchestratorEvent & { _arrivalTime: number });

          if (connectorId && innerEvent) {
            const iteration = agentSource?.iteration ?? store.currentIteration;
            store.registerConnector(connectorId, connectorName, iteration);

            const innerType = innerEvent.type;
            const innerData = innerEvent.data || {};
            const stepId = `step-${arrivalTime}-${Math.random().toString(36).slice(2, 7)}`;

            if (innerType === 'thought') {
              store.addStep(connectorId, iteration, {
                id: stepId,
                type: 'thought',
                status: 'success',
                reasoning: String(innerData.content || innerData.message || ''),
                arrivalTime,
              });
            } else if (innerType === 'action' || innerType === 'tool_start') {
              const toolName = String(innerData.tool || innerData.name || 'tool');
              const args = (innerData.args || {}) as Record<string, unknown>;
              store.addStep(connectorId, iteration, {
                id: stepId,
                type: 'tool_call',
                toolName,
                targetEntity: parseTargetEntity(toolName, args),
                status: 'running',
                arrivalTime,
              });
            } else if (innerType === 'observation' || innerType === 'tool_complete') {
              const resultToolName = String(innerData.tool || innerData.name || '');
              const storeState = useChatStore.getState();
              const iterState = storeState.iterations.find((it) => it.iteration === iteration);
              const connState = iterState?.connectors.get(connectorId);
              if (connState) {
                const runningStep = [...connState.steps].reverse().find(
                  (s) => s.status === 'running' && (!resultToolName || s.toolName === resultToolName),
                );
                if (runningStep) {
                  const duration = arrivalTime - runningStep.arrivalTime;
                  const result = innerData.result ?? innerData.content;
                  storeState.updateStepStatus(connectorId, iteration, runningStep.id, 'success', duration);

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
                  useChatStore.setState({ iterations: updatedIterations });
                }
              }
            } else if (innerType === 'error') {
              store.addStep(connectorId, iteration, {
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
          const spConnId = data.connector_id;
          if (spConnId) {
            const currentIter = store.currentIteration || 1;
            store.registerConnector(spConnId, data.connector_name || 'Unknown', currentIter);
            store.addStep(spConnId, currentIter, {
              id: `step-progress-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
              type: 'tool_call',
              status: 'running',
              arrivalTime: Date.now(),
            });
          }
          break;
        }

        case 'budget_extended': {
          const beConnId = data.connector_id;
          if (beConnId) {
            const currentIter = store.currentIteration || 1;
            store.registerConnector(beConnId, data.connector_name || 'Unknown', currentIter);
          }
          break;
        }

        case 'connector_complete': {
          store.addOrchestratorEvent({
            ...data,
            _arrivalTime: Date.now(),
          } as OrchestratorEvent & { _arrivalTime: number });

          const ccId = data.connector_id;
          if (ccId) {
            const ccStatus = (data.status || 'success') as 'success' | 'failed' | 'timeout' | 'partial';
            store.updateConnectorStatus(ccId, ccStatus);
          }
          break;
        }

        // ============================================================
        // Synthesis & final answer
        // ============================================================
        case 'synthesis_chunk': {
          store.addOrchestratorEvent({
            ...data,
            _arrivalTime: Date.now(),
          } as OrchestratorEvent & { _arrivalTime: number });

          const chunk = data.content || '';
          if (!chunk) break;

          store.appendSynthesis(chunk);
          const afterAppend = useChatStore.getState();

          if (!synthSessionMsgId) {
            synthSessionMsgId = `synthesis-${sessionId}-${Date.now()}`;
            store.addMessage({
              id: synthSessionMsgId,
              role: 'assistant',
              content: afterAppend.synthesisAcc,
              timestamp: new Date(),
            });
            store.setSynthMessageCreated(true);
          } else {
            store.updateMessage(synthSessionMsgId, {
              content: afterAppend.synthesisAcc,
            });
          }
          break;
        }

        case 'final_answer': {
          const finalContent = data.content || '';
          const structured = parseSynthesis(finalContent);

          if (synthSessionMsgId) {
            // Snapshot investigation panes for the completed message
            const paneSnapshot = buildPaneSnapshot();
            store.updateMessage(synthSessionMsgId, {
              content: finalContent,
              ...(structured && { structuredContent: structured }),
              ...paneSnapshot,
            });
            store.resetSynthesis();
            store.resetOrchestrator();
          } else {
            const paneSnapshot = buildPaneSnapshot();
            store.addMessage({
              id: `assistant-${Date.now()}`,
              role: 'assistant',
              content: finalContent,
              timestamp: new Date(),
              ...(structured && { structuredContent: structured }),
              ...paneSnapshot,
            });
          }
          synthSessionMsgId = null;
          break;
        }

        // ============================================================
        // Investigation metadata events
        // ============================================================
        case 'hypothesis_update': {
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

        case 'follow_up_suggestions': {
          const suggestions = data.suggestions || [];
          if (Array.isArray(suggestions) && suggestions.length > 0) {
            store.setFollowUpSuggestions(suggestions);
          }
          break;
        }

        case 'citation_map': {
          const citations = data.citations || {};
          const messages = store.messages;
          const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
          if (lastAssistant) {
            const frontendCitations: Record<string, { stepId: string; connectorId: string; connectorName: string; connectorType: string; dataRef?: { table: string; session_id: string; row_count: number } }> = {};
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

        case 'usage_summary': {
          store.setUsageSummary({
            total_tokens: data.total_tokens ?? 0,
            effective_tokens: data.effective_tokens ?? 0,
            prompt_tokens: data.prompt_tokens ?? 0,
            completion_tokens: data.completion_tokens ?? 0,
            cache_read_tokens: data.cache_read_tokens ?? 0,
            cache_write_tokens: data.cache_write_tokens ?? 0,
            estimated_cost_usd: data.estimated_cost_usd ?? 0,
            llm_calls: data.llm_calls ?? 0,
          });
          break;
        }

        case 'context_usage': {
          store.setContextUsage({
            percentage: (data as Record<string, unknown>).percentage as number ?? 0,
            tokensUsed: (data as Record<string, unknown>).tokens_used as number ?? 0,
            tokensLimit: (data as Record<string, unknown>).tokens_limit as number ?? 200000,
          });
          break;
        }

        case 'error': {
          const errorMsg = data.message || 'Unknown error';
          store.addMessage({
            id: `error-${Date.now()}`,
            role: 'assistant',
            content: errorMsg,
            timestamp: new Date(),
            errorType: data.error_type || 'Error',
            errorSeverity: 'fatal',
          });
          break;
        }

        case 'approval_required': {
          if (data.approval_id) {
            store.pushApproval({
              approval_id: data.approval_id,
              tool: data.tool || '',
              danger_level: data.danger_level || 'dangerous',
              details: data.details || {},
              tool_args: data.tool_args || {},
              message: data.message || '',
            });
          }
          break;
        }

        case 'keepalive':
          break;

        default:
          break;
      }
    };

    /**
     * Build pane snapshot from current investigation state
     * for attaching to completed assistant messages.
     */
    const buildPaneSnapshot = () => {
      const storeState = useChatStore.getState();
      const paneSnapshot = new Map<string, {
        connectorId: string;
        connectorName: string;
        status: 'running' | 'complete' | 'error';
        isExpanded: boolean;
        currentStep: number;
        maxSteps: number;
        events: Array<{
          id: string;
          type: 'thought' | 'action';
          content: string;
          timestamp: Date;
          toolName?: string;
        }>;
        dataRefs?: Array<{ table: string; session_id: string; row_count: number }>;
      }>();

      for (const iter of storeState.iterations) {
        for (const [connId, connector] of iter.connectors) {
          paneSnapshot.set(connId, {
            connectorId: connector.connectorId,
            connectorName: connector.connectorName,
            status: connector.status === 'running' ? 'running' : connector.status === 'success' ? 'complete' : 'error',
            isExpanded: false,
            currentStep: connector.steps.length,
            maxSteps: connector.steps.length,
            events: connector.steps.map((step) => ({
              id: step.id,
              type: step.type === 'tool_call' ? 'action' as const : 'thought' as const,
              content: step.reasoning || step.observationSummary || step.toolName || '',
              timestamp: new Date(step.arrivalTime),
              toolName: step.toolName,
            })),
            dataRefs: undefined,
          });
        }
      }

      if (paneSnapshot.size === 0) return {};

      const connectorSources = Array.from(paneSnapshot.values()).map((pane) => ({
        id: pane.connectorId,
        name: pane.connectorName,
      }));

      storeState.resetInvestigation();

      return {
        agentPanes: paneSnapshot.size > 0 ? paneSnapshot : undefined,
        connectorSources: connectorSources.length > 1 ? connectorSources : undefined,
        orchestratorEvents: storeState.orchestratorEvents.length > 0
          ? [...storeState.orchestratorEvents]
          : undefined,
        requestStartTime: storeState.orchestratorEvents.length > 0
          ? storeState.requestStartTime
          : undefined,
      };
    };

    const scheduleReconnect = () => {
      if (!isMountedRef.current || abortController.signal.aborted) return;

      const delay = getRetryDelay(retryCountRef.current);
      retryCountRef.current += 1;

      reconnectTimeoutRef.current = setTimeout(async () => {
        if (!isMountedRef.current || abortController.signal.aborted) return;

        const currentToken = await refreshToken(30);
        if (currentToken && isMountedRef.current) {
          connect(currentToken);
        } else if (isMountedRef.current) {
          if (token) {
            connect(token);
          }
        }
      }, delay);
    };

    connect(token);

    return () => {
      isMountedRef.current = false;
      disconnect();
    };
  }, [sessionId, token, visibility, forceConnect, currentUserId, onRemoteMessage, onProcessingChange, onSessionExpired, disconnect, refreshToken]);

  return { disconnect };
}
