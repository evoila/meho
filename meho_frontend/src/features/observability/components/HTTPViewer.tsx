// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * HTTPViewer Component
 *
 * Displays HTTP request/response details with status badge and timing.
 * Shows method, URL, headers, and body with syntax highlighting.
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight, Clock, ArrowRight } from 'lucide-react';
import { cn, JsonViewer, CopyButton, Badge } from '@/shared';
import type { EventDetails } from '@/api/types';

export interface HTTPViewerProps {
  /** Event details containing HTTP data */
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
 * Get status badge variant based on HTTP status code.
 */
function getStatusVariant(code: number): 'success' | 'warning' | 'error' | 'info' {
  if (code >= 200 && code < 300) return 'success';
  if (code >= 300 && code < 400) return 'warning';
  if (code >= 400) return 'error';
  return 'info';
}

/**
 * Get method color class.
 */
function getMethodColor(method: string): string {
  switch (method.toUpperCase()) {
    case 'GET':
      return 'text-emerald-400';
    case 'POST':
      return 'text-amber-400';
    case 'PUT':
    case 'PATCH':
      return 'text-blue-400';
    case 'DELETE':
      return 'text-red-400';
    default:
      return 'text-text-secondary';
  }
}

interface CollapsibleSectionProps {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
  actions?: React.ReactNode;
  isEmpty?: boolean;
}

/**
 * Collapsible section component.
 */
function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
  actions,
  isEmpty = false,
}: CollapsibleSectionProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  if (isEmpty) {
    return null;
  }

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
        <span className="text-text-primary font-medium text-sm">{title}</span>
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
 * Try to parse JSON string or return as-is.
 */
function parseBody(body: string | null | undefined): unknown {
  if (!body) return null;
  try {
    return JSON.parse(body);
  } catch {
    return body;
  }
}

/**
 * HTTP request/response viewer.
 *
 * @example
 * ```tsx
 * <HTTPViewer
 *   details={{
 *     http_method: 'GET',
 *     http_url: 'https://api.example.com/vms',
 *     http_status_code: 200,
 *     http_headers: { 'Content-Type': 'application/json' },
 *     http_response_body: '{"vms": [...]}',
 *     http_duration_ms: 234
 *   }}
 * />
 * ```
 */
export function HTTPViewer({ details, className }: HTTPViewerProps) {
  const method = details.http_method?.toUpperCase() || 'GET';
  const statusCode = details.http_status_code;
  const hasHeaders = details.http_headers && Object.keys(details.http_headers).length > 0;
  const hasRequestBody = !!details.http_request_body;
  const hasResponseBody = !!details.http_response_body;

  const parsedRequestBody = parseBody(details.http_request_body);
  const parsedResponseBody = parseBody(details.http_response_body);

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      {/* Header with method, URL, status, and duration */}
      <div className="flex flex-col gap-2">
        {/* Method + URL */}
        <div className="flex items-center gap-3 flex-wrap">
          <span className={cn('font-mono font-bold text-lg', getMethodColor(method))}>
            {method}
          </span>
          <ArrowRight className="w-4 h-4 text-text-tertiary" />
          <span className="text-text-secondary font-mono text-sm break-all">
            {details.http_url || 'Unknown URL'}
          </span>
        </div>

        {/* Status + Duration */}
        <div className="flex items-center gap-3">
          {statusCode !== undefined && statusCode !== null && (
            <Badge variant={getStatusVariant(statusCode)}>
              {statusCode}
            </Badge>
          )}
          {details.http_duration_ms !== undefined && details.http_duration_ms !== null && (
            <div className="flex items-center gap-1.5 text-text-tertiary text-sm">
              <Clock className="w-3.5 h-3.5" />
              <span>{formatDuration(details.http_duration_ms)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Collapsible sections */}
      <div className="space-y-3">
        {/* Request Headers */}
        <CollapsibleSection
          title="Request Headers"
          isEmpty={!hasHeaders}
          actions={hasHeaders && <CopyButton data={details.http_headers} size="sm" />}
        >
          <div className="space-y-1">
            {Object.entries(details.http_headers || {}).map(([key, value]) => (
              <div key={key} className="flex gap-2 font-mono text-sm">
                <span className="text-primary">{key}:</span>
                <span className="text-text-secondary">{value}</span>
              </div>
            ))}
          </div>
        </CollapsibleSection>

        {/* Request Body */}
        <CollapsibleSection
          title="Request Body"
          isEmpty={!hasRequestBody}
          actions={hasRequestBody && <CopyButton data={parsedRequestBody} size="sm" />}
        >
          {typeof parsedRequestBody === 'object' ? (
            <JsonViewer data={parsedRequestBody} />
          ) : (
            <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono">
              {String(parsedRequestBody)}
            </pre>
          )}
        </CollapsibleSection>

        {/* Response Body */}
        <CollapsibleSection
          title="Response Body"
          defaultOpen
          isEmpty={!hasResponseBody}
          actions={hasResponseBody && <CopyButton data={parsedResponseBody} size="sm" />}
        >
          {typeof parsedResponseBody === 'object' ? (
            <JsonViewer data={parsedResponseBody} />
          ) : (
            <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono">
              {String(parsedResponseBody)}
            </pre>
          )}
        </CollapsibleSection>
      </div>
    </div>
  );
}
