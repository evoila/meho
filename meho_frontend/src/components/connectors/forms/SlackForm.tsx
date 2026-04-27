// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import type { ConnectorFormBaseProps, SlackFormState } from './types';

export interface SlackFormProps extends ConnectorFormBaseProps {
  state: SlackFormState;
  onChange: (patch: Partial<SlackFormState>) => void;
}

export function validateSlackForm(state: SlackFormState): string | null {
  if (!state.botToken.trim()) return 'Slack bot token is required';
  return null;
}

export function SlackForm({ state, onChange, submitting }: SlackFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="space-y-4"
    >
      <div className="p-4 bg-purple-500/5 border border-purple-500/20 rounded-xl">
        <h3 className="text-purple-400 font-medium text-sm mb-3">Slack Connection</h3>
        <div className="space-y-4">
          <div>
            <label htmlFor="create-slack-bot-token" className="block text-sm text-text-secondary mb-1.5">Bot Token <span className="text-red-400">*</span></label>
            <input
              id="create-slack-bot-token"
              type="password"
              value={state.botToken}
              onChange={(e) => onChange({ botToken: e.target.value })}
              placeholder="xoxb-xxxxxxxxxxxx"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
            />
          </div>
          <div>
            <label htmlFor="create-slack-app-token" className="block text-sm text-text-secondary mb-1.5">App Token</label>
            <input
              id="create-slack-app-token"
              type="password"
              value={state.appToken}
              onChange={(e) => onChange({ appToken: e.target.value })}
              placeholder="xapp-xxxxxxxxxxxx"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
            />
            <p className="text-xs text-text-tertiary mt-1">Required for Socket Mode (default)</p>
          </div>
          <div>
            <label htmlFor="create-slack-user-token" className="block text-sm text-text-secondary mb-1.5">User Token</label>
            <input
              id="create-slack-user-token"
              type="password"
              value={state.userToken}
              onChange={(e) => onChange({ userToken: e.target.value })}
              placeholder="xoxp-xxxxxxxxxxxx"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
            />
            <p className="text-xs text-text-tertiary mt-1">Optional -- enables search.messages</p>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
