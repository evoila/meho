// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * AutomationToggle
 *
 * Admin-only toggle to enable/disable a connector for automated session access.
 * Lives in connector settings alongside existing safety controls.
 *
 * Phase 75: CRED-08
 */
import { useState } from 'react';
import { Info } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';

interface AutomationToggleProps {
  connectorId: string;
  automationEnabled: boolean;
  onUpdate?: (enabled: boolean) => void;
}

export function AutomationToggle({ connectorId, automationEnabled, onUpdate }: Readonly<AutomationToggleProps>) {
  const [enabled, setEnabled] = useState(automationEnabled);
  const apiClient = getAPIClient(config.apiURL);

  const mutation = useMutation({
    mutationFn: async (newValue: boolean) => {
      await apiClient.updateConnector(connectorId, { automation_enabled: newValue });
      return newValue;
    },
    onSuccess: (newValue) => {
      setEnabled(newValue);
      onUpdate?.(newValue);
      toast.success('Automation access updated');
    },
    onError: () => {
      toast.error('Failed to update automation access');
    },
  });

  const handleToggle = () => {
    mutation.mutate(!enabled);
  };

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-semibold text-text-primary border-b border-white/5 pb-2">
        Automation Access
      </h4>
      <div className="flex items-center gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          onClick={handleToggle}
          disabled={mutation.isPending}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 ${
            enabled ? 'bg-[var(--color-primary-500)]' : 'bg-[var(--color-surface-active)]'
          } ${mutation.isPending ? 'opacity-60 cursor-wait' : ''}`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full shadow ring-0 transition duration-200 ease-in-out ${
              enabled ? 'translate-x-5 bg-white' : 'translate-x-0 bg-text-tertiary'
            }`}
          />
        </button>
        <span className="text-sm text-text-primary">
          Available to automated sessions (events and scheduled tasks)
        </span>
      </div>
      <p className="flex items-center gap-1.5 text-xs text-text-tertiary">
        <Info className="h-3 w-3 shrink-0" />
        When disabled, automated sessions cannot use this connector.
      </p>
    </div>
  );
}
