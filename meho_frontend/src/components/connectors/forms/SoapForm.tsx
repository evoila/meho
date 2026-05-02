// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { FileCode } from 'lucide-react';
import type { ConnectorFormBaseProps, SoapFormState } from './types';

export interface SoapFormProps extends ConnectorFormBaseProps {
  state: SoapFormState;
  onChange: (patch: Partial<SoapFormState>) => void;
}

export function validateSoapForm(state: SoapFormState): string | null {
  if (!state.wsdlUrl.trim()) return 'WSDL URL is required for SOAP connectors';
  return null;
}

export function SoapForm({ state, onChange, submitting }: SoapFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-amber-500/5 rounded-xl border border-amber-500/20"
    >
      <div className="flex items-center gap-2 text-amber-400 text-sm font-medium">
        <FileCode className="h-4 w-4" />
        SOAP Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-wsdl-url" className="block text-sm font-medium text-text-secondary mb-2">
            WSDL URL *
          </label>
          <input
            id="create-wsdl-url"
            type="url"
            value={state.wsdlUrl}
            onChange={(e) => onChange({ wsdlUrl: e.target.value })}
            placeholder="https://vcenter.local/sdk/vimService.wsdl"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">URL to the WSDL service description file</p>
        </div>

        <div>
          <label htmlFor="create-soap-auth-type" className="block text-sm font-medium text-text-secondary mb-2">
            SOAP Auth Type
          </label>
          <select
            id="create-soap-auth-type"
            value={state.authType}
            onChange={(e) => onChange({ authType: e.target.value as 'none' | 'basic' | 'session' })}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all appearance-none"
          >
            <option value="none">No Auth</option>
            <option value="basic">HTTP Basic Auth</option>
            <option value="session">Session Based (VMware)</option>
          </select>
          <p className="text-xs text-text-tertiary mt-1">
            Session-based is recommended for VMware VIM API
          </p>
        </div>

        <div>
          <label htmlFor="create-soap-timeout" className="block text-sm font-medium text-text-secondary mb-2">
            Timeout (seconds)
          </label>
          <input
            id="create-soap-timeout"
            type="number"
            value={state.timeout}
            onChange={(e) => onChange({ timeout: parseInt(e.target.value) || 30 })}
            min="5"
            max="300"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
          />
        </div>

        <div className="col-span-2">
          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={state.verifySsl}
              onChange={(e) => onChange({ verifySsl: e.target.checked })}
              disabled={submitting}
              className="w-5 h-5 rounded border-white/20 bg-surface text-primary focus:ring-primary/50"
            />
            <span className="text-sm text-text-secondary">Verify SSL Certificate</span>
          </label>
          <p className="text-xs text-text-tertiary mt-1 ml-8">
            Disable for self-signed certificates (not recommended for production)
          </p>
        </div>
      </div>

      <div className="p-3 bg-amber-500/10 rounded-lg border border-amber-500/20 text-amber-300 text-sm">
        <p className="font-medium">📋 SOAP Connector Setup</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Create the connector with WSDL URL</li>
          <li>Set your credentials in the Credentials section</li>
          <li>MEHO will parse the WSDL and discover all operations</li>
          <li>The agent can then call SOAP operations via natural language</li>
        </ol>
      </div>
    </motion.div>
  );
}
