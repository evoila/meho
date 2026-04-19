// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Passthrough Badge Component (03.1-02)
 *
 * Subtle badge shown above single-connector passthrough messages.
 * Indicates the response came directly from one connector without
 * multi-connector synthesis. Uses emerald color treatment consistent
 * with connector success badges in AgentPane.
 */
import { ArrowRight } from 'lucide-react';

interface PassthroughBadgeProps {
  connectorName: string;
}

export function PassthroughBadge({ connectorName }: Readonly<PassthroughBadgeProps>) {
  return (
    <span className="text-xs px-2.5 py-1 rounded-md bg-emerald-900/30 text-emerald-400 border border-emerald-800/40 inline-flex items-center gap-1.5 mb-2">
      <ArrowRight className="w-3 h-3" />
      Direct from {connectorName}
    </span>
  );
}
