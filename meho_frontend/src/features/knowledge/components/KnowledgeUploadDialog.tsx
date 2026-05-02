// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * KnowledgeUploadDialog
 *
 * Reusable scope-aware upload dialog that handles all three knowledge scopes:
 * - Global: org-wide knowledge
 * - Type: shared across all instances of a connector type
 * - Instance: specific to one connector
 *
 * Supports multi-file PDF upload and single URL ingestion.
 * Used by both KnowledgePage tree and ConnectorKnowledge tab.
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  Upload, X, FileText, Loader2, CheckCircle, AlertCircle,
  Clock, Tag, Link2,
} from 'lucide-react';
import { getKnowledgeClient } from '@/api/clients/knowledge';
import type { IngestionJobStatus, KnowledgeScope } from '../../../api/types/knowledge';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';

interface KnowledgeUploadDialogProps {
  scope: KnowledgeScope;
  onSuccess?: () => void;
  /** If true, render as embedded section (no dialog chrome) */
  inline?: boolean;
  /**
   * New-version upload mode: when set, the dialog uploads a new version
   * into the existing document family that contains `targetDocumentId`.
   * The scope, knowledge type and tags are inherited from the family.
   */
  targetDocumentId?: string;
  targetFamilyName?: string;
}

type TabId = 'file' | 'url';
type QueueItemStatus = 'pending' | 'uploading' | 'processing' | 'completed' | 'error';

interface UploadQueueItem {
  localId: string;
  file: File;
  fileSignature: string;
  status: QueueItemStatus;
  progress: number;
  jobId?: string;
  jobStatus?: IngestionJobStatus | null;
  error?: string | null;
}

interface BatchUploadConfig {
  knowledgeType: 'documentation' | 'procedure';
  tags: string[];
  docVersion: string;
}

const FILE_UPLOAD_CONCURRENCY = 2;

function createLocalId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function buildFileSignature(file: File): string {
  return `${file.name}-${file.size}-${file.lastModified}`;
}

function createQueueItem(
  file: File,
  status: QueueItemStatus = 'pending',
  error: string | null = null,
): UploadQueueItem {
  return {
    localId: createLocalId(),
    file,
    fileSignature: buildFileSignature(file),
    status,
    progress: status === 'completed' ? 100 : 0,
    error,
  };
}

function getJobProgress(job: IngestionJobStatus | null | undefined): number {
  if (!job?.progress) return 0;
  const progressValue = job.progress.overall_progress !== undefined
    ? job.progress.overall_progress * 100
    : job.progress.percent;
  return Math.round(progressValue);
}

function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / 1024).toFixed(1)} KB`;
}

/**
 * Extract a human-readable message from a thrown error. Prefers FastAPI's
 * `response.data.detail` (where the 409/400 explanation lives), then the
 * axios/js Error `message`, and finally a caller-provided fallback.
 */
function extractErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object') {
    const anyErr = err as {
      response?: { data?: { detail?: unknown; message?: unknown } };
      message?: string;
    };
    const detail = anyErr.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === 'object' && typeof (first as { msg?: unknown }).msg === 'string') {
        return (first as { msg: string }).msg;
      }
    }
    const dataMessage = anyErr.response?.data?.message;
    if (typeof dataMessage === 'string' && dataMessage.trim()) return dataMessage;
    if (typeof anyErr.message === 'string' && anyErr.message.trim()) return anyErr.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

function getQueueItemLabel(status: QueueItemStatus, stage?: string, hasJobId?: boolean): string {
  if (status === 'error' && !hasJobId) return 'Invalid';
  if (stage) {
    const stageMap: Record<string, string> = {
      uploading: 'Uploading',
      extracting: 'Extracting',
      chunking: 'Chunking',
      embedding: 'Embedding',
      storing: 'Storing',
      completed: 'Complete',
      failed: 'Failed',
    };
    return stageMap[stage] || stage;
  }
  return {
    pending: 'Ready',
    uploading: 'Uploading',
    processing: 'Processing',
    completed: 'Complete',
    error: 'Failed',
  }[status];
}

function UploadQueueRow({
  item,
  formatETA,
  onRemove,
  onResume,
  onCancel,
}: Readonly<{
  item: UploadQueueItem;
  formatETA: (isoString: string) => string;
  onRemove: (localId: string) => void;
  onResume?: (localId: string) => void;
  onCancel?: (localId: string) => void;
}>) {
  const isActive = item.status === 'uploading' || item.status === 'processing';
  const canRemove = !isActive;
  const stage = item.jobStatus?.progress?.current_stage;
  const statusLabel = getQueueItemLabel(item.status, stage, Boolean(item.jobId));
  const statusClass = item.status === 'completed'
    ? 'bg-green-400/10 text-green-400 border-green-400/20'
    : item.status === 'error'
      ? 'bg-red-400/10 text-red-400 border-red-400/20'
      : item.status === 'pending'
        ? 'bg-white/5 text-text-secondary border-white/10'
        : 'bg-primary/10 text-primary border-primary/20';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.98 }}
      className="bg-surface border border-white/10 rounded-xl p-4 space-y-3"
    >
      <div className="flex items-start gap-3">
        <div className={clsx(
          'mt-0.5 p-2 rounded-lg border flex-shrink-0',
          item.status === 'completed'
            ? 'bg-green-400/10 text-green-400 border-green-400/20'
            : item.status === 'error'
              ? 'bg-red-400/10 text-red-400 border-red-400/20'
              : 'bg-primary/10 text-primary border-primary/20',
        )}>
          {item.status === 'completed' ? (
            <CheckCircle className="h-4 w-4" />
          ) : item.status === 'error' ? (
            <AlertCircle className="h-4 w-4" />
          ) : item.status === 'uploading' || item.status === 'processing' ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <FileText className="h-4 w-4" />
          )}
        </div>

        <div className="flex-1 min-w-0 space-y-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <p className="text-sm font-medium text-white truncate">
                  {item.file.name}
                </p>
                <span className={clsx(
                  'inline-flex items-center px-2 py-0.5 rounded-full border text-[10px] font-medium',
                  statusClass,
                )}>
                  {statusLabel}
                </span>
                <span className="text-xs text-text-tertiary">
                  {formatFileSize(item.file.size)}
                </span>
              </div>
              {item.jobStatus?.progress?.status_message && isActive && (
                <p className="text-xs text-text-secondary mt-1">
                  {item.jobStatus.progress.status_message}
                </p>
              )}
            </div>

            {isActive && item.jobId && onCancel ? (
              <button
                onClick={() => onCancel(item.localId)}
                className="px-2 py-1 text-[10px] font-medium bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-md transition-colors border border-red-500/20 flex-shrink-0"
              >
                Stop
              </button>
            ) : canRemove ? (
              <button
                onClick={() => onRemove(item.localId)}
                className="p-1 hover:bg-white/10 rounded-full transition-colors flex-shrink-0"
                aria-label={`Remove ${item.file.name}`}
              >
                <X className="h-4 w-4 text-text-secondary" />
              </button>
            ) : null}
          </div>

          {item.status !== 'pending' && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-xs text-text-tertiary">
                  {item.status === 'completed'
                    ? 'Completed'
                    : item.status === 'error'
                      ? 'Stopped'
                      : 'Progress'}
                </span>
                <span className="text-xs text-text-secondary">
                  {item.progress}%
                </span>
              </div>
              <div className="w-full bg-white/5 rounded-full h-1.5 overflow-hidden">
                <motion.div
                  className={clsx(
                    'h-full rounded-full',
                    item.status === 'error'
                      ? 'bg-red-500'
                      : item.status === 'completed'
                        ? 'bg-green-500'
                        : 'bg-gradient-to-r from-primary to-accent',
                  )}
                  initial={{ width: 0 }}
                  animate={{ width: `${item.progress}%` }}
                  transition={{ duration: 0.4 }}
                />
              </div>
            </div>
          )}

          {item.jobStatus?.progress?.estimated_completion && isActive && (
            <div className="text-xs text-text-tertiary flex items-center gap-1">
              <Clock className="h-3 w-3" />
              ETA: {formatETA(item.jobStatus.progress.estimated_completion)}
            </div>
          )}

          {item.status === 'completed' && item.jobStatus?.progress?.chunks_created ? (
            <div className="flex items-center gap-2 text-green-400 bg-green-400/10 p-2 rounded-lg border border-green-400/20 text-xs">
              <CheckCircle className="h-3.5 w-3.5" />
              <span>{item.jobStatus.progress.chunks_created} chunks created.</span>
            </div>
          ) : null}

          {item.error ? (
            <div className="flex items-center gap-2 text-red-400 bg-red-400/10 p-2 rounded-lg border border-red-400/20 text-xs">
              <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
              <span className="flex-1">{item.error}</span>
              {item.jobStatus?.resumable && onResume && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onResume(item.localId);
                  }}
                  className="ml-2 px-2.5 py-1 bg-primary/20 hover:bg-primary/30 text-primary border border-primary/30 rounded-lg text-xs font-medium transition-colors flex-shrink-0"
                >
                  Resume
                </button>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </motion.div>
  );
}

export function KnowledgeUploadDialog({
  scope,
  onSuccess,
  inline,
  targetDocumentId,
  targetFamilyName,
}: Readonly<KnowledgeUploadDialogProps>) { // NOSONAR (cognitive complexity)
  const isNewVersionMode = Boolean(targetDocumentId);
  const [activeTab, setActiveTab] = useState<TabId>('file');
  const [uploadItems, setUploadItems] = useState<UploadQueueItem[]>([]);
  const [isBatchActive, setIsBatchActive] = useState(false);
  const [urlInput, setUrlInput] = useState('');
  const [knowledgeType, setKnowledgeType] = useState<'documentation' | 'procedure'>('documentation');
  const [tags, setTags] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState('');
  const [docVersion, setDocVersion] = useState('');
  const [urlUploading, setUrlUploading] = useState(false);
  const [urlProgress, setUrlProgress] = useState(0);
  const [urlStatus, setUrlStatus] = useState<'idle' | 'uploading' | 'processing' | 'completed' | 'error'>('idle');
  const [urlError, setUrlError] = useState<string | null>(null);
  const [urlJobStatus, setUrlJobStatus] = useState<IngestionJobStatus | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const filePollIntervalRef = useRef<number | null>(null);
  const urlPollIntervalRef = useRef<number | null>(null);
  const filePollInFlightRef = useRef<Set<string>>(new Set());
  const urlPollInFlightRef = useRef(false);
  const startingUploadsRef = useRef<Set<string>>(new Set());
  const batchConfigRef = useRef<BatchUploadConfig | null>(null);
  const successNotifiedRef = useRef(false);

  const knowledgeClient = getKnowledgeClient();
  const queryClient = useQueryClient();

  useEffect(() => {
    return () => {
      if (filePollIntervalRef.current) {
        clearInterval(filePollIntervalRef.current);
      }
      if (urlPollIntervalRef.current) {
        clearInterval(urlPollIntervalRef.current);
      }
    };
  }, []);

  const invalidateKnowledgeQueries = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['knowledge-tree'] });
    if (scope.connector_id) {
      void queryClient.invalidateQueries({
        queryKey: ['connector-knowledge-documents', scope.connector_id],
      });
    }
  }, [queryClient, scope.connector_id]);

  const addFilesToQueue = useCallback((selectedFiles: File[]) => {
    if (selectedFiles.length === 0) return;

    successNotifiedRef.current = false;
    setUploadItems((prev) => {
      const existing = new Set(prev.map((item) => item.fileSignature));
      const next = [...prev];

      for (const selectedFile of selectedFiles) {
        const fileSignature = buildFileSignature(selectedFile);
        if (existing.has(fileSignature)) {
          continue;
        }
        existing.add(fileSignature);

        const ext = selectedFile.name.split('.').pop()?.toLowerCase();
        if (ext !== 'pdf') {
          next.push(
            createQueueItem(
              selectedFile,
              'error',
              'Only PDF files are supported. Convert HTML/DOCX to PDF first.',
            ),
          );
          continue;
        }

        next.push(createQueueItem(selectedFile));
      }

      return next;
    });
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (isBatchActive) return;
    addFilesToQueue(Array.from(e.dataTransfer.files));
  }, [addFilesToQueue, isBatchActive]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const addTag = useCallback(() => {
    const nextTag = tagInput.trim();
    if (nextTag && !tags.includes(nextTag)) {
      setTags((prev) => [...prev, nextTag]);
      setTagInput('');
    }
  }, [tagInput, tags]);

  const removeTag = useCallback((tag: string) => {
    setTags((prev) => prev.filter((t) => t !== tag));
  }, []);

  const removeUploadItem = useCallback((localId: string) => {
    setUploadItems((prev) => prev.filter((item) => item.localId !== localId));
  }, []);

  const handleCancelItem = useCallback(async (localId: string) => {
    const item = uploadItems.find((row) => row.localId === localId);
    if (!item?.jobId) return;

    try {
      await knowledgeClient.cancelJob(item.jobId);
    } catch {
      // Cancel request failed -- polling will pick up the actual state.
    }
  }, [knowledgeClient, uploadItems]);

  const handleResumeItem = useCallback(async (localId: string) => {
    const item = uploadItems.find((row) => row.localId === localId);
    if (!item?.jobId) return;

    setUploadItems((prev) => prev.map((row) =>
      row.localId === localId
        ? { ...row, status: 'processing' as QueueItemStatus, error: null, progress: Math.max(row.progress, 1) }
        : row,
    ));

    try {
      await knowledgeClient.resumeJob(item.jobId);
    } catch (err: unknown) {
      setUploadItems((prev) => prev.map((row) =>
        row.localId === localId
          ? { ...row, status: 'error' as QueueItemStatus, error: extractErrorMessage(err, 'Resume failed') }
          : row,
      ));
    }
  }, [knowledgeClient, uploadItems]);

  const pollFileJobStatus = useCallback(async (localId: string, jobId: string) => {
    if (filePollInFlightRef.current.has(jobId)) return;
    filePollInFlightRef.current.add(jobId);

    try {
      const job = await knowledgeClient.getJobStatus(jobId);
      const nextStatus: QueueItemStatus = job.status === 'completed'
        ? 'completed'
        : job.status === 'failed'
          ? 'error'
          : 'processing';

      let completedNow = false;
      setUploadItems((prev) => prev.map((item) => {
        if (item.localId !== localId) return item;
        completedNow = nextStatus === 'completed' && item.status !== 'completed';
        return {
          ...item,
          status: nextStatus,
          progress: nextStatus === 'completed' ? 100 : getJobProgress(job),
          jobStatus: job,
          error: nextStatus === 'error' ? (job.error || 'Processing failed') : item.error,
        };
      }));

      if (completedNow) {
        invalidateKnowledgeQueries();
      }
    } catch {
      // Ignore polling errors and try again on the next interval tick.
    } finally {
      filePollInFlightRef.current.delete(jobId);
    }
  }, [knowledgeClient, invalidateKnowledgeQueries]);

  const startUploadForItem = useCallback(async (item: UploadQueueItem) => {
    if (startingUploadsRef.current.has(item.localId)) return;

    const batchConfig = batchConfigRef.current;
    if (!batchConfig) return;

    startingUploadsRef.current.add(item.localId);
    setUploadItems((prev) => prev.map((row) => (
      row.localId === item.localId
        ? { ...row, status: 'uploading', progress: 0, error: null, jobStatus: null }
        : row
    )));

    try {
      const response = isNewVersionMode && targetDocumentId
        ? await knowledgeClient.uploadDocumentVersion(targetDocumentId, {
          file: item.file,
          doc_version: batchConfig.docVersion,
        })
        : await knowledgeClient.uploadDocument({
          file: item.file,
          knowledge_type: batchConfig.knowledgeType,
          tags: batchConfig.tags,
          connector_id: scope.connector_id,
          scope_type: scope.scope_type,
          connector_type_scope: scope.connector_type_scope,
          doc_version: batchConfig.docVersion,
        });

      setUploadItems((prev) => prev.map((row) => (
        row.localId === item.localId
          ? { ...row, status: 'processing', progress: Math.max(row.progress, 1), jobId: response.job_id }
          : row
      )));

      await pollFileJobStatus(item.localId, response.job_id);
    } catch (err: unknown) {
      setUploadItems((prev) => prev.map((row) => (
        row.localId === item.localId
          ? {
            ...row,
            status: 'error',
            error: extractErrorMessage(err, 'Upload failed'),
          }
          : row
      )));
    } finally {
      startingUploadsRef.current.delete(item.localId);
    }
  }, [knowledgeClient, pollFileJobStatus, scope, isNewVersionMode, targetDocumentId]);

  const pollUrlJobStatus = useCallback(async (jobId: string) => {
    if (urlPollInFlightRef.current) return;
    urlPollInFlightRef.current = true;

    try {
      const job = await knowledgeClient.getJobStatus(jobId);
      setUrlJobStatus(job);
      setUrlProgress(getJobProgress(job));

      if (job.status === 'completed') {
        setUrlStatus('completed');
        setUrlProgress(100);
        setUrlUploading(false);
        invalidateKnowledgeQueries();
        if (urlPollIntervalRef.current) {
          clearInterval(urlPollIntervalRef.current);
          urlPollIntervalRef.current = null;
        }
        if (onSuccess) {
          setTimeout(onSuccess, 1500);
        }
      } else if (job.status === 'failed') {
        setUrlStatus('error');
        setUrlError(job.error || 'Processing failed');
        setUrlUploading(false);
        if (urlPollIntervalRef.current) {
          clearInterval(urlPollIntervalRef.current);
          urlPollIntervalRef.current = null;
        }
      }
    } catch {
      // Ignore polling errors and retry on the next tick.
    } finally {
      urlPollInFlightRef.current = false;
    }
  }, [knowledgeClient, invalidateKnowledgeQueries, onSuccess]);

  useEffect(() => {
    if (!isBatchActive) {
      if (filePollIntervalRef.current) {
        clearInterval(filePollIntervalRef.current);
        filePollIntervalRef.current = null;
      }
      return;
    }

    const activeCount = uploadItems.filter(
      (item) => item.status === 'uploading' || item.status === 'processing',
    ).length;
    const pendingItems = uploadItems.filter((item) => item.status === 'pending');
    const availableSlots = FILE_UPLOAD_CONCURRENCY - activeCount;

    if (availableSlots > 0) {
      pendingItems.slice(0, availableSlots).forEach((item) => {
        void startUploadForItem(item);
      });
    }

    const pollableItems = uploadItems.filter(
      (item) => item.jobId && (item.status === 'uploading' || item.status === 'processing'),
    );

    if (filePollIntervalRef.current) {
      clearInterval(filePollIntervalRef.current);
      filePollIntervalRef.current = null;
    }

    if (pollableItems.length > 0) {
      filePollIntervalRef.current = window.setInterval(() => {
        pollableItems.forEach((item) => {
          if (item.jobId) {
            void pollFileJobStatus(item.localId, item.jobId);
          }
        });
      }, 1000);
    }

    return () => {
      if (filePollIntervalRef.current) {
        clearInterval(filePollIntervalRef.current);
        filePollIntervalRef.current = null;
      }
    };
  }, [isBatchActive, pollFileJobStatus, startUploadForItem, uploadItems]);

  useEffect(() => {
    if (!isBatchActive) return;

    const hasPending = uploadItems.some((item) => item.status === 'pending');
    const hasActive = uploadItems.some(
      (item) => item.status === 'uploading' || item.status === 'processing',
    );

    if (hasPending || hasActive) return;

    setIsBatchActive(false);
    invalidateKnowledgeQueries();

    const allSuccessful = uploadItems.length > 0
      && uploadItems.every((item) => item.status === 'completed');

    if (allSuccessful && onSuccess && !successNotifiedRef.current) {
      successNotifiedRef.current = true;
      setTimeout(onSuccess, 1500);
    }
  }, [invalidateKnowledgeQueries, isBatchActive, onSuccess, uploadItems]);

  const handleFileUpload = useCallback(() => {
    const hasPendingFiles = uploadItems.some((item) => item.status === 'pending');
    if (!hasPendingFiles) return;
    if (!docVersion.trim()) return;

    batchConfigRef.current = {
      knowledgeType,
      tags: [...tags],
      docVersion: docVersion.trim(),
    };
    successNotifiedRef.current = false;
    setIsBatchActive(true);
  }, [docVersion, knowledgeType, tags, uploadItems]);

  const handleUrlIngest = useCallback(async () => {
    const url = urlInput.trim();
    if (!url) return;
    const normalizedVersion = docVersion.trim();
    if (!normalizedVersion) return;

    setUrlUploading(true);
    setUrlStatus('uploading');
    setUrlError(null);
    setUrlProgress(0);
    setUrlJobStatus(null);

    try {
      const response = await knowledgeClient.ingestUrl({
        url,
        scope_type: scope.scope_type,
        connector_type_scope: scope.connector_type_scope,
        connector_id: scope.connector_id,
        knowledge_type: knowledgeType,
        tags,
        doc_version: normalizedVersion,
      });

      setUrlStatus('processing');
      setUrlUploading(false);

      await pollUrlJobStatus(response.job_id);
      if (urlPollIntervalRef.current) {
        clearInterval(urlPollIntervalRef.current);
      }
      urlPollIntervalRef.current = window.setInterval(() => {
        void pollUrlJobStatus(response.job_id);
      }, 1000);
    } catch (err: unknown) {
      setUrlStatus('error');
      setUrlError(extractErrorMessage(err, 'URL ingestion failed'));
      setUrlUploading(false);
    }
  }, [knowledgeClient, docVersion, knowledgeType, pollUrlJobStatus, scope, tags, urlInput]);

  const reset = useCallback(() => {
    setUploadItems([]);
    setIsBatchActive(false);
    setUrlInput('');
    setKnowledgeType('documentation');
    setTags([]);
    setTagInput('');
    setDocVersion('');
    setUrlUploading(false);
    setUrlProgress(0);
    setUrlStatus('idle');
    setUrlError(null);
    setUrlJobStatus(null);
    batchConfigRef.current = null;
    successNotifiedRef.current = false;
    filePollInFlightRef.current.clear();
    startingUploadsRef.current.clear();

    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
    if (filePollIntervalRef.current) {
      clearInterval(filePollIntervalRef.current);
      filePollIntervalRef.current = null;
    }
    if (urlPollIntervalRef.current) {
      clearInterval(urlPollIntervalRef.current);
      urlPollIntervalRef.current = null;
    }
  }, []);

  const getStageDisplayName = (stage: string): string => {
    const stageMap: Record<string, string> = {
      uploading: 'Uploading',
      extracting: 'Extracting',
      chunking: 'Chunking',
      embedding: 'Embedding',
      storing: 'Storing',
      completed: 'Complete',
      failed: 'Failed',
    };
    return stageMap[stage] || stage;
  };

  // Snapshot time for ETA calculation via lazy state initializer (purity-safe)
  const [etaNow] = useState(() => Date.now());
  const formatETA = (isoString: string): string => {
    const eta = new Date(isoString);
    const diffMs = eta.getTime() - etaNow;
    const diffSec = Math.round(diffMs / 1000);
    if (diffSec < 60) return `${diffSec}s`;
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m`;
    return `${Math.round(diffMin / 60)}h`;
  };

  const scopeLabel =
    scope.scope_type === 'global'
      ? 'Global Knowledge'
      : scope.scope_type === 'type'
        ? `All ${(scope.connector_type_scope || '').charAt(0).toUpperCase() + (scope.connector_type_scope || '').slice(1)} connectors`
        : 'This connector instance';

  const hasPendingFiles = uploadItems.some((item) => item.status === 'pending');
  const isFileBatchRunning = isBatchActive || uploadItems.some(
    (item) => item.status === 'uploading' || item.status === 'processing',
  );
  const hasQueueItems = uploadItems.length > 0;
  const urlHasInput = urlInput.trim().length > 0;
  const shouldShowMetadataControls = (activeTab === 'file'
    ? hasQueueItems && !isFileBatchRunning
    : urlHasInput && urlStatus === 'idle') || (isNewVersionMode && !isFileBatchRunning);
  const fileActiveCount = uploadItems.filter(
    (item) => item.status === 'uploading' || item.status === 'processing',
  ).length;
  const fileCompletedCount = uploadItems.filter((item) => item.status === 'completed').length;
  const fileErrorCount = uploadItems.filter((item) => item.status === 'error').length;
  const showUploadAnother = activeTab === 'file'
    && hasQueueItems
    && !isFileBatchRunning
    && !hasPendingFiles;

  return (
    <div className={clsx('space-y-5', !inline && 'glass rounded-xl border border-white/10 p-6')}>
      {/* Scope indicator */}
      {isNewVersionMode ? (
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <span className="px-2 py-0.5 rounded-md bg-accent/10 border border-accent/20 text-accent font-medium text-xs">
            NEW VERSION
          </span>
          <span className="truncate">
            Adding new version of: <span className="text-white font-medium">{targetFamilyName || 'this document'}</span>
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <span className="px-2 py-0.5 rounded-md bg-primary/10 border border-primary/20 text-primary font-medium text-xs">
            {scope.scope_type.toUpperCase()}
          </span>
          <span>Uploading to: {scopeLabel}</span>
        </div>
      )}

      {/* Tab switcher: File | URL -- only first-version uploads support URL ingestion */}
      {!isNewVersionMode && (
      <div className="flex gap-1 bg-white/5 rounded-lg p-1 border border-white/10">
        {([
          { id: 'file' as const, label: 'File', icon: FileText },
          { id: 'url' as const, label: 'URL', icon: Link2 },
        ]).map((tab) => (
          <button
            key={tab.id}
            onClick={() => {
              setActiveTab(tab.id);
              setUrlError(null);
            }}
            className={clsx(
              'flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-all',
              activeTab === tab.id
                ? 'bg-primary/20 text-white border border-primary/30'
                : 'text-text-secondary hover:text-white hover:bg-white/5',
            )}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>
      )}

      {/* File Upload Tab */}
      {activeTab === 'file' && (
        <div className="space-y-4">
          <div
            role="button"
            tabIndex={isFileBatchRunning ? -1 : 0}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onClick={() => {
              if (!isFileBatchRunning) {
                fileInputRef.current?.click();
              }
            }}
            onKeyDown={(e) => {
              if (!isFileBatchRunning && (e.key === 'Enter' || e.key === ' ')) {
                e.preventDefault();
                fileInputRef.current?.click();
              }
            }}
            className={clsx(
              'border-2 border-dashed rounded-xl p-8 text-center transition-all',
              isFileBatchRunning
                ? 'border-white/10 bg-white/[0.02] opacity-70 cursor-not-allowed'
                : hasQueueItems
                  ? 'border-primary/50 bg-primary/5 cursor-pointer hover:bg-primary/[0.08]'
                  : 'border-white/10 hover:border-primary/50 hover:bg-white/5 cursor-pointer',
            )}
          >
            <AnimatePresence mode="wait">
              {!hasQueueItems ? (
                <motion.div
                  key="empty"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                >
                  <Upload className="h-8 w-8 text-primary mx-auto mb-3" />
                  <p className="text-text-primary font-medium mb-1">
                    Click to browse or drag and drop
                  </p>
                  <p className="text-xs text-text-tertiary">
                    Upload one or more PDF files
                  </p>
                </motion.div>
              ) : (
                <motion.div
                  key="selected"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="space-y-2"
                >
                  <FileText className="h-7 w-7 text-primary mx-auto" />
                  <p className="font-medium text-white text-sm">
                    {uploadItems.length} file{uploadItems.length !== 1 ? 's' : ''} in queue
                  </p>
                  <p className="text-xs text-text-secondary">
                    {isFileBatchRunning
                      ? 'Uploading in progress. Additional files can be added after this batch completes.'
                      : 'Click or drop more PDFs to add them to the queue.'}
                  </p>
                </motion.div>
              )}
            </AnimatePresence>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf"
              multiple
              disabled={isFileBatchRunning}
              onChange={(e) => {
                const selectedFiles = Array.from(e.target.files ?? []);
                addFilesToQueue(selectedFiles);
                e.currentTarget.value = '';
              }}
              className="hidden"
            />
          </div>

          {hasQueueItems && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-text-secondary">
                  Upload Queue
                </span>
                <span className="text-xs text-text-tertiary">
                  {fileCompletedCount} complete
                  {fileErrorCount > 0 ? `, ${fileErrorCount} failed` : ''}
                </span>
              </div>
              <AnimatePresence mode="popLayout">
                {uploadItems.map((item) => (
                  <UploadQueueRow
                    key={item.localId}
                    item={item}
                    formatETA={formatETA}
                    onRemove={removeUploadItem}
                    onResume={handleResumeItem}
                    onCancel={handleCancelItem}
                  />
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>
      )}

      {/* URL Tab */}
      {activeTab === 'url' && (
        <div className="space-y-4">
          <div className="relative">
            <Link2 className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
            <input
              type="url"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              placeholder="https://docs.example.com/..."
              className="w-full pl-10 pr-4 py-2.5 bg-surface/50 border border-white/10 rounded-xl text-text-primary placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all text-sm"
              disabled={urlUploading}
            />
          </div>
        </div>
      )}

      {/* Knowledge Type + Tags */}
      {shouldShowMetadataControls && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
          className="space-y-4"
        >
          {/* Knowledge Type (hidden in new-version mode — inherited from family) */}
          {!isNewVersionMode && (
          <div className="space-y-2">
            <span className="text-sm font-medium text-text-secondary">Knowledge Type</span>
            <div className="grid grid-cols-2 gap-2" role="group" aria-label="Knowledge Type">
              {[
                { id: 'documentation' as const, label: 'Documentation', sub: 'Architecture, References' },
                { id: 'procedure' as const, label: 'Procedure', sub: 'Runbooks, Guides' },
              ].map((opt) => (
                <button
                  key={opt.id}
                  onClick={() => setKnowledgeType(opt.id)}
                  disabled={isFileBatchRunning || urlUploading}
                  className={clsx(
                    'p-3 rounded-xl border text-sm font-medium transition-all text-left',
                    knowledgeType === opt.id
                      ? 'bg-primary/10 border-primary/50 text-white'
                      : 'bg-surface border-white/10 text-text-secondary hover:bg-white/5',
                  )}
                >
                  <span>{opt.label}</span>
                  <span className="block text-xs opacity-70 mt-0.5">{opt.sub}</span>
                </button>
              ))}
            </div>
          </div>
          )}

          {/* Version (required) */}
          <div className="space-y-2">
            <label htmlFor="knowledge-upload-version" className="text-sm font-medium text-text-secondary">
              Version <span className="text-red-400 font-normal">*</span>
            </label>
            <input
              id="knowledge-upload-version"
              type="text"
              value={docVersion}
              onChange={(e) => setDocVersion(e.target.value)}
              placeholder="e.g. v8, v9, 1.0.0"
              disabled={isFileBatchRunning || urlUploading}
              className="w-full px-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all text-sm"
            />
            <p className="text-xs text-text-tertiary">
              Required. Must be unique within this document family.
            </p>
          </div>

          {/* Tags (hidden in new-version mode — inherited from family) */}
          {!isNewVersionMode && (
          <div className="space-y-2">
            <label htmlFor="knowledge-upload-tags" className="text-sm font-medium text-text-secondary">
              Tags
            </label>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Tag className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
                <input
                  id="knowledge-upload-tags"
                  type="text"
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addTag();
                    }
                  }}
                  placeholder="Add tags..."
                  disabled={isFileBatchRunning || urlUploading}
                  className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all text-sm"
                />
              </div>
              <button
                onClick={addTag}
                disabled={!tagInput.trim() || isFileBatchRunning || urlUploading}
                className="px-3 py-2 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-white font-medium transition-all disabled:opacity-50 text-sm"
              >
                Add
              </button>
            </div>
            {tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {tags.map((tag) => (
                  <span
                    key={tag}
                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary/10 border border-primary/20 text-primary-light rounded-lg text-xs"
                  >
                    {tag}
                    <button
                      onClick={() => removeTag(tag)}
                      disabled={isFileBatchRunning || urlUploading}
                      className="hover:bg-primary/20 rounded-full p-0.5"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
          )}
        </motion.div>
      )}

      {/* URL Progress */}
      {activeTab === 'url' && urlStatus !== 'idle' && (
        <div className="bg-surface border border-white/10 rounded-xl p-4 space-y-3">
          {urlJobStatus?.progress?.current_stage && (
            <div className="flex items-center gap-3 text-xs overflow-x-auto pb-1 scrollbar-hide">
              {['uploading', 'extracting', 'chunking', 'embedding', 'storing'].map((stage) => {
                const stages = ['uploading', 'extracting', 'chunking', 'embedding', 'storing'];
                const currentIndex = stages.indexOf(urlJobStatus.progress.current_stage || '');
                const stageIndex = stages.indexOf(stage);
                const isComplete = stageIndex < currentIndex;
                const isCurrent = stage === urlJobStatus.progress.current_stage;
                return (
                  <span
                    key={stage}
                    className={clsx(
                      'flex items-center gap-1 whitespace-nowrap',
                      isComplete ? 'text-green-400' : isCurrent ? 'text-primary font-medium' : 'text-text-tertiary',
                    )}
                  >
                    {isComplete ? <CheckCircle className="h-3 w-3" /> : isCurrent ? <Loader2 className="h-3 w-3 animate-spin" /> : <div className="w-3 h-3 rounded-full border border-current opacity-50" />}
                    {getStageDisplayName(stage)}
                  </span>
                );
              })}
            </div>
          )}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs font-medium text-white">
                {urlJobStatus?.progress?.status_message || (
                  urlStatus === 'uploading' ? 'Uploading...' :
                  urlStatus === 'processing' ? 'Processing...' :
                  urlStatus === 'completed' ? 'Completed!' :
                  'Failed'
                )}
              </span>
              <span className="text-xs text-text-secondary">{urlProgress}%</span>
            </div>
            <div className="w-full bg-white/5 rounded-full h-1.5 overflow-hidden">
              <motion.div
                className={clsx('h-full rounded-full', urlStatus === 'error' ? 'bg-red-500' : 'bg-gradient-to-r from-primary to-accent')}
                initial={{ width: 0 }}
                animate={{ width: `${urlProgress}%` }}
                transition={{ duration: 0.5 }}
              />
            </div>
            {urlJobStatus?.progress?.estimated_completion && (
              <div className="mt-1 text-xs text-text-tertiary flex items-center gap-1">
                <Clock className="h-3 w-3" />
                ETA: {formatETA(urlJobStatus.progress.estimated_completion)}
              </div>
            )}
          </div>

          {urlStatus === 'completed' && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex items-center gap-2 text-green-400 bg-green-400/10 p-2.5 rounded-lg border border-green-400/20 text-xs"
            >
              <CheckCircle className="h-4 w-4" />
              <span>
                Upload completed!
                {urlJobStatus?.progress?.chunks_created ? ` ${urlJobStatus.progress.chunks_created} chunks created.` : ''}
              </span>
            </motion.div>
          )}

          {urlStatus === 'error' && urlError && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex items-center gap-2 text-red-400 bg-red-400/10 p-2.5 rounded-lg border border-red-400/20 text-xs"
            >
              <AlertCircle className="h-4 w-4" />
              <span>{urlError}</span>
            </motion.div>
          )}
        </div>
      )}

      {/* File batch progress summary */}
      {activeTab === 'file' && isFileBatchRunning && (
        <div className="flex items-center gap-2 text-sm text-text-secondary bg-surface border border-white/10 rounded-xl p-3">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          <span>
            Uploading {fileActiveCount} active file{fileActiveCount !== 1 ? 's' : ''}
            {hasPendingFiles ? `, ${uploadItems.filter((item) => item.status === 'pending').length} queued` : ''}.
          </span>
        </div>
      )}

      {/* Actions */}
      {activeTab === 'file' && hasQueueItems && !isFileBatchRunning && (
        <div className="space-y-2">
          {hasPendingFiles && !docVersion.trim() && (
            <div className="flex items-center gap-2 text-amber-400 bg-amber-400/10 p-2.5 rounded-lg border border-amber-400/20 text-xs">
              <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
              <span>Enter a version (e.g. <span className="font-mono">v9</span> or <span className="font-mono">1.0.0</span>) before uploading.</span>
            </div>
          )}
          <div className="flex gap-2">
            {hasPendingFiles ? (
              <button
                onClick={handleFileUpload}
                disabled={!docVersion.trim()}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-medium text-white bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 transition-all text-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Upload className="h-4 w-4" />
                Upload {uploadItems.filter((item) => item.status === 'pending').length} File{uploadItems.filter((item) => item.status === 'pending').length !== 1 ? 's' : ''}
              </button>
            ) : null}
            <button
              onClick={reset}
              className="px-4 py-2.5 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all text-sm"
            >
              {showUploadAnother ? 'Upload Another' : 'Reset'}
            </button>
          </div>
        </div>
      )}

      {activeTab === 'url' && urlStatus === 'idle' && urlHasInput && (
        <div className="space-y-2">
          {!docVersion.trim() && (
            <div className="flex items-center gap-2 text-amber-400 bg-amber-400/10 p-2.5 rounded-lg border border-amber-400/20 text-xs">
              <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" />
              <span>Enter a version before ingesting.</span>
            </div>
          )}
          <div className="flex gap-2">
          <button
            onClick={handleUrlIngest}
            disabled={urlUploading || !docVersion.trim()}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-medium text-white bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 transition-all text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {urlUploading ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Processing...</>
            ) : (
              <><Link2 className="h-4 w-4" /> Crawl &amp; Ingest</>
            )}
          </button>
          <button
            onClick={reset}
            className="px-4 py-2.5 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all text-sm"
          >
            Reset
          </button>
          </div>
        </div>
      )}

      {activeTab === 'url' && urlStatus === 'completed' && (
        <button
          onClick={reset}
          className="w-full px-4 py-2.5 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all text-sm"
        >
          Upload Another
        </button>
      )}
    </div>
  );
}
