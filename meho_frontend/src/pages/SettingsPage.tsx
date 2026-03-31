// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Settings Page - Admin configuration for MEHO
 * 
 * TASK-77: Externalize Prompts & Models
 * 
 * Allows admins to configure:
 * - Installation context (added to system prompt)
 * - Model overrides
 * - Feature flags
 */
import { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { 
  Settings, Save, RotateCcw, Eye, EyeOff, History, 
  Cpu, Thermometer, MessageSquare, AlertCircle, Check
} from 'lucide-react';
import { getAPIClient } from '../lib/api-client';
import { config } from '../lib/config';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';

interface TenantConfig {
  tenant_id: string;
  installation_context?: string;
  model_override?: string;
  temperature_override?: number;
  features?: Record<string, unknown>;
  updated_by?: string;
  updated_at?: string;
  created_at?: string;
}

interface AuditEntry {
  field_changed: string;
  old_value?: string;
  new_value?: string;
  changed_by: string;
  changed_at: string;
}

interface AllowedModel {
  id: string;
  name: string;
  provider: string;
  recommended: boolean;
}

interface PromptPreview {
  system_prompt: string;
  character_count: number;
  has_tenant_context: boolean;
  model: string;
  temperature: number;
}

export function SettingsPage() {
  const [installationContext, setInstallationContext] = useState('');
  const [modelOverride, setModelOverride] = useState('');
  const [temperatureOverride, setTemperatureOverride] = useState<number | undefined>();
  const [showPromptPreview, setShowPromptPreview] = useState(false);
  const [showAuditLog, setShowAuditLog] = useState(false);
  
  const queryClient = useQueryClient();
  const apiClient = getAPIClient(config.apiURL);

  // Fetch current config
  const { data: currentConfig, isLoading } = useQuery<TenantConfig>({
    queryKey: ['admin-config'],
    queryFn: () => apiClient.getAdminConfig<TenantConfig>(),
  });

  // Fetch allowed models
  const { data: modelsData } = useQuery<{ allowed_models: AllowedModel[] }>({
    queryKey: ['allowed-models'],
    queryFn: () => apiClient.getAdminModels<{ allowed_models: AllowedModel[] }>(),
  });

  // Fetch prompt preview
  const { data: promptPreview, refetch: refetchPreview } = useQuery<PromptPreview>({
    queryKey: ['prompt-preview'],
    queryFn: () => apiClient.getPromptPreview<PromptPreview>(),
    enabled: showPromptPreview,
  });

  // Fetch audit log
  const { data: auditData } = useQuery<{ entries: AuditEntry[] }>({
    queryKey: ['config-audit'],
    queryFn: () => apiClient.getConfigAudit<{ entries: AuditEntry[] }>(),
    enabled: showAuditLog,
  });

  // Save config mutation
  const saveMutation = useMutation({
    mutationFn: (data: {
      installation_context?: string;
      model_override?: string;
      temperature_override?: number;
    }) => apiClient.updateAdminConfig<TenantConfig>(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-config'] });
      queryClient.invalidateQueries({ queryKey: ['prompt-preview'] });
      queryClient.invalidateQueries({ queryKey: ['config-audit'] });
      // hasChanges recomputes automatically from useMemo when currentConfig refreshes
    },
  });

  // Initialize form from loaded config (setState-during-render
  // avoids cascading re-renders from useEffect + synchronous setState)
  const [prevConfig, setPrevConfig] = useState<TenantConfig | undefined>(undefined);
  if (currentConfig !== prevConfig) {
    setPrevConfig(currentConfig);
    if (currentConfig) {
      setInstallationContext(currentConfig.installation_context || '');
      setModelOverride(currentConfig.model_override || '');
      setTemperatureOverride(currentConfig.temperature_override);
    }
  }

  // Track changes -- derived state computed during render (no effect needed)
  const hasChanges = useMemo(() => {
    if (!currentConfig) return false;
    return (
      installationContext !== (currentConfig.installation_context || '') ||
      modelOverride !== (currentConfig.model_override || '') ||
      temperatureOverride !== currentConfig.temperature_override
    );
  }, [installationContext, modelOverride, temperatureOverride, currentConfig]);

  const handleSave = () => {
    saveMutation.mutate({
      installation_context: installationContext || undefined,
      model_override: modelOverride || undefined,
      temperature_override: temperatureOverride,
    });
  };

  const handleReset = () => {
    if (currentConfig) {
      setInstallationContext(currentConfig.installation_context || '');
      setModelOverride(currentConfig.model_override || '');
      setTemperatureOverride(currentConfig.temperature_override);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-cyan-500"></div>
      </div>
    );
  }

  const models = modelsData?.allowed_models || [];

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900">
      <div className="max-w-4xl mx-auto p-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Settings className="h-8 w-8 text-cyan-400" />
            <div>
              <h1 className="text-2xl font-bold text-white">Settings</h1>
              <p className="text-slate-400 text-sm">Configure MEHO for your environment</p>
            </div>
          </div>
          
          <div className="flex gap-2">
            <button
              onClick={handleReset}
              disabled={!hasChanges}
              className={clsx(
                "flex items-center gap-2 px-4 py-2 rounded-lg transition-all",
                hasChanges 
                  ? "bg-slate-700 text-white hover:bg-slate-600" 
                  : "bg-slate-800 text-slate-500 cursor-not-allowed"
              )}
            >
              <RotateCcw className="h-4 w-4" />
              Reset
            </button>
            <button
              onClick={handleSave}
              disabled={!hasChanges || saveMutation.isPending}
              className={clsx(
                "flex items-center gap-2 px-4 py-2 rounded-lg transition-all",
                hasChanges 
                  ? "bg-cyan-600 text-white hover:bg-cyan-500" 
                  : "bg-slate-800 text-slate-500 cursor-not-allowed"
              )}
            >
              {saveMutation.isPending ? (
                <div className="animate-spin h-4 w-4 border-2 border-white/30 border-t-white rounded-full" />
              ) : (
                <Save className="h-4 w-4" />
              )}
              {saveMutation.isSuccess ? 'Saved!' : 'Save Changes'}
            </button>
          </div>
        </div>

        {/* Success/Error Messages */}
        <AnimatePresence>
          {saveMutation.isSuccess && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex items-center gap-2 p-4 bg-emerald-900/30 border border-emerald-500/30 rounded-lg text-emerald-300"
            >
              <Check className="h-5 w-5" />
              Configuration saved successfully!
            </motion.div>
          )}
          {saveMutation.isError && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="flex items-center gap-2 p-4 bg-red-900/30 border border-red-500/30 rounded-lg text-red-300"
            >
              <AlertCircle className="h-5 w-5" />
              Failed to save configuration. Please try again.
            </motion.div>
          )}
        </AnimatePresence>

        {/* Installation Context */}
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-6 space-y-4">
          <div className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-cyan-400" />
            <h2 className="text-lg font-semibold text-white">Installation Context</h2>
          </div>
          
          <p className="text-slate-400 text-sm">
            Describe your environment and how MEHO should behave. This context is added to 
            MEHO's system prompt under "Your Environment".
          </p>
          
          <textarea
            value={installationContext}
            onChange={(e) => setInstallationContext(e.target.value)}
            placeholder={`Example:

This MEHO instance is deployed for Acme Trading Corp.

You help engineers manage our trading platform:
- TradeX API (order management)
- MarketData service (real-time quotes)
- Kubernetes cluster (trading-prod, trading-staging)

Trading hours: 9:30 AM - 4:00 PM EST
Critical alerts: mention #trading-ops

When investigating trade failures, check:
1. Risk limits first
2. Market data connectivity
3. Order routing status`}
            className="w-full h-64 p-4 bg-slate-900 border border-slate-600 rounded-lg 
                       text-white placeholder-slate-500 resize-y
                       focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500"
          />
          
          <div className="flex justify-between items-center text-sm">
            <span className="text-slate-500">
              {installationContext.length} / 10,000 characters
            </span>
            <span className="text-slate-500">
              💡 Be specific about systems, workflows, and priorities
            </span>
          </div>
        </div>

        {/* Model Configuration */}
        <div className="bg-slate-800/50 border border-slate-700 rounded-xl p-6 space-y-4">
          <div className="flex items-center gap-2">
            <Cpu className="h-5 w-5 text-cyan-400" />
            <h2 className="text-lg font-semibold text-white">Model Settings</h2>
          </div>
          
          <div className="grid grid-cols-2 gap-6">
            {/* Model Selection */}
            <div className="space-y-2">
              <label htmlFor="settings-model-override" className="text-sm text-slate-400">Model Override</label>
              <select
                id="settings-model-override"
                value={modelOverride}
                onChange={(e) => setModelOverride(e.target.value)}
                className="w-full p-3 bg-slate-900 border border-slate-600 rounded-lg 
                           text-white focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              >
                <option value="">Use Default (from ENV)</option>
                {models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name} ({model.provider})
                    {model.recommended ? ' ★' : ''}
                  </option>
                ))}
              </select>
              <p className="text-xs text-slate-500">
                Override the default model. Leave empty to use STREAMING_AGENT_MODEL env var.
              </p>
            </div>
            
            {/* Temperature */}
            <div className="space-y-2">
              <label htmlFor="settings-temperature-override" className="text-sm text-slate-400 flex items-center gap-2">
                <Thermometer className="h-4 w-4" />
                Temperature Override
              </label>
              <input
                id="settings-temperature-override"
                type="number"
                min="0"
                max="2"
                step="0.1"
                value={temperatureOverride ?? ''}
                onChange={(e) => setTemperatureOverride(e.target.value ? parseFloat(e.target.value) : undefined)}
                placeholder="0.7"
                className="w-full p-3 bg-slate-900 border border-slate-600 rounded-lg 
                           text-white placeholder-slate-500
                           focus:outline-none focus:ring-2 focus:ring-cyan-500/50"
              />
              <p className="text-xs text-slate-500">
                0.0 = deterministic, 2.0 = creative. Leave empty for default.
              </p>
            </div>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex gap-4">
          {/* Prompt Preview */}
          <button
            onClick={() => {
              setShowPromptPreview(!showPromptPreview);
              if (!showPromptPreview) refetchPreview();
            }}
            className="flex items-center gap-2 px-4 py-2 bg-slate-700 text-white rounded-lg hover:bg-slate-600 transition-all"
          >
            {showPromptPreview ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            {showPromptPreview ? 'Hide' : 'Show'} Prompt Preview
          </button>
          
          {/* Audit Log */}
          <button
            onClick={() => setShowAuditLog(!showAuditLog)}
            className="flex items-center gap-2 px-4 py-2 bg-slate-700 text-white rounded-lg hover:bg-slate-600 transition-all"
          >
            <History className="h-4 w-4" />
            {showAuditLog ? 'Hide' : 'Show'} Audit Log
          </button>
        </div>

        {/* Prompt Preview Panel */}
        <AnimatePresence>
          {showPromptPreview && promptPreview && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="bg-slate-800/50 border border-slate-700 rounded-xl p-6 space-y-4"
            >
              <div className="flex items-center justify-between">
                <h3 className="text-lg font-semibold text-white">System Prompt Preview</h3>
                <div className="flex gap-4 text-sm text-slate-400">
                  <span>{promptPreview.character_count.toLocaleString()} characters</span>
                  <span>Model: {promptPreview.model}</span>
                  <span>Temp: {promptPreview.temperature}</span>
                  {promptPreview.has_tenant_context && (
                    <span className="text-cyan-400">✓ Has tenant context</span>
                  )}
                </div>
              </div>
              
              <pre className="p-4 bg-slate-900 rounded-lg text-slate-300 text-sm overflow-x-auto max-h-96 overflow-y-auto whitespace-pre-wrap">
                {promptPreview.system_prompt}
              </pre>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Audit Log Panel */}
        <AnimatePresence>
          {showAuditLog && auditData && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="bg-slate-800/50 border border-slate-700 rounded-xl p-6 space-y-4"
            >
              <h3 className="text-lg font-semibold text-white">Configuration Audit Log</h3>
              
              {auditData.entries.length === 0 ? (
                <p className="text-slate-400">No configuration changes recorded yet.</p>
              ) : (
                <div className="space-y-2">
                  {auditData.entries.map((entry, i) => (
                    <div key={i} className="p-3 bg-slate-900 rounded-lg text-sm">
                      <div className="flex justify-between items-center mb-1">
                        <span className="font-medium text-cyan-400">{entry.field_changed}</span>
                        <span className="text-slate-500">
                          {new Date(entry.changed_at).toLocaleString()} by {entry.changed_by}
                        </span>
                      </div>
                      <div className="text-slate-400">
                        {entry.old_value && <span className="line-through text-red-400/50 mr-2">{entry.old_value.slice(0, 100)}...</span>}
                        <span className="text-emerald-400">{entry.new_value?.slice(0, 100)}{(entry.new_value?.length || 0) > 100 ? '...' : ''}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>

        {/* Last Updated Info */}
        {currentConfig?.updated_at && (
          <div className="text-center text-sm text-slate-500">
            Last updated: {new Date(currentConfig.updated_at).toLocaleString()}
            {currentConfig.updated_by && ` by ${currentConfig.updated_by}`}
          </div>
        )}
      </div>
    </div>
  );
}

