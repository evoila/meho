// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { describe, it, expect } from 'vitest';
import { formatOperationLabel } from '../formatOperationLabel';

describe('formatOperationLabel', () => {
  it('formats call_operation with operation_id and namespace param', () => {
    expect(
      formatOperationLabel('call_operation', {
        connector_id: 'k8s-prod-1',
        operation_id: 'get_pods',
        parameter_sets: [{ namespace: 'payment-svc' }],
      }),
    ).toBe('Get Pods \u2192 ns: payment-svc');
  });

  it('formats call_operation with operation_id only (no params)', () => {
    expect(
      formatOperationLabel('call_operation', {
        connector_id: 'vmware-1',
        operation_id: 'list_vms',
        parameter_sets: [],
      }),
    ).toBe('List Vms');
  });

  it('formats search_operations with query', () => {
    expect(
      formatOperationLabel('search_operations', { query: 'pod status' }),
    ).toBe('Search operations: pod status');
  });

  it('formats search_knowledge with query', () => {
    expect(
      formatOperationLabel('search_knowledge', { query: 'OOM remediation' }),
    ).toBe('Knowledge search: OOM remediation');
  });

  it('formats recall_memory', () => {
    expect(formatOperationLabel('recall_memory', {})).toBe('Recall memory');
  });

  it('formats reduce_data with query', () => {
    expect(
      formatOperationLabel('reduce_data', { query: 'filter by namespace' }),
    ).toBe('Analyze data: filter by namespace');
  });

  it('formats unknown_tool with humanized name', () => {
    expect(formatOperationLabel('unknown_tool', {})).toBe('unknown tool');
  });

  it('formats call_operation with vm_name param', () => {
    expect(
      formatOperationLabel('call_operation', {
        connector_id: 'vmware-1',
        operation_id: 'get_vm_status',
        parameter_sets: [{ vm_name: 'my-vm' }],
      }),
    ).toBe('Get Vm Status \u2192 vm: my-vm');
  });

  it('formats call_operation with promql param (truncated)', () => {
    expect(
      formatOperationLabel('call_operation', {
        connector_id: 'prom-1',
        operation_id: 'query_metrics',
        parameter_sets: [
          {
            promql:
              'container_memory_usage_bytes{namespace="payment-svc",pod=~"payment-.*"}',
          },
        ],
      }),
    ).toBe(
      'Query Metrics \u2192 container_memory_usage_bytes{namespace="payment-sv...',
    );
  });
});
