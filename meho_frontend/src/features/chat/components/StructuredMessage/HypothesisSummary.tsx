// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * HypothesisSummary (Phase 62)
 *
 * Inline hypothesis validation summary with colored status pills.
 * Shows validated/invalidated/inconclusive/investigating badges
 * using the Phase 60 dot color language.
 */

interface HypothesisSummaryProps {
  hypotheses: Array<{ text: string; status: string }>;
}

const STATUS_STYLES: Record<string, string> = {
  validated: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  invalidated: 'bg-red-500/20 text-red-400 border-red-500/30',
  inconclusive: 'bg-slate-500/20 text-slate-400 border-slate-500/30',
  investigating: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
};

export function HypothesisSummary({ hypotheses }: HypothesisSummaryProps) {
  if (!hypotheses || hypotheses.length === 0) return null;

  return (
    <div className="mt-3 p-3 rounded-lg bg-slate-800/40 border border-slate-700/50">
      <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wider mb-2">
        Investigation Hypotheses
      </div>
      <div className="space-y-1.5">
        {hypotheses.map((h, i) => {
          const statusStyle = STATUS_STYLES[h.status] || STATUS_STYLES.inconclusive;
          return (
            <div key={i} className="flex items-start gap-2">
              <span
                className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold border whitespace-nowrap ${statusStyle}`}
              >
                {h.status}
              </span>
              <span className="text-sm text-text-primary leading-snug">
                {h.text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
