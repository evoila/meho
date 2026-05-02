// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import type { ConnectorFormBaseProps, ArgocdFormState } from './types';

export interface ArgocdFormProps extends ConnectorFormBaseProps {
  state: ArgocdFormState;
  onChange: (patch: Partial<ArgocdFormState>) => void;
}

export function validateArgocdForm(state: ArgocdFormState): string | null {
  if (!state.serverUrl.trim()) return 'ArgoCD server URL is required';
  if (!state.apiToken.trim()) return 'ArgoCD API token is required';
  return null;
}

export function ArgocdForm({ state, onChange, submitting }: ArgocdFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="space-y-4"
    >
      <div className="p-4 bg-orange-500/5 border border-orange-500/20 rounded-xl">
        <h3 className="text-orange-400 font-medium text-sm mb-3">ArgoCD Connection</h3>
        <div className="space-y-4">
          <div>
            <label htmlFor="create-argo-server-url" className="block text-sm text-text-secondary mb-1.5">Server URL <span className="text-red-400">*</span></label>
            <input
              id="create-argo-server-url"
              type="url"
              value={state.serverUrl}
              onChange={(e) => onChange({ serverUrl: e.target.value })}
              placeholder="https://argocd.example.com"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
            />
          </div>
          <div>
            <label htmlFor="create-argo-api-token" className="block text-sm text-text-secondary mb-1.5">API Token <span className="text-red-400">*</span></label>
            <input
              id="create-argo-api-token"
              type="password"
              value={state.apiToken}
              onChange={(e) => onChange({ apiToken: e.target.value })}
              placeholder="Generated via argocd account generate-token or the UI"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
            />
          </div>
          <div>
            <label htmlFor="create-argo-routing-desc" className="block text-sm text-text-secondary mb-1.5">Routing Description</label>
            <input
              id="create-argo-routing-desc"
              type="text"
              value={state.routingDescription}
              onChange={(e) => onChange({ routingDescription: e.target.value })}
              placeholder="ArgoCD server for production GitOps deployments"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
            />
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={state.skipTls}
              onChange={(e) => onChange({ skipTls: e.target.checked })}
              disabled={submitting}
              className="rounded border-white/20 bg-surface text-orange-500 focus:ring-orange-500/50"
            />
            <span className="text-sm text-text-secondary">Skip TLS verification (self-signed certs)</span>
          </label>
        </div>
      </div>
    </motion.div>
  );
}
