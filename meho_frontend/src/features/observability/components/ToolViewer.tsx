// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ToolViewer Component
 *
 * Displays tool input/output side-by-side with syntax highlighting.
 * Shows error state if tool execution failed.
 */
import { Clock, AlertTriangle } from 'lucide-react';
import { cn, JsonViewer, CopyButton } from '@/shared';
import type { EventDetails } from '@/api/types';

export interface ToolViewerProps {
  /** Event details containing tool data */
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

/**
 * Side-by-side tool INPUT/OUTPUT viewer.
 * Similar layout to existing ToolCallModal in ConnectorCard.
 *
 * @example
 * ```tsx
 * <ToolViewer
 *   details={{
 *     tool_name: 'search_vms',
 *     tool_input: { filter: { name: 'prod-*' } },
 *     tool_output: { vms: [...], count: 5 },
 *     tool_duration_ms: 234
 *   }}
 * />
 * ```
 */
export function ToolViewer({ details, className }: ToolViewerProps) {
  const hasError = !!details.tool_error;

  return (
    <div className={cn('flex flex-col', className)}>
      {/* Header with tool name and duration */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-surface">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary/20 flex items-center justify-center">
            <div className="w-2.5 h-2.5 rounded-full bg-primary" />
          </div>
          <span className="text-text-primary font-semibold">
            {details.tool_name || 'Tool Call'}
          </span>
        </div>
        {details.tool_duration_ms !== undefined && details.tool_duration_ms !== null && (
          <div className="flex items-center gap-1.5 text-text-tertiary text-sm">
            <Clock className="w-3.5 h-3.5" />
            <span>{formatDuration(details.tool_duration_ms)}</span>
          </div>
        )}
      </div>

      {/* Error banner if tool failed */}
      {hasError && (
        <div className="flex items-center gap-2 px-4 py-2 bg-red-950/30 border-b border-red-900/30">
          <AlertTriangle className="w-4 h-4 text-red-400" />
          <span className="text-red-300 text-sm">{details.tool_error}</span>
        </div>
      )}

      {/* Side-by-side INPUT/OUTPUT */}
      <div className="grid grid-cols-2 gap-0">
        {/* INPUT */}
        <div className="border-r border-border flex flex-col">
          <div className="px-4 py-2 bg-surface border-b border-border flex-shrink-0 flex items-center justify-between">
            <span className="text-primary text-xs font-semibold tracking-wide uppercase">
              Input
            </span>
            {details.tool_input && <CopyButton data={details.tool_input} size="sm" />}
          </div>
          <div className="max-h-[55vh] overflow-auto bg-background p-4">
            {details.tool_input ? (
              <JsonViewer data={details.tool_input} />
            ) : (
              <span className="text-text-tertiary text-sm italic">No input data</span>
            )}
          </div>
        </div>

        {/* OUTPUT */}
        <div className="flex flex-col">
          <div className="px-4 py-2 bg-surface border-b border-border flex-shrink-0 flex items-center justify-between">
            <span className="text-primary text-xs font-semibold tracking-wide uppercase">
              Output
            </span>
            {details.tool_output != null && <CopyButton data={details.tool_output} size="sm" />}
          </div>
          <div className="max-h-[55vh] overflow-auto bg-background p-4">
            {details.tool_output ? (
              <JsonViewer data={details.tool_output} />
            ) : hasError ? (
              <span className="text-red-400 text-sm">Execution failed - see error above</span>
            ) : (
              <span className="text-text-tertiary text-sm italic">No output data</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
