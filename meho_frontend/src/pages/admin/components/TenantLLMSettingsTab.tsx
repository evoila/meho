// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant LLM Settings Tab
 * 
 * Configure LLM overrides: model, temperature, installation context.
 */
import { useState, useMemo } from 'react';
import { Input, Button } from '@/shared';
import type { Tenant, UpdateTenantRequest } from '@/api/types';

interface TenantLLMSettingsTabProps {
  tenant: Tenant;
  onUpdate: (request: UpdateTenantRequest) => Promise<void>;
  isUpdating: boolean;
}

const AVAILABLE_MODELS = [
  { value: '', label: 'Use Default' },
  { value: 'openai:gpt-4.1-mini', label: 'GPT-4.1 Mini' },
  { value: 'openai:gpt-4.1', label: 'GPT-4.1' },
];

export function TenantLLMSettingsTab({ tenant, onUpdate, isUpdating }: TenantLLMSettingsTabProps) {
  // Initialize state from tenant props
  const [modelOverride, setModelOverride] = useState(tenant.model_override || '');
  const [temperatureOverride, setTemperatureOverride] = useState<number | undefined>(
    tenant.temperature_override ?? undefined
  );
  const [installationContext, setInstallationContext] = useState(tenant.installation_context || '');

  // Derive hasChanges from current state vs tenant props
  const hasChanges = useMemo(() => {
    return (
      modelOverride !== (tenant.model_override || '') ||
      temperatureOverride !== (tenant.temperature_override ?? undefined) ||
      installationContext !== (tenant.installation_context || '')
    );
  }, [modelOverride, temperatureOverride, installationContext, tenant.model_override, tenant.temperature_override, tenant.installation_context]);

  const handleSave = async () => {
    await onUpdate({
      model_override: modelOverride || undefined,
      temperature_override: temperatureOverride,
      installation_context: installationContext || undefined,
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-medium text-white mb-2">LLM Configuration</h3>
        <p className="text-sm text-text-secondary">
          Override default LLM settings for this tenant. Leave empty to use system defaults.
        </p>
      </div>

      {/* Model Override */}
      <div>
        <label htmlFor="tenant-model-override" className="block text-sm font-medium text-text-primary mb-2">
          Model Override
        </label>
        <select
          id="tenant-model-override"
          value={modelOverride}
          onChange={(e) => setModelOverride(e.target.value)}
          className="w-full max-w-md px-3 py-2 bg-surface border border-border rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-primary/50"
          disabled={isUpdating}
        >
          {AVAILABLE_MODELS.map((model) => (
            <option key={model.value} value={model.value}>
              {model.label}
            </option>
          ))}
        </select>
        <p className="mt-1.5 text-xs text-text-secondary">
          Select a specific model or use the system default.
        </p>
      </div>

      {/* Temperature Override */}
      <div>
        <label htmlFor="tenant-temperature-override" className="block text-sm font-medium text-text-primary mb-2">
          Temperature Override
        </label>
        <div className="flex items-center gap-4 max-w-md">
          <input
            id="tenant-temperature-override"
            type="range"
            min="0"
            max="2"
            step="0.1"
            value={temperatureOverride ?? 0.7}
            onChange={(e) => setTemperatureOverride(parseFloat(e.target.value))}
            className="flex-1 h-2 bg-surface rounded-lg appearance-none cursor-pointer accent-primary"
            disabled={isUpdating}
          />
          <Input
            type="number"
            value={temperatureOverride ?? ''}
            onChange={(e) => {
              const val = e.target.value ? parseFloat(e.target.value) : undefined;
              if (val === undefined || (val >= 0 && val <= 2)) {
                setTemperatureOverride(val);
              }
            }}
            placeholder="Default"
            min={0}
            max={2}
            step={0.1}
            className="w-24"
            disabled={isUpdating}
          />
        </div>
        <p className="mt-1.5 text-xs text-text-secondary">
          Range 0-2. Lower values are more deterministic, higher values are more creative.
        </p>
      </div>

      {/* Installation Context */}
      <div>
        <label htmlFor="tenant-installation-context" className="block text-sm font-medium text-text-primary mb-2">
          Installation Context
        </label>
        <textarea
          id="tenant-installation-context"
          value={installationContext}
          onChange={(e) => setInstallationContext(e.target.value)}
          placeholder="Add custom context about this tenant's environment, systems, or preferences..."
          rows={6}
          maxLength={10000}
          className="w-full px-3 py-2 bg-surface border border-border rounded-lg text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
          disabled={isUpdating}
        />
        <div className="flex justify-between mt-1.5">
          <p className="text-xs text-text-secondary">
            This context is included in the system prompt for all conversations.
          </p>
          <p className="text-xs text-text-secondary">
            {installationContext.length}/10000
          </p>
        </div>
      </div>

      {/* Info Box */}
      <div className="p-4 bg-surface/30 border border-border rounded-lg">
        <h4 className="text-sm font-medium text-white mb-2">About LLM Settings</h4>
        <ul className="text-sm text-text-secondary space-y-1 list-disc list-inside">
          <li>Model overrides affect all AI interactions for this tenant.</li>
          <li>Temperature affects creativity vs. consistency in responses.</li>
          <li>Installation context helps the AI understand the tenant's environment.</li>
        </ul>
      </div>

      {/* Save Button */}
      <div className="flex justify-end pt-4 border-t border-border">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={!hasChanges}
          isLoading={isUpdating}
        >
          Save LLM Settings
        </Button>
      </div>
    </div>
  );
}
