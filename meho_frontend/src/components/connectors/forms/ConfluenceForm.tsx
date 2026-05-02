// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, ConfluenceFormState } from './types';

export interface ConfluenceFormProps extends ConnectorFormBaseProps {
  state: ConfluenceFormState;
  onChange: (patch: Partial<ConfluenceFormState>) => void;
}

export function validateConfluenceForm(state: ConfluenceFormState): string | null {
  if (!state.siteUrl.trim()) return 'Confluence site URL is required';
  if (!state.email.trim()) return 'Email is required';
  if (!state.apiToken.trim()) return 'API token is required';
  return null;
}

export function ConfluenceForm({ state, onChange, submitting }: ConfluenceFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-[#1868DB]/30"
    >
      <div className="flex items-center gap-2 text-[#1868DB] text-sm font-medium">
        <Server className="h-4 w-4" />
        Confluence Cloud Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-confluence-site-url" className="block text-sm font-medium text-text-secondary mb-2">
            Confluence Site URL *
          </label>
          <input
            id="create-confluence-site-url"
            type="text"
            value={state.siteUrl}
            onChange={(e) => onChange({ siteUrl: e.target.value })}
            placeholder="https://your-domain.atlassian.net"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Your Atlassian site URL (same as Jira if on the same instance)</p>
        </div>

        <div>
          <label htmlFor="create-confluence-email" className="block text-sm font-medium text-text-secondary mb-2">
            Email *
          </label>
          <input
            id="create-confluence-email"
            type="email"
            value={state.email}
            onChange={(e) => onChange({ email: e.target.value })}
            placeholder="user@company.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Atlassian account email</p>
        </div>

        <div>
          <label htmlFor="create-confluence-api-token" className="block text-sm font-medium text-text-secondary mb-2">
            API Token *
          </label>
          <input
            id="create-confluence-api-token"
            type="password"
            value={state.apiToken}
            onChange={(e) => onChange({ apiToken: e.target.value })}
            placeholder="Your Atlassian API token"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all font-mono text-sm"
          />
          <p className="text-xs text-text-tertiary mt-1">Generate at id.atlassian.com/manage-profile/security/api-tokens</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-confluence-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-confluence-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Confluence wiki for runbooks and documentation"
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route documentation queries to this Confluence instance</p>
        </div>
      </div>

      <div className="p-3 bg-[#1868DB]/10 rounded-lg border border-[#1868DB]/20 text-blue-300 text-sm">
        <p className="font-medium">Confluence Cloud Connector</p>
        <p className="mt-2 text-xs opacity-80">
          Connect to Confluence Cloud for searching, reading, and creating wiki pages and spaces.
          Operations include: search, get page, create page, update page, list spaces, and get space.
        </p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Confluence Cloud site URL</li>
          <li>Enter your Atlassian email and API token</li>
          <li>MEHO will verify connectivity and list accessible spaces</li>
        </ol>
      </div>
    </motion.div>
  );
}
