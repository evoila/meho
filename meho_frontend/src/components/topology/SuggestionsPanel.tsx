// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SuggestionsPanel - Full-width SAME_AS suggestions tab (Phase 76 Plan 05)
 *
 * Adapted for full-width tab content. Card grid layout (2 columns on lg).
 * Each card: Entity A vs Entity B with confidence percentage, match type badge,
 * "Approve Match" (green) and "Dismiss Match" buttons.
 * Empty state per UI-SPEC.
 */

import { useState, useMemo, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Link2,
  RefreshCw,
  AlertCircle,
  Search,
  CheckCheck,
  Check,
  X as XIcon,
  ArrowLeftRight,
} from 'lucide-react';
import { clsx } from 'clsx';
import { toast } from 'sonner';

import {
  fetchSuggestions,
  approveSuggestion,
  rejectSuggestion,
  triggerDiscovery,
  type SameAsSuggestion,
  type DiscoveryResponse,
} from '../../lib/topologyApi';

function formatMatchType(matchType: string): string {
  return matchType
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function getConfidenceColor(confidence: number): string {
  if (confidence >= 0.9) return 'text-emerald-400';
  if (confidence >= 0.7) return 'text-amber-400';
  return 'text-orange-400';
}

function getConfidenceBg(confidence: number): string {
  if (confidence >= 0.9) return 'bg-emerald-500/10 border-emerald-500/20';
  if (confidence >= 0.7) return 'bg-amber-500/10 border-amber-500/20';
  return 'bg-orange-500/10 border-orange-500/20';
}

export function SuggestionsPanel() {
  const [loadingStates, setLoadingStates] = useState<
    Record<string, 'approve' | 'reject'>
  >({});
  const [discoveryResult, setDiscoveryResult] =
    useState<DiscoveryResponse | null>(null);

  const queryClient = useQueryClient();

  // Fetch suggestions
  const {
    data: suggestionsData,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['topology', 'suggestions'],
    queryFn: () => fetchSuggestions({ limit: 100 }),
    refetchInterval: 60000,
  });

  const suggestions = useMemo(
    () => suggestionsData?.suggestions ?? [],
    [suggestionsData?.suggestions],
  );

  // High-confidence suggestions for bulk approve
  const highConfidenceSuggestions = suggestions.filter(
    (s) => s.confidence >= 0.9,
  );

  // Approve mutation
  const approveMutation = useMutation({
    mutationFn: approveSuggestion,
    onMutate: (suggestionId) => {
      setLoadingStates((prev) => ({ ...prev, [suggestionId]: 'approve' }));
    },
    onSuccess: (_data, suggestionId) => {
      const suggestion = suggestions.find((s) => s.id === suggestionId);
      if (suggestion) {
        toast.success(
          `Match approved: ${suggestion.entity_a_name} linked to ${suggestion.entity_b_name}`,
        );
      }
      queryClient.invalidateQueries({ queryKey: ['topology'] });
    },
    onSettled: (_, __, suggestionId) => {
      setLoadingStates((prev) => {
        const { [suggestionId]: _, ...rest } = prev;
        return rest;
      });
    },
  });

  // Reject mutation
  const rejectMutation = useMutation({
    mutationFn: rejectSuggestion,
    onMutate: (suggestionId) => {
      setLoadingStates((prev) => ({ ...prev, [suggestionId]: 'reject' }));
    },
    onSuccess: (_data, suggestionId) => {
      const suggestion = suggestions.find((s) => s.id === suggestionId);
      if (suggestion) {
        toast('Match dismissed', {
          description: `${suggestion.entity_a_name} / ${suggestion.entity_b_name}`,
        });
      }
      queryClient.invalidateQueries({ queryKey: ['topology'] });
    },
    onSettled: (_, __, suggestionId) => {
      setLoadingStates((prev) => {
        const { [suggestionId]: _, ...rest } = prev;
        return rest;
      });
    },
  });

  // Bulk approve mutation
  const bulkApproveMutation = useMutation({
    mutationFn: async (suggestionIds: string[]) => {
      for (const id of suggestionIds) {
        await approveSuggestion(id);
      }
      return suggestionIds.length;
    },
    onSuccess: (count) => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      toast.success(`Approved ${count} high-confidence matches`);
    },
    onError: (err) => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      toast.error(
        err instanceof Error
          ? err.message
          : 'Failed to bulk approve suggestions',
      );
    },
  });

  // Discovery mutation
  const preDiscoverySuggestionIds = useRef<Set<string>>(new Set());
  const discoveryMutation = useMutation({
    mutationFn: () => {
      preDiscoverySuggestionIds.current = new Set(
        suggestions.map((s) => s.id),
      );
      return triggerDiscovery({ min_similarity: 0.7, limit: 50 });
    },
    onSuccess: (result) => {
      setDiscoveryResult(result);
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      if (result.suggestions_created > 0) {
        toast.success(
          `Found ${result.suggestions_created} new match${result.suggestions_created > 1 ? 'es' : ''}!`,
        );
      } else {
        toast('No new matches found');
      }
      setTimeout(() => setDiscoveryResult(null), 5000);
    },
    onError: () => {
      setDiscoveryResult(null);
      toast.error('Discovery scan failed');
    },
  });

  const handleApprove = (suggestion: SameAsSuggestion) => {
    approveMutation.mutate(suggestion.id);
  };

  const handleReject = (suggestion: SameAsSuggestion) => {
    rejectMutation.mutate(suggestion.id);
  };

  const handleBulkApprove = () => {
    const ids = highConfidenceSuggestions.map((s) => s.id);
    bulkApproveMutation.mutate(ids);
  };

  const handleDiscovery = () => {
    setDiscoveryResult(null);
    discoveryMutation.mutate();
  };

  // Loading state
  if (isLoading && suggestions.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center text-[--color-text-secondary]">
          <RefreshCw className="w-8 h-8 mx-auto mb-4 animate-spin" />
          <div>Loading suggestions...</div>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center text-red-400">
          <AlertCircle className="w-12 h-12 mx-auto mb-4" />
          <div className="text-lg font-medium">Failed to load suggestions</div>
          <div className="text-sm mt-2">
            {error instanceof Error ? error.message : 'Unknown error'}
          </div>
          <button
            onClick={() => refetch()}
            className="mt-4 px-4 py-2 bg-[--color-primary] text-white rounded-lg hover:bg-[--color-primary-hover]"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Empty state
  if (suggestions.length === 0) {
    return (
      <div className="flex-1 flex flex-col">
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center max-w-md">
            <Link2 className="w-12 h-12 text-[--color-text-tertiary] mx-auto mb-4" />
            <h3 className="text-base font-semibold text-[--color-text-primary] mb-2">
              No pending suggestions
            </h3>
            <p className="text-sm text-[--color-text-secondary]">
              MEHO suggests cross-system matches when it discovers entities that
              appear to represent the same resource. Suggestions appear here for
              your review.
            </p>
            <button
              onClick={handleDiscovery}
              disabled={discoveryMutation.isPending}
              className="mt-4 flex items-center gap-2 px-4 py-2 mx-auto text-sm font-medium rounded-lg bg-[--color-primary] text-white hover:bg-[--color-primary-hover] transition-colors disabled:opacity-50"
            >
              {discoveryMutation.isPending ? (
                <>
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  Scanning...
                </>
              ) : (
                <>
                  <Search className="w-4 h-4" />
                  Scan for Matches
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-6">
      {/* Header with actions */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-[--color-text-secondary] uppercase tracking-wide">
          Pending Suggestions ({suggestions.length})
        </h2>
        <div className="flex items-center gap-2">
          {/* Bulk approve */}
          {highConfidenceSuggestions.length > 1 && (
            <button
              onClick={handleBulkApprove}
              disabled={bulkApproveMutation.isPending}
              className="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg bg-emerald-600/20 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-600/30 transition-colors disabled:opacity-50"
            >
              {bulkApproveMutation.isPending ? (
                <>
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  Approving...
                </>
              ) : (
                <>
                  <CheckCheck className="w-4 h-4" />
                  Approve all high confidence (
                  {highConfidenceSuggestions.length})
                </>
              )}
            </button>
          )}

          {/* Discovery */}
          <button
            onClick={handleDiscovery}
            disabled={discoveryMutation.isPending}
            className="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg bg-[--color-surface] text-[--color-text-secondary] border border-[--color-border] hover:bg-[--color-surface-hover] transition-colors disabled:opacity-50"
          >
            {discoveryMutation.isPending ? (
              <>
                <RefreshCw className="w-4 h-4 animate-spin" />
                Scanning...
              </>
            ) : (
              <>
                <Search className="w-4 h-4" />
                Scan for Matches
              </>
            )}
          </button>

          {/* Refresh */}
          <button
            onClick={() => refetch()}
            className="p-2 text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-surface-hover] rounded-lg transition-colors"
            title="Refresh suggestions"
          >
            <RefreshCw
              className={clsx('w-4 h-4', isLoading && 'animate-spin')}
            />
          </button>
        </div>
      </div>

      {/* Discovery result feedback */}
      {discoveryResult && (
        <div
          className={clsx(
            'p-3 rounded-lg text-sm mb-4',
            discoveryResult.suggestions_created > 0
              ? 'bg-emerald-900/30 text-emerald-400 border border-emerald-500/30'
              : 'bg-[--color-surface] text-[--color-text-secondary] border border-[--color-border]',
          )}
        >
          {discoveryResult.suggestions_created > 0
            ? `Found ${discoveryResult.suggestions_created} new match${discoveryResult.suggestions_created > 1 ? 'es' : ''}!`
            : 'No new matches found'}
          <span className="text-xs opacity-75 ml-2">
            ({discoveryResult.total_pairs_analyzed} pairs analyzed)
          </span>
        </div>
      )}

      {/* Card grid */}
      <div className="flex-1 overflow-y-auto">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {suggestions.map((suggestion) => {
            const confidencePercent = Math.round(
              suggestion.confidence * 100,
            );
            const itemLoading = !!loadingStates[suggestion.id];

            return (
              <div
                key={suggestion.id}
                className={clsx(
                  'border rounded-lg p-4 transition-colors',
                  getConfidenceBg(suggestion.confidence),
                )}
              >
                {/* Entity A vs Entity B */}
                <div className="flex items-start gap-3 mb-3">
                  <div className="flex-1 min-w-0">
                    <div
                      className="text-sm font-semibold text-[--color-text-primary] truncate"
                      title={suggestion.entity_a_name}
                    >
                      {suggestion.entity_a_name}
                    </div>
                    {suggestion.entity_a_connector_name && (
                      <div className="text-xs text-[--color-text-secondary]">
                        {suggestion.entity_a_connector_name}
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col items-center flex-shrink-0">
                    <ArrowLeftRight className="w-4 h-4 text-[--color-text-tertiary]" />
                    <span
                      className={clsx(
                        'text-xs font-bold mt-0.5',
                        getConfidenceColor(suggestion.confidence),
                      )}
                    >
                      {confidencePercent}%
                    </span>
                  </div>

                  <div className="flex-1 min-w-0 text-right">
                    <div
                      className="text-sm font-semibold text-[--color-text-primary] truncate"
                      title={suggestion.entity_b_name}
                    >
                      {suggestion.entity_b_name}
                    </div>
                    {suggestion.entity_b_connector_name && (
                      <div className="text-xs text-[--color-text-secondary]">
                        {suggestion.entity_b_connector_name}
                      </div>
                    )}
                  </div>
                </div>

                {/* Match type badge */}
                <div className="mb-3">
                  <span className="inline-flex px-2 py-0.5 text-xs rounded bg-[--color-surface] text-[--color-text-secondary] border border-[--color-border]">
                    {formatMatchType(suggestion.match_type)}
                  </span>
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleApprove(suggestion)}
                    disabled={itemLoading}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium bg-emerald-600/20 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-600/30 transition-colors disabled:opacity-50"
                  >
                    {loadingStates[suggestion.id] === 'approve' ? (
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Check className="w-3.5 h-3.5" />
                    )}
                    Approve Match
                  </button>
                  <button
                    onClick={() => handleReject(suggestion)}
                    disabled={itemLoading}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium bg-[--color-surface] text-[--color-text-secondary] border border-[--color-border] hover:bg-[--color-surface-hover] transition-colors disabled:opacity-50"
                  >
                    {loadingStates[suggestion.id] === 'reject' ? (
                      <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <XIcon className="w-3.5 h-3.5" />
                    )}
                    Dismiss Match
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
