// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Session Status Indicator
 *
 * Phase 38: Group Session Foundation
 * Shows visual status for team sessions:
 * - awaiting_approval: bold amber pill badge (demands attention)
 * - active: pulsing green dot (agent running)
 * - idle: nothing (reduces noise)
 */

interface SessionStatusIndicatorProps {
  status: 'awaiting_approval' | 'active' | 'idle';
}

export function SessionStatusIndicator({ status }: SessionStatusIndicatorProps) {
  if (status === 'idle') {
    return null;
  }

  if (status === 'awaiting_approval') {
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide bg-amber-500/20 text-amber-400 border border-amber-500/30">
        Awaiting Approval
      </span>
    );
  }

  if (status === 'active') {
    return (
      <span className="relative flex h-2.5 w-2.5">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-green-500" />
      </span>
    );
  }

  return null;
}
