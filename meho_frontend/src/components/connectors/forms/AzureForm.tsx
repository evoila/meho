// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, AzureFormState } from './types';

export interface AzureFormProps extends ConnectorFormBaseProps {
  state: AzureFormState;
  onChange: (patch: Partial<AzureFormState>) => void;
}

export function validateAzureForm(state: AzureFormState): string | null {
  if (!state.tenantId.trim()) return 'Azure Tenant ID is required';
  if (!state.clientId.trim()) return 'Azure Client ID is required';
  if (!state.clientSecret.trim()) return 'Azure Client Secret is required';
  if (!state.subscriptionId.trim()) return 'Azure Subscription ID is required';
  return null;
}

export function AzureForm({ state, onChange, submitting }: AzureFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
    >
      <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Microsoft Azure Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label htmlFor="create-azure-tenant-id" className="block text-sm font-medium text-text-secondary mb-2">
            Tenant ID *
          </label>
          <input
            id="create-azure-tenant-id"
            type="text"
            value={state.tenantId}
            onChange={(e) => onChange({ tenantId: e.target.value })}
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Azure Active Directory tenant ID</p>
        </div>

        <div>
          <label htmlFor="create-azure-subscription-id" className="block text-sm font-medium text-text-secondary mb-2">
            Subscription ID *
          </label>
          <input
            id="create-azure-subscription-id"
            type="text"
            value={state.subscriptionId}
            onChange={(e) => onChange({ subscriptionId: e.target.value })}
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Target Azure subscription</p>
        </div>

        <div>
          <label htmlFor="create-azure-client-id" className="block text-sm font-medium text-text-secondary mb-2">
            Client ID (Application ID) *
          </label>
          <input
            id="create-azure-client-id"
            type="text"
            value={state.clientId}
            onChange={(e) => onChange({ clientId: e.target.value })}
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Service principal application (client) ID</p>
        </div>

        <div>
          <label htmlFor="create-azure-client-secret" className="block text-sm font-medium text-text-secondary mb-2">
            Client Secret *
          </label>
          <input
            id="create-azure-client-secret"
            type="password"
            value={state.clientSecret}
            onChange={(e) => onChange({ clientSecret: e.target.value })}
            placeholder="Service principal client secret"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Service principal client secret value</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-azure-resource-group-filter" className="block text-sm font-medium text-text-secondary mb-2">
            Resource Group Filter (optional)
          </label>
          <input
            id="create-azure-resource-group-filter"
            type="text"
            value={state.resourceGroupFilter}
            onChange={(e) => onChange({ resourceGroupFilter: e.target.value })}
            placeholder="Leave empty for all resource groups"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Limit operations to a specific resource group (optional)</p>
        </div>
      </div>

      <div className="text-xs text-text-tertiary space-y-1 mt-2">
        <p className="font-medium text-text-secondary">Setup instructions:</p>
        <ol className="list-decimal list-inside space-y-0.5 ml-1">
          <li>Create a Service Principal in Azure AD (App registrations)</li>
          <li>Assign Reader role on the target subscription</li>
          <li>Create a client secret and copy the value</li>
          <li>MEHO will register 42 Azure operations for: Compute, Monitor, AKS, Networking, Storage, Web</li>
        </ol>
      </div>
    </motion.div>
  );
}
