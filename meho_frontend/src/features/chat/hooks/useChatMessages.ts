// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Messages Hook
 * 
 * Manages local message state with deduplication and CRUD operations.
 * Messages are synced with backend via session API.
 */
import { useState, useCallback } from 'react';
import type { ChatMessage } from '../types';

/**
 * Deduplicate consecutive assistant messages with identical content
 */
export const deduplicateMessages = (messages: ChatMessage[]): ChatMessage[] => {
  return messages.filter((msg, index) => {
    if (index === 0) return true;
    const prevMsg = messages[index - 1];
    // Only deduplicate assistant messages with identical content
    if (msg.role === 'assistant' && prevMsg.role === 'assistant' && msg.content === prevMsg.content) {
      return false;
    }
    return true;
  });
};

/**
 * Phase 59: Deduplicate messages by ID (for merge-on-return scenarios)
 */
export const deduplicateById = (messages: ChatMessage[]): ChatMessage[] => {
  const seen = new Set<string>();
  return messages.filter((msg) => {
    if (seen.has(msg.id)) return false;
    seen.add(msg.id);
    return true;
  });
};

/**
 * Phase 59: Merge DB-loaded messages with existing local messages.
 * DB messages take precedence; only local messages with unseen IDs are appended.
 */
export const mergeMessages = (
  dbMessages: ChatMessage[],
  localMessages: ChatMessage[],
): ChatMessage[] => {
  const dbIds = new Set(dbMessages.map((m) => m.id));
  const newLocal = localMessages.filter((m) => !dbIds.has(m.id));
  return deduplicateMessages([...dbMessages, ...newLocal]);
};

export interface UseChatMessagesReturn {
  messages: ChatMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>;
  addMessage: (message: ChatMessage) => void;
  updateMessage: (id: string, updates: Partial<ChatMessage>) => void;
  removeMessage: (id: string) => void;
  clearMessages: () => void;
  loadMessages: (messages: ChatMessage[]) => void;
  mergeNewMessages: (incoming: ChatMessage[]) => void;
}

export function useChatMessages(): UseChatMessagesReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessages((prev) => [...prev, message]);
  }, []);

  const updateMessage = useCallback((id: string, updates: Partial<ChatMessage>) => {
    setMessages((prev) =>
      prev.map((msg) => (msg.id === id ? { ...msg, ...updates } : msg))
    );
  }, []);

  const removeMessage = useCallback((id: string) => {
    setMessages((prev) => prev.filter((msg) => msg.id !== id));
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  const loadMessages = useCallback((newMessages: ChatMessage[]) => {
    setMessages(deduplicateMessages(newMessages));
  }, []);

  /**
   * Phase 59: Merge incoming messages with existing state (for live SSE merge).
   * Only appends messages with IDs not already in state.
   */
  const mergeNewMessages = useCallback((incoming: ChatMessage[]) => {
    setMessages((prev) => {
      const existingIds = new Set(prev.map((m) => m.id));
      const newMsgs = incoming.filter((m) => !existingIds.has(m.id));
      return [...prev, ...newMsgs];
    });
  }, []);

  return {
    messages,
    setMessages,
    addMessage,
    updateMessage,
    removeMessage,
    clearMessages,
    loadMessages,
    mergeNewMessages,
  };
}

