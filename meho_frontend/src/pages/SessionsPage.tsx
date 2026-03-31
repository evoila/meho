// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Sessions List Page
 *
 * Paginated list of session transcripts with filtering and navigation.
 * Part of the deep observability feature (TASK-186).
 */
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Activity,
  Clock,
  Zap,
  Bot,
  ChevronLeft,
  ChevronRight,
  AlertCircle,
  CheckCircle,
  Loader2,
  MessageSquare,
} from 'lucide-react';
import { Badge, Button, Card, Spinner } from '@/shared';
import { useSessionList } from '@/features/observability';
import type { SessionListItem } from '@/api/types';

/**
 * Format duration for display.
 */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

/**
 * Format timestamp for display.
 */
function formatTimestamp(timestamp: string): string {
  try {
    const date = new Date(timestamp);
    return date.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return timestamp;
  }
}

/**
 * Format token count for display.
 */
function formatTokens(tokens: number): string {
  if (tokens >= 1000) {
    return `${(tokens / 1000).toFixed(1)}k`;
  }
  return tokens.toString();
}

/**
 * Get status badge info.
 */
function getStatusInfo(status: string): {
  icon: React.ReactNode;
  variant: 'success' | 'error' | 'warning' | 'default';
  label: string;
} {
  switch (status) {
    case 'completed':
      return {
        icon: <CheckCircle className="w-3 h-3" />,
        variant: 'success',
        label: 'Completed',
      };
    case 'active':
    case 'running':
      return {
        icon: <Loader2 className="w-3 h-3 animate-spin" />,
        variant: 'warning',
        label: 'Running',
      };
    case 'error':
    case 'failed':
      return {
        icon: <AlertCircle className="w-3 h-3" />,
        variant: 'error',
        label: 'Failed',
      };
    default:
      return {
        icon: <Activity className="w-3 h-3" />,
        variant: 'default',
        label: status,
      };
  }
}

/**
 * Session card component.
 */
function SessionCard({ session }: { session: SessionListItem }) {
  const statusInfo = getStatusInfo(session.status);

  return (
    <Link
      to={`/sessions/${session.session_id}`}
      className="block transition-transform hover:scale-[1.01] focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 focus:ring-offset-background rounded-lg"
    >
      <Card className="p-4 hover:border-primary/50 transition-colors">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            <Badge variant={statusInfo.variant} className="flex items-center gap-1">
              {statusInfo.icon}
              {statusInfo.label}
            </Badge>
          </div>
          <span className="text-text-tertiary text-sm">
            {formatTimestamp(session.created_at)}
          </span>
        </div>

        {/* Session ID */}
        <div className="mb-3">
          <span className="font-mono text-text-secondary text-sm">
            {session.session_id.slice(0, 8)}...{session.session_id.slice(-4)}
          </span>
          {session.user_query && (
            <p className="text-text-tertiary text-sm mt-1 truncate flex items-center gap-1">
              <MessageSquare className="w-3 h-3 flex-shrink-0" />
              {session.user_query}
            </p>
          )}
        </div>

        {/* Stats Grid */}
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 pt-3 border-t border-border">
          {/* LLM Calls */}
          <div className="flex items-center gap-1.5">
            <Bot className="w-3.5 h-3.5 text-primary" />
            <span className="text-text-secondary text-sm">{session.total_llm_calls}</span>
            <span className="text-text-tertiary text-xs">LLM</span>
          </div>

          {/* Tokens */}
          <div className="flex items-center gap-1.5">
            <Zap className="w-3.5 h-3.5 text-amber-400" />
            <span className="text-text-secondary text-sm">{formatTokens(session.total_tokens)}</span>
            <span className="text-text-tertiary text-xs">tokens</span>
          </div>

          {/* Duration */}
          {session.total_duration_ms > 0 && (
            <div className="flex items-center gap-1.5">
              <Clock className="w-3.5 h-3.5 text-text-tertiary" />
              <span className="text-text-secondary text-sm">
                {formatDuration(session.total_duration_ms)}
              </span>
            </div>
          )}
        </div>
      </Card>
    </Link>
  );
}

/**
 * Status filter tabs.
 */
type StatusFilter = 'all' | 'completed' | 'active' | 'error';

/**
 * Sessions List Page.
 */
export function SessionsPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const {
    sessions,
    total,
    page,
    pageSize,
    hasMore,
    loading,
    error,
    nextPage,
    prevPage,
    refetch,
  } = useSessionList(
    { status: statusFilter === 'all' ? undefined : statusFilter },
    { initialPageSize: 12 }
  );

  const totalPages = Math.ceil(total / pageSize);

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <div className="flex-shrink-0 border-b border-border bg-surface px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-text-primary font-semibold text-lg flex items-center gap-2">
              <Activity className="w-5 h-5 text-primary" />
              Session Transcripts
            </h1>
            <p className="text-text-tertiary text-sm mt-0.5">
              Browse and analyze past session executions
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Refresh
          </Button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex-shrink-0 px-6 py-3 border-b border-border bg-surface/50">
        <div className="flex flex-wrap gap-2">
          {(['all', 'completed', 'active', 'error'] as const).map((status) => (
            <Badge
              key={status}
              variant={statusFilter === status ? 'primary' : 'default'}
              className="cursor-pointer capitalize"
              onClick={() => setStatusFilter(status)}
            >
              {status === 'all' ? 'All Sessions' : status}
            </Badge>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {loading && sessions.length === 0 ? (
          <div className="flex items-center justify-center h-64">
            <Spinner size="lg" />
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center h-64 gap-4">
            <AlertCircle className="w-12 h-12 text-red-400" />
            <p className="text-red-400">{error}</p>
            <Button variant="outline" onClick={() => refetch()}>
              Try Again
            </Button>
          </div>
        ) : sessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 gap-4">
            <Activity className="w-12 h-12 text-text-tertiary" />
            <p className="text-text-tertiary">No sessions found</p>
            {statusFilter !== 'all' && (
              <Button variant="outline" size="sm" onClick={() => setStatusFilter('all')}>
                Show All Sessions
              </Button>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            {/* Sessions Grid */}
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {sessions.map((session) => (
                <SessionCard key={session.session_id} session={session} />
              ))}
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between pt-4 border-t border-border">
                <span className="text-text-tertiary text-sm">
                  Page {page} of {totalPages} ({total} sessions)
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={prevPage}
                    disabled={page === 1 || loading}
                    className="flex items-center gap-1"
                  >
                    <ChevronLeft className="w-4 h-4" />
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={nextPage}
                    disabled={!hasMore || loading}
                    className="flex items-center gap-1"
                  >
                    Next
                    <ChevronRight className="w-4 h-4" />
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
