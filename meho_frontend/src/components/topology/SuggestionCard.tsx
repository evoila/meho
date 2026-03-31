// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SuggestionCard - Expandable SAME_AS correlation suggestion card (Phase 17 Plan 03)
 *
 * Shows:
 * - Entity names from both sides with confidence and match type
 * - Clickable header to expand/collapse inline detail view
 * - When expanded: EntityComparisonTable + LLM reasoning + approve/reject actions
 * - When collapsed: compact summary with action buttons
 */

import { useQuery } from '@tanstack/react-query';
import { Check, X, Sparkles, Link2, ChevronDown, ChevronRight, Brain, Loader2 } from 'lucide-react';
import { clsx } from 'clsx';
import { motion, AnimatePresence } from 'motion/react';

import { EntityComparisonTable } from './EntityComparisonTable';
import { fetchEntity, type SameAsSuggestion } from '../../lib/topologyApi';

interface SuggestionCardProps {
  suggestion: SameAsSuggestion;
  onApprove: () => void;
  onReject: () => void;
  onVerify: () => void;
  isLoading?: boolean;
  loadingAction?: 'approve' | 'reject' | 'verify';
  isExpanded?: boolean;
  onToggleExpand?: (id: string) => void;
}

/**
 * Get confidence color based on score
 */
function getConfidenceColor(confidence: number): string {
  if (confidence >= 0.9) return 'text-green-400';
  if (confidence >= 0.7) return 'text-yellow-400';
  return 'text-orange-400';
}

/**
 * Get confidence label
 */
function getConfidenceLabel(confidence: number): string {
  if (confidence >= 0.9) return 'High';
  if (confidence >= 0.7) return 'Medium';
  return 'Low';
}

/**
 * Format match type for display
 */
function formatMatchType(matchType: string): string {
  return matchType
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Parse LLM verification result defensively.
 * May be a JSON string, object, or null.
 */
function parseLlmResult(result: Record<string, unknown> | null | undefined): {
  reasoning: string | null;
  raw: Record<string, unknown> | null;
} {
  if (!result) return { reasoning: null, raw: null };

  let parsed: Record<string, unknown> = result;
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed as unknown as string) as Record<string, unknown>;
    } catch {
      return { reasoning: parsed as unknown as string, raw: null };
    }
  }

  const reasoning =
    typeof parsed === 'object' && 'reasoning' in parsed
      ? String(parsed.reasoning)
      : null;

  return { reasoning, raw: parsed };
}

export function SuggestionCard({
  suggestion,
  onApprove,
  onReject,
  onVerify,
  isLoading = false,
  loadingAction,
  isExpanded = false,
  onToggleExpand,
}: SuggestionCardProps) {
  const confidencePercent = Math.round(suggestion.confidence * 100);
  const confidenceColor = getConfidenceColor(suggestion.confidence);
  const confidenceLabel = getConfidenceLabel(suggestion.confidence);
  const showVerifyButton =
    suggestion.confidence < 0.9 && !suggestion.llm_verification_attempted;

  // Fetch full entity details only when expanded (lazy loading)
  const { data: entityA, isLoading: loadingA } = useQuery({
    queryKey: ['topology', 'entity', suggestion.entity_a_id],
    queryFn: () => fetchEntity(suggestion.entity_a_id),
    enabled: isExpanded,
  });
  const { data: entityB, isLoading: loadingB } = useQuery({
    queryKey: ['topology', 'entity', suggestion.entity_b_id],
    queryFn: () => fetchEntity(suggestion.entity_b_id),
    enabled: isExpanded,
  });

  const entitiesLoading = loadingA || loadingB;

  // Parse LLM result
  const llmResult = parseLlmResult(suggestion.llm_verification_result);
  const hasLlmReasoning =
    suggestion.llm_verification_attempted && (llmResult.reasoning || llmResult.raw);

  const handleHeaderClick = () => {
    onToggleExpand?.(suggestion.id);
  };

  return (
    <div
      className={clsx(
        'bg-gray-800/80 border rounded-lg transition-colors',
        isExpanded
          ? 'border-amber-500/50'
          : 'border-gray-700 hover:border-gray-600'
      )}
    >
      {/* Clickable Header */}
      <div
        className="p-4 cursor-pointer select-none"
        onClick={handleHeaderClick}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            handleHeaderClick();
          }
        }}
      >
        {/* Top Row: Label + Confidence */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-amber-400" />
            ) : (
              <ChevronRight className="w-4 h-4 text-amber-400" />
            )}
            <Link2 className="w-4 h-4 text-amber-400" />
            <span className="text-xs font-medium text-amber-400 uppercase tracking-wide">
              SAME_AS Suggestion
            </span>
          </div>
          <div className={clsx('text-xs font-medium', confidenceColor)}>
            {confidencePercent}% ({confidenceLabel})
          </div>
        </div>

        {/* Entity A */}
        <div className="mb-2">
          <div
            className="text-sm font-semibold text-white truncate"
            title={suggestion.entity_a_name}
          >
            {suggestion.entity_a_name}
          </div>
          {suggestion.entity_a_connector_name && (
            <div className="text-xs text-gray-400 mt-0.5">
              via {suggestion.entity_a_connector_name}
            </div>
          )}
        </div>

        {/* Connector Line */}
        <div className="flex items-center gap-2 my-2 text-gray-500">
          <div className="flex-1 h-px bg-gray-700" />
          <span className="text-xs">{'\u2194'}</span>
          <div className="flex-1 h-px bg-gray-700" />
        </div>

        {/* Entity B */}
        <div className="mb-3">
          <div
            className="text-sm font-semibold text-white truncate"
            title={suggestion.entity_b_name}
          >
            {suggestion.entity_b_name}
          </div>
          {suggestion.entity_b_connector_name && (
            <div className="text-xs text-gray-400 mt-0.5">
              via {suggestion.entity_b_connector_name}
            </div>
          )}
        </div>

        {/* Match Type Badge */}
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <span className="px-2 py-0.5 bg-gray-700/50 rounded">
            {formatMatchType(suggestion.match_type)}
          </span>
          {suggestion.llm_verification_attempted && (
            <span className="flex items-center gap-1 px-2 py-0.5 bg-purple-900/30 text-purple-400 rounded">
              <Sparkles className="w-3 h-3" />
              LLM Verified
            </span>
          )}
        </div>
      </div>

      {/* Expanded Content */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 space-y-3">
              {/* Divider */}
              <div className="h-px bg-gray-700" />

              {/* Loading state for entity details */}
              {entitiesLoading && (
                <div className="flex items-center justify-center py-4 text-gray-400">
                  <Loader2 className="w-5 h-5 animate-spin mr-2" />
                  <span className="text-xs">Loading entity details...</span>
                </div>
              )}

              {/* Entity Comparison Table */}
              {entityA && entityB && (
                <EntityComparisonTable
                  entityA={entityA}
                  entityB={entityB}
                  matchDetails={suggestion.match_details}
                />
              )}

              {/* LLM Reasoning Section -- the "holy shit" moment */}
              {hasLlmReasoning && (
                <div className="border-l-4 border-purple-500 bg-purple-500/10 p-3 rounded-r">
                  <div className="flex items-center gap-1.5 mb-2">
                    <Brain className="w-4 h-4 text-purple-400" />
                    <span className="text-sm font-semibold text-purple-300">
                      AI Analysis
                    </span>
                  </div>
                  <div className="text-sm text-gray-200 leading-relaxed whitespace-pre-wrap">
                    {llmResult.reasoning
                      ? llmResult.reasoning
                      : JSON.stringify(llmResult.raw, null, 2)}
                  </div>
                </div>
              )}

              {/* Action Buttons */}
              <div className="flex items-center gap-2 pt-1">
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onApprove();
                  }}
                  disabled={isLoading}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                    'bg-green-600/20 text-green-400 border border-green-500/30',
                    'hover:bg-green-600/30 hover:border-green-500/50',
                    'disabled:opacity-50 disabled:cursor-not-allowed'
                  )}
                >
                  {loadingAction === 'approve' ? (
                    <div className="w-4 h-4 border-2 border-green-400/30 border-t-green-400 rounded-full animate-spin" />
                  ) : (
                    <Check className="w-4 h-4" />
                  )}
                  Approve
                </button>

                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onReject();
                  }}
                  disabled={isLoading}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                    'bg-red-600/20 text-red-400 border border-red-500/30',
                    'hover:bg-red-600/30 hover:border-red-500/50',
                    'disabled:opacity-50 disabled:cursor-not-allowed'
                  )}
                >
                  {loadingAction === 'reject' ? (
                    <div className="w-4 h-4 border-2 border-red-400/30 border-t-red-400 rounded-full animate-spin" />
                  ) : (
                    <X className="w-4 h-4" />
                  )}
                  Reject
                </button>

                {showVerifyButton && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onVerify();
                    }}
                    disabled={isLoading}
                    className={clsx(
                      'flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                      'bg-purple-600/20 text-purple-400 border border-purple-500/30',
                      'hover:bg-purple-600/30 hover:border-purple-500/50',
                      'disabled:opacity-50 disabled:cursor-not-allowed'
                    )}
                    title="Use LLM to verify this suggestion"
                  >
                    {loadingAction === 'verify' ? (
                      <div className="w-4 h-4 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
                    ) : (
                      <Sparkles className="w-4 h-4" />
                    )}
                  </button>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Collapsed Action Buttons */}
      {!isExpanded && (
        <div className="px-4 pb-4">
          <div className="flex items-center gap-2">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onApprove();
              }}
              disabled={isLoading}
              className={clsx(
                'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                'bg-green-600/20 text-green-400 border border-green-500/30',
                'hover:bg-green-600/30 hover:border-green-500/50',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            >
              {loadingAction === 'approve' ? (
                <div className="w-4 h-4 border-2 border-green-400/30 border-t-green-400 rounded-full animate-spin" />
              ) : (
                <Check className="w-4 h-4" />
              )}
              Approve
            </button>

            <button
              onClick={(e) => {
                e.stopPropagation();
                onReject();
              }}
              disabled={isLoading}
              className={clsx(
                'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                'bg-red-600/20 text-red-400 border border-red-500/30',
                'hover:bg-red-600/30 hover:border-red-500/50',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            >
              {loadingAction === 'reject' ? (
                <div className="w-4 h-4 border-2 border-red-400/30 border-t-red-400 rounded-full animate-spin" />
              ) : (
                <X className="w-4 h-4" />
              )}
              Reject
            </button>

            {showVerifyButton && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onVerify();
                }}
                disabled={isLoading}
                className={clsx(
                  'flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                  'bg-purple-600/20 text-purple-400 border border-purple-500/30',
                  'hover:bg-purple-600/30 hover:border-purple-500/50',
                  'disabled:opacity-50 disabled:cursor-not-allowed'
                )}
                title="Use LLM to verify this suggestion"
              >
                {loadingAction === 'verify' ? (
                  <div className="w-4 h-4 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
                ) : (
                  <Sparkles className="w-4 h-4" />
                )}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
