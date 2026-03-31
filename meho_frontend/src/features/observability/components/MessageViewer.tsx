// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * MessageViewer Component
 *
 * Displays PydanticAI messages in a chat-style format.
 * Parses the message structure (kind, parts) and renders appropriately.
 */
import { useState } from 'react';
import { User, Bot, Wrench, ChevronDown, ChevronRight, AlertCircle } from 'lucide-react';
import { cn, JsonViewer, CopyButton } from '@/shared';

export interface MessageViewerProps {
  /** Array of PydanticAI message objects */
  messages: Array<Record<string, unknown>>;
  /** Additional CSS classes */
  className?: string;
}

/**
 * Message part structure from PydanticAI.
 */
interface MessagePart {
  part_kind?: string;
  content?: string;
  tool_name?: string;
  args?: Record<string, unknown>;
  tool_call_id?: string;
  return_value?: unknown;
  [key: string]: unknown;
}

/**
 * Parsed message structure.
 */
interface ParsedMessage {
  kind: 'request' | 'response' | 'unknown';
  parts: MessagePart[];
  timestamp?: string;
}

/**
 * Parse a raw message object into a structured format.
 */
function parseMessage(msg: Record<string, unknown>): ParsedMessage {
  const kind = (msg.kind as string) || 'unknown';
  const parts = (msg.parts as MessagePart[]) || [];
  const timestamp = msg.timestamp as string | undefined;

  return {
    kind: kind === 'request' ? 'request' : kind === 'response' ? 'response' : 'unknown',
    parts,
    timestamp,
  };
}

/**
 * Collapsible tool call/result card.
 */
function ToolCard({
  title,
  icon,
  children,
  defaultOpen = false,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="border border-border rounded-md overflow-hidden bg-surface/50">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-surface-hover transition-colors text-left"
      >
        {isOpen ? (
          <ChevronDown className="w-3.5 h-3.5 text-text-tertiary flex-shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-text-tertiary flex-shrink-0" />
        )}
        <span className="text-text-tertiary">{icon}</span>
        <span className="text-text-secondary text-sm font-medium truncate">{title}</span>
      </button>
      {isOpen && (
        <div className="border-t border-border bg-background p-3 max-h-64 overflow-auto">
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * Render a single message part.
 */
function MessagePartRenderer({ part }: { part: MessagePart }) {
  const partKind = part.part_kind || 'unknown';

  // User prompt - plain text
  if (partKind === 'user-prompt') {
    return (
      <p className="text-text-secondary text-sm whitespace-pre-wrap">
        {part.content || '(empty)'}
      </p>
    );
  }

  // System prompt - show but styled differently
  if (partKind === 'system-prompt') {
    return (
      <div className="text-amber-300/80 text-xs italic">
        <span className="font-semibold">System: </span>
        <span className="line-clamp-2">{part.content || '(system prompt)'}</span>
      </div>
    );
  }

  // Text response
  if (partKind === 'text') {
    const content = part.content || '';
    // Check if content looks like JSON (starts with { or [)
    if (content.trim().startsWith('{') || content.trim().startsWith('[')) {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(content);
      } catch {
        // Not valid JSON, will render as text
      }
      if (parsed !== null) {
        return <JsonViewer data={parsed} />;
      }
    }
    return (
      <p className="text-text-secondary text-sm whitespace-pre-wrap">
        {content || '(empty response)'}
      </p>
    );
  }

  // Tool call
  if (partKind === 'tool-call') {
    const toolName = part.tool_name || 'unknown_tool';
    const args = part.args || {};
    return (
      <ToolCard
        title={`Call: ${toolName}`}
        icon={<Wrench className="w-3.5 h-3.5" />}
      >
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Arguments</span>
            <CopyButton data={args} size="sm" />
          </div>
          <JsonViewer data={args} />
        </div>
      </ToolCard>
    );
  }

  // Tool return
  if (partKind === 'tool-return') {
    const returnValue = part.return_value ?? part.content;
    const toolCallId = part.tool_call_id || '';
    return (
      <ToolCard
        title={`Result${toolCallId ? `: ${toolCallId.slice(0, 8)}...` : ''}`}
        icon={<Wrench className="w-3.5 h-3.5" />}
      >
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-text-tertiary">Return Value</span>
            <CopyButton data={returnValue} size="sm" />
          </div>
          {typeof returnValue === 'object' && returnValue !== null ? (
            <JsonViewer data={returnValue} />
          ) : (
            <pre className="text-text-secondary text-xs whitespace-pre-wrap font-mono">
              {String(returnValue)}
            </pre>
          )}
        </div>
      </ToolCard>
    );
  }

  // Unknown part kind - show as JSON
  return (
    <div className="text-text-tertiary text-xs">
      <span className="font-semibold">{partKind}: </span>
      <JsonViewer data={part} />
    </div>
  );
}

/**
 * Render a complete message (request or response).
 */
function MessageBubble({ message, index }: { message: ParsedMessage; index: number }) {
  const isRequest = message.kind === 'request';
  const isResponse = message.kind === 'response';

  // Filter out system prompts for cleaner display (they're shown in System Prompt section)
  const displayParts = message.parts.filter(
    (part) => part.part_kind !== 'system-prompt'
  );

  // Skip if no displayable parts
  if (displayParts.length === 0) {
    return null;
  }

  return (
    <div
      className={cn(
        'flex gap-3',
        isRequest ? 'flex-row-reverse' : 'flex-row'
      )}
    >
      {/* Avatar */}
      <div
        className={cn(
          'w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0',
          isRequest ? 'bg-blue-600' : 'bg-gray-600'
        )}
      >
        {isRequest ? (
          <User className="w-4 h-4 text-white" />
        ) : (
          <Bot className="w-4 h-4 text-white" />
        )}
      </div>

      {/* Message content */}
      <div
        className={cn(
          'flex-1 max-w-[85%] rounded-lg p-3',
          isRequest
            ? 'bg-blue-600/20 border border-blue-500/30'
            : 'bg-gray-800 border border-gray-700',
          !isRequest && !isResponse && 'bg-surface border border-border'
        )}
      >
        {/* Header with role label */}
        <div className="flex items-center gap-2 mb-2">
          <span
            className={cn(
              'text-xs font-semibold uppercase tracking-wide',
              isRequest ? 'text-blue-400' : 'text-gray-400'
            )}
          >
            {isRequest ? 'User' : isResponse ? 'Assistant' : 'Unknown'}
          </span>
          {message.timestamp && (
            <span className="text-xs text-text-tertiary">
              {new Date(message.timestamp).toLocaleTimeString()}
            </span>
          )}
        </div>

        {/* Parts */}
        <div className="space-y-3">
          {displayParts.map((part, partIdx) => (
            <MessagePartRenderer key={`${index}-${partIdx}`} part={part} />
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * Chat-style message viewer for PydanticAI messages.
 *
 * Parses PydanticAI message structure and displays in a readable format:
 * - Request messages (user) shown on the right with blue styling
 * - Response messages (assistant) shown on the left with gray styling
 * - Tool calls and results shown as collapsible cards
 *
 * @example
 * ```tsx
 * <MessageViewer
 *   messages={[
 *     { kind: "request", parts: [{ part_kind: "user-prompt", content: "Hello" }] },
 *     { kind: "response", parts: [{ part_kind: "text", content: "Hi there!" }] }
 *   ]}
 * />
 * ```
 */
export function MessageViewer({ messages, className }: MessageViewerProps) {
  if (!messages || messages.length === 0) {
    return (
      <div className="flex items-center gap-2 text-text-tertiary text-sm italic">
        <AlertCircle className="w-4 h-4" />
        <span>No messages to display</span>
      </div>
    );
  }

  const parsedMessages = messages.map(parseMessage);

  return (
    <div className={cn('space-y-4', className)}>
      {parsedMessages.map((msg, idx) => (
        <MessageBubble key={idx} message={msg} index={idx} />
      ))}
    </div>
  );
}
