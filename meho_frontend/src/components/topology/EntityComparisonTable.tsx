// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EntityComparisonTable - Side-by-side smart diff of two topology entities (Phase 17 Plan 03)
 *
 * Displays entity attributes in a 3-column table with:
 * - Priority attributes first (hostname, provider_id, IP addresses, name)
 * - Green highlighting for matching values
 * - Red highlighting for mismatching values
 * - Amber left border for match-evidence rows
 */

import { ConnectorIcon } from './ConnectorIcon';
import { clsx } from 'clsx';
import type { TopologyEntity } from '../../lib/topologyApi';

// ============================================================================
// Types
// ============================================================================

interface EntityComparisonTableProps {
  entityA: TopologyEntity;
  entityB: TopologyEntity;
  matchDetails?: Record<string, unknown> | null;
}

interface ComparisonRow {
  key: string;
  valueA: string | null;
  valueB: string | null;
  isMatch: boolean;
  isHighlighted: boolean;
}

// ============================================================================
// Priority ordering for attribute keys
// ============================================================================

const PRIORITY_KEYS = [
  'hostname',
  'provider_id',
  'addresses',
  'ip',
  'InternalIP',
  'ExternalIP',
  'name',
];

// ============================================================================
// Helpers
// ============================================================================

/**
 * Parse matchDetails defensively -- may be JSON string, object, or null.
 * Returns an object with matching_fields and verified_via arrays.
 */
function parseMatchDetails(matchDetails?: Record<string, unknown> | null): {
  matchingFields: string[];
  verifiedVia: string[];
} {
  if (!matchDetails) return { matchingFields: [], verifiedVia: [] };

  let parsed = matchDetails;
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed);
    } catch {
      return { matchingFields: [], verifiedVia: [] };
    }
  }

  const matchingFields = Array.isArray(parsed.matching_fields)
    ? parsed.matching_fields.map(String)
    : [];
  const verifiedVia = Array.isArray(parsed.verified_via)
    ? parsed.verified_via.map(String)
    : [];

  return { matchingFields, verifiedVia };
}

/**
 * Format a value for display. Objects/arrays become JSON strings.
 */
function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/**
 * Build comparison rows from two entities' raw_attributes.
 */
function buildComparisonRows(
  entityA: TopologyEntity,
  entityB: TopologyEntity,
  matchDetails?: Record<string, unknown> | null
): ComparisonRow[] {
  const attrsA = entityA.raw_attributes ?? {};
  const attrsB = entityB.raw_attributes ?? {};

  // Collect all unique keys
  const allKeys = new Set([...Object.keys(attrsA), ...Object.keys(attrsB)]);

  // Separate into priority and non-priority
  const priorityFound: string[] = [];
  const rest: string[] = [];

  for (const key of allKeys) {
    if (PRIORITY_KEYS.includes(key)) {
      priorityFound.push(key);
    } else {
      rest.push(key);
    }
  }

  // Sort priority keys by their defined order, rest alphabetically
  priorityFound.sort(
    (a, b) => PRIORITY_KEYS.indexOf(a) - PRIORITY_KEYS.indexOf(b)
  );
  rest.sort((a, b) => a.localeCompare(b));

  const sortedKeys = [...priorityFound, ...rest];

  // Parse match evidence
  const { matchingFields, verifiedVia } = parseMatchDetails(matchDetails);
  const evidenceKeys = new Set([...matchingFields, ...verifiedVia]);

  return sortedKeys.map((key) => {
    const rawA = attrsA[key] ?? null;
    const rawB = attrsB[key] ?? null;
    const valueA = rawA !== null ? formatValue(rawA) : null;
    const valueB = rawB !== null ? formatValue(rawB) : null;

    // Match requires both non-null and identical JSON representation
    const isMatch =
      rawA !== null &&
      rawB !== null &&
      JSON.stringify(rawA) === JSON.stringify(rawB);

    const isHighlighted = evidenceKeys.has(key);

    return { key, valueA, valueB, isMatch, isHighlighted };
  });
}

// ============================================================================
// Component
// ============================================================================

export function EntityComparisonTable({
  entityA,
  entityB,
  matchDetails,
}: EntityComparisonTableProps) {
  const rows = buildComparisonRows(entityA, entityB, matchDetails);

  if (rows.length === 0) {
    return (
      <div className="text-xs text-gray-500 italic py-2">
        No attributes to compare
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-700">
      <table className="w-full text-xs bg-gray-800">
        {/* Header */}
        <thead>
          <tr className="border-b border-gray-700">
            <th className="px-3 py-2 text-left text-gray-400 font-medium w-1/4">
              Attribute
            </th>
            <th className="px-3 py-2 text-left text-gray-400 font-medium w-[37.5%]">
              <div className="flex items-center gap-1.5">
                {entityA.connector_type && (
                  <ConnectorIcon
                    connectorType={entityA.connector_type}
                    size={14}
                  />
                )}
                <span className="truncate" title={entityA.name}>
                  {entityA.name}
                </span>
              </div>
            </th>
            <th className="px-3 py-2 text-left text-gray-400 font-medium w-[37.5%]">
              <div className="flex items-center gap-1.5">
                {entityB.connector_type && (
                  <ConnectorIcon
                    connectorType={entityB.connector_type}
                    size={14}
                  />
                )}
                <span className="truncate" title={entityB.name}>
                  {entityB.name}
                </span>
              </div>
            </th>
          </tr>
        </thead>

        {/* Body */}
        <tbody>
          {rows.map((row) => {
            const bothPresent = row.valueA !== null && row.valueB !== null;
            const cellColorA = bothPresent
              ? row.isMatch
                ? 'bg-green-500/10 text-green-400'
                : 'bg-red-500/10 text-red-400'
              : row.valueA === null
                ? 'text-gray-600 italic'
                : 'text-gray-300';
            const cellColorB = bothPresent
              ? row.isMatch
                ? 'bg-green-500/10 text-green-400'
                : 'bg-red-500/10 text-red-400'
              : row.valueB === null
                ? 'text-gray-600 italic'
                : 'text-gray-300';

            return (
              <tr
                key={row.key}
                className={clsx(
                  'border-b border-gray-700/50 last:border-b-0',
                  row.isHighlighted && 'border-l-2 border-l-amber-500'
                )}
              >
                <td className="px-3 py-1.5 text-gray-400 font-mono">
                  {row.key}
                </td>
                <td
                  className={clsx('px-3 py-1.5', cellColorA)}
                  title={row.valueA && row.valueA.length > 80 ? row.valueA : undefined}
                >
                  <span className={row.valueA && row.valueA.length > 80 ? 'block truncate max-w-[200px]' : ''}>
                    {row.valueA ?? '\u2014'}
                  </span>
                </td>
                <td
                  className={clsx('px-3 py-1.5', cellColorB)}
                  title={row.valueB && row.valueB.length > 80 ? row.valueB : undefined}
                >
                  <span className={row.valueB && row.valueB.length > 80 ? 'block truncate max-w-[200px]' : ''}>
                    {row.valueB ?? '\u2014'}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
