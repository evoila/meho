// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connector Settings Component (Task 29)
 * 
 * Allows editing connector configuration:
 * - Base URL (KEY FEATURE - fix wrong URLs like VCF)
 * - Description
 * - Safety settings
 * - Related connectors (for cross-connector topology correlation)
 */

import { useState, useEffect } from 'react';
import type { Connector, UpdateConnectorRequest } from '../../lib/api-client';
import { CheckCircle, XCircle, Save, AlertTriangle, Link2, Plus, X, Lock, Info } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { AutomationToggle } from './AutomationToggle';
import { useAuth } from '../../contexts/AuthContext';

interface ConnectorSettingsProps {
  connector: Connector;
  onUpdate: (updates: UpdateConnectorRequest) => Promise<void>;
  // All connectors for selecting related ones
  allConnectors?: Connector[];
}

export default function ConnectorSettings({ connector, onUpdate, allConnectors = [] }: Readonly<ConnectorSettingsProps>) {
  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState({
    name: connector.name,
    base_url: connector.base_url,
    description: connector.description || '',
    routing_description: connector.routing_description || '',
    // TASK-75: Allow changing auth type (protocol is immutable after creation)
    auth_type: connector.auth_type,
    default_safety_level: connector.default_safety_level,
    is_active: connector.is_active,
    // SESSION auth fields
    login_url: connector.login_url || '',
    login_method: connector.login_method || 'POST',
    login_config: connector.login_config || {},
    // Related connectors for cross-connector topology correlation
    related_connector_ids: connector.related_connector_ids || [],
  });
  
  // Phase 75: Admin detection for automation toggle
  const { user } = useAuth();
  const isAdmin = user?.roles?.includes('admin') || user?.roles?.includes('global_admin') || user?.isGlobalAdmin;

  // Available connectors for selection (exclude self)
  const availableConnectors = allConnectors.filter(c => c.id !== connector.id);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  // Sync formData when connector prop changes (e.g., after save)
  useEffect(() => {
    setFormData({
      name: connector.name,
      base_url: connector.base_url,
      description: connector.description || '',
      routing_description: connector.routing_description || '',
      auth_type: connector.auth_type,
      default_safety_level: connector.default_safety_level,
      is_active: connector.is_active,
      login_url: connector.login_url || '',
      login_method: connector.login_method || 'POST',
      login_config: connector.login_config || {},
      related_connector_ids: connector.related_connector_ids || [],
    });
  }, [connector]);

  // Use formData auth_type so SESSION fields show/hide dynamically when editing
  const isSessionAuth = formData.auth_type === 'SESSION';

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccess(false);

    try {
      await onUpdate(formData);
      setSuccess(true);
      setEditing(false);

      // Clear success message after 3 seconds
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to update connector');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    // Reset form to original values (protocol is immutable after creation)
    setFormData({
      name: connector.name,
      base_url: connector.base_url,
      description: connector.description || '',
      routing_description: connector.routing_description || '',
      auth_type: connector.auth_type,
      default_safety_level: connector.default_safety_level,
      is_active: connector.is_active,
      login_url: connector.login_url || '',
      login_method: connector.login_method || 'POST',
      login_config: connector.login_config || {},
      related_connector_ids: connector.related_connector_ids || [],
    });
    setEditing(false);
    setError(null);
  };

  const handleAddRelatedConnector = (connectorId: string) => {
    if (!formData.related_connector_ids.includes(connectorId)) {
      setFormData({
        ...formData,
        related_connector_ids: [...formData.related_connector_ids, connectorId],
      });
    }
  };

  const handleRemoveRelatedConnector = (connectorId: string) => {
    setFormData({
      ...formData,
      related_connector_ids: formData.related_connector_ids.filter(id => id !== connectorId),
    });
  };

  const handleLoginConfigChange = (field: string, value: string | number | boolean) => {
    setFormData({
      ...formData,
      login_config: {
        ...formData.login_config,
        [field]: value
      }
    });
  };

  // Check if credentials are masked (superadmin viewing tenant)
  const isCredentialsMasked = connector.auth_config_masked || connector.login_config_masked || connector.protocol_config_masked;

  return (
    <div className="space-y-6">
      {/* Masked credentials warning banner (Phase 3 - TASK-140) */}
      {isCredentialsMasked && (
        <div className="p-4 bg-amber-500/10 border border-amber-500/20 rounded-xl flex items-start gap-3">
          <Lock className="h-5 w-5 text-amber-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm font-medium text-amber-200">
              Credentials Hidden for Security
            </p>
            <p className="text-xs text-amber-200/70 mt-1">
              You are viewing this connector as a superadmin. Credential values are hidden to protect tenant security. 
              You can view configuration but cannot see or modify authentication credentials.
            </p>
          </div>
          <div className="group relative">
            <Info className="h-4 w-4 text-amber-400/50 hover:text-amber-400 cursor-help" />
            <div className="absolute right-0 top-6 w-64 p-3 bg-surface-elevated border border-white/10 rounded-lg shadow-xl opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50">
              <p className="text-xs text-text-secondary">
                This is a security policy. Superadmins can manage tenants but cannot access their credentials 
                to prevent unauthorized use of tenant systems.
              </p>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-bold text-white">
            Connector Configuration
          </h3>
          <p className="text-text-secondary text-sm">
            Manage basic settings and authentication details
          </p>
        </div>
        {!editing && !isCredentialsMasked && (
          <button
            onClick={() => setEditing(true)}
            className="px-4 py-2 text-sm font-medium text-white bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl transition-all"
          >
            Edit Settings
          </button>
        )}
      </div>

      <AnimatePresence>
        {success && (
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className="p-4 bg-green-500/10 border border-green-500/20 rounded-xl flex items-center gap-3"
          >
            <CheckCircle className="h-5 w-5 text-green-400" />
            <span className="text-sm text-green-200">
              Settings updated successfully!
            </span>
          </motion.div>
        )}

        {error && (
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl flex items-center gap-3"
          >
            <XCircle className="h-5 w-5 text-red-400" />
            <span className="text-sm text-red-200">{error}</span>
          </motion.div>
        )}
      </AnimatePresence>

      <form onSubmit={handleSubmit} className="space-y-6">
        <div className="grid grid-cols-1 gap-6">
          {/* Name */}
          <div>
            <label htmlFor="connector-settings-name" className="block text-sm font-medium text-text-secondary mb-2">
              Name
            </label>
            <input
              id="connector-settings-name"
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              disabled={!editing}
              className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              required
            />
          </div>

          {/* Base URL */}
          <div>
            <label htmlFor="connector-settings-base-url" className="block text-sm font-medium text-text-secondary mb-2">
              Base URL
            </label>
            <div className="relative">
              <input
                id="connector-settings-base-url"
                type="url"
                value={formData.base_url}
                onChange={(e) => setFormData({ ...formData, base_url: e.target.value })}
                disabled={!editing}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                placeholder="https://api.example.com"
                required
              />
            </div>
            <p className="mt-2 text-xs text-text-tertiary">
              The base URL for API requests. Include protocol (https://) and any path prefix.
            </p>
          </div>

          {/* Description */}
          <div>
            <label htmlFor="connector-settings-description" className="block text-sm font-medium text-text-secondary mb-2">
              Description
            </label>
            <textarea
              id="connector-settings-description"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              disabled={!editing}
              rows={3}
              className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed resize-none"
              placeholder="Description of this connector..."
            />
          </div>

          {/* Routing Description - for orchestrator LLM routing */}
          <div>
            <label htmlFor="connector-settings-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
              Routing Description
            </label>
            <textarea
              id="connector-settings-routing-desc"
              value={formData.routing_description}
              onChange={(e) => setFormData({ ...formData, routing_description: e.target.value })}
              disabled={!editing}
              rows={3}
              className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed resize-none"
              placeholder="e.g., 'Production Kubernetes cluster hosting api.example.com and web services'"
            />
            <p className="mt-2 text-xs text-text-tertiary">
              Describe what this connector manages. Used by the AI orchestrator to decide which connectors to query for a given question.
            </p>
          </div>
        </div>

        {/* Separator */}
        <div className="border-t border-white/10 pt-6">
          {/* Auth Type Selection */}
          <div className="mb-6">
            <span className="block text-sm font-medium text-text-secondary mb-3">
              Authentication Type
            </span>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
              {/* Show different auth types based on connector_type */}
              {(connector.connector_type === 'soap' || connector.connector_type === 'vmware'
                ? [
                    { id: 'SESSION', label: 'Session Based', desc: 'Recommended for VMware' },
                    { id: 'BASIC', label: 'Basic Auth' },
                    { id: 'NONE', label: 'No Auth' }
                  ]
                : [
                    { id: 'API_KEY', label: 'API Key' },
                    { id: 'BASIC', label: 'Basic Auth' },
                    { id: 'OAUTH2', label: 'OAuth 2.0' },
                    { id: 'SESSION', label: 'Session Based' },
                    { id: 'NONE', label: 'No Auth' }
                  ]
              ).map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => editing && setFormData({ ...formData, auth_type: option.id as 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION' })}
                  disabled={!editing}
                  className={`px-3 py-2 rounded-lg text-xs font-medium transition-all border ${
                    formData.auth_type === option.id
                      ? "bg-primary/10 border-primary/50 text-white"
                      : "bg-white/5 border-white/10 text-text-secondary"
                  } ${editing ? "hover:bg-white/10 cursor-pointer" : "opacity-60 cursor-not-allowed"}`}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-xs text-text-tertiary">
              {connector.connector_type === 'soap' || connector.connector_type === 'vmware'
                ? "For VMware VIM API, use 'Session Based' authentication."
                : "Change authentication type to enable/configure credentials."}
            </p>
          </div>

          {/* SOAP/VMware Session Info */}
          {(connector.connector_type === 'soap' || connector.connector_type === 'vmware') && formData.auth_type === 'SESSION' && (
            <div className="p-4 bg-amber-500/10 border border-amber-500/20 rounded-xl">
              <p className="text-sm text-amber-300 font-medium mb-2">
                🔐 {connector.connector_type === 'vmware' ? 'VMware' : 'SOAP'} Session Authentication
              </p>
              <p className="text-xs text-amber-200/70">
                Session handling is managed internally by the connector client.
                Just add your username and password in the <strong>Credentials</strong> tab.
                {connector.connector_type === 'soap' && ' The client will automatically call the VMware Login/Logout operations.'}
              </p>
            </div>
          )}
        </div>

        {/* SESSION Auth Configuration - Only for REST APIs (SOAP/VMware handle sessions internally) */}
        {isSessionAuth && connector.connector_type !== 'soap' && connector.connector_type !== 'vmware' && (
          <div className="border-t border-white/10 pt-6 mt-6">
            <h4 className="text-md font-bold text-white mb-6 flex items-center gap-2">
              <span className="w-1 h-6 bg-primary rounded-full"></span>
              Session Authentication
            </h4>

            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* Login Endpoint */}
                <div>
                  <label htmlFor="connector-login-endpoint" className="block text-sm font-medium text-text-secondary mb-2">
                    Login Endpoint
                  </label>
                  <input
                    id="connector-login-endpoint"
                    type="text"
                    value={formData.login_url}
                    onChange={(e) => setFormData({ ...formData, login_url: e.target.value })}
                    disabled={!editing}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                    placeholder="/v1/tokens"
                    required={isSessionAuth}
                  />
                </div>

                {/* Login Method */}
                <div>
                  <label htmlFor="connector-login-method" className="block text-sm font-medium text-text-secondary mb-2">
                    Login Method
                  </label>
                  <select
                    id="connector-login-method"
                    value={formData.login_method}
                    onChange={(e) => setFormData({ ...formData, login_method: e.target.value as 'POST' | 'GET' })}
                    disabled={!editing}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed appearance-none"
                  >
                    <option value="POST" className="bg-surface text-white">POST</option>
                    <option value="GET" className="bg-surface text-white">GET</option>
                  </select>
                </div>

                {/* Login Auth Type */}
                <div className="col-span-2">
                  <label htmlFor="connector-login-auth-type" className="block text-sm font-medium text-text-secondary mb-2">
                    Login Auth Type
                  </label>
                  <select
                    id="connector-login-auth-type"
                    value={formData.login_config.login_auth_type || 'body'}
                    onChange={(e) => handleLoginConfigChange('login_auth_type', e.target.value)}
                    disabled={!editing}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed appearance-none"
                  >
                    <option value="body" className="bg-surface text-white">JSON Body (username/password)</option>
                    <option value="basic" className="bg-surface text-white">HTTP Basic Auth</option>
                  </select>
                  <p className="mt-2 text-xs text-text-tertiary">
                    {formData.login_config.login_auth_type === 'basic'
                      ? 'Credentials sent in Authorization header (e.g., vCenter)'
                      : 'Credentials sent in JSON request body (default)'}
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* Token Location */}
                <div>
                  <label htmlFor="connector-token-location" className="block text-sm font-medium text-text-secondary mb-2">
                    Token Location
                  </label>
                  <select
                    id="connector-token-location"
                    value={formData.login_config.token_location || 'body'}
                    onChange={(e) => handleLoginConfigChange('token_location', e.target.value)}
                    disabled={!editing}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed appearance-none"
                  >
                    <option value="body" className="bg-surface text-white">Response Body (JSON)</option>
                    <option value="header" className="bg-surface text-white">Response Header</option>
                    <option value="cookie" className="bg-surface text-white">Cookie</option>
                  </select>
                  <p className="mt-2 text-xs text-text-tertiary">
                    Where the session token is located in the login response
                  </p>
                </div>

                {/* Token Path/Name */}
                {formData.login_config.token_location === 'body' ? (
                  <div>
                    <label htmlFor="connector-token-path" className="block text-sm font-medium text-text-secondary mb-2">
                      Access Token Path (JSONPath)
                    </label>
                    <input
                      id="connector-token-path"
                      type="text"
                      value={formData.login_config.token_path || ''}
                      onChange={(e) => handleLoginConfigChange('token_path', e.target.value)}
                      disabled={!editing}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                      placeholder="$.accessToken"
                    />
                    <p className="mt-2 text-xs text-text-tertiary">
                      JSONPath to extract token from response body (e.g., $.value)
                    </p>
                  </div>
                ) : (
                  <div>
                    <label htmlFor="connector-token-name" className="block text-sm font-medium text-text-secondary mb-2">
                      Token Name
                    </label>
                    <input
                      id="connector-token-name"
                      type="text"
                      value={formData.login_config.token_name || ''}
                      onChange={(e) => handleLoginConfigChange('token_name', e.target.value)}
                      disabled={!editing}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                      placeholder={formData.login_config.token_location === 'header' ? 'X-Auth-Token' : 'sessionId'}
                    />
                    <p className="mt-2 text-xs text-text-tertiary">
                      Header or cookie name containing the token
                    </p>
                  </div>
                )}
              </div>

              {/* Custom Login Headers */}
              <div className="col-span-1">
                <span className="block text-sm font-medium text-text-secondary mb-2">
                  Custom Login Headers (Optional)
                </span>
                <div className="bg-white/5 border border-white/10 rounded-xl p-4">
                  <p className="text-xs text-text-tertiary mb-3">
                    Add custom HTTP headers to send with login request (e.g., vmware-use-header-authn: test)
                  </p>
                  {formData.login_config.login_headers && Object.keys(formData.login_config.login_headers).length > 0 ? (
                    <div className="space-y-2">
                      {Object.entries(formData.login_config.login_headers).map(([key, value]) => (
                        <div key={key} className="flex items-center gap-2 text-sm">
                          <span className="font-mono text-white">{key}:</span>
                          <span className="font-mono text-text-secondary">{value as string}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-text-tertiary italic">No custom headers configured</p>
                  )}
                  {editing && (
                    <p className="text-xs text-amber-400 mt-3">
                      Note: Custom headers cannot be edited here. Please recreate the connector to change headers.
                    </p>
                  )}
                </div>
              </div>

              {/* Header Name for API Requests */}
              <div className="col-span-1">
                <label htmlFor="connector-api-header-name" className="block text-sm font-medium text-text-secondary mb-2">
                  Header Name for API Requests (Optional)
                </label>
                <input
                  id="connector-api-header-name"
                  type="text"
                  value={formData.login_config.header_name || ''}
                  onChange={(e) => handleLoginConfigChange('header_name', e.target.value)}
                  disabled={!editing}
                  className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                  placeholder="vmware-api-session-id"
                />
                <p className="mt-2 text-xs text-text-tertiary">
                  Custom header name for sending the session token in API requests. Leave empty to use standard Authorization Bearer header.
                </p>
              </div>

              {/* Refresh Configuration */}
              <div className="bg-white/5 border border-white/10 rounded-xl p-6 space-y-6">
                <h5 className="text-sm font-bold text-white flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-accent" />
                  Token Refresh (Optional)
                </h5>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label htmlFor="connector-refresh-endpoint" className="block text-sm font-medium text-text-secondary mb-2">
                      Refresh Endpoint
                    </label>
                    <input
                      id="connector-refresh-endpoint"
                      type="text"
                      value={formData.login_config.refresh_url || ''}
                      onChange={(e) => handleLoginConfigChange('refresh_url', e.target.value)}
                      disabled={!editing}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed font-mono text-sm"
                      placeholder="/v1/tokens/refresh"
                    />
                  </div>

                  <div>
                    <label htmlFor="connector-refresh-method" className="block text-sm font-medium text-text-secondary mb-2">
                      Refresh Method
                    </label>
                    <select
                      id="connector-refresh-method"
                      value={formData.login_config.refresh_method || 'POST'}
                      onChange={(e) => handleLoginConfigChange('refresh_method', e.target.value)}
                      disabled={!editing}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed appearance-none"
                    >
                      <option value="POST" className="bg-surface text-white">POST</option>
                      <option value="PATCH" className="bg-surface text-white">PATCH</option>
                      <option value="PUT" className="bg-surface text-white">PUT</option>
                    </select>
                  </div>

                  <div>
                    <label htmlFor="connector-session-duration" className="block text-sm font-medium text-text-secondary mb-2">
                      Session Duration (seconds)
                    </label>
                    <input
                      id="connector-session-duration"
                      type="number"
                      value={formData.login_config.session_duration_seconds || 3600}
                      onChange={(e) => handleLoginConfigChange('session_duration_seconds', parseInt(e.target.value))}
                      disabled={!editing}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                      min="60"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="border-t border-white/10 pt-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Default Safety Level */}
            <div>
              <label htmlFor="connector-default-safety-level" className="block text-sm font-medium text-text-secondary mb-2">
                Default Safety Level
              </label>
              <select
                id="connector-default-safety-level"
                value={formData.default_safety_level}
                onChange={(e) => setFormData({ ...formData, default_safety_level: e.target.value as 'safe' | 'caution' | 'dangerous' })}
                disabled={!editing}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all disabled:opacity-50 disabled:cursor-not-allowed appearance-none"
              >
                <option value="safe" className="bg-surface text-white">Safe - No approval needed</option>
                <option value="caution" className="bg-surface text-white">Caution - Approval for some operations</option>
                <option value="dangerous" className="bg-surface text-white">Dangerous - Always require approval</option>
              </select>
            </div>

            {/* Active Status */}
            <div className="flex items-center h-full pt-6">
              <label className="flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={formData.is_active}
                  onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                  disabled={!editing}
                  className="w-5 h-5 rounded border-white/20 bg-white/5 text-primary focus:ring-primary/50 transition-all disabled:opacity-50"
                />
                <span className="ml-3 text-sm font-medium text-white">
                  Active (connector is available for use)
                </span>
              </label>
            </div>
          </div>
        </div>

        {/* Related Connectors - for cross-connector topology correlation */}
        <div className="border-t border-white/10 pt-6">
          <div className="mb-4">
            <div className="flex items-center gap-2 mb-2">
              <Link2 className="h-4 w-4 text-primary" />
              <span className="text-sm font-medium text-white">
                Related Connectors
              </span>
            </div>
            <p className="text-xs text-text-tertiary">
              Link this connector to related infrastructure connectors for automatic topology correlation.
              E.g., link a Kubernetes connector to the GCP connector that hosts the cluster.
            </p>
          </div>

          {/* Currently linked connectors */}
          {formData.related_connector_ids.length > 0 && (
            <div className="space-y-2 mb-4">
              {formData.related_connector_ids.map((relatedId) => {
                const relatedConnector = allConnectors.find(c => c.id === relatedId);
                return (
                  <div
                    key={relatedId}
                    className="flex items-center justify-between px-4 py-3 bg-primary/5 border border-primary/20 rounded-xl"
                  >
                    <div className="flex items-center gap-3">
                      <Link2 className="h-4 w-4 text-primary" />
                      <div>
                        <span className="text-sm font-medium text-white">
                          {relatedConnector?.name || relatedId}
                        </span>
                        {relatedConnector && (
                          <span className="ml-2 text-xs text-text-tertiary">
                            ({relatedConnector.connector_type})
                          </span>
                        )}
                      </div>
                    </div>
                    {editing && (
                      <button
                        type="button"
                        onClick={() => handleRemoveRelatedConnector(relatedId)}
                        className="p-1.5 text-text-tertiary hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Add related connector */}
          {editing && availableConnectors.length > 0 && (
            <div className="flex gap-3">
              <select
                className="flex-1 px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                defaultValue=""
                onChange={(e) => {
                  if (e.target.value) {
                    handleAddRelatedConnector(e.target.value);
                    e.target.value = '';
                  }
                }}
              >
                <option value="" className="bg-surface text-text-tertiary">
                  Select a connector to link...
                </option>
                {availableConnectors
                  .filter(c => !formData.related_connector_ids.includes(c.id))
                  .map((c) => (
                    <option key={c.id} value={c.id} className="bg-surface text-white">
                      {c.name} ({c.connector_type})
                    </option>
                  ))}
              </select>
              <button
                type="button"
                onClick={() => {
                  const select = document.querySelector('select') as HTMLSelectElement;
                  if (select?.value) {
                    handleAddRelatedConnector(select.value);
                    select.value = '';
                  }
                }}
                className="px-4 py-3 bg-primary/10 hover:bg-primary/20 text-primary rounded-xl transition-colors"
              >
                <Plus className="h-5 w-5" />
              </button>
            </div>
          )}

          {!editing && formData.related_connector_ids.length === 0 && (
            <p className="text-sm text-text-tertiary italic">
              No related connectors configured. Edit settings to add connections.
            </p>
          )}

          {/* Info box about correlation */}
          <div className="mt-4 p-3 bg-blue-500/10 border border-blue-500/20 rounded-xl">
            <p className="text-xs text-blue-300">
              <strong>Why link connectors?</strong> When a K8s cluster runs on GCP, linking the K8s connector to the GCP connector 
              enables MEHO to automatically discover that K8s nodes and GCP VMs are the same physical resources, 
              allowing full-stack diagnosis across the topology.
            </p>
          </div>
        </div>

        {/* Automation Access -- admin only (Phase 75) */}
        {isAdmin && (
          <div className="border-t border-white/10 pt-6">
            <AutomationToggle
              connectorId={connector.id}
              automationEnabled={connector.automation_enabled ?? true}
              onUpdate={() => {
                // Invalidate connector query to refresh data
              }}
            />
          </div>
        )}

        {/* Action Buttons */}
        <AnimatePresence>
          {editing && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="flex gap-3 pt-4 border-t border-white/10"
            >
              <button
                type="submit"
                disabled={saving}
                className="flex items-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? (
                  <>
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                    Saving...
                  </>
                ) : (
                  <>
                    <Save className="h-4 w-4" />
                    Save Changes
                  </>
                )}
              </button>
              <button
                type="button"
                onClick={handleCancel}
                disabled={saving}
                className="px-6 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Cancel
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </form>
    </div>
  );
}
