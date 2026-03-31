// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Page - Main interaction with MEHO
 *
 * Features: SSE streaming via useChatStreaming hook, workflow approval,
 * execution monitoring, session persistence.
 *
 * Refactored (03.1-01): Delegates ALL SSE streaming to useChatStreaming hook.
 * No inline fetch/ReadableStream/manual SSE parsing. ChatPage is a clean state
 * manager that receives structured callbacks from the hook.
 *
 * Refactored (60-01): All state migrated to Zustand store. Zero useRef
 * stale-closure hacks -- streaming callbacks use useChatStore.getState().
 *
 * Refactored (68.2-02): Removed ChatLayout/ConnectionBanner/useConnectionManager/handleAgentPaneUpdate.
 * Scroll uses use-stick-to-bottom for proper streaming scroll behavior.
 */
import { useRef, useEffect, useCallback, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { AnimatePresence, motion } from 'motion/react';
import { ArrowDown } from 'lucide-react';
import { useStickToBottom } from 'use-stick-to-bottom';

import { getAPIClient, triggerSessionExpired } from '@/lib/api-client';
import { config } from '@/lib/config';
import { useAuth } from '@/contexts/AuthContext';
import { ChatSessionSidebar } from '@/components/ChatSessionSidebar';
import type { ChatSession } from '@/lib/api-client';
import { ApprovalModal } from '@/components/ApprovalModal';
import type { AuditEntry } from '@/components/AuditCard';
import type { Recipe } from '@/api/types/recipe';

// Feature imports
import {
  ChatHeader,
  ChatInput,
  ChatEmptyState,
  ChatMessageList,
  ContextBar,
} from '@/features/chat/components';
import {
  useChatStreaming,
  useWorkflowActions,
  useWorkflowPolling,
  useSessionEvents,
  useAutocomplete,
} from '@/features/chat/hooks';
import type { ChatMessage, AgentPaneState } from '@/features/chat/types';
import { TokenUsageBadge } from '@/features/observability/components/TokenUsageBadge';
import { useChatStore } from '@/features/chat/stores';

export function ChatPage() {
  // --- Zustand store selectors (render-triggering reads) ---
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const input = useChatStore((s) => s.input);
  const isProcessing = useChatStore((s) => s.isProcessing);
  const currentWorkflow = useChatStore((s) => s.currentWorkflow);
  const pendingApprovals = useChatStore((s) => s.pendingApprovals);
  const isApprovingAction = useChatStore((s) => s.isApprovingAction);
  const messages = useChatStore((s) => s.messages);
  const orchestratorEvents = useChatStore((s) => s.orchestratorEvents);
  const isOrchestratorActive = useChatStore((s) => s.isOrchestratorActive);
  const requestStartTime = useChatStore((s) => s.requestStartTime);
  const usageSummary = useChatStore((s) => s.usageSummary);
  const sessionVisibility = useChatStore((s) => s.sessionVisibility);
  const isWarRoomProcessing = useChatStore((s) => s.isWarRoomProcessing);
  const sessionIsActive = useChatStore((s) => s.sessionIsActive);
  const followUpSuggestions = useChatStore((s) => s.followUpSuggestions);
  const sessionMode = useChatStore((s) => s.sessionMode);
  const triggerSource = useChatStore((s) => s.triggerSource);

  // Phase 63: Textarea ref for cursor position tracking in autocomplete
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Phase 68.2-02: Stick-to-bottom scroll for streaming messages
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({
    resize: 'smooth',
    initial: 'instant',
  });

  // Phase 63-03: Pending recipe execution state (parameterized recipe form)
  const [pendingRecipe, setPendingRecipe] = useState<{
    recipe: Recipe;
    params: Record<string, string>;
  } | null>(null);

  // Phase 63-03: Recipe selection handler for / autocomplete
  const handleRecipeSelect = useCallback((recipe: Recipe) => {
    if (recipe.parameters && recipe.parameters.length > 0) {
      // Parameterized recipe: show inline form
      const defaultParams: Record<string, string> = {};
      for (const param of recipe.parameters) {
        defaultParams[param.name] = param.default != null ? String(param.default) : '';
      }
      setPendingRecipe({ recipe, params: defaultParams });
    } else {
      // No parameters: execute immediately by sending original_question
      handleSendMessage(recipe.original_question);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- handleSendMessage reads from store.getState()
  }, []);

  // Phase 63: Autocomplete hook (trigger detection, items, keyboard nav)
  const autocomplete = useAutocomplete(input, textareaRef, {
    onRecipeSelect: handleRecipeSelect,
  });

  // Phase 62: Breadcrumb chip click handler (scroll to reasoning segment)
  const handleBreadcrumbChipClick = useCallback((messageId: string, connectorName: string) => {
    const messageEl = document.querySelector(`[data-message-id="${messageId}"]`);
    if (!messageEl) return;

    // Find the connector segment by data attribute
    const segment = messageEl.querySelector(`[data-connector="${connectorName}"]`);
    if (segment) {
      segment.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, []);

  // Auth & API
  const { user } = useAuth();
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  // Streaming hook (03.1-01: sole streaming engine)
  const { streamChat, cancelStream } = useChatStreaming();

  // Phase 64-02: Announce context usage thresholds to screen readers
  const contextUsage = useChatStore((s) => s.contextUsage);
  const lastAnnouncedThresholdRef = useRef<number>(0);
  useEffect(() => {
    if (!contextUsage) return;
    const pct = contextUsage.percentage;
    const lastThreshold = lastAnnouncedThresholdRef.current;

    if (pct >= 90 && lastThreshold < 90) {
      useChatStore.getState().announce(
        'Warning: conversation context is nearly full. Consider starting a new chat.',
        'assertive',
      );
      lastAnnouncedThresholdRef.current = 90;
    } else if (pct >= 70 && pct < 90 && lastThreshold < 70) {
      useChatStore.getState().announce(
        `Context usage is at ${pct} percent.`,
        'polite',
      );
      lastAnnouncedThresholdRef.current = 70;
    }
  }, [contextUsage]);

  // Workflow polling hook
  const { startPolling, stopPolling } = useWorkflowPolling({
    sessionId: currentSessionId,
    onWorkflowUpdate: (workflow) => {
      useChatStore.getState().setCurrentWorkflow(workflow);
    },
    onMessagesLoaded: (loadedMessages: ChatMessage[]) => {
      useChatStore.getState().loadMessages(loadedMessages);
    },
    onComplete: () => {
      useChatStore.getState().setIsProcessing(false);
    },
  });

  // Workflow actions hook (deprecated - shows deprecation messages)
  const workflowActions = useWorkflowActions({
    addMessage: (msg: ChatMessage) => useChatStore.getState().addMessage(msg),
  });

  // Phase 39: War room session events (SSE subscription for group sessions)
  const handleRemoteMessage = useCallback((msg: ChatMessage) => {
    useChatStore.getState().addMessage(msg);
  }, []);

  const handleWarRoomProcessingChange = useCallback((processing: boolean) => {
    const store = useChatStore.getState();
    store.setIsWarRoomProcessing(processing);
    // When processing completes via SSE, also reset local processing state
    if (!processing) {
      store.setIsProcessing(false);
    }
  }, []);

  useSessionEvents({
    sessionId: currentSessionId,
    visibility: sessionVisibility,
    currentUserId: user?.sub,
    onRemoteMessage: handleRemoteMessage,
    onProcessingChange: handleWarRoomProcessingChange,
    forceConnect: sessionIsActive,  // Phase 59: Connect even for private sessions when active
    onSessionExpired: triggerSessionExpired,  // Phase 66: SSE auth -> re-auth modal
  });

  // Create session mutation
  const createSessionMutation = useMutation({
    mutationFn: () => apiClient.createSession(),
    onSuccess: (session) => {
      useChatStore.getState().setCurrentSessionId(session.id);
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
    },
  });

  // Fetch pending approvals from the DB and show the modal if any exist.
  // Shared across sidebar click, deep-link, and return-navigation paths.
  const hydrateApprovals = async (sessionId: string) => {
    const store = useChatStore.getState();
    try {
      const dbApprovals = await apiClient.getPendingApprovals(sessionId);
      store.setApprovals(
        dbApprovals.map((pa) => ({
          approval_id: pa.approval_id,
          tool: pa.tool_name,
          danger_level: pa.danger_level,
          details: {
            method: pa.method,
            path: pa.path,
            description: pa.description,
          },
          tool_args: pa.tool_args || {},
          message: pa.description || `Pending ${pa.tool_name} operation`,
        })),
      );
    } catch (err) {
      console.error('Failed to fetch pending approvals:', err);
    }
  };

  // Handle session selection
  const handleSelectSession = async (session: ChatSession | null) => {
    if (!session) {
      handleNewSession();
      return;
    }

    try {
      const store = useChatStore.getState();
      const sessionData = await apiClient.getSession(session.id);
      store.setCurrentSessionId(session.id);
      store.setSessionVisibility(session.visibility || 'private');
      // Phase 65-05: Restore session mode from backend
      store.setSessionMode((session as ChatSession).session_mode || 'agent');
      // Phase 75: Set trigger source for automation banner
      store.setTriggerSource((session as ChatSession).trigger_source ?? null);

      const loadedMessages: ChatMessage[] = sessionData.messages.map((msg) => ({
        id: msg.id,
        role: msg.role,
        content: msg.content,
        workflowId: msg.workflow_id || undefined,
        timestamp: new Date(msg.created_at),
        // War room sender attribution (Phase 39)
        senderName: msg.sender_name || undefined,
        senderId: msg.sender_id || undefined,
      }));

      store.loadMessages(loadedMessages);

      // Phase 59: Detect active investigation and set processing state
      const isActive = sessionData.is_active === true;
      store.setSessionIsActive(isActive);
      store.setIsProcessing(isActive);  // Show processing state if investigation is still running

      // Phase 39 gap closure: Initialize war room processing state from is_active
      const isGroupSession = (session.visibility || 'private') !== 'private';
      store.setIsWarRoomProcessing(isGroupSession && isActive);
      store.setCurrentWorkflow(null);

      // Phase 68.2-02: Scroll to bottom after loading messages
      setTimeout(() => scrollToBottom('instant'), 100);

      await hydrateApprovals(session.id);
    } catch (error) {
      console.error('Error loading session:', error);
    }
  };

  // Start new session
  const handleNewSession = () => {
    useChatStore.getState().resetAll();
  };

  // Deep-link: open session from ?session=<id> (e.g. event "View Session")
  // Phase 61: Also handle ?query=<text> (topology investigate prefill)
  useEffect(() => {
    const sessionParam = searchParams.get('session');
    const queryParam = searchParams.get('query');

    if (sessionParam && sessionParam !== currentSessionId) {
      (async () => {
        try {
          const store = useChatStore.getState();
          const sessionData = await apiClient.getSession(sessionParam);
          store.setCurrentSessionId(sessionParam);
          const vis = sessionData.visibility || 'private';
          store.setSessionVisibility(vis);
          store.setSessionMode(sessionData.session_mode || 'agent');
          // Phase 75: Set trigger source for automation banner
          store.setTriggerSource(sessionData.trigger_source ?? null);
          const loaded: ChatMessage[] = sessionData.messages.map((msg) => ({
            id: msg.id,
            role: msg.role,
            content: msg.content,
            workflowId: msg.workflow_id || undefined,
            timestamp: new Date(msg.created_at),
            senderName: msg.sender_name || undefined,
            senderId: msg.sender_id || undefined,
          }));
          store.loadMessages(loaded);

          // Set active/processing state so SSE events stream correctly
          const isActive = sessionData.is_active === true;
          store.setSessionIsActive(isActive);
          store.setIsProcessing(isActive);
          const isGroupSession = vis !== 'private';
          store.setIsWarRoomProcessing(isGroupSession && isActive);

          await hydrateApprovals(sessionParam);

          setSearchParams({}, { replace: true });
        } catch (err) {
          console.error('Failed to load session from URL:', err);
          setSearchParams({}, { replace: true });
        }
      })();
    } else if (queryParam) {
      // Prefill the chat input with the investigation query from topology
      useChatStore.getState().setInput(queryParam);
      setSearchParams({}, { replace: true });
    }
  }, [searchParams, apiClient, currentSessionId, setSearchParams]);

  // Re-hydrate pending approvals when returning to Chat page with a session
  // already loaded in Zustand (e.g. navigated to Settings and back).
  useEffect(() => {
    if (currentSessionId && !searchParams.get('session')) {
      hydrateApprovals(currentSessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // Handle send message (03.1-01: delegates streaming to useChatStreaming hook)
  // Phase 62: Optional messageOverride for follow-up chip click (bypasses input field)
  const handleSendMessage = async (messageOverride?: string) => {
    const store = useChatStore.getState();
    const messageContent = messageOverride || store.input;
    if (!messageContent.trim() || store.isProcessing || store.isWarRoomProcessing) return;

    // Phase 63-03: Clear pending recipe form on send
    setPendingRecipe(null);

    // Phase 62: Clear follow-up suggestions on any message send
    store.clearFollowUpSuggestions();

    // Phase 63-03: Read @mention from store for connector_id routing
    const mention = store.activeMention;
    let finalMessage = messageContent;
    let connectorId: string | undefined;

    if (mention) {
      // Strip the @connectorName prefix from the message
      const mentionPrefix = `@${mention.connectorName} `;
      if (finalMessage.startsWith(mentionPrefix)) {
        finalMessage = finalMessage.slice(mentionPrefix.length).trim();
      }
      connectorId = mention.connectorId;
      // Clear mention after capturing
      store.clearActiveMention();
    }

    // Reset state for new message
    store.setInput('');
    store.setIsProcessing(true);
    store.resetOrchestrator();
    store.resetInvestigation();
    store.setUsageSummary(null);

    // Track request timing
    const startTime = Date.now();
    store.setRequestStartTime(startTime);

    // Create session if needed
    let sessionId = store.currentSessionId;
    if (!sessionId) {
      try {
        const session = await createSessionMutation.mutateAsync();
        sessionId = session.id;
      } catch (error) {
        console.error('Error creating session:', error);
        useChatStore.getState().setIsProcessing(false);
        return;
      }
    }

    // Add user message with sender identity (Phase 39: war room attribution)
    // Phase 63-03: Attach mentionMetadata for pill rendering
    const userMessage: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: messageContent, // Show original text (with @name) in the bubble
      timestamp: new Date(),
      senderName: user?.name || user?.email || undefined,
      senderId: user?.sub || undefined,
      ...(mention && {
        mentionMetadata: {
          connectorId: mention.connectorId,
          connectorName: mention.connectorName,
          connectorType: mention.connectorType,
        },
      }),
    };
    useChatStore.getState().addMessage(userMessage);

    // Phase 39: Set war room processing state for group sessions
    const vis = useChatStore.getState().sessionVisibility;
    if (vis && vis !== 'private') {
      useChatStore.getState().setIsWarRoomProcessing(true);
    }

    // Stream via hook with structured callbacks
    // Phase 63-03: Pass connector_id for @mention routing and stripped message
    await streamChat({
      sessionId,
      message: finalMessage,
      connectorId,

      onThinking: () => {
        // No-op -- the hook adds the thinking message via onMessage
      },

      onMessage: (msg) => {
        const s = useChatStore.getState();

        // Helper: snapshot agent panes from store (stale-closure-free via getState)
        const snapshotPanes = () => {
          const storeState = useChatStore.getState();
          // Build AgentPaneState Map from iterations for backward compatibility
          const paneSnapshot = new Map<string, AgentPaneState>();
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

          if (paneSnapshot.size === 0) return { paneSnapshot: undefined, connectorSources: undefined, allDataRefs: undefined };

          // Extract connector sources for SourceTags
          const connectorSources = Array.from(paneSnapshot.values()).map((pane) => ({
            id: pane.connectorId,
            name: pane.connectorName,
          }));

          // Reset live investigation state
          useChatStore.getState().resetInvestigation();

          return {
            paneSnapshot: paneSnapshot.size > 0 ? paneSnapshot : undefined,
            connectorSources: connectorSources.length > 1 ? connectorSources : undefined,
            allDataRefs: undefined,
          };
        };

        // If synthesis message exists and this is the final answer, update it
        if (msg.role === 'assistant' && !msg.isProgressUpdate && s.synthMessageCreated) {
          const synthMsgId = `synthesis-${sessionId}`;
          const { paneSnapshot, connectorSources, allDataRefs } = snapshotPanes();
          const currentStore = useChatStore.getState();
          useChatStore.getState().updateMessage(synthMsgId, {
            content: msg.content,
            orchestratorEvents: currentStore.orchestratorEvents.length > 0
              ? [...currentStore.orchestratorEvents]
              : undefined,
            requestStartTime: currentStore.orchestratorEvents.length > 0
              ? currentStore.requestStartTime
              : undefined,
            agentPanes: paneSnapshot,
            connectorSources,
            dataRefs: allDataRefs,
          });
          // Clear state
          useChatStore.getState().resetSynthesis();
          useChatStore.getState().resetOrchestrator();
        } else if (s.orchestratorEvents.length > 0 && !msg.isProgressUpdate && msg.role === 'assistant') {
          // Non-synthesis final answer with orchestrator events -- attach them
          const { paneSnapshot, connectorSources, allDataRefs } = snapshotPanes();
          const currentStore = useChatStore.getState();
          useChatStore.getState().addMessage({
            ...msg,
            orchestratorEvents: [...currentStore.orchestratorEvents],
            requestStartTime: currentStore.requestStartTime,
            agentPanes: paneSnapshot,
            connectorSources,
            dataRefs: allDataRefs,
          });
          // Clear orchestrator state
          useChatStore.getState().resetOrchestrator();
        } else {
          useChatStore.getState().addMessage(msg);
        }
      },

      onUpdateMessage: (id, updates) => {
        const s = useChatStore.getState();
        // Handle synthesis chunk accumulation
        if (id.startsWith('thinking-') && updates.content && s.isOrchestratorActive) {
          s.appendSynthesis(updates.content);
          const synthMsgId = `synthesis-${sessionId}`;
          const afterAppend = useChatStore.getState();
          if (!afterAppend.synthMessageCreated) {
            // First chunk: remove thinking indicator, create synthesis message
            useChatStore.getState().removeMessage(id);
            useChatStore.getState().addMessage({
              id: synthMsgId,
              role: 'assistant',
              content: afterAppend.synthesisAcc,
              timestamp: new Date(),
              // Surface passthrough metadata for Wave 2
              ...(updates.passthrough && {
                passthrough: updates.passthrough,
                sourceConnector: updates.sourceConnector,
                sourceConnectorId: updates.sourceConnectorId,
              }),
            });
            useChatStore.getState().setSynthMessageCreated(true);
          } else {
            useChatStore.getState().updateMessage(synthMsgId, {
              content: afterAppend.synthesisAcc,
              // Keep passthrough metadata in sync
              ...(updates.passthrough && {
                passthrough: updates.passthrough,
                sourceConnector: updates.sourceConnector,
                sourceConnectorId: updates.sourceConnectorId,
              }),
            });
          }
        } else {
          useChatStore.getState().updateMessage(id, updates);
        }
      },

      onRemoveMessage: (id) => useChatStore.getState().removeMessage(id),

      onPlanReady: (plan, workflowId, requiresApproval) => {
        useChatStore.getState().setCurrentWorkflow({
          id: workflowId,
          goal: messageContent,
          status: requiresApproval ? 'WAITING_APPROVAL' : 'RUNNING',
          plan,
          result: null,
          tenant_id: '',
          user_id: '',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
        if (!requiresApproval) {
          startPolling(workflowId);
        }
      },

      onApprovalRequired: (approval) => {
        useChatStore.getState().pushApproval(approval);
      },

      onComplete: () => useChatStore.getState().setIsProcessing(false),

      onError: () => {
        // Error messages are already added by the hook via onMessage
        useChatStore.getState().setIsProcessing(false);
      },

      onStartPolling: startPolling,

      onAuditEntry: (entry: AuditEntry) => {
        // Add audit card as a special message in the chat
        const auditMessage: ChatMessage = {
          id: `audit-${Date.now()}`,
          role: 'assistant',
          content: '',
          timestamp: new Date(),
          auditEntry: entry,
        };
        useChatStore.getState().addMessage(auditMessage);
      },

      onUsageSummary: (summary) => useChatStore.getState().setUsageSummary(summary),

      onOrchestratorEvent: (event) => {
        const s = useChatStore.getState();
        s.addOrchestratorEvent(event);
        // Detect orchestrator mode start
        if (event.type === 'orchestrator_start') {
          useChatStore.getState().setOrchestratorActive(true);
          useChatStore.getState().resetSynthesis();
          useChatStore.getState().startInvestigation(Date.now());
        }
        // Track iterations
        if (event.type === 'iteration_start' && 'data' in event) {
          const iterData = event.data as { iteration?: number };
          if (iterData.iteration !== undefined) {
            useChatStore.getState().addIteration(iterData.iteration);
          }
        }
      },
    });
  };

  // Handle key press
  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  // Phase 65-05: Handle mode toggle change and persist to backend
  const handleSessionModeChange = useCallback((mode: 'ask' | 'agent') => {
    useChatStore.getState().setSessionMode(mode);
    const sessionId = useChatStore.getState().currentSessionId;
    if (sessionId) {
      const apiClient = getAPIClient();
      apiClient.updateSessionMode(sessionId, mode).catch((err) => {
        console.error('Failed to persist session mode:', err);
      });
    }
  }, []);

  // Phase 62: Follow-up chip click handler
  const handleFollowUpClick = useCallback((suggestion: string) => {
    handleSendMessage(suggestion);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- handleSendMessage reads from store.getState(), no stale closure risk
  }, []);

  // Phase 63-02: Start new chat with investigation summary handoff
  const handleStartNewChat = useCallback(async () => {
    const store = useChatStore.getState();
    const sessionId = store.currentSessionId;
    if (!sessionId) return;

    try {
      const result = await apiClient.summarizeSession(sessionId);
      // Navigate to the new session
      store.setCurrentSessionId(result.new_session_id);
      store.setContextUsage(null);
      store.setSessionMode('agent');  // Phase 66: Reset to default mode on new chat
      // Reload the new session's messages
      const sessionData = await apiClient.getSession(result.new_session_id);
      const loadedMessages: ChatMessage[] = sessionData.messages.map((msg) => ({
        id: msg.id,
        role: msg.role,
        content: msg.content,
        timestamp: new Date(msg.created_at),
      }));
      store.loadMessages(loadedMessages);
      // Refresh session list
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
    } catch (error) {
      console.error('Failed to summarize session:', error);
      store.addMessage({
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Failed to start new chat: ${error instanceof Error ? error.message : 'Unknown error'}`,
        timestamp: new Date(),
      });
    }
  }, [apiClient, queryClient]);

  // Handle action approval (Phase 5: no re-send, agent resumes in same SSE stream)
  const handleApproveAction = async (approvalId: string) => {
    if (!currentSessionId) return;
    useChatStore.getState().setIsApprovingAction(true);
    try {
      await apiClient.approveAction(currentSessionId, approvalId);
    } catch (error: unknown) {
      console.error('Failed to approve action:', error);
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        || (error instanceof Error ? error.message : 'Unknown error');
      useChatStore.getState().addMessage({
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Failed to approve action: ${detail}`,
        timestamp: new Date(),
      });
    } finally {
      useChatStore.getState().setIsApprovingAction(false);
      useChatStore.getState().removeApproval(approvalId);
    }
  };

  // Handle action rejection (Phase 5: agent continues reasoning in same SSE stream)
  const handleRejectAction = async (approvalId: string) => {
    if (!currentSessionId) return;
    useChatStore.getState().setIsApprovingAction(true);
    try {
      await apiClient.rejectAction(currentSessionId, approvalId);
    } catch (error: unknown) {
      console.error('Failed to reject action:', error);
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        || (error instanceof Error ? error.message : 'Unknown error');
      useChatStore.getState().addMessage({
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Failed to reject action: ${detail}`,
        timestamp: new Date(),
      });
    } finally {
      useChatStore.getState().setIsApprovingAction(false);
      useChatStore.getState().removeApproval(approvalId);
    }
  };

  return (
    <div className="h-full flex bg-background overflow-hidden">
      <h1 className="sr-only">Chat</h1>
      {/* Session Sidebar */}
      <ChatSessionSidebar
        currentSessionId={currentSessionId}
        onSelectSession={handleSelectSession}
        onNewSession={handleNewSession}
      />

      {/* Main Chat Area (no ChatLayout wrapper -- direct flex layout) */}
      <div className="flex-1 flex flex-col min-w-0">
        <ChatHeader
          sessionId={currentSessionId}
          visibility={sessionVisibility}
          onVisibilityChange={(v) => useChatStore.getState().setSessionVisibility(v)}
          triggerSource={triggerSource}
        />

        {/* Messages area with stick-to-bottom scroll */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto overflow-x-hidden p-6 scrollbar-hide relative">
          {/* Background Ambient Glow -- fixed positioning so it doesn't affect content height */}
          <div className="absolute inset-0 overflow-hidden pointer-events-none fixed">
            <div className="absolute top-[20%] right-[10%] w-[30%] h-[30%] rounded-full bg-primary/5 blur-[100px]" />
            <div className="absolute bottom-[20%] left-[10%] w-[30%] h-[30%] rounded-full bg-accent/5 blur-[100px]" />
          </div>

          <div ref={contentRef} className="max-w-4xl mx-auto relative z-10 pb-4">
            <AnimatePresence mode="wait">
              {messages.length === 0 && (
                <ChatEmptyState onSuggestionClick={(s) => useChatStore.getState().setInput(s)} />
              )}
            </AnimatePresence>

            <ChatMessageList
              messages={messages}
              currentWorkflow={currentWorkflow}
              isProcessing={isProcessing}
              isApproving={workflowActions.isApproving}
              onApprove={workflowActions.approveWorkflow}
              onReject={workflowActions.rejectWorkflow}
              onEditPlan={workflowActions.editPlan}
              onCloneWorkflow={workflowActions.cloneWorkflow}
              onRetryWorkflow={workflowActions.retryWorkflow}
              liveEventsStartTime={requestStartTime}
              orchestratorEvents={orchestratorEvents}
              isOrchestratorActive={isOrchestratorActive}
              followUpSuggestions={followUpSuggestions}
              onFollowUpClick={handleFollowUpClick}
              onBreadcrumbChipClick={handleBreadcrumbChipClick}
            />

            {/* Token usage badge (OBSV-05: surfaces cost after conversation) */}
            {usageSummary && (
              <div className="flex justify-end px-4 pb-2">
                <TokenUsageBadge
                  usage={{
                    total_tokens: usageSummary.total_tokens,
                    prompt_tokens: usageSummary.prompt_tokens,
                    completion_tokens: usageSummary.completion_tokens,
                    estimated_cost_usd: usageSummary.estimated_cost_usd,
                  }}
                  effectiveTokens={usageSummary.effective_tokens}
                  size="sm"
                />
              </div>
            )}
          </div>

          {/* Floating scroll-to-bottom button */}
          <AnimatePresence>
            {!isAtBottom && (
              <motion.button
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.8 }}
                transition={{ duration: 0.15 }}
                onClick={() => scrollToBottom()}
                className="sticky bottom-4 mx-auto block w-8 h-8 rounded-full bg-surface border border-border shadow-lg flex items-center justify-center text-text-secondary hover:text-text-primary hover:bg-surface-hover transition-colors"
                aria-label="Scroll to bottom"
              >
                <ArrowDown className="w-4 h-4" />
              </motion.button>
            )}
          </AnimatePresence>
        </div>

        {/* Phase 63-02: Context usage bar + warning banner above input */}
        <ContextBar onStartNewChat={handleStartNewChat} />

        {/* Phase 63-03: Recipe parameter form (shown when selecting a parameterized recipe) */}
        {pendingRecipe && (
          <div className="px-6 pb-2">
            <div className="max-w-4xl mx-auto">
              <div className="bg-surface border border-white/10 rounded-xl p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-medium text-text-primary">
                    {pendingRecipe.recipe.name}
                  </h4>
                  <button
                    onClick={() => setPendingRecipe(null)}
                    className="text-xs text-text-tertiary hover:text-text-secondary"
                  >
                    Cancel
                  </button>
                </div>
                {pendingRecipe.recipe.description && (
                  <p className="text-xs text-text-tertiary">{pendingRecipe.recipe.description}</p>
                )}
                {pendingRecipe.recipe.parameters.map((param) => (
                  <div key={param.name} className="space-y-1">
                    <label htmlFor={`recipe-param-${param.name}`} className="text-xs text-text-secondary font-medium">
                      {param.name}
                      {param.required && <span className="text-red-400 ml-0.5">*</span>}
                    </label>
                    {param.description && (
                      <p className="text-[10px] text-text-tertiary">{param.description}</p>
                    )}
                    <input
                      id={`recipe-param-${param.name}`}
                      type="text"
                      value={pendingRecipe.params[param.name] ?? ''}
                      onChange={(e) =>
                        setPendingRecipe((prev) =>
                          prev
                            ? { ...prev, params: { ...prev.params, [param.name]: e.target.value } }
                            : null,
                        )
                      }
                      placeholder={param.default != null ? String(param.default) : param.name}
                      className="w-full bg-background border border-white/10 rounded-lg px-3 py-1.5 text-sm text-text-primary placeholder-text-tertiary focus:border-primary/50 focus:outline-none"
                    />
                  </div>
                ))}
                <button
                  onClick={() => {
                    if (!pendingRecipe) return;
                    // Interpolate parameters into the recipe's original_question
                    let query = pendingRecipe.recipe.original_question;
                    for (const [key, value] of Object.entries(pendingRecipe.params)) {
                      query = query.replace(new RegExp(`\\{\\{${key}\\}\\}`, 'g'), value);
                    }
                    setPendingRecipe(null);
                    handleSendMessage(query);
                  }}
                  className="w-full py-2 bg-primary text-white text-sm font-medium rounded-lg hover:bg-primary/90 transition-colors"
                >
                  Run Recipe
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Input area - disabled when approval modal is showing or war room processing */}
        <ChatInput
          value={input}
          onChange={(v) => useChatStore.getState().setInput(v)}
          onSend={handleSendMessage}
          onStop={() => {
            cancelStream();
            useChatStore.getState().setIsProcessing(false);
          }}
          onKeyPress={handleKeyPress}
          isProcessing={isProcessing}
          disabled={pendingApprovals.length > 0}
          isWarRoomProcessing={isWarRoomProcessing}
          userName={user?.name || user?.email}
          textareaRef={textareaRef}
          autocompleteItems={autocomplete.items}
          autocompleteSelectedIndex={autocomplete.selectedIndex}
          autocompleteVisible={autocomplete.isOpen}
          autocompleteTriggerType={autocomplete.trigger?.type ?? null}
          onAutocompleteSelect={autocomplete.selectItem}
          onAutocompleteKeyDown={autocomplete.handleKeyDown}
          sessionMode={sessionMode}
          onSessionModeChange={handleSessionModeChange}
        />
      </div>

      {/* Approval Modal (shows first in queue, next auto-appears after resolve) */}
      {pendingApprovals.length > 0 && (
        <ApprovalModal
          approval={pendingApprovals[0]}
          onApprove={handleApproveAction}
          onReject={handleRejectAction}
          isProcessing={isApprovingAction}
          queuePosition={1}
          queueTotal={pendingApprovals.length}
        />
      )}
    </div>
  );
}
