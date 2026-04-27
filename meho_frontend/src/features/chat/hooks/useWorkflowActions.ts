// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Workflow Actions Hook
 * 
 * NOTE: These workflow actions are deprecated. Workflow approval/rejection
 * is now handled via SSE streaming events in the chat.
 * 
 * This hook provides stub implementations for backwards compatibility
 * with the ChatMessageList component. Actions will show deprecation messages.
 */
import { useCallback, useState } from 'react';
import type { Plan } from '@/api/types';
import type { ChatMessage } from '../types';

interface UseWorkflowActionsProps {
  addMessage: (message: ChatMessage) => void;
}

export interface UseWorkflowActionsReturn {
  approveWorkflow: () => void;
  rejectWorkflow: () => void;
  editPlan: (editedPlan: Plan) => Promise<void>;
  cloneWorkflow: () => Promise<void>;
  retryWorkflow: () => Promise<void>;
  isApproving: boolean;
  isCancelling: boolean;
}

export function useWorkflowActions({
  addMessage,
}: UseWorkflowActionsProps): UseWorkflowActionsReturn {
  const [isApproving, setIsApproving] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);

  const showDeprecationMessage = useCallback((action: string) => {
    addMessage({
      id: `deprecated-${Date.now()}`,
      role: 'assistant',
      content: `⚠️ ${action} is no longer supported. Workflow approval is now handled automatically via chat streaming. Please start a new conversation.`,
      timestamp: new Date(),
    });
  }, [addMessage]);

  const approveWorkflow = useCallback(() => {
    setIsApproving(true);
    showDeprecationMessage('Manual workflow approval');
    setTimeout(() => setIsApproving(false), 1000);
  }, [showDeprecationMessage]);

  const rejectWorkflow = useCallback(() => {
    setIsCancelling(true);
    showDeprecationMessage('Manual workflow rejection');
    setTimeout(() => setIsCancelling(false), 1000);
  }, [showDeprecationMessage]);

  const editPlan = useCallback(async (_editedPlan: Plan) => {
    showDeprecationMessage('Plan editing');
  }, [showDeprecationMessage]);

  const cloneWorkflow = useCallback(async () => {
    showDeprecationMessage('Workflow cloning');
  }, [showDeprecationMessage]);

  const retryWorkflow = useCallback(async () => {
    showDeprecationMessage('Workflow retry');
  }, [showDeprecationMessage]);

  return {
    approveWorkflow,
    rejectWorkflow,
    editPlan,
    cloneWorkflow,
    retryWorkflow,
    isApproving,
    isCancelling,
  };
}
