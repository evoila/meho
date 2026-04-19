// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EventDetailModal Component
 *
 * Main modal for displaying event details with tabbed interface.
 * Composes LLMViewer, HTTPViewer, SQLViewer, ToolViewer, and raw JSON view.
 */
import { useMemo } from 'react';
import { X, Clock, Bot, Globe, Database, Wrench, FileJson, AlertTriangle } from 'lucide-react';
import { createPortal } from 'react-dom';
import { SimpleTabs, Badge, JsonViewer } from '@/shared';
import type { EventResponse, EventDetails } from '@/api/types';
import { hasLLMDetails, hasHTTPDetails, hasSQLDetails, hasToolDetails } from '@/api/types';
import { LLMViewer } from './LLMViewer';
import { HTTPViewer } from './HTTPViewer';
import { SQLViewer } from './SQLViewer';
import { ToolViewer } from './ToolViewer';
import { TokenUsageBadge } from './TokenUsageBadge';

export interface EventDetailModalProps {
  /** The event to display */
  event: EventResponse | null;
  /** Whether the modal is open */
  isOpen: boolean;
  /** Callback when modal is closed */
  onClose: () => void;
  /** Session ID for fetching additional details */
  sessionId?: string;
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
 * Format timestamp for display.
 */
function formatTimestamp(timestamp: string): string {
  try {
    const date = new Date(timestamp);
    return date.toLocaleString();
  } catch {
    return timestamp;
  }
}

/**
 * Get event type badge variant.
 */
function getEventTypeBadge(type: string): { variant: 'default' | 'primary' | 'success' | 'warning' | 'error' | 'info'; icon: React.ReactNode } {
  switch (type.toLowerCase()) {
    case 'llm_call':
    case 'thought':
      return { variant: 'primary', icon: <Bot className="w-3 h-3" /> };
    case 'http_request':
      return { variant: 'info', icon: <Globe className="w-3 h-3" /> };
    case 'sql_query':
      return { variant: 'warning', icon: <Database className="w-3 h-3" /> };
    case 'tool_call':
    case 'action':
      return { variant: 'success', icon: <Wrench className="w-3 h-3" /> };
    case 'error':
      return { variant: 'error', icon: <AlertTriangle className="w-3 h-3" /> };
    default:
      return { variant: 'default', icon: <FileJson className="w-3 h-3" /> };
  }
}

/**
 * Build available tabs based on event details.
 */
function buildTabs(details: EventDetails) {
  const tabs: Array<{ id: string; label: string; icon: React.ReactNode }> = [];

  if (hasLLMDetails(details)) {
    tabs.push({ id: 'llm', label: 'LLM', icon: <Bot className="w-4 h-4" /> });
  }
  if (hasHTTPDetails(details)) {
    tabs.push({ id: 'http', label: 'HTTP', icon: <Globe className="w-4 h-4" /> });
  }
  if (hasSQLDetails(details)) {
    tabs.push({ id: 'sql', label: 'SQL', icon: <Database className="w-4 h-4" /> });
  }
  if (hasToolDetails(details)) {
    tabs.push({ id: 'tool', label: 'Tool', icon: <Wrench className="w-4 h-4" /> });
  }

  // Always add Raw tab
  tabs.push({ id: 'raw', label: 'Raw', icon: <FileJson className="w-4 h-4" /> });

  return tabs;
}

/**
 * Modal for displaying detailed event information.
 * Shows appropriate viewer based on event type with tabbed interface.
 *
 * @example
 * ```tsx
 * <EventDetailModal
 *   event={selectedEvent}
 *   isOpen={!!selectedEvent}
 *   onClose={() => setSelectedEvent(null)}
 * />
 * ```
 */
export function EventDetailModal({ event, isOpen, onClose }: EventDetailModalProps) {
  // Build tabs based on available details
  const tabs = useMemo(() => {
    if (!event) return [{ id: 'raw', label: 'Raw', icon: <FileJson className="w-4 h-4" /> }];
    return buildTabs(event.details);
  }, [event]);

  // Get default tab (first available specific viewer, or raw)
  const defaultTab = tabs[0]?.id || 'raw';

  if (!event) {
    return null;
  }

  const typeBadge = getEventTypeBadge(event.type);
  const hasTokenUsage = event.details.token_usage;

  // Render modal content with portal
  const modalContent = (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- modal backdrop, keyboard close handled by Escape
    <div
      className="fixed inset-0 z-[99999] flex items-center justify-center p-6 bg-black/85 backdrop-blur-sm"
      onClick={onClose}
    >
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- stop propagation on modal content */}
      <div
        className="bg-surface border border-border rounded-2xl shadow-xl max-w-5xl w-full max-h-[85vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between px-6 py-4 border-b border-border bg-background flex-shrink-0">
          <div className="flex flex-col gap-2">
            {/* Type badge + Summary */}
            <div className="flex items-center gap-3">
              <Badge variant={typeBadge.variant} size="sm" className="flex items-center gap-1.5">
                {typeBadge.icon}
                <span>{event.type}</span>
              </Badge>
              <span className="text-text-primary font-semibold text-lg">
                {event.summary}
              </span>
            </div>

            {/* Metadata row */}
            <div className="flex items-center gap-4 text-sm text-text-tertiary">
              <span>{formatTimestamp(event.timestamp)}</span>
              {event.agent_name && (
                <>
                  <span>•</span>
                  <span>Agent: {event.agent_name}</span>
                </>
              )}
              {event.node_name && (
                <>
                  <span>•</span>
                  <span>Node: {event.node_name}</span>
                </>
              )}
              {event.step_number !== undefined && event.step_number !== null && (
                <>
                  <span>•</span>
                  <span>Step {event.step_number}</span>
                </>
              )}
            </div>
          </div>

          <button
            onClick={onClose}
            className="text-text-tertiary hover:text-text-primary transition-colors p-2 hover:bg-surface-hover rounded-lg"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tabs + Content */}
        <div className="flex-1 min-h-0 flex flex-col">
          <SimpleTabs
            tabs={tabs.map((t) => ({
              id: t.id,
              label: (
                <span className="flex items-center gap-1.5">
                  {t.icon}
                  {t.label}
                </span>
              ),
            }))}
            defaultTab={defaultTab}
            className="flex-1 min-h-0"
            tabListClassName="px-6 border-b border-border bg-surface"
            panelClassName="flex-1 overflow-auto p-6"
          >
            {(activeTab) => (
              <div className="h-full overflow-auto">
                {activeTab === 'llm' && hasLLMDetails(event.details) && (
                  <LLMViewer details={event.details} />
                )}
                {activeTab === 'http' && hasHTTPDetails(event.details) && (
                  <HTTPViewer details={event.details} />
                )}
                {activeTab === 'sql' && hasSQLDetails(event.details) && (
                  <SQLViewer details={event.details} />
                )}
                {activeTab === 'tool' && hasToolDetails(event.details) && (
                  <ToolViewer details={event.details} />
                )}
                {activeTab === 'raw' && (
                  <div className="space-y-4">
                    <div className="flex items-center justify-between">
                      <span className="text-text-primary font-medium">Raw Event Data</span>
                    </div>
                    <div className="border border-border rounded-lg bg-background p-4 max-h-[60vh] overflow-auto">
                      <JsonViewer data={event} />
                    </div>
                  </div>
                )}
              </div>
            )}
          </SimpleTabs>
        </div>

        {/* Footer with token usage and duration */}
        <div className="flex items-center justify-between px-6 py-3 border-t border-border bg-surface flex-shrink-0">
          <div className="flex items-center gap-4">
            {hasTokenUsage && event.details.token_usage && (
              <TokenUsageBadge usage={event.details.token_usage} size="sm" />
            )}
          </div>
          {event.duration_ms !== undefined && event.duration_ms !== null && (
            <div className="flex items-center gap-1.5 text-text-tertiary text-sm">
              <Clock className="w-3.5 h-3.5" />
              <span>{formatDuration(event.duration_ms)}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  // Use portal to render at document body level
  if (!isOpen) {
    return null;
  }

  return createPortal(modalContent, document.body);
}
