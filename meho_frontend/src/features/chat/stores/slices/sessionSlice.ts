// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Session Slice
 *
 * Manages session identity state: current session ID, visibility, and active status.
 */
import type { StateCreator } from 'zustand';
import type { ChatStore } from '../useChatStore';

export type SessionMode = 'ask' | 'agent';

export interface SessionSlice {
  currentSessionId: string | null;
  sessionVisibility: string | undefined;
  sessionIsActive: boolean;
  sessionMode: SessionMode;
  triggerSource: string | null;  // Phase 75: automation trigger source for banner display
  setCurrentSessionId: (id: string | null) => void;
  setSessionVisibility: (v: string | undefined) => void;
  setSessionIsActive: (active: boolean) => void;
  setSessionMode: (mode: SessionMode) => void;
  setTriggerSource: (source: string | null) => void;
  resetSession: () => void;
}

export const createSessionSlice: StateCreator<
  ChatStore,
  [['zustand/devtools', never]],
  [],
  SessionSlice
> = (set) => ({
  currentSessionId: null,
  sessionVisibility: undefined,
  sessionIsActive: false,
  sessionMode: 'agent',
  triggerSource: null,
  setCurrentSessionId: (id) => set({ currentSessionId: id }, false, 'session/setId'),
  setSessionVisibility: (v) => set({ sessionVisibility: v }, false, 'session/setVisibility'),
  setSessionIsActive: (active) => set({ sessionIsActive: active }, false, 'session/setIsActive'),
  setSessionMode: (mode) => set({ sessionMode: mode }, false, 'session/setMode'),
  setTriggerSource: (source) => set({ triggerSource: source }, false, 'session/setTriggerSource'),
  resetSession: () =>
    set(
      {
        currentSessionId: null,
        sessionVisibility: undefined,
        sessionIsActive: false,
        sessionMode: 'agent',
        triggerSource: null,
      },
      false,
      'session/reset',
    ),
});
