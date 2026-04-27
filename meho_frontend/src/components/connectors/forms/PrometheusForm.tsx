// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import clsx from 'clsx';
import type { ConnectorFormBaseProps, PrometheusFormState } from './types';

export interface PrometheusFormProps extends ConnectorFormBaseProps {
  state: PrometheusFormState;
  onChange: (patch: Partial<PrometheusFormState>) => void;
}

export function validatePrometheusForm(state: PrometheusFormState): string | null {
  if (!state.baseUrl.trim()) return 'Prometheus URL is required';
  if (state.authType === 'basic') {
    if (!state.username.trim()) return 'Username is required for Basic Auth';
    if (!state.password.trim()) return 'Password is required for Basic Auth';
  }
  if (state.authType === 'bearer' && !state.token.trim()) return 'Bearer token is required';
  return null;
}

export function PrometheusForm({ state, onChange, submitting }: PrometheusFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-red-500/5 rounded-xl border border-red-500/20"
    >
      <div className="flex items-center gap-2 text-red-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Prometheus Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-prometheus-url" className="block text-sm font-medium text-text-secondary mb-2">
            Prometheus URL *
          </label>
          <input
            id="create-prometheus-url"
            type="text"
            value={state.baseUrl}
            onChange={(e) => onChange({ baseUrl: e.target.value })}
            placeholder="http://prometheus:9090"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">The Prometheus server URL (e.g., http://prometheus:9090)</p>
        </div>

        <div className="col-span-2">
          <span className="block text-sm font-medium text-text-secondary mb-2">
            Authentication Type
          </span>
          <div className="grid grid-cols-3 gap-3">
            {[
              { id: 'none' as const, label: 'No Auth' },
              { id: 'basic' as const, label: 'Basic Auth' },
              { id: 'bearer' as const, label: 'Bearer Token' },
            ].map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => onChange({ authType: option.id })}
                disabled={submitting}
                className={clsx(
                  'px-4 py-2.5 rounded-lg border text-sm font-medium transition-all',
                  state.authType === option.id
                    ? 'border-red-500/50 bg-red-500/10 text-red-300'
                    : 'border-white/10 bg-surface text-text-secondary hover:border-white/20'
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        {state.authType === 'basic' && (
          <>
            <div>
              <label htmlFor="create-prometheus-username" className="block text-sm font-medium text-text-secondary mb-2">
                Username *
              </label>
              <input
                id="create-prometheus-username"
                type="text"
                value={state.username}
                onChange={(e) => onChange({ username: e.target.value })}
                placeholder="prometheus"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
              />
            </div>
            <div>
              <label htmlFor="create-prometheus-password" className="block text-sm font-medium text-text-secondary mb-2">
                Password *
              </label>
              <input
                id="create-prometheus-password"
                type="password"
                value={state.password}
                onChange={(e) => onChange({ password: e.target.value })}
                placeholder="Enter password"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
              />
            </div>
          </>
        )}

        {state.authType === 'bearer' && (
          <div className="col-span-2">
            <label htmlFor="create-prometheus-token" className="block text-sm font-medium text-text-secondary mb-2">
              Bearer Token *
            </label>
            <textarea
              id="create-prometheus-token"
              value={state.token}
              onChange={(e) => onChange({ token: e.target.value })}
              placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
              rows={3}
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all font-mono text-sm resize-none"
            />
            <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
          </div>
        )}

        <div className="col-span-2">
          <label htmlFor="create-prometheus-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-prometheus-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Production Prometheus monitoring K8s cluster in Graz datacenter. Metrics for pods, nodes, services."
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route metric queries to this Prometheus instance.</p>
        </div>

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.skipTls}
              onChange={(e) => onChange({ skipTls: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-red-500 focus:ring-red-500/50"
            />
            <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Enable for self-signed certificates (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-red-500/10 rounded-lg border border-red-500/20 text-red-300 text-sm">
        <p className="font-medium">Prometheus Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Prometheus server URL and authentication details</li>
          <li>MEHO will verify connectivity via /api/v1/status/buildinfo</li>
          <li>Operations include: CPU/memory metrics, RED metrics, scrape targets, alerts</li>
          <li>The agent can investigate infrastructure health through natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
