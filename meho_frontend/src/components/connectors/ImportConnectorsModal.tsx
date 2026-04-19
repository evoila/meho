// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Import Connectors Modal
 * 
 * Allows users to import connectors from an encrypted JSON/YAML file.
 * Credentials are decrypted with AES-256-GCM using a user-provided password.
 * 
 * TASK-142: Connector Import/Export
 */
import { useState, useCallback, useRef } from 'react';
import { X, Upload, Loader2, AlertCircle, Lock, FileText, CheckCircle, RefreshCw, Copy, ArrowRightLeft } from 'lucide-react';
import { motion } from 'motion/react';
import clsx from 'clsx';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { ConflictStrategy, ImportConnectorsResponse } from '../../lib/api-client';

interface ImportConnectorsModalProps {
  onClose: () => void;
  onSuccess?: () => void;
}

interface ConflictOption {
  value: ConflictStrategy;
  label: string;
  description: string;
  icon: React.ReactNode;
}

const ACCEPTED_EXTENSIONS = ['.json', '.yaml', '.yml'];

const CONFLICT_OPTIONS: ConflictOption[] = [
  {
    value: 'skip',
    label: 'Skip',
    description: 'Keep existing connectors, ignore duplicates',
    icon: <ArrowRightLeft className="h-4 w-4" />,
  },
  {
    value: 'overwrite',
    label: 'Overwrite',
    description: 'Replace existing connectors with imported ones',
    icon: <RefreshCw className="h-4 w-4" />,
  },
  {
    value: 'rename',
    label: 'Rename',
    description: 'Import as new with suffix (e.g., "Name (2)")',
    icon: <Copy className="h-4 w-4" />,
  },
];

export function ImportConnectorsModal({ onClose, onSuccess }: ImportConnectorsModalProps) {
  // File state
  const [file, setFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  
  // Import options
  const [password, setPassword] = useState('');
  const [conflictStrategy, setConflictStrategy] = useState<ConflictStrategy>('skip');
  
  // UI state
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportConnectorsResponse | null>(null);

  const apiClient = getAPIClient(config.apiURL);

  // Validation
  const hasFile = file !== null;
  const hasPassword = password.length > 0;
  const canImport = hasFile && hasPassword && !importing && !result;

  // File selection handler
  const handleFileSelect = useCallback((selectedFile: File) => {
    const ext = selectedFile.name.split('.').pop()?.toLowerCase();
    if (!ext || !ACCEPTED_EXTENSIONS.includes(`.${ext}`)) {
      setError(`Invalid file type. Accepted: ${ACCEPTED_EXTENSIONS.join(', ')}`);
      return;
    }

    setFile(selectedFile);
    setError(null);
    setResult(null);
  }, []);

  // Drag and drop handlers
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

  // File input change handler
  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      handleFileSelect(selectedFile);
    }
  }, [handleFileSelect]);

  // Remove file
  const handleRemoveFile = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setFile(null);
    setError(null);
    setResult(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  }, []);

  // Import handler
  const handleImport = useCallback(async () => {
    if (!canImport || !file) return;

    setImporting(true);
    setError(null);

    try {
      // Read file as text
      const fileContent = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.onerror = () => reject(new Error('Failed to read file'));
        reader.readAsText(file);
      });

      // Convert to base64
      const base64Content = btoa(unescape(encodeURIComponent(fileContent)));

      // Call API
      const response = await apiClient.importConnectors({
        file_content: base64Content,
        password,
        conflict_strategy: conflictStrategy,
      });

      setResult(response);

      // Close modal after delay if successful
      if (response.imported > 0 || response.skipped > 0) {
        setTimeout(() => {
          onSuccess?.();
          onClose();
        }, 2000);
      }

    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to import connectors');
    } finally {
      setImporting(false);
    }
  }, [canImport, file, password, conflictStrategy, apiClient, onSuccess, onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="relative w-full max-w-xl max-h-[90vh] overflow-hidden glass rounded-2xl border border-white/10 shadow-2xl flex flex-col"
      >
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-white/10 bg-surface/95 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <div className="p-3 bg-primary/10 rounded-xl text-primary">
              <Upload className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-xl font-bold text-white" data-testid="import-modal-title">Import Connectors</h2>
              <p className="text-sm text-text-secondary">Import connector configuration from file</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-white/5 rounded-xl transition-colors text-text-secondary hover:text-white"
            data-testid="import-modal-close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* File Upload Dropzone */}
          <div className="space-y-3">
            <span className="block text-sm font-medium text-text-secondary">
              Select File
            </span>

            <div
              role="button"
              tabIndex={0}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onClick={() => fileInputRef.current?.click()}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInputRef.current?.click(); } }}
              className={clsx(
                "border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all duration-300",
                !file
                  ? "border-white/10 hover:border-primary/50 hover:bg-white/5"
                  : "border-primary/50 bg-primary/5"
              )}
              data-testid="import-dropzone"
            >
              {!file ? (
                <div>
                  <div className="w-14 h-14 rounded-xl bg-surface border border-white/10 flex items-center justify-center mx-auto mb-4 shadow-lg">
                    <Upload className="h-7 w-7 text-primary" />
                  </div>
                  <p className="text-text-primary font-medium mb-2">
                    <span className="text-primary">Click to browse</span> or drag and drop
                  </p>
                  <p className="text-sm text-text-tertiary">
                    JSON or YAML files (.json, .yaml, .yml)
                  </p>
                </div>
              ) : (
                <div className="flex items-center justify-center gap-4">
                  <div className="w-12 h-12 rounded-xl bg-primary/20 flex items-center justify-center">
                    <FileText className="h-6 w-6 text-primary" />
                  </div>
                  <div className="text-left">
                    <p className="font-medium text-white" data-testid="import-file-name">{file.name}</p>
                    <p className="text-sm text-text-secondary">{(file.size / 1024).toFixed(1)} KB</p>
                  </div>
                  <button
                    onClick={handleRemoveFile}
                    className="ml-4 p-2 hover:bg-white/10 rounded-full transition-colors"
                    data-testid="import-remove-file"
                  >
                    <X className="h-5 w-5 text-text-secondary hover:text-white" />
                  </button>
                </div>
              )}

              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(',')}
                onChange={handleFileInputChange}
                className="hidden"
                data-testid="import-file-input"
              />
            </div>
          </div>

          {/* Password Input */}
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-white font-medium">
              <Lock className="h-4 w-4 text-primary" />
              <h3>Decryption Password</h3>
            </div>

            <div>
              <label htmlFor="import-password" className="block text-sm font-medium text-text-secondary mb-2">
                Password *
              </label>
              <input
                id="import-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter password used during export"
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                data-testid="import-password"
              />
              <p className="text-xs text-text-tertiary mt-1">
                Enter the same password that was used when exporting the connectors.
              </p>
            </div>
          </div>

          {/* Conflict Strategy Selector */}
          <div className="space-y-4">
            <span className="block text-sm font-medium text-text-secondary">
              Conflict Resolution
            </span>

            <div className="space-y-2">
              {CONFLICT_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className={clsx(
                    "flex items-start gap-3 p-4 rounded-xl border cursor-pointer transition-all",
                    conflictStrategy === option.value
                      ? "bg-primary/10 border-primary/50"
                      : "bg-surface border-white/10 hover:bg-white/5"
                  )}
                  data-testid={`import-conflict-${option.value}`}
                >
                  <input
                    type="radio"
                    name="conflictStrategy"
                    value={option.value}
                    checked={conflictStrategy === option.value}
                    onChange={(e) => setConflictStrategy(e.target.value as ConflictStrategy)}
                    className="mt-1 w-4 h-4 text-primary bg-surface border-white/20 focus:ring-primary/50"
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className={clsx(
                        "text-sm",
                        conflictStrategy === option.value ? "text-primary" : "text-text-tertiary"
                      )}>
                        {option.icon}
                      </span>
                      <span className="font-medium text-white">{option.label}</span>
                    </div>
                    <p className="text-sm text-text-secondary mt-1">{option.description}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Error Display */}
          {error && (
            <div className="flex items-center gap-2 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
              <AlertCircle className="h-5 w-5 flex-shrink-0" />
              <span className="text-sm" data-testid="import-error">{error}</span>
            </div>
          )}

          {/* Success Display */}
          {result && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 p-4 bg-green-500/10 text-green-400 rounded-xl border border-green-500/20">
                <CheckCircle className="h-5 w-5 flex-shrink-0" />
                <div className="text-sm" data-testid="import-success">
                  <p className="font-medium">Import Complete!</p>
                  <p>
                    {result.imported > 0 && `Imported ${result.imported} connector${result.imported > 1 ? 's' : ''}`}
                    {result.imported > 0 && result.skipped > 0 && ', '}
                    {result.skipped > 0 && `Skipped ${result.skipped} existing`}
                  </p>
                  {result.operations_synced > 0 && (
                    <p className="text-text-secondary mt-1">
                      {result.operations_synced} operations synced
                    </p>
                  )}
                </div>
              </div>

              {/* Imported connector names */}
              {result.connectors.length > 0 && (
                <div className="p-4 bg-surface border border-white/10 rounded-xl">
                  <p className="text-xs text-text-tertiary mb-2">Imported connectors:</p>
                  <div className="flex flex-wrap gap-2">
                    {result.connectors.map((name) => (
                      <span
                        key={`conn-${name}`}
                        className="px-2 py-1 text-xs bg-primary/10 text-primary rounded-lg"
                      >
                        {name}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Operations sync warnings (Phase 9) */}
              {result.warnings && result.warnings.length > 0 && (
                <div className="p-4 bg-amber-500/10 border border-amber-500/20 rounded-xl" data-testid="import-warnings">
                  <p className="text-xs text-amber-400 mb-2">Operations Sync Warnings:</p>
                  <ul className="text-sm text-amber-300 space-y-1">
                    {result.warnings.map((warning, i) => (
                      <li key={i} className="flex items-start gap-2">
                        <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
                        {warning}
                      </li>
                    ))}
                  </ul>
                  <p className="text-xs text-text-tertiary mt-2">
                    Connectors were imported successfully. You can manually sync operations later.
                  </p>
                </div>
              )}

              {/* Import errors */}
              {result.errors.length > 0 && (
                <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl" data-testid="import-errors">
                  <p className="text-xs text-red-400 mb-2">Import Errors:</p>
                  <ul className="text-sm text-red-300 space-y-1">
                    {result.errors.map((err, i) => (
                      <li key={i} className="flex items-start gap-2">
                        <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
                        {err}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex gap-3 p-6 border-t border-white/10 bg-surface/95">
          <button
            type="button"
            onClick={onClose}
            disabled={importing}
            className="flex-1 px-6 py-3 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleImport}
            disabled={!canImport}
            className="flex-1 flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
            data-testid="import-submit"
          >
            {importing ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin" />
                Importing...
              </>
            ) : result ? (
              <>
                <CheckCircle className="h-5 w-5" />
                Done
              </>
            ) : (
              <>
                <Upload className="h-5 w-5" />
                Import
              </>
            )}
          </button>
        </div>
      </motion.div>
    </div>
  );
}

