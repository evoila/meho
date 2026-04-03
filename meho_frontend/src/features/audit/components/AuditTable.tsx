// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * AuditTable Component
 *
 * Filterable audit event table for admin and contextual views.
 * Columns: Time, User, Event Type, Action, Resource, Result.
 * Expandable row detail showing JSON `details` payload.
 * Accepts `defaultFilters` for contextual usage (e.g. ConnectorsPage).
 */
import { useState, useMemo, useCallback } from 'react';
import {
  ChevronDown,
  ChevronRight,
  ChevronLeft,
  Filter,
  CheckCircle2,
  XCircle,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { useAuditEvents } from '../hooks/useAuditEvents';
import type { AuditEvent, AuditEventFilters } from '@/api/types/audit';

interface AuditTableProps {
  /** Pre-set filters for contextual usage (hides those filter controls). */
  defaultFilters?: Partial<AuditEventFilters>;
  /** Page size limit override. */
  limit?: number;
}

const PAGE_SIZE = 20;

const RESULT_STYLES: Record<string, { bg: string; text: string; icon: typeof CheckCircle2 }> = {
  success: { bg: 'bg-green-400/10', text: 'text-green-400', icon: CheckCircle2 },
  failure: { bg: 'bg-amber-500/10', text: 'text-amber-400', icon: AlertTriangle },
  error: { bg: 'bg-red-500/10', text: 'text-red-400', icon: XCircle },
};

/** Relative time formatter: "2 min ago", "3h ago", "5d ago" */
function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Human-readable event type label */
function formatEventType(type: string): string {
  return type.replaceAll(/\./g, ' ').replaceAll(/\b\w/g, (c) => c.toUpperCase());
}

export function AuditTable({ defaultFilters, limit }: Readonly<AuditTableProps>) {
  const pageSize = limit ?? PAGE_SIZE;
  const [offset, setOffset] = useState(0);
  const [expandedRow, setExpandedRow] = useState<string | null>(null);

  // User-controlled filters (supplement to defaultFilters)
  const [eventTypeFilter, setEventTypeFilter] = useState('');
  const [resourceTypeFilter, setResourceTypeFilter] = useState('');
  const [resultFilter, setResultFilter] = useState('');

  const filters = useMemo<AuditEventFilters>(() => {
    const f: AuditEventFilters = {
      ...defaultFilters,
      offset,
      limit: pageSize,
    };
    if (eventTypeFilter) f.event_type = eventTypeFilter;
    if (resourceTypeFilter && !defaultFilters?.resource_type) {
      f.resource_type = resourceTypeFilter;
    }
    return f;
  }, [defaultFilters, offset, pageSize, eventTypeFilter, resourceTypeFilter]);

  const { data, isLoading, error } = useAuditEvents(filters);

  const events = useMemo(() => data?.events ?? [], [data?.events]);
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / pageSize);
  const currentPage = Math.floor(offset / pageSize) + 1;

  const handlePrev = useCallback(() => {
    setOffset((prev) => Math.max(0, prev - pageSize));
  }, [pageSize]);

  const handleNext = useCallback(() => {
    setOffset((prev) => (prev + pageSize < total ? prev + pageSize : prev));
  }, [pageSize, total]);

  const toggleRow = useCallback((id: string) => {
    setExpandedRow((prev) => (prev === id ? null : id));
  }, []);

  // When filters change, reset offset
  const handleFilterChange = useCallback(
    (setter: (v: string) => void) => (value: string) => {
      setter(value);
      setOffset(0);
    },
    [],
  );

  // Derive unique values for filter dropdowns from visible events
  const eventTypes = useMemo(() => {
    const set = new Set(events.map((e) => e.event_type));
    return Array.from(set).sort();
  }, [events]);

  const resourceTypes = useMemo(() => {
    const set = new Set(events.map((e) => e.resource_type));
    return Array.from(set).sort();
  }, [events]);

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <Filter className="h-4 w-4 text-text-tertiary" />

        <select
          value={eventTypeFilter}
          onChange={(e) => handleFilterChange(setEventTypeFilter)(e.target.value)}
          className="bg-surface border border-white/10 rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
        >
          <option value="">All Event Types</option>
          {eventTypes.map((t) => (
            <option key={t} value={t}>
              {formatEventType(t)}
            </option>
          ))}
        </select>

        {/* Hide resource type filter when default is set */}
        {!defaultFilters?.resource_type && (
          <select
            value={resourceTypeFilter}
            onChange={(e) => handleFilterChange(setResourceTypeFilter)(e.target.value)}
            className="bg-surface border border-white/10 rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
          >
            <option value="">All Resources</option>
            {resourceTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        )}

        <select
          value={resultFilter}
          onChange={(e) => handleFilterChange(setResultFilter)(e.target.value)}
          className="bg-surface border border-white/10 rounded-lg px-3 py-1.5 text-sm text-text-primary focus:outline-none focus:ring-1 focus:ring-primary/50"
        >
          <option value="">All Results</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="error">Error</option>
        </select>
      </div>

      {/* Loading */}
      {isLoading && (
        <div className="text-center py-8">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary mx-auto mb-3" />
          <p className="text-text-secondary text-sm">Loading audit events...</p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="flex items-center gap-2 p-3 bg-red-500/10 text-red-400 rounded-lg border border-red-500/20 text-sm">
          <XCircle className="h-4 w-4 flex-shrink-0" />
          <span>Failed to load audit events: {(error as Error).message}</span>
        </div>
      )}

      {/* Table */}
      {!isLoading && !error && (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface/70 text-text-secondary border-b border-white/10">
                <th className="w-8 px-3 py-3" />
                <th className="text-left px-4 py-3 font-medium">Time</th>
                <th className="text-left px-4 py-3 font-medium">User</th>
                <th className="text-left px-4 py-3 font-medium">Event Type</th>
                <th className="text-left px-4 py-3 font-medium">Action</th>
                {!defaultFilters?.resource_type && (
                  <th className="text-left px-4 py-3 font-medium">Resource</th>
                )}
                <th className="text-left px-4 py-3 font-medium">Name</th>
                <th className="text-left px-4 py-3 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {events.length === 0 && (
                <tr>
                  <td
                    colSpan={defaultFilters?.resource_type ? 7 : 8}
                    className="text-center py-12 text-text-tertiary"
                  >
                    No audit events found
                  </td>
                </tr>
              )}
              {events
                .filter(
                  (e) => !resultFilter || e.result === resultFilter,
                )
                .map((event) => (
                  <AuditRow
                    key={event.id}
                    event={event}
                    expanded={expandedRow === event.id}
                    onToggle={() => toggleRow(event.id)}
                    hideResourceType={!!defaultFilters?.resource_type}
                  />
                ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {total > pageSize && (
        <div className="flex items-center justify-between text-sm text-text-secondary">
          <span>
            Showing {offset + 1}--{Math.min(offset + pageSize, total)} of {total}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={handlePrev}
              disabled={offset === 0}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-white/10 hover:bg-surface-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
              Prev
            </button>
            <span className="px-2">
              Page {currentPage} of {totalPages}
            </span>
            <button
              onClick={handleNext}
              disabled={offset + pageSize >= total}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-white/10 hover:bg-surface-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row sub-component
// ---------------------------------------------------------------------------

interface AuditRowProps {
  event: AuditEvent;
  expanded: boolean;
  onToggle: () => void;
  hideResourceType: boolean;
}

function AuditRow({ event, expanded, onToggle, hideResourceType }: Readonly<AuditRowProps>) {
  const style = RESULT_STYLES[event.result] ?? RESULT_STYLES.error;
  const ResultIcon = style.icon;

  return (
    <>
      <tr
        onClick={onToggle}
        className={clsx(
          'border-b border-white/5 cursor-pointer transition-colors',
          expanded ? 'bg-surface/50' : 'hover:bg-surface/30',
        )}
      >
        <td className="px-3 py-3 text-text-tertiary">
          {(() => {
            if (!event.details) return <span className="w-4" />;
            return expanded
              ? <ChevronDown className="h-4 w-4" />
              : <ChevronRight className="h-4 w-4" />;
          })()}
        </td>
        <td className="px-4 py-3 text-text-secondary whitespace-nowrap" title={new Date(event.created_at).toLocaleString()}>
          {relativeTime(event.created_at)}
        </td>
        <td className="px-4 py-3 text-text-primary truncate max-w-[160px]">
          {event.user_email ?? event.user_id}
        </td>
        <td className="px-4 py-3 text-text-secondary">
          <span className="px-2 py-0.5 rounded-md bg-white/5 border border-white/10 text-xs font-medium">
            {event.event_type}
          </span>
        </td>
        <td className="px-4 py-3 text-text-primary capitalize">{event.action}</td>
        {!hideResourceType && (
          <td className="px-4 py-3 text-text-secondary">{event.resource_type}</td>
        )}
        <td className="px-4 py-3 text-text-primary truncate max-w-[200px]">
          {event.resource_name ?? event.resource_id ?? '--'}
        </td>
        <td className="px-4 py-3">
          <span
            className={clsx(
              'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border',
              style.bg,
              style.text,
              `border-current/20`,
            )}
          >
            <ResultIcon className="h-3 w-3" />
            {event.result}
          </span>
        </td>
      </tr>

      {/* Expandable detail row */}
      {expanded && event.details && (
        <tr className="bg-surface/30">
          <td />
          <td colSpan={hideResourceType ? 6 : 7} className="px-4 py-3">
            <div className="text-xs font-mono text-text-secondary bg-black/20 rounded-lg p-3 overflow-x-auto max-h-48 overflow-y-auto">
              <pre>{JSON.stringify(event.details, null, 2)}</pre>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
