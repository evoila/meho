// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Message List Component
 *
 * Renders the list of chat messages with approval cards and execution monitors.
 * Orchestrator-only rendering path: OrchestratorProgress with ConnectorCards.
 */
import { motion } from 'motion/react';
import { Message, TypingIndicator } from '@/components/Message';
import { PlanPreview } from '@/components/PlanPreview';
import { AuditCard } from '@/components/AuditCard';
import { OrchestratorProgress } from './OrchestratorProgress';
import { PassthroughBadge } from './PassthroughBadge';
import { DataPreview } from './DataPreview';
import { ErrorCard } from './ErrorCard';
import { StructuredMessage } from './StructuredMessage';
import { ConnectorBreadcrumb } from './ConnectorBreadcrumb';
import { FollowUpChips } from './FollowUpChips';
import { MentionPill } from './MentionPill';
import { stripSynthesisXml } from '../utils/parseSynthesis';
import type { ChatMessage } from '../types';
import type { Workflow, Plan } from '@/lib/api-client';
import type { OrchestratorEvent } from '@/api/types/orchestrator';

interface ChatMessageListProps {
  messages: ChatMessage[];
  currentWorkflow: Workflow | null;
  isProcessing: boolean;
  isApproving: boolean;
  onApprove: () => void;
  onReject: () => void;
  onEditPlan: (plan: Plan) => void | Promise<void>;
  onCloneWorkflow: () => void | Promise<void>;
  onRetryWorkflow: () => void | Promise<void>;
  liveEventsStartTime?: number;
  orchestratorEvents?: OrchestratorEvent[];
  isOrchestratorActive?: boolean;
  onRetry?: (query: string) => void;
  followUpSuggestions?: string[];
  onFollowUpClick?: (suggestion: string) => void;
  onBreadcrumbChipClick?: (messageId: string, connectorName: string) => void;
}

/**
 * Strip the @connectorName prefix from message content for mention messages.
 * The mention pill is rendered separately, so the text should not duplicate it.
 */
function stripMentionPrefix(content: string): string {
  const match = content.match(/^@\S+\s*/);
  return match ? content.slice(match[0].length) : content;
}

export function ChatMessageList({
  messages,
  currentWorkflow,
  isProcessing,
  isApproving,
  onApprove,
  onReject,
  onEditPlan,
  onCloneWorkflow,
  onRetryWorkflow,
  liveEventsStartTime,
  orchestratorEvents,
  isOrchestratorActive,
  onRetry,
  followUpSuggestions,
  onFollowUpClick,
  onBreadcrumbChipClick,
}: ChatMessageListProps) {
  return (
    <>
      <div className="space-y-6" role="log" aria-live="polite" aria-relevant="additions" aria-label="Chat conversation">
        {messages.map((message, index) => {
          // War room consecutive sender collapsing (Phase 39)
          const prevMessage = index > 0 ? messages[index - 1] : null;
          const showSenderName = message.role === 'user' && !!message.senderName && (
            !prevMessage ||
            prevMessage.role !== 'user' ||
            prevMessage.senderName !== message.senderName
          );

          return (
          <div
            key={message.id}
            data-message-id={message.id}
            data-testid={
              message.status === 'WAITING_APPROVAL'
                ? 'approval-required-message'
                : message.status === 'RUNNING' && message.plan
                  ? 'auto-execute-message'
                  : undefined
            }
          >
            {/* Audit trail card (Phase 5: post-approval/denial) */}
            {message.auditEntry && (
              <AuditCard entry={message.auditEntry} />
            )}

            {/* Orchestrator Progress - for completed messages */}
            {message.orchestratorEvents && message.orchestratorEvents.length > 0 && message.requestStartTime && (
              <OrchestratorProgress
                events={message.orchestratorEvents}
                startTime={message.requestStartTime}
                isLive={false}
              />
            )}

            {/* Passthrough attribution */}
            {message.passthrough && message.sourceConnector && (
              <PassthroughBadge connectorName={message.sourceConnector} />
            )}

            {/* Skip empty Message bubble for audit-only entries */}
            {!message.auditEntry && (
              message.errorType ? (
                <ErrorCard
                  errorType={message.errorType}
                  message={message.content}
                  severity={message.errorSeverity || 'fatal'}
                  details={message.errorDetails}
                  connectorName={message.errorConnector}
                  onRetry={message.retryQuery && message.errorSeverity === 'retryable'
                    ? () => onRetry?.(message.retryQuery ?? '')
                    : undefined}
                />
              ) : message.structuredContent && message.role === 'assistant' ? (
                <StructuredMessage
                  structuredContent={message.structuredContent}
                  citations={message.citations}
                />
              ) : (
                <>
                  <Message
                    role={message.role}
                    content={
                      message.role === 'user' && message.mentionMetadata
                        ? stripMentionPrefix(message.content)
                        : message.role === 'assistant'
                          ? stripSynthesisXml(message.content)
                          : message.content
                    }
                    isStreaming={false}
                    isProgressUpdate={message.isProgressUpdate}
                    senderName={message.senderName}
                    showSenderName={showSenderName}
                  />
                  {/* Phase 63: @mention pill badge above user message content */}
                  {message.role === 'user' && message.mentionMetadata && (
                    <div className="flex justify-end px-4 -mt-4 mb-2">
                      <MentionPill
                        connectorName={message.mentionMetadata.connectorName}
                        connectorType={message.mentionMetadata.connectorType}
                      />
                    </div>
                  )}
                </>
              )
            )}

            {/* Connector breadcrumb trail (Phase 62 upgrade of SourceTags) */}
            {message.connectorSources && message.connectorSources.length > 0 && message.role === 'assistant' && (
              <ConnectorBreadcrumb
                connectors={message.connectorSources}
                onChipClick={(connectorName) => onBreadcrumbChipClick?.(message.id, connectorName)}
              />
            )}

            {/* Data preview for messages with data_refs */}
            {message.dataRefs && message.dataRefs.length > 0 && (
              <DataPreview
                dataRefs={message.dataRefs}
                sessionId={message.dataRefs[0].session_id}
              />
            )}

            {/* Show plan preview if workflow is waiting for approval */}
            {message.workflowId &&
              message.plan &&
              message.status === 'WAITING_APPROVAL' &&
              currentWorkflow?.id === message.workflowId && (
                <PlanPreview
                  plan={message.plan}
                  workflowId={message.workflowId}
                  workflowStatus={message.status}
                  onApprove={onApprove}
                  onReject={onReject}
                  onEdit={onEditPlan}
                  onClone={onCloneWorkflow}
                  onRetry={onRetryWorkflow}
                  isApproving={isApproving}
                />
              )}

            {/* Show plan with repeatability for completed/failed workflows */}
            {message.workflowId &&
              message.plan &&
              (message.status === 'COMPLETED' || message.status === 'FAILED') &&
              currentWorkflow?.id === message.workflowId && (
                <PlanPreview
                  plan={message.plan}
                  workflowId={message.workflowId}
                  workflowStatus={message.status}
                  onApprove={onApprove}
                  onReject={onReject}
                  onClone={onCloneWorkflow}
                  onRetry={onRetryWorkflow}
                  isApproving={false}
                />
              )}

            {/* Show execution monitor if workflow is running */}
            {message.workflowId &&
              currentWorkflow?.id === message.workflowId &&
              currentWorkflow.status === 'RUNNING' && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  className="my-6 glass rounded-xl p-6 border border-primary/20"
                  data-testid="execution-status"
                >
                  <div className="flex items-center gap-3 mb-2">
                    <div className="w-2 h-2 rounded-full bg-primary animate-pulse" />
                    <h3 className="text-sm font-semibold text-white uppercase tracking-wider">
                      Executing Plan
                    </h3>
                  </div>
                  <p className="text-sm text-text-secondary pl-5">
                    Running automated steps...
                  </p>
                </motion.div>
              )}
          </div>
          );
        })}
      </div>

      {/* Follow-up suggestion chips (Phase 62) */}
      {followUpSuggestions && followUpSuggestions.length > 0 && !isProcessing && (
        <FollowUpChips
          suggestions={followUpSuggestions}
          onSuggestionClick={(suggestion) => onFollowUpClick?.(suggestion)}
        />
      )}

      {/* Live Orchestrator Progress */}
      {isProcessing && isOrchestratorActive && liveEventsStartTime && orchestratorEvents && (
        <div className="mt-4">
          <OrchestratorProgress
            events={orchestratorEvents}
            startTime={liveEventsStartTime}
            isLive={true}
          />
          <TypingIndicator />
        </div>
      )}

      {/* Typing indicator when processing without orchestrator */}
      {isProcessing && !isOrchestratorActive && (
        <TypingIndicator />
      )}
    </>
  );
}
