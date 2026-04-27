// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * DocumentPreviewModal
 *
 * Full-screen modal for previewing an ingested document. Two tabs:
 *   - "Document" renders the markdown document preview.
 *   - "Chunks" displays individual chunk cards with metadata.
 *
 * Data is fetched on open via GET /api/knowledge/documents/{id}/detail.
 */
import { useState, useEffect, useRef, useId, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { motion, AnimatePresence } from 'motion/react';
import {
  X,
  FileText,
  Layers,
  Loader2,
  AlertCircle,
  ChevronDown,
  ChevronUp,
  Hash,
  BookOpen,
  Tag,
  Code,
  Globe,
  Sparkles,
} from 'lucide-react';
import clsx from 'clsx';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useFocusTrap } from '@/shared/hooks/useFocusTrap';
import { getKnowledgeClient } from '@/api/clients/knowledge';
import type {
  DocumentDetailResponse,
  DocumentChunkPreview,
  ChunkSearchMetadata,
} from '@/api/types/knowledge';

type TabId = 'document' | 'chunks';

interface DocumentPreviewModalProps {
  documentId: string;
  onClose: () => void;
}

const TRUNCATE_LENGTH = 600;

function MetadataRow({ label, value, icon }: Readonly<{ label: string; value: string; icon?: React.ReactNode }>) {
  return (
    <div className="flex items-start gap-2 text-xs">
      {icon && <span className="text-text-tertiary mt-0.5 flex-shrink-0">{icon}</span>}
      <span className="text-text-tertiary min-w-[80px] flex-shrink-0">{label}</span>
      <span className="text-text-secondary break-all">{value}</span>
    </div>
  );
}

function ChunkCard({
  chunk,
  index,
  total,
}: Readonly<{ chunk: DocumentChunkPreview; index: number; total: number }>) {
  const [expanded, setExpanded] = useState(false);
  const needsTruncation = chunk.text.length > TRUNCATE_LENGTH;
  const displayText = expanded || !needsTruncation ? chunk.text : chunk.text.slice(0, TRUNCATE_LENGTH) + '...';
  const meta: ChunkSearchMetadata | undefined = chunk.search_metadata ?? undefined;

  const metadataRows = useMemo(() => {
    if (!meta) return [];
    const rows: { label: string; value: string; icon: React.ReactNode }[] = [];

    if (meta.chapter) rows.push({ label: 'Chapter', icon: <BookOpen className="h-3 w-3" />, value: meta.chapter });
    if (meta.section) rows.push({ label: 'Section', icon: <BookOpen className="h-3 w-3" />, value: meta.section });
    if (meta.subsection) rows.push({ label: 'Subsection', icon: <BookOpen className="h-3 w-3" />, value: meta.subsection });
    if (meta.heading_hierarchy && meta.heading_hierarchy.length > 0) {
      rows.push({ label: 'Path', icon: <Hash className="h-3 w-3" />, value: meta.heading_hierarchy.join(' > ') });
    }
    if (meta.content_type) rows.push({ label: 'Type', icon: <Tag className="h-3 w-3" />, value: meta.content_type });
    if (meta.endpoint_path) {
      const method = meta.http_method ? `${meta.http_method} ` : '';
      rows.push({ label: 'Endpoint', icon: <Globe className="h-3 w-3" />, value: `${method}${meta.endpoint_path}` });
    }
    if (meta.programming_language) rows.push({ label: 'Language', icon: <Code className="h-3 w-3" />, value: meta.programming_language });
    if (meta.page_numbers && meta.page_numbers.length > 0) {
      rows.push({ label: 'Pages', icon: <FileText className="h-3 w-3" />, value: meta.page_numbers.join(', ') });
    } else if (meta.page_number && meta.page_number > 0) {
      rows.push({ label: 'Page', icon: <FileText className="h-3 w-3" />, value: String(meta.page_number) });
    }
    if (meta.keywords && meta.keywords.length > 0) {
      rows.push({ label: 'Keywords', icon: <Tag className="h-3 w-3" />, value: meta.keywords.join(', ') });
    }

    return rows;
  }, [meta]);

  return (
    <div className="rounded-lg border border-white/5 bg-white/[0.02] overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-white/[0.03] border-b border-white/5">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono font-medium text-primary bg-primary/10 px-1.5 py-0.5 rounded">
            {index + 1}/{total}
          </span>
          <span className="text-[10px] font-mono text-text-tertiary">{chunk.id.slice(0, 8)}</span>
        </div>
        {needsTruncation && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="flex items-center gap-1 text-[10px] text-text-tertiary hover:text-text-secondary transition-colors"
          >
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {expanded ? 'Collapse' : 'Expand'}
          </button>
        )}
      </div>

      {/* Text */}
      <div className="px-3 py-2">
        <pre className="text-xs text-text-secondary whitespace-pre-wrap font-sans leading-relaxed">
          {displayText}
        </pre>
      </div>

      {/* Metadata */}
      {metadataRows.length > 0 && (
        <div className="px-3 pb-2 pt-1 border-t border-white/5 space-y-1">
          {metadataRows.map((row) => (
            <MetadataRow key={row.label} label={row.label} value={row.value} icon={row.icon} />
          ))}
        </div>
      )}
    </div>
  );
}

const markdownComponents: Components = {
  h1: ({ node: _node, children, ...props }) => (
    <h1
      className="text-3xl font-bold tracking-tight text-white mt-2 mb-5 pb-3 border-b border-white/10"
      {...props}
    >
      {children}
    </h1>
  ),
  h2: ({ node: _node, children, ...props }) => (
    <h2
      className="text-2xl font-semibold tracking-tight text-white mt-8 mb-4"
      {...props}
    >
      {children}
    </h2>
  ),
  h3: ({ node: _node, children, ...props }) => (
    <h3 className="text-xl font-semibold text-white mt-6 mb-3" {...props}>
      {children}
    </h3>
  ),
  h4: ({ node: _node, children, ...props }) => (
    <h4 className="text-lg font-medium text-white mt-5 mb-2" {...props}>
      {children}
    </h4>
  ),
  h5: ({ node: _node, children, ...props }) => (
    <h5
      className="text-base font-medium text-white mt-4 mb-2 uppercase tracking-wide"
      {...props}
    >
      {children}
    </h5>
  ),
  h6: ({ node: _node, children, ...props }) => (
    <h6
      className="text-sm font-semibold text-text-secondary mt-4 mb-2 uppercase tracking-wide"
      {...props}
    >
      {children}
    </h6>
  ),
  p: ({ node: _node, ...props }) => (
    <p
      className="text-[15px] leading-8 text-text-secondary mb-4"
      {...props}
    />
  ),
  ul: ({ node: _node, ...props }) => (
    <ul
      className="list-disc pl-6 my-4 space-y-2 marker:text-text-tertiary text-text-secondary"
      {...props}
    />
  ),
  ol: ({ node: _node, ...props }) => (
    <ol
      className="list-decimal pl-6 my-4 space-y-2 marker:text-text-tertiary text-text-secondary"
      {...props}
    />
  ),
  li: ({ node: _node, ...props }) => (
    <li className="pl-1 text-[15px] leading-7" {...props} />
  ),
  a: ({ node: _node, children, ...props }) => (
    <a
      className="text-primary hover:text-primary-hover underline decoration-primary/30 hover:decoration-primary transition-colors"
      target="_blank"
      rel="noopener noreferrer"
      {...props}
    >
      {children}
    </a>
  ),
  strong: ({ node: _node, ...props }) => (
    <strong className="font-semibold text-white" {...props} />
  ),
  em: ({ node: _node, ...props }) => (
    <em className="italic text-text-secondary/90" {...props} />
  ),
  hr: ({ node: _node, ...props }) => (
    <hr className="my-8 border-white/10" {...props} />
  ),
  blockquote: ({ node: _node, ...props }) => (
    <blockquote
      className="my-5 border-l-4 border-primary/40 pl-4 italic text-text-secondary bg-white/[0.02] py-2 rounded-r-lg"
      {...props}
    />
  ),
  code({ node: _node, className, children, ...props }) {
    const isInline = !className?.includes('language-');
    if (isInline) {
      return (
        <code
          className="bg-primary/10 text-primary px-1.5 py-0.5 rounded-md text-[0.9em] font-mono"
          {...props}
        >
          {children}
        </code>
      );
    }

    return (
      <code className={clsx('font-mono text-sm', className)} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ node: _node, ...props }) => (
    <pre
      className="my-5 overflow-x-auto rounded-xl border border-white/10 bg-black/30 p-4 text-sm leading-7"
      {...props}
    />
  ),
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto my-6 rounded-xl border border-white/10 bg-white/[0.02]">
      <table className="min-w-full divide-y divide-white/10" {...props} />
    </div>
  ),
  thead: ({ node: _node, ...props }) => (
    <thead className="bg-white/[0.04]" {...props} />
  ),
  tbody: ({ node: _node, ...props }) => (
    <tbody className="divide-y divide-white/5" {...props} />
  ),
  th: ({ node: _node, ...props }) => (
    <th
      className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
      {...props}
    />
  ),
  td: ({ node: _node, ...props }) => (
    <td
      className="px-4 py-3 align-top text-sm leading-6 text-text-primary whitespace-normal"
      {...props}
    />
  ),
};

const CHUNK_PAGE_SIZE = 50;

export function DocumentPreviewModal({ documentId, onClose }: Readonly<DocumentPreviewModalProps>) {
  const [activeTab, setActiveTab] = useState<TabId>('document');
  const [data, setData] = useState<DocumentDetailResponse | null>(null);
  const [allChunks, setAllChunks] = useState<DocumentChunkPreview[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Track documentId so we can reset dependent state during render when it
  // changes (avoids the react-hooks/set-state-in-effect anti-pattern).
  const [trackedDocumentId, setTrackedDocumentId] = useState(documentId);
  const containerRef = useRef<HTMLDivElement>(null);
  const labelId = useId();
  useFocusTrap(containerRef, true);

  if (trackedDocumentId !== documentId) {
    setTrackedDocumentId(documentId);
    setData(null);
    setAllChunks([]);
    setLoading(true);
    setError(null);
  }

  const totalChunks = data?.total_chunks ?? data?.chunks_created ?? 0;
  const hasMoreChunks = allChunks.length < totalChunks;

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    globalThis.addEventListener('keydown', handleKeyDown);
    return () => globalThis.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;

    getKnowledgeClient().getDocumentDetail(documentId, {
      chunk_offset: 0,
      chunk_limit: CHUNK_PAGE_SIZE,
    }).then((detail: DocumentDetailResponse) => {
      if (!cancelled) {
        setData(detail);
        setAllChunks(detail.chunks);
        setLoading(false);
      }
    }).catch((err: unknown) => {
      if (!cancelled) {
        setError(err instanceof Error ? err.message : 'Failed to load document');
        setLoading(false);
      }
    });

    return () => { cancelled = true; };
  }, [documentId]);

  const loadMoreChunks = () => {
    if (loadingMore || !hasMoreChunks) return;
    setLoadingMore(true);
    getKnowledgeClient().getDocumentDetail(documentId, {
      chunk_offset: allChunks.length,
      chunk_limit: CHUNK_PAGE_SIZE,
    }).then((detail: DocumentDetailResponse) => {
      setAllChunks((prev) => [...prev, ...detail.chunks]);
      setLoadingMore(false);
    }).catch(() => {
      setLoadingMore(false);
    });
  };

  const renderedMarkdown = useMemo(() => {
    if (!data) return '';

    // Prefer the stored markdown from S3 (full Docling export with formatting).
    if (data.markdown) return data.markdown;

    // Fallback: reconstruct structure from chunk metadata for documents
    // ingested before markdown persistence was added.
    if (!allChunks.length) return '';

    let prevChapter = '';
    let prevSection = '';
    let prevSubsection = '';

    const parts: string[] = [];

    for (const chunk of allChunks) {
      const meta = chunk.search_metadata;
      const headings: string[] = [];

      if (meta) {
        if (meta.chapter && meta.chapter !== prevChapter) {
          headings.push(`## ${meta.chapter}`);
          prevChapter = meta.chapter;
          prevSection = '';
          prevSubsection = '';
        }
        if (meta.section && meta.section !== prevSection) {
          headings.push(`### ${meta.section}`);
          prevSection = meta.section;
          prevSubsection = '';
        }
        if (meta.subsection && meta.subsection !== prevSubsection) {
          headings.push(`#### ${meta.subsection}`);
          prevSubsection = meta.subsection;
        }
      }

      if (headings.length > 0) {
        parts.push(headings.join('\n\n'));
      }
      parts.push(chunk.text);
    }

    return parts.join('\n\n');
  }, [data, allChunks]);

  const modal = (
    <AnimatePresence>
      <motion.div
        key="doc-preview-backdrop"
        ref={containerRef}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelId}
        onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      >
        <motion.div
          key="doc-preview-panel"
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ duration: 0.2, ease: 'easeOut' }}
          className="w-full max-w-4xl max-h-[85vh] rounded-2xl bg-surface shadow-2xl flex flex-col overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-border">
            <div className="flex items-center gap-3 min-w-0">
              <div className="p-2 rounded-lg bg-primary/10 text-primary flex-shrink-0">
                <FileText className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <h2 id={labelId} className="text-base font-semibold text-white truncate">
                  {data?.filename ?? 'Loading...'}
                </h2>
                {data && (
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    <span className={clsx(
                      'text-[10px] font-medium px-1.5 py-px rounded border',
                      data.status === 'completed'
                        ? 'bg-green-400/10 text-green-400 border-green-400/20'
                        : 'bg-white/5 text-text-secondary border-white/10',
                    )}>
                      {data.status}
                    </span>
                    {data.chunks_created > 0 && (
                      <span className="text-[10px] text-text-tertiary">
                        {data.chunks_created} chunk{data.chunks_created !== 1 ? 's' : ''}
                      </span>
                    )}
                    {data.file_size != null && (
                      <span className="text-[10px] text-text-tertiary">
                        {(data.file_size / 1024).toFixed(1)} KB
                      </span>
                    )}
                    {data.knowledge_type && (
                      <span className="text-[10px] text-text-tertiary capitalize">
                        {data.knowledge_type}
                      </span>
                    )}
                    {data.doc_version && (
                      <span className="text-[10px] text-accent font-medium">
                        {data.doc_version}
                      </span>
                    )}
                  </div>
                )}
                {data?.tags.length ? (
                  <div className="flex flex-wrap items-center gap-1.5 mt-2">
                    {data.tags.map((tag, index) => (
                      <span
                        key={`${tag}-${index}`}
                        className="inline-flex items-center px-2 py-0.5 rounded-full border border-primary/20 bg-primary/10 text-primary text-[10px] font-medium"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-2 text-text-tertiary hover:text-white hover:bg-white/5 rounded-lg transition-colors flex-shrink-0"
              aria-label="Close"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Tabs */}
          <div className="flex border-b border-border px-5">
            <button
              onClick={() => setActiveTab('document')}
              className={clsx(
                'flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors relative',
                activeTab === 'document'
                  ? 'text-primary'
                  : 'text-text-tertiary hover:text-text-secondary',
              )}
            >
              <FileText className="h-4 w-4" />
              Document
              {activeTab === 'document' && (
                <motion.div
                  layoutId="doc-preview-tab-indicator"
                  className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary rounded-full"
                />
              )}
            </button>
            <button
              onClick={() => setActiveTab('chunks')}
              className={clsx(
                'flex items-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors relative',
                activeTab === 'chunks'
                  ? 'text-primary'
                  : 'text-text-tertiary hover:text-text-secondary',
              )}
            >
              <Layers className="h-4 w-4" />
              Chunks{data ? ` (${totalChunks})` : ''}
              {activeTab === 'chunks' && (
                <motion.div
                  layoutId="doc-preview-tab-indicator"
                  className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary rounded-full"
                />
              )}
            </button>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center py-20">
                <Loader2 className="h-6 w-6 animate-spin text-primary" />
                <span className="ml-2 text-sm text-text-secondary">Loading document...</span>
              </div>
            ) : error ? (
              <div className="flex items-center justify-center py-20 text-red-400">
                <AlertCircle className="h-5 w-5 mr-2" />
                <span className="text-sm">{error}</span>
              </div>
            ) : totalChunks === 0 ? (
              <div className="flex items-center justify-center py-20 text-text-tertiary">
                <span className="text-sm">No chunks available for this document.</span>
              </div>
            ) : activeTab === 'document' ? (
              renderedMarkdown ? (
                <div className="px-8 py-7">
                  <div className="max-w-4xl">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={markdownComponents}
                    >
                      {renderedMarkdown}
                    </ReactMarkdown>
                  </div>
                </div>
              ) : data?.markdown_available ? (
                <div className="flex flex-col items-center justify-center py-20 text-text-tertiary gap-2">
                  <FileText className="h-8 w-8" />
                  <span className="text-sm">
                    Document preview is too large to render
                    {data.markdown_size ? ` (${(data.markdown_size / 1024 / 1024).toFixed(1)} MB)` : ''}.
                  </span>
                  <span className="text-xs">Use the Chunks tab to browse individual sections.</span>
                </div>
              ) : (
                <div className="flex items-center justify-center py-20 text-text-tertiary">
                  <span className="text-sm">No document preview available. Use the Chunks tab.</span>
                </div>
              )
            ) : (
              <div className="p-4 space-y-3">
                {data?.summary && (
                  <div className="rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 flex items-start gap-3">
                    <Sparkles className="h-4 w-4 text-primary mt-0.5 flex-shrink-0" />
                    <div className="min-w-0">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-primary/70">
                        Document Summary
                      </span>
                      <p className="text-sm text-text-secondary mt-1 leading-relaxed">
                        {data.summary}
                      </p>
                    </div>
                  </div>
                )}
                {allChunks.map((chunk) => (
                  <ChunkCard
                    key={chunk.id}
                    chunk={chunk}
                    index={chunk.chunk_index}
                    total={totalChunks}
                  />
                ))}
                {hasMoreChunks && (
                  <button
                    onClick={loadMoreChunks}
                    disabled={loadingMore}
                    className="w-full py-2.5 rounded-lg border border-white/10 bg-white/[0.03] hover:bg-white/[0.06] text-sm text-text-secondary hover:text-white transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
                  >
                    {loadingMore ? (
                      <>
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        Loading...
                      </>
                    ) : (
                      <>
                        Load more chunks ({allChunks.length} of {totalChunks})
                      </>
                    )}
                  </button>
                )}
              </div>
            )}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );

  return createPortal(modal, document.body);
}
