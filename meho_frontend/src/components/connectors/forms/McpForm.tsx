// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import clsx from 'clsx';
import type { ConnectorFormBaseProps, McpFormState } from './types';

export interface McpFormProps extends ConnectorFormBaseProps {
  state: McpFormState;
  onChange: (patch: Partial<McpFormState>) => void;
}

export function validateMcpForm(state: McpFormState): string | null {
  if (state.transportType === 'streamable_http' && !state.serverUrl.trim()) {
    return 'MCP server URL is required';
  }
  if (state.transportType === 'stdio' && !state.command.trim()) {
    return 'MCP command is required for stdio transport';
  }
  return null;
}

export function McpForm({ state, onChange, submitting }: McpFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="space-y-4"
    >
      <div className="p-4 bg-cyan-500/5 border border-cyan-500/20 rounded-xl">
        <h3 className="text-cyan-400 font-medium text-sm mb-3">MCP Server Connection</h3>
        <div className="space-y-4">
          <div>
            {/* eslint-disable-next-line jsx-a11y/label-has-associated-control -- transport type uses button group, not input */}
            <label className="block text-sm text-text-secondary mb-1.5">Transport Type</label>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={() => onChange({ transportType: 'streamable_http' })}
                disabled={submitting}
                className={clsx(
                  'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium border transition-all',
                  state.transportType === 'streamable_http'
                    ? 'bg-cyan-500/10 border-cyan-500/40 text-cyan-400'
                    : 'bg-surface border-white/10 text-text-secondary hover:border-white/20'
                )}
              >
                Streamable HTTP
              </button>
              <button
                type="button"
                onClick={() => onChange({ transportType: 'stdio' })}
                disabled={submitting}
                className={clsx(
                  'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium border transition-all',
                  state.transportType === 'stdio'
                    ? 'bg-cyan-500/10 border-cyan-500/40 text-cyan-400'
                    : 'bg-surface border-white/10 text-text-secondary hover:border-white/20'
                )}
              >
                stdio
              </button>
            </div>
          </div>
          {state.transportType === 'streamable_http' && (
            <div>
              <label htmlFor="create-mcp-server-url" className="block text-sm text-text-secondary mb-1.5">Server URL <span className="text-red-400">*</span></label>
              <input
                id="create-mcp-server-url"
                type="url"
                value={state.serverUrl}
                onChange={(e) => onChange({ serverUrl: e.target.value })}
                placeholder="https://mcp-server.example.com/mcp"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
              />
            </div>
          )}
          {state.transportType === 'stdio' && (
            <div>
              <label htmlFor="create-mcp-command" className="block text-sm text-text-secondary mb-1.5">Command <span className="text-red-400">*</span></label>
              <input
                id="create-mcp-command"
                type="text"
                value={state.command}
                onChange={(e) => onChange({ command: e.target.value })}
                placeholder="npx -y @modelcontextprotocol/server-filesystem"
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
              />
              <p className="text-xs text-text-tertiary mt-1">The command to launch the MCP server as a subprocess</p>
            </div>
          )}
          <div>
            <label htmlFor="create-mcp-api-key" className="block text-sm text-text-secondary mb-1.5">API Key</label>
            <input
              id="create-mcp-api-key"
              type="password"
              value={state.apiKey}
              onChange={(e) => onChange({ apiKey: e.target.value })}
              placeholder="Optional: Bearer token for MCP server authentication"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
            />
          </div>
        </div>
      </div>
    </motion.div>
  );
}
