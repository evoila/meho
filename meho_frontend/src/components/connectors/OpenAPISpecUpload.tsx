// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * OpenAPI Spec Upload Component
 * 
 * Allows uploading OpenAPI specifications (JSON/YAML) via file or paste
 */
import { useState, useRef, useCallback } from 'react';
import { Upload, FileText, X, Loader2, CheckCircle, AlertCircle, Download, Code } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import clsx from 'clsx';

const ACCEPTED_EXTENSIONS = ['.json', '.yaml', '.yml'];

interface OpenAPISpecUploadProps {
  connectorId: string;
  onSuccess?: () => void;
}

export function OpenAPISpecUpload({ connectorId, onSuccess }: OpenAPISpecUploadProps) {
  const [file, setFile] = useState<File | null>(null);
  const [pastedContent, setPastedContent] = useState('');
  const [uploadMode, setUploadMode] = useState<'file' | 'paste'>('file');
  const [uploading, setUploading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [result, setResult] = useState<{ message: string; endpoints_count: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const apiClient = getAPIClient(config.apiURL);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);

    try {
      const blob = await apiClient.downloadOpenAPISpec(connectorId);

      // Create download link
      const url = globalThis.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `openapi-spec-${connectorId}.json`; // Default filename, server will override
      document.body.appendChild(a);
      a.click();
      globalThis.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'response' in err && (err as { response?: { status?: number } }).response?.status === 404) {
        setDownloadError('No OpenAPI spec uploaded yet');
      } else {
        setDownloadError(err instanceof Error ? err.message : 'Failed to download spec');
      }
    } finally {
      setDownloading(false);
    }
  }, [apiClient, connectorId]);

  const handleFileSelect = useCallback((selectedFile: File) => {
    const ext = selectedFile.name.split('.').pop()?.toLowerCase();
    if (!ext || !ACCEPTED_EXTENSIONS.includes(`.${ext}`)) {
      setError(`Invalid file type. Accepted: ${ACCEPTED_EXTENSIONS.join(', ')}`);
      return;
    }

    setFile(selectedFile);
    setError(null);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile) {
      handleFileSelect(droppedFile);
    }
  }, [handleFileSelect]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      handleFileSelect(selectedFile);
    }
  }, [handleFileSelect]);

  const handleUpload = useCallback(async () => {
    if (uploadMode === 'file' && !file) {
      setError('Please select a file');
      return;
    }

    if (uploadMode === 'paste' && !pastedContent.trim()) {
      setError('Please paste spec content');
      return;
    }

    setUploading(true);
    setError(null);
    setResult(null);

    try {
      let uploadFile: File;

      if (uploadMode === 'file' && file) {
        uploadFile = file;
      } else {
        // Create file from pasted content
        const blob = new Blob([pastedContent], { type: 'application/json' });
        uploadFile = new File([blob], 'openapi-spec.json', { type: 'application/json' });
      }

      const response = await apiClient.uploadOpenAPISpec(connectorId, uploadFile);
      setResult(response);

      if (onSuccess) {
        setTimeout(onSuccess, 2000);
      }

    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to upload spec');
    } finally {
      setUploading(false);
    }
  }, [uploadMode, file, pastedContent, connectorId, apiClient, onSuccess]);

  return (
    <div className="space-y-8">
      {/* Download Current Spec */}
      <div className="bg-primary/5 border border-primary/10 rounded-xl p-6 relative overflow-hidden">
        <div className="absolute top-0 right-0 w-32 h-32 bg-primary/10 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2" />

        <div className="flex items-start justify-between gap-4 relative z-10">
          <div className="flex-1">
            <h3 className="text-base font-bold text-white mb-1 flex items-center gap-2">
              <Code className="h-4 w-4 text-primary" />
              Current Specification
            </h3>
            <p className="text-sm text-text-secondary">
              Download the currently uploaded spec for debugging or backup
            </p>
          </div>
          <button
            onClick={handleDownload}
            disabled={downloading}
            className="flex items-center gap-2 px-4 py-2 bg-primary/10 hover:bg-primary/20 text-primary border border-primary/20 rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
          >
            {downloading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Downloading...
              </>
            ) : (
              <>
                <Download className="h-4 w-4" />
                Download
              </>
            )}
          </button>
        </div>
        {downloadError && (
          <div className="mt-3 flex items-center gap-2 text-sm text-orange-400 bg-orange-500/10 border border-orange-500/20 p-3 rounded-lg">
            <AlertCircle className="h-4 w-4 flex-shrink-0" />
            {downloadError}
          </div>
        )}
      </div>

      <div>
        <h3 className="text-lg font-bold text-white mb-2">Upload New Specification</h3>
        <p className="text-sm text-text-secondary">
          Upload your OpenAPI 3.x specification to automatically extract and configure endpoints
        </p>
      </div>

      {/* Mode Selector */}
      <div className="flex p-1 bg-white/5 rounded-xl border border-white/10">
        <button
          onClick={() => setUploadMode('file')}
          className={clsx(
            "flex-1 px-4 py-2 rounded-lg text-sm font-medium transition-all",
            uploadMode === 'file'
              ? "bg-white/10 text-white shadow-sm"
              : "text-text-secondary hover:text-white hover:bg-white/5"
          )}
        >
          Upload File
        </button>
        <button
          onClick={() => setUploadMode('paste')}
          className={clsx(
            "flex-1 px-4 py-2 rounded-lg text-sm font-medium transition-all",
            uploadMode === 'paste'
              ? "bg-white/10 text-white shadow-sm"
              : "text-text-secondary hover:text-white hover:bg-white/5"
          )}
        >
          Paste Content
        </button>
      </div>

      {/* File Upload Mode */}
      <AnimatePresence mode="wait">
        {uploadMode === 'file' ? (
          <motion.div
            key="file"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
          >
            <div
              role="button"
              tabIndex={0}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onClick={() => fileInputRef.current?.click()}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInputRef.current?.click(); } }}
              className={clsx(
                "border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-all group",
                file
                  ? "border-primary/50 bg-primary/5"
                  : "border-white/10 hover:border-primary/30 hover:bg-white/5"
              )}
            >
              {!file ? (
                <>
                  <div className="w-16 h-16 rounded-full bg-white/5 flex items-center justify-center mx-auto mb-4 group-hover:scale-110 transition-transform">
                    <Upload className="h-8 w-8 text-text-secondary group-hover:text-primary transition-colors" />
                  </div>
                  <p className="text-white font-medium mb-2">
                    <span className="text-primary">Click to browse</span> or drag and drop
                  </p>
                  <p className="text-sm text-text-tertiary">
                    Supported: JSON, YAML (.json, .yaml, .yml)
                  </p>
                </>
              ) : (
                <div className="flex items-center justify-center gap-4">
                  <div className="w-12 h-12 rounded-lg bg-primary/10 flex items-center justify-center">
                    <FileText className="h-6 w-6 text-primary" />
                  </div>
                  <div className="text-left">
                    <p className="font-medium text-white">{file.name}</p>
                    <p className="text-sm text-text-secondary">{(file.size / 1024).toFixed(1)} KB</p>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setFile(null);
                    }}
                    className="ml-4 p-2 hover:bg-white/10 rounded-lg text-text-secondary hover:text-white transition-colors"
                  >
                    <X className="h-5 w-5" />
                  </button>
                </div>
              )}
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(',')}
                onChange={handleFileInputChange}
                className="hidden"
              />
            </div>
          </motion.div>
        ) : (
          <motion.div
            key="paste"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
          >
            <textarea
              value={pastedContent}
              onChange={(e) => setPastedContent(e.target.value)}
              placeholder={`{\n  "openapi": "3.0.0",\n  "info": {\n    "title": "My API",\n    "version": "1.0.0"\n  },\n  "paths": {...}\n}`}
              rows={15}
              className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm resize-none"
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Upload Results */}
      <AnimatePresence>
        {result && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            className="p-4 bg-green-500/10 border border-green-500/20 rounded-xl"
          >
            <div className="flex items-start gap-3">
              <CheckCircle className="h-5 w-5 text-green-400 mt-0.5" />
              <div className="flex-1">
                <p className="font-bold text-green-200 mb-2">Spec uploaded successfully!</p>
                <div className="text-sm text-green-300/80 space-y-1">
                  {result.endpoints_count !== undefined && (
                    <p>• {result.endpoints_count} endpoints extracted</p>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        )}

        {error && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            className="flex items-center gap-3 p-4 bg-red-500/10 border border-red-500/20 text-red-200 rounded-xl"
          >
            <AlertCircle className="h-5 w-5 text-red-400" />
            <span>{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Actions */}
      <div className="flex gap-3 pt-4 border-t border-white/10">
        <button
          onClick={handleUpload}
          disabled={uploading || (!file && !pastedContent.trim()) || !!result}
          className="flex items-center justify-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed flex-1"
        >
          {uploading ? (
            <>
              <Loader2 className="h-5 w-5 animate-spin" />
              Uploading...
            </>
          ) : result ? (
            <>
              <CheckCircle className="h-5 w-5" />
              Uploaded
            </>
          ) : (
            <>
              <Upload className="h-5 w-5" />
              Upload & Parse Spec
            </>
          )}
        </button>

        {result && onSuccess && (
          <button
            onClick={onSuccess}
            className="px-6 py-2.5 bg-white/10 hover:bg-white/20 text-white rounded-xl font-medium transition-all"
          >
            View Endpoints
          </button>
        )}
      </div>
    </div>
  );
}
