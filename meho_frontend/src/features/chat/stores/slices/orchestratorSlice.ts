// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Orchestrator Slice
 *
 * Replaces orchestratorEventsRef, isOrchestratorActiveRef, synthesisAccRef,
 * synthMessageCreatedRef, and requestStartTimeRef from ChatPage.
 *
 * All streaming callbacks use useChatStore.getState() to read these values,
 * eliminating stale-closure bugs.
 *
 * Includes investigation tracking state (iterations, connectors, steps, hypotheses).
 */
import type { StateCreator } from 'zustand';
import type { ChatStore } from '../useChatStore';
import type { OrchestratorEvent } from '@/api/types/orchestrator';

// --- Investigation Types ---

export interface InvestigationStep {
  id: string;
  type: 'tool_call' | 'thought';
  toolName?: string;
  targetEntity?: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  duration?: number;
  reasoning?: string;
  observationSummary?: string;
  observationData?: unknown;
  arrivalTime: number;
}

export interface ConnectorInvestigation {
  connectorId: string;
  connectorName: string;
  status: 'running' | 'success' | 'failed' | 'timeout' | 'partial';
  steps: InvestigationStep[];
  totalTime?: number;
}

export interface IterationState {
  iteration: number;
  connectors: Map<string, ConnectorInvestigation>;
  totalTime?: number;
}

// --- Target Entity Parsing ---

/**
 * Extract a human-readable target entity from tool call arguments.
 * Best-effort heuristic -- different tools have different argument structures.
 */
export function parseTargetEntity(
  _toolName: string,
  args: Record<string, unknown>,
): string | undefined {
  // K8s patterns
  if (args.namespace && args.name) return `${String(args.namespace)}/${String(args.name)}`;
  if (args.namespace) return `ns/${String(args.namespace)}`;
  if (args.pod_name) return `pod/${String(args.pod_name)}`;
  if (args.deployment) return `deploy/${String(args.deployment)}`;
  if (args.service) return `svc/${String(args.service)}`;

  // Generic patterns
  if (args.query && typeof args.query === 'string') return args.query.slice(0, 60);
  if (args.operation_id) return String(args.operation_id);
  if (args.path) return String(args.path);
  if (args.table) return String(args.table);
  if (args.name) return String(args.name);
  if (args.target) return String(args.target);

  // VMware patterns
  if (args.vm_name) return `vm/${String(args.vm_name)}`;
  if (args.host) return String(args.host);

  return undefined;
}

// --- Hypothesis Tracking ---

export interface Hypothesis {
  id: string;
  text: string;
  status: 'investigating' | 'validated' | 'invalidated' | 'inconclusive';
  connectorId?: string;
  connectorName?: string;
  updatedAt: number;
}

// --- Investigation Plan (Phase 99) ---

export interface InvestigationPlan {
  classification: 'quick' | 'standard' | 'deep';
  reasoning: string;
  strategy: 'direct' | 'progressive' | 'parallel';
  plannedSystems: Array<{
    id: string;
    name: string;
    reason: string;
    priority: number;
    conditional: boolean;
  }>;
  estimatedCalls: number;
}

// --- Slice ---

export interface OrchestratorSlice {
  // Original orchestrator state
  orchestratorEvents: OrchestratorEvent[];
  isOrchestratorActive: boolean;
  synthesisAcc: string;
  synthMessageCreated: boolean;
  requestStartTime: number;
  addOrchestratorEvent: (event: OrchestratorEvent) => void;
  setOrchestratorActive: (active: boolean) => void;
  appendSynthesis: (chunk: string) => void;
  setSynthMessageCreated: (created: boolean) => void;
  setRequestStartTime: (time: number) => void;
  resetOrchestrator: () => void;
  resetSynthesis: () => void;

  // Investigation plan (Phase 99)
  investigationPlan: InvestigationPlan | null;
  setInvestigationPlan: (plan: InvestigationPlan) => void;

  // Investigation tracking state
  iterations: IterationState[];
  currentIteration: number;
  investigationStartTime: number | null;
  totalStepCount: number;
  totalConnectorCount: number;
  hypotheses: Hypothesis[];
  startInvestigation: (startTime: number) => void;
  addIteration: (iterationNum: number) => void;
  registerConnector: (connectorId: string, connectorName: string, iteration: number) => void;
  addStep: (connectorId: string, iteration: number, step: InvestigationStep) => void;
  updateStepStatus: (connectorId: string, iteration: number, stepId: string, status: InvestigationStep['status'], duration?: number) => void;
  updateConnectorStatus: (connectorId: string, status: ConnectorInvestigation['status'], totalTime?: number) => void;
  resetInvestigation: () => void;
  upsertHypothesis: (h: Hypothesis) => void;
  clearHypotheses: () => void;
}

export const createOrchestratorSlice: StateCreator<
  ChatStore,
  [['zustand/devtools', never]],
  [],
  OrchestratorSlice
> = (set) => ({
  // --- Original orchestrator state ---
  orchestratorEvents: [],
  isOrchestratorActive: false,
  synthesisAcc: '',
  synthMessageCreated: false,
  requestStartTime: 0,

  addOrchestratorEvent: (event) =>
    set(
      (state) => ({
        orchestratorEvents: [...state.orchestratorEvents, event],
      }),
      false,
      'orchestrator/addEvent',
    ),

  setOrchestratorActive: (active) =>
    set({ isOrchestratorActive: active }, false, 'orchestrator/setActive'),

  appendSynthesis: (chunk) =>
    set(
      (state) => ({ synthesisAcc: state.synthesisAcc + chunk }),
      false,
      'orchestrator/appendSynthesis',
    ),

  setSynthMessageCreated: (created) =>
    set({ synthMessageCreated: created }, false, 'orchestrator/setSynthCreated'),

  setRequestStartTime: (time) =>
    set({ requestStartTime: time }, false, 'orchestrator/setStartTime'),

  resetOrchestrator: () =>
    set(
      {
        orchestratorEvents: [],
        isOrchestratorActive: false,
        synthesisAcc: '',
        synthMessageCreated: false,
        requestStartTime: 0,
      },
      false,
      'orchestrator/reset',
    ),

  resetSynthesis: () =>
    set(
      {
        synthesisAcc: '',
        synthMessageCreated: false,
      },
      false,
      'orchestrator/resetSynthesis',
    ),

  // --- Investigation plan (Phase 99) ---
  investigationPlan: null,

  setInvestigationPlan: (plan) =>
    set({ investigationPlan: plan }, false, 'orchestrator/setInvestigationPlan'),

  // --- Investigation tracking state ---
  iterations: [],
  currentIteration: 0,
  investigationStartTime: null,
  totalStepCount: 0,
  totalConnectorCount: 0,
  hypotheses: [],

  startInvestigation: (startTime) =>
    set(
      {
        investigationPlan: null,
        iterations: [],
        currentIteration: 0,
        investigationStartTime: startTime,
        totalStepCount: 0,
        totalConnectorCount: 0,
        hypotheses: [],
      },
      false,
      'orchestrator/startInvestigation',
    ),

  addIteration: (iterationNum) =>
    set(
      (state) => {
        const newIteration: IterationState = {
          iteration: iterationNum,
          connectors: new Map(),
        };
        return {
          iterations: [...state.iterations, newIteration],
          currentIteration: iterationNum,
        };
      },
      false,
      'orchestrator/addIteration',
    ),

  registerConnector: (connectorId, connectorName, iteration) =>
    set(
      (state) => {
        const iterations = state.iterations.map((iter) => {
          if (iter.iteration !== iteration) return iter;
          if (iter.connectors.has(connectorId)) return iter;
          const connectors = new Map(iter.connectors);
          connectors.set(connectorId, {
            connectorId,
            connectorName,
            status: 'running',
            steps: [],
          });
          return { ...iter, connectors };
        });
        // Count unique connectors across all iterations
        const connectorIds = new Set<string>();
        for (const iter of iterations) {
          for (const cId of iter.connectors.keys()) {
            connectorIds.add(cId);
          }
        }
        return {
          iterations,
          totalConnectorCount: connectorIds.size,
        };
      },
      false,
      'orchestrator/registerConnector',
    ),

  addStep: (connectorId, iteration, step) =>
    set(
      (state) => {
        const iterations = state.iterations.map((iter) => {
          if (iter.iteration !== iteration) return iter;
          const connectors = new Map(iter.connectors);
          const connector = connectors.get(connectorId);
          if (!connector) return iter;
          connectors.set(connectorId, {
            ...connector,
            steps: [...connector.steps, step],
          });
          return { ...iter, connectors };
        });
        return {
          iterations,
          totalStepCount: state.totalStepCount + 1,
        };
      },
      false,
      'orchestrator/addStep',
    ),

  updateStepStatus: (connectorId, iteration, stepId, status, duration) =>
    set(
      (state) => {
        const iterations = state.iterations.map((iter) => {
          if (iter.iteration !== iteration) return iter;
          const connectors = new Map(iter.connectors);
          const connector = connectors.get(connectorId);
          if (!connector) return iter;
          connectors.set(connectorId, {
            ...connector,
            steps: connector.steps.map((s) =>
              s.id === stepId ? { ...s, status, ...(duration !== undefined && { duration }) } : s,
            ),
          });
          return { ...iter, connectors };
        });
        return { iterations };
      },
      false,
      'orchestrator/updateStepStatus',
    ),

  updateConnectorStatus: (connectorId, status, totalTime) =>
    set(
      (state) => {
        const iterations = state.iterations.map((iter) => {
          const connectors = new Map(iter.connectors);
          const connector = connectors.get(connectorId);
          if (!connector) return iter;
          connectors.set(connectorId, {
            ...connector,
            status,
            ...(totalTime !== undefined && { totalTime }),
          });
          return { ...iter, connectors };
        });
        return { iterations };
      },
      false,
      'orchestrator/updateConnectorStatus',
    ),

  resetInvestigation: () =>
    set(
      {
        investigationPlan: null,
        iterations: [],
        currentIteration: 0,
        investigationStartTime: null,
        totalStepCount: 0,
        totalConnectorCount: 0,
        hypotheses: [],
      },
      false,
      'orchestrator/resetInvestigation',
    ),

  upsertHypothesis: (h) =>
    set(
      (state) => {
        const existing = state.hypotheses.findIndex((x) => x.id === h.id);
        if (existing >= 0) {
          // Update existing hypothesis status
          const updated = [...state.hypotheses];
          updated[existing] = { ...updated[existing], ...h };
          return { hypotheses: updated };
        }
        // Add new hypothesis (limit to 10)
        return {
          hypotheses: [...state.hypotheses.slice(-9), h],
        };
      },
      false,
      'orchestrator/upsertHypothesis',
    ),

  clearHypotheses: () =>
    set({ hypotheses: [] }, false, 'orchestrator/clearHypotheses'),
});
