// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ScheduledTaskRunHistory Component
 *
 * Expandable per-task run history table with status badges, duration,
 * prompt snippet, and session links. Supports Load More pagination.
 *
 * Phase 45 - Scheduled Tasks
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Loader2,
  ExternalLink,
  AlertCircle,
} from 'lucide-react';
import clsx from 'clsx';
import { useNavigate } from 'react-router-dom';
import { getAPIClient } from '../../../lib/api-client';
import { Tooltip } from '../../../shared/components/ui';
import type { ScheduledTaskRun } from '../../../api/types/scheduledTask';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;

const STATUS_STYLES: Record<
  string,
  { className: string; label: string; pulse?: boolean }
> = {
  success: {
    className: 'bg-green-400/10 text-green-400 border border-green-400/20',
    label: 'Success',
  },
  failed: {
    className: 'bg-red-400/10 text-red-400 border border-red-400/20',
    label: 'Failed',
  },
  running: {
    className: 'bg-amber-400/10 text-amber-400 border border-amber-400/20',
    label: 'Running',
    pulse: true,
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '--';
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function truncate(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + '...';
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ScheduledTaskRunHistoryProps {
  taskId: string;
}

export function ScheduledTaskRunHistory({
  taskId,
}: Readonly<ScheduledTaskRunHistoryProps>) {
  const api = getAPIClient();
  const navigate = useNavigate();
  const [limit, setLimit] = useState(PAGE_SIZE);

  const { data: runs = [], isLoading } = useQuery({
    queryKey: ['scheduled-task-runs', taskId, limit],
    queryFn: () => api.getScheduledTaskRuns(taskId, limit, 0),
    refetchInterval: 15_000, // refresh run history more frequently
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-5 w-5 animate-spin text-zinc-400" />
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="text-center py-8">
        <p className="text-sm text-zinc-500">No runs yet</p>
      </div>
    );
  }

  return (
    <div>
      <h4 className="text-xs font-medium text-zinc-400 uppercase tracking-wider mb-3">
        Run History
      </h4>

      <table className="w-full">
        <thead>
          <tr className="border-b border-zinc-700/30">
            <th className="text-left text-xs font-medium text-zinc-500 px-3 py-2">
              Timestamp
            </th>
            <th className="text-left text-xs font-medium text-zinc-500 px-3 py-2">
              Status
            </th>
            <th className="text-left text-xs font-medium text-zinc-500 px-3 py-2">
              Duration
            </th>
            <th className="text-left text-xs font-medium text-zinc-500 px-3 py-2">
              Prompt
            </th>
            <th className="text-right text-xs font-medium text-zinc-500 px-3 py-2">
              Session
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-700/20">
          {runs.map((run) => (
            <RunRow key={run.id} run={run} navigate={navigate} />
          ))}
        </tbody>
      </table>

      {/* Load More */}
      {runs.length >= limit && (
        <div className="flex justify-center mt-3">
          <button
            onClick={() => setLimit((prev) => prev + PAGE_SIZE)}
            className="text-xs text-zinc-400 hover:text-zinc-200 transition-colors px-3 py-1.5 rounded-lg border border-zinc-700/50 hover:border-zinc-600"
          >
            Load More
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run Row Sub-component
// ---------------------------------------------------------------------------

function RunRow({
  run,
  navigate,
}: Readonly<{
  run: ScheduledTaskRun;
  navigate: (path: string) => void;
}>) {
  const statusStyle = STATUS_STYLES[run.status] ?? {
    className: 'bg-zinc-400/10 text-zinc-400 border border-zinc-400/20',
    label: run.status,
  };

  return (
    <tr className="hover:bg-zinc-800/30 transition-colors">
      {/* Timestamp */}
      <td className="px-3 py-2">
        <span className="text-xs text-zinc-300">
          {formatTimestamp(run.started_at)}
        </span>
      </td>

      {/* Status badge */}
      <td className="px-3 py-2">
        <span
          className={clsx(
            'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium',
            statusStyle.className,
            statusStyle.pulse && 'animate-pulse'
          )}
        >
          {statusStyle.label}
          {run.status === 'failed' && run.error_message && (
            <Tooltip content={run.error_message}>
              <AlertCircle className="h-3 w-3 ml-0.5 cursor-help" />
            </Tooltip>
          )}
        </span>
      </td>

      {/* Duration */}
      <td className="px-3 py-2">
        <span className="text-xs text-zinc-400">
          {formatDuration(run.duration_seconds)}
        </span>
      </td>

      {/* Prompt snippet */}
      <td className="px-3 py-2">
        <span className="text-xs text-zinc-500 font-mono">
          {truncate(run.prompt_snapshot, 60)}
        </span>
      </td>

      {/* Session link */}
      <td className="px-3 py-2 text-right">
        {run.session_id ? (
          <button
            onClick={() => navigate(`/chat?session=${run.session_id}`)}
            className="inline-flex items-center gap-1 text-xs text-primary hover:text-primary/80 transition-colors"
          >
            View Session
            <ExternalLink className="h-3 w-3" />
          </button>
        ) : (
          <span className="text-xs text-zinc-600">--</span>
        )}
      </td>
    </tr>
  );
}
