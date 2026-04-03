// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connector Card Component
 *
 * Individual connector status card for the orchestrator.
 * Shows connector name, status badge, event timeline,
 * and findings preview when complete.
 * 
 * Click any event to open a modal with full INPUT/OUTPUT JSON.
 */
import { useState, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { motion, AnimatePresence } from 'motion/react';
import {
  ChevronDown,
  ChevronRight,
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  AlertTriangle,
  Ban,
  X,
  Copy,
  Check,
} from 'lucide-react';
import clsx from 'clsx';
import type { ConnectorState, ConnectorStatus, WrappedAgentEvent } from '@/api/types/orchestrator';
import { extractOutputSummary } from '../utils/extractOutputSummary';
import { groupSteps } from '../utils/groupSteps';
import type { StepGroup } from '../utils/groupSteps';
import { StepCard } from './StepCard';

/**
 * Extended connector state with timing info (from OrchestratorProgress).
 */
interface ConnectorStateWithTiming extends ConnectorState {
  firstEventTime?: number;
  eventsWithTimestamps?: Array<WrappedAgentEvent & { arrivalTime: number }>;
}

interface ConnectorCardProps {
  connector: ConnectorStateWithTiming;
  startTime: number;
  totalElapsed: number;
  defaultExpanded?: boolean;
  isLive?: boolean;
}

/**
 * Get status icon and color.
 */
function getStatusDisplay(status: ConnectorStatus): {
  icon: React.ReactNode;
  color: string;
  bgColor: string;
  label: string;
} {
  switch (status) {
    case 'pending':
      return {
        icon: <Clock className="w-3.5 h-3.5" />,
        color: 'text-slate-400',
        bgColor: 'bg-slate-700/50',
        label: 'Pending',
      };
    case 'running':
      return {
        icon: <Loader2 className="w-3.5 h-3.5 animate-spin" />,
        color: 'text-cyan-400',
        bgColor: 'bg-cyan-900/30',
        label: 'Running',
      };
    case 'success':
      return {
        icon: <CheckCircle className="w-3.5 h-3.5" />,
        color: 'text-emerald-400',
        bgColor: 'bg-emerald-900/30',
        label: 'Success',
      };
    case 'partial':
      return {
        icon: <AlertTriangle className="w-3.5 h-3.5" />,
        color: 'text-yellow-400',
        bgColor: 'bg-yellow-900/30',
        label: 'Partial',
      };
    case 'failed':
      return {
        icon: <XCircle className="w-3.5 h-3.5" />,
        color: 'text-red-400',
        bgColor: 'bg-red-900/30',
        label: 'Failed',
      };
    case 'timeout':
      return {
        icon: <Clock className="w-3.5 h-3.5" />,
        color: 'text-amber-400',
        bgColor: 'bg-amber-900/30',
        label: 'Timeout',
      };
    case 'cancelled':
      return {
        icon: <Ban className="w-3.5 h-3.5" />,
        color: 'text-slate-400',
        bgColor: 'bg-slate-700/50',
        label: 'Cancelled',
      };
    default:
      return {
        icon: <Clock className="w-3.5 h-3.5" />,
        color: 'text-slate-400',
        bgColor: 'bg-slate-700/50',
        label: 'Unknown',
      };
  }
}

// extractOutputSummary is now imported from ../utils/extractOutputSummary

/**
 * Format duration for display.
 */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

/**
 * Grouped tool call with input and output.
 */
interface ToolCall {
  tool: string;
  input: unknown;
  output: unknown;
}

/**
 * Syntax-highlight a single line of JSON.
 * Colors match MEHO app theme (purple primary accent).
 */
function highlightLine(line: string, keyOffset: number): React.ReactNode[] { // NOSONAR (cognitive complexity)
  const result: React.ReactNode[] = [];
  let i = 0;
  let key = keyOffset;
  
  const patterns = [
    { type: 'string', regex: /"(?:[^"\\]|\\.)*"/, color: 'text-emerald-400' },
    { type: 'number', regex: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/, color: 'text-amber-400' },
    { type: 'boolean', regex: /\b(?:true|false)\b/, color: 'text-violet-400' },
    { type: 'null', regex: /\bnull\b/, color: 'text-text-tertiary' },
    { type: 'brace', regex: /[{}[\]]/, color: 'text-text-secondary' },
    { type: 'colon', regex: /:/, color: 'text-text-tertiary' },
    { type: 'comma', regex: /,/, color: 'text-text-tertiary' },
  ];

  while (i < line.length) {
    // Handle leading whitespace (preserve indentation)
    if (line[i] === ' ') {
      let spaces = '';
      while (i < line.length && line[i] === ' ') {
        spaces += ' ';
        i++;
      }
      result.push(<span key={key++}>{spaces}</span>);
      continue;
    }

    let matched = false;
    for (const { regex, color, type } of patterns) {
      const match = line.slice(i).match(new RegExp(`^${regex.source}`));
      if (match) {
        const text = match[0];
        
        if (type === 'string') {
          const afterMatch = line.slice(i + text.length).match(/^\s*:/);
          if (afterMatch) {
            // Keys in primary purple color to match app theme
            result.push(<span key={key++} className="text-primary">{text}</span>);
          } else {
            result.push(<span key={key++} className={color}>{text}</span>);
          }
        } else {
          result.push(<span key={key++} className={color}>{text}</span>);
        }
        
        i += text.length;
        matched = true;
        break;
      }
    }
    
    if (!matched) {
      result.push(<span key={key++}>{line[i]}</span>);
      i++;
    }
  }
  
  return result;
}

/**
 * Syntax-highlighted JSON viewer with line numbers (editor-style).
 * Styled to match MEHO app theme.
 */
function JsonViewer({ data }: Readonly<{ data: unknown }>) {
  // Compute JSON lines outside JSX to avoid constructing JSX in try/catch
  const parsed = useMemo(() => {
    try {
      const json = JSON.stringify(data, null, 2);
      const lines = json.split('\n');
      return { lines, lineNumWidth: String(lines.length).length };
    } catch {
      return null;
    }
  }, [data]);

  if (!parsed) {
    return <span className="text-text-tertiary">{String(data)}</span>;
  }

  const { lines, lineNumWidth } = parsed;

  return (
    <div className="flex text-[15px] font-mono leading-6">
      {/* Line numbers gutter */}
      <div className="flex-shrink-0 select-none text-right pr-4 border-r border-border/30 text-text-tertiary min-w-[3rem]">
        {lines.map((_, idx) => (
          <div key={`line-${idx}`}>
            {String(idx + 1).padStart(lineNumWidth, ' ')}
          </div>
        ))}
      </div>
      {/* Code content */}
      <div className="flex-1 pl-4 overflow-x-auto">
        {lines.map((line, idx) => (
          <div key={`line-${idx}`} className="whitespace-pre">
            {highlightLine(line, idx * 1000)}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Copy to clipboard button with feedback.
 */
function CopyButton({ data }: Readonly<{ data: unknown }>) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      const text = JSON.stringify(data, null, 2);
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <button
      onClick={handleCopy}
      className="text-text-tertiary hover:text-text-primary transition-colors p-1.5 rounded-lg hover:bg-surface-hover"
      title="Copy to clipboard"
    >
      {copied ? (
        <Check className="w-4 h-4 text-green-400" />
      ) : (
        <Copy className="w-4 h-4" />
      )}
    </button>
  );
}

/**
 * Tool Call Modal - Shows INPUT and OUTPUT side by side with syntax highlighting.
 * Styled to match MEHO app theme (purple primary, dark surfaces).
 */
function ToolCallModal({
  toolCall,
  onClose,
}: Readonly<{
  toolCall: ToolCall;
  onClose: () => void;
}>) {
  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- modal backdrop, keyboard close handled by Escape
    <div
      className="fixed inset-0 z-[99999] flex items-center justify-center p-6 bg-black/85 backdrop-blur-sm"
      onClick={onClose}
    >
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- stop propagation on modal content */}
      <div
        className="bg-surface border border-border rounded-2xl shadow-xl max-w-6xl w-full max-h-[85vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-background">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary/20 flex items-center justify-center">
              <div className="w-2.5 h-2.5 rounded-full bg-primary" />
            </div>
            <span className="text-text-primary font-semibold">
              {toolCall.tool}
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-text-tertiary hover:text-text-primary transition-colors p-2 hover:bg-surface-hover rounded-lg"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content - Side by side */}
        <div className="grid grid-cols-2 gap-0 h-[calc(85vh-72px)]">
          {/* INPUT */}
          <div className="border-r border-border flex flex-col min-h-0">
            <div className="px-5 py-3 bg-surface border-b border-border flex-shrink-0 flex items-center justify-between">
              <span className="text-primary text-xs font-semibold tracking-wide uppercase">Input</span>
              <CopyButton data={toolCall.input} />
            </div>
            <div className="flex-1 overflow-auto min-h-0 bg-background p-4">
              <JsonViewer data={toolCall.input} />
            </div>
          </div>

          {/* OUTPUT */}
          <div className="flex flex-col min-h-0">
            <div className="px-5 py-3 bg-surface border-b border-border flex-shrink-0 flex items-center justify-between">
              <span className="text-primary text-xs font-semibold tracking-wide uppercase">Output</span>
              <CopyButton data={toolCall.output} />
            </div>
            <div className="flex-1 overflow-auto min-h-0 bg-background p-4">
              <JsonViewer data={toolCall.output} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Think Event Modal - Shows thought content.
 * Styled to match MEHO app theme (purple primary, dark surfaces).
 */
function ThinkModal({
  content,
  onClose,
}: Readonly<{
  content: string;
  onClose: () => void;
}>) {
  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- modal backdrop, keyboard close handled by Escape
    <div
      className="fixed inset-0 z-[99999] flex items-center justify-center p-6 bg-black/85 backdrop-blur-sm"
      onClick={onClose}
    >
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- stop propagation on modal content */}
      <div
        className="bg-surface border border-border rounded-2xl shadow-xl max-w-2xl w-full max-h-[60vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-border bg-background">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary/20 flex items-center justify-center">
              <div className="w-2.5 h-2.5 rounded-full bg-primary" />
            </div>
            <span className="text-text-primary font-semibold">
              Thought
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-text-tertiary hover:text-text-primary transition-colors p-2 hover:bg-surface-hover rounded-lg"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="flex-1 overflow-auto max-h-[calc(60vh-72px)] bg-background">
          <div className="text-text-secondary text-sm whitespace-pre-wrap p-6 leading-relaxed">
            {content}
          </div>
        </div>
      </div>
    </div>
  );
}

export function ConnectorCard({
  connector,
  startTime,
  totalElapsed: _totalElapsed,
  defaultExpanded = false,
  isLive = false,
}: Readonly<ConnectorCardProps>) {
  void _totalElapsed; // Kept for API compatibility
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const [selectedToolCall, setSelectedToolCall] = useState<ToolCall | null>(null);
  const [selectedThought, setSelectedThought] = useState<string | null>(null);

  const statusDisplay = useMemo(() => getStatusDisplay(connector.status), [connector.status]);

  // Calculate connector TTFT (time from startTime to first event)
  const connectorTTFT = useMemo(() => {
    if (connector.firstEventTime) {
      return connector.firstEventTime - startTime;
    }
    return null;
  }, [connector.firstEventTime, startTime]);

  // Extract agent type from first event (e.g., "generic" -> "generic_agent")
  const agentType = useMemo(() => {
    const firstEvent = connector.events[0];
    if (firstEvent?.agent_source?.agent_name) {
      const fullName = firstEvent.agent_source.agent_name;
      const baseName = fullName.split('_')[0];
      return baseName.endsWith('_agent') ? baseName : `${baseName}_agent`;
    }
    return null;
  }, [connector.events]);

  // Group events into coherent steps (think -> act -> result as one unit)
  const steps = useMemo(
    () => groupSteps(connector.events ?? []),
    [connector.events],
  );

  /**
   * Handle step click -- opens the tool call modal or thought modal
   * depending on the step type.
   */
  const handleStepClick = (step: StepGroup) => {
    if (step.toolName === 'thinking') {
      if (step.thought) setSelectedThought(step.thought);
      return;
    }

    // Build ToolCall from step data for the existing modal
    // Extract output from the observation event if available
    const obsEvent = step.originalEvents.find(
      (e) => e.inner_event?.type === 'observation',
    );
    const obsData = obsEvent?.inner_event?.data as Record<string, unknown> | undefined;
    const output = obsData?.result ?? obsData ?? (step.result ? { content: step.result } : null);
    const outputSummary = extractOutputSummary(step.toolName, output);

    setSelectedToolCall({
      tool: step.toolLabel,
      input: step.args,
      output: output ?? (outputSummary ? { summary: outputSummary } : { status: step.status }),
    });
  };

  const hasEvents = steps.length > 0;
  const hasFindings = connector.findings && connector.findings.length > 0;
  const isError = connector.status === 'failed' || connector.status === 'timeout';

  return (
    <div
      className={clsx(
        'rounded-md border transition-colors',
        isError
          ? 'bg-red-950/20 border-red-900/40'
          : 'bg-slate-900/60 border-slate-800/50'
      )}
    >
      {/* Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={clsx(
          'w-full flex items-center gap-2 px-3 py-2 text-left',
          'hover:bg-slate-800/30 transition-colors rounded-md'
        )}
      >
        {/* Expand/collapse icon */}
        <span className="text-slate-500 flex-shrink-0">
          {isExpanded ? (
            <ChevronDown className="w-3 h-3" />
          ) : (
            <ChevronRight className="w-3 h-3" />
          )}
        </span>

        {/* Agent type + Connector name: generic_agent (rke2-prod-dc-graz) */}
        <span className="font-medium text-slate-200 text-sm truncate">
          {agentType ? `${agentType} (${connector.name})` : connector.name}
        </span>

        {/* Status badge */}
        <span
          className={clsx(
            'flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium',
            statusDisplay.bgColor,
            statusDisplay.color
          )}
        >
          {statusDisplay.icon}
          <span>{statusDisplay.label}</span>
        </span>

        {/* Metrics - TTFT for this connector */}
        {connectorTTFT !== null && (
          <span className="ml-auto font-mono text-xs text-slate-500">
            TTFT {formatDuration(connectorTTFT)}
          </span>
        )}
      </button>

      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 space-y-2">
              {/* Findings - full content, no truncation */}
              {hasFindings && (
                <div className="rounded-lg bg-emerald-950/30 border border-emerald-800/30 overflow-hidden">
                  <div className="px-3 py-2 bg-emerald-900/20 border-b border-emerald-800/30">
                    <span className="text-xs font-medium text-emerald-300">
                      Findings
                    </span>
                  </div>
                  <div className="px-3 py-2 text-sm text-slate-300 whitespace-pre-wrap leading-relaxed">
                    {connector.findings}
                  </div>
                </div>
              )}

              {/* Error message */}
              {connector.error && (
                <div className="rounded-lg bg-red-950/30 border border-red-800/30 overflow-hidden">
                  <div className="px-3 py-2 bg-red-900/20 border-b border-red-800/30">
                    <span className="text-xs font-medium text-red-300">
                      Error
                    </span>
                  </div>
                  <div className="px-3 py-2 text-sm text-red-200">
                    {connector.error}
                  </div>
                </div>
              )}

              {/* Grouped step timeline - coherent steps with operation labels */}
              {hasEvents && (
                <div className="rounded-lg bg-slate-900/80 border border-slate-700/50 overflow-hidden">
                  {/* Header */}
                  <div className="px-3 py-2 bg-slate-800/60 border-b border-slate-700/50">
                    <span className="text-xs font-medium text-slate-300">
                      Workflow
                    </span>
                    <span className="text-xs text-slate-500 ml-2">
                      {steps.length} steps
                    </span>
                  </div>

                  {/* Step cards */}
                  <div className="p-2 space-y-0.5">
                    {steps.map((step, idx) => (
                      <StepCard
                        key={step.id}
                        step={step}
                        stepNumber={idx + 1}
                        isLive={isLive}
                        onClickStep={handleStepClick}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Tool call modal - INPUT/OUTPUT side by side (Portal to body) */}
              {selectedToolCall && createPortal(
                <ToolCallModal
                  toolCall={selectedToolCall}
                  onClose={() => setSelectedToolCall(null)}
                />,
                document.body
              )}

              {/* Thought modal (Portal to body) */}
              {selectedThought && createPortal(
                <ThinkModal
                  content={selectedThought}
                  onClose={() => setSelectedThought(null)}
                />,
                document.body
              )}

              {/* Loading indicator for live running state */}
              {isLive && !hasEvents && !hasFindings && (
                <div className="flex items-center justify-center gap-2 py-2 text-slate-500 text-xs">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  <span>Processing...</span>
                </div>
              )}

              {/* Empty state */}
              {!isLive && !hasEvents && !hasFindings && !connector.error && (
                <div className="text-center py-2 text-slate-600 text-xs italic">
                  No events recorded
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
