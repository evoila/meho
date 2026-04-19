// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConfidenceBadge - SAME_AS confidence pill badge (D-08)
 *
 * Parses verifiedVia evidence to determine confidence level:
 * - CONFIRMED (green): providerID match
 * - HIGH (blue): IP address match
 * - MEDIUM (amber): hostname match
 * - SUGGESTED (zinc): embedding similarity or unknown
 *
 * Shows confidence level as pill badge with evidence in tooltip.
 */

import { clsx } from 'clsx';

type ConfidenceLevel = 'CONFIRMED' | 'HIGH' | 'MEDIUM' | 'SUGGESTED';

interface ConfidenceBadgeProps {
  verifiedVia: string[];
  similarityScore?: number;
  className?: string;
}

function parseConfidence(verifiedVia: string[]): { level: ConfidenceLevel; evidence: string } {
  const joined = verifiedVia.join(' ').toLowerCase();

  if (joined.includes('providerid') || joined.includes('provider_id')) {
    return { level: 'CONFIRMED', evidence: verifiedVia.find(v => v.toLowerCase().includes('provider')) || verifiedVia[0] || '' };
  }
  if (joined.includes('ip') && (joined.includes('match') || joined.includes('address') || /\d+\.\d+\.\d+\.\d+/.test(joined))) {
    return { level: 'HIGH', evidence: verifiedVia.find(v => v.toLowerCase().includes('ip')) || verifiedVia[0] || '' };
  }
  if (joined.includes('hostname')) {
    return { level: 'MEDIUM', evidence: verifiedVia.find(v => v.toLowerCase().includes('hostname')) || verifiedVia[0] || '' };
  }
  return { level: 'SUGGESTED', evidence: verifiedVia[0] || 'embedding similarity' };
}

export function ConfidenceBadge({ verifiedVia, similarityScore, className }: Readonly<ConfidenceBadgeProps>) {
  const { level, evidence } = parseConfidence(verifiedVia);

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded-full font-medium',
        level === 'CONFIRMED' && 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
        level === 'HIGH' && 'bg-blue-500/15 text-blue-400 border border-blue-500/30',
        level === 'MEDIUM' && 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
        level === 'SUGGESTED' && 'bg-zinc-500/15 text-zinc-400 border border-zinc-500/30',
        className,
      )}
      title={`${level} -- ${evidence}${similarityScore ? ` (similarity: ${(similarityScore * 100).toFixed(0)}%)` : ''}`}
    >
      {level}
    </span>
  );
}
