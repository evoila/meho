// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SQLViewer Component
 *
 * Displays SQL query with syntax highlighting, parameters, and results.
 * Shows row count and execution time.
 */
import { useState, useMemo } from 'react';
import { ChevronDown, ChevronRight, Clock, Database, Table } from 'lucide-react';
import { cn, CopyButton } from '@/shared';
import { highlightSQL } from '@/shared/lib/syntax-highlight';
import type { EventDetails } from '@/api/types';

export interface SQLViewerProps {
  /** Event details containing SQL data */
  details: EventDetails;
  /** Additional CSS classes */
  className?: string;
}

/**
 * Format duration for display.
 */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

interface CollapsibleSectionProps {
  title: string;
  icon: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
  actions?: React.ReactNode;
  count?: number;
}

/**
 * Collapsible section component.
 */
function CollapsibleSection({
  title,
  icon,
  defaultOpen = false,
  children,
  actions,
  count,
}: CollapsibleSectionProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center gap-2 px-4 py-2.5 bg-surface hover:bg-surface-hover transition-colors"
      >
        {isOpen ? (
          <ChevronDown className="w-4 h-4 text-text-tertiary" />
        ) : (
          <ChevronRight className="w-4 h-4 text-text-tertiary" />
        )}
        <span className="text-text-tertiary">{icon}</span>
        <span className="text-text-primary font-medium text-sm">{title}</span>
        {count !== undefined && (
          <span className="text-text-tertiary text-xs">({count})</span>
        )}
        {actions && (
          // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- stop propagation container inside button
          <div className="ml-auto" onClick={(e) => e.stopPropagation()}>
            {actions}
          </div>
        )}
      </button>
      {isOpen && (
        <div className="border-t border-border bg-background p-4 max-h-96 overflow-auto">
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * Format SQL query for display (basic formatting).
 */
function formatSQL(query: string): string {
  // Basic formatting: capitalize keywords and add newlines
  const keywords = ['SELECT', 'FROM', 'WHERE', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'ON', 'AND', 'OR', 'ORDER BY', 'GROUP BY', 'HAVING', 'LIMIT', 'OFFSET', 'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE'];
  
  let formatted = query;
  keywords.forEach((kw) => {
    // nosemgrep: detect-non-literal-regexp -- kw iterates over hardcoded SQL keyword array, not user input
    const regex = new RegExp(`\\b${kw}\\b`, 'gi');
    formatted = formatted.replace(regex, `\n${kw}`);
  });
  
  return formatted.trim();
}

/**
 * SQL query viewer with syntax highlighting and result preview.
 *
 * @example
 * ```tsx
 * <SQLViewer
 *   details={{
 *     sql_query: "SELECT * FROM vms WHERE status = $1",
 *     sql_parameters: { "$1": "running" },
 *     sql_row_count: 42,
 *     sql_result_sample: [{ name: "vm-1", status: "running" }, ...],
 *     sql_duration_ms: 12
 *   }}
 * />
 * ```
 */
export function SQLViewer({ details, className }: SQLViewerProps) {
  const hasParameters = details.sql_parameters && Object.keys(details.sql_parameters).length > 0;
  const hasResults = details.sql_result_sample && details.sql_result_sample.length > 0;

  const formattedQuery = useMemo(() => {
    return details.sql_query ? formatSQL(details.sql_query) : '';
  }, [details.sql_query]);

  const highlightedQuery = useMemo(() => {
    return highlightSQL(formattedQuery);
  }, [formattedQuery]);

  // Get column headers from first result
  const columns = useMemo(() => {
    if (!hasResults || !details.sql_result_sample?.[0]) return [];
    return Object.keys(details.sql_result_sample[0]);
  }, [hasResults, details.sql_result_sample]);

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      {/* Header with row count and duration */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database className="w-5 h-5 text-primary" />
          <span className="text-text-primary font-medium">SQL Query</span>
        </div>
        <div className="flex items-center gap-3">
          {details.sql_row_count !== undefined && details.sql_row_count !== null && (
            <div className="flex items-center gap-1.5 text-text-secondary text-sm">
              <Table className="w-3.5 h-3.5" />
              <span>{details.sql_row_count} rows</span>
            </div>
          )}
          {details.sql_duration_ms !== undefined && details.sql_duration_ms !== null && (
            <div className="flex items-center gap-1.5 text-text-tertiary text-sm">
              <Clock className="w-3.5 h-3.5" />
              <span>{formatDuration(details.sql_duration_ms)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Query */}
      {details.sql_query && (
        <div className="border border-border rounded-lg overflow-hidden">
          <div className="px-4 py-2.5 bg-surface border-b border-border flex items-center justify-between">
            <span className="text-text-primary font-medium text-sm">Query</span>
            <CopyButton data={details.sql_query} size="sm" />
          </div>
          <div className="bg-background p-4 overflow-x-auto">
            <pre className="text-sm font-mono leading-relaxed whitespace-pre-wrap">
              {highlightedQuery}
            </pre>
          </div>
        </div>
      )}

      {/* Parameters */}
      {hasParameters && (
        <CollapsibleSection
          title="Parameters"
          icon={<Database className="w-4 h-4" />}
          actions={<CopyButton data={details.sql_parameters} size="sm" />}
        >
          <div className="space-y-1">
            {Object.entries(details.sql_parameters || {}).map(([key, value]) => (
              <div key={key} className="flex gap-2 font-mono text-sm">
                <span className="text-primary">{key}:</span>
                <span className="text-text-secondary">
                  {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                </span>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* Result Sample */}
      {hasResults && (
        <CollapsibleSection
          title="Result Sample"
          icon={<Table className="w-4 h-4" />}
          defaultOpen
          count={details.sql_result_sample?.length}
          actions={<CopyButton data={details.sql_result_sample} size="sm" />}
        >
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  {columns.map((col) => (
                    <th
                      key={col}
                      className="px-3 py-2 text-left text-primary font-medium text-xs uppercase tracking-wide"
                    >
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {details.sql_result_sample?.map((row, idx) => (
                  <tr key={idx} className="border-b border-border/50 last:border-0">
                    {columns.map((col) => (
                      <td key={col} className="px-3 py-2 text-text-secondary font-mono">
                        {typeof row[col] === 'object'
                          ? JSON.stringify(row[col])
                          : String(row[col] ?? '')}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CollapsibleSection>
      )}
    </div>
  );
}
