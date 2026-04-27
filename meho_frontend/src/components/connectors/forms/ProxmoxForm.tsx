// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import clsx from 'clsx';
import type { ConnectorFormBaseProps, ProxmoxFormState } from './types';

export interface ProxmoxFormProps extends ConnectorFormBaseProps {
  state: ProxmoxFormState;
  onChange: (patch: Partial<ProxmoxFormState>) => void;
}

export function validateProxmoxForm(state: ProxmoxFormState): string | null {
  if (!state.host.trim()) return 'Proxmox host is required';
  if (state.authType === 'password') {
    if (!state.username.trim()) return 'Proxmox username is required';
    if (!state.password.trim()) return 'Proxmox password is required';
  } else {
    if (!state.tokenId.trim()) return 'API Token ID is required';
    if (!state.tokenSecret.trim()) return 'API Token Secret is required';
  }
  return null;
}

export function ProxmoxForm({ state, onChange, submitting }: ProxmoxFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-orange-500/5 rounded-xl border border-orange-500/20"
    >
      <div className="flex items-center gap-2 text-orange-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Proxmox VE Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2 md:col-span-1">
          <label htmlFor="create-proxmox-host" className="block text-sm font-medium text-text-secondary mb-2">
            Proxmox Host *
          </label>
          <input
            id="create-proxmox-host"
            type="text"
            value={state.host}
            onChange={(e) => onChange({ host: e.target.value })}
            placeholder="proxmox.example.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Hostname or IP only (no https://)</p>
        </div>

        <div>
          <label htmlFor="create-proxmox-port" className="block text-sm font-medium text-text-secondary mb-2">
            Port
          </label>
          <input
            id="create-proxmox-port"
            type="number"
            value={state.port}
            onChange={(e) => onChange({ port: parseInt(e.target.value) || 8006 })}
            min="1"
            max="65535"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
          />
        </div>

        <div className="col-span-2">
          <span className="block text-sm font-medium text-text-secondary mb-2">
            Authentication Method
          </span>
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => onChange({ authType: 'password' })}
              className={clsx(
                "px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                state.authType === 'password'
                  ? "bg-orange-500/10 border-orange-500/50 text-white"
                  : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
              )}
            >
              Username/Password
            </button>
            <button
              type="button"
              onClick={() => onChange({ authType: 'token' })}
              className={clsx(
                "px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                state.authType === 'token'
                  ? "bg-orange-500/10 border-orange-500/50 text-white"
                  : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
              )}
            >
              API Token (Recommended)
            </button>
          </div>
        </div>

        {state.authType === 'password' ? (
          <>
            <div>
              <label htmlFor="create-proxmox-username" className="block text-sm font-medium text-text-secondary mb-2">
                Username *
              </label>
              <input
                id="create-proxmox-username"
                type="text"
                value={state.username}
                onChange={(e) => onChange({ username: e.target.value })}
                placeholder="root@pam"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
              />
            </div>

            <div>
              <label htmlFor="create-proxmox-password" className="block text-sm font-medium text-text-secondary mb-2">
                Password *
              </label>
              <input
                id="create-proxmox-password"
                type="password"
                value={state.password}
                onChange={(e) => onChange({ password: e.target.value })}
                placeholder="••••••••"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
              />
            </div>
          </>
        ) : (
          <>
            <div>
              <label htmlFor="create-proxmox-token-id" className="block text-sm font-medium text-text-secondary mb-2">
                API Token ID *
              </label>
              <input
                id="create-proxmox-token-id"
                type="text"
                value={state.tokenId}
                onChange={(e) => onChange({ tokenId: e.target.value })}
                placeholder="user@realm!tokenname"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
              />
              <p className="text-xs text-text-tertiary mt-1">Format: user@realm!tokenname</p>
            </div>

            <div>
              <label htmlFor="create-proxmox-token-secret" className="block text-sm font-medium text-text-secondary mb-2">
                API Token Secret *
              </label>
              <input
                id="create-proxmox-token-secret"
                type="password"
                value={state.tokenSecret}
                onChange={(e) => onChange({ tokenSecret: e.target.value })}
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
              />
            </div>
          </>
        )}

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.disableSsl}
              onChange={(e) => onChange({ disableSsl: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-orange-500 focus:ring-orange-500/50"
            />
            <span className="text-sm text-text-secondary">Disable SSL Certificate Verification</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Enable for self-signed certificates (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-orange-500/10 rounded-lg border border-orange-500/20 text-orange-300 text-sm">
        <p className="font-medium">🖥️ Proxmox VE Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Proxmox VE host details and credentials</li>
          <li>MEHO will connect and register 40+ Proxmox operations</li>
          <li>Operations include: VMs, LXC containers, snapshots, storage, and more</li>
          <li>The agent can manage your Proxmox environment via natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
