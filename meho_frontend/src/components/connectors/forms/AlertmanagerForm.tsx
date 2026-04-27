// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import clsx from 'clsx';
import type { ConnectorFormBaseProps, AlertmanagerFormState } from './types';

export interface AlertmanagerFormProps extends ConnectorFormBaseProps {
  state: AlertmanagerFormState;
  onChange: (patch: Partial<AlertmanagerFormState>) => void;
}

export function validateAlertmanagerForm(state: AlertmanagerFormState): string | null {
  if (!state.baseUrl.trim()) return 'Alertmanager URL is required';
  if (state.authType === 'basic') {
    if (!state.username.trim()) return 'Username is required for Basic Auth';
    if (!state.password.trim()) return 'Password is required for Basic Auth';
  }
  if (state.authType === 'bearer' && !state.token.trim()) return 'Bearer token is required';
  return null;
}

export function AlertmanagerForm({ state, onChange, submitting }: AlertmanagerFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-red-500/5 rounded-xl border border-red-500/20"
    >
      <div className="flex items-center gap-2 text-red-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Alertmanager Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-alertmanager-url" className="block text-sm font-medium text-text-secondary mb-2">
            Alertmanager URL *
          </label>
          <input
            id="create-alertmanager-url"
            type="text"
            value={state.baseUrl}
            onChange={(e) => onChange({ baseUrl: e.target.value })}
            placeholder="http://alertmanager:9093"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">The Alertmanager server URL (e.g., http://alertmanager:9093)</p>
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
              <label htmlFor="create-alertmanager-username" className="block text-sm font-medium text-text-secondary mb-2">
                Username *
              </label>
              <input
                id="create-alertmanager-username"
                type="text"
                value={state.username}
                onChange={(e) => onChange({ username: e.target.value })}
                placeholder="alertmanager"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
              />
            </div>
            <div>
              <label htmlFor="create-alertmanager-password" className="block text-sm font-medium text-text-secondary mb-2">
                Password *
              </label>
              <input
                id="create-alertmanager-password"
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
            <label htmlFor="create-alertmanager-token" className="block text-sm font-medium text-text-secondary mb-2">
              Bearer Token *
            </label>
            <textarea
              id="create-alertmanager-token"
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
          <label htmlFor="create-alertmanager-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-alertmanager-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Production Alertmanager managing K8s cluster alerts in Graz datacenter"
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route alert queries to this Alertmanager instance.</p>
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
        <p className="font-medium">Alertmanager Connector</p>
        <p className="mt-2 text-xs opacity-80">
          Connect to Alertmanager for alert investigation and silence management.
          Supports active/silenced/inhibited alert listing, silence CRUD with trust approval,
          and cluster status monitoring.
        </p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Alertmanager server URL and authentication details</li>
          <li>MEHO will verify connectivity via /api/v2/status or /-/ready</li>
          <li>Operations include: alert listing, silence management, cluster status</li>
          <li>The agent can investigate alerts and manage silences through natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
