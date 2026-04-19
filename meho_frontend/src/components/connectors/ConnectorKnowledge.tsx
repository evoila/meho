// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorKnowledge Component
 *
 * Knowledge tab content for ConnectorDetails. Manages per-connector documents:
 * - Lists documents uploaded to this connector (from ingestion jobs)
 * - Upload new documents with scope selector (instance or type-level)
 * - Delete documents with confirmation
 * - Empty state with upload prompt
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { FileText, Trash2, Loader2, AlertCircle, Upload, ChevronUp } from 'lucide-react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import { KnowledgeUploadDialog } from '../../features/knowledge/components/KnowledgeUploadDialog';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { KnowledgeScope } from '../../api/types/knowledge';

interface ConnectorKnowledgeProps {
  connectorId: string;
  connectorType: string;
}

export function ConnectorKnowledge({ connectorId, connectorType }: Readonly<ConnectorKnowledgeProps>) {
  const [showUpload, setShowUpload] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState<string | null>(null);
  const [uploadScope, setUploadScope] = useState<'instance' | 'type'>('instance');

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Fetch documents for this connector
  const {
    data: documentsData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['connector-knowledge-documents', connectorId],
    queryFn: () => apiClient.listConnectorDocuments(connectorId, { limit: 200 }),
    refetchInterval: 10000,
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (documentId: string) =>
      apiClient.deleteConnectorDocument(connectorId, documentId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['connector-knowledge-documents', connectorId],
      });
      queryClient.invalidateQueries({ queryKey: ['knowledge-tree'] });
      setShowDeleteConfirm(null);
      setDeletingId(null);
    },
    onError: () => {
      setDeletingId(null);
    },
  });

  const handleDelete = (documentId: string) => {
    setDeletingId(documentId);
    deleteMutation.mutate(documentId);
  };

  const documents = documentsData?.documents ?? [];

  const statusStyles: Record<string, string> = {
    completed: 'bg-green-400/10 text-green-400 border-green-400/20',
    processing: 'bg-blue-400/10 text-blue-400 border-blue-400/20',
    pending: 'bg-amber-400/10 text-amber-400 border-amber-400/20',
    failed: 'bg-red-400/10 text-red-400 border-red-400/20',
  };

  // Build scope for upload dialog
  const uploadKnowledgeScope: KnowledgeScope = uploadScope === 'instance'
    ? { scope_type: 'instance', connector_id: connectorId }
    : { scope_type: 'type', connector_type_scope: connectorType };

  // Pretty connector type name
  const connectorTypeDisplay = {
    rest: 'REST',
    soap: 'SOAP',
    vmware: 'VMware',
    proxmox: 'Proxmox',
    graphql: 'GraphQL',
    grpc: 'gRPC',
    kubernetes: 'Kubernetes',
    email: 'Email',
  }[connectorType] || connectorType.toUpperCase();

  // Loading state
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="h-8 w-8 text-primary animate-spin" />
        <span className="ml-3 text-text-secondary">Loading documents...</span>
      </div>
    );
  }

  // Error state
  if (isError) {
    return (
      <div className="flex items-center gap-3 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
        <AlertCircle className="h-5 w-5 flex-shrink-0" />
        <span>Failed to load documents: {(error as Error).message}</span>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with Upload Toggle */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-white">Knowledge Documents</h3>
          <p className="text-sm text-text-secondary mt-0.5">
            {documents.length} document{documents.length !== 1 ? 's' : ''} uploaded
          </p>
        </div>
        <button
          onClick={() => setShowUpload(!showUpload)}
          className={clsx(
            "flex items-center gap-2 px-4 py-2 rounded-xl font-medium text-sm transition-all",
            showUpload
              ? "bg-white/5 text-text-secondary hover:bg-white/10 border border-white/10"
              : "bg-primary hover:bg-primary-hover text-white shadow-lg shadow-primary/20"
          )}
        >
          {showUpload ? (
            <>
              <ChevronUp className="h-4 w-4" />
              Hide Upload
            </>
          ) : (
            <>
              <Upload className="h-4 w-4" />
              Upload Document
            </>
          )}
        </button>
      </div>

      {/* Upload Section with Scope Selector */}
      <AnimatePresence>
        {showUpload && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="glass rounded-xl border border-white/10 p-6 space-y-4">
              {/* Scope Selector */}
              <div className="space-y-2">
                <span className="text-sm font-medium text-text-secondary">Upload scope</span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setUploadScope('instance')}
                    className={clsx(
                      'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium transition-all border text-left',
                      uploadScope === 'instance'
                        ? 'bg-primary/10 border-primary/50 text-white'
                        : 'bg-surface border-white/10 text-text-secondary hover:bg-white/5'
                    )}
                  >
                    <span className="block font-medium">This connector instance</span>
                    <span className="block text-xs opacity-70 mt-0.5">
                      Knowledge specific to this connector only
                    </span>
                  </button>
                  <button
                    onClick={() => setUploadScope('type')}
                    className={clsx(
                      'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium transition-all border text-left',
                      uploadScope === 'type'
                        ? 'bg-primary/10 border-primary/50 text-white'
                        : 'bg-surface border-white/10 text-text-secondary hover:bg-white/5'
                    )}
                  >
                    <span className="block font-medium">All {connectorTypeDisplay} connectors</span>
                    <span className="block text-xs opacity-70 mt-0.5">
                      Shared across all {connectorTypeDisplay} instances
                    </span>
                  </button>
                </div>
              </div>

              {/* Upload Dialog */}
              <KnowledgeUploadDialog
                scope={uploadKnowledgeScope}
                onSuccess={() => {
                  setShowUpload(false);
                  queryClient.invalidateQueries({
                    queryKey: ['connector-knowledge-documents', connectorId],
                  });
                  queryClient.invalidateQueries({ queryKey: ['knowledge-tree'] });
                }}
                inline
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Empty State */}
      {documents.length === 0 && !showUpload && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-center py-16 bg-surface/50 border border-white/10 rounded-2xl"
        >
          <div className="w-16 h-16 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mx-auto mb-4">
            <FileText className="h-8 w-8 text-primary" />
          </div>
          <p className="text-white font-medium mb-2">No knowledge documents uploaded</p>
          <p className="text-sm text-text-secondary max-w-md mx-auto mb-6">
            Upload a document to help the agent understand this system better.
            Supported formats: PDF.
          </p>
          <button
            onClick={() => setShowUpload(true)}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-primary hover:bg-primary-hover text-white rounded-xl font-medium transition-all shadow-lg shadow-primary/20"
          >
            <Upload className="h-4 w-4" />
            Upload Document
          </button>
        </motion.div>
      )}

      {/* Document List */}
      {documents.length > 0 && (
        <div className="space-y-3">
          <AnimatePresence mode="popLayout">
            {documents.map((doc) => {
              const statusClass =
                statusStyles[doc.status] ??
                'bg-white/5 text-text-secondary border-white/10';
              const isDeleting = deletingId === doc.id && deleteMutation.isPending;

              return (
                <motion.div
                  key={doc.id}
                  layout
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  className="glass rounded-xl p-5 border border-white/10 hover:border-primary/30 transition-all group"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-3 mb-2">
                        <div className="p-2 rounded-lg bg-primary/10 text-primary">
                          <FileText className="h-4 w-4" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <h4 className="font-medium text-white truncate text-sm">
                            {doc.filename || 'Untitled document'}
                          </h4>
                          <div className="flex items-center gap-2 mt-1">
                            <span
                              className={clsx(
                                'text-xs font-medium px-2 py-0.5 rounded-md border',
                                statusClass
                              )}
                            >
                              {doc.status}
                            </span>
                            <span className="text-xs text-text-tertiary">
                              {new Date(doc.started_at).toLocaleDateString()}
                            </span>
                          </div>
                        </div>
                      </div>

                      <div className="grid grid-cols-3 gap-4 text-sm mt-3">
                        <div>
                          <span className="block text-text-tertiary text-xs mb-0.5">
                            Chunks
                          </span>
                          <span className="text-white font-medium">
                            {doc.chunks_created ?? 0}
                          </span>
                        </div>
                        <div>
                          <span className="block text-text-tertiary text-xs mb-0.5">
                            Size
                          </span>
                          <span className="text-white font-medium">
                            {doc.file_size
                              ? `${(doc.file_size / 1024).toFixed(1)} KB`
                              : '\u2014'}
                          </span>
                        </div>
                        <div>
                          <span className="block text-text-tertiary text-xs mb-0.5">
                            Type
                          </span>
                          <span className="text-white font-medium capitalize">
                            {doc.knowledge_type}
                          </span>
                        </div>
                      </div>

                      {/* Progress bar for processing documents */}
                      {doc.status === 'processing' && doc.progress && (
                        <div className="mt-3 space-y-1.5 bg-white/5 p-3 rounded-lg border border-white/5">
                          <div className="flex items-center justify-between text-sm">
                            <span className="text-white font-medium text-xs">
                              {doc.progress.status_message || 'Processing...'}
                            </span>
                            <span className="text-text-secondary text-xs">
                              {Math.round(
                                (doc.progress.overall_progress || 0) * 100
                              )}
                              %
                            </span>
                          </div>
                          <div className="w-full bg-white/10 rounded-full h-1.5 overflow-hidden">
                            <motion.div
                              className="bg-primary h-full rounded-full"
                              initial={{ width: 0 }}
                              animate={{
                                width: `${Math.round((doc.progress.overall_progress || 0) * 100)}%`,
                              }}
                              transition={{ duration: 0.5 }}
                            />
                          </div>
                        </div>
                      )}

                      {/* Error message */}
                      {doc.error && (
                        <div className="flex items-center gap-2 text-sm text-red-400 mt-3 bg-red-400/10 p-2.5 rounded-lg border border-red-400/20">
                          <AlertCircle className="h-4 w-4 flex-shrink-0" />
                          <span className="text-xs">{doc.error}</span>
                        </div>
                      )}

                      {/* Tags */}
                      {doc.tags && doc.tags.length > 0 && (
                        <div className="flex flex-wrap gap-1.5 mt-3">
                          {doc.tags.map((tag) => (
                            <span
                              key={tag}
                              className="inline-flex items-center px-2 py-0.5 bg-white/5 border border-white/10 text-text-secondary rounded-md text-xs"
                            >
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Delete button */}
                    <div className="flex-shrink-0">
                      {showDeleteConfirm === doc.id ? (
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => handleDelete(doc.id)}
                            disabled={isDeleting}
                            className="px-3 py-1.5 text-xs font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors border border-red-500/20 disabled:opacity-50"
                          >
                            {isDeleting ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              'Delete'
                            )}
                          </button>
                          <button
                            onClick={() => setShowDeleteConfirm(null)}
                            disabled={isDeleting}
                            className="px-3 py-1.5 text-xs font-medium text-text-secondary hover:text-white transition-colors"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setShowDeleteConfirm(doc.id)}
                          className="p-2 text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-lg transition-colors opacity-0 group-hover:opacity-100"
                          title="Delete document"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      )}
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}
