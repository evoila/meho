// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Combined Zustand Chat Store
 *
 * Single store composed of 4 typed slices, replacing all ChatPage useState
 * and useRef stale-closure hacks. Streaming callbacks use getState() for
 * synchronous access to current state without React render cycle.
 *
 * Slices: session, messages, orchestrator (includes investigation tracking), ui
 */
import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { createSessionSlice, type SessionSlice } from './slices/sessionSlice';
import { createMessageSlice, type MessageSlice } from './slices/messageSlice';
import { createOrchestratorSlice, type OrchestratorSlice } from './slices/orchestratorSlice';
import { createUISlice, type UISlice } from './slices/uiSlice';

export type ChatStore = SessionSlice &
  MessageSlice &
  OrchestratorSlice &
  UISlice & {
    resetAll: () => void;
  };

export const useChatStore = create<ChatStore>()(
  devtools(
    (...a) => ({
      ...createSessionSlice(...a),
      ...createMessageSlice(...a),
      ...createOrchestratorSlice(...a),
      ...createUISlice(...a),
      resetAll: () => {
        const [set] = a;
        // Atomic reset of all slices
        set(
          {
            // Session
            currentSessionId: null,
            sessionVisibility: undefined,
            sessionIsActive: false,
            sessionMode: 'agent' as const,
            triggerSource: null,
            // Messages
            messages: [],
            // Orchestrator
            orchestratorEvents: [],
            isOrchestratorActive: false,
            synthesisAcc: '',
            synthMessageCreated: false,
            requestStartTime: 0,
            // Investigation tracking (now part of orchestrator)
            investigationPlan: null,
            iterations: [],
            currentIteration: 0,
            investigationStartTime: null,
            totalStepCount: 0,
            totalConnectorCount: 0,
            hypotheses: [],
            // UI
            isProcessing: false,
            isWarRoomProcessing: false,
            currentWorkflow: null,
            pendingApprovals: [],
            isApprovingAction: false,
            usageSummary: null,
            activeMention: null,
            contextUsage: null,
            announcements: [],
            followUpSuggestions: [],
          },
          false,
          'store/resetAll',
        );
      },
    }),
    { name: 'ChatStore', enabled: import.meta.env.DEV },
  ),
);
