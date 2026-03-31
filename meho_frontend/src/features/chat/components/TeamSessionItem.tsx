// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Team Session Item
 *
 * Phase 38: Group Session Foundation
 * Renders a single team session in the sidebar Team tab.
 * Shows title, creator/trigger source, and status indicator.
 */
import { Users } from 'lucide-react';
import clsx from 'clsx';
import type { TeamSession } from '@/api/types/chat';
import { SessionStatusIndicator } from './SessionStatusIndicator';

interface TeamSessionItemProps {
  session: TeamSession;
  isActive: boolean;
  onClick: () => void;
}

export function TeamSessionItem({ session, isActive, onClick }: TeamSessionItemProps) {
  // Show creator name or trigger source
  const attribution = session.trigger_source
    ? `via ${session.trigger_source}`
    : session.created_by_name
      ? `by ${session.created_by_name}`
      : 'by Unknown';

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } }}
      className={clsx(
        'group relative flex items-center gap-3 px-3 py-3 rounded-xl cursor-pointer transition-all duration-200',
        isActive
          ? 'bg-primary/10 text-primary'
          : 'hover:bg-white/5 text-text-secondary hover:text-text-primary'
      )}
    >
      <Users
        className={clsx(
          'h-4 w-4 flex-shrink-0 transition-colors',
          isActive ? 'text-primary' : 'text-text-tertiary group-hover:text-text-secondary'
        )}
      />

      <div className="flex-1 min-w-0">
        <div
          className={clsx(
            'text-sm font-medium truncate transition-colors',
            isActive ? 'text-primary' : 'text-text-secondary group-hover:text-text-primary'
          )}
        >
          {session.title || 'Team Investigation'}
        </div>
        <div className="text-[10px] text-text-tertiary opacity-70 group-hover:opacity-100 transition-opacity truncate">
          {attribution}
        </div>
      </div>

      <div className="flex-shrink-0">
        <SessionStatusIndicator status={session.status} />
      </div>
    </div>
  );
}
