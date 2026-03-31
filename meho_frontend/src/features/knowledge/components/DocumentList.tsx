// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * DocumentList
 *
 * Compact document list for the knowledge tree. Displays individual
 * documents with status, metadata, and inline delete confirmation.
 * Used by GlobalSection and InstanceNode to show per-scope documents.
 */
import { useState } from 'react';
import { FileText, Trash2, Loader2, AlertCircle } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { KnowledgeDocument } from '../../../api/types/knowledge';

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
}

export function DocumentList({ documents, onDelete, deletingId }: DocumentListProps) {
  const [confirmId, setConfirmId] = useState<string | null>(null);

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

  return (
    <div className="space-y-1.5">
      <AnimatePresence mode="popLayout">
        {documents.map((doc) => {
          const statusClass =
            STATUS_STYLES[doc.status] ?? 'bg-white/5 text-text-secondary border-white/10';
          const isDeleting = deletingId === doc.id;

          return (
            <motion.div
              key={doc.id}
              layout
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="rounded-lg border border-white/5 bg-white/[0.02] hover:border-white/10 transition-colors group"
            >
              <div className="flex items-center gap-2.5 px-3 py-2">
                <div className="p-1.5 rounded-md bg-primary/10 text-primary flex-shrink-0">
                  <FileText className="h-3 w-3" />
                </div>

                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-white truncate">
                    {doc.filename || 'Untitled document'}
                  </p>
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

                {/* Delete / confirm */}
                <div className="flex-shrink-0">
                  {confirmId === doc.id ? (
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={() => handleDelete(doc.id)}
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
                        onClick={() => setConfirmId(null)}
                        disabled={isDeleting}
                        className="px-2 py-1 text-[10px] font-medium text-text-secondary hover:text-white transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setConfirmId(doc.id)}
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
                  <span className="truncate">{doc.error}</span>
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
    </div>
  );
}
