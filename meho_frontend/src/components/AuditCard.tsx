// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Audit Card Component (Phase 5: Graduated Trust Model)
 *
 * Compact inline card for the chat flow showing audit trail entries after
 * approval/denial of WRITE or DESTRUCTIVE operations. Displays decision,
 * outcome status, trust tier badge, and collapsible details.
 */
import { useState } from 'react';
import { motion } from 'motion/react';
import { CheckCircle, XCircle, ChevronDown, ChevronRight } from 'lucide-react';
import clsx from 'clsx';

export interface AuditEntry {
  approval_id: string | null;
  tool: string;
  trust_tier: string; // "write" or "destructive"
  decision: string; // "approved" or "denied"
  outcome_status: string; // "success", "failure", "skipped"
  outcome_summary: string;
  connector_name: string;
  timestamp: string;
  user_id?: string;  // Phase 7.1: authenticated user attribution
}

interface AuditCardProps {
  entry: AuditEntry;
}

// Tier badge colors
const TIER_BADGE: Record<string, { bg: string; text: string; border: string }> = {
  write: {
    bg: 'bg-yellow-500/10',
    text: 'text-yellow-400',
    border: 'border-yellow-500/30',
  },
  destructive: {
    bg: 'bg-red-500/10',
    text: 'text-red-400',
    border: 'border-red-500/30',
  },
};

// Compute relative time (simple implementation)
function relativeTime(timestamp: string): string {
  const now = Date.now();
  const then = new Date(timestamp).getTime();
  const diffMs = now - then;
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 10) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  return new Date(timestamp).toLocaleDateString();
}

// Determine display state from decision + outcome
function resolveStatus(decision: string, outcomeStatus: string) {
  if (decision === 'denied') {
    return {
      icon: XCircle,
      iconColor: 'text-red-400',
      bgTint: 'bg-red-500/5 border-red-500/20',
      label: 'Denied',
    };
  }
  if (outcomeStatus === 'success') {
    return {
      icon: CheckCircle,
      iconColor: 'text-green-400',
      bgTint: 'bg-green-500/5 border-green-500/20',
      label: 'Approved',
    };
  }
  // approved + failure
  return {
    icon: XCircle,
    iconColor: 'text-red-400',
    bgTint: 'bg-red-500/5 border-red-500/20',
    label: 'Approved',
  };
}

export function AuditCard({ entry }: AuditCardProps) {
  const [expanded, setExpanded] = useState(false);

  const status = resolveStatus(entry.decision, entry.outcome_status);
  const StatusIcon = status.icon;
  const tierBadge = TIER_BADGE[entry.trust_tier.toLowerCase()] || TIER_BADGE.write;

  const summaryLine = `${status.label} ${entry.tool} on ${entry.connector_name}${entry.outcome_summary ? ` - ${entry.outcome_summary}` : ''}`;

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className={clsx(
        'my-2 rounded-lg border overflow-hidden',
        status.bgTint
      )}
      data-testid="audit-card"
    >
      {/* Main row */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        <StatusIcon className={clsx('h-4 w-4 flex-shrink-0', status.iconColor)} />

        <span className="flex-1 text-sm text-text-primary truncate">{summaryLine}</span>

        {/* Tier badge */}
        <span
          className={clsx(
            'inline-flex items-center px-1.5 py-0.5 text-[10px] font-bold uppercase rounded border flex-shrink-0',
            tierBadge.bg,
            tierBadge.text,
            tierBadge.border
          )}
        >
          {entry.trust_tier.toUpperCase()}
        </span>

        {/* Timestamp */}
        <span className="text-[10px] text-text-tertiary flex-shrink-0 ml-1">
          {relativeTime(entry.timestamp)}
        </span>

        {/* Expand chevron */}
        {expanded ? (
          <ChevronDown className="h-3 w-3 text-text-tertiary flex-shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 text-text-tertiary flex-shrink-0" />
        )}
      </button>

      {/* Collapsible details */}
      {expanded && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
          className="px-3 pb-3 space-y-1"
        >
          <div className="text-xs text-text-secondary">
            <span className="font-medium">Tool:</span> {entry.tool}
          </div>
          <div className="text-xs text-text-secondary">
            <span className="font-medium">Connector:</span> {entry.connector_name}
          </div>
          <div className="text-xs text-text-secondary">
            <span className="font-medium">Decision:</span>{' '}
            <span className={entry.decision === 'approved' ? 'text-green-400' : 'text-red-400'}>
              {entry.decision}
            </span>
          </div>
          <div className="text-xs text-text-secondary">
            <span className="font-medium">Outcome:</span>{' '}
            <span
              className={
                entry.outcome_status === 'success' ? 'text-green-400' : 'text-red-400'
              }
            >
              {entry.outcome_status}
            </span>
          </div>
          {entry.outcome_summary && (
            <div className="text-xs text-text-secondary">
              <span className="font-medium">Summary:</span> {entry.outcome_summary}
            </div>
          )}
          {entry.user_id && (
            <div className="text-xs text-text-secondary">
              <span className="font-medium">User:</span> {entry.user_id}
            </div>
          )}
          {entry.approval_id && (
            <div className="text-[10px] text-text-tertiary font-mono opacity-40 mt-1">
              ID: {entry.approval_id}
            </div>
          )}
        </motion.div>
      )}
    </motion.div>
  );
}
