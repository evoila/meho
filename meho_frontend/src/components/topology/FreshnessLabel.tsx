// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * FreshnessLabel - Color-coded relative timestamp with absolute tooltip (D-07)
 *
 * Shows relative time ("just now", "5m ago", "2h ago", "3d ago") with
 * color coding: green (<24h fresh), amber (1-7d aging), red (>7d stale).
 * Hover tooltip shows full ISO timestamp.
 */

import { useMemo } from 'react';
import { clsx } from 'clsx';

interface FreshnessLabelProps {
  timestamp: string | null;
  className?: string;
}

export function FreshnessLabel({ timestamp, className }: FreshnessLabelProps) {
  const { relative, absolute, freshness } = useMemo(() => {
    if (!timestamp) return { relative: 'Unknown', absolute: '', freshness: 'unknown' as const };

    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHr = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHr / 24);

    let relative: string;
    if (diffSec < 60) relative = 'just now';
    else if (diffMin < 60) relative = `${diffMin}m ago`;
    else if (diffHr < 24) relative = `${diffHr}h ago`;
    else relative = `${diffDay}d ago`;

    const freshness = diffHr < 24 ? 'fresh' : diffDay <= 7 ? 'aging' : 'stale';

    return { relative, absolute: date.toISOString(), freshness };
  }, [timestamp]);

  return (
    <span
      className={clsx(
        'text-xs',
        freshness === 'fresh' && 'text-emerald-400',
        freshness === 'aging' && 'text-amber-400',
        freshness === 'stale' && 'text-red-400',
        freshness === 'unknown' && 'text-zinc-500',
        className,
      )}
      title={absolute || 'No timestamp available'}
    >
      {relative}
    </span>
  );
}
