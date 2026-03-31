// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Workflow Polling Hook
 * 
 * NOTE: Workflow polling is no longer used as workflow state is managed via SSE streaming.
 * This hook is kept for backwards compatibility but will be removed in a future refactor.
 * 
 * The chat streaming now handles:
 * - Workflow creation and status updates
 * - Approval requests via SSE events
 * - Completion notifications
 */
import { useCallback, useRef } from 'react';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import type { Workflow, ChatMessage as APIChatMessage } from '@/api/types';
import type { ChatMessage } from '../types';

interface PollingOptions {
  sessionId: string | null;
  onWorkflowUpdate: (workflow: Workflow) => void;
  onMessagesLoaded: (messages: ChatMessage[]) => void;
  onComplete: () => void;
}


export function useWorkflowPolling(options: PollingOptions) {
  const { sessionId, onMessagesLoaded, onComplete } = options;
  const pollIntervalRef = useRef<number | null>(null);
  const apiClient = getAPIClient(config.apiURL);

  const startPolling = useCallback((workflowId: string) => {
    // Workflow polling is deprecated - workflow state is now managed via SSE streaming
    // This function now just reloads session messages after a short delay
    console.warn(`startPolling(${workflowId}) is deprecated - workflow state is managed via SSE`);
    
    // Clear any existing poll
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
    }

    // Just reload session messages after a delay
    setTimeout(async () => {
      if (sessionId) {
        try {
          const sessionData = await apiClient.getSession(sessionId);
          const loadedMessages: ChatMessage[] = sessionData.messages.map((msg: APIChatMessage) => ({
            id: msg.id,
            role: msg.role,
            content: msg.content,
            workflowId: msg.workflow_id || undefined,
            timestamp: new Date(msg.created_at),
          }));
          onMessagesLoaded(loadedMessages);
        } catch (error) {
          console.error('Error reloading session:', error);
        }
      }
      onComplete();
    }, 2000);
  }, [sessionId, apiClient, onMessagesLoaded, onComplete]);

  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }, []);

  return {
    startPolling,
    stopPolling,
  };
}
