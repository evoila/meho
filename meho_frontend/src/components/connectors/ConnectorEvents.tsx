// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorEvents Component
 *
 * Events tab content for ConnectorDetails. Manages per-connector events:
 * - Card list of event registrations with stats
 * - Slide-out panel for create/edit with HMAC secret display-once
 * - Detail view with configuration section and event history
 * - Test button with step-by-step inline progress
 *
 * Phase 41 - Events System UI
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Webhook,
  Plus,
  Trash2,
  Pencil,
  Play,
  ExternalLink,
  Check,
  X,
  AlertTriangle,
  Clock,
  ArrowLeft,
  Loader2,
  AlertCircle,
  Shield,
  Wand2,
  Info,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import { CopyButton } from '../../shared/components/ui/CopyButton';
import { Modal } from '../../shared/components/ui';
import type {
  EventRegistration,
  EventCreateResponse,
  EventTestResponse,
} from '../../api/types/event';
import type { Connector } from '../../lib/api-client';
import { NotificationTargetConfig } from './NotificationTargetConfig';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STATUS_PILL_STYLES: Record<string, { className: string; label: string }> = {
  processed: {
    className: 'bg-green-400/10 text-green-400 border-green-400/20',
    label: 'Delivered',
  },
  deduplicated: {
    className: 'bg-amber-400/10 text-amber-400 border-amber-400/20',
    label: 'Duplicate',
  },
  rate_limited: {
    className: 'bg-orange-400/10 text-orange-400 border-orange-400/20',
    label: 'Rate Limited',
  },
  failed: {
    className: 'bg-red-400/10 text-red-400 border-red-400/20',
    label: 'Failed',
  },
  test: {
    className: 'bg-purple-400/10 text-purple-400 border-purple-400/20',
    label: 'Test',
  },
};

const TEST_STEP_LABELS: Record<string, string> = {
  template_rendered: 'Template rendered',
  session_created: 'Session created',
  investigation_started: 'Investigation started',
};

const DEFAULT_TEST_PAYLOAD = JSON.stringify(
  {
    alertname: 'HighCPUUsage',
    severity: 'warning',
    instance: 'web-server-01',
    description: 'CPU usage above 90% for 5 minutes',
  },
  null,
  2
);

/**
 * Per-connector-type variable hints showing nested access patterns.
 */
const VARIABLE_HINTS: Record<string, string> = {
  jira: 'For Jira: {{payload.issue.key}}, {{payload.issue.fields.summary}}, {{payload.issue.fields.status.name}}',
  confluence: 'For Confluence: {{payload.page.title}}, {{payload.page.space.key}}, {{payload.userAccountId}}',
  prometheus: 'For Prometheus: {{payload.alerts[0].labels.alertname}}, {{payload.alerts[0].labels.severity}}',
  alertmanager: 'For Alertmanager: {{payload.alerts[0].labels.alertname}}, {{payload.status}}',
  kubernetes: 'For K8s: {{payload.involvedObject.kind}}, {{payload.involvedObject.name}}, {{payload.reason}}',
  vmware: 'For vSphere: {{payload.vm.name}}, {{payload.host.name}}, {{payload.eventType}}',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  if (diffDay > 0) return rtf.format(-diffDay, 'day');
  if (diffHour > 0) return rtf.format(-diffHour, 'hour');
  if (diffMin > 0) return rtf.format(-diffMin, 'minute');
  return 'just now';
}

function truncateMiddle(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  const half = Math.floor((maxLen - 3) / 2);
  return str.slice(0, half) + '...' + str.slice(-half);
}

function buildEventUrl(eventUrl: string): string {
  // event_url from backend is relative: /api/events/{id}
  // Construct full URL from config.apiURL
  const base = config.apiURL.replace(/\/$/, '');
  const path = eventUrl.startsWith('/api/') ? eventUrl.slice(4) : eventUrl;
  return `${base}${path.startsWith('/') ? path : '/' + path}`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Status badge for active/paused */
function StatusBadge({ isActive }: { isActive: boolean }) {
  return (
    <span
      className={clsx(
        'text-xs font-medium px-2 py-0.5 rounded-md border',
        isActive
          ? 'bg-green-400/10 text-green-400 border-green-400/20'
          : 'bg-amber-400/10 text-amber-400 border-amber-400/20'
      )}
    >
      {isActive ? 'Active' : 'Paused'}
    </span>
  );
}

/** Status pill for event history */
function EventStatusPill({ status }: { status: string }) {
  const style = STATUS_PILL_STYLES[status] || {
    className: 'bg-white/5 text-text-secondary border-white/10',
    label: status,
  };
  return (
    <span
      className={clsx(
        'text-xs font-medium px-2 py-0.5 rounded-md border whitespace-nowrap',
        style.className
      )}
    >
      {style.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// EventCardList
// ---------------------------------------------------------------------------

interface EventCardListProps {
  events: EventRegistration[];
  onSelect: (id: string) => void;
  onAddClick: () => void;
}

function EventCardList({ events, onSelect, onAddClick }: EventCardListProps) {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-white">Events</h3>
          <p className="text-sm text-text-secondary mt-0.5">
            {events.length} event{events.length !== 1 ? 's' : ''} configured
          </p>
        </div>
        <button
          onClick={onAddClick}
          className="flex items-center gap-2 px-4 py-2 rounded-xl font-medium text-sm bg-primary hover:bg-primary-hover text-white shadow-lg shadow-primary/20 transition-all"
        >
          <Plus className="h-4 w-4" />
          Add Event
        </button>
      </div>

      {/* Card list */}
      <div className="space-y-3">
        <AnimatePresence mode="popLayout">
          {events.map((event) => {
            const fullUrl = buildEventUrl(event.event_url);
            return (
              <motion.div
                key={event.id}
                layout
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                onClick={() => onSelect(event.id)}
                className="glass rounded-xl p-5 border border-white/10 hover:border-primary/30 transition-all cursor-pointer group"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    {/* Name + status */}
                    <div className="flex items-center gap-3 mb-2">
                      <div className="p-2 rounded-lg bg-primary/10 text-primary">
                        <Webhook className="h-4 w-4" />
                      </div>
                      <span className="text-sm font-medium text-white truncate flex-1 min-w-0">
                        {event.name}
                      </span>
                      <StatusBadge isActive={event.is_active} />
                      {event.delegation_active === false && (
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

                    {/* URL */}
                    <div className="flex items-center gap-2 mb-3">
                      <code className="text-xs text-text-secondary font-mono truncate flex-1">
                        {truncateMiddle(fullUrl, 60)}
                      </code>
                      <CopyButton data={fullUrl} size="sm" />
                    </div>

                    {/* Stats */}
                    <div className="grid grid-cols-3 gap-4 text-sm">
                      <div>
                        <span className="block text-text-tertiary text-xs mb-0.5">
                          Total Events
                        </span>
                        <span className="text-white font-medium">
                          {event.total_events_received}
                        </span>
                      </div>
                      <div>
                        <span className="block text-text-tertiary text-xs mb-0.5">
                          Today
                        </span>
                        <span className="text-white font-medium">
                          {event.events_today}
                        </span>
                      </div>
                      <div>
                        <span className="block text-text-tertiary text-xs mb-0.5">
                          Last Event
                        </span>
                        <span className="text-white font-medium">
                          {event.last_event_at
                            ? relativeTime(event.last_event_at)
                            : '\u2014'}
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// EventSlideOutPanel
// ---------------------------------------------------------------------------

interface EventSlideOutPanelProps {
  isOpen: boolean;
  editingEvent: EventRegistration | null;
  connectorId: string;
  connectorType: string;
  onClose: () => void;
}

function EventSlideOutPanel({
  isOpen,
  editingEvent,
  connectorId,
  connectorType,
  onClose,
}: EventSlideOutPanelProps) {
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const [name, setName] = useState(editingEvent?.name || '');
  const [promptTemplate, setPromptTemplate] = useState(
    editingEvent?.prompt_template || 'Investigate: {{payload}}'
  );
  const [rateLimit, setRateLimit] = useState(
    editingEvent?.rate_limit_per_hour ?? 10
  );
  const [isActive, setIsActive] = useState(editingEvent?.is_active ?? true);
  const [requireSignature, setRequireSignature] = useState(
    editingEvent?.require_signature ?? true
  );
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);

  // Phase 74: Automation settings
  const [allowedConnectorIds, setAllowedConnectorIds] = useState<string[] | null>(
    editingEvent?.allowed_connector_ids ?? null
  );
  const [automationExpanded, setAutomationExpanded] = useState(!editingEvent);

  // Phase 75: Notification targets
  const [notificationTargets, setNotificationTargets] = useState<Array<{ connector_id: string; contact: string }>>(
    editingEvent?.notification_targets ?? []
  );

  // Load all tenant connectors for the scope multi-select
  const { data: allConnectors } = useQuery({
    queryKey: ['all-connectors'],
    queryFn: () => apiClient.listConnectors(),
  });

  const isEdit = !!editingEvent;

  const createMutation = useMutation({
    mutationFn: () =>
      apiClient.createConnectorEvent(connectorId, {
        name,
        prompt_template: promptTemplate,
        rate_limit_per_hour: rateLimit,
        require_signature: requireSignature,
        allowed_connector_ids: allowedConnectorIds,
        notification_targets: notificationTargets.filter(t => t.connector_id && t.contact),
      }),
    onSuccess: (data: EventCreateResponse) => {
      queryClient.invalidateQueries({
        queryKey: ['connector-events', connectorId],
      });
      setCreatedSecret(data.secret);
      toast.success('Event registration created');
    },
    onError: (error: Error) => {
      toast.error(`Failed to create event: ${error.message}`);
    },
  });

  const updateMutation = useMutation({
    mutationFn: () =>
      apiClient.updateConnectorEvent(connectorId, editingEvent?.id ?? '', {
        name,
        prompt_template: promptTemplate,
        rate_limit_per_hour: rateLimit,
        is_active: isActive,
        require_signature: requireSignature,
        allowed_connector_ids: allowedConnectorIds,
        notification_targets: notificationTargets.filter(t => t.connector_id && t.contact),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['connector-events', connectorId],
      });
      toast.success('Event registration updated');
      onClose();
    },
    onError: (error: Error) => {
      toast.error(`Failed to update event: ${error.message}`);
    },
  });

  const generateMutation = useMutation({
    mutationFn: () => {
      const currentText = promptTemplate.trim();
      const defaultTemplate = 'Investigate: {{payload}}';
      const hasUserInput = currentText && currentText !== defaultTemplate;
      return apiClient.generateEventPrompt(connectorId, hasUserInput ? currentText : undefined);
    },
    onSuccess: (data) => {
      setPromptTemplate(data.prompt);
    },
    onError: () => {
      toast.error('Could not generate prompt. Try again.');
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (isEdit) {
      updateMutation.mutate();
    } else {
      createMutation.mutate();
    }
  };

  const handleDone = () => {
    setCreatedSecret(null);
    onClose();
  };

  const isPending = createMutation.isPending || updateMutation.isPending;

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 bg-black/40 z-40"
            onClick={createdSecret ? undefined : onClose}
          />
          {/* Panel */}
          <motion.div
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', damping: 25, stiffness: 200 }}
            className="fixed right-0 top-0 h-full w-[480px] z-50 bg-surface border-l border-white/10 shadow-2xl overflow-y-auto"
          >
            <div className="p-6 space-y-6">
              {/* Header */}
              <div className="flex items-center justify-between">
                <h3 className="text-lg font-semibold text-white">
                  {isEdit ? 'Edit Event' : 'Create Event'}
                </h3>
                <button
                  onClick={createdSecret ? handleDone : onClose}
                  className="p-2 text-text-secondary hover:text-white hover:bg-white/5 rounded-lg transition-colors"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>

              {/* Secret display-once banner */}
              {createdSecret && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="space-y-4"
                >
                  <div className="bg-green-400/10 border border-green-400/20 rounded-xl p-4">
                    <div className="flex items-center gap-2 mb-3">
                      <div className="p-1.5 rounded-lg bg-green-400/20">
                        <Check className="h-4 w-4 text-green-400" />
                      </div>
                      <span className="text-sm font-medium text-green-400">
                        Event registration created
                      </span>
                    </div>
                    <div className="bg-black/30 rounded-lg p-3 font-mono text-sm text-white break-all flex items-start gap-2">
                      <code className="flex-1">{createdSecret}</code>
                      <CopyButton data={createdSecret} size="sm" />
                    </div>
                  </div>
                  <div className="flex items-start gap-2 text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded-xl p-3">
                    <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-0.5" />
                    <span className="text-xs">
                      Copy this secret now. It won&apos;t be shown again.
                    </span>
                  </div>
                  <button
                    onClick={handleDone}
                    className="w-full px-4 py-2.5 bg-primary hover:bg-primary-hover text-white rounded-xl font-medium transition-all"
                  >
                    Done
                  </button>
                </motion.div>
              )}

              {/* Form (hidden after creation success) */}
              {!createdSecret && (
                <form onSubmit={handleSubmit} className="space-y-5">
                  {/* Name */}
                  <div>
                    <label htmlFor="event-name" className="block text-xs text-text-tertiary mb-1.5 font-medium">
                      Name
                    </label>
                    <input
                      id="event-name"
                      type="text"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="e.g., Alert Event"
                      required
                      className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2.5 placeholder:text-text-tertiary focus:outline-none focus:border-primary/50"
                    />
                  </div>

                  {/* Prompt Template */}
                  <div>
                    <div className="flex items-center justify-between mb-1.5">
                      <label htmlFor="event-prompt-template" className="text-xs text-text-tertiary font-medium">
                        Prompt Template
                      </label>
                      <button
                        type="button"
                        onClick={() => generateMutation.mutate()}
                        disabled={generateMutation.isPending}
                        className={clsx(
                          'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors',
                          'bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20',
                          'disabled:opacity-50 disabled:cursor-not-allowed'
                        )}
                      >
                        {generateMutation.isPending ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Wand2 className="h-3 w-3" />
                        )}
                        Generate
                      </button>
                    </div>
                    <textarea
                      id="event-prompt-template"
                      value={promptTemplate}
                      onChange={(e) => setPromptTemplate(e.target.value)}
                      placeholder="Investigate: {{payload}}"
                      rows={5}
                      className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2.5 placeholder:text-text-tertiary focus:outline-none focus:border-primary/50 resize-none font-mono"
                    />
                    <p className="text-xs text-text-tertiary mt-1.5">
                      Variables:{' '}
                      <code className="text-text-secondary">{'{{payload}}'}</code>,{' '}
                      <code className="text-text-secondary">{'{{connector_name}}'}</code>,{' '}
                      <code className="text-text-secondary">{'{{event_type}}'}</code>
                      {'. '}
                      Use <code className="text-text-secondary">{'{{payload.field}}'}</code> for nested access.
                      {connectorType && VARIABLE_HINTS[connectorType] && (
                        <>
                          <br />
                          <span className="text-text-tertiary/80">{VARIABLE_HINTS[connectorType]}</span>
                        </>
                      )}
                    </p>
                  </div>

                  {/* Rate Limit */}
                  <div>
                    <label htmlFor="event-rate-limit" className="block text-xs text-text-tertiary mb-1.5 font-medium">
                      Rate limit per hour
                    </label>
                    <input
                      id="event-rate-limit"
                      type="number"
                      value={rateLimit}
                      onChange={(e) =>
                        setRateLimit(Math.max(1, parseInt(e.target.value) || 1))
                      }
                      min={1}
                      max={100}
                      className="w-32 bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2.5 focus:outline-none focus:border-primary/50"
                    />
                  </div>

                  {/* Require HMAC signature toggle */}
                  <div className="flex items-center justify-between py-2">
                    <div>
                      <span className="text-sm text-text-secondary">
                        Require HMAC signature
                      </span>
                      <p className="text-xs text-text-tertiary mt-0.5">
                        Disable for systems that cannot sign events (e.g. Jira)
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => setRequireSignature(!requireSignature)}
                      className={clsx(
                        'relative inline-flex h-6 w-11 items-center rounded-full transition-colors flex-shrink-0',
                        requireSignature ? 'bg-primary' : 'bg-white/10'
                      )}
                    >
                      <span
                        className={clsx(
                          'inline-block h-4 w-4 rounded-full bg-white transition-transform',
                          requireSignature ? 'translate-x-6' : 'translate-x-1'
                        )}
                      />
                    </button>
                  </div>

                  {!requireSignature && (
                    <div className="flex items-start gap-2 text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded-xl p-3">
                      <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-0.5" />
                      <span className="text-xs">
                        Without signature verification, any system that knows the event URL can trigger investigations. Only disable this for trusted sources.
                      </span>
                    </div>
                  )}

                  {/* Automation Settings (Phase 74) */}
                  <div className="border-t border-white/10 pt-4 mt-2">
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
                        <ChevronUp className="h-4 w-4 text-text-tertiary" />
                      ) : (
                        <ChevronDown className="h-4 w-4 text-text-tertiary" />
                      )}
                    </button>

                    {automationExpanded && (
                      <div className="mt-4 space-y-5">
                        {/* Connector Access Scope */}
                        <div>
                          <label htmlFor="event-connector-scope" className="flex items-center gap-2 text-xs text-text-tertiary mb-1.5 font-medium">
                            <Shield className="h-4 w-4 text-text-tertiary" />
                            Connector Access Scope
                          </label>
                          <p className="text-xs text-text-tertiary mb-2">
                            Which connectors can this event registration's automated session access? Default: all connectors.
                          </p>
                          <select
                            id="event-connector-scope"
                            value=""
                            onChange={(e) => {
                              if (e.target.value) {
                                const newIds = [...(allowedConnectorIds || []), e.target.value];
                                setAllowedConnectorIds(newIds);
                                e.target.value = '';
                              }
                            }}
                            className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2.5 focus:outline-none focus:border-primary/50"
                          >
                            <option value="" className="bg-surface text-text-tertiary">
                              {allowedConnectorIds === null ? 'All connectors (default)' : 'Add a connector to scope...'}
                            </option>
                            {(allConnectors || [])
                              .filter((c: Connector) => !(allowedConnectorIds || []).includes(c.id))
                              .map((c: Connector) => (
                                <option key={c.id} value={c.id} className="bg-surface text-white">
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
                                className="text-xs text-text-tertiary hover:text-white transition-colors"
                              >
                                Reset to all
                              </button>
                            </div>
                          )}
                        </div>

                        {/* Credential Model Info */}
                        <div className="bg-blue-500/10 border border-blue-500/20 rounded-xl p-4 flex items-start gap-3">
                          <Info className="h-5 w-5 text-blue-400 flex-shrink-0 mt-0.5" />
                          <p className="text-xs text-blue-200">
                            This event will use service credentials if available, otherwise your personal credentials will be used as a fallback for automated investigations.
                          </p>
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

                  {/* Active toggle (edit mode only) */}
                  {isEdit && (
                    <div className="flex items-center justify-between py-2">
                      <span className="text-sm text-text-secondary">
                        Event active
                      </span>
                      <button
                        type="button"
                        onClick={() => setIsActive(!isActive)}
                        className={clsx(
                          'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
                          isActive ? 'bg-primary' : 'bg-white/10'
                        )}
                      >
                        <span
                          className={clsx(
                            'inline-block h-4 w-4 rounded-full bg-white transition-transform',
                            isActive ? 'translate-x-6' : 'translate-x-1'
                          )}
                        />
                      </button>
                    </div>
                  )}

                  {/* Submit */}
                  <button
                    type="submit"
                    disabled={isPending || !name.trim()}
                    className="w-full px-4 py-2.5 bg-primary hover:bg-primary-hover text-white rounded-xl font-medium transition-all disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    {isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin" />
                        {isEdit ? 'Saving...' : 'Creating...'}
                      </>
                    ) : isEdit ? (
                      'Save Changes'
                    ) : (
                      'Create Event'
                    )}
                  </button>
                </form>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// EventHistoryTable
// ---------------------------------------------------------------------------

interface EventHistoryTableProps {
  connectorId: string;
  eventId: string;
}

function EventHistoryTable({ connectorId, eventId }: EventHistoryTableProps) {
  const apiClient = getAPIClient(config.apiURL);
  const navigate = useNavigate();
  const [offset, setOffset] = useState(0);
  const limit = 25;

  const { data, isLoading } = useQuery({
    queryKey: ['event-history', connectorId, eventId, offset],
    queryFn: () =>
      apiClient.getEventHistory(connectorId, eventId, { limit, offset }),
  });

  if (isLoading && offset === 0) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-5 w-5 text-primary animate-spin" />
        <span className="ml-2 text-text-secondary text-sm">Loading events...</span>
      </div>
    );
  }

  const events = data?.events || [];
  const hasMore = data?.has_more || false;
  const total = data?.total || 0;

  if (events.length === 0 && offset === 0) {
    return (
      <div className="text-center py-8 text-text-secondary text-sm">
        No events received yet.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="text-xs text-text-tertiary">
        {total} total event{total !== 1 ? 's' : ''}
      </div>

      {/* Compact table */}
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-white/10 bg-white/[0.02]">
              <th className="text-left text-xs font-medium text-text-tertiary px-4 py-2.5">
                Time
              </th>
              <th className="text-left text-xs font-medium text-text-tertiary px-4 py-2.5">
                Status
              </th>
              <th className="text-left text-xs font-medium text-text-tertiary px-4 py-2.5">
                Details
              </th>
              <th className="text-left text-xs font-medium text-text-tertiary px-4 py-2.5">
                Session
              </th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr
                key={event.id}
                className="border-b border-white/5 last:border-b-0 hover:bg-white/[0.02]"
              >
                <td
                  className="px-4 py-2.5 text-xs text-text-secondary whitespace-nowrap"
                  title={new Date(event.created_at).toLocaleString()}
                >
                  {relativeTime(event.created_at)}
                </td>
                <td className="px-4 py-2.5">
                  <EventStatusPill status={event.status} />
                </td>
                <td className="px-4 py-2.5">
                  <span className="text-xs font-mono text-text-tertiary">
                    {event.payload_hash.slice(0, 12)}...
                  </span>
                  <span className="text-xs text-text-tertiary ml-2">
                    {event.payload_size_bytes > 1024
                      ? `${(event.payload_size_bytes / 1024).toFixed(1)} KB`
                      : `${event.payload_size_bytes} B`}
                  </span>
                </td>
                <td className="px-4 py-2.5">
                  {event.session_id ? (
                    <button
                      onClick={() =>
                        navigate(`/chat?session=${event.session_id}`)
                      }
                      className="flex items-center gap-1 text-xs text-primary hover:text-primary-light transition-colors"
                    >
                      <ExternalLink className="h-3 w-3" />
                      View Session
                    </button>
                  ) : event.error_message ? (
                    <span
                      className="text-xs text-red-400 truncate max-w-[120px] block"
                      title={event.error_message}
                    >
                      {event.error_message}
                    </span>
                  ) : (
                    <span className="text-xs text-text-tertiary">&mdash;</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Load more */}
      {hasMore && (
        <button
          onClick={() => setOffset((prev) => prev + limit)}
          disabled={isLoading}
          className="w-full py-2 text-xs font-medium text-text-secondary hover:text-white hover:bg-white/5 rounded-lg transition-colors border border-white/10"
        >
          {isLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin mx-auto" />
          ) : (
            'Load more'
          )}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EventTestPanel
// ---------------------------------------------------------------------------

interface EventTestPanelProps {
  connectorId: string;
  eventId: string;
}

function EventTestPanel({ connectorId, eventId }: EventTestPanelProps) {
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [payload, setPayload] = useState(DEFAULT_TEST_PAYLOAD);
  const [parseError, setParseError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<EventTestResponse | null>(null);

  const testMutation = useMutation({
    mutationFn: () => {
      try {
        const parsed = JSON.parse(payload);
        setParseError(null);
        return apiClient.testConnectorEvent(connectorId, eventId, parsed);
      } catch {
        setParseError('Invalid JSON payload');
        return Promise.reject(new Error('Invalid JSON'));
      }
    },
    onSuccess: (data) => {
      setTestResult(data);
      // Invalidate event history to show the test event
      queryClient.invalidateQueries({
        queryKey: ['event-history', connectorId, eventId],
      });
    },
    onError: (error: Error) => {
      if (error.message !== 'Invalid JSON') {
        toast.error(`Test failed: ${error.message}`);
      }
    },
  });

  return (
    <div className="space-y-4 bg-white/[0.02] border border-white/10 rounded-xl p-4">
      <h4 className="text-sm font-medium text-white flex items-center gap-2">
        <Play className="h-4 w-4 text-primary" />
        Test Event
      </h4>

      {/* JSON editor */}
      <div>
        <label htmlFor="event-test-payload" className="block text-xs text-text-tertiary mb-1.5">
          Test Payload (JSON)
        </label>
        <textarea
          id="event-test-payload"
          value={payload}
          onChange={(e) => {
            setPayload(e.target.value);
            setParseError(null);
          }}
          rows={8}
          className={clsx(
            'w-full bg-black/30 border text-white text-xs rounded-lg px-3 py-2.5 focus:outline-none resize-none font-mono leading-relaxed',
            parseError
              ? 'border-red-400/50 focus:border-red-400'
              : 'border-white/10 focus:border-primary/50'
          )}
        />
        {parseError && (
          <p className="text-xs text-red-400 mt-1">{parseError}</p>
        )}
      </div>

      {/* Send button */}
      <button
        onClick={() => {
          setTestResult(null);
          testMutation.mutate();
        }}
        disabled={testMutation.isPending}
        className="flex items-center gap-2 px-4 py-2 bg-primary hover:bg-primary-hover text-white rounded-lg text-sm font-medium transition-all disabled:opacity-50"
      >
        {testMutation.isPending ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            Sending...
          </>
        ) : (
          <>
            <Play className="h-4 w-4" />
            Send Test
          </>
        )}
      </button>

      {/* Step-by-step progress */}
      {testResult && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="space-y-2"
        >
          {testResult.steps.map((step, i) => (
            <div key={i} className="flex items-center gap-3 py-1.5">
              {step.status === 'success' ? (
                <div className="p-1 rounded-full bg-green-400/20">
                  <Check className="h-3 w-3 text-green-400" />
                </div>
              ) : (
                <div className="p-1 rounded-full bg-red-400/20">
                  <X className="h-3 w-3 text-red-400" />
                </div>
              )}
              <span
                className={clsx(
                  'text-sm',
                  step.status === 'success' ? 'text-white' : 'text-red-400'
                )}
              >
                {TEST_STEP_LABELS[step.step] || step.step}
              </span>
              {step.detail && (
                <span className="text-xs text-text-tertiary">{step.detail}</span>
              )}
            </div>
          ))}

          {/* Result */}
          {testResult.status === 'success' && testResult.session_id && (
            <div className="flex items-center gap-2 mt-2 pt-2 border-t border-white/5">
              <Check className="h-4 w-4 text-green-400" />
              <span className="text-sm text-green-400">Test passed</span>
              <button
                onClick={() =>
                  navigate(`/chat?session=${testResult.session_id}`)
                }
                className="ml-auto flex items-center gap-1 text-xs text-primary hover:text-primary-light transition-colors"
              >
                <ExternalLink className="h-3 w-3" />
                View Session
              </button>
            </div>
          )}

          {testResult.status === 'failed' && testResult.error && (
            <div className="flex items-start gap-2 bg-red-400/10 border border-red-400/20 rounded-lg p-3 mt-2">
              <AlertCircle className="h-4 w-4 text-red-400 flex-shrink-0 mt-0.5" />
              <span className="text-xs text-red-400">{testResult.error}</span>
            </div>
          )}
        </motion.div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EventDetailView
// ---------------------------------------------------------------------------

interface EventDetailViewProps {
  connectorId: string;
  eventId: string;
  onBack: () => void;
  onEdit: (event: EventRegistration) => void;
}

function EventDetailView({
  connectorId,
  eventId,
  onBack,
  onEdit,
}: EventDetailViewProps) {
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showTest, setShowTest] = useState(false);

  const { data: eventReg, isLoading } = useQuery({
    queryKey: ['connector-event', connectorId, eventId],
    queryFn: () => apiClient.getConnectorEvent(connectorId, eventId),
  });

  const deleteMutation = useMutation({
    mutationFn: () => apiClient.deleteConnectorEvent(connectorId, eventId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['connector-events', connectorId],
      });
      toast.success('Event registration deleted');
      onBack();
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete event: ${error.message}`);
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-8 w-8 text-primary animate-spin" />
        <span className="ml-3 text-text-secondary">Loading event registration...</span>
      </div>
    );
  }

  if (!eventReg) {
    return (
      <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
        <AlertCircle className="h-5 w-5 flex-shrink-0" />
        <span>Event registration not found</span>
      </div>
    );
  }

  const fullUrl = buildEventUrl(eventReg.event_url);

  return (
    <div className="space-y-6">
      {/* Back button + header */}
      <div className="flex items-center gap-3">
        <button
          onClick={onBack}
          className="p-2 hover:bg-white/5 rounded-lg transition-colors text-text-secondary hover:text-white"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3">
            <h3 className="text-lg font-semibold text-white truncate">
              {eventReg.name}
            </h3>
            <StatusBadge isActive={eventReg.is_active} />
            {eventReg.delegation_active === false && (
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
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowTest(!showTest)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-primary/10 text-primary hover:bg-primary/20 rounded-lg transition-colors border border-primary/20"
          >
            <Play className="h-3.5 w-3.5" />
            Test
          </button>
          <button
            onClick={() => onEdit(eventReg)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-text-secondary hover:text-white hover:bg-white/5 rounded-lg transition-colors"
          >
            <Pencil className="h-3.5 w-3.5" />
            Edit
          </button>
          <button
            onClick={() => setShowDeleteConfirm(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
        </div>
      </div>

      {/* Test Panel */}
      <AnimatePresence>
        {showTest && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <EventTestPanel
              connectorId={connectorId}
              eventId={eventId}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Configuration section */}
      <div className="space-y-4">
        <h4 className="text-sm font-medium text-white flex items-center gap-2">
          <Shield className="h-4 w-4 text-text-tertiary" />
          Configuration
        </h4>
        <div className="glass rounded-xl border border-white/10 p-4 space-y-4">
          {/* URL */}
          <div>
            <span className="block text-xs text-text-tertiary mb-1">
              Event URL
            </span>
            <div className="flex items-center gap-2">
              <code className="text-sm font-mono text-text-secondary break-all flex-1">
                {fullUrl}
              </code>
              <CopyButton data={fullUrl} size="sm" />
            </div>
          </div>

          {/* Prompt Template */}
          <div>
            <span className="block text-xs text-text-tertiary mb-1">
              Prompt Template
            </span>
            <pre className="bg-black/30 border border-white/5 rounded-lg p-3 text-xs font-mono text-white whitespace-pre-wrap overflow-x-auto">
              {eventReg.prompt_template}
            </pre>
          </div>

          {/* Signature + Rate Limit */}
          <div className="flex gap-8 flex-wrap">
            <div>
              <span className="block text-xs text-text-tertiary mb-1">
                HMAC Signature
              </span>
              <span className={clsx(
                'text-sm font-medium',
                eventReg.require_signature ? 'text-green-400' : 'text-amber-400'
              )}>
                {eventReg.require_signature ? 'Required' : 'Not required'}
              </span>
            </div>
            <div>
              <span className="block text-xs text-text-tertiary mb-1">
                Rate Limit
              </span>
              <span className="text-sm text-white">
                {eventReg.rate_limit_per_hour}/hour
              </span>
            </div>
            <div>
              <span className="block text-xs text-text-tertiary mb-1">
                Created
              </span>
              <span className="text-sm text-white">
                {new Date(eventReg.created_at).toLocaleDateString('en-US', {
                  month: 'short',
                  day: 'numeric',
                  year: 'numeric',
                })}
              </span>
            </div>
            <div>
              <span className="block text-xs text-text-tertiary mb-1">
                Updated
              </span>
              <span className="text-sm text-white">
                {new Date(eventReg.updated_at).toLocaleDateString('en-US', {
                  month: 'short',
                  day: 'numeric',
                  year: 'numeric',
                })}
              </span>
            </div>
          </div>

          {!eventReg.require_signature && (
            <div className="flex items-start gap-2 text-amber-400 bg-amber-400/10 border border-amber-400/20 rounded-lg p-3">
              <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-0.5" />
              <span className="text-xs">
                HMAC signature verification is disabled. Any system with the event URL can trigger investigations.
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Event History */}
      <div className="space-y-4">
        <h4 className="text-sm font-medium text-white flex items-center gap-2">
          <Clock className="h-4 w-4 text-text-tertiary" />
          Event History
        </h4>
        <EventHistoryTable connectorId={connectorId} eventId={eventId} />
      </div>

      {/* Delete Confirmation Modal */}
      <Modal
        isOpen={showDeleteConfirm}
        onClose={() => setShowDeleteConfirm(false)}
        title="Delete Event?"
        description="This action cannot be undone."
        footer={
          <>
            <button
              onClick={() => setShowDeleteConfirm(false)}
              disabled={deleteMutation.isPending}
              className="px-4 py-2 text-sm text-text-secondary hover:text-white transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
              className="px-4 py-2 text-sm bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors flex items-center gap-2"
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              Delete
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-sm text-text-secondary">
            Are you sure you want to delete this event registration? All event history will
            be permanently lost.
          </p>
          <div className="bg-red-500/5 border border-red-500/10 rounded-xl p-3">
            <p className="text-xs text-red-400">
              External systems sending events to this event URL will receive
              errors after deletion.
            </p>
          </div>
        </div>
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

interface ConnectorEventsProps {
  connectorId: string;
  connectorType: string;
}

export function ConnectorEvents({ connectorId, connectorType }: ConnectorEventsProps) {
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [showCreatePanel, setShowCreatePanel] = useState(false);
  const [editingEvent, setEditingEvent] = useState<EventRegistration | null>(
    null
  );

  const apiClient = getAPIClient(config.apiURL);

  const {
    data: eventRegistrations,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['connector-events', connectorId],
    queryFn: () => apiClient.listConnectorEvents(connectorId),
  });

  // Loading state
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-8 w-8 text-primary animate-spin" />
        <span className="ml-3 text-text-secondary">Loading events...</span>
      </div>
    );
  }

  // Error state
  if (isError) {
    return (
      <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
        <AlertCircle className="h-5 w-5 flex-shrink-0" />
        <span>Failed to load events: {(error as Error).message}</span>
      </div>
    );
  }

  const eventList = eventRegistrations || [];

  const handleEdit = (event: EventRegistration) => {
    setEditingEvent(event);
  };

  const handleClosePanel = () => {
    setShowCreatePanel(false);
    setEditingEvent(null);
  };

  // Detail view
  if (selectedEventId) {
    return (
      <>
        <EventDetailView
          connectorId={connectorId}
          eventId={selectedEventId}
          onBack={() => setSelectedEventId(null)}
          onEdit={handleEdit}
        />
        {/* Slide-out panel rendered at root level to avoid z-index issues */}
        <EventSlideOutPanel
          isOpen={!!editingEvent}
          editingEvent={editingEvent}
          connectorId={connectorId}
          connectorType={connectorType}
          onClose={handleClosePanel}
        />
      </>
    );
  }

  // Empty state
  if (eventList.length === 0) {
    return (
      <>
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center py-16 bg-surface/50 border border-white/10 rounded-2xl"
        >
          <div className="w-16 h-16 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mx-auto mb-4">
            <Webhook className="h-8 w-8 text-primary" />
          </div>
          <p className="text-white font-medium mb-2">
            No events configured
          </p>
          <p className="text-sm text-text-secondary max-w-md mx-auto mb-6">
            Create a event to receive events from external systems. MEHO will
            automatically investigate incoming alerts using the prompt template
            you define.
          </p>
          <button
            onClick={() => setShowCreatePanel(true)}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-primary hover:bg-primary-hover text-white rounded-xl font-medium transition-all shadow-lg shadow-primary/20"
          >
            <Plus className="h-4 w-4" />
            Add Event
          </button>
        </motion.div>

        <EventSlideOutPanel
          isOpen={showCreatePanel}
          editingEvent={null}
          connectorId={connectorId}
          connectorType={connectorType}
          onClose={handleClosePanel}
        />
      </>
    );
  }

  // List view
  return (
    <>
      <EventCardList
        events={eventList}
        onSelect={setSelectedEventId}
        onAddClick={() => setShowCreatePanel(true)}
      />

      {/* Slide-out panel rendered at root level */}
      <EventSlideOutPanel
        isOpen={showCreatePanel || !!editingEvent}
        editingEvent={editingEvent}
        connectorId={connectorId}
        connectorType={connectorType}
        onClose={handleClosePanel}
      />
    </>
  );
}
