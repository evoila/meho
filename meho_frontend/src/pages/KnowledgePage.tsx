// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Knowledge Base Page
 *
 * Three-tier knowledge management hub:
 * - Knowledge Tree (Global > Type > Instance) with scope-aware upload
 * - Grounded search with an LLM answer + cited retrieved chunks
 * - Admin audit table (collapsed)
 */
import { useCallback, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  AlertCircle,
  BookOpen,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  FileCode,
  FileText,
  Globe,
  Loader2,
  Search,
  Server,
  SlidersHorizontal,
  Sparkles,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type {
  KnowledgeSearchCitation,
  KnowledgeSearchResult,
  SearchKnowledgeResponse,
} from '../api/types/knowledge';
import { getKnowledgeClient } from '@/api/clients/knowledge';
import { AuditTable } from '../features/audit';
import { useAuth } from '../contexts/AuthContext';
import { useLicense } from '../hooks/useLicense';
import { KnowledgeTree } from '../features/knowledge/components/KnowledgeTree';

const MIN_QUERY_LENGTH = 2;
const DEFAULT_TOP_K = 8;
const MIN_TOP_K = 1;
const MAX_TOP_K = 25;
const FALLBACK_ANSWER =
  'The retrieved chunks did not contain enough information to answer the question.';

interface SearchMetadataDisplay {
  filename?: string;
  section_header?: string;
  heading_path?: string[];
  page_number?: number;
  page_start?: number;
  page_end?: number;
  source_chunk_index?: number | null;
}

interface SearchResultCardProps {
  result: KnowledgeSearchResult;
  index: number;
  highlighted: boolean;
  cited: boolean;
}

interface CitationCardProps {
  citation: KnowledgeSearchCitation;
  active: boolean;
  onClick: (chunkIndex: number) => void;
}

export function KnowledgePage() {
  const [searchInput, setSearchInput] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [topK, setTopK] = useState(DEFAULT_TOP_K);
  const [submittedTopK, setSubmittedTopK] = useState(DEFAULT_TOP_K);
  const [highlightedChunkIndex, setHighlightedChunkIndex] = useState<number | null>(null);
  const [auditExpanded, setAuditExpanded] = useState(false);
  const topKLabelRef = useRef<HTMLSpanElement>(null);

  const { user } = useAuth();
  const isAdmin = !!user?.isGlobalAdmin;
  const license = useLicense();

  const knowledgeClient = getKnowledgeClient();

  const searchResults = useQuery({
    queryKey: ['knowledge-grounded-search', submittedQuery, submittedTopK],
    queryFn: async (): Promise<SearchKnowledgeResponse> => {
      return await knowledgeClient.searchKnowledge({
        query: submittedQuery,
        top_k: submittedTopK,
      });
    },
    enabled: submittedQuery.length >= MIN_QUERY_LENGTH,
    staleTime: 30000,
  });

  const handleTopKChange = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
    setTopK(Number(event.target.value));
  }, []);

  const results = searchResults.data?.results ?? searchResults.data?.chunks ?? [];
  const citations = useMemo(
    () => searchResults.data?.citations ?? [],
    [searchResults.data?.citations],
  );
  const answer = searchResults.data?.answer?.trim() ?? '';
  const answerError = searchResults.data?.answer_error ?? '';
  const hasSearched = submittedQuery.length >= MIN_QUERY_LENGTH;

  const citedChunkIndices = useMemo(() => {
    return new Set(citations.map((citation) => citation.chunk_index));
  }, [citations]);

  function scrollToChunk(chunkIndex: number): void {
    setHighlightedChunkIndex(chunkIndex);
    requestAnimationFrame(() => {
      document
        .getElementById(`knowledge-result-${chunkIndex}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  function handleSearchSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const trimmedQuery = searchInput.trim();
    if (trimmedQuery.length < MIN_QUERY_LENGTH) {
      return;
    }

    setHighlightedChunkIndex(null);
    setSubmittedTopK(topK);
    if (trimmedQuery === submittedQuery && topK === submittedTopK) {
      void searchResults.refetch();
      return;
    }

    setSubmittedQuery(trimmedQuery);
  }

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-background">
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute right-0 top-0 h-[500px] w-[500px] rounded-full bg-primary/5 blur-[100px]" />
        <div className="absolute bottom-0 left-0 h-[500px] w-[500px] rounded-full bg-secondary/5 blur-[100px]" />
      </div>

      <div className="glass z-10 border-b border-white/5 px-8 py-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white" data-testid="knowledge-page-title">
            Knowledge Base
          </h1>
          <p className="mt-1 text-sm text-text-secondary">
            Search your ingested knowledge with grounded answers and cited chunks
          </p>
        </div>
      </div>

      <div className="z-10 flex-1 overflow-y-auto p-8">
        <div className="mx-auto max-w-6xl">
          <div className="grid grid-cols-1 gap-8 lg:grid-cols-5">
            <div className="space-y-4 lg:col-span-2">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-text-secondary">
                Knowledge Tree
              </h2>
              <KnowledgeTree />
            </div>

            <div className="space-y-4 lg:col-span-3">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-text-secondary">
                Ask Your Knowledge
              </h2>

              <form onSubmit={handleSearchSubmit}>
                <div className="glass rounded-2xl border border-white/10 p-3">
                  <div className="relative flex items-center gap-3">
                    <Search className="pointer-events-none absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-text-tertiary" />
                    <input
                      type="text"
                      value={searchInput}
                      onChange={(event) => setSearchInput(event.target.value)}
                      placeholder="Ask a question about your ingested documents..."
                      className="w-full rounded-xl border border-white/10 bg-surface/50 py-3 pl-12 pr-28 text-base text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                    />
                    <button
                      type="submit"
                      disabled={searchInput.trim().length < MIN_QUERY_LENGTH || searchResults.isFetching}
                      className="absolute right-2 inline-flex items-center gap-2 rounded-xl bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {searchResults.isFetching ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Sparkles className="h-4 w-4" />
                      )}
                      Ask
                    </button>
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-4 px-1">
                    <p className="text-xs text-text-tertiary">
                      The answer is generated only from retrieved chunks. If the data is missing, it should say so.
                    </p>
                    <div className="flex shrink-0 items-center gap-2">
                      <SlidersHorizontal className="h-3.5 w-3.5 text-text-tertiary" />
                      <label htmlFor="top-k-slider" className="text-xs text-text-tertiary whitespace-nowrap">
                        Top-K
                      </label>
                      <input
                        id="top-k-slider"
                        type="range"
                        min={MIN_TOP_K}
                        max={MAX_TOP_K}
                        value={topK}
                        onChange={handleTopKChange}
                        className="h-1.5 w-20 cursor-pointer appearance-none rounded-full bg-white/10 accent-primary [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary"
                      />
                      <span ref={topKLabelRef} className="min-w-[1.5rem] text-center text-xs font-medium text-primary">
                        {topK}
                      </span>
                    </div>
                  </div>
                </div>
              </form>

              {!hasSearched && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="py-12 text-center"
                >
                  <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-primary/20 bg-primary/10">
                    <BookOpen className="h-7 w-7 text-primary" />
                  </div>
                  <p className="mb-2 font-medium text-white">Search across all knowledge scopes</p>
                  <p className="mx-auto max-w-md text-sm text-text-secondary">
                    Ask a question and review the grounded answer, cited chunk quotes, and the ranked retrieval results underneath.
                  </p>
                </motion.div>
              )}

              {searchResults.isError && (
                <div className="flex items-center gap-3 rounded-xl border border-red-500/20 bg-red-500/10 p-4 text-red-400">
                  <AlertCircle className="h-5 w-5 flex-shrink-0" />
                  <span>Search failed: {(searchResults.error as Error).message}</span>
                </div>
              )}

              {searchResults.isLoading && hasSearched && (
                <div className="glass rounded-2xl border border-white/10 px-6 py-10 text-center">
                  <Loader2 className="mx-auto mb-4 h-8 w-8 animate-spin text-primary" />
                  <p className="font-medium text-white">Searching and grounding an answer...</p>
                  <p className="mt-2 text-sm text-text-secondary">
                    Retrieving the best chunks, then generating a citations-only response.
                  </p>
                </div>
              )}

              {hasSearched && !searchResults.isLoading && results.length === 0 && !searchResults.isError && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="rounded-2xl border border-white/10 bg-surface/50 py-12 text-center"
                >
                  <Search className="mx-auto mb-4 h-10 w-10 text-text-tertiary" />
                  <p className="mb-1 text-text-secondary">No relevant chunks found</p>
                  <p className="text-sm text-text-tertiary">
                    Try rephrasing the question or upload more knowledge first.
                  </p>
                </motion.div>
              )}

              {results.length > 0 && (
                <div className="space-y-4">
                  <motion.div
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="glass rounded-2xl border border-white/10 p-6"
                  >
                    <div className="mb-4 flex items-center justify-between gap-3">
                      <div className="inline-flex items-center gap-2 rounded-full border border-primary/20 bg-primary/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-primary">
                        <Sparkles className="h-3.5 w-3.5" />
                        Grounded Answer
                      </div>
                      <span className="text-xs text-text-tertiary">
                        {results.length} retrieved chunk{results.length !== 1 ? 's' : ''}
                      </span>
                    </div>

                    {answerError ? (
                      <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                        {answerError}
                      </div>
                    ) : (
                      <p className="whitespace-pre-wrap text-sm leading-7 text-text-primary">
                        {answer || FALLBACK_ANSWER}
                      </p>
                    )}

                    {citations.length > 0 && (
                      <div className="mt-5 border-t border-white/5 pt-5">
                        <div className="mb-3 text-xs font-semibold uppercase tracking-[0.18em] text-text-tertiary">
                          References
                        </div>
                        <div className="space-y-2">
                          {citations.map((citation, index) => (
                            <CitationCard
                              key={`${citation.result_id}-${index}`}
                              citation={citation}
                              active={highlightedChunkIndex === citation.chunk_index}
                              onClick={scrollToChunk}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                  </motion.div>

                  <div className="flex items-center justify-between px-1">
                    <div className="text-sm font-medium text-white">Retrieved Chunks</div>
                    <div className="text-xs text-text-tertiary">
                      Ranked by retrieval score
                    </div>
                  </div>

                  <AnimatePresence mode="popLayout">
                    {results.map((result, index) => (
                      <motion.div
                        key={result.id || `${result.connector_id}-${index}`}
                        id={`knowledge-result-${index}`}
                        layout
                        initial={{ opacity: 0, y: 12 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.98 }}
                        transition={{ delay: index * 0.03 }}
                      >
                        <SearchResultCard
                          result={result}
                          index={index}
                          highlighted={highlightedChunkIndex === index}
                          cited={citedChunkIndices.has(index)}
                        />
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </div>
              )}
            </div>
          </div>

          {isAdmin && license.edition === 'enterprise' && (
            <div className="mt-8 border-t border-white/5 pt-6">
              <button
                onClick={() => setAuditExpanded(!auditExpanded)}
                className="flex items-center gap-2 text-sm font-medium text-text-secondary transition-colors hover:text-white"
              >
                {auditExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                <ClipboardList className="h-4 w-4" />
                Recent Knowledge Activity
              </button>
              {auditExpanded && (
                <div className="mt-3">
                  <AuditTable defaultFilters={{ resource_type: 'knowledge_doc' }} limit={10} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function getConnectorBadgeStyle(connectorType: string): string {
  switch (connectorType) {
    case 'vmware':
      return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20';
    case 'proxmox':
      return 'bg-orange-500/10 text-orange-300 border-orange-500/20';
    case 'gcp':
      return 'bg-sky-500/10 text-sky-300 border-sky-500/20';
    case 'kubernetes':
      return 'bg-blue-500/10 text-blue-300 border-blue-500/20';
    case 'soap':
      return 'bg-amber-500/10 text-amber-300 border-amber-500/20';
    case 'graphql':
      return 'bg-pink-500/10 text-pink-300 border-pink-500/20';
    case 'grpc':
      return 'bg-indigo-500/10 text-indigo-300 border-indigo-500/20';
    case 'global':
      return 'bg-violet-500/10 text-violet-300 border-violet-500/20';
    default:
      return 'bg-slate-500/10 text-slate-300 border-slate-500/20';
  }
}

function ConnectorTypeIcon({ type, className }: Readonly<{ type: string; className?: string }>) {
  const isTyped = ['vmware', 'proxmox', 'gcp', 'kubernetes'].includes(type);
  const isSoap = type === 'soap';

  if (isTyped) {
    return <Server className={className} />;
  }
  if (isSoap) {
    return <FileCode className={className} />;
  }
  if (type === 'global') {
    return <BookOpen className={className} />;
  }
  return <Globe className={className} />;
}

function getHeadingLabel(item: SearchMetadataDisplay): string {
  if (item.heading_path && item.heading_path.length > 0) {
    return item.heading_path.filter(Boolean).join(' > ');
  }
  return item.section_header?.trim() ?? '';
}

function getPageLabel(item: SearchMetadataDisplay): string {
  const start = item.page_start || item.page_number || 0;
  const end = item.page_end || item.page_start || item.page_number || 0;
  if (start <= 0 && end <= 0) {
    return '';
  }
  if (start > 0 && end > 0 && start !== end) {
    return `p.${start}-${end}`;
  }
  return `p.${start || end}`;
}

function getSourceChunkLabel(item: SearchMetadataDisplay): string {
  if (item.source_chunk_index === undefined || item.source_chunk_index === null || item.source_chunk_index < 0) {
    return '';
  }
  return `chunk ${item.source_chunk_index}`;
}

function SearchResultCard({ result, index, highlighted, cited }: Readonly<SearchResultCardProps>) {
  const badgeStyle = getConnectorBadgeStyle(result.connector_type || 'rest');
  const headingLabel = getHeadingLabel(result);
  const pageLabel = getPageLabel(result);
  const sourceChunkLabel = getSourceChunkLabel(result);
  const score = result.score ?? 0;
  const scorePercent = Math.max(0, Math.min(100, Math.round(score * 100)));

  return (
    <div
      className={clsx(
        'glass rounded-2xl border p-5 transition-all',
        highlighted ? 'border-primary/50 shadow-[0_0_0_1px_rgba(129,140,248,0.25)]' : 'border-white/10',
        cited && !highlighted ? 'border-l-2 border-l-primary/60' : undefined
      )}
    >
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={clsx(
                'inline-flex h-7 min-w-7 items-center justify-center rounded-lg px-2 text-xs font-bold',
                cited ? 'bg-primary text-white' : 'bg-white/6 text-text-secondary'
              )}
            >
              {index + 1}
            </span>

            {result.connector_name && (
              <span
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium',
                  badgeStyle
                )}
              >
                <ConnectorTypeIcon type={result.connector_type || 'rest'} className="h-3 w-3" />
                {result.connector_name}
              </span>
            )}

            <span className="rounded-md border border-white/10 bg-white/5 px-2 py-0.5 text-xs capitalize text-text-secondary">
              {result.knowledge_type}
            </span>

            {result.filename && (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2 py-0.5 text-xs text-text-secondary">
                <FileText className="h-3.5 w-3.5" />
                {result.family_name || result.filename}
              </span>
            )}

            {result.doc_version && (
              <span className="inline-flex items-center rounded-md border border-accent/30 bg-accent/10 px-2 py-0.5 text-xs font-mono text-accent">
                {result.doc_version}
              </span>
            )}
          </div>

          <div className="text-right">
            <div className="text-xs font-semibold text-text-secondary">{score.toFixed(3)}</div>
            <div className="text-[11px] text-text-tertiary">{scorePercent}% match</div>
          </div>
        </div>

        {(headingLabel || pageLabel || sourceChunkLabel || result.tags.length > 0) && (
          <div className="flex flex-wrap gap-2">
            {headingLabel && (
              <span className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-text-secondary">
                {headingLabel}
              </span>
            )}
            {pageLabel && (
              <span className="rounded-md border border-blue-500/20 bg-blue-500/10 px-2 py-1 text-xs text-blue-200">
                {pageLabel}
              </span>
            )}
            {sourceChunkLabel && (
              <span className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-text-secondary">
                {sourceChunkLabel}
              </span>
            )}
            {result.tags.slice(0, 4).map((tag) => (
              <span
                key={`${result.id}-${tag}`}
                className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-text-tertiary"
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        <div className="h-1.5 overflow-hidden rounded-full bg-white/5">
          <div
            className="h-full rounded-full bg-gradient-to-r from-primary to-secondary transition-[width]"
            style={{ width: `${Math.max(scorePercent, 2)}%` }}
          />
        </div>

        <p className="whitespace-pre-wrap text-sm leading-7 text-text-primary">{result.text}</p>
      </div>
    </div>
  );
}

function CitationCard({ citation, active, onClick }: Readonly<CitationCardProps>) {
  const headingLabel = getHeadingLabel(citation);
  const pageLabel = getPageLabel(citation);
  const sourceChunkLabel = getSourceChunkLabel(citation);

  return (
    <button
      type="button"
      onClick={() => onClick(citation.chunk_index)}
      className={clsx(
        'w-full rounded-xl border px-4 py-3 text-left transition-all',
        active
          ? 'border-primary/40 bg-primary/10'
          : 'border-white/10 bg-white/[0.03] hover:border-primary/25 hover:bg-primary/[0.06]'
      )}
    >
      <div className="flex items-start gap-3">
        <span className="inline-flex h-7 min-w-7 items-center justify-center rounded-lg bg-primary px-2 text-xs font-bold text-white">
          {citation.chunk_index + 1}
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {citation.connector_name && (
              <span className="text-xs font-medium text-primary">{citation.connector_name}</span>
            )}
            {citation.filename && (
              <span className="text-xs text-text-secondary">{citation.filename}</span>
            )}
            {citation.score !== undefined && (
              <span className="text-xs text-text-tertiary">{citation.score.toFixed(3)}</span>
            )}
          </div>

          {(headingLabel || pageLabel || sourceChunkLabel) && (
            <div className="flex flex-wrap gap-2 text-[11px] text-text-tertiary">
              {headingLabel && <span>{headingLabel}</span>}
              {pageLabel && <span>{pageLabel}</span>}
              {sourceChunkLabel && <span>{sourceChunkLabel}</span>}
            </div>
          )}

          <p className="line-clamp-3 text-sm italic leading-6 text-text-secondary">"{citation.quote}"</p>
        </div>
      </div>
    </button>
  );
}
