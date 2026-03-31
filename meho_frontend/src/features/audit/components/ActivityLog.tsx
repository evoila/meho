// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ActivityLog Component
 *
 * Personal activity log for the "My Activity" tab.
 * Timeline-style layout showing the current user's own audit events.
 * Uses the useMyActivity hook (GET /api/audit/my-activity).
 */
import { useState, useCallback, useMemo } from 'react';
import {
  Plug,
  BookOpen,
  Settings,
  LogIn,
  LogOut,
  CheckSquare,
  XSquare,
  FileText,
  Loader2,
  ChevronDown,
  AlertCircle,
} from 'lucide-react';
import clsx from 'clsx';
import { useMyActivity } from '../hooks/useAuditEvents';
import type { AuditEvent } from '@/api/types/audit';

const PAGE_SIZE = 20;

/** Map event_type prefix to an icon */
function getEventIcon(eventType: string) {
  if (eventType.startsWith('connector.')) return Plug;
  if (eventType.startsWith('knowledge.')) return BookOpen;
  if (eventType.startsWith('config.')) return Settings;
  if (eventType === 'auth.login') return LogIn;
  if (eventType === 'auth.logout') return LogOut;
  if (eventType.startsWith('workflow.approve')) return CheckSquare;
  if (eventType.startsWith('workflow.deny')) return XSquare;
  return FileText;
}

/** Build a human-readable description from an audit event */
function describeEvent(event: AuditEvent): string {
  const name = event.resource_name ? ` '${event.resource_name}'` : '';
  const type = event.resource_type;

  switch (event.action) {
    case 'create':
      return `Created ${type}${name}`;
    case 'update':
      return `Updated ${type}${name}`;
    case 'delete':
      return `Deleted ${type}${name}`;
    case 'login':
      return 'Signed in';
    case 'logout':
      return 'Signed out';
    case 'approve':
      return `Approved workflow action${name}`;
    case 'deny':
      return `Denied workflow action${name}`;
    default:
      return `${event.action} ${type}${name}`;
  }
}

/** Relative time with more detail for recent events */
function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

export function ActivityLog() {
  const [limit, setLimit] = useState(PAGE_SIZE);

  const { data, isLoading, error } = useMyActivity(0, limit);

  const events = useMemo(() => data?.events ?? [], [data?.events]);
  const total = data?.total ?? 0;
  const hasMore = events.length < total;

  const handleLoadMore = useCallback(() => {
    setLimit((prev) => prev + PAGE_SIZE);
  }, []);

  // Group events by date for visual separation
  const groupedEvents = useMemo(() => {
    const groups: { date: string; events: AuditEvent[] }[] = [];
    let currentDate = '';

    for (const event of events) {
      const date = new Date(event.created_at).toLocaleDateString(undefined, {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
      });
      if (date !== currentDate) {
        currentDate = date;
        groups.push({ date, events: [event] });
      } else {
        groups[groups.length - 1].events.push(event);
      }
    }

    return groups;
  }, [events]);

  if (isLoading && events.length === 0) {
    return (
      <div className="text-center py-12">
        <Loader2 className="h-8 w-8 text-primary animate-spin mx-auto mb-3" />
        <p className="text-text-secondary text-sm">Loading your activity...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center gap-2 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20 text-sm">
        <AlertCircle className="h-4 w-4 flex-shrink-0" />
        <span>Failed to load activity: {(error as Error).message}</span>
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="text-center py-16">
        <FileText className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
        <p className="text-text-secondary font-medium mb-1">No activity recorded yet</p>
        <p className="text-sm text-text-tertiary">
          Your actions will appear here as you use the platform.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {groupedEvents.map((group) => (
        <div key={group.date}>
          {/* Date header */}
          <div className="sticky top-0 z-10 mb-3">
            <span className="text-xs font-medium text-text-tertiary uppercase tracking-wider">
              {group.date}
            </span>
          </div>

          {/* Timeline */}
          <div className="relative pl-8 space-y-1">
            {/* Vertical line */}
            <div className="absolute left-[11px] top-2 bottom-2 w-px bg-white/10" />

            {group.events.map((event) => {
              const Icon = getEventIcon(event.event_type);
              const isError = event.result === 'error' || event.result === 'failure';

              return (
                <div
                  key={event.id}
                  className="relative flex items-start gap-3 py-2 group"
                >
                  {/* Timeline dot */}
                  <div
                    className={clsx(
                      'absolute left-[-21px] top-3 w-[22px] h-[22px] rounded-full flex items-center justify-center border-2 border-background',
                      isError ? 'bg-red-500/20' : 'bg-surface',
                    )}
                  >
                    <Icon
                      className={clsx(
                        'h-3 w-3',
                        isError ? 'text-red-400' : 'text-text-tertiary group-hover:text-primary',
                      )}
                    />
                  </div>

                  {/* Content */}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-text-primary">
                      {describeEvent(event)}
                      {isError && (
                        <span className="ml-2 text-xs text-red-400">({event.result})</span>
                      )}
                    </p>
                    <p
                      className="text-xs text-text-tertiary mt-0.5"
                      title={new Date(event.created_at).toLocaleString()}
                    >
                      {relativeTime(event.created_at)}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {/* Load more */}
      {hasMore && (
        <div className="text-center pt-2">
          <button
            onClick={handleLoadMore}
            disabled={isLoading}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm text-text-secondary hover:text-white border border-white/10 rounded-lg hover:bg-surface-hover transition-colors disabled:opacity-50"
          >
            {isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <ChevronDown className="h-4 w-4" />
            )}
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
