// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { AnimatePresence, motion } from 'motion/react';
import { Upload, ChevronDown, ChevronRight, CheckCircle, AlertCircle, Key, Lock, Shield, ShieldAlert, ShieldCheck } from 'lucide-react';
import clsx from 'clsx';
import { parseKubeconfig, getKubeconfigContexts, getCurrentContext } from '../../../lib/kubeconfig';
import { HTTP_METHODS } from './types';
import type { ConnectorFormBaseProps, RestFormState } from './types';

export interface RestFormProps extends ConnectorFormBaseProps {
  state: RestFormState;
  onChange: (patch: Partial<RestFormState>) => void;
  onApplyKubeconfig: (fields: { name: string; baseUrl: string; authType: 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION' }) => void;
}

export function validateRestForm(state: RestFormState): string | null {
  if (!state.baseUrl.trim()) return 'Base URL is required';
  if (state.allowedMethods.length === 0) return 'At least one HTTP method must be allowed';
  return null;
}

export function RestForm({ state, onChange, submitting, onApplyKubeconfig }: RestFormProps) {
  const parseAndApply = (text: string, context: string) => {
    const result = parseKubeconfig(text, context);
    if (!result.success || !result.info) {
      onChange({ kubeconfigError: result.error || 'Failed to parse kubeconfig', kubeconfigInfo: null });
      return;
    }
    const info = result.info;
    onChange({ kubeconfigInfo: info, kubeconfigError: null });

    let authType: RestFormState['authType'] = 'OAUTH2';
    let pendingCredentials: RestFormState['pendingCredentials'] = null;
    if (info.authType === 'token' && info.token) {
      authType = 'OAUTH2';
      pendingCredentials = { access_token: info.token };
    } else if (info.authType === 'basic' && info.username && info.password) {
      authType = 'BASIC';
      pendingCredentials = { username: info.username, password: info.password };
    }
    onChange({ authType, pendingCredentials });
    onApplyKubeconfig({ name: info.name, baseUrl: info.server, authType });
  };

  const handleKubeconfigChange = (text: string) => {
    onChange({ kubeconfigText: text, kubeconfigError: null, kubeconfigInfo: null, pendingCredentials: null });
    if (!text.trim()) {
      onChange({ kubeconfigContexts: [], selectedKubeContext: '' });
      return;
    }
    const contexts = getKubeconfigContexts(text);
    onChange({ kubeconfigContexts: contexts });
    const currentCtx = getCurrentContext(text);
    if (currentCtx && contexts.includes(currentCtx)) {
      onChange({ selectedKubeContext: currentCtx });
      parseAndApply(text, currentCtx);
    } else if (contexts.length > 0) {
      onChange({ selectedKubeContext: contexts[0] });
      parseAndApply(text, contexts[0]);
    }
  };

  const handleKubeContextChange = (context: string) => {
    onChange({ selectedKubeContext: context });
    if (state.kubeconfigText && context) {
      parseAndApply(state.kubeconfigText, context);
    }
  };

  const handleMethodToggle = (method: string) => {
    if (state.allowedMethods.includes(method)) {
      onChange({ allowedMethods: state.allowedMethods.filter(m => m !== method) });
    } else {
      onChange({ allowedMethods: [...state.allowedMethods, method] });
    }
  };

  const handleAddHeader = () => {
    onChange({ customLoginHeaders: [...state.customLoginHeaders, { key: '', value: '' }] });
  };

  const handleRemoveHeader = (index: number) => {
    onChange({ customLoginHeaders: state.customLoginHeaders.filter((_, i) => i !== index) });
  };

  const handleHeaderChange = (index: number, field: 'key' | 'value', value: string) => {
    const updated = [...state.customLoginHeaders];
    updated[index] = { ...updated[index], [field]: value };
    onChange({ customLoginHeaders: updated });
  };

  return (
    <>
      {/* Base URL, OpenAPI URL, Kubeconfig Import */}
      <div className="grid grid-cols-1 gap-6">
        <div>
          <label htmlFor="create-connector-base-url" className="block text-sm font-medium text-text-secondary mb-2">
            Base URL *
          </label>
          <input
            id="create-connector-base-url"
            type="url"
            value={state.baseUrl}
            onChange={(e) => onChange({ baseUrl: e.target.value })}
            placeholder="https://api.github.com"
            disabled={submitting}
            data-testid="connector-base-url-input"
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
          />
        </div>

        <div>
          <label htmlFor="create-connector-openapi-url" className="block text-sm font-medium text-text-secondary mb-2">
            OpenAPI Spec URL <span className="text-text-tertiary">(optional)</span>
          </label>
          <input
            id="create-connector-openapi-url"
            type="url"
            value={state.openapiUrl}
            onChange={(e) => onChange({ openapiUrl: e.target.value })}
            placeholder="https://api.example.com/openapi.json"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">
            If provided, the OpenAPI spec will be fetched and ingested automatically
          </p>
        </div>

        {/* Kubeconfig Import */}
        <div className="mt-2">
          <button
            type="button"
            onClick={() => onChange({ showKubeconfigImport: !state.showKubeconfigImport })}
            className="flex items-center gap-2 text-sm text-primary hover:text-primary/80 transition-colors"
          >
            {state.showKubeconfigImport ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
            <Upload className="h-4 w-4" />
            Import from Kubeconfig
          </button>

          <AnimatePresence>
            {state.showKubeconfigImport && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mt-4 p-4 bg-blue-500/5 rounded-xl border border-blue-500/20 space-y-4"
              >
                <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
                  <Upload className="h-4 w-4" />
                  Kubernetes Cluster Import
                </div>

                <div>
                  <label htmlFor="create-kubeconfig-contents" className="block text-sm font-medium text-text-secondary mb-2">
                    Paste kubeconfig contents
                  </label>
                  <textarea
                    id="create-kubeconfig-contents"
                    value={state.kubeconfigText}
                    onChange={(e) => handleKubeconfigChange(e.target.value)}
                    placeholder={`apiVersion: v1
kind: Config
clusters:
- name: my-cluster
  cluster:
    server: https://...`}
                    rows={6}
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm resize-none"
                  />
                </div>

                {/* Context selector */}
                {state.kubeconfigContexts.length > 1 && (
                  <div>
                    <label htmlFor="create-kube-context" className="block text-sm font-medium text-text-secondary mb-2">
                      Select Context
                    </label>
                    <select
                      id="create-kube-context"
                      value={state.selectedKubeContext}
                      onChange={(e) => handleKubeContextChange(e.target.value)}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all appearance-none"
                    >
                      {state.kubeconfigContexts.map((ctx) => (
                        <option key={ctx} value={ctx}>{ctx}</option>
                      ))}
                    </select>
                  </div>
                )}

                {/* Parse result */}
                {state.kubeconfigInfo && (
                  <div className="p-3 bg-green-500/10 rounded-lg border border-green-500/20 text-green-300 text-sm space-y-2">
                    <p className="font-medium flex items-center gap-2">
                      <CheckCircle className="h-4 w-4" />
                      Kubeconfig parsed successfully
                    </p>
                    <div className="text-xs space-y-1 opacity-80">
                      <p><span className="text-text-tertiary">Server:</span> {state.kubeconfigInfo.server}</p>
                      <p><span className="text-text-tertiary">Auth Type:</span> {state.kubeconfigInfo.authType}</p>
                      {state.kubeconfigInfo.authWarning && (
                        <p className="text-amber-300 mt-2">{state.kubeconfigInfo.authWarning}</p>
                      )}
                    </div>
                  </div>
                )}

                {/* Parse error */}
                {state.kubeconfigError && (
                  <div className="p-3 bg-red-500/10 rounded-lg border border-red-500/20 text-red-300 text-sm flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                    <span>{state.kubeconfigError}</span>
                  </div>
                )}

                <p className="text-xs text-text-tertiary">
                  Your kubeconfig will be parsed locally. Only the server URL and token are extracted and sent to the server.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Authentication */}
      <div className="space-y-6">
        <div className="flex items-center gap-2 text-white font-medium">
          <Key className="h-4 w-4 text-primary" />
          <h3>Authentication</h3>
        </div>

        <div className="space-y-6">
          <div>
            <span className="block text-sm font-medium text-text-secondary mb-2">
              Auth Type
            </span>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {[
                { id: 'API_KEY', label: 'API Key (Bearer)' },
                { id: 'BASIC', label: 'Basic Auth' },
                { id: 'OAUTH2', label: 'OAuth 2.0' },
                { id: 'SESSION', label: 'Session Based' },
                { id: 'NONE', label: 'No Auth' }
              ].map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => onChange({ authType: option.id as RestFormState['authType'] })}
                  className={clsx(
                    "px-4 py-3 rounded-xl text-sm font-medium text-left transition-all border",
                    state.authType === option.id
                      ? "bg-primary/10 border-primary/50 text-white"
                      : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          {/* SESSION auth configuration */}
          {state.authType === 'SESSION' && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              className="space-y-6 p-6 bg-white/5 rounded-xl border border-white/10"
            >
              <div className="flex items-center gap-2 text-primary text-sm font-medium">
                <Lock className="h-4 w-4" />
                Session Configuration
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="col-span-2">
                  <label htmlFor="create-session-login-url" className="block text-sm font-medium text-text-secondary mb-2">
                    Login URL *
                  </label>
                  <input
                    id="create-session-login-url"
                    type="text"
                    value={state.loginUrl}
                    onChange={(e) => onChange({ loginUrl: e.target.value })}
                    placeholder="/api/v1/auth/login"
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  />
                  <p className="text-xs text-text-tertiary mt-1">Relative to base URL</p>
                </div>

                <div>
                  <label htmlFor="create-session-login-method" className="block text-sm font-medium text-text-secondary mb-2">
                    Login Method
                  </label>
                  <select
                    id="create-session-login-method"
                    value={state.loginMethod}
                    onChange={(e) => onChange({ loginMethod: e.target.value as 'POST' | 'GET' })}
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                  >
                    <option value="POST">POST</option>
                    <option value="GET">GET</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="create-session-login-auth-type" className="block text-sm font-medium text-text-secondary mb-2">
                    Login Auth Type
                  </label>
                  <select
                    id="create-session-login-auth-type"
                    value={state.loginAuthType}
                    onChange={(e) => onChange({ loginAuthType: e.target.value as 'body' | 'basic' })}
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                  >
                    <option value="body">JSON Body (username/password)</option>
                    <option value="basic">HTTP Basic Auth</option>
                  </select>
                  <p className="text-xs text-text-tertiary mt-1">
                    {state.loginAuthType === 'basic' ? 'Credentials in Authorization header' : 'Credentials in JSON body'}
                  </p>
                </div>

                <div>
                  <label htmlFor="create-session-token-location" className="block text-sm font-medium text-text-secondary mb-2">
                    Token Location
                  </label>
                  <select
                    id="create-session-token-location"
                    value={state.tokenLocation}
                    onChange={(e) => onChange({ tokenLocation: e.target.value as 'header' | 'cookie' | 'body' })}
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                  >
                    <option value="header">Response Header</option>
                    <option value="cookie">Cookie</option>
                    <option value="body">Response Body (JSON)</option>
                  </select>
                </div>

                <div>
                  <label htmlFor="create-session-token-name" className="block text-sm font-medium text-text-secondary mb-2">
                    Token Name *
                  </label>
                  <input
                    id="create-session-token-name"
                    type="text"
                    value={state.tokenName}
                    onChange={(e) => onChange({ tokenName: e.target.value })}
                    placeholder="X-Auth-Token"
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  />
                  <p className="text-xs text-text-tertiary mt-1">Header/cookie name or JSON key in login response</p>
                </div>

                {state.tokenLocation === 'body' && (
                  <div>
                    <label htmlFor="create-session-token-path" className="block text-sm font-medium text-text-secondary mb-2">
                      Token Path
                    </label>
                    <input
                      id="create-session-token-path"
                      type="text"
                      value={state.tokenPath}
                      onChange={(e) => onChange({ tokenPath: e.target.value })}
                      placeholder="$.token"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">JSONPath for nested tokens (e.g., $.data.token)</p>
                  </div>
                )}

                <div>
                  <label htmlFor="create-session-header-name" className="block text-sm font-medium text-text-secondary mb-2">
                    Header Name (for API requests)
                  </label>
                  <input
                    id="create-session-header-name"
                    type="text"
                    value={state.headerName}
                    onChange={(e) => onChange({ headerName: e.target.value })}
                    placeholder="vmware-api-session-id"
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  />
                  <p className="text-xs text-text-tertiary mt-1">Optional: Custom header name for sending token (default: Authorization Bearer)</p>
                </div>

                <div>
                  <label htmlFor="create-session-duration" className="block text-sm font-medium text-text-secondary mb-2">
                    Session Duration (s)
                  </label>
                  <input
                    id="create-session-duration"
                    type="number"
                    value={state.sessionDuration}
                    onChange={(e) => onChange({ sessionDuration: parseInt(e.target.value) || 3600 })}
                    min="60"
                    max="86400"
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  />
                </div>

                {/* Custom Login Headers */}
                <div className="col-span-2 space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="block text-sm font-medium text-text-secondary">
                      Custom Login Headers
                    </span>
                    <button
                      type="button"
                      onClick={handleAddHeader}
                      disabled={submitting}
                      className="px-3 py-1 text-xs bg-primary/10 hover:bg-primary/20 text-primary rounded-lg transition-colors"
                    >
                      + Add Header
                    </button>
                  </div>
                  <p className="text-xs text-text-tertiary">
                    Optional headers to send with login request (e.g., vmware-use-header-authn: test)
                  </p>
                  {state.customLoginHeaders.length > 0 && (
                    <div className="space-y-2">
                      {state.customLoginHeaders.map((header, index) => (
                        <div key={index} className="flex gap-2">
                          <input
                            type="text"
                            value={header.key}
                            onChange={(e) => handleHeaderChange(index, 'key', e.target.value)}
                            placeholder="Header name"
                            disabled={submitting}
                            className="flex-1 px-3 py-2 bg-surface border border-white/10 rounded-lg text-white text-sm placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
                          />
                          <input
                            type="text"
                            value={header.value}
                            onChange={(e) => handleHeaderChange(index, 'value', e.target.value)}
                            placeholder="Header value"
                            disabled={submitting}
                            className="flex-1 px-3 py-2 bg-surface border border-white/10 rounded-lg text-white text-sm placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
                          />
                          <button
                            type="button"
                            onClick={() => handleRemoveHeader(index)}
                            disabled={submitting}
                            className="px-3 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg transition-colors text-sm"
                          >
                            Remove
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          )}
        </div>
      </div>

      {/* Safety Policies */}
      <div className="space-y-6">
        <div className="flex items-center gap-2 text-white font-medium">
          <Shield className="h-4 w-4 text-primary" />
          <h3>Safety Policies</h3>
        </div>

        <div className="space-y-6">
          {/* Allowed Methods */}
          <div>
            <span className="block text-sm font-medium text-text-secondary mb-3">
              Allowed HTTP Methods
            </span>
            <div className="flex flex-wrap gap-3">
              {HTTP_METHODS.map((method) => (
                <label key={method} className={clsx(
                  "flex items-center gap-2 px-4 py-2 rounded-xl border cursor-pointer transition-all select-none",
                  state.allowedMethods.includes(method)
                    ? "bg-primary/10 border-primary/50 text-white"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}>
                  <input
                    type="checkbox"
                    checked={state.allowedMethods.includes(method)}
                    onChange={() => handleMethodToggle(method)}
                    disabled={submitting}
                    className="hidden"
                  />
                  <span className="text-sm font-medium">{method}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Default Safety Level */}
          <div>
            <span className="block text-sm font-medium text-text-secondary mb-3">
              Default Safety Level
            </span>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <button
                type="button"
                onClick={() => onChange({ defaultSafetyLevel: 'safe' })}
                className={clsx(
                  "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                  state.defaultSafetyLevel === 'safe'
                    ? "bg-green-400/10 border-green-400/50 text-green-400"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}
              >
                <ShieldCheck className="h-6 w-6" />
                <span className="text-sm font-medium">Safe</span>
              </button>

              <button
                type="button"
                onClick={() => onChange({ defaultSafetyLevel: 'caution' })}
                className={clsx(
                  "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                  state.defaultSafetyLevel === 'caution'
                    ? "bg-amber-500/10 border-amber-500/50 text-amber-400"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}
              >
                <Shield className="h-6 w-6" />
                <span className="text-sm font-medium">Caution</span>
              </button>

              <button
                type="button"
                onClick={() => onChange({ defaultSafetyLevel: 'dangerous' })}
                className={clsx(
                  "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                  state.defaultSafetyLevel === 'dangerous'
                    ? "bg-red-500/10 border-red-500/50 text-red-400"
                    : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                )}
              >
                <ShieldAlert className="h-6 w-6" />
                <span className="text-sm font-medium">Dangerous</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
