// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Sessions Hook
 * 
 * Manages chat session lifecycle: creating, loading, and switching sessions.
 */
import { useState, useCallback } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import type { ChatSession, ChatMessage as APIChatMessage } from '@/api/types';
import type { ChatMessage } from '../types';

export function useChatSessions() {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Deduplicate consecutive identical assistant messages
  const deduplicateMessages = useCallback((msgs: ChatMessage[]): ChatMessage[] => {
    return msgs.filter((msg, index) => {
      if (index === 0) return true;
      const prevMsg = msgs[index - 1];
      if (msg.role === 'assistant' && prevMsg.role === 'assistant' && msg.content === prevMsg.content) {
        return false;
      }
      return true;
    });
  }, []);

  // Create session mutation
  const createSessionMutation = useMutation({
    mutationFn: () => apiClient.createSession(),
    onSuccess: (session) => {
      setCurrentSessionId(session.id);
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] });
    },
  });

  // Save message to session mutation
  const saveMessageMutation = useMutation({
    mutationFn: ({ sessionId, role, content, workflowId }: {
      sessionId: string;
      role: 'user' | 'assistant';
      content: string;
      workflowId?: string;
    }) => apiClient.addMessageToSession(sessionId, { role, content, workflow_id: workflowId }),
  });

  // Load session when selected
  const selectSession = useCallback(async (session: ChatSession | null) => {
    if (!session) {
      setCurrentSessionId(null);
      setMessages([]);
      return;
    }

    try {
      const sessionData = await apiClient.getSession(session.id);
      setCurrentSessionId(session.id);

      const loadedMessages: ChatMessage[] = sessionData.messages.map((msg: APIChatMessage) => ({
        id: msg.id,
        role: msg.role,
        content: msg.content,
        workflowId: msg.workflow_id || undefined,
        timestamp: new Date(msg.created_at),
      }));

      setMessages(deduplicateMessages(loadedMessages));
    } catch (error) {
      console.error('Error loading session:', error);
    }
  }, [apiClient, deduplicateMessages]);

  // Start a new chat session
  const startNewSession = useCallback(() => {
    setCurrentSessionId(null);
    setMessages([]);
  }, []);

  // Add a message locally
  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((prev) => [...prev, message]);
  }, []);

  // Update a message by ID
  const updateMessage = useCallback((id: string, updates: Partial<ChatMessage>) => {
    setMessages((prev) =>
      prev.map((msg) => (msg.id === id ? { ...msg, ...updates } : msg))
    );
  }, []);

  // Remove a message by ID
  const removeMessage = useCallback((id: string) => {
    setMessages((prev) => prev.filter((msg) => msg.id !== id));
  }, []);

  // Create session if needed
  const ensureSession = useCallback(async (): Promise<string | null> => {
    if (currentSessionId) return currentSessionId;
    
    try {
      const session = await createSessionMutation.mutateAsync();
      return session.id;
    } catch (error) {
      console.error('Error creating session:', error);
      return null;
    }
  }, [currentSessionId, createSessionMutation]);

  return {
    currentSessionId,
    messages,
    setMessages,
    selectSession,
    startNewSession,
    addMessage,
    updateMessage,
    removeMessage,
    ensureSession,
    saveMessage: saveMessageMutation.mutate,
    deduplicateMessages,
  };
}

