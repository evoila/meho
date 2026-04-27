// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Group Steps Utility
 *
 * Groups consecutive think -> act -> result agent events into coherent
 * StepGroup objects for the ConnectorCard timeline. Each StepGroup
 * represents one logical step (D-14) with a human-readable label (D-12)
 * and expandable reasoning (D-13).
 */
import type { WrappedAgentEvent } from '@/api/types/orchestrator';
import { formatOperationLabel } from './formatOperationLabel';

/**
 * A grouped step combining thought + action + observation into one unit.
 */
export interface StepGroup {
  /** Unique ID for React key */
  id: string;
  /** Optional LLM reasoning that preceded this action */
  thought?: string;
  /** Raw tool name (e.g. "call_operation", "thinking") */
  toolName: string;
  /** Human-readable label from formatOperationLabel */
  toolLabel: string;
  /** Target entity extracted from args */
  targetEntity?: string;
  /** Raw tool arguments */
  args: Record<string, unknown>;
  /** Observation/result content */
  result?: string;
  /** Brief summary of observation */
  observationSummary?: string;
  /** Step status */
  status: 'pending' | 'running' | 'success' | 'failed';
  /** Duration in ms (from action to observation) */
  duration?: number;
  /** When this step arrived (for ordering) */
  arrivalTime: number;
  /** Original events for modal drill-down */
  originalEvents: WrappedAgentEvent[];
}

/**
 * Group a flat list of WrappedAgentEvents into StepGroup objects.
 *
 * Groups consecutive thought -> action -> observation cycles.
 * Standalone thoughts become their own group.
 * tool_start/tool_complete events are used for timing but not rendered.
 */
export function groupSteps(events: WrappedAgentEvent[]): StepGroup[] {
  const groups: StepGroup[] = [];
  let bufferedThought: string | undefined;
  let bufferedThoughtEvent: WrappedAgentEvent | undefined;
  let stepCounter = 0;

  for (let i = 0; i < events.length; i++) {
    const event = events[i];
    const inner = event.inner_event;
    if (!inner) continue;

    const eventType = inner.type;
    const data = inner.data as Record<string, unknown>;

    switch (eventType) {
      case 'thought': {
        // If we already have a buffered thought without a following action,
        // flush it as a standalone thought group.
        if (bufferedThought !== undefined && bufferedThoughtEvent) {
          groups.push(
            makeThoughtGroup(
              bufferedThought,
              bufferedThoughtEvent,
              stepCounter++,
            ),
          );
        }
        bufferedThought = String(data.content ?? data.message ?? '');
        bufferedThoughtEvent = event;
        break;
      }

      case 'action': {
        const toolName = String(data.tool ?? 'unknown');
        const args = (data.args as Record<string, unknown>) ?? {};
        const toolLabel = formatOperationLabel(toolName, args);
        const arrivalTime = event._arrivalTime ?? Date.now();

        const originalEvents: WrappedAgentEvent[] = [];
        if (bufferedThoughtEvent) originalEvents.push(bufferedThoughtEvent);
        originalEvents.push(event);

        const group: StepGroup = {
          id: `step-${stepCounter++}`,
          thought: bufferedThought,
          toolName,
          toolLabel,
          args,
          status: 'running',
          arrivalTime,
          originalEvents,
        };

        // Reset buffered thought
        bufferedThought = undefined;
        bufferedThoughtEvent = undefined;

        groups.push(group);
        break;
      }

      case 'observation': {
        // Attach to the most recent running group
        const lastRunning = findLastRunning(groups);
        if (lastRunning) {
          lastRunning.result = String(
            data.content ?? data.result ?? '',
          );
          lastRunning.observationSummary = String(
            data.content ?? data.result ?? '',
          ).slice(0, 120);

          // Determine status: failed if error key is present
          if (data.error) {
            lastRunning.status = 'failed';
          } else {
            lastRunning.status = 'success';
          }

          // Calculate duration from action arrival to observation arrival
          const obsTime = event._arrivalTime ?? Date.now();
          if (lastRunning.arrivalTime) {
            lastRunning.duration = obsTime - lastRunning.arrivalTime;
          }

          lastRunning.originalEvents.push(event);
        }
        break;
      }

      case 'tool_start':
      case 'tool_complete':
        // Used for timing but not rendered as separate steps
        break;

      default:
        // Skip unknown event types
        break;
    }
  }

  // Flush any remaining buffered thought
  if (bufferedThought !== undefined && bufferedThoughtEvent) {
    groups.push(
      makeThoughtGroup(
        bufferedThought,
        bufferedThoughtEvent,
        stepCounter++,
      ),
    );
  }

  return groups;
}

/** Find the last group with status === 'running'. */
function findLastRunning(groups: StepGroup[]): StepGroup | undefined {
  for (let i = groups.length - 1; i >= 0; i--) {
    if (groups[i].status === 'running') return groups[i];
  }
  return undefined;
}

/** Create a standalone thought group. */
function makeThoughtGroup(
  thought: string,
  event: WrappedAgentEvent,
  counter: number,
): StepGroup {
  return {
    id: `step-${counter}`,
    thought,
    toolName: 'thinking',
    toolLabel: 'Reasoning',
    args: {},
    status: 'success',
    arrivalTime: event._arrivalTime ?? Date.now(),
    originalEvents: [event],
  };
}
