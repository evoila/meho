// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, KubernetesFormState } from './types';

export interface KubernetesFormProps extends ConnectorFormBaseProps {
  state: KubernetesFormState;
  onChange: (patch: Partial<KubernetesFormState>) => void;
}

export function validateKubernetesForm(state: KubernetesFormState): string | null {
  if (!state.serverUrl.trim()) return 'Kubernetes API server URL is required';
  if (!state.token.trim()) return 'Service Account token is required';
  return null;
}

export function KubernetesForm({ state, onChange, submitting }: KubernetesFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
    >
      <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Kubernetes Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-k8s-server-url" className="block text-sm font-medium text-text-secondary mb-2">
            API Server URL *
          </label>
          <input
            id="create-k8s-server-url"
            type="text"
            value={state.serverUrl}
            onChange={(e) => onChange({ serverUrl: e.target.value })}
            placeholder="https://10.5.27.3:6443"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">The Kubernetes API server endpoint (e.g., from kubectl cluster-info)</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-k8s-token" className="block text-sm font-medium text-text-secondary mb-2">
            Service Account Token *
          </label>
          <textarea
            id="create-k8s-token"
            value={state.token}
            onChange={(e) => onChange({ token: e.target.value })}
            placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
            rows={4}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Bearer token from a Kubernetes Service Account (use kubectl create token)</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-k8s-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-k8s-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Production Kubernetes cluster (RKE2) in Graz datacenter. Query for pods, deployments, services, nodes, namespaces."
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route queries to this connector. Describe what this cluster manages.</p>
        </div>

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.skipTls}
              onChange={(e) => onChange({ skipTls: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-blue-500 focus:ring-blue-500/50"
            />
            <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Enable for self-signed certificates in lab/dev environments (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-blue-500/10 rounded-lg border border-blue-500/20 text-blue-300 text-sm">
        <p className="font-medium">☸️ Kubernetes Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your K8s API server URL and Service Account token</li>
          <li>MEHO will connect and register 49 Kubernetes operations</li>
          <li>Operations include: pods, deployments, services, nodes, and more</li>
          <li>The agent can query and manage your cluster via natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
