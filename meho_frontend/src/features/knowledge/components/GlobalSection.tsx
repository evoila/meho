// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * GlobalSection
 *
 * Expandable section for global (org-wide) knowledge in the knowledge tree.
 * Shows doc count, upload button, collapsible upload form, and per-document
 * list with delete capability.
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Globe, ChevronRight, ChevronDown, Plus, FileText, Loader2 } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { getAPIClient } from '../../../lib/api-client';
import { config } from '../../../lib/config';
import { KnowledgeUploadDialog } from './KnowledgeUploadDialog';
import { DocumentList } from './DocumentList';

interface GlobalSectionProps {
  documentCount: number;
  chunkCount: number;
}

export function GlobalSection({ documentCount, chunkCount }: GlobalSectionProps) {
  const [expanded, setExpanded] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const { data: docsData, isLoading: docsLoading } = useQuery({
    queryKey: ['global-knowledge-documents'],
    queryFn: () => apiClient.listKnowledgeDocuments({ limit: 200 }),
    enabled: expanded && documentCount > 0,
    refetchInterval: 10000,
  });

  const deleteMutation = useMutation({
    mutationFn: (documentId: string) => apiClient.deleteKnowledgeDocument(documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['global-knowledge-documents'] });
      queryClient.invalidateQueries({ queryKey: ['knowledge-tree'] });
      setDeletingId(null);
    },
    onError: () => setDeletingId(null),
  });

  const handleDelete = (documentId: string) => {
    setDeletingId(documentId);
    deleteMutation.mutate(documentId);
  };

  const documents = docsData?.documents ?? [];

  return (
    <div className="rounded-xl border border-white/10 overflow-hidden">
      {/* Header */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/5 transition-colors cursor-pointer"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-text-tertiary flex-shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 text-text-tertiary flex-shrink-0" />
        )}
        <div className="w-8 h-8 rounded-lg bg-blue-500/10 border border-blue-500/20 flex items-center justify-center flex-shrink-0">
          <Globe className="h-4 w-4 text-blue-400" />
        </div>
        <span className="text-white font-medium text-sm flex-1 text-left">Global Knowledge</span>

        {/* Doc count badge */}
        <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-white/5 border border-white/10 text-text-secondary text-xs">
          <FileText className="h-3 w-3" />
          {documentCount} doc{documentCount !== 1 ? 's' : ''}
        </span>

        {/* Upload button */}
        <button
          onClick={(e) => { e.stopPropagation(); setShowUpload(!showUpload); setExpanded(true); }}
          className={clsx(
            'p-1.5 rounded-lg transition-colors',
            showUpload
              ? 'bg-primary/20 text-primary'
              : 'hover:bg-white/10 text-text-tertiary hover:text-white'
          )}
          title="Upload to Global"
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>

      {/* Expanded content */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-3 pl-14">
              {chunkCount > 0 && (
                <p className="text-xs text-text-tertiary mb-2">
                  {chunkCount} chunk{chunkCount !== 1 ? 's' : ''} indexed
                </p>
              )}

              {/* Document list */}
              {documentCount > 0 && (
                docsLoading ? (
                  <div className="flex items-center gap-2 py-2">
                    <Loader2 className="h-3 w-3 text-text-tertiary animate-spin" />
                    <span className="text-xs text-text-tertiary">Loading documents...</span>
                  </div>
                ) : (
                  <DocumentList
                    documents={documents}
                    onDelete={handleDelete}
                    deletingId={deletingId}
                  />
                )
              )}

              {documentCount === 0 && !showUpload && (
                <p className="text-xs text-text-tertiary">
                  No global knowledge uploaded yet. Upload docs that apply across all connectors.
                </p>
              )}
            </div>

            {/* Upload form */}
            {showUpload && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                className="overflow-hidden border-t border-white/5"
              >
                <div className="p-4">
                  <KnowledgeUploadDialog
                    scope={{ scope_type: 'global' }}
                    onSuccess={() => setShowUpload(false)}
                    inline
                  />
                </div>
              </motion.div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
