// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * InstanceNode
 *
 * Per-connector-instance node in the knowledge tree.
 * Expandable to show uploaded documents with delete capability.
 * Shows instance name, doc count, upload button, and collapsible
 * document list + upload form.
 */
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Server, Plus, FileText, ChevronRight, ChevronDown, Loader2 } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { getAPIClient } from '../../../lib/api-client';
import { config } from '../../../lib/config';
import { KnowledgeUploadDialog } from './KnowledgeUploadDialog';
import { DocumentList } from './DocumentList';

interface InstanceNodeProps {
  connectorId: string;
  connectorName: string;
  documentCount: number;
  chunkCount: number;
}

export function InstanceNode({ connectorId, connectorName, documentCount }: Readonly<InstanceNodeProps>) {
  const [expanded, setExpanded] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const { data: docsData, isLoading: docsLoading } = useQuery({
    queryKey: ['connector-knowledge-documents', connectorId],
    queryFn: () => apiClient.listConnectorDocuments(connectorId, { limit: 200 }),
    enabled: expanded && documentCount > 0,
    refetchInterval: 10000,
  });

  const deleteMutation = useMutation({
    mutationFn: (documentId: string) =>
      apiClient.deleteConnectorDocument(connectorId, documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-knowledge-documents', connectorId] });
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
    <div className="rounded-lg border border-white/5 overflow-hidden">
      {/* Header row */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}
        className="flex items-center gap-3 px-3 py-2 hover:bg-white/5 transition-colors cursor-pointer"
      >
        {(() => {
          if (documentCount === 0) return <span className="w-3 flex-shrink-0" />;
          return expanded
            ? <ChevronDown className="h-3 w-3 text-text-tertiary flex-shrink-0" />
            : <ChevronRight className="h-3 w-3 text-text-tertiary flex-shrink-0" />;
        })()}
        <div className="w-6 h-6 rounded-md bg-white/5 flex items-center justify-center flex-shrink-0">
          <Server className="h-3 w-3 text-text-tertiary" />
        </div>
        <span className="text-sm text-text-primary flex-1 truncate">{connectorName}</span>

        {/* Doc count badge */}
        <span className="flex items-center gap-1 px-1.5 py-0.5 rounded text-xs text-text-tertiary bg-white/5">
          <FileText className="h-2.5 w-2.5" />
          {documentCount}
        </span>

        {/* Upload button */}
        <button
          onClick={(e) => { e.stopPropagation(); setShowUpload(!showUpload); setExpanded(true); }}
          className={clsx(
            'p-1 rounded-md transition-colors',
            showUpload
              ? 'bg-primary/20 text-primary'
              : 'hover:bg-white/10 text-text-tertiary hover:text-white'
          )}
          title={`Upload to ${connectorName}`}
        >
          <Plus className="h-3.5 w-3.5" />
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
            {/* Document list */}
            {documentCount > 0 && (
              <div className="px-3 pb-2 pl-9">
                {docsLoading ? (
                  <div className="flex items-center gap-2 py-1.5">
                    <Loader2 className="h-3 w-3 text-text-tertiary animate-spin" />
                    <span className="text-xs text-text-tertiary">Loading...</span>
                  </div>
                ) : (
                  <DocumentList
                    documents={documents}
                    onDelete={handleDelete}
                    deletingId={deletingId}
                  />
                )}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Upload form */}
      <AnimatePresence>
        {showUpload && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden border-t border-white/5"
          >
            <div className="p-3">
              <KnowledgeUploadDialog
                scope={{ scope_type: 'instance', connector_id: connectorId }}
                onSuccess={() => setShowUpload(false)}
                inline
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
