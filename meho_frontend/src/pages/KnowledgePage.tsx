// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Knowledge Base Page
 *
 * Three-tier knowledge management hub:
 * - Knowledge Tree (Global > Type > Instance) with scope-aware upload
 * - Cross-scope search across all knowledge tiers
 * - Admin audit table (collapsed)
 */
import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, BookOpen, Loader2, AlertCircle, Server, Globe, FileCode, ChevronDown, ChevronRight, ClipboardList } from 'lucide-react';
import { getAPIClient } from '../lib/api-client';
import { config } from '../lib/config';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { KnowledgeSearchResult, SearchKnowledgeResponse } from '../api/types/knowledge';
import { AuditTable } from '../features/audit';
import { useAuth } from '../contexts/AuthContext';
import { useLicense } from '../hooks/useLicense';
import { KnowledgeTree } from '../features/knowledge/components/KnowledgeTree';

export function KnowledgePage() {
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [debounceTimer, setDebounceTimer] = useState<ReturnType<typeof setTimeout> | null>(null);
  const [auditExpanded, setAuditExpanded] = useState(false);
  const { user } = useAuth();
  const isAdmin = !!user?.isGlobalAdmin;
  const license = useLicense();

  const apiClient = getAPIClient(config.apiURL);

  const handleSearchChange = useCallback(
    (value: string) => {
      setSearchQuery(value);
      if (debounceTimer) clearTimeout(debounceTimer);
      const timer = setTimeout(() => {
        setDebouncedQuery(value.trim());
      }, 400);
      setDebounceTimer(timer);
    },
    [debounceTimer]
  );

  const handleSearchSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (debounceTimer) clearTimeout(debounceTimer);
      setDebouncedQuery(searchQuery.trim());
    },
    [searchQuery, debounceTimer]
  );

  // Cross-connector search (no connector_id = searches all connectors)
  const searchResults = useQuery({
    queryKey: ['knowledge-cross-search', debouncedQuery],
    queryFn: async (): Promise<SearchKnowledgeResponse> => {
      return await apiClient.searchKnowledge({
        query: debouncedQuery,
        top_k: 20,
      });
    },
    enabled: debouncedQuery.length >= 2,
    staleTime: 30000,
  });

  const results = searchResults.data?.chunks ?? [];
  const hasSearched = debouncedQuery.length >= 2;

  return (
    <div className="h-full overflow-hidden flex flex-col bg-background relative">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-secondary/5 rounded-full blur-[100px]" />
      </div>

      {/* Header */}
      <div className="glass border-b border-white/5 px-8 py-6 z-10">
        <div>
          <h1 className="text-2xl font-bold text-white tracking-tight" data-testid="knowledge-page-title">
            Knowledge Base
          </h1>
          <p className="text-sm text-text-secondary mt-1">
            Manage and search knowledge across all scopes
          </p>
        </div>
      </div>

      {/* Content Area */}
      <div className="flex-1 overflow-y-auto z-10 p-8">
        <div className="max-w-5xl mx-auto">
          <div className="grid grid-cols-1 lg:grid-cols-5 gap-8">
            {/* Left: Knowledge Tree */}
            <div className="lg:col-span-2 space-y-4">
              <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider">
                Knowledge Tree
              </h2>
              <KnowledgeTree />
            </div>

            {/* Right: Cross-scope Search */}
            <div className="lg:col-span-3 space-y-4">
              <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider">
                Search All Knowledge
              </h2>

              {/* Search Bar */}
              <form onSubmit={handleSearchSubmit}>
                <div className="glass p-3 rounded-2xl border border-white/10">
                  <div className="relative">
                    <Search className="absolute left-4 top-1/2 -translate-y-1/2 h-5 w-5 text-text-tertiary" />
                    <input
                      type="text"
                      value={searchQuery}
                      onChange={(e) => handleSearchChange(e.target.value)}
                      placeholder="Search knowledge across all connectors..."
                      className="w-full pl-12 pr-4 py-3 bg-surface/50 border border-white/10 rounded-xl text-text-primary placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-base"
                    />
                    {searchResults.isFetching && (
                      <Loader2 className="absolute right-4 top-1/2 -translate-y-1/2 h-4 w-4 text-primary animate-spin" />
                    )}
                  </div>
                </div>
              </form>

              {/* Empty Search State */}
              {!hasSearched && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="text-center py-12"
                >
                  <div className="w-14 h-14 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mx-auto mb-4">
                    <BookOpen className="h-7 w-7 text-primary" />
                  </div>
                  <p className="text-white font-medium mb-2">Search across all knowledge</p>
                  <p className="text-sm text-text-secondary max-w-md mx-auto">
                    Search global, connector-type, and connector-instance knowledge.
                    Results show which scope each document belongs to.
                  </p>
                </motion.div>
              )}

              {/* Error State */}
              {searchResults.isError && (
                <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
                  <AlertCircle className="h-5 w-5 flex-shrink-0" />
                  <span>Search failed: {(searchResults.error as Error).message}</span>
                </div>
              )}

              {/* Loading State */}
              {searchResults.isLoading && hasSearched && (
                <div className="text-center py-12">
                  <Loader2 className="h-8 w-8 text-primary animate-spin mx-auto mb-4" />
                  <p className="text-text-secondary">Searching...</p>
                </div>
              )}

              {/* Empty Results */}
              {hasSearched && !searchResults.isLoading && results.length === 0 && !searchResults.isError && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="text-center py-12 bg-surface/50 border border-white/10 rounded-2xl"
                >
                  <Search className="h-10 w-10 text-text-tertiary mx-auto mb-4" />
                  <p className="text-text-secondary mb-1">No results found</p>
                  <p className="text-sm text-text-tertiary">
                    Try a different search query or upload knowledge to your connectors
                  </p>
                </motion.div>
              )}

              {/* Results List */}
              {results.length > 0 && (
                <div className="space-y-3">
                  <p className="text-sm text-text-secondary">
                    {results.length} result{results.length !== 1 ? 's' : ''} found
                  </p>
                  <AnimatePresence mode="popLayout">
                    {results.map((result, index) => (
                      <SearchResultCard key={result.id || index} result={result} index={index} />
                    ))}
                  </AnimatePresence>
                </div>
              )}
            </div>
          </div>

          {/* Contextual audit section -- enterprise admin only, default collapsed */}
          {isAdmin && license.edition === 'enterprise' && (
            <div className="mt-8 pt-6 border-t border-white/5">
              <button
                onClick={() => setAuditExpanded(!auditExpanded)}
                className="flex items-center gap-2 text-sm font-medium text-text-secondary hover:text-white transition-colors"
              >
                {auditExpanded ? (
                  <ChevronDown className="h-4 w-4" />
                ) : (
                  <ChevronRight className="h-4 w-4" />
                )}
                <ClipboardList className="h-4 w-4" />
                Recent Knowledge Activity
              </button>
              {auditExpanded && (
                <div className="mt-3">
                  <AuditTable
                    defaultFilters={{ resource_type: 'knowledge_doc' }}
                    limit={10}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Connector type badge color mapping
 */
function getConnectorBadgeStyle(connectorType: string): string {
  switch (connectorType) {
    case 'vmware':
      return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
    case 'proxmox':
      return 'bg-orange-500/10 text-orange-400 border-orange-500/20';
    case 'gcp':
      return 'bg-sky-500/10 text-sky-400 border-sky-500/20';
    case 'kubernetes':
      return 'bg-blue-500/10 text-blue-400 border-blue-500/20';
    case 'soap':
      return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
    case 'graphql':
      return 'bg-pink-500/10 text-pink-400 border-pink-500/20';
    case 'grpc':
      return 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20';
    default:
      return 'bg-sky-500/10 text-sky-400 border-sky-500/20';
  }
}

/**
 * Connector type icon component
 */
function ConnectorTypeIcon({ type, className }: Readonly<{ type: string; className?: string }>) {
  const isTyped = ['vmware', 'proxmox', 'gcp', 'kubernetes'].includes(type);
  const isSoap = type === 'soap';

  if (isTyped) return <Server className={className} />;
  if (isSoap) return <FileCode className={className} />;
  return <Globe className={className} />;
}

interface SearchResultCardProps {
  result: KnowledgeSearchResult;
  index: number;
}

function SearchResultCard({ result, index }: Readonly<SearchResultCardProps>) {
  const badgeStyle = getConnectorBadgeStyle(result.connector_type || 'rest');

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      transition={{ delay: index * 0.03 }}
      className="glass rounded-xl p-5 border border-white/10 hover:border-primary/30 transition-all"
    >
      <div className="space-y-3">
        {/* Content */}
        <p className="text-text-primary leading-relaxed line-clamp-4 text-sm">
          {result.text}
        </p>

        {/* Footer: connector badge + metadata */}
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 flex-wrap">
            {/* Connector Badge */}
            {result.connector_name && (
              <span
                className={clsx(
                  'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border',
                  badgeStyle
                )}
              >
                <ConnectorTypeIcon type={result.connector_type || 'rest'} className="h-3 w-3" />
                {result.connector_name}
              </span>
            )}

            {/* Knowledge type */}
            <span className="text-xs px-2 py-0.5 rounded-md bg-white/5 border border-white/10 text-text-secondary capitalize">
              {result.knowledge_type}
            </span>

            {/* Tags */}
            {result.tags && result.tags.length > 0 && result.tags.slice(0, 3).map((tag) => (
              <span
                key={tag}
                className="text-xs px-2 py-0.5 rounded-md bg-white/5 border border-white/10 text-text-tertiary"
              >
                {tag}
              </span>
            ))}
          </div>

          {/* Score */}
          {result.score !== undefined && result.score > 0 && (
            <span className="text-xs text-text-tertiary whitespace-nowrap">
              {(result.score * 100).toFixed(0)}% match
            </span>
          )}
        </div>
      </div>
    </motion.div>
  );
}
