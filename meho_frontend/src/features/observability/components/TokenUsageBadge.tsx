// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TokenUsageBadge Component
 *
 * Displays token usage metrics with optional cost.
 * Expandable to show prompt/completion breakdown.
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight, Coins, Zap } from 'lucide-react';
import { cn } from '@/shared';
import type { TokenUsage } from '@/api/types';

export interface TokenUsageBadgeProps {
  /** Token usage data */
  usage: TokenUsage;
  /** Effective token count (after cache savings). Shown instead of total when provided. */
  effectiveTokens?: number;
  /** Size variant */
  size?: 'sm' | 'md';
  /** Whether to show expanded breakdown by default */
  defaultExpanded?: boolean;
  /** Additional CSS classes */
  className?: string;
}

/**
 * Format number with thousands separator.
 */
function formatNumber(num: number): string {
  return num.toLocaleString();
}

/**
 * Format cost in USD.
 */
function formatCost(cost: number): string {
  if (cost < 0.01) {
    return `$${cost.toFixed(4)}`;
  }
  return `$${cost.toFixed(2)}`;
}

/**
 * Compact display of token usage with optional cost.
 * Click to expand and see prompt/completion breakdown.
 *
 * @example
 * ```tsx
 * <TokenUsageBadge
 *   usage={{
 *     prompt_tokens: 1500,
 *     completion_tokens: 500,
 *     total_tokens: 2000,
 *     estimated_cost_usd: 0.006
 *   }}
 * />
 * ```
 */
export function TokenUsageBadge({
  usage,
  effectiveTokens,
  size = 'md',
  defaultExpanded = false,
  className,
}: TokenUsageBadgeProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  const textSize = size === 'sm' ? 'text-xs' : 'text-sm';
  const iconSize = size === 'sm' ? 'w-3 h-3' : 'w-3.5 h-3.5';
  const padding = size === 'sm' ? 'px-2 py-0.5' : 'px-2.5 py-1';

  const displayTokens = effectiveTokens ?? usage.total_tokens;
  const hasCacheSavings = effectiveTokens != null && effectiveTokens < usage.total_tokens;

  return (
    <div className={cn('inline-flex flex-col', className)}>
      {/* Main badge */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-lg',
          'bg-primary/10 text-primary border border-primary/20',
          'hover:bg-primary/20 transition-colors',
          padding,
          textSize
        )}
      >
        <Zap className={iconSize} />
        <span className="font-medium">{formatNumber(displayTokens)} tokens</span>
        {usage.estimated_cost_usd !== null && (
          <>
            <span className="text-text-tertiary">•</span>
            <Coins className={cn(iconSize, 'text-amber-400')} />
            <span className="text-amber-400">{formatCost(usage.estimated_cost_usd)}</span>
          </>
        )}
        {isExpanded ? (
          <ChevronDown className={cn(iconSize, 'text-text-tertiary ml-1')} />
        ) : (
          <ChevronRight className={cn(iconSize, 'text-text-tertiary ml-1')} />
        )}
      </button>

      {/* Expanded breakdown */}
      {isExpanded && (
        <div
          className={cn(
            'mt-1.5 rounded-lg bg-surface border border-border p-2',
            'text-xs space-y-1'
          )}
        >
          <div className="flex justify-between">
            <span className="text-text-tertiary">Prompt:</span>
            <span className="text-text-secondary font-mono">
              {formatNumber(usage.prompt_tokens)}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-text-tertiary">Completion:</span>
            <span className="text-text-secondary font-mono">
              {formatNumber(usage.completion_tokens)}
            </span>
          </div>
          {hasCacheSavings && (
            <>
              <div className="flex justify-between pt-1 border-t border-border/50">
                <span className="text-text-tertiary">Gross tokens:</span>
                <span className="text-text-secondary font-mono">
                  {formatNumber(usage.total_tokens)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-tertiary">Cache savings:</span>
                <span className="text-emerald-400 font-mono">
                  −{Math.round((1 - displayTokens / usage.total_tokens) * 100)}%
                </span>
              </div>
            </>
          )}
          <div className="flex justify-between pt-1 border-t border-border/50">
            <span className="text-text-tertiary">Effective:</span>
            <span className="text-primary font-mono font-medium">
              {formatNumber(displayTokens)}
            </span>
          </div>
          {usage.estimated_cost_usd !== null && (
            <div className="flex justify-between">
              <span className="text-text-tertiary">Est. Cost:</span>
              <span className="text-amber-400 font-mono">
                {formatCost(usage.estimated_cost_usd)}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
