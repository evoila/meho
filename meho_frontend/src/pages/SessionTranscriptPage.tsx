// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Session Transcript Page
 *
 * Multi-transcript view for session conversation history.
 * Shows all transcripts (executions) for a chat session in an accordion layout.
 * Each transcript can be expanded to show its events.
 */
import { useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  ArrowLeft,
  Clock,
  Zap,
  Globe,
  Database,
  Wrench,
  Bot,
  AlertTriangle,
  Coins,
  ChevronDown,
  ChevronRight,
  MessageSquare,
  Copy,
} from 'lucide-react';
import { cn, Badge, Button, Card, Spinner } from '@/shared';
import { useSessionTranscript } from '@/features/observability';
import { EventDetailModal } from '@/features/observability/components/EventDetailModal';
import type { EventResponse, EventDetails, TranscriptItem, SessionSummary } from '@/api/types';
import { hasLLMDetails, hasHTTPDetails, hasSQLDetails, hasToolDetails } from '@/api/types';

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
    return date.toLocaleTimeString();
  } catch {
    return timestamp;
  }
}

/**
 * Format date for display.
 */
function formatDate(timestamp: string): string {
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
 * Get event type info for rendering.
 */
function getEventTypeInfo(type: string, details: EventDetails): {
  icon: React.ReactNode;
  color: string;
  bgColor: string;
  label: string;
} {
  if (hasLLMDetails(details)) {
    return {
      icon: <Bot className="w-3.5 h-3.5" />,
      color: 'text-primary',
      bgColor: 'bg-primary/10',
      label: 'LLM',
    };
  }
  if (hasHTTPDetails(details)) {
    return {
      icon: <Globe className="w-3.5 h-3.5" />,
      color: 'text-blue-400',
      bgColor: 'bg-blue-500/10',
      label: 'HTTP',
    };
  }
  if (hasSQLDetails(details)) {
    return {
      icon: <Database className="w-3.5 h-3.5" />,
      color: 'text-amber-400',
      bgColor: 'bg-amber-500/10',
      label: 'SQL',
    };
  }
  if (hasToolDetails(details)) {
    return {
      icon: <Wrench className="w-3.5 h-3.5" />,
      color: 'text-emerald-400',
      bgColor: 'bg-emerald-500/10',
      label: 'Tool',
    };
  }

  switch (type.toLowerCase()) {
    case 'error':
      return {
        icon: <AlertTriangle className="w-3.5 h-3.5" />,
        color: 'text-red-400',
        bgColor: 'bg-red-500/10',
        label: 'Error',
      };
    default:
      return {
        icon: <Zap className="w-3.5 h-3.5" />,
        color: 'text-text-secondary',
        bgColor: 'bg-surface',
        label: type,
      };
  }
}

/**
 * Get status badge variant.
 */
function getStatusVariant(status: string): 'success' | 'warning' | 'error' | 'default' {
  switch (status.toLowerCase()) {
    case 'completed':
      return 'success';
    case 'running':
      return 'warning';
    case 'failed':
    case 'error':
      return 'error';
    default:
      return 'default';
  }
}

/**
 * Event list item component.
 */
function EventListItem({
  event,
  isSelected,
  onClick,
}: {
  event: EventResponse;
  isSelected: boolean;
  onClick: () => void;
}) {
  const typeInfo = getEventTypeInfo(event.type, event.details);

  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full flex items-start gap-3 p-3 text-left rounded-lg transition-colors',
        isSelected
          ? 'bg-primary/10 border border-primary/30'
          : 'bg-surface hover:bg-surface-hover border border-transparent'
      )}
    >
      <div className={cn('flex-shrink-0 p-1.5 rounded-lg', typeInfo.bgColor, typeInfo.color)}>
        {typeInfo.icon}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={cn('text-xs font-medium', typeInfo.color)}>{typeInfo.label}</span>
          <span className="text-text-tertiary text-xs">{formatTimestamp(event.timestamp)}</span>
        </div>
        <p className="text-text-secondary text-sm truncate mt-0.5">{event.summary}</p>
        {event.duration_ms !== undefined && event.duration_ms !== null && (
          <div className="flex items-center gap-1 mt-1 text-text-tertiary text-xs">
            <Clock className="w-3 h-3" />
            <span>{formatDuration(event.duration_ms)}</span>
          </div>
        )}
      </div>
    </button>
  );
}

/**
 * Compact summary stats for a transcript.
 */
function TranscriptStats({ summary }: { summary: SessionSummary }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-text-tertiary">
      <span className="flex items-center gap-1">
        <Bot className="w-3 h-3 text-primary" />
        {summary.llm_calls} LLM
      </span>
      <span className="flex items-center gap-1">
        <Zap className="w-3 h-3 text-amber-400" />
        {summary.total_tokens.toLocaleString()} tokens
      </span>
      {summary.total_duration_ms !== null && (
        <span className="flex items-center gap-1">
          <Clock className="w-3 h-3" />
          {formatDuration(summary.total_duration_ms)}
        </span>
      )}
      {summary.estimated_cost_usd !== null && (
        <span className="flex items-center gap-1">
          <Coins className="w-3 h-3 text-emerald-400" />
          ${summary.estimated_cost_usd.toFixed(4)}
        </span>
      )}
    </div>
  );
}

/**
 * Transcript accordion item.
 */
function TranscriptAccordionItem({
  transcript,
  isExpanded,
  onToggle,
  onEventClick,
  selectedEventId,
  index,
}: {
  transcript: TranscriptItem;
  isExpanded: boolean;
  onToggle: () => void;
  onEventClick: (event: EventResponse) => void;
  selectedEventId: string | null;
  index: number;
}) {
  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Accordion Header */}
      <button
        onClick={onToggle}
        className={cn(
          'w-full flex items-start gap-3 p-4 text-left transition-colors',
          isExpanded ? 'bg-surface-hover' : 'bg-surface hover:bg-surface-hover'
        )}
      >
        {/* Expand/Collapse icon */}
        <div className="flex-shrink-0 mt-0.5 text-text-tertiary">
          {isExpanded ? (
            <ChevronDown className="w-4 h-4" />
          ) : (
            <ChevronRight className="w-4 h-4" />
          )}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant={getStatusVariant(transcript.status)} className="text-xs">
              {transcript.status}
            </Badge>
            <span className="text-text-tertiary text-xs">
              {formatDate(transcript.created_at)}
            </span>
            <span className="text-text-tertiary text-xs">
              #{index + 1}
            </span>
          </div>

          {/* User query */}
          <div className="flex items-start gap-2 mt-2">
            <MessageSquare className="w-4 h-4 text-text-tertiary flex-shrink-0 mt-0.5" />
            <p className="text-text-primary text-sm line-clamp-2">
              {transcript.user_query || 'No query recorded'}
            </p>
          </div>

          {/* Stats */}
          <div className="mt-2">
            <TranscriptStats summary={transcript.summary} />
          </div>
        </div>
      </button>

      {/* Accordion Content - Events List */}
      {isExpanded && (
        <div className="border-t border-border bg-background p-4 space-y-2">
          {transcript.events.length === 0 ? (
            <p className="text-text-tertiary text-center py-4">No events recorded</p>
          ) : (
            transcript.events.map((event) => (
              <EventListItem
                key={event.id}
                event={event}
                isSelected={selectedEventId === event.id}
                onClick={() => onEventClick(event)}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Session Transcript Page with multi-transcript support.
 */
export function SessionTranscriptPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [selectedEvent, setSelectedEvent] = useState<EventResponse | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [expandedTranscripts, setExpandedTranscripts] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);

  const { transcripts, totalTranscripts, loading, error } = useSessionTranscript(
    sessionId ?? null,
    { include_details: true }
  );

  // Auto-expand first transcript on load
  useState(() => {
    if (transcripts.length > 0 && expandedTranscripts.size === 0) {
      setExpandedTranscripts(new Set([transcripts[0].transcript_id]));
    }
  });

  const handleToggleTranscript = (transcriptId: string) => {
    setExpandedTranscripts((prev) => {
      const next = new Set(prev);
      if (next.has(transcriptId)) {
        next.delete(transcriptId);
      } else {
        next.add(transcriptId);
      }
      return next;
    });
  };

  const handleEventClick = (event: EventResponse) => {
    setSelectedEvent(event);
    setShowModal(true);
  };

  // Calculate aggregate stats
  const aggregateStats = transcripts.reduce(
    (acc, t) => ({
      totalLlmCalls: acc.totalLlmCalls + t.summary.llm_calls,
      totalTokens: acc.totalTokens + t.summary.total_tokens,
      totalDurationMs: acc.totalDurationMs + (t.summary.total_duration_ms ?? 0),
      totalCost: acc.totalCost + (t.summary.estimated_cost_usd ?? 0),
    }),
    { totalLlmCalls: 0, totalTokens: 0, totalDurationMs: 0, totalCost: 0 }
  );

  if (!sessionId) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-text-tertiary">No session ID provided</p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Spinner size="lg" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4">
        <AlertTriangle className="w-12 h-12 text-red-400" />
        <p className="text-red-400">{error}</p>
        <Button variant="outline" onClick={() => navigate('/sessions')}>
          Go Back
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header */}
      <div className="flex-shrink-0 border-b border-border bg-surface px-6 py-4">
        <div className="flex items-center gap-4">
          <Link
            to="/sessions"
            className="text-text-tertiary hover:text-text-primary transition-colors"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="flex-1">
            <h1 className="text-text-primary font-semibold text-lg">Session Transcripts</h1>
            <div className="flex items-center gap-2 text-text-tertiary text-sm">
              <button
                onClick={() => {
                  navigator.clipboard.writeText(sessionId);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                }}
                className="font-mono text-xs hover:text-text-secondary transition-colors cursor-pointer inline-flex items-center gap-1"
                title="Click to copy full session ID"
              >
                {sessionId.slice(0, 8)}...
                {copied ? (
                  <span className="text-green-400 text-[10px]">Copied!</span>
                ) : (
                  <Copy className="w-3 h-3 opacity-50" />
                )}
              </button>
              <span>•</span>
              <span>{totalTranscripts} execution{totalTranscripts !== 1 ? 's' : ''}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 flex flex-col lg:flex-row min-h-0 overflow-hidden">
        {/* Left Panel - Transcript List */}
        <div className="lg:w-1/2 xl:w-2/5 flex flex-col border-r border-border min-h-0">
          {/* Aggregate stats */}
          <div className="flex-shrink-0 p-4 border-b border-border">
            <Card className="p-4">
              <h3 className="text-text-tertiary text-xs uppercase tracking-wide mb-3">
                Session Summary
              </h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="flex flex-col">
                  <span className="text-text-tertiary text-xs">Executions</span>
                  <span className="text-text-primary text-lg font-semibold">
                    {totalTranscripts}
                  </span>
                </div>
                <div className="flex flex-col">
                  <span className="text-text-tertiary text-xs flex items-center gap-1">
                    <Bot className="w-3 h-3 text-primary" />
                    LLM Calls
                  </span>
                  <span className="text-text-primary text-lg font-semibold">
                    {aggregateStats.totalLlmCalls}
                  </span>
                </div>
                <div className="flex flex-col">
                  <span className="text-text-tertiary text-xs flex items-center gap-1">
                    <Zap className="w-3 h-3 text-amber-400" />
                    Tokens
                  </span>
                  <span className="text-text-primary text-lg font-semibold">
                    {aggregateStats.totalTokens.toLocaleString()}
                  </span>
                </div>
                <div className="flex flex-col">
                  <span className="text-text-tertiary text-xs flex items-center gap-1">
                    <Coins className="w-3 h-3 text-emerald-400" />
                    Est. Cost
                  </span>
                  <span className="text-text-primary text-lg font-semibold">
                    ${aggregateStats.totalCost.toFixed(4)}
                  </span>
                </div>
              </div>
            </Card>
          </div>

          {/* Transcript accordion list */}
          <div className="flex-1 overflow-auto p-4 space-y-3">
            {transcripts.length === 0 ? (
              <p className="text-text-tertiary text-center py-8">No transcripts found</p>
            ) : (
              transcripts.map((transcript, index) => (
                <TranscriptAccordionItem
                  key={transcript.transcript_id}
                  transcript={transcript}
                  isExpanded={expandedTranscripts.has(transcript.transcript_id)}
                  onToggle={() => handleToggleTranscript(transcript.transcript_id)}
                  onEventClick={handleEventClick}
                  selectedEventId={selectedEvent?.id ?? null}
                  index={index}
                />
              ))
            )}
          </div>
        </div>

        {/* Right Panel - Event Details (desktop) */}
        <div className="hidden lg:flex lg:w-1/2 xl:w-3/5 flex-col bg-background">
          {selectedEvent ? (
            <div className="flex-1 overflow-auto p-6">
              <Card className="h-full">
                <div className="p-4 border-b border-border">
                  <div className="flex items-center gap-2">
                    <Badge variant="primary">{selectedEvent.type}</Badge>
                    <span className="text-text-primary font-medium">{selectedEvent.summary}</span>
                  </div>
                  <p className="text-text-tertiary text-sm mt-1">
                    {formatTimestamp(selectedEvent.timestamp)}
                  </p>
                </div>
                <div className="p-4 overflow-auto">
                  {hasLLMDetails(selectedEvent.details) && (
                    <div className="space-y-4">
                      {selectedEvent.details.llm_prompt && (
                        <div>
                          <h4 className="text-text-tertiary text-xs uppercase tracking-wide mb-2">
                            System Prompt
                          </h4>
                          <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono bg-surface rounded-lg p-3 max-h-48 overflow-auto">
                            {selectedEvent.details.llm_prompt}
                          </pre>
                        </div>
                      )}
                      {selectedEvent.details.llm_response && (
                        <div>
                          <h4 className="text-text-tertiary text-xs uppercase tracking-wide mb-2">
                            Response
                          </h4>
                          <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono bg-surface rounded-lg p-3 max-h-48 overflow-auto">
                            {selectedEvent.details.llm_response}
                          </pre>
                        </div>
                      )}
                    </div>
                  )}
                  {hasHTTPDetails(selectedEvent.details) && (
                    <div className="space-y-2">
                      <p className="font-mono">
                        <span className="text-emerald-400 font-bold">
                          {selectedEvent.details.http_method}
                        </span>{' '}
                        <span className="text-text-secondary">{selectedEvent.details.http_url}</span>
                      </p>
                      {selectedEvent.details.http_status_code && (
                        <Badge
                          variant={selectedEvent.details.http_status_code < 400 ? 'success' : 'error'}
                        >
                          {selectedEvent.details.http_status_code}
                        </Badge>
                      )}
                    </div>
                  )}
                  {hasToolDetails(selectedEvent.details) && (
                    <div className="space-y-2">
                      <p className="text-primary font-medium">{selectedEvent.details.tool_name}</p>
                      {selectedEvent.details.tool_error && (
                        <p className="text-red-400 text-sm">{selectedEvent.details.tool_error}</p>
                      )}
                    </div>
                  )}
                  <div className="mt-4">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setShowModal(true)}
                    >
                      View Full Details
                    </Button>
                  </div>
                </div>
              </Card>
            </div>
          ) : (
            <div className="flex-1 flex items-center justify-center text-text-tertiary">
              <div className="text-center">
                <MessageSquare className="w-12 h-12 mx-auto mb-3 opacity-50" />
                <p>Select an event from a transcript to view details</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Event Detail Modal */}
      <EventDetailModal
        event={selectedEvent}
        isOpen={showModal}
        onClose={() => setShowModal(false)}
        sessionId={sessionId}
      />
    </div>
  );
}
