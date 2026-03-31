// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Inline Error Card Component (Phase 59)
 *
 * Renders structured error messages inline in the chat flow with:
 * - Color-coded left border (amber retryable, red fatal, blue informational)
 * - Severity icon + error type header
 * - Expandable details section for stack traces
 * - Retry button for retryable errors
 * - Connector badge when a specific connector failed
 */
import { useState } from 'react';
import { AlertTriangle, XCircle, Info, ChevronDown, ChevronUp, RotateCcw } from 'lucide-react';
import { motion } from 'motion/react';
import type { ErrorSeverity } from '../types';

interface ErrorCardProps {
  errorType: string;
  message: string;
  severity: ErrorSeverity;
  details?: string;
  connectorName?: string;
  onRetry?: () => void;
}

const severityConfig = {
  retryable: {
    border: 'border-l-4 border-amber-500',
    bg: 'bg-amber-500/5',
    icon: AlertTriangle,
    iconColor: 'text-amber-500',
    label: 'Retryable',
  },
  fatal: {
    border: 'border-l-4 border-red-500',
    bg: 'bg-red-500/5',
    icon: XCircle,
    iconColor: 'text-red-500',
    label: 'Error',
  },
  informational: {
    border: 'border-l-4 border-blue-500',
    bg: 'bg-blue-500/5',
    icon: Info,
    iconColor: 'text-blue-500',
    label: 'Info',
  },
} as const;

export function ErrorCard({ errorType, message, severity, details, connectorName, onRetry }: ErrorCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const config = severityConfig[severity];
  const Icon = config.icon;

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="flex gap-4 mb-6 px-4"
    >
      {/* Spacer to align with assistant messages (matches avatar width) */}
      <div className="flex-shrink-0 w-9" />

      <div className="flex-1 max-w-3xl">
        <div
          className={`${config.border} ${config.bg} rounded-lg overflow-hidden border border-white/10`}
          data-testid="error-card"
        >
          {/* Header: severity icon + error type */}
          <div className="flex items-center gap-3 px-4 py-3">
            <Icon className={`h-5 w-5 flex-shrink-0 ${config.iconColor}`} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-white">{errorType}</span>
                {connectorName && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-white/10 text-text-tertiary font-mono">
                    {connectorName}
                  </span>
                )}
              </div>
            </div>
          </div>

          {/* Message body */}
          <div className="px-4 pb-3 text-sm text-text-secondary leading-relaxed">
            {message}
          </div>

          {/* Expandable details section */}
          {details && (
            <div className="border-t border-white/5">
              <button
                onClick={() => setIsExpanded(!isExpanded)}
                className="flex items-center gap-2 px-4 py-2 w-full text-left text-xs text-text-tertiary hover:text-text-secondary transition-colors"
                type="button"
              >
                {isExpanded ? (
                  <ChevronUp className="h-3.5 w-3.5" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5" />
                )}
                <span>{isExpanded ? 'Hide details' : 'Show details'}</span>
              </button>
              {isExpanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  transition={{ duration: 0.2 }}
                  className="px-4 pb-3"
                >
                  <pre className="text-xs font-mono text-text-tertiary bg-black/20 rounded p-3 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap break-words">
                    {details}
                  </pre>
                </motion.div>
              )}
            </div>
          )}

          {/* Retry button (only for retryable errors) */}
          {onRetry && (
            <div className="border-t border-white/5 px-4 py-2.5">
              <button
                onClick={onRetry}
                className="flex items-center gap-1.5 text-xs font-medium text-amber-400 hover:text-amber-300 transition-colors"
                type="button"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                <span>Retry</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}
