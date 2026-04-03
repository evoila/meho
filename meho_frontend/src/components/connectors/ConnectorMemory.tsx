// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorMemory Component
 *
 * Memory tab content for ConnectorDetails. Displays connector memories
 * in a compact expandable list with type/confidence badges, filters,
 * search, inline edit, and inline delete confirmation.
 *
 * Phase 13 - Memory UI (read + write operations, not yet wired into ConnectorDetails).
 */

import { useState, useMemo, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Brain,
  Search,
  Trash2,
  Edit3,
  Save,
  X,
  Loader2,
  AlertCircle,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { MemoryResponse, MemoryUpdate, MemoryType } from '../../api/types';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TYPE_BADGE_STYLES: Record<string, string> = {
  entity: 'bg-blue-400/10 text-blue-400 border-blue-400/20',
  pattern: 'bg-purple-400/10 text-purple-400 border-purple-400/20',
  outcome: 'bg-green-400/10 text-green-400 border-green-400/20',
  config: 'bg-amber-400/10 text-amber-400 border-amber-400/20',
};

const CONFIDENCE_BADGE_STYLES: Record<string, string> = {
  operator: 'bg-primary/10 text-primary border-primary/20',
  confirmed_outcome: 'bg-green-400/10 text-green-300 border-green-400/20',
  auto_extracted: 'bg-slate-400/10 text-slate-400 border-slate-400/20',
};

const CONFIDENCE_LABELS: Record<string, string> = {
  operator: 'Operator',
  confirmed_outcome: 'Confirmed',
  auto_extracted: 'Auto',
};

const TYPE_FILTERS: Array<{ value: string | null; label: string }> = [
  { value: null, label: 'All' },
  { value: 'entity', label: 'Entity' },
  { value: 'pattern', label: 'Pattern' },
  { value: 'outcome', label: 'Outcome' },
  { value: 'config', label: 'Config' },
];

const CONFIDENCE_OPTIONS: Array<{ value: string | null; label: string }> = [
  { value: null, label: 'All Confidence' },
  { value: 'operator', label: 'Operator' },
  { value: 'confirmed_outcome', label: 'Confirmed' },
  { value: 'auto_extracted', label: 'Auto-extracted' },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  if (diffDay > 0) return rtf.format(-diffDay, 'day');
  if (diffHour > 0) return rtf.format(-diffHour, 'hour');
  if (diffMin > 0) return rtf.format(-diffMin, 'minute');
  return 'just now';
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatSourceType(sourceType: string): string {
  return sourceType
    .split('_')
    .map(capitalize)
    .join('-');
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ConnectorMemoryProps {
  connectorId: string;
  onCountChange?: (count: number) => void;
}

export function ConnectorMemory({ connectorId, onCountChange }: Readonly<ConnectorMemoryProps>) {
  // ----- Filter state -----
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  const [confidenceFilter, setConfidenceFilter] = useState<string | null>(null);
  const [searchText, setSearchText] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');

  // ----- UI state -----
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<Partial<MemoryUpdate>>({});
  const [showDeleteConfirm, setShowDeleteConfirm] = useState<string | null>(null);

  // ----- API setup -----
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // ----- Debounce search (300ms) -----
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchText);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchText]);

  // ----- Data fetching -----
  const {
    data: memories,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['connector-memories', connectorId, typeFilter, confidenceFilter],
    queryFn: () =>
      apiClient.listConnectorMemories(connectorId, {
        memory_type: typeFilter || undefined,
        confidence_level: confidenceFilter || undefined,
        limit: 200,
      }),
  });

  // ----- Notify parent of count -----
  useEffect(() => {
    if (memories) {
      onCountChange?.(memories.length);
    }
  }, [memories, onCountChange]);

  // ----- Client-side processing: sort by last_seen DESC, filter by search text -----
  const processedMemories = useMemo(() => {
    if (!memories) return [];

    let result = [...memories];

    // Sort by last_seen descending (overriding backend created_at sort)
    result.sort(
      (a, b) => new Date(b.last_seen).getTime() - new Date(a.last_seen).getTime()
    );

    // Client-side text search
    if (debouncedSearch) {
      const q = debouncedSearch.toLowerCase();
      result = result.filter(
        (m) =>
          m.title.toLowerCase().includes(q) ||
          m.body.toLowerCase().includes(q)
      );
    }

    return result;
  }, [memories, debouncedSearch]);

  // ----- Mutations -----
  const updateMutation = useMutation({
    mutationFn: ({ memoryId, updates }: { memoryId: string; updates: MemoryUpdate }) =>
      apiClient.updateConnectorMemory(connectorId, memoryId, updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-memories', connectorId] });
      setEditingId(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (memoryId: string) =>
      apiClient.deleteConnectorMemory(connectorId, memoryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-memories', connectorId] });
      setShowDeleteConfirm(null);
    },
  });

  // ----- Edit handlers -----
  const startEdit = (memory: MemoryResponse) => {
    setEditingId(memory.id);
    setEditForm({
      title: memory.title,
      body: memory.body,
      memory_type: memory.memory_type,
      tags: [...memory.tags],
    });
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditForm({});
  };

  const saveEdit = () => {
    if (!editingId) return;
    updateMutation.mutate({
      memoryId: editingId,
      updates: editForm as MemoryUpdate,
    });
  };

  // ----- Determine empty state context -----
  const hasNoMemories = !isLoading && !isError && memories && memories.length === 0;
  const hasNoFilters = !typeFilter && !confidenceFilter;
  const hasNoResults = processedMemories.length === 0 && !hasNoMemories;

  // ----- Loading state -----
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-8 w-8 text-primary animate-spin" />
        <span className="ml-3 text-text-secondary">Loading memories...</span>
      </div>
    );
  }

  // ----- Error state -----
  if (isError) {
    return (
      <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
        <AlertCircle className="h-5 w-5 flex-shrink-0" />
        <span>Failed to load memories: {(error as Error).message}</span>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="space-y-3">
        {/* Type filter pills */}
        <div className="flex items-center gap-2 flex-wrap">
          {TYPE_FILTERS.map((filter) => {
            const isActive = typeFilter === filter.value;
            const typeColor =
              filter.value && isActive
                ? TYPE_BADGE_STYLES[filter.value]
                : undefined;

            return (
              <button
                key={filter.label}
                onClick={() => setTypeFilter(filter.value)}
                className={clsx(
                  'px-3 py-1.5 text-xs font-medium rounded-lg border transition-all',
                  isActive && !filter.value &&
                    'bg-white/10 text-white border-white/20',
                  isActive && filter.value && typeColor,
                  !isActive &&
                    'bg-white/5 text-text-secondary border-white/10 hover:bg-white/10 hover:text-white'
                )}
              >
                {filter.label}
              </button>
            );
          })}
        </div>

        {/* Confidence dropdown + Search */}
        <div className="flex items-center gap-3">
          <select
            value={confidenceFilter || ''}
            onChange={(e) =>
              setConfidenceFilter(e.target.value || null)
            }
            className="bg-white/5 border border-white/10 text-text-secondary text-xs rounded-lg px-3 py-2 focus:outline-none focus:border-primary/50 appearance-none cursor-pointer"
          >
            {CONFIDENCE_OPTIONS.map((opt) => (
              <option key={opt.label} value={opt.value || ''}>
                {opt.label}
              </option>
            ))}
          </select>

          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
            <input
              type="text"
              placeholder="Search memories..."
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg pl-9 pr-3 py-2 placeholder:text-text-tertiary focus:outline-none focus:border-primary/50"
            />
          </div>
        </div>
      </div>

      {/* Empty state (no memories at all and no filters active) */}
      {hasNoMemories && hasNoFilters && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center py-16 bg-surface/50 border border-white/10 rounded-2xl"
        >
          <div className="w-16 h-16 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mx-auto mb-4">
            <Brain className="h-8 w-8 text-primary" />
          </div>
          <p className="text-white font-medium mb-2">No memories yet</p>
          <p className="text-sm text-text-secondary max-w-md mx-auto">
            Memories are created automatically from conversations or when you
            tell the agent to remember something.
          </p>
        </motion.div>
      )}

      {/* Empty state (filters active but no results) */}
      {hasNoMemories && !hasNoFilters && (
        <div className="text-center py-12 text-text-secondary text-sm">
          No memories match the selected filters.
        </div>
      )}

      {/* No search results */}
      {hasNoResults && (
        <div className="text-center py-12 text-text-secondary text-sm">
          No memories match &ldquo;{debouncedSearch}&rdquo;
        </div>
      )}

      {/* Memory list */}
      {processedMemories.length > 0 && (
        <div className="space-y-2">
          <AnimatePresence mode="popLayout">
            {processedMemories.map((memory) => {
              const isExpanded = expandedId === memory.id;
              const isEditing = editingId === memory.id;
              const isDeleteConfirm = showDeleteConfirm === memory.id;

              return (
                <motion.div
                  key={memory.id}
                  layout
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  className="border border-white/10 rounded-xl overflow-hidden"
                >
                  {/* Collapsed row */}
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => {
                      if (!isEditing) {
                        setExpandedId(isExpanded ? null : memory.id);
                      }
                    }}
                    onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !isEditing) { e.preventDefault(); setExpandedId(isExpanded ? null : memory.id); } }}
                    className={clsx(
                      'p-4 cursor-pointer hover:bg-white/5 transition-colors',
                      isExpanded && 'bg-white/[0.02]'
                    )}
                  >
                    {/* First line: type badge, title, confidence badge, relative time */}
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={clsx(
                          'text-xs font-medium px-2 py-0.5 rounded-md border flex-shrink-0',
                          TYPE_BADGE_STYLES[memory.memory_type] ||
                            'bg-white/5 text-text-secondary border-white/10'
                        )}
                      >
                        {memory.memory_type}
                      </span>

                      <span className="text-sm font-medium text-white truncate flex-1 min-w-0">
                        {memory.title}
                      </span>

                      <span
                        className={clsx(
                          'text-xs font-medium px-2 py-0.5 rounded-md border flex-shrink-0',
                          CONFIDENCE_BADGE_STYLES[memory.confidence_level] ||
                            'bg-white/5 text-text-secondary border-white/10'
                        )}
                      >
                        {CONFIDENCE_LABELS[memory.confidence_level] || memory.confidence_level}
                      </span>

                      <span className="text-xs text-text-tertiary flex-shrink-0 ml-1">
                        {relativeTime(memory.last_seen)}
                      </span>
                    </div>

                    {/* Second line: body preview */}
                    <p className="text-sm text-text-secondary truncate mt-1 pl-0.5">
                      {memory.body}
                    </p>
                  </div>

                  {/* Expanded area */}
                  <AnimatePresence>
                    {isExpanded && (
                      <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: 'auto' }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.2 }}
                        className="overflow-hidden"
                      >
                        <div className="px-4 pb-4 space-y-4 border-t border-white/5 pt-4">
                          {/* Edit mode */}
                          {isEditing ? (
                            <div className="space-y-3">
                              {/* Title input */}
                              <div>
                                <label htmlFor={`memory-edit-title-${memory.id}`} className="block text-xs text-text-tertiary mb-1">
                                  Title
                                </label>
                                <input
                                  id={`memory-edit-title-${memory.id}`}
                                  type="text"
                                  value={editForm.title || ''}
                                  onChange={(e) =>
                                    setEditForm((f) => ({ ...f, title: e.target.value }))
                                  }
                                  className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-primary/50"
                                />
                              </div>

                              {/* Body textarea */}
                              <div>
                                <label htmlFor={`memory-edit-body-${memory.id}`} className="block text-xs text-text-tertiary mb-1">
                                  Body
                                </label>
                                <textarea
                                  id={`memory-edit-body-${memory.id}`}
                                  value={editForm.body || ''}
                                  onChange={(e) =>
                                    setEditForm((f) => ({ ...f, body: e.target.value }))
                                  }
                                  rows={4}
                                  className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-primary/50 resize-none"
                                />
                              </div>

                              {/* Memory type select */}
                              <div>
                                <label htmlFor={`memory-edit-type-${memory.id}`} className="block text-xs text-text-tertiary mb-1">
                                  Type
                                </label>
                                <select
                                  id={`memory-edit-type-${memory.id}`}
                                  value={editForm.memory_type || ''}
                                  onChange={(e) =>
                                    setEditForm((f) => ({
                                      ...f,
                                      memory_type: e.target.value as MemoryType,
                                    }))
                                  }
                                  className="bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-primary/50 appearance-none cursor-pointer"
                                >
                                  <option value="entity">Entity</option>
                                  <option value="pattern">Pattern</option>
                                  <option value="outcome">Outcome</option>
                                  <option value="config">Config</option>
                                </select>
                              </div>

                              {/* Tags (comma-separated input) */}
                              <div>
                                <label htmlFor={`memory-edit-tags-${memory.id}`} className="block text-xs text-text-tertiary mb-1">
                                  Tags (comma-separated)
                                </label>
                                <input
                                  id={`memory-edit-tags-${memory.id}`}
                                  type="text"
                                  value={(editForm.tags || []).join(', ')}
                                  onChange={(e) =>
                                    setEditForm((f) => ({
                                      ...f,
                                      tags: e.target.value
                                        .split(',')
                                        .map((t) => t.trim())
                                        .filter(Boolean),
                                    }))
                                  }
                                  className="w-full bg-white/5 border border-white/10 text-white text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-primary/50"
                                  placeholder="tag1, tag2, tag3"
                                />
                              </div>

                              {/* Save / Cancel buttons */}
                              <div className="flex items-center gap-2 pt-1">
                                <button
                                  onClick={saveEdit}
                                  disabled={updateMutation.isPending}
                                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-medium bg-primary hover:bg-primary-hover text-white rounded-lg transition-colors disabled:opacity-50"
                                >
                                  {updateMutation.isPending ? (
                                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                  ) : (
                                    <Save className="h-3.5 w-3.5" />
                                  )}
                                  Save
                                </button>
                                <button
                                  onClick={cancelEdit}
                                  disabled={updateMutation.isPending}
                                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-medium text-text-secondary hover:text-white transition-colors"
                                >
                                  <X className="h-3.5 w-3.5" />
                                  Cancel
                                </button>
                              </div>

                              {updateMutation.isError && (
                                <div className="flex items-center gap-2 text-xs text-red-400 bg-red-400/10 p-2 rounded-lg border border-red-400/20">
                                  <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                                  <span>
                                    Failed to save:{' '}
                                    {(updateMutation.error as Error).message}
                                  </span>
                                </div>
                              )}
                            </div>
                          ) : (
                            <>
                              {/* Full body text */}
                              <p className="text-sm text-text-secondary whitespace-pre-wrap">
                                {memory.body}
                              </p>

                              {/* Tags */}
                              {memory.tags.length > 0 && (
                                <div className="flex flex-wrap gap-1.5">
                                  {memory.tags.map((tag) => (
                                    <span
                                      key={tag}
                                      className="inline-flex items-center px-2 py-0.5 bg-white/5 border border-white/10 text-text-secondary rounded-md text-xs"
                                    >
                                      {tag}
                                    </span>
                                  ))}
                                </div>
                              )}

                              {/* Provenance */}
                              <div className="text-xs text-text-tertiary space-y-0.5">
                                <p>
                                  Source: {formatSourceType(memory.source_type)} &middot;{' '}
                                  {new Date(memory.created_at).toLocaleDateString('en-US', {
                                    month: 'short',
                                    day: 'numeric',
                                    year: 'numeric',
                                  })}
                                </p>
                                {memory.occurrence_count > 1 && (
                                  <p>
                                    Seen {memory.occurrence_count} times &middot; Last:{' '}
                                    {relativeTime(memory.last_seen)}
                                  </p>
                                )}
                                {memory.merged && (
                                  <p className="text-text-tertiary/60">merged</p>
                                )}
                              </div>

                              {/* Action buttons */}
                              <div className="flex items-center gap-2 pt-1">
                                {isDeleteConfirm ? (
                                  <div className="flex items-center gap-2">
                                    <span className="text-xs text-text-secondary mr-1">
                                      Delete this memory?
                                    </span>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        deleteMutation.mutate(memory.id);
                                      }}
                                      disabled={deleteMutation.isPending}
                                      className="px-3 py-1.5 text-xs font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors border border-red-500/20 disabled:opacity-50"
                                    >
                                      {deleteMutation.isPending ? (
                                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                      ) : (
                                        'Delete'
                                      )}
                                    </button>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setShowDeleteConfirm(null);
                                      }}
                                      disabled={deleteMutation.isPending}
                                      className="px-3 py-1.5 text-xs font-medium text-text-secondary hover:text-white transition-colors"
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                ) : (
                                  <>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        startEdit(memory);
                                      }}
                                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-text-secondary hover:text-white hover:bg-white/5 rounded-lg transition-colors"
                                    >
                                      <Edit3 className="h-3.5 w-3.5" />
                                      Edit
                                    </button>
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setShowDeleteConfirm(memory.id);
                                      }}
                                      className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors"
                                    >
                                      <Trash2 className="h-3.5 w-3.5" />
                                      Delete
                                    </button>
                                  </>
                                )}
                              </div>

                              {deleteMutation.isError && (
                                <div className="flex items-center gap-2 text-xs text-red-400 bg-red-400/10 p-2 rounded-lg border border-red-400/20">
                                  <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
                                  <span>
                                    Failed to delete:{' '}
                                    {(deleteMutation.error as Error).message}
                                  </span>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}
