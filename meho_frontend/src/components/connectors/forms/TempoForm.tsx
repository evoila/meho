// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import clsx from 'clsx';
import type { ConnectorFormBaseProps, TempoFormState } from './types';

export interface TempoFormProps extends ConnectorFormBaseProps {
  state: TempoFormState;
  onChange: (patch: Partial<TempoFormState>) => void;
}

export function validateTempoForm(state: TempoFormState): string | null {
  if (!state.baseUrl.trim()) return 'Tempo URL is required';
  if (state.authType === 'basic') {
    if (!state.username.trim()) return 'Username is required for Basic Auth';
    if (!state.password.trim()) return 'Password is required for Basic Auth';
  }
  if (state.authType === 'bearer' && !state.token.trim()) return 'Bearer token is required';
  return null;
}

export function TempoForm({ state, onChange, submitting }: TempoFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-cyan-500/5 rounded-xl border border-cyan-500/20"
    >
      <div className="flex items-center gap-2 text-cyan-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Tempo Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-tempo-url" className="block text-sm font-medium text-text-secondary mb-2">
            Tempo URL *
          </label>
          <input
            id="create-tempo-url"
            type="text"
            value={state.baseUrl}
            onChange={(e) => onChange({ baseUrl: e.target.value })}
            placeholder="http://tempo:3200"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">The Tempo server URL (e.g., http://tempo:3200)</p>
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
                    ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-300'
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
              <label htmlFor="create-tempo-username" className="block text-sm font-medium text-text-secondary mb-2">
                Username *
              </label>
              <input
                id="create-tempo-username"
                type="text"
                value={state.username}
                onChange={(e) => onChange({ username: e.target.value })}
                placeholder="tempo"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
              />
            </div>
            <div>
              <label htmlFor="create-tempo-password" className="block text-sm font-medium text-text-secondary mb-2">
                Password *
              </label>
              <input
                id="create-tempo-password"
                type="password"
                value={state.password}
                onChange={(e) => onChange({ password: e.target.value })}
                placeholder="Enter password"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
              />
            </div>
          </>
        )}

        {state.authType === 'bearer' && (
          <div className="col-span-2">
            <label htmlFor="create-tempo-token" className="block text-sm font-medium text-text-secondary mb-2">
              Bearer Token *
            </label>
            <textarea
              id="create-tempo-token"
              value={state.token}
              onChange={(e) => onChange({ token: e.target.value })}
              placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
              rows={3}
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all font-mono text-sm resize-none"
            />
            <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
          </div>
        )}

        <div className="col-span-2">
          <label htmlFor="create-tempo-org-id" className="block text-sm font-medium text-text-secondary mb-2">
            Org ID (Multi-Tenant)
          </label>
          <input
            id="create-tempo-org-id"
            type="text"
            value={state.orgId}
            onChange={(e) => onChange({ orgId: e.target.value })}
            placeholder="my-tenant"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Optional tenant org ID for multi-tenant Tempo deployments (sets X-Scope-OrgID header)</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-tempo-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-tempo-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Production Tempo receiving traces from K8s microservices in Graz datacenter"
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route trace queries to this Tempo instance.</p>
        </div>

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.skipTls}
              onChange={(e) => onChange({ skipTls: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-cyan-500 focus:ring-cyan-500/50"
            />
            <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Enable for self-signed certificates (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-cyan-500/10 rounded-lg border border-cyan-500/20 text-cyan-300 text-sm">
        <p className="font-medium">Tempo Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Tempo server URL and authentication details</li>
          <li>MEHO will verify connectivity via /api/status/buildinfo or /ready</li>
          <li>Operations include: trace search, service graph, tag discovery, span details</li>
          <li>The agent can investigate distributed traces and latency through natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
