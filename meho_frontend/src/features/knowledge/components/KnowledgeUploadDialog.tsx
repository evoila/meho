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
 * Supports file upload (PDF) and URL ingestion.
 * Used by both KnowledgePage tree and ConnectorKnowledge tab.
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  Upload, X, FileText, Loader2, CheckCircle, AlertCircle,
  Clock, Tag, Link2,
} from 'lucide-react';
import { getAPIClient, type IngestionJobStatus } from '../../../lib/api-client';
import { config } from '../../../lib/config';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import type { KnowledgeScope } from '../../../api/types/knowledge';

interface KnowledgeUploadDialogProps {
  scope: KnowledgeScope;
  onSuccess?: () => void;
  /** If true, render as embedded section (no dialog chrome) */
  inline?: boolean;
}

type TabId = 'file' | 'url';

export function KnowledgeUploadDialog({ scope, onSuccess, inline }: KnowledgeUploadDialogProps) {
  const [activeTab, setActiveTab] = useState<TabId>('file');
  const [file, setFile] = useState<File | null>(null);
  const [urlInput, setUrlInput] = useState('');
  const [knowledgeType, setKnowledgeType] = useState<'documentation' | 'procedure'>('documentation');
  const [tags, setTags] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState('');
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState<'idle' | 'uploading' | 'processing' | 'completed' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<IngestionJobStatus | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollIntervalRef = useRef<number | null>(null);
  const pollInFlightRef = useRef(false);

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
    };
  }, []);

  const handleFileSelect = useCallback((selectedFile: File) => {
    const ext = selectedFile.name.split('.').pop()?.toLowerCase();
    if (ext !== 'pdf') {
      setError('Only PDF files are supported. Convert HTML/DOCX to PDF first.');
      return;
    }
    setFile(selectedFile);
    setError(null);
    setStatus('idle');
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile) handleFileSelect(droppedFile);
  }, [handleFileSelect]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const addTag = useCallback(() => {
    if (tagInput.trim() && !tags.includes(tagInput.trim())) {
      setTags(prev => [...prev, tagInput.trim()]);
      setTagInput('');
    }
  }, [tagInput, tags]);

  const removeTag = useCallback((tag: string) => {
    setTags(prev => prev.filter(t => t !== tag));
  }, []);

  const pollJobStatus = useCallback(async (id: string) => {
    if (pollInFlightRef.current) return;  // Skip if previous poll still running
    pollInFlightRef.current = true;
    try {
      const job = await apiClient.getJobStatus(id);
      setJobStatus(job);

      if (job.progress) {
        const progressValue = job.progress.overall_progress !== undefined
          ? job.progress.overall_progress * 100
          : job.progress.percent;
        setProgress(Math.round(progressValue));
      }

      if (job.status === 'completed') {
        setStatus('completed');
        setProgress(100);
        setUploading(false);
        if (pollIntervalRef.current) {
          clearInterval(pollIntervalRef.current);
          pollIntervalRef.current = null;
        }
        // Invalidate tree + connector doc lists
        queryClient.invalidateQueries({ queryKey: ['knowledge-tree'] });
        if (scope.connector_id) {
          queryClient.invalidateQueries({ queryKey: ['connector-knowledge-documents', scope.connector_id] });
        }
        if (onSuccess) setTimeout(onSuccess, 1500);
      } else if (job.status === 'failed') {
        setStatus('error');
        setError(job.error || 'Processing failed');
        setUploading(false);
        if (pollIntervalRef.current) {
          clearInterval(pollIntervalRef.current);
          pollIntervalRef.current = null;
        }
      }
    } catch {
      // ignore polling errors
    } finally {
      pollInFlightRef.current = false;
    }
  }, [apiClient, onSuccess, queryClient, scope.connector_id]);

  const handleFileUpload = useCallback(async () => {
    if (!file) return;
    setUploading(true);
    setStatus('uploading');
    setError(null);
    setProgress(0);
    setJobStatus(null);

    try {
      const response = await apiClient.uploadDocument({
        file,
        knowledge_type: knowledgeType,
        tags,
        connector_id: scope.connector_id,
        scope_type: scope.scope_type,
        connector_type_scope: scope.connector_type_scope,
      });

      setStatus('processing');
      setUploading(false);

      await pollJobStatus(response.job_id);
      pollIntervalRef.current = window.setInterval(() => {
        pollJobStatus(response.job_id);
      }, 1000);
    } catch (err: unknown) {
      setStatus('error');
      setError(err instanceof Error ? err.message : 'Upload failed');
      setUploading(false);
    }
  }, [file, knowledgeType, tags, apiClient, pollJobStatus, scope]);

  const handleUrlIngest = useCallback(async () => {
    const url = urlInput.trim();
    if (!url) return;
    setUploading(true);
    setStatus('uploading');
    setError(null);
    setProgress(0);
    setJobStatus(null);

    try {
      const response = await apiClient.ingestUrl({
        url,
        scope_type: scope.scope_type,
        connector_type_scope: scope.connector_type_scope,
        connector_id: scope.connector_id,
        knowledge_type: knowledgeType,
        tags,
      });

      setStatus('processing');
      setUploading(false);

      await pollJobStatus(response.job_id);
      pollIntervalRef.current = window.setInterval(() => {
        pollJobStatus(response.job_id);
      }, 1000);
    } catch (err: unknown) {
      setStatus('error');
      setError(err instanceof Error ? err.message : 'URL ingestion failed');
      setUploading(false);
    }
  }, [urlInput, knowledgeType, tags, apiClient, pollJobStatus, scope]);

  const reset = useCallback(() => {
    setFile(null);
    setUrlInput('');
    setKnowledgeType('documentation');
    setTags([]);
    setTagInput('');
    setUploading(false);
    setProgress(0);
    setStatus('idle');
    setError(null);
    setJobStatus(null);
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
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

  return (
    <div className={clsx('space-y-5', !inline && 'glass rounded-xl border border-white/10 p-6')}>
      {/* Scope indicator */}
      <div className="flex items-center gap-2 text-sm text-text-secondary">
        <span className="px-2 py-0.5 rounded-md bg-primary/10 border border-primary/20 text-primary font-medium text-xs">
          {scope.scope_type.toUpperCase()}
        </span>
        <span>Uploading to: {scopeLabel}</span>
      </div>

      {/* Tab switcher: File | URL */}
      <div className="flex gap-1 bg-white/5 rounded-lg p-1 border border-white/10">
        {([
          { id: 'file' as const, label: 'File', icon: FileText },
          { id: 'url' as const, label: 'URL', icon: Link2 },
        ]).map((tab) => (
          <button
            key={tab.id}
            onClick={() => { setActiveTab(tab.id); setError(null); }}
            className={clsx(
              'flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-all',
              activeTab === tab.id
                ? 'bg-primary/20 text-white border border-primary/30'
                : 'text-text-secondary hover:text-white hover:bg-white/5'
            )}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </button>
        ))}
      </div>

      {/* File Upload Tab */}
      {activeTab === 'file' && (
        <div className="space-y-4">
          {/* Drop zone */}
          <div
            role="button"
            tabIndex={0}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInputRef.current?.click(); } }}
            className={clsx(
              'border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all',
              !file
                ? 'border-white/10 hover:border-primary/50 hover:bg-white/5'
                : 'border-primary/50 bg-primary/5'
            )}
          >
            <AnimatePresence mode="wait">
              {!file ? (
                <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                  <Upload className="h-8 w-8 text-primary mx-auto mb-3" />
                  <p className="text-text-primary font-medium mb-1">Click to browse or drag and drop</p>
                  <p className="text-xs text-text-tertiary">PDF only</p>
                </motion.div>
              ) : (
                <motion.div key="selected" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex items-center justify-center gap-3">
                  <FileText className="h-6 w-6 text-primary" />
                  <div className="text-left">
                    <p className="font-medium text-white text-sm">{file.name}</p>
                    <p className="text-xs text-text-secondary">{(file.size / 1024).toFixed(1)} KB</p>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); setFile(null); }}
                    className="p-1 hover:bg-white/10 rounded-full transition-colors"
                  >
                    <X className="h-4 w-4 text-text-secondary" />
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); }}
              className="hidden"
            />
          </div>
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
              disabled={uploading}
            />
          </div>
        </div>
      )}

      {/* Knowledge Type + Tags */}
      {(file || (activeTab === 'url' && urlInput.trim())) && status === 'idle' && (
        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="space-y-4">
          {/* Knowledge Type */}
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
                  disabled={uploading}
                  className={clsx(
                    'p-3 rounded-xl border text-sm font-medium transition-all text-left',
                    knowledgeType === opt.id
                      ? 'bg-primary/10 border-primary/50 text-white'
                      : 'bg-surface border-white/10 text-text-secondary hover:bg-white/5'
                  )}
                >
                  <span>{opt.label}</span>
                  <span className="block text-xs opacity-70 mt-0.5">{opt.sub}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Tags */}
          <div className="space-y-2">
            <label htmlFor="knowledge-upload-tags" className="text-sm font-medium text-text-secondary">Tags</label>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Tag className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
                <input
                  id="knowledge-upload-tags"
                  type="text"
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyPress={(e) => { if (e.key === 'Enter') { e.preventDefault(); addTag(); } }}
                  placeholder="Add tags..."
                  disabled={uploading}
                  className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 transition-all text-sm"
                />
              </div>
              <button
                onClick={addTag}
                disabled={!tagInput.trim() || uploading}
                className="px-3 py-2 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-white font-medium transition-all disabled:opacity-50 text-sm"
              >
                Add
              </button>
            </div>
            {tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {tags.map((tag) => (
                  <span key={tag} className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary/10 border border-primary/20 text-primary-light rounded-lg text-xs">
                    {tag}
                    <button onClick={() => removeTag(tag)} disabled={uploading} className="hover:bg-primary/20 rounded-full p-0.5">
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        </motion.div>
      )}

      {/* Progress */}
      {status !== 'idle' && (
        <div className="bg-surface border border-white/10 rounded-xl p-4 space-y-3">
          {jobStatus?.progress?.current_stage && (
            <div className="flex items-center gap-3 text-xs overflow-x-auto pb-1 scrollbar-hide">
              {['uploading', 'extracting', 'chunking', 'embedding', 'storing'].map((stage) => {
                const stages = ['uploading', 'extracting', 'chunking', 'embedding', 'storing'];
                const currentIndex = stages.indexOf(jobStatus.progress.current_stage || '');
                const stageIndex = stages.indexOf(stage);
                const isComplete = stageIndex < currentIndex;
                const isCurrent = stage === jobStatus.progress.current_stage;
                return (
                  <span
                    key={stage}
                    className={clsx(
                      'flex items-center gap-1 whitespace-nowrap',
                      isComplete ? 'text-green-400' : isCurrent ? 'text-primary font-medium' : 'text-text-tertiary'
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
                {jobStatus?.progress?.status_message || (
                  status === 'uploading' ? 'Uploading...' :
                  status === 'processing' ? 'Processing...' :
                  status === 'completed' ? 'Completed!' :
                  'Failed'
                )}
              </span>
              <span className="text-xs text-text-secondary">{progress}%</span>
            </div>
            <div className="w-full bg-white/5 rounded-full h-1.5 overflow-hidden">
              <motion.div
                className={clsx('h-full rounded-full', status === 'error' ? 'bg-red-500' : 'bg-gradient-to-r from-primary to-accent')}
                initial={{ width: 0 }}
                animate={{ width: `${progress}%` }}
                transition={{ duration: 0.5 }}
              />
            </div>
            {jobStatus?.progress?.estimated_completion && (
              <div className="mt-1 text-xs text-text-tertiary flex items-center gap-1">
                <Clock className="h-3 w-3" />
                ETA: {formatETA(jobStatus.progress.estimated_completion)}
              </div>
            )}
          </div>

          {status === 'completed' && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-2 text-green-400 bg-green-400/10 p-2.5 rounded-lg border border-green-400/20 text-xs">
              <CheckCircle className="h-4 w-4" />
              <span>Upload completed!{jobStatus?.progress?.chunks_created ? ` ${jobStatus.progress.chunks_created} chunks created.` : ''}</span>
            </motion.div>
          )}

          {status === 'error' && error && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-2 text-red-400 bg-red-400/10 p-2.5 rounded-lg border border-red-400/20 text-xs">
              <AlertCircle className="h-4 w-4" />
              <span>{error}</span>
            </motion.div>
          )}
        </div>
      )}

      {/* Actions */}
      {status === 'idle' && (file || (activeTab === 'url' && urlInput.trim())) && (
        <div className="flex gap-2">
          <button
            onClick={activeTab === 'file' ? handleFileUpload : handleUrlIngest}
            disabled={uploading}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl font-medium text-white bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 transition-all text-sm disabled:opacity-50"
          >
            {uploading ? (
              <><Loader2 className="h-4 w-4 animate-spin" /> Processing...</>
            ) : activeTab === 'file' ? (
              <><Upload className="h-4 w-4" /> Upload</>
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
      )}

      {status === 'completed' && (
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
