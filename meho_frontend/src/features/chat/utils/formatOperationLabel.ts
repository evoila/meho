// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Format Operation Label Utility
 *
 * Converts raw tool names and arguments into human-readable labels
 * for the ConnectorCard timeline. Replaces cryptic "call_operation"
 * with labels like "Get Pods -> ns: payment-svc" (D-12).
 */

/**
 * Title-case a snake_case string: "get_pods" -> "Get Pods".
 */
function titleCase(s: string): string {
  return s
    .split('_')
    .map((w) => (w.length > 0 ? w[0].toUpperCase() + w.slice(1) : ''))
    .join(' ');
}

/**
 * Truncate a string to `maxLen` chars, adding "..." if truncated.
 */
function truncate(s: string, maxLen: number): string {
  return s.length > maxLen ? s.slice(0, maxLen) + '...' : s;
}

/**
 * Extract a human-readable target from call_operation parameter_sets.
 *
 * Checks K8s, VMware, Prometheus, and generic patterns.
 */
export function extractTarget(
  _operationId: string,
  params: Record<string, unknown>,
): string | undefined {
  // K8s patterns
  if (params.namespace && params.name) {
    return `ns/${String(params.namespace)}/${String(params.name)}`;
  }
  if (params.namespace) return `ns: ${String(params.namespace)}`;
  if (params.pod_name) return `pod: ${String(params.pod_name)}`;

  // VMware patterns
  if (params.vm_name) return `vm: ${String(params.vm_name)}`;
  if (params.host) return String(params.host);

  // Prometheus patterns
  if (params.promql) return truncate(String(params.promql), 50);
  if (params.metric_name) return String(params.metric_name);

  // Generic patterns
  if (params.query) return truncate(String(params.query), 50);

  return undefined;
}

/** Lookup table for non-call_operation tools. */
const TOOL_LABELS: Record<
  string,
  (args: Record<string, unknown>) => string
> = {
  search_operations: (args) =>
    `Search operations: ${String(args.query ?? '')}`,
  search_knowledge: (args) =>
    `Knowledge search: ${String(args.query ?? '')}`,
  recall_memory: () => 'Recall memory',
  search_types: (args) => `Search types: ${String(args.query ?? '')}`,
  reduce_data: (args) =>
    `Analyze data: ${String(args.query || 'filter results')}`,
  search_topology: (args) =>
    `Topology search: ${String(args.query ?? '')}`,
};

/**
 * Convert a tool name + args into a human-readable label.
 *
 * For `call_operation`: extracts `operation_id`, title-cases it,
 * and appends a target extracted from `parameter_sets`.
 *
 * For other tools: uses a lookup map, falling back to humanizing
 * the raw tool name.
 */
export function formatOperationLabel(
  toolName: string,
  args: Record<string, unknown>,
): string {
  // Handle call_operation specially
  if (toolName === 'call_operation') {
    const operationId = String(args.operation_id ?? 'operation');
    const humanName = titleCase(operationId);

    // Extract target from first parameter_sets entry
    const paramSets = args.parameter_sets as
      | Array<Record<string, unknown>>
      | undefined;
    if (paramSets && paramSets.length > 0) {
      const target = extractTarget(operationId, paramSets[0]);
      if (target) return `${humanName} \u2192 ${target}`;
    }

    return humanName;
  }

  // Lookup known tools
  const formatter = TOOL_LABELS[toolName];
  if (formatter) return formatter(args);

  // Fallback: humanize tool name
  return toolName.replace(/_/g, ' ');
}
