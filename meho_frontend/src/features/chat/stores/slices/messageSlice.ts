// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Message Slice
 *
 * Manages chat messages with CRUD operations and deduplication.
 * Imports utility functions from useChatMessages hook (no duplication).
 */
import type { StateCreator } from 'zustand';
import type { ChatStore } from '../useChatStore';
import type { ChatMessage } from '../../types';
import { deduplicateMessages } from '../../hooks/useChatMessages';

export interface MessageSlice {
  messages: ChatMessage[];
  addMessage: (message: ChatMessage) => void;
  updateMessage: (id: string, updates: Partial<ChatMessage>) => void;
  removeMessage: (id: string) => void;
  clearMessages: () => void;
  loadMessages: (msgs: ChatMessage[]) => void;
  mergeNewMessages: (incoming: ChatMessage[]) => void;
}

export const createMessageSlice: StateCreator<
  ChatStore,
  [['zustand/devtools', never]],
  [],
  MessageSlice
> = (set) => ({
  messages: [],

  addMessage: (message) =>
    set(
      (state) => ({ messages: [...state.messages, message] }),
      false,
      'messages/add',
    ),

  updateMessage: (id, updates) =>
    set(
      (state) => ({
        messages: state.messages.map((msg) =>
          msg.id === id ? { ...msg, ...updates } : msg,
        ),
      }),
      false,
      'messages/update',
    ),

  removeMessage: (id) =>
    set(
      (state) => ({
        messages: state.messages.filter((msg) => msg.id !== id),
      }),
      false,
      'messages/remove',
    ),

  clearMessages: () => set({ messages: [] }, false, 'messages/clear'),

  loadMessages: (msgs) =>
    set({ messages: deduplicateMessages(msgs) }, false, 'messages/load'),

  mergeNewMessages: (incoming) =>
    set(
      (state) => {
        const existingIds = new Set(state.messages.map((m) => m.id));
        const newMsgs = incoming.filter((m) => !existingIds.has(m.id));
        return { messages: [...state.messages, ...newMsgs] };
      },
      false,
      'messages/merge',
    ),
});
