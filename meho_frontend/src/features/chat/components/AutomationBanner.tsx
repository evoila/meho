// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * AutomationBanner
 *
 * Informational banner displayed in ChatHeader when viewing an automated session
 * (triggered by event or scheduled task). Shows trigger source and trust model summary.
 *
 * Phase 75: CRED-11
 */
import { Bot } from 'lucide-react';

interface AutomationBannerProps {
  triggerSource: string;
}

export function AutomationBanner({ triggerSource }: Readonly<AutomationBannerProps>) {
  return (
    <div className="flex items-center gap-2 px-4 py-2 bg-blue-500/10 border-b border-blue-500/20 text-sm">
      <Bot className="h-4 w-4 text-blue-400 shrink-0" />
      <span className="text-blue-200">
        Triggered by{' '}
        <span className="font-semibold text-blue-100">{triggerSource}</span>
      </span>
      <span className="text-blue-300/60">|</span>
      <span className="text-blue-300/80 text-xs">
        READ operations auto-approved, WRITE/DESTRUCTIVE require approval
      </span>
    </div>
  );
}
