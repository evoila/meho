// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * DocumentList
 *
 * Compact document list for the knowledge tree. Displays individual
 * documents with status, metadata, and inline delete confirmation.
 * Used by GlobalSection and InstanceNode to show per-scope documents.
 */
import { useState, useCallback } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  FileText, Trash2, Loader2, AlertCircle, RotateCcw, Square, Plus, History, X,
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { KnowledgeDocument, KnowledgeScope, DocumentVersion } from '../../../api/types/knowledge';
import { getKnowledgeClient } from '@/api/clients/knowledge';
import { DocumentPreviewModal } from './DocumentPreviewModal';
import { KnowledgeUploadDialog } from './KnowledgeUploadDialog';

const STATUS_STYLES: Record<string, string> = {
  completed: 'bg-green-400/10 text-green-400 border-green-400/20',
  processing: 'bg-blue-400/10 text-blue-400 border-blue-400/20',
  pending: 'bg-amber-400/10 text-amber-400 border-amber-400/20',
  failed: 'bg-red-400/10 text-red-400 border-red-400/20',
};

interface DocumentListProps {
  documents: KnowledgeDocument[];
  onDelete: (documentId: string) => void;
  deletingId: string | null;
  onResume?: () => void;
  /** Scope used as fallback when a document has no family (legacy) or for invalidation context. */
  scope?: KnowledgeScope;
}

interface VersionHistoryPopoverProps {
  familyId: string;
  familyName: string;
  currentDocumentId: string;
  onClose: () => void;
  onDelete: (versionJobId: string) => void;
  deletingId: string | null;
}

function VersionHistoryPopover({
  familyId,
  familyName,
  currentDocumentId,
  onClose,
  onDelete,
  deletingId,
}: Readonly<VersionHistoryPopoverProps>) {
  const knowledgeClient = getKnowledgeClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['knowledge-family-versions', familyId],
    queryFn: () => knowledgeClient.listDocumentVersions(familyId),
  });

  const versions: DocumentVersion[] = data?.versions ?? [];

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.98 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Version history"
    >
      <div className="w-full max-w-lg glass border border-white/10 rounded-xl p-5 space-y-4">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-white font-semibold text-sm">Version history</h3>
            <p className="text-xs text-text-tertiary mt-0.5 truncate max-w-md">{familyName}</p>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-md hover:bg-white/10 text-text-secondary hover:text-white transition-colors"
            aria-label="Close version history"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {isLoading && (
          <div className="flex items-center gap-2 py-4">
            <Loader2 className="h-4 w-4 animate-spin text-text-tertiary" />
            <span className="text-xs text-text-tertiary">Loading versions...</span>
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2 text-red-400 bg-red-400/10 p-2 rounded-lg border border-red-400/20 text-xs">
            <AlertCircle className="h-3.5 w-3.5" />
            <span>{error instanceof Error ? error.message : 'Failed to load versions'}</span>
          </div>
        )}

        {!isLoading && !error && versions.length === 0 && (
          <p className="text-xs text-text-tertiary py-2">No versions found.</p>
        )}

        {!isLoading && !error && versions.length > 0 && (
          <ul className="space-y-1.5 max-h-80 overflow-y-auto">
            {versions.map((v) => {
              const isCurrent = v.job_id === currentDocumentId;
              const isDeletingThis = deletingId === v.job_id;
              return (
                <li
                  key={v.job_id}
                  className={clsx(
                    'flex items-center gap-3 rounded-lg border border-white/10 px-3 py-2 bg-white/[0.02]',
                    isCurrent && 'ring-1 ring-primary/40',
                  )}
                >
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-accent/30 bg-accent/10 text-accent text-[10px] font-mono">
                    {v.doc_version || 'unversioned'}
                  </span>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-white truncate">{v.filename || 'Untitled'}</p>
                    <p className="text-[10px] text-text-tertiary">
                      {new Date(v.started_at).toLocaleString()} -- {v.chunks_created} chunk{v.chunks_created !== 1 ? 's' : ''}
                    </p>
                  </div>
                  <button
                    onClick={() => onDelete(v.job_id)}
                    disabled={isDeletingThis}
                    className="p-1 text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-md transition-colors disabled:opacity-50"
                    title="Delete this version"
                  >
                    {isDeletingThis ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" />
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </motion.div>
  );
}

interface NewVersionModalProps {
  documentId: string;
  familyName: string;
  scope: KnowledgeScope;
  onClose: () => void;
}

function NewVersionModal({ documentId, familyName, scope, onClose }: Readonly<NewVersionModalProps>) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Upload new version"
    >
      <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto glass border border-white/10 rounded-xl p-5">
        <div className="flex items-start justify-between mb-4">
          <h3 className="text-white font-semibold text-sm">Upload new version</h3>
          <button
            onClick={onClose}
            className="p-1 rounded-md hover:bg-white/10 text-text-secondary hover:text-white transition-colors"
            aria-label="Close upload dialog"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <KnowledgeUploadDialog
          scope={scope}
          targetDocumentId={documentId}
          targetFamilyName={familyName}
          onSuccess={onClose}
          inline
        />
      </div>
    </motion.div>
  );
}

export function DocumentList({
  documents,
  onDelete,
  deletingId,
  onResume,
  scope,
}: Readonly<DocumentListProps>) { // NOSONAR (cognitive complexity)
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [resumingId, setResumingId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [historyDoc, setHistoryDoc] = useState<KnowledgeDocument | null>(null);
  const [newVersionDoc, setNewVersionDoc] = useState<KnowledgeDocument | null>(null);

  const knowledgeClient = getKnowledgeClient();
  const queryClient = useQueryClient();

  const handleResume = useCallback(async (docId: string) => {
    setResumingId(docId);
    try {
      await knowledgeClient.resumeJob(docId);
      onResume?.();
    } catch {
      // Resume request failed -- the document list will refresh and show the error.
    } finally {
      setResumingId(null);
    }
  }, [knowledgeClient, onResume]);

  const handleCancel = useCallback(async (docId: string) => {
    setCancellingId(docId);
    try {
      await knowledgeClient.cancelJob(docId);
      onResume?.();
    } catch {
      // Cancel request failed -- polling will pick up actual state.
    } finally {
      setCancellingId(null);
    }
  }, [knowledgeClient, onResume]);

  const handleDeleteVersion = useCallback(async (versionJobId: string) => {
    try {
      await knowledgeClient.deleteKnowledgeDocument(versionJobId);
      if (historyDoc?.family_id) {
        void queryClient.invalidateQueries({ queryKey: ['knowledge-family-versions', historyDoc.family_id] });
      }
      onResume?.();
    } catch {
      // Error is surfaced via the parent's delete flow on next refetch.
    }
  }, [knowledgeClient, historyDoc?.family_id, onResume, queryClient]);

  if (documents.length === 0) {
    return (
      <p className="text-xs text-text-tertiary py-1">
        No documents uploaded yet.
      </p>
    );
  }

  const handleDelete = (id: string) => {
    onDelete(id);
    setConfirmId(null);
  };

  const resolvedScope: KnowledgeScope = scope ?? { scope_type: 'global' };

  return (
    <div className="space-y-1.5">
      <AnimatePresence mode="popLayout">
        {documents.map((doc) => {
          const statusClass =
            STATUS_STYLES[doc.status] ?? 'bg-white/5 text-text-secondary border-white/10';
          const isDeleting = deletingId === doc.id;
          const versionCount = doc.version_count ?? 1;

          return (
            <motion.div
              key={doc.id}
              layout
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className={clsx(
                'rounded-lg border border-white/5 bg-white/[0.02] hover:border-white/10 transition-colors group',
                doc.status === 'completed' && 'cursor-pointer',
              )}
            >
              <div
                className="flex items-center gap-2.5 px-3 py-2"
                onClick={() => { if (doc.status === 'completed') setPreviewId(doc.id); }}
                role={doc.status === 'completed' ? 'button' : undefined}
                tabIndex={doc.status === 'completed' ? 0 : undefined}
                onKeyDown={(e) => {
                  if (doc.status === 'completed' && (e.key === 'Enter' || e.key === ' ')) {
                    e.preventDefault();
                    setPreviewId(doc.id);
                  }
                }}
              >
                <div className="p-1.5 rounded-md bg-primary/10 text-primary flex-shrink-0">
                  <FileText className="h-3 w-3" />
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <p className="text-xs font-medium text-white truncate">
                      {doc.filename || 'Untitled document'}
                    </p>
                    {doc.doc_version && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (doc.family_id) setHistoryDoc(doc);
                        }}
                        disabled={!doc.family_id}
                        className={clsx(
                          'inline-flex items-center gap-0.5 px-1.5 py-px rounded border text-[10px] font-mono transition-colors flex-shrink-0',
                          doc.family_id
                            ? 'bg-accent/10 border-accent/30 text-accent hover:bg-accent/20 cursor-pointer'
                            : 'bg-white/5 border-white/10 text-text-secondary cursor-default',
                        )}
                        title={doc.family_id ? `View version history (${versionCount} version${versionCount !== 1 ? 's' : ''})` : 'Version'}
                      >
                        {doc.doc_version}
                        {versionCount > 1 && doc.family_id && (
                          <span className="ml-0.5 opacity-70">+{versionCount - 1}</span>
                        )}
                      </button>
                    )}
                  </div>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span
                      className={clsx(
                        'text-[10px] font-medium px-1.5 py-px rounded border',
                        statusClass,
                      )}
                    >
                      {doc.status}
                    </span>
                    {doc.chunks_created > 0 && (
                      <span className="text-[10px] text-text-tertiary">
                        {doc.chunks_created} chunk{doc.chunks_created !== 1 ? 's' : ''}
                      </span>
                    )}
                    {doc.file_size != null && (
                      <span className="text-[10px] text-text-tertiary">
                        {(doc.file_size / 1024).toFixed(1)} KB
                      </span>
                    )}
                  </div>
                </div>

                {/* Actions: history / upload-new-version / stop / delete */}
                <div className="flex-shrink-0 flex items-center gap-1">
                  {/* Upload new version (only for completed docs with a family) */}
                  {doc.status === 'completed' && doc.family_id && (
                    <button
                      onClick={(e) => { e.stopPropagation(); setNewVersionDoc(doc); }}
                      className="p-1 text-text-tertiary hover:text-accent hover:bg-accent/10 rounded-md transition-colors opacity-0 group-hover:opacity-100"
                      title="Upload new version"
                    >
                      <Plus className="h-3 w-3" />
                    </button>
                  )}

                  {/* History shortcut (when more than one version exists) */}
                  {doc.family_id && versionCount > 1 && (
                    <button
                      onClick={(e) => { e.stopPropagation(); setHistoryDoc(doc); }}
                      className="p-1 text-text-tertiary hover:text-white hover:bg-white/10 rounded-md transition-colors opacity-0 group-hover:opacity-100"
                      title="View version history"
                    >
                      <History className="h-3 w-3" />
                    </button>
                  )}

                  {doc.status === 'processing' ? (
                    <button
                      onClick={(e) => { e.stopPropagation(); void handleCancel(doc.id); }}
                      disabled={cancellingId === doc.id}
                      className="px-2 py-1 text-[10px] font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-md transition-colors border border-red-500/20 disabled:opacity-50 opacity-0 group-hover:opacity-100"
                      title="Stop processing"
                    >
                      {cancellingId === doc.id ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <span className="flex items-center gap-1"><Square className="h-2.5 w-2.5" />Stop</span>
                      )}
                    </button>
                  ) : confirmId === doc.id ? (
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(doc.id); }}
                        disabled={isDeleting}
                        className="px-2 py-1 text-[10px] font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-md transition-colors border border-red-500/20 disabled:opacity-50"
                      >
                        {isDeleting ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          'Delete'
                        )}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setConfirmId(null); }}
                        disabled={isDeleting}
                        className="px-2 py-1 text-[10px] font-medium text-text-secondary hover:text-white transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); setConfirmId(doc.id); }}
                      className="p-1 text-text-tertiary hover:text-red-400 hover:bg-red-400/10 rounded-md transition-colors opacity-0 group-hover:opacity-100"
                      title="Delete document"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </div>

              {/* Error row */}
              {doc.error && (
                <div className="flex items-center gap-1.5 text-[10px] text-red-400 px-3 pb-2 -mt-0.5">
                  <AlertCircle className="h-3 w-3 flex-shrink-0" />
                  <span className="truncate flex-1">{doc.error}</span>
                  {doc.resumable && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        void handleResume(doc.id);
                      }}
                      disabled={resumingId === doc.id}
                      className="ml-1 px-2 py-0.5 bg-primary/20 hover:bg-primary/30 text-primary border border-primary/30 rounded-md text-[10px] font-medium transition-colors flex-shrink-0 disabled:opacity-50"
                    >
                      {resumingId === doc.id ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <span className="flex items-center gap-1">
                          <RotateCcw className="h-2.5 w-2.5" />
                          Resume
                        </span>
                      )}
                    </button>
                  )}
                </div>
              )}

              {/* Progress bar */}
              {doc.status === 'processing' && doc.progress && (
                <div className="px-3 pb-2 -mt-0.5">
                  <div className="w-full bg-white/10 rounded-full h-1 overflow-hidden">
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
            </motion.div>
          );
        })}
      </AnimatePresence>

      {previewId && (
        <DocumentPreviewModal
          documentId={previewId}
          onClose={() => setPreviewId(null)}
        />
      )}

      <AnimatePresence>
        {historyDoc?.family_id && (
          <VersionHistoryPopover
            familyId={historyDoc.family_id}
            familyName={historyDoc.family_name || historyDoc.filename || 'Document'}
            currentDocumentId={historyDoc.id}
            onClose={() => setHistoryDoc(null)}
            onDelete={handleDeleteVersion}
            deletingId={deletingId}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {newVersionDoc && (
          <NewVersionModal
            documentId={newVersionDoc.id}
            familyName={newVersionDoc.family_name || newVersionDoc.filename || 'Document'}
            scope={resolvedScope}
            onClose={() => {
              setNewVersionDoc(null);
              onResume?.();
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
