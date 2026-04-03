// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Export Connectors Modal
 * 
 * Allows users to export selected connectors to an encrypted JSON/YAML file.
 * Credentials are encrypted with AES-256-GCM using a user-provided password.
 * 
 * TASK-142: Connector Import/Export
 */
import { useState, useCallback, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { X, Download, Loader2, AlertCircle, Lock, Plug, CheckCircle, FileJson, FileCode } from 'lucide-react';
import { motion } from 'motion/react';
import clsx from 'clsx';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { Connector, ExportFormat } from '../../lib/api-client';

interface ExportConnectorsModalProps {
  onClose: () => void;
  onSuccess?: () => void;
}

export function ExportConnectorsModal({ onClose, onSuccess }: ExportConnectorsModalProps) { // NOSONAR (cognitive complexity)
  // Selection state
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  
  // Export options
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [format, setFormat] = useState<ExportFormat>('json');
  
  // UI state
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const apiClient = getAPIClient(config.apiURL);

  // Fetch connectors
  const { data: connectors, isLoading } = useQuery({
    queryKey: ['connectors'],
    queryFn: () => apiClient.listConnectors(),
  });

  // Validation
  const passwordValid = password.length >= 8;
  const passwordsMatch = password === confirmPassword;
  const hasSelection = selectedIds.size > 0;
  const canExport = hasSelection && passwordValid && passwordsMatch && !exporting;

  // Validation messages
  const validationErrors = useMemo(() => {
    const errors: string[] = [];
    if (password && password.length < 8) {
      errors.push('Password must be at least 8 characters');
    }
    if (confirmPassword && !passwordsMatch) {
      errors.push('Passwords do not match');
    }
    return errors;
  }, [password, confirmPassword, passwordsMatch]);

  // Toggle connector selection
  const toggleConnector = useCallback((id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  // Select/deselect all
  const toggleSelectAll = useCallback(() => {
    if (!connectors) return;
    
    if (selectedIds.size === connectors.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(connectors.map(c => c.id)));
    }
  }, [connectors, selectedIds.size]);

  // Export handler
  const handleExport = useCallback(async () => {
    if (!canExport) return;

    setExporting(true);
    setError(null);

    try {
      const blob = await apiClient.exportConnectors({
        connector_ids: Array.from(selectedIds),
        password,
        format,
      });

      // Generate filename with timestamp
      const timestamp = new Date().toISOString().slice(0, 10);
      const filename = `meho-connectors-${timestamp}.${format}`;

      // Trigger download
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      setSuccess(true);
      
      // Close modal after short delay
      setTimeout(() => {
        onSuccess?.();
        onClose();
      }, 1500);

    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to export connectors');
    } finally {
      setExporting(false);
    }
  }, [canExport, selectedIds, password, format, apiClient, onSuccess, onClose]);

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
              <Download className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-xl font-bold text-white" data-testid="export-modal-title">Export Connectors</h2>
              <p className="text-sm text-text-secondary">Download encrypted connector configuration</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-white/5 rounded-xl transition-colors text-text-secondary hover:text-white"
            data-testid="export-modal-close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {/* Connector Selection */}
          <div className="space-y-3">
            <span className="block text-sm font-medium text-text-secondary">
              Select connectors to export
            </span>

            {(() => {
              if (isLoading) return (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-6 w-6 animate-spin text-primary" />
                </div>
              );
              if (connectors && connectors.length > 0) return (
              <div className="border border-white/10 rounded-xl overflow-hidden">
                {/* Select All */}
                <label className="flex items-center gap-3 px-4 py-3 bg-white/5 border-b border-white/10 cursor-pointer hover:bg-white/10 transition-colors">
                  <input
                    type="checkbox"
                    checked={selectedIds.size === connectors.length}
                    onChange={toggleSelectAll}
                    className="w-4 h-4 rounded border-white/20 bg-surface text-primary focus:ring-primary/50"
                    data-testid="export-select-all"
                  />
                  <span className="text-sm font-medium text-white">
                    Select All ({connectors.length})
                  </span>
                </label>

                {/* Connector List */}
                <div className="max-h-48 overflow-y-auto">
                  {connectors.map((connector: Connector) => (
                    <label
                      key={connector.id}
                      className="flex items-center gap-3 px-4 py-3 border-b border-white/5 last:border-b-0 cursor-pointer hover:bg-white/5 transition-colors"
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds.has(connector.id)}
                        onChange={() => toggleConnector(connector.id)}
                        className="w-4 h-4 rounded border-white/20 bg-surface text-primary focus:ring-primary/50"
                        data-testid={`export-connector-${connector.id}`}
                      />
                      <div className="p-1.5 bg-primary/10 rounded-lg">
                        <Plug className="h-4 w-4 text-primary" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-white truncate">{connector.name}</p>
                        <p className="text-xs text-text-tertiary truncate">{connector.base_url}</p>
                      </div>
                      <span className={clsx(
                        "px-2 py-0.5 text-xs rounded-md font-medium",
                        ({
                          vmware: "bg-emerald-500/10 text-emerald-400",
                          proxmox: "bg-orange-500/10 text-orange-400",
                          kubernetes: "bg-blue-500/10 text-blue-400",
                          gcp: "bg-sky-500/10 text-sky-400",
                        } as Record<string, string>)[connector.connector_type] ?? "bg-white/10 text-text-secondary"
                      )}>
                        {connector.connector_type.toUpperCase()}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
              );
              return (
                <div className="text-center py-8 bg-surface border border-white/10 rounded-xl">
                  <Plug className="h-8 w-8 text-text-tertiary mx-auto mb-2" />
                  <p className="text-text-secondary">No connectors found</p>
                </div>
              );
            })()}

            {!hasSelection && connectors && connectors.length > 0 && (
              <p className="text-xs text-amber-400 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                Select at least one connector to export
              </p>
            )}
          </div>

          {/* Password Input */}
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-white font-medium">
              <Lock className="h-4 w-4 text-primary" />
              <h3>Encryption Password</h3>
            </div>

            <div className="space-y-3">
              <div>
                <label htmlFor="export-password" className="block text-sm font-medium text-text-secondary mb-2">
                  Password *
                </label>
                <input
                  id="export-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter encryption password"
                  className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  data-testid="export-password"
                />
                <p className="text-xs text-text-tertiary mt-1">
                  Minimum 8 characters. Credentials will be encrypted with AES-256-GCM.
                </p>
              </div>

              <div>
                <label htmlFor="export-confirm-password" className="block text-sm font-medium text-text-secondary mb-2">
                  Confirm Password *
                </label>
                <input
                  id="export-confirm-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="Confirm encryption password"
                  className={clsx(
                    "w-full px-4 py-3 bg-surface border rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all",
                    confirmPassword && !passwordsMatch ? "border-red-500/50" : "border-white/10"
                  )}
                  data-testid="export-password-confirm"
                />
              </div>

              {/* Validation Errors */}
              {validationErrors.length > 0 && (
                <div className="space-y-1">
                  {validationErrors.map((err, i) => (
                    <p key={i} className="text-xs text-red-400 flex items-center gap-1">
                      <AlertCircle className="h-3 w-3" />
                      {err}
                    </p>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Format Selector */}
          <div className="space-y-3">
            <span className="block text-sm font-medium text-text-secondary">
              Export Format
            </span>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setFormat('json')}
                className={clsx(
                  "flex items-center justify-center gap-2 px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                  format === 'json'
                    ? "bg-primary/10 border-primary/50 text-white"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}
                data-testid="export-format-json"
              >
                <FileJson className="h-4 w-4" />
                JSON
              </button>
              <button
                type="button"
                onClick={() => setFormat('yaml')}
                className={clsx(
                  "flex items-center justify-center gap-2 px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                  format === 'yaml'
                    ? "bg-primary/10 border-primary/50 text-white"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}
                data-testid="export-format-yaml"
              >
                <FileCode className="h-4 w-4" />
                YAML
              </button>
            </div>
          </div>

          {/* Error Display */}
          {error && (
            <div className="flex items-center gap-2 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
              <AlertCircle className="h-5 w-5 flex-shrink-0" />
              <span className="text-sm">{error}</span>
            </div>
          )}

          {/* Success Display */}
          {success && (
            <div className="flex items-center gap-2 p-4 bg-green-500/10 text-green-400 rounded-xl border border-green-500/20">
              <CheckCircle className="h-5 w-5 flex-shrink-0" />
              <span className="text-sm">Export successful! Download starting...</span>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex gap-3 p-6 border-t border-white/10 bg-surface/95">
          <button
            type="button"
            onClick={onClose}
            disabled={exporting}
            className="flex-1 px-6 py-3 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleExport}
            disabled={!canExport || success}
            className="flex-1 flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
            data-testid="export-submit"
          >
            {(() => {
              if (exporting) return <><Loader2 className="h-5 w-5 animate-spin" /> Exporting...</>;
              if (success) return <><CheckCircle className="h-5 w-5" /> Exported</>;
              return <><Download className="h-5 w-5" /> Export {selectedIds.size > 0 ? `${selectedIds.size} Connector${selectedIds.size > 1 ? 's' : ''}` : ''}</>;
            })()}
          </button>
        </div>
      </motion.div>
    </div>
  );
}

