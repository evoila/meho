// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Endpoint Editor Modal (★★★ Key Feature)
 * 
 * Allows admins to:
 * - Enable/disable endpoints
 * - Set safety levels
 * - Add enhanced descriptions
 * - Add admin notes
 * - Provide usage examples
 */
import { useState, useCallback } from 'react';
import { X, Save, RotateCcw, Shield, Loader2, CheckCircle, AlertCircle, AlertTriangle } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { Endpoint, UpdateEndpointRequest } from '../../lib/api-client';
import clsx from 'clsx';

interface EndpointEditorModalProps {
  connectorId: string;
  endpoint: Endpoint;
  onClose: () => void;
  onSuccess: () => void;
}

export function EndpointEditorModal({ connectorId, endpoint, onClose, onSuccess }: EndpointEditorModalProps) {
  const [isEnabled, setIsEnabled] = useState(endpoint.is_enabled);
  const [safetyLevel, setSafetyLevel] = useState<string>(endpoint.safety_level || 'auto');
  const [requiresApproval, setRequiresApproval] = useState(endpoint.requires_approval);
  const [customDescription, setCustomDescription] = useState(endpoint.custom_description || '');
  const [customNotes, setCustomNotes] = useState(endpoint.custom_notes || '');
  const [examplesJson, setExamplesJson] = useState(
    endpoint.usage_examples ? JSON.stringify(endpoint.usage_examples, null, 2) : ''
  );

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: async (request: UpdateEndpointRequest) => {
      return await apiClient.updateEndpoint(connectorId, endpoint.id, request);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['endpoints', connectorId] });
      onSuccess();
    },
  });

  const handleSave = useCallback(async () => {
    // Parse examples JSON
    let parsedExamples = undefined;
    if (examplesJson.trim()) {
      try {
        parsedExamples = JSON.parse(examplesJson);
      } catch (_e) {
        alert('Invalid JSON in usage examples');
        return;
      }
    }

    const request: UpdateEndpointRequest = {
      is_enabled: isEnabled,
      safety_level: safetyLevel as UpdateEndpointRequest['safety_level'],
      requires_approval: requiresApproval,
      custom_description: customDescription.trim() || undefined,
      custom_notes: customNotes.trim() || undefined,
      usage_examples: parsedExamples,
    };

    await updateMutation.mutateAsync(request);
  }, [isEnabled, safetyLevel, requiresApproval, customDescription, customNotes, examplesJson, updateMutation]);

  const handleReset = useCallback(() => {
    setIsEnabled(endpoint.is_enabled);
    setSafetyLevel(endpoint.safety_level || 'auto');
    setRequiresApproval(endpoint.requires_approval);
    setCustomDescription(endpoint.custom_description || '');
    setCustomNotes(endpoint.custom_notes || '');
    setExamplesJson(endpoint.usage_examples ? JSON.stringify(endpoint.usage_examples, null, 2) : '');
  }, [endpoint]);

  const methodColors: Record<string, string> = {
    GET: 'bg-green-500/10 text-green-400 border-green-500/20',
    POST: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    PUT: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    PATCH: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    DELETE: 'bg-red-500/10 text-red-400 border-red-500/20',
  };

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50 p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="relative w-full max-w-4xl max-h-[90vh] overflow-y-auto glass rounded-2xl border border-white/10 shadow-2xl"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between p-6 border-b border-white/10 bg-surface/95 backdrop-blur-xl">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1">
              <span className={clsx(
                "text-xs font-mono font-bold px-2 py-1 rounded-lg border",
                methodColors[endpoint.method] || 'bg-white/10 text-text-secondary border-white/10'
              )}>
                {endpoint.method}
              </span>
              <code className="text-sm font-mono text-white">{endpoint.path}</code>
            </div>
            {endpoint.operation_id && (
              <p className="text-xs text-text-tertiary font-mono">Operation: {endpoint.operation_id}</p>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-white/10 rounded-xl text-text-secondary hover:text-white transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Form */}
        <div className="p-6 space-y-6">
          {/* Activation & Safety */}
          <div className="bg-white/5 border border-white/10 rounded-xl p-6">
            <div className="flex items-center gap-2 mb-6">
              <Shield className="h-5 w-5 text-primary" />
              <h3 className="text-sm font-bold text-white uppercase tracking-wider">Activation & Safety</h3>
            </div>

            <div className="space-y-6">
              {/* Status */}
              <div>
                <span className="block text-sm font-medium text-text-secondary mb-3">
                  Status
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <label className={clsx(
                    "flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all",
                    isEnabled
                      ? "bg-green-500/10 border-green-500/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <input
                      type="radio"
                      checked={isEnabled}
                      onChange={() => setIsEnabled(true)}
                      className="text-green-500 focus:ring-green-500 bg-black/20 border-white/20"
                    />
                    <span className={clsx("text-sm font-medium", isEnabled ? "text-green-400" : "text-text-secondary")}>
                      Enabled (visible to agent)
                    </span>
                  </label>

                  <label className={clsx(
                    "flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all",
                    !isEnabled
                      ? "bg-red-500/10 border-red-500/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <input
                      type="radio"
                      checked={!isEnabled}
                      onChange={() => setIsEnabled(false)}
                      className="text-red-500 focus:ring-red-500 bg-black/20 border-white/20"
                    />
                    <span className={clsx("text-sm font-medium", !isEnabled ? "text-red-400" : "text-text-secondary")}>
                      Disabled (hidden from agent)
                    </span>
                  </label>
                </div>
              </div>

              {/* Trust Level */}
              <div>
                <span className="block text-sm font-medium text-text-secondary mb-3">
                  Trust Level
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                  <label className={clsx(
                    "flex flex-col gap-2 p-3 rounded-xl border cursor-pointer transition-all",
                    safetyLevel === 'auto'
                      ? "bg-white/10 border-white/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <div className="flex items-center gap-2">
                      <input
                        type="radio"
                        value="auto"
                        checked={safetyLevel === 'auto' || safetyLevel === 'safe'}
                        onChange={() => setSafetyLevel('auto')}
                        className="text-white focus:ring-white bg-black/20 border-white/20"
                      />
                      <span className={clsx("text-sm font-bold", (safetyLevel === 'auto' || safetyLevel === 'safe') ? "text-white" : "text-text-secondary")}>
                        Auto
                      </span>
                    </div>
                    <span className="text-xs text-text-tertiary ml-6">Use default classification</span>
                  </label>

                  <label className={clsx(
                    "flex flex-col gap-2 p-3 rounded-xl border cursor-pointer transition-all",
                    safetyLevel === 'read'
                      ? "bg-green-500/10 border-green-500/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <div className="flex items-center gap-2">
                      <input
                        type="radio"
                        value="read"
                        checked={safetyLevel === 'read'}
                        onChange={() => setSafetyLevel('read')}
                        className="text-green-500 focus:ring-green-500 bg-black/20 border-white/20"
                      />
                      <span className={clsx("text-sm font-bold", safetyLevel === 'read' ? "text-green-400" : "text-white")}>
                        Read
                      </span>
                    </div>
                    <span className="text-xs text-text-tertiary ml-6">Auto-approved, no interruption</span>
                  </label>

                  <label className={clsx(
                    "flex flex-col gap-2 p-3 rounded-xl border cursor-pointer transition-all",
                    (safetyLevel === 'write' || safetyLevel === 'caution')
                      ? "bg-yellow-500/10 border-yellow-500/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <div className="flex items-center gap-2">
                      <input
                        type="radio"
                        value="write"
                        checked={safetyLevel === 'write' || safetyLevel === 'caution'}
                        onChange={() => setSafetyLevel('write')}
                        className="text-yellow-500 focus:ring-yellow-500 bg-black/20 border-white/20"
                      />
                      <span className={clsx("text-sm font-bold", (safetyLevel === 'write' || safetyLevel === 'caution') ? "text-yellow-400" : "text-white")}>
                        Write
                      </span>
                    </div>
                    <span className="text-xs text-text-tertiary ml-6">Requires approval</span>
                  </label>

                  <label className={clsx(
                    "flex flex-col gap-2 p-3 rounded-xl border cursor-pointer transition-all",
                    (safetyLevel === 'destructive' || safetyLevel === 'dangerous')
                      ? "bg-red-500/10 border-red-500/30"
                      : "bg-white/5 border-white/10 hover:bg-white/10"
                  )}>
                    <div className="flex items-center gap-2">
                      <input
                        type="radio"
                        value="destructive"
                        checked={safetyLevel === 'destructive' || safetyLevel === 'dangerous'}
                        onChange={() => setSafetyLevel('destructive')}
                        className="text-red-500 focus:ring-red-500 bg-black/20 border-white/20"
                      />
                      <span className={clsx("text-sm font-bold", (safetyLevel === 'destructive' || safetyLevel === 'dangerous') ? "text-red-400" : "text-white")}>
                        Destructive
                      </span>
                    </div>
                    <span className="text-xs text-text-tertiary ml-6">Requires approval, red modal</span>
                  </label>
                </div>
                <p className="text-xs text-text-tertiary mt-2">
                  Override the automatic trust classification for this endpoint.
                  "Auto" uses the default rules (HTTP method for REST, operation registry for typed connectors).
                </p>
              </div>

              {/* Requires Approval */}
              <div>
                <label className="flex items-center gap-3 cursor-pointer group">
                  <div className="relative flex items-center">
                    <input
                      type="checkbox"
                      checked={requiresApproval}
                      onChange={(e) => setRequiresApproval(e.target.checked)}
                      className="peer h-5 w-5 rounded border-white/20 bg-white/5 text-primary focus:ring-primary/50 transition-all"
                    />
                  </div>
                  <span className="text-sm font-medium text-text-secondary group-hover:text-white transition-colors">
                    Require explicit approval before execution
                  </span>
                </label>
              </div>
            </div>
          </div>

          {/* Documentation */}
          <div className="bg-white/5 border border-white/10 rounded-xl p-6 space-y-6">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider mb-4">Documentation</h3>

            {/* Original Description */}
            <div>
              <span className="block text-sm font-medium text-text-secondary mb-2">
                Original Description (from OpenAPI spec):
              </span>
              <div className="p-4 bg-black/20 rounded-xl border border-white/5 text-sm text-text-secondary">
                {endpoint.description || endpoint.summary || <span className="italic opacity-50">No description available</span>}
              </div>
            </div>

            {/* Enhanced Description */}
            <div>
              <label htmlFor="endpoint-enhanced-description" className="block text-sm font-medium text-text-secondary mb-2">
                Enhanced Description (shown to agent):
              </label>
              <textarea
                id="endpoint-enhanced-description"
                value={customDescription}
                onChange={(e) => setCustomDescription(e.target.value)}
                placeholder={`⚠️ CRITICAL: Add important context, gotchas, prerequisites...\n\nExample:\nThis endpoint requires X-API-Key header.\nCommon issue: Returns 422 if 'title' field is missing (despite spec saying optional).`}
                rows={6}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none font-mono text-sm"
              />
              <p className="text-xs text-text-tertiary mt-2 flex items-center gap-1">
                <AlertTriangle className="h-3 w-3" />
                This will be merged with the original description when shown to the agent
              </p>
            </div>

            {/* Admin Notes */}
            <div>
              <label htmlFor="endpoint-admin-notes" className="block text-sm font-medium text-text-secondary mb-2">
                Admin Notes (internal only):
              </label>
              <textarea
                id="endpoint-admin-notes"
                value={customNotes}
                onChange={(e) => setCustomNotes(e.target.value)}
                placeholder="Internal notes, policies, contacts, etc.\nExample: Contact security@company.com before enabling this endpoint."
                rows={3}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none text-sm"
              />
            </div>

            {/* Usage Examples */}
            <div>
              <label htmlFor="endpoint-usage-examples" className="block text-sm font-medium text-text-secondary mb-2">
                Usage Examples (JSON):
              </label>
              <textarea
                id="endpoint-usage-examples"
                value={examplesJson}
                onChange={(e) => setExamplesJson(e.target.value)}
                placeholder={`{\n  "path_params": {"owner": "myorg", "repo": "my-repo"},\n  "query_params": {},\n  "body": null\n}`}
                rows={6}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none font-mono text-sm"
              />
            </div>
          </div>

          {/* Error */}
          <AnimatePresence>
            {updateMutation.isError && (
              <motion.div
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="flex items-center gap-3 p-4 bg-red-500/10 border border-red-500/20 text-red-200 rounded-xl"
              >
                <AlertCircle className="h-5 w-5 text-red-400" />
                <span>{(updateMutation.error as Error).message}</span>
              </motion.div>
            )}

            {/* Success */}
            {updateMutation.isSuccess && (
              <motion.div
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                className="flex items-center gap-3 p-4 bg-green-500/10 border border-green-500/20 text-green-200 rounded-xl"
              >
                <CheckCircle className="h-5 w-5 text-green-400" />
                <span>Endpoint updated successfully!</span>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Actions */}
          <div className="flex gap-3 pt-6 border-t border-white/10">
            <button
              type="button"
              onClick={handleReset}
              disabled={updateMutation.isPending}
              className="px-4 py-2.5 border border-white/10 text-text-secondary hover:text-white hover:bg-white/5 rounded-xl transition-all disabled:opacity-50 flex items-center gap-2"
            >
              <RotateCcw className="h-4 w-4" />
              Reset
            </button>
            <div className="flex-1" />
            <button
              type="button"
              onClick={onClose}
              disabled={updateMutation.isPending}
              className="px-6 py-2.5 border border-white/10 text-text-secondary hover:text-white hover:bg-white/5 rounded-xl transition-all disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={updateMutation.isPending}
              className="flex items-center justify-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50"
            >
              {updateMutation.isPending ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Saving...
                </>
              ) : (
                <>
                  <Save className="h-5 w-5" />
                  Save Changes
                </>
              )}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}
