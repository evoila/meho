// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, JiraFormState } from './types';

export interface JiraFormProps extends ConnectorFormBaseProps {
  state: JiraFormState;
  onChange: (patch: Partial<JiraFormState>) => void;
}

export function validateJiraForm(state: JiraFormState): string | null {
  if (!state.siteUrl.trim()) return 'Jira site URL is required';
  if (!state.email.trim()) return 'Email is required';
  if (!state.apiToken.trim()) return 'API token is required';
  return null;
}

export function JiraForm({ state, onChange, submitting }: JiraFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
    >
      <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Jira Cloud Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2">
          <label htmlFor="create-jira-site-url" className="block text-sm font-medium text-text-secondary mb-2">
            Jira Site URL *
          </label>
          <input
            id="create-jira-site-url"
            type="text"
            value={state.siteUrl}
            onChange={(e) => onChange({ siteUrl: e.target.value })}
            placeholder="https://yoursite.atlassian.net"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Your Jira Cloud site URL</p>
        </div>

        <div>
          <label htmlFor="create-jira-email" className="block text-sm font-medium text-text-secondary mb-2">
            Email *
          </label>
          <input
            id="create-jira-email"
            type="email"
            value={state.email}
            onChange={(e) => onChange({ email: e.target.value })}
            placeholder="user@company.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Atlassian account email</p>
        </div>

        <div>
          <label htmlFor="create-jira-api-token" className="block text-sm font-medium text-text-secondary mb-2">
            API Token *
          </label>
          <input
            id="create-jira-api-token"
            type="password"
            value={state.apiToken}
            onChange={(e) => onChange({ apiToken: e.target.value })}
            placeholder="Your Atlassian API token"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm"
          />
          <p className="text-xs text-text-tertiary mt-1">Generate at id.atlassian.com/manage-profile/security/api-tokens</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-jira-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-jira-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Production Jira tracking engineering team issues"
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route issue queries to this Jira instance</p>
        </div>
      </div>

      <div className="p-3 bg-blue-500/10 rounded-lg border border-blue-500/20 text-blue-300 text-sm">
        <p className="font-medium">Jira Cloud Connector</p>
        <p className="mt-2 text-xs opacity-80">
          Connect to Jira Cloud for issue search, creation, commenting, and status transitions.
          Operations include: search, create, comment, transition, and project listing.
        </p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Enter your Jira Cloud site URL</li>
          <li>Enter your Atlassian email and API token</li>
          <li>MEHO will verify connectivity and list accessible projects</li>
        </ol>
      </div>
    </motion.div>
  );
}
