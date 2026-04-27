// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, VmwareFormState } from './types';

export interface VmwareFormProps extends ConnectorFormBaseProps {
  state: VmwareFormState;
  onChange: (patch: Partial<VmwareFormState>) => void;
}

export function validateVmwareForm(state: VmwareFormState): string | null {
  if (!state.host.trim()) return 'vCenter host is required';
  if (!state.username.trim()) return 'vCenter username is required';
  if (!state.password.trim()) return 'vCenter password is required';
  return null;
}

export function VmwareForm({ state, onChange, submitting }: VmwareFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-emerald-500/5 rounded-xl border border-emerald-500/20"
    >
      <div className="flex items-center gap-2 text-emerald-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        VMware vSphere Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2 md:col-span-1">
          <label htmlFor="create-vcenter-host" className="block text-sm font-medium text-text-secondary mb-2">
            vCenter Host *
          </label>
          <input
            id="create-vcenter-host"
            type="text"
            value={state.host}
            onChange={(e) => onChange({ host: e.target.value })}
            placeholder="vcenter.example.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Hostname or IP only (no https://)</p>
        </div>

        <div>
          <label htmlFor="create-vcenter-port" className="block text-sm font-medium text-text-secondary mb-2">
            Port
          </label>
          <input
            id="create-vcenter-port"
            type="number"
            value={state.port}
            onChange={(e) => onChange({ port: parseInt(e.target.value) || 443 })}
            min="1"
            max="65535"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
          />
        </div>

        <div>
          <label htmlFor="create-vcenter-username" className="block text-sm font-medium text-text-secondary mb-2">
            Username *
          </label>
          <input
            id="create-vcenter-username"
            type="text"
            value={state.username}
            onChange={(e) => onChange({ username: e.target.value })}
            placeholder="administrator@vsphere.local"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
          />
        </div>

        <div>
          <label htmlFor="create-vcenter-password" className="block text-sm font-medium text-text-secondary mb-2">
            Password *
          </label>
          <input
            id="create-vcenter-password"
            type="password"
            value={state.password}
            onChange={(e) => onChange({ password: e.target.value })}
            placeholder="••••••••"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
          />
        </div>

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.disableSsl}
              onChange={(e) => onChange({ disableSsl: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-emerald-500 focus:ring-emerald-500/50"
            />
            <span className="text-sm text-text-secondary">Disable SSL Certificate Verification</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Enable for self-signed certificates (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-emerald-500/10 rounded-lg border border-emerald-500/20 text-emerald-300 text-sm">
        <p className="font-medium">🖥️ VMware vSphere Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your vCenter Server details and credentials</li>
          <li>MEHO will connect and register 174+ VMware operations</li>
          <li>Operations include: VM power, snapshots, vMotion, DRS, HA, and more</li>
          <li>The agent can manage your vSphere environment via natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
