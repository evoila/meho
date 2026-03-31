// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Investigation Plan Component (Phase 99)
 *
 * Renders the orchestrator's investigation plan before specialist dispatch.
 * Shows classification (quick/standard/deep), reasoning, strategy, and planned systems.
 * Collapsible card that appears above connector cards in OrchestratorProgress.
 */
import { useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { Zap, Search, Brain, ChevronDown, ChevronRight } from 'lucide-react';
import clsx from 'clsx';
import type { InvestigationPlan as InvestigationPlanType } from '../stores/slices/orchestratorSlice';

interface InvestigationPlanProps {
  plan: InvestigationPlanType;
}

const CLASSIFICATION_CONFIG = {
  quick: {
    icon: Zap,
    label: 'Quick check',
    color: 'text-emerald-400',
    bgColor: 'bg-emerald-500/10',
    borderColor: 'border-emerald-500/20',
  },
  standard: {
    icon: Search,
    label: 'Investigating',
    color: 'text-blue-400',
    bgColor: 'bg-blue-500/10',
    borderColor: 'border-blue-500/20',
  },
  deep: {
    icon: Brain,
    label: 'Deep analysis',
    color: 'text-purple-400',
    bgColor: 'bg-purple-500/10',
    borderColor: 'border-purple-500/20',
  },
} as const;

export function InvestigationPlan({ plan }: InvestigationPlanProps) {
  const [expanded, setExpanded] = useState(true);
  const config = CLASSIFICATION_CONFIG[plan.classification] ?? CLASSIFICATION_CONFIG.standard;
  const Icon = config.icon;

  const systemCount = plan.plannedSystems.length;
  const conditionalCount = plan.plannedSystems.filter(s => s.conditional).length;
  const systemLabel = conditionalCount > 0
    ? `${systemCount - conditionalCount}\u2192${systemCount} systems`
    : `${systemCount} system${systemCount !== 1 ? 's' : ''}`;

  return (
    <motion.div
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className={clsx(
        'rounded-lg border p-3 mb-3',
        config.bgColor,
        config.borderColor,
      )}
      role="status"
      aria-label={`${config.label}: ${plan.reasoning}`}
    >
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 w-full text-left"
        aria-expanded={expanded}
      >
        <Icon className={clsx('w-4 h-4 flex-shrink-0', config.color)} />
        <span className={clsx('text-sm font-medium', config.color)}>
          {config.label}
        </span>
        <span className="text-xs text-zinc-400 ml-auto flex items-center gap-2">
          {systemLabel} &middot; ~{plan.estimatedCalls} calls
          {expanded ? (
            <ChevronDown className="w-3 h-3" />
          ) : (
            <ChevronRight className="w-3 h-3" />
          )}
        </span>
      </button>

      {/* Expandable body */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            {/* Reasoning */}
            <p className="text-sm text-zinc-300 mt-2">
              {plan.reasoning}
            </p>

            {/* Planned systems */}
            {plan.plannedSystems.length > 0 && (
              <div className="mt-2 space-y-1">
                {plan.plannedSystems.map((sys, idx) => (
                  <div
                    key={sys.id}
                    className="flex items-center gap-2 text-xs text-zinc-400"
                  >
                    <span className="text-zinc-500">
                      {idx + 1}.
                    </span>
                    <span className="text-zinc-300">{sys.name}</span>
                    <span className="text-zinc-500">&mdash;</span>
                    <span>{sys.reason}</span>
                    {sys.conditional && (
                      <span className="text-amber-400/70 text-[10px] uppercase tracking-wide">
                        if needed
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
