// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Step Card Component
 *
 * Renders a single StepGroup as a coherent step card in the connector timeline.
 * Each step shows an operation-aware label (D-12), expandable reasoning (D-13),
 * and groups think/act/result into one visual unit (D-14).
 */
import { useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import {
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  ChevronRight,
  ChevronDown,
  Brain,
} from 'lucide-react';
import clsx from 'clsx';
import type { StepGroup } from '../utils/groupSteps';

interface StepCardProps {
  step: StepGroup;
  stepNumber: number;
  isLive?: boolean;
  onClickStep?: (step: StepGroup) => void;
}

/**
 * Format duration in ms for display.
 */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

/**
 * Get the status icon for a step.
 */
function getStatusIcon(
  status: StepGroup['status'],
  isLive?: boolean,
): React.ReactNode {
  switch (status) {
    case 'running':
      return (
        <Loader2
          className={clsx(
            'w-3.5 h-3.5 text-cyan-400',
            isLive && 'animate-spin',
          )}
        />
      );
    case 'success':
      return <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />;
    case 'failed':
      return <XCircle className="w-3.5 h-3.5 text-red-400" />;
    case 'pending':
    default:
      return <Clock className="w-3.5 h-3.5 text-slate-500" />;
  }
}

/**
 * StepCard renders a single grouped step (think + act + result as one unit).
 *
 * - Header: step number, operation label, status icon, duration
 * - Expandable reasoning section (D-13): shows thought on click
 * - Result summary: one-line observation when available
 */
export function StepCard({
  step,
  stepNumber,
  isLive,
  onClickStep,
}: Readonly<StepCardProps>) {
  const [isReasoningExpanded, setIsReasoningExpanded] = useState(false);
  const isThought = step.toolName === 'thinking';

  // Standalone thought rendering
  if (isThought) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.15 }}
        className="flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-slate-800/80 transition-all duration-150"
      >
        <span className="text-slate-600 w-3 text-center flex-shrink-0 font-mono text-[10px] mt-0.5">
          {stepNumber}
        </span>
        <Brain className="w-3.5 h-3.5 text-violet-400 flex-shrink-0 mt-0.5" />
        <span className="text-slate-400 text-xs italic line-clamp-3 flex-1">
          {step.thought}
        </span>
      </motion.div>
    );
  }

  const hasReasoning = Boolean(step.thought);
  const hasResult = Boolean(step.observationSummary ?? step.result);

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.15 }}
      className="group"
    >
      {/* Main header row */}
      <button
        onClick={() => onClickStep?.(step)}
        className={clsx(
          'w-full flex items-center gap-2 text-left rounded-md px-2 py-1.5',
          'hover:bg-slate-800/80 transition-all duration-150',
          'cursor-pointer',
        )}
      >
        {/* Step number badge */}
        <span className="text-slate-600 w-3 text-center flex-shrink-0 font-mono text-[10px]">
          {stepNumber}
        </span>

        {/* Status icon */}
        <span className="flex-shrink-0">
          {getStatusIcon(step.status, isLive)}
        </span>

        {/* Operation label */}
        <span
          className={clsx(
            'text-sm font-medium truncate flex-1',
            step.status === 'failed'
              ? 'text-red-300'
              : 'text-sky-300 group-hover:text-sky-200',
          )}
        >
          {step.toolLabel}
        </span>

        {/* Reasoning toggle */}
        {hasReasoning && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setIsReasoningExpanded(!isReasoningExpanded);
            }}
            className="text-violet-400/60 hover:text-violet-400 transition-colors flex-shrink-0 p-0.5"
            title="Show reasoning"
          >
            {isReasoningExpanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </button>
        )}

        {/* Duration */}
        {step.duration !== undefined && step.status !== 'running' && (
          <span className="text-slate-500 text-xs font-mono flex-shrink-0">
            {formatDuration(step.duration)}
          </span>
        )}
      </button>

      {/* Expandable reasoning section (D-13) */}
      <AnimatePresence>
        {isReasoningExpanded && step.thought && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div className="ml-8 mr-2 mb-1 px-2 py-1.5 rounded bg-violet-500/5 border-l-2 border-violet-500/30">
              <span className="text-[10px] font-medium text-violet-400 uppercase tracking-wide">
                Reasoning
              </span>
              <p className="text-xs text-slate-500 italic mt-0.5 whitespace-pre-wrap leading-relaxed">
                {step.thought}
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Result summary */}
      {hasResult && (
        <div className="flex items-center gap-2 px-2 py-0.5 ml-8">
          <span className="text-slate-600 text-xs">{'\u2192'}</span>
          <span className="text-amber-300/90 text-xs line-clamp-1">
            {step.observationSummary ?? step.result}
          </span>
        </div>
      )}
    </motion.div>
  );
}
