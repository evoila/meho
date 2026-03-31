// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * LLMViewer Component
 *
 * Displays LLM call details with collapsible sections.
 * Shows prompt, messages, response, parsed output, and token usage.
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight, Clock, Bot, MessageSquare, FileText } from 'lucide-react';
import { cn, JsonViewer, CopyButton } from '@/shared';
import type { EventDetails } from '@/api/types';
import { TokenUsageBadge } from './TokenUsageBadge';
import { MessageViewer } from './MessageViewer';

export interface LLMViewerProps {
  /** Event details containing LLM data */
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

interface CollapsibleSectionProps {
  title: string;
  icon: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
  actions?: React.ReactNode;
}

/**
 * Collapsible section component.
 */
function CollapsibleSection({
  title,
  icon,
  defaultOpen = false,
  children,
  actions,
}: CollapsibleSectionProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

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
        <span className="text-text-tertiary">{icon}</span>
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
 * LLM call details viewer with collapsible sections.
 *
 * @example
 * ```tsx
 * <LLMViewer
 *   details={{
 *     llm_prompt: "You are a helpful assistant...",
 *     llm_messages: [...],
 *     llm_response: "The VM status is...",
 *     token_usage: { prompt_tokens: 500, completion_tokens: 100, total_tokens: 600, estimated_cost_usd: 0.002 },
 *     model: "gpt-4.1-mini"
 *   }}
 * />
 * ```
 */
export function LLMViewer({ details, className }: LLMViewerProps) {
  const hasPrompt = !!details.llm_prompt;
  const hasMessages = details.llm_messages && details.llm_messages.length > 0;
  const hasResponse = !!details.llm_response;
  const hasParsed = !!details.llm_parsed;
  const hasTokenUsage = !!details.token_usage;

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      {/* Header with model and duration */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bot className="w-5 h-5 text-primary" />
          <span className="text-text-primary font-medium">
            {details.model || 'LLM Call'}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {hasTokenUsage && details.token_usage && (
            <TokenUsageBadge usage={details.token_usage} size="sm" />
          )}
          {details.llm_duration_ms !== undefined && details.llm_duration_ms !== null && (
            <div className="flex items-center gap-1.5 text-text-tertiary text-sm">
              <Clock className="w-3.5 h-3.5" />
              <span>{formatDuration(details.llm_duration_ms)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Collapsible sections */}
      <div className="space-y-3">
        {/* System Prompt */}
        {hasPrompt && (
          <CollapsibleSection
            title="System Prompt"
            icon={<FileText className="w-4 h-4" />}
            actions={<CopyButton data={details.llm_prompt} size="sm" />}
          >
            <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono leading-relaxed">
              {details.llm_prompt}
            </pre>
          </CollapsibleSection>
        )}

        {/* Messages */}
        {hasMessages && (
          <CollapsibleSection
            title={`Messages (${details.llm_messages?.length ?? 0})`}
            icon={<MessageSquare className="w-4 h-4" />}
            defaultOpen
            actions={<CopyButton data={details.llm_messages} size="sm" />}
          >
            <MessageViewer messages={details.llm_messages ?? []} />
          </CollapsibleSection>
        )}

        {/* Response */}
        {hasResponse && (
          <CollapsibleSection
            title="Response"
            icon={<Bot className="w-4 h-4" />}
            defaultOpen
            actions={<CopyButton data={details.llm_response} size="sm" />}
          >
            <pre className="text-text-secondary text-sm whitespace-pre-wrap font-mono leading-relaxed">
              {details.llm_response}
            </pre>
          </CollapsibleSection>
        )}

        {/* Parsed Output (if structured) */}
        {hasParsed && (
          <CollapsibleSection
            title="Parsed Output"
            icon={<FileText className="w-4 h-4" />}
            actions={<CopyButton data={details.llm_parsed} size="sm" />}
          >
            <JsonViewer data={details.llm_parsed} />
          </CollapsibleSection>
        )}
      </div>
    </div>
  );
}
