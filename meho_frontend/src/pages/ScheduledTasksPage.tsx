// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Scheduled Tasks Page
 *
 * Main management page for scheduled tasks. Lists all tasks with inline toggle,
 * run-now button, edit/delete actions, and expandable run history.
 *
 * Phase 45 - Scheduled Tasks
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Plus,
  Calendar,
  Play,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronRight,
  Loader2,
  AlertTriangle,
} from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { getAPIClient } from '../lib/api-client';
import type { ScheduledTask } from '../api/types/scheduledTask';
import { ScheduledTaskModal } from '../features/scheduledTasks/components/ScheduledTaskModal';
import { ScheduledTaskRunHistory } from '../features/scheduledTasks/components/ScheduledTaskRunHistory';

// ---------------------------------------------------------------------------
// Status badge styles (matches webhook/connector pattern)
// ---------------------------------------------------------------------------

const STATUS_PILL: Record<string, { className: string; label: string }> = {
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
  },
};

function StatusBadge({ status }: Readonly<{ status: string | null }>) {
  if (!status) return <span className="text-zinc-500 text-xs">Never run</span>;
  const pill = STATUS_PILL[status] ?? {
    className: 'bg-zinc-400/10 text-zinc-400 border border-zinc-400/20',
    label: status,
  };
  return (
    <span
      className={clsx(
        'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
        pill.className
      )}
    >
      {pill.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Relative time helper
// ---------------------------------------------------------------------------

function relativeTime(iso: string | null): string {
  if (!iso) return '--';
  const d = new Date(iso);
  const now = new Date();
  const diff = d.getTime() - now.getTime();
  const absDiff = Math.abs(diff);
  const seconds = Math.floor(absDiff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  let label: string;
  if (days > 0) label = `${days}d`;
  else if (hours > 0) label = `${hours}h`;
  else if (minutes > 0) label = `${minutes}m`;
  else label = `${seconds}s`;

  return diff > 0 ? `in ${label}` : `${label} ago`;
}

// ---------------------------------------------------------------------------
// Toggle switch component
// ---------------------------------------------------------------------------

function ToggleSwitch({
  enabled,
  loading,
  onToggle,
}: Readonly<{
  enabled: boolean;
  loading: boolean;
  onToggle: () => void;
}>) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      disabled={loading}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      className={clsx(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent',
        'transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary/50',
        enabled ? 'bg-green-500' : 'bg-zinc-600',
        loading && 'opacity-50 cursor-not-allowed'
      )}
    >
      <span
        className={clsx(
          'pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0',
          'transition duration-200 ease-in-out',
          enabled ? 'translate-x-4' : 'translate-x-0'
        )}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main Page Component
// ---------------------------------------------------------------------------

export function ScheduledTasksPage() {
  const api = getAPIClient();
  const queryClient = useQueryClient();

  const [modalOpen, setModalOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);

  // ---- Queries ----
  const {
    data: tasks = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ['scheduled-tasks'],
    queryFn: () => api.getScheduledTasks(),
    refetchInterval: 30_000, // auto-refresh every 30s
  });

  // ---- Mutations ----
  const toggleMutation = useMutation({
    mutationFn: (taskId: string) => api.toggleScheduledTask(taskId),
    onMutate: async (taskId) => {
      await queryClient.cancelQueries({ queryKey: ['scheduled-tasks'] });
      const prev = queryClient.getQueryData<ScheduledTask[]>([
        'scheduled-tasks',
      ]);
      queryClient.setQueryData<ScheduledTask[]>(
        ['scheduled-tasks'],
        (old) =>
          old?.map((t) =>
            t.id === taskId ? { ...t, is_enabled: !t.is_enabled } : t
          ) ?? []
      );
      return { prev };
    },
    onError: (_err, _taskId, context) => {
      if (context?.prev) {
        queryClient.setQueryData(['scheduled-tasks'], context.prev);
      }
      toast.error('Failed to toggle task');
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] });
    },
  });

  const runNowMutation = useMutation({
    mutationFn: (taskId: string) => api.runScheduledTaskNow(taskId),
    onSuccess: (_data, _taskId) => {
      toast.success('Task triggered - investigation session started');
      queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] });
    },
    onError: () => {
      toast.error('Failed to trigger task');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (taskId: string) => api.deleteScheduledTask(taskId),
    onSuccess: () => {
      toast.success('Task deleted');
      queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] });
    },
    onError: () => {
      toast.error('Failed to delete task');
    },
  });

  // ---- Handlers ----
  const handleCreate = () => {
    setEditingTask(null);
    setModalOpen(true);
  };

  const handleEdit = (task: ScheduledTask) => {
    setEditingTask(task);
    setModalOpen(true);
  };

  const handleDelete = (task: ScheduledTask) => {
    if (confirm(`Delete scheduled task "${task.name}"?`)) {
      deleteMutation.mutate(task.id);
    }
  };

  const handleToggleExpand = (taskId: string) => {
    setExpandedTaskId((prev) => (prev === taskId ? null : taskId));
  };

  // ---- Loading / Error states ----
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-red-400">Failed to load scheduled tasks</p>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-white">Scheduled Tasks</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Automated investigations that run on a schedule
          </p>
        </div>
        <button
          onClick={handleCreate}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white font-medium text-sm hover:bg-primary/90 transition-colors"
        >
          <Plus className="h-4 w-4" />
          Create Task
        </button>
      </div>

      {/* Empty state */}
      {tasks.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <div className="w-16 h-16 rounded-2xl bg-zinc-800/50 border border-zinc-700/50 flex items-center justify-center mb-4">
            <Calendar className="h-8 w-8 text-zinc-500" />
          </div>
          <h3 className="text-lg font-medium text-white mb-2">
            No scheduled tasks yet
          </h3>
          <p className="text-sm text-zinc-400 max-w-sm mb-6">
            Create automated investigation tasks that run on a cron schedule.
            Each task creates a new session with your predefined prompt.
          </p>
          <button
            onClick={handleCreate}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white font-medium text-sm hover:bg-primary/90 transition-colors"
          >
            <Plus className="h-4 w-4" />
            Create your first task
          </button>
        </div>
      ) : (
        /* Task table */
        <div className="bg-zinc-800/50 border border-zinc-700/50 rounded-xl overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-zinc-700/50">
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3 w-8" />
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Name
                </th>
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Schedule
                </th>
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Timezone
                </th>
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Next Run
                </th>
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Last Run
                </th>
                <th className="text-left text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Enabled
                </th>
                <th className="text-right text-xs font-medium text-zinc-400 uppercase tracking-wider px-4 py-3">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700/30">
              {tasks.map((task) => (
                <TaskRow
                  key={task.id}
                  task={task}
                  expanded={expandedTaskId === task.id}
                  onToggleExpand={() => handleToggleExpand(task.id)}
                  onToggleEnabled={() => toggleMutation.mutate(task.id)}
                  toggleLoading={
                    toggleMutation.isPending &&
                    toggleMutation.variables === task.id
                  }
                  onRunNow={() => runNowMutation.mutate(task.id)}
                  runNowLoading={
                    runNowMutation.isPending &&
                    runNowMutation.variables === task.id
                  }
                  onEdit={() => handleEdit(task)}
                  onDelete={() => handleDelete(task)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create/Edit Modal */}
      <ScheduledTaskModal
        isOpen={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setEditingTask(null);
        }}
        task={editingTask}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Task Row Sub-component
// ---------------------------------------------------------------------------

function TaskRow({
  task,
  expanded,
  onToggleExpand,
  onToggleEnabled,
  toggleLoading,
  onRunNow,
  runNowLoading,
  onEdit,
  onDelete,
}: Readonly<{
  task: ScheduledTask;
  expanded: boolean;
  onToggleExpand: () => void;
  onToggleEnabled: () => void;
  toggleLoading: boolean;
  onRunNow: () => void;
  runNowLoading: boolean;
  onEdit: () => void;
  onDelete: () => void;
}>) {
  return (
    <>
      <tr className="hover:bg-zinc-700/20 transition-colors">
        {/* Expand chevron */}
        <td className="px-4 py-3">
          <button
            onClick={onToggleExpand}
            className="p-0.5 rounded text-zinc-400 hover:text-white transition-colors"
            title={expanded ? 'Collapse run history' : 'Expand run history'}
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </td>

        {/* Name */}
        <td className="px-4 py-3">
          <button
            onClick={onToggleExpand}
            className="text-left"
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-zinc-100">
                {task.name}
              </span>
              {task.delegation_active === false && (
                <span
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-400/10 text-amber-400 border border-amber-400/20"
                  title="The user who delegated credentials for this automation has been deactivated. Automated sessions will use service credentials only."
                  aria-label="Delegation inactive"
                >
                  <AlertTriangle className="h-3.5 w-3.5" />
                  Delegation inactive
                </span>
              )}
            </div>
            {task.description && (
              <span className="block text-xs text-zinc-500 mt-0.5 truncate max-w-[200px]">
                {task.description}
              </span>
            )}
          </button>
        </td>

        {/* Schedule */}
        <td className="px-4 py-3">
          <code className="text-xs text-zinc-300 bg-zinc-700/50 px-1.5 py-0.5 rounded font-mono">
            {task.cron_expression}
          </code>
        </td>

        {/* Timezone */}
        <td className="px-4 py-3">
          <span className="text-sm text-zinc-400">{task.timezone}</span>
        </td>

        {/* Next Run */}
        <td className="px-4 py-3">
          <span className="text-sm text-zinc-300">
            {task.is_enabled ? relativeTime(task.next_run_at) : '--'}
          </span>
        </td>

        {/* Last Run */}
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <StatusBadge status={task.last_run_status} />
            {task.last_run_at && (
              <span className="text-xs text-zinc-500">
                {relativeTime(task.last_run_at)}
              </span>
            )}
          </div>
        </td>

        {/* Enabled toggle */}
        <td className="px-4 py-3">
          <ToggleSwitch
            enabled={task.is_enabled}
            loading={toggleLoading}
            onToggle={onToggleEnabled}
          />
        </td>

        {/* Actions */}
        <td className="px-4 py-3 text-right">
          <div className="flex items-center justify-end gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRunNow();
              }}
              disabled={runNowLoading}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-amber-400 hover:bg-zinc-700/50 transition-colors disabled:opacity-50"
              title="Run Now"
            >
              {runNowLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-700/50 transition-colors"
              title="Edit"
            >
              <Pencil className="h-4 w-4" />
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-red-400 hover:bg-zinc-700/50 transition-colors"
              title="Delete"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
        </td>
      </tr>

      {/* Expanded run history */}
      {expanded && (
        <tr>
          <td colSpan={8} className="px-0 py-0">
            <div className="bg-zinc-900/50 border-t border-zinc-700/30 px-8 py-4">
              <ScheduledTaskRunHistory taskId={task.id} />
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
