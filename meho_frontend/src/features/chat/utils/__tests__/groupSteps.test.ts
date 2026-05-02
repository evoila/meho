// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { describe, it, expect } from 'vitest';
import { groupSteps } from '../groupSteps';
import type { WrappedAgentEvent } from '@/api/types/orchestrator';

/** Helper to create a WrappedAgentEvent for testing. */
function makeEvent(
  innerType: string,
  data: Record<string, unknown>,
  arrivalTime = Date.now(),
): WrappedAgentEvent {
  return {
    type: 'agent_event',
    agent_source: {
      agent_name: 'generic_agent',
      connector_id: 'k8s-prod',
      connector_name: 'K8s Prod',
      iteration: 1,
    },
    inner_event: { type: innerType, data },
    _arrivalTime: arrivalTime,
  };
}

describe('groupSteps', () => {
  it('groups [thought, action, observation] into one StepGroup', () => {
    const events = [
      makeEvent('thought', { content: 'Checking pod status...' }, 1000),
      makeEvent(
        'action',
        {
          tool: 'call_operation',
          args: {
            operation_id: 'get_pods',
            parameter_sets: [{ namespace: 'default' }],
          },
        },
        2000,
      ),
      makeEvent(
        'observation',
        { content: 'Found 3 pods', tool: 'call_operation' },
        3000,
      ),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].thought).toBe('Checking pod status...');
    expect(groups[0].toolName).toBe('call_operation');
    expect(groups[0].toolLabel).toContain('Get Pods');
    expect(groups[0].result).toBe('Found 3 pods');
    expect(groups[0].status).toBe('success');
  });

  it('groups [action, observation] without thought into one StepGroup', () => {
    const events = [
      makeEvent(
        'action',
        {
          tool: 'call_operation',
          args: {
            operation_id: 'list_vms',
            parameter_sets: [],
          },
        },
        1000,
      ),
      makeEvent(
        'observation',
        { content: 'Found 5 VMs', tool: 'call_operation' },
        2000,
      ),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].thought).toBeUndefined();
    expect(groups[0].toolName).toBe('call_operation');
    expect(groups[0].status).toBe('success');
  });

  it('groups two cycles into two StepGroups', () => {
    const events = [
      makeEvent('thought', { content: 'First thought' }, 1000),
      makeEvent(
        'action',
        {
          tool: 'call_operation',
          args: { operation_id: 'get_pods', parameter_sets: [] },
        },
        2000,
      ),
      makeEvent(
        'observation',
        { content: 'First result', tool: 'call_operation' },
        3000,
      ),
      makeEvent('thought', { content: 'Second thought' }, 4000),
      makeEvent(
        'action',
        {
          tool: 'search_knowledge',
          args: { query: 'OOM fix' },
        },
        5000,
      ),
      makeEvent(
        'observation',
        { content: 'Second result', tool: 'search_knowledge' },
        6000,
      ),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(2);
    expect(groups[0].thought).toBe('First thought');
    expect(groups[1].thought).toBe('Second thought');
    expect(groups[1].toolLabel).toContain('Knowledge search');
  });

  it('creates standalone thought-only group for lone thought', () => {
    const events = [
      makeEvent('thought', { content: 'Just thinking...' }, 1000),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].toolName).toBe('thinking');
    expect(groups[0].toolLabel).toBe('Reasoning');
    expect(groups[0].thought).toBe('Just thinking...');
  });

  it('marks running action without observation as running', () => {
    const events = [
      makeEvent('thought', { content: 'Checking...' }, 1000),
      makeEvent(
        'action',
        {
          tool: 'call_operation',
          args: { operation_id: 'get_pods', parameter_sets: [] },
        },
        2000,
      ),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].status).toBe('running');
    expect(groups[0].result).toBeUndefined();
  });

  it('marks failed observation as failed', () => {
    const events = [
      makeEvent(
        'action',
        {
          tool: 'call_operation',
          args: { operation_id: 'get_pods', parameter_sets: [] },
        },
        1000,
      ),
      makeEvent(
        'observation',
        {
          content: 'Connection refused',
          tool: 'call_operation',
          error: 'Connection refused',
        },
        2000,
      ),
    ];

    const groups = groupSteps(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].status).toBe('failed');
  });
});
