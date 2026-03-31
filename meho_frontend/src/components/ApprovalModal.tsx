// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Approval Modal Component
 *
 * Centered overlay modal for WRITE and DESTRUCTIVE operations.
 * Shows full operation details: operation ID, connector name,
 * parameters, trust tier. Supports a queue of concurrent approvals.
 *
 * DESTRUCTIVE operations require an extra confirmation click.
 * Renders via React Portal to escape parent stacking contexts.
 */
import { useState, useEffect, useRef, useId } from 'react';
import { createPortal } from 'react-dom';
import { motion, AnimatePresence } from 'motion/react';
import { AlertTriangle, CheckCircle, XCircle, Server, FileText, Globe } from 'lucide-react';
import clsx from 'clsx';
import { useFocusTrap } from '@/shared/hooks/useFocusTrap';

export interface ApprovalRequest {
  approval_id: string | null;
  tool: string;
  danger_level: string;
  details: {
    method?: string;
    path?: string;
    description?: string;
    impact?: string;
  };
  tool_args?: Record<string, unknown>;
  message: string;
}

interface ApprovalModalProps {
  approval: ApprovalRequest;
  onApprove: (approvalId: string) => Promise<void>;
  onReject: (approvalId: string) => Promise<void>;
  isProcessing?: boolean;
  queuePosition?: number;
  queueTotal?: number;
}

const TIER_COLORS: Record<string, { bg: string; text: string; border: string; iconColor: string; label: string }> = {
  write: {
    bg: 'bg-yellow-500/15',
    text: 'text-yellow-400',
    border: 'border-yellow-500/30',
    iconColor: 'text-yellow-400',
    label: 'Write Operation Requires Approval',
  },
  destructive: {
    bg: 'bg-red-500/15',
    text: 'text-red-400',
    border: 'border-red-500/30',
    iconColor: 'text-red-500',
    label: 'Destructive Operation Requires Approval',
  },
  caution: {
    bg: 'bg-yellow-500/15',
    text: 'text-yellow-400',
    border: 'border-yellow-500/30',
    iconColor: 'text-yellow-400',
    label: 'Write Operation Requires Approval',
  },
  dangerous: {
    bg: 'bg-red-500/15',
    text: 'text-red-400',
    border: 'border-red-500/30',
    iconColor: 'text-red-500',
    label: 'Destructive Operation Requires Approval',
  },
  critical: {
    bg: 'bg-red-500/15',
    text: 'text-red-400',
    border: 'border-red-500/30',
    iconColor: 'text-red-500',
    label: 'Destructive Operation Requires Approval',
  },
};

const METHOD_COLORS: Record<string, string> = {
  GET: 'text-blue-400 bg-blue-500/10 border-blue-500/30',
  POST: 'text-green-400 bg-green-500/10 border-green-500/30',
  PUT: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/30',
  PATCH: 'text-purple-400 bg-purple-500/10 border-purple-500/30',
  DELETE: 'text-red-400 bg-red-500/10 border-red-500/30',
};

function resolveTier(dangerLevel: string): 'write' | 'destructive' {
  const dl = dangerLevel.toLowerCase();
  if (dl === 'destructive' || dl === 'critical' || dl === 'dangerous') return 'destructive';
  return 'write';
}

/**
 * Build display-friendly parameters from tool_args, stripping internal
 * fields like connector_id that aren't useful for the operator.
 */
function getDisplayParams(args: Record<string, unknown> | undefined): Record<string, unknown> | null {
  if (!args) return null;
  const { connector_id: _, ...rest } = args;
  if (Object.keys(rest).length === 0) return null;
  return rest;
}

export function ApprovalModal({
  approval,
  onApprove,
  onReject,
  isProcessing = false,
  queuePosition,
  queueTotal,
}: ApprovalModalProps) {
  const [showConfirm, setShowConfirm] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const approvalLabelId = useId();
  useFocusTrap(containerRef, true);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isProcessing && approval.approval_id) {
        onReject(approval.approval_id);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isProcessing, approval.approval_id, onReject]);

  const tier = resolveTier(approval.danger_level);
  const isDestructive = tier === 'destructive';
  const colors = TIER_COLORS[approval.danger_level.toLowerCase()] || TIER_COLORS.write;

  const operationId = (approval.tool_args?.operation_id as string) || approval.details.path || '';
  const method = approval.details.method?.toUpperCase();
  const methodColor = method ? METHOD_COLORS[method] || '' : '';
  const displayParams = getDisplayParams(approval.tool_args);
  const showQueue = queueTotal && queueTotal > 1;

  const handleApprove = async () => {
    if (isDestructive && !showConfirm) {
      setShowConfirm(true);
      return;
    }
    if (approval.approval_id) {
      await onApprove(approval.approval_id);
    }
  };

  const handleReject = async () => {
    if (approval.approval_id) {
      await onReject(approval.approval_id);
    }
  };

  const modal = (
    <AnimatePresence>
      <motion.div
        key="approval-backdrop"
        ref={containerRef}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center"
        role="dialog"
        aria-modal="true"
        aria-labelledby={approvalLabelId}
        data-testid="approval-modal-backdrop"
      >
        <motion.div
          key="approval-panel"
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ duration: 0.2, ease: 'easeOut' }}
          className="max-w-lg w-full mx-4 rounded-2xl bg-surface shadow-2xl overflow-hidden"
          data-testid="approval-modal"
        >
          {/* Color-coded header banner */}
          <div className={clsx('px-5 py-4 flex items-center gap-3', colors.bg)}>
            <AlertTriangle className={clsx('h-6 w-6 flex-shrink-0', colors.iconColor)} />
            <div className="flex-1 min-w-0">
              <h2 id={approvalLabelId} className={clsx('font-semibold text-base', colors.text)}>
                {colors.label}
              </h2>
              <p className="text-xs text-text-secondary mt-0.5 truncate">
                {approval.message}
              </p>
            </div>
            {showQueue && (
              <span className="px-2 py-1 text-xs font-medium text-text-secondary bg-black/20 rounded-lg whitespace-nowrap">
                {queuePosition} of {queueTotal}
              </span>
            )}
          </div>

          {/* Body */}
          <div className="px-5 py-4 space-y-3">
            {/* Operation ID */}
            {operationId && (
              <div className="flex items-start gap-3 p-3 rounded-lg bg-surface/50 border border-border">
                <Globe className="h-4 w-4 text-text-tertiary flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-text-tertiary font-medium mb-1 uppercase tracking-wider">Operation</div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {method && (
                      <span className={clsx(
                        'inline-flex items-center px-2 py-0.5 text-xs font-bold uppercase rounded border',
                        methodColor,
                      )}>
                        {method}
                      </span>
                    )}
                    <code className="px-2 py-1 bg-black/20 rounded text-sm font-mono text-text-primary break-all">
                      {operationId}
                    </code>
                  </div>
                </div>
              </div>
            )}

            {/* Connector / description */}
            {approval.details.description && (
              <div className="flex items-start gap-3 p-3 rounded-lg bg-surface/50 border border-border">
                <Server className="h-4 w-4 text-text-tertiary flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-text-tertiary font-medium mb-1 uppercase tracking-wider">Target</div>
                  <div className="text-sm text-text-primary">{approval.details.description}</div>
                </div>
              </div>
            )}

            {/* Trust tier badge */}
            <div className="flex items-center gap-2">
              <span className={clsx(
                'inline-flex items-center px-2 py-1 text-xs font-bold uppercase rounded border',
                colors.bg, colors.text, colors.border,
              )}>
                {tier.toUpperCase()}
              </span>
            </div>

            {/* Parameters (always expanded) */}
            {displayParams && (
              <div className="flex items-start gap-3 p-3 rounded-lg bg-surface/50 border border-border">
                <FileText className="h-4 w-4 text-text-tertiary flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-text-tertiary font-medium mb-1 uppercase tracking-wider">Parameters</div>
                  <pre className="text-xs bg-black/20 p-3 rounded border border-white/5 overflow-x-auto max-h-48 text-text-secondary font-mono scrollbar-hide">
                    {JSON.stringify(displayParams, null, 2)}
                  </pre>
                </div>
              </div>
            )}

            {/* Impact warning for destructive */}
            {approval.details.impact && isDestructive && (
              <div className={clsx(
                'flex items-start gap-2 p-3 rounded-lg border',
                colors.bg, colors.border,
              )}>
                <AlertTriangle className={clsx('h-4 w-4 mt-0.5 flex-shrink-0', colors.iconColor)} />
                <p className={clsx('text-sm', colors.text)}>{approval.details.impact}</p>
              </div>
            )}

            {/* Extra confirmation step for DESTRUCTIVE */}
            {isDestructive && showConfirm && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="p-3 bg-red-500/20 border border-red-500/50 rounded-lg"
              >
                <p className="text-sm text-red-300 font-medium">
                  This is a DESTRUCTIVE operation that may cause permanent data loss.
                </p>
                <p className="text-xs text-red-400 mt-1">Click Confirm to proceed.</p>
              </motion.div>
            )}
          </div>

          {/* Action buttons */}
          <div className="px-5 pb-5 flex items-center gap-3">
            {showConfirm ? (
              <>
                <button
                  onClick={handleApprove}
                  disabled={isProcessing}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-2 px-4 py-3',
                    'bg-red-600 text-white font-medium rounded-xl',
                    'hover:bg-red-500 focus:outline-none focus:ring-2 focus:ring-red-500/50',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                    'transition-all active:scale-[0.98]',
                  )}
                  data-testid="approval-confirm-button"
                >
                  {isProcessing ? 'Processing...' : (
                    <><CheckCircle className="h-4 w-4" /> Confirm</>
                  )}
                </button>
                <button
                  onClick={() => setShowConfirm(false)}
                  disabled={isProcessing}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-2 px-4 py-3',
                    'bg-surface hover:bg-surface-hover text-text-secondary font-medium',
                    'border border-border rounded-xl',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                    'transition-all active:scale-[0.98]',
                  )}
                >
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={handleApprove}
                  disabled={isProcessing}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-2 px-4 py-3',
                    isDestructive
                      ? 'bg-red-600 hover:bg-red-500 text-white focus:ring-red-500/50'
                      : 'bg-primary hover:bg-primary-hover text-white focus:ring-primary/50',
                    'font-medium rounded-xl',
                    'focus:outline-none focus:ring-2',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                    'transition-all active:scale-[0.98]',
                  )}
                  data-testid="approval-approve-button"
                >
                  {isProcessing ? 'Processing...' : (
                    <><CheckCircle className="h-4 w-4" /> Approve</>
                  )}
                </button>
                <button
                  onClick={handleReject}
                  disabled={isProcessing}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-2 px-4 py-3',
                    'bg-surface hover:bg-surface-hover text-text-secondary font-medium',
                    'border border-border rounded-xl',
                    'focus:outline-none focus:ring-2 focus:ring-border',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                    'transition-all active:scale-[0.98]',
                  )}
                  data-testid="approval-deny-button"
                >
                  <XCircle className="h-4 w-4" />
                  Deny
                </button>
              </>
            )}
          </div>

          {/* Approval ID */}
          {approval.approval_id && (
            <div className="px-5 pb-3 text-[10px] text-text-tertiary font-mono opacity-40 text-center">
              ID: {approval.approval_id}
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  return createPortal(modal, document.body);
}
