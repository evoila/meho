// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import type { ConnectorFormBaseProps, GithubFormState } from './types';

export interface GithubFormProps extends ConnectorFormBaseProps {
  state: GithubFormState;
  onChange: (patch: Partial<GithubFormState>) => void;
}

export function validateGithubForm(state: GithubFormState): string | null {
  if (!state.organization.trim()) return 'GitHub organization is required';
  if (!state.pat.trim()) return 'Personal access token is required';
  return null;
}

export function GithubForm({ state, onChange, submitting }: GithubFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="space-y-4"
    >
      <div className="p-4 bg-violet-500/5 border border-violet-500/20 rounded-xl">
        <h3 className="text-violet-400 font-medium text-sm mb-3">GitHub Connection</h3>
        <div className="space-y-4">
          <div>
            <label htmlFor="create-github-org" className="block text-sm text-text-secondary mb-1.5">Organization <span className="text-red-400">*</span></label>
            <input
              id="create-github-org"
              type="text"
              value={state.organization}
              onChange={(e) => onChange({ organization: e.target.value })}
              placeholder="my-org"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
            />
          </div>
          <div>
            <label htmlFor="create-github-pat" className="block text-sm text-text-secondary mb-1.5">Personal Access Token <span className="text-red-400">*</span></label>
            <input
              id="create-github-pat"
              type="password"
              value={state.pat}
              onChange={(e) => onChange({ pat: e.target.value })}
              placeholder="ghp_xxxxxxxxxxxx (Classic PAT with repo, read:org)"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
            />
          </div>
          <div>
            <label htmlFor="create-github-base-url" className="block text-sm text-text-secondary mb-1.5">API Base URL</label>
            <input
              id="create-github-base-url"
              type="url"
              value={state.baseUrl}
              onChange={(e) => onChange({ baseUrl: e.target.value })}
              placeholder="https://api.github.com"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
            />
            <p className="text-xs text-text-tertiary mt-1">Change for GitHub Enterprise. Default: https://api.github.com</p>
          </div>
          <div>
            <label htmlFor="create-github-routing-desc" className="block text-sm text-text-secondary mb-1.5">Routing Description</label>
            <input
              id="create-github-routing-desc"
              type="text"
              value={state.routingDescription}
              onChange={(e) => onChange({ routingDescription: e.target.value })}
              placeholder="GitHub repos, PRs, Actions for CI/CD pipeline tracing"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
            />
          </div>
        </div>
      </div>
    </motion.div>
  );
}
