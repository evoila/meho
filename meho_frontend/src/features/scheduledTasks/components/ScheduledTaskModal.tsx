// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ScheduledTaskModal Component
 *
 * Create/edit modal for scheduled tasks. Features NL-first cron input
 * with conversion + next-5-runs preview, timezone selector, and prompt textarea.
 *
 * Phase 45 - Scheduled Tasks
 */

import { useState, useCallback, useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Wand2, ChevronDown, ChevronUp, Shield, Info, X } from 'lucide-react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { Modal, Button } from '../../../shared/components/ui';
import { getAPIClient } from '../../../lib/api-client';
import type {
  ScheduledTask,
  CreateScheduledTaskRequest,
  UpdateScheduledTaskRequest,
} from '../../../api/types/scheduledTask';
import type { Connector } from '../../../lib/api-client';
import { NotificationTargetConfig } from '@/components/connectors/NotificationTargetConfig';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ScheduledTaskModalProps {
  isOpen: boolean;
  onClose: () => void;
  task: ScheduledTask | null; // null = create, object = edit
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ScheduledTaskModal({ // NOSONAR (cognitive complexity)
  isOpen,
  onClose,
  task,
}: Readonly<ScheduledTaskModalProps>) {
  const api = getAPIClient();
  const queryClient = useQueryClient();
  const isEdit = task !== null;

  // ---- Form state ----
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [nlInput, setNlInput] = useState('');
  const [cronExpression, setCronExpression] = useState('');
  const [timezone, setTimezone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone
  );
  const [prompt, setPrompt] = useState('');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [nextRuns, setNextRuns] = useState<string[]>([]);
  const [humanReadable, setHumanReadable] = useState<string | null>(null);
  const [tzSearch, setTzSearch] = useState('');
  const [tzDropdownOpen, setTzDropdownOpen] = useState(false);

  // Phase 74: Automation settings
  const [allowedConnectorIds, setAllowedConnectorIds] = useState<string[] | null>(null);
  const [delegateCredentials, setDelegateCredentials] = useState(false);
  const [automationExpanded, setAutomationExpanded] = useState(false);

  // Phase 75: Notification targets
  const [notificationTargets, setNotificationTargets] = useState<Array<{ connector_id: string; contact: string }>>([]);

  // Load all tenant connectors for the scope multi-select
  const { data: allConnectors } = useQuery({
    queryKey: ['all-connectors'],
    queryFn: () => api.listConnectors(),
  });

  // ---- Populate on open / reset on close (setState-during-render) ----
  const [prevOpen, setPrevOpen] = useState(false);
  const [prevTask, setPrevTask] = useState<ScheduledTask | null>(null);
  if (isOpen !== prevOpen || task !== prevTask) {
    setPrevOpen(isOpen);
    setPrevTask(task);
    if (isOpen) {
      if (task) {
        setName(task.name);
        setDescription(task.description ?? '');
        setCronExpression(task.cron_expression);
        setTimezone(task.timezone);
        setPrompt(task.prompt);
        setNlInput('');
        setShowAdvanced(true); // show cron when editing
        setNextRuns([]);
        setHumanReadable(null);
        // Phase 74: restore automation settings
        setAllowedConnectorIds(task.allowed_connector_ids ?? null);
        setDelegateCredentials(task.delegate_credentials ?? false);
        setAutomationExpanded(!!task.allowed_connector_ids || !!task.delegate_credentials);
        // Phase 75: restore notification targets
        setNotificationTargets(task.notification_targets ?? []);
      } else {
        setName('');
        setDescription('');
        setCronExpression('');
        setTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone);
        setPrompt('');
        setNlInput('');
        setShowAdvanced(false);
        setNextRuns([]);
        setHumanReadable(null);
        // Phase 74: reset automation settings
        setAllowedConnectorIds(null);
        setDelegateCredentials(false);
        setAutomationExpanded(false);
        // Phase 75: reset notification targets
        setNotificationTargets([]);
      }
      setTzSearch('');
      setTzDropdownOpen(false);
    }
  }

  // ---- Timezones ----
  const { data: timezones = [] } = useQuery({
    queryKey: ['scheduled-task-timezones'],
    queryFn: () => api.getTimezones(),
    staleTime: Infinity, // timezones never change
  });

  const filteredTimezones = useMemo(() => {
    if (!tzSearch) return timezones.slice(0, 50); // show first 50 when no search
    const lower = tzSearch.toLowerCase();
    return timezones.filter((tz) => tz.toLowerCase().includes(lower)).slice(0, 50);
  }, [timezones, tzSearch]);

  // ---- NL-to-cron conversion ----
  const parseMutation = useMutation({
    mutationFn: ({ text, tz }: { text: string; tz: string }) =>
      api.parseSchedule(text, tz),
    onSuccess: (data) => {
      setCronExpression(data.cron_expression);
      setNextRuns(data.next_runs);
      setHumanReadable(data.human_readable);
      setShowAdvanced(true);
    },
    onError: () => {
      toast.error('Could not parse schedule. Try a different phrasing.');
    },
  });

  const handleConvert = useCallback(() => {
    const text = nlInput.trim();
    if (!text) return;
    parseMutation.mutate({ text, tz: timezone });
  }, [nlInput, timezone, parseMutation]);

  const handleNlKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleConvert();
    }
  };

  // ---- Cron validation (when raw cron changes) ----
  const validateMutation = useMutation({
    mutationFn: ({ cron, tz }: { cron: string; tz: string }) =>
      api.validateCron(cron, tz),
    onSuccess: (data) => {
      if (data.is_valid) {
        setNextRuns(data.next_runs);
      } else {
        setNextRuns([]);
      }
    },
  });

  const handleCronBlur = () => {
    if (cronExpression.trim()) {
      validateMutation.mutate({ cron: cronExpression.trim(), tz: timezone });
    }
  };

  // ---- Submit ----
  const createMutation = useMutation({
    mutationFn: (data: CreateScheduledTaskRequest) =>
      api.createScheduledTask(data),
    onSuccess: () => {
      toast.success('Scheduled task created');
      queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] });
      onClose();
    },
    onError: () => {
      toast.error('Failed to create task');
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: UpdateScheduledTaskRequest;
    }) => api.updateScheduledTask(id, data),
    onSuccess: () => {
      toast.success('Scheduled task updated');
      queryClient.invalidateQueries({ queryKey: ['scheduled-tasks'] });
      onClose();
    },
    onError: () => {
      toast.error('Failed to update task');
    },
  });

  const generatePromptMutation = useMutation({
    mutationFn: () => api.generateScheduledTaskPrompt(),
    onSuccess: (data) => {
      setPrompt(data.prompt);
    },
    onError: () => {
      toast.error('Could not generate prompt. Try again.');
    },
  });

  const isSubmitting = createMutation.isPending || updateMutation.isPending;
  const canSubmit =
    name.trim() && cronExpression.trim() && prompt.trim() && !isSubmitting;

  const handleSubmit = () => {
    if (!canSubmit) return;
    if (isEdit && task) {
      updateMutation.mutate({
        id: task.id,
        data: {
          name: name.trim(),
          description: description.trim() || undefined,
          cron_expression: cronExpression.trim(),
          timezone,
          prompt: prompt.trim(),
          allowed_connector_ids: allowedConnectorIds,
          delegate_credentials: delegateCredentials,
          notification_targets: notificationTargets.filter(t => t.connector_id && t.contact),
        },
      });
    } else {
      createMutation.mutate({
        name: name.trim(),
        description: description.trim() || undefined,
        cron_expression: cronExpression.trim(),
        timezone,
        prompt: prompt.trim(),
        allowed_connector_ids: allowedConnectorIds,
        delegate_credentials: delegateCredentials,
        notification_targets: notificationTargets.filter(t => t.connector_id && t.contact),
      });
    }
  };

  // ---- Render ----
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isEdit ? 'Edit Scheduled Task' : 'Create Scheduled Task'}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!canSubmit}
          >
            {isSubmitting && (
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
            )}
            {isEdit ? 'Update Task' : 'Create Task'}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        {/* Name */}
        <div>
          <label htmlFor="scheduled-task-name" className="block text-sm font-medium text-zinc-300 mb-1.5">
            Name <span className="text-red-400">*</span>
          </label>
          <input
            id="scheduled-task-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., Daily cluster health check"
            className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
          />
        </div>

        {/* Description */}
        <div>
          <label htmlFor="scheduled-task-description" className="block text-sm font-medium text-zinc-300 mb-1.5">
            Description
          </label>
          <input
            id="scheduled-task-description"
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional description"
            className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
          />
        </div>

        {/* Schedule input (NL-first) */}
        <div>
          <label htmlFor="scheduled-task-schedule" className="block text-sm font-medium text-zinc-300 mb-1.5">
            Schedule <span className="text-red-400">*</span>
          </label>

          {/* NL input row */}
          <div className="flex gap-2">
            <input
              id="scheduled-task-schedule"
              type="text"
              value={nlInput}
              onChange={(e) => setNlInput(e.target.value)}
              onKeyDown={handleNlKeyDown}
              placeholder="e.g., every weekday at 9 AM"
              className="flex-1 px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
            />
            <button
              onClick={handleConvert}
              disabled={!nlInput.trim() || parseMutation.isPending}
              className={clsx(
                'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                'bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            >
              {parseMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Wand2 className="h-4 w-4" />
              )}
              Convert
            </button>
          </div>

          {/* Human readable result */}
          {humanReadable && (
            <p className="mt-1.5 text-xs text-zinc-400">
              {humanReadable}
            </p>
          )}

          {/* Advanced toggle */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="mt-2 inline-flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
          >
            {showAdvanced ? (
              <ChevronUp className="h-3 w-3" />
            ) : (
              <ChevronDown className="h-3 w-3" />
            )}
            Advanced (raw cron)
          </button>

          {/* Raw cron input */}
          {showAdvanced && (
            <div className="mt-2">
              <input
                type="text"
                value={cronExpression}
                onChange={(e) => setCronExpression(e.target.value)}
                onBlur={handleCronBlur}
                placeholder="*/5 * * * *"
                className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm font-mono placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
              />
              {validateMutation.data && !validateMutation.data.is_valid && (
                <p className="mt-1 text-xs text-red-400">
                  {validateMutation.data.error || 'Invalid cron expression'}
                </p>
              )}
            </div>
          )}

          {/* Next 5 runs preview */}
          {nextRuns.length > 0 && (
            <div className="mt-3 p-3 rounded-lg bg-zinc-900/50 border border-zinc-700/30">
              <p className="text-xs font-medium text-zinc-400 mb-1.5">
                Next 5 runs
              </p>
              <ul className="space-y-0.5">
                {nextRuns.map((run, i) => {
                  const d = new Date(run);
                  const dayName = d.toLocaleDateString(undefined, {
                    weekday: 'short',
                  });
                  const formatted = d.toLocaleString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false,
                  });
                  return (
                    <li key={`run-${i}`} className="text-xs text-zinc-300">
                      <span className="text-zinc-500 w-8 inline-block">
                        {dayName}
                      </span>{' '}
                      {formatted}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>

        {/* Timezone */}
        <div className="relative">
          <label htmlFor="scheduled-task-timezone" className="block text-sm font-medium text-zinc-300 mb-1.5">
            Timezone
          </label>
          <div className="relative">
            <input
              id="scheduled-task-timezone"
              type="text"
              value={tzDropdownOpen ? tzSearch : timezone}
              onChange={(e) => {
                setTzSearch(e.target.value);
                if (!tzDropdownOpen) setTzDropdownOpen(true);
              }}
              onFocus={() => {
                setTzDropdownOpen(true);
                setTzSearch('');
              }}
              onBlur={() => {
                // Delay to allow click on dropdown item
                setTimeout(() => setTzDropdownOpen(false), 200);
              }}
              placeholder="Search timezones..."
              className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
            />
            {tzDropdownOpen && filteredTimezones.length > 0 && (
              <ul className="absolute z-50 mt-1 w-full max-h-48 overflow-y-auto rounded-lg bg-zinc-800 border border-zinc-700 shadow-xl scrollbar-purple">
                {filteredTimezones.map((tz) => (
                  <li key={tz}>
                    <button
                      type="button"
                      className={clsx(
                        'w-full text-left px-3 py-1.5 text-sm hover:bg-zinc-700/50 transition-colors',
                        tz === timezone
                          ? 'text-primary font-medium'
                          : 'text-zinc-300'
                      )}
                      onMouseDown={(e) => {
                        e.preventDefault(); // prevent blur
                        setTimezone(tz);
                        setTzDropdownOpen(false);
                        setTzSearch('');
                        // Re-validate cron with new timezone
                        if (cronExpression.trim()) {
                          validateMutation.mutate({
                            cron: cronExpression.trim(),
                            tz,
                          });
                        }
                      }}
                    >
                      {tz}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Prompt */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label htmlFor="scheduled-task-prompt" className="text-sm font-medium text-zinc-300">
              Investigation Prompt <span className="text-red-400">*</span>
            </label>
            <button
              type="button"
              onClick={() => generatePromptMutation.mutate()}
              disabled={generatePromptMutation.isPending}
              className={clsx(
                'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors',
                'bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            >
              {generatePromptMutation.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Wand2 className="h-3 w-3" />
              )}
              Generate
            </button>
          </div>
          <textarea
            id="scheduled-task-prompt"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={4}
            placeholder="This prompt will be sent as the first message in each scheduled investigation session."
            className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 resize-y"
          />
          <p className="mt-1 text-xs text-zinc-500">
            This prompt will be sent as the first message in each scheduled
            investigation session.
          </p>
        </div>

        {/* Automation Settings (Phase 74) */}
        <div className="border-t border-zinc-700/50 pt-4 mt-2">
          <button
            type="button"
            onClick={() => setAutomationExpanded(!automationExpanded)}
            className="w-full flex items-center justify-between"
          >
            <h4 className="text-sm font-medium text-white flex items-center gap-2">
              <div className="w-1 h-4 bg-accent rounded-full" />
              Automation Settings
            </h4>
            {automationExpanded ? (
              <ChevronUp className="h-4 w-4 text-zinc-500" />
            ) : (
              <ChevronDown className="h-4 w-4 text-zinc-500" />
            )}
          </button>

          {automationExpanded && (
            <div className="mt-4 space-y-5">
              {/* Connector Access Scope */}
              <div>
                <label htmlFor="task-connector-scope" className="flex items-center gap-2 text-xs text-zinc-400 mb-1.5 font-medium">
                  <Shield className="h-4 w-4 text-zinc-500" />
                  Connector Access Scope
                </label>
                <p className="text-xs text-zinc-500 mb-2">
                  Which connectors can this task's automated session access? Default: all connectors.
                </p>
                <select
                  id="task-connector-scope"
                  value=""
                  onChange={(e) => {
                    if (e.target.value) {
                      const newIds = [...(allowedConnectorIds || []), e.target.value];
                      setAllowedConnectorIds(newIds);
                      e.target.value = '';
                    }
                  }}
                  className="w-full px-3 py-2 rounded-lg bg-zinc-800/50 border border-zinc-700/50 text-zinc-100 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
                >
                  <option value="" className="bg-zinc-800">
                    {allowedConnectorIds === null ? 'All connectors (default)' : 'Add a connector to scope...'}
                  </option>
                  {(allConnectors || [])
                    .filter((c: Connector) => !(allowedConnectorIds || []).includes(c.id))
                    .map((c: Connector) => (
                      <option key={c.id} value={c.id} className="bg-zinc-800">
                        {c.name} ({c.connector_type})
                      </option>
                    ))}
                </select>

                {/* Selected connector pills */}
                {allowedConnectorIds && allowedConnectorIds.length > 0 && (
                  <div className="flex flex-wrap gap-2 mt-2">
                    {allowedConnectorIds.map((cid) => {
                      const conn = (allConnectors || []).find((c: Connector) => c.id === cid);
                      return (
                        <span
                          key={cid}
                          className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-xs bg-primary/10 text-primary border border-primary/20"
                        >
                          {conn?.name || cid.slice(0, 8)}
                          <button
                            type="button"
                            onClick={() => {
                              const newIds = allowedConnectorIds.filter((id) => id !== cid);
                              setAllowedConnectorIds(newIds.length > 0 ? newIds : null);
                            }}
                            className="ml-1 hover:text-red-400 transition-colors"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        </span>
                      );
                    })}
                    <button
                      type="button"
                      onClick={() => setAllowedConnectorIds(null)}
                      className="text-xs text-zinc-500 hover:text-white transition-colors"
                    >
                      Reset to all
                    </button>
                  </div>
                )}
              </div>

              {/* Credential Delegation Consent */}
              <div>
                <div className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="task-delegate-credentials"
                    checked={delegateCredentials}
                    onChange={(e) => setDelegateCredentials(e.target.checked)}
                    className="h-4 w-4 rounded border-zinc-600 bg-zinc-800/50 text-primary focus:ring-2 focus:ring-primary-500"
                  />
                  <label htmlFor="task-delegate-credentials" className="text-sm text-zinc-300 cursor-pointer">
                    Allow credential delegation
                  </label>
                </div>
                <p className="text-xs text-zinc-500 mt-1 ml-7">
                  If no service credential exists, MEHO may use your stored credentials for automated investigations
                </p>

                {delegateCredentials && (
                  <div className="mt-3 ml-7 bg-blue-500/10 border border-blue-500/20 rounded-xl p-4 flex items-start gap-3">
                    <Info className="h-5 w-5 text-blue-400 flex-shrink-0 mt-0.5" />
                    <p className="text-xs text-blue-200">
                      Your credentials will be used as a fallback when this task runs and no service credential is configured for the target connectors. You can revoke this by disabling or deleting the task.
                    </p>
                  </div>
                )}
              </div>

              {/* Notification Targets (Phase 75) */}
              <NotificationTargetConfig
                targets={notificationTargets}
                onChange={setNotificationTargets}
                availableConnectors={(allConnectors || []).map((c: Connector) => ({
                  id: c.id,
                  name: c.name,
                  connector_type: c.connector_type,
                }))}
              />
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
