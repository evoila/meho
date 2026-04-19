// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Extract Output Summary Utility
 *
 * Extracts a human-readable summary from tool call output data.
 * Shared between ConnectorCard (inline timeline) and agent pane (sidebar timeline).
 *
 * Priority:
 * 1. Backend-provided `summary` field (fully generic)
 * 2. Common patterns (count, row_count, arrays)
 * 3. Fallback field inspection
 */
export function extractOutputSummary(_toolName: string, output: unknown): string | undefined { // NOSONAR (cognitive complexity)
  if (!output || typeof output !== 'object') return undefined;

  const data = output as Record<string, unknown>;

  // 1. PRIORITY: Backend-provided summary (any agent can use this)
  if (typeof data.summary === 'string' && data.summary) {
    return data.summary;
  }

  // 2. GENERIC: Common patterns that work across any agent

  // Table + row count pattern
  if (typeof data.table === 'string' && typeof data.row_count === 'number') {
    return `${data.table} (${data.row_count} rows)`;
  }

  // Count with array preview
  if (typeof data.count === 'number') {
    // Try to get names from common array fields
    const arrayField = data.operations || data.items || data.results || data.data;
    if (Array.isArray(arrayField) && arrayField.length > 0) {
      const names = arrayField
        .slice(0, 3)
        .map((item: unknown) => {
          if (typeof item === 'object' && item !== null) {
            const obj = item as Record<string, unknown>;
            return obj.name || obj.operation_id || obj.id || obj.title;
          }
          return null;
        })
        .filter(Boolean);
      if (names.length > 0) {
        return `${data.count} items (${names.join(', ')}${data.count > 3 ? '...' : ''})`;
      }
    }
    return `${data.count} items`;
  }

  // Row count without table name
  if (typeof data.row_count === 'number') {
    return `${data.row_count} rows`;
  }

  // Data available flag (large result stored)
  if (data.data_available === false) {
    const rowCount = data.row_count as number | undefined;
    return rowCount ? `${rowCount} items (stored)` : 'data stored';
  }

  // Common array fields
  const arrayFields = ['items', 'results', 'data', 'records', 'rows', 'entries', 'operations'];
  for (const field of arrayFields) {
    if (Array.isArray(data[field])) {
      const arr = data[field] as unknown[];
      if (arr.length > 0) {
        // Try to extract preview names
        const names = arr
          .slice(0, 3)
          .map((item: unknown) => {
            if (typeof item === 'object' && item !== null) {
              const obj = item as Record<string, unknown>;
              return obj.name || obj.id || obj.title;
            }
            return null;
          })
          .filter(Boolean);
        if (names.length > 0) {
          return `${arr.length} ${field} (${names.join(', ')}${arr.length > 3 ? '...' : ''})`;
        }
        return `${arr.length} ${field}`;
      }
    }
  }

  // Status field
  if (typeof data.status === 'string') {
    return data.status;
  }

  // Message field
  if (typeof data.message === 'string' && data.message.length < 50) {
    return data.message;
  }

  return undefined;
}
