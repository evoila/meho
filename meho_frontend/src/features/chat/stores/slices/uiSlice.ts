// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * UI Slice
 *
 * Manages transient UI state: input, processing indicators, approval queue,
 * workflow state, and usage summary.
 */
import type { StateCreator } from 'zustand';
import type { ChatStore } from '../useChatStore';
import type { Workflow } from '@/lib/api-client';
import type { ApprovalRequest } from '@/components/ApprovalModal';
import type { UsageSummary } from '../../types';

/** Phase 64-02: Screen reader announcement with priority level */
export interface Announcement {
  id: string;
  message: string;
  priority: 'polite' | 'assertive';
}

/** Phase 63-02: Context usage state from backend SSE event */
export interface ContextUsage {
  percentage: number;
  tokensUsed: number;
  tokensLimit: number;
}

/** Phase 63: Active mention stored when user selects from @ autocomplete dropdown */
export interface ActiveMention {
  connectorId: string;
  connectorName: string;
  connectorType: string;
}

export interface UISlice {
  input: string;
  isProcessing: boolean;
  isWarRoomProcessing: boolean;
  currentWorkflow: Workflow | null;
  pendingApprovals: ApprovalRequest[];
  isApprovingAction: boolean;
  usageSummary: UsageSummary | null;
  activeMention: ActiveMention | null;
  contextUsage: ContextUsage | null;
  setInput: (input: string) => void;
  setIsProcessing: (processing: boolean) => void;
  setIsWarRoomProcessing: (processing: boolean) => void;
  setCurrentWorkflow: (workflow: Workflow | null) => void;
  /** Append an approval to the queue (deduplicates by approval_id). */
  pushApproval: (approval: ApprovalRequest) => void;
  /** Remove a resolved approval from the queue by ID. */
  removeApproval: (approvalId: string) => void;
  /** Replace the entire queue (for DB hydration). */
  setApprovals: (approvals: ApprovalRequest[]) => void;
  setIsApprovingAction: (approving: boolean) => void;
  setUsageSummary: (summary: UsageSummary | null) => void;
  setActiveMention: (mention: ActiveMention | null) => void;
  clearActiveMention: () => void;
  setContextUsage: (usage: ContextUsage | null) => void;
  announcements: Announcement[];
  announce: (message: string, priority?: 'polite' | 'assertive') => void;
  clearAnnouncement: (id: string) => void;
  followUpSuggestions: string[];
  setFollowUpSuggestions: (suggestions: string[]) => void;
  clearFollowUpSuggestions: () => void;
  resetUI: () => void;
}

export const createUISlice: StateCreator<
  ChatStore,
  [['zustand/devtools', never]],
  [],
  UISlice
> = (set) => ({
  input: '',
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

  setInput: (input) => set({ input }, false, 'ui/setInput'),
  setIsProcessing: (processing) =>
    set({ isProcessing: processing }, false, 'ui/setProcessing'),
  setIsWarRoomProcessing: (processing) =>
    set({ isWarRoomProcessing: processing }, false, 'ui/setWarRoomProcessing'),
  setCurrentWorkflow: (workflow) =>
    set({ currentWorkflow: workflow }, false, 'ui/setWorkflow'),
  pushApproval: (approval) =>
    set(
      (state) => {
        const exists = state.pendingApprovals.some(
          (a) => a.approval_id === approval.approval_id,
        );
        if (exists) return state;
        return { pendingApprovals: [...state.pendingApprovals, approval] };
      },
      false,
      'ui/pushApproval',
    ),
  removeApproval: (approvalId) =>
    set(
      (state) => ({
        pendingApprovals: state.pendingApprovals.filter(
          (a) => a.approval_id !== approvalId,
        ),
      }),
      false,
      'ui/removeApproval',
    ),
  setApprovals: (approvals) =>
    set({ pendingApprovals: approvals }, false, 'ui/setApprovals'),
  setIsApprovingAction: (approving) =>
    set({ isApprovingAction: approving }, false, 'ui/setApprovingAction'),
  setUsageSummary: (summary) =>
    set({ usageSummary: summary }, false, 'ui/setUsageSummary'),
  setActiveMention: (mention) =>
    set({ activeMention: mention }, false, 'ui/setActiveMention'),
  clearActiveMention: () =>
    set({ activeMention: null }, false, 'ui/clearActiveMention'),
  setContextUsage: (usage) =>
    set({ contextUsage: usage }, false, 'ui/setContextUsage'),
  announce: (message, priority = 'polite') =>
    set(
      (state) => ({
        announcements: [
          ...state.announcements,
          { id: crypto.randomUUID(), message, priority },
        ],
      }),
      false,
      'ui/announce',
    ),
  clearAnnouncement: (id) =>
    set(
      (state) => ({
        announcements: state.announcements.filter((a) => a.id !== id),
      }),
      false,
      'ui/clearAnnouncement',
    ),
  setFollowUpSuggestions: (suggestions) =>
    set({ followUpSuggestions: suggestions }, false, 'ui/setFollowUpSuggestions'),
  clearFollowUpSuggestions: () =>
    set({ followUpSuggestions: [] }, false, 'ui/clearFollowUpSuggestions'),

  resetUI: () =>
    set(
      {
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
      'ui/reset',
    ),
});
