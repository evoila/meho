// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Credential Management Component (Task 29 + Phase 74)
 *
 * Manages user credentials for connectors:
 * - Check credential status
 * - Set credentials (BASIC: username/password, API_KEY: api_key)
 * - Delete credentials
 * - Test connection
 *
 * Phase 74: Service Credential admin section (admin-only)
 * - View service credential status
 * - Set/update service credential
 * - Remove service credential
 */

import { useState, useEffect, useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { Connector, CredentialStatus, MEHOAPIClient } from '../../lib/api-client';
import { useAuth } from '../../contexts/AuthContext';
import {
  CheckCircle,
  XCircle,
  Key,
  Trash2,
  Lock,
  ShieldCheck,
  Save,
  AlertTriangle,
  Info,
  EyeOff
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { toast } from 'sonner';
import { CredentialHealthBadge } from './CredentialHealthBadge';


// Service credential status from backend
interface ServiceCredentialStatus {
  has_service_credential: boolean;
  credential_type: string | null;
  updated_at: string | null;
}

interface CredentialManagementProps {
  connector: Connector;
  apiClient: MEHOAPIClient;
}

export default function CredentialManagement({ connector, apiClient }: CredentialManagementProps) { // NOSONAR (cognitive complexity)
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Form state
  const [credentials, setCredentials] = useState<Record<string, string>>({});

  // Service credential state (Phase 74)
  const isAdmin = user?.roles?.includes('admin') || user?.roles?.includes('global_admin') || user?.isGlobalAdmin;
  const [svcCredStatus, setSvcCredStatus] = useState<ServiceCredentialStatus | null>(null);
  const [svcCredLoading, setSvcCredLoading] = useState(false);
  const [svcCredSaving, setSvcCredSaving] = useState(false);
  const [svcCredDeleting, setSvcCredDeleting] = useState(false);
  const [svcCredFormOpen, setSvcCredFormOpen] = useState(false);
  const [svcCredentials, setSvcCredentials] = useState<Record<string, string>>({});
  const [svcCredType, setSvcCredType] = useState<string>('');

  const loadCredentialStatus = useCallback(async () => {
    try {
      setLoading(true);
      const credStatus = await apiClient.getCredentialStatus(connector.id);
      setStatus(credStatus);
    } catch (err: unknown) {
      console.error('Failed to load credential status:', err);
      setError(err instanceof Error ? err.message : 'Failed to load credential status');
    } finally {
      setLoading(false);
    }
  }, [connector.id, apiClient]);

  // Load service credential status (Phase 74)
  const loadServiceCredentialStatus = useCallback(async () => {
    if (!isAdmin) return;
    try {
      setSvcCredLoading(true);
      const response = await apiClient.client.get<ServiceCredentialStatus>(
        `/api/connectors/${connector.id}/service-credential`
      );
      setSvcCredStatus(response.data);
    } catch (err: unknown) {
      console.error('Failed to load service credential status:', err);
    } finally {
      setSvcCredLoading(false);
    }
  }, [connector.id, apiClient, isAdmin]);

  useEffect(() => {
    loadCredentialStatus();
    loadServiceCredentialStatus();
  }, [loadCredentialStatus, loadServiceCredentialStatus]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      await apiClient.setUserCredentials(connector.id, credentials);
      setSuccess('Credentials saved successfully!');

      // Clear form
      setCredentials({});

      // Reload status
      await loadCredentialStatus();

      // Invalidate credential status query
      queryClient.invalidateQueries({ queryKey: ['credentialStatus', connector.id] });

      // Clear success message after 3 seconds
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to save credentials');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm('Are you sure you want to delete your credentials? You will need to provide them again to use this connector.')) {
      return;
    }

    setDeleting(true);
    setError(null);
    setSuccess(null);

    try {
      await apiClient.deleteUserCredentials(connector.id);
      setSuccess('Credentials deleted successfully');

      // Reload status
      await loadCredentialStatus();

      // Invalidate credential status query
      queryClient.invalidateQueries({ queryKey: ['credentialStatus', connector.id] });

      // Clear success message after 3 seconds
      setTimeout(() => setSuccess(null), 3000);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to delete credentials');
    } finally {
      setDeleting(false);
    }
  };

  // Service credential handlers (Phase 74)
  const handleSvcCredSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSvcCredSaving(true);
    try {
      await apiClient.client.put(`/api/connectors/${connector.id}/service-credential`, {
        credential_type: svcCredType,
        credentials: svcCredentials,
      });
      toast.success('Service credential saved successfully');
      setSvcCredFormOpen(false);
      setSvcCredentials({});
      setSvcCredType('');
      await loadServiceCredentialStatus();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      toast.error(`Failed to save service credential: ${message}`);
    } finally {
      setSvcCredSaving(false);
    }
  };

  const handleSvcCredDelete = async () => {
    if (!window.confirm(
      `Remove the service credential for ${connector.name}? Automated sessions will fall back to creator delegation, which may fail if no delegation is configured.`
    )) {
      return;
    }
    setSvcCredDeleting(true);
    try {
      await apiClient.client.delete(`/api/connectors/${connector.id}/service-credential`);
      toast.success('Service credential removed');
      await loadServiceCredentialStatus();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      toast.error(`Failed to remove service credential: ${message}`);
    } finally {
      setSvcCredDeleting(false);
    }
  };

  const openSvcCredForm = () => {
    // Pre-populate credential type based on connector auth_type
    const connAuthType = connector.auth_type;
    if (connAuthType === 'BASIC' || connAuthType === 'SESSION') {
      setSvcCredType('PASSWORD');
    } else if (connAuthType === 'API_KEY') {
      setSvcCredType('API_KEY');
    } else {
      setSvcCredType('');
    }
    setSvcCredentials({});
    setSvcCredFormOpen(true);
  };

  // Determine auth requirements
  const requiresCredentials = connector.auth_type !== 'NONE';
  const credentialType = connector.auth_type;
  
  // Check if credentials are masked (superadmin viewing tenant) - Phase 3 TASK-140
  const isCredentialsMasked = connector.auth_config_masked || connector.login_config_masked || connector.protocol_config_masked;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    );
  }

  // Show masked credentials banner for superadmins viewing tenant data
  if (isCredentialsMasked) {
    return (
      <div className="space-y-6">
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-amber-500/10 flex items-center justify-center border border-amber-500/20">
            <EyeOff className="h-5 w-5 text-amber-400" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-white">
              Credentials
            </h3>
            <p className="text-sm text-text-secondary">
              Authentication type: <span className="font-mono text-primary">{credentialType}</span>
            </p>
          </div>
        </div>

        <div className="p-6 bg-amber-500/10 border border-amber-500/20 rounded-xl">
          <div className="flex items-start gap-4">
            <div className="w-12 h-12 rounded-full bg-amber-500/10 flex items-center justify-center border border-amber-500/20">
              <Lock className="h-6 w-6 text-amber-400" />
            </div>
            <div className="flex-1">
              <p className="font-bold text-amber-200">Credentials Hidden for Security</p>
              <p className="text-sm text-amber-200/70 mt-2">
                You are viewing this connector as a superadmin in tenant context. 
                For security, credential values are not visible to superadmins.
              </p>
              <div className="mt-4 p-3 bg-white/5 rounded-lg border border-white/10">
                <div className="flex items-center gap-2 text-xs text-text-tertiary">
                  <Info className="h-4 w-4" />
                  <span>
                    This policy ensures tenant credentials cannot be accessed by platform administrators. 
                    To view or modify credentials, the tenant admin must log in directly.
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Show credential status if available (without actual values) */}
        {status?.has_credentials && (
          <div className="p-4 bg-green-500/10 border border-green-500/20 rounded-xl flex items-center gap-3">
            <ShieldCheck className="h-5 w-5 text-green-400" />
            <div>
              <p className="text-sm font-medium text-green-200">Credentials Configured</p>
              <p className="text-xs text-green-200/70">
                This connector has credentials stored (type: {status.credential_type})
              </p>
            </div>
          </div>
        )}
      </div>
    );
  }

  if (!requiresCredentials) {
    return (
      <div className="p-6 bg-white/5 border border-white/10 rounded-xl flex items-center gap-4">
        <div className="w-12 h-12 rounded-full bg-green-500/10 flex items-center justify-center border border-green-500/20">
          <CheckCircle className="h-6 w-6 text-green-400" />
        </div>
        <div>
          <p className="font-bold text-white">No credentials needed</p>
          <p className="text-sm text-text-secondary">This connector does not require authentication.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20">
          <Key className="h-5 w-5 text-primary" />
        </div>
        <div>
          <h3 className="text-lg font-bold text-white">
            Credentials
          </h3>
          <p className="text-sm text-text-secondary">
            Authentication type: <span className="font-mono text-primary">{credentialType}</span>
          </p>
        </div>
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
            <span className="text-sm text-green-200">{success}</span>
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

      {/* Current Status */}
      {status?.has_credentials && (
        <div className="p-6 bg-primary/5 border border-primary/10 rounded-xl relative overflow-hidden">
          <div className="absolute top-0 right-0 w-32 h-32 bg-primary/10 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2" />

          <div className="relative z-10 flex items-center justify-between">
            <div className="flex-1">
              <div className="flex items-center gap-2 mb-2">
                <ShieldCheck className="h-5 w-5 text-primary" />
                <p className="text-base font-bold text-white">
                  Credentials Configured
                </p>
              </div>
              <p className="text-sm text-text-secondary mb-3">
                Your credentials are securely stored and encrypted.
              </p>
              <div className="flex items-center gap-4 text-xs text-text-tertiary">
                <span className="px-2 py-1 bg-white/5 rounded-lg border border-white/10">
                  Type: {status.credential_type}
                </span>
                {status.last_used_at && (
                  <span>Last used: {new Date(status.last_used_at).toLocaleString()}</span>
                )}
                <CredentialHealthBadge
                  health={status.credential_health}
                  healthMessage={status.credential_health_message ?? undefined}
                  updatedAt={status.last_used_at ?? undefined}
                />
              </div>
            </div>
            <button
              onClick={handleDelete}
              disabled={deleting}
              className="px-4 py-2 text-sm font-medium text-red-400 hover:bg-red-500/10 border border-transparent hover:border-red-500/20 rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              <Trash2 className="h-4 w-4" />
              {deleting ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </div>
      )}

      {/* SESSION Auth Configuration (Read-Only) */}
      {credentialType === 'SESSION' && connector.login_url && (
        <div className="p-6 bg-white/5 border border-white/10 rounded-xl">
          <h4 className="text-sm font-bold text-white mb-4 flex items-center gap-2">
            <div className="w-1 h-4 bg-accent rounded-full" />
            Session Configuration
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
            <div className="flex justify-between p-3 bg-white/5 rounded-lg">
              <span className="text-text-secondary">Login Endpoint</span>
              <span className="font-mono text-white">{connector.login_url}</span>
            </div>
            <div className="flex justify-between p-3 bg-white/5 rounded-lg">
              <span className="text-text-secondary">Method</span>
              <span className="font-mono text-white">{connector.login_method || 'POST'}</span>
            </div>
            {connector.login_config?.login_auth_type && (
              <div className="flex justify-between p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary">Login Auth Type</span>
                <span className="font-mono text-white">
                  {connector.login_config.login_auth_type === 'basic' ? 'HTTP Basic Auth' : 'JSON Body'}
                </span>
              </div>
            )}
            {connector.login_config?.token_location && (
              <div className="flex justify-between p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary">Token Location</span>
                <span className="font-mono text-white">
                  {connector.login_config.token_location === 'header' && 'Response Header'}
                  {connector.login_config.token_location === 'cookie' && 'Cookie'}
                  {connector.login_config.token_location === 'body' && 'Response Body (JSON)'}
                </span>
              </div>
            )}
            {connector.login_config?.token_name && (
              <div className="flex justify-between p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary">Token Name</span>
                <span className="font-mono text-white">{connector.login_config.token_name}</span>
              </div>
            )}
            {connector.login_config?.header_name && (
              <div className="flex justify-between p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary">API Header Name</span>
                <span className="font-mono text-white">{connector.login_config.header_name}</span>
              </div>
            )}
            {connector.login_config?.session_duration_seconds && (
              <div className="flex justify-between p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary">Session Duration</span>
                <span className="font-mono text-white">
                  {Math.floor(connector.login_config.session_duration_seconds / 60)} minutes
                </span>
              </div>
            )}
            {connector.login_config?.login_headers && Object.keys(connector.login_config.login_headers).length > 0 && (
              <div className="col-span-2 p-3 bg-white/5 rounded-lg">
                <span className="text-text-secondary block mb-2">Custom Login Headers</span>
                <div className="space-y-1">
                  {Object.entries(connector.login_config.login_headers).map(([key, value], idx) => (
                    <div key={idx} className="font-mono text-xs text-white">
                      {key}: <span className="text-text-secondary">{value as string}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Credentials Form - Only show if NO credentials configured */}
      {!status?.has_credentials && (
        <form onSubmit={handleSubmit} className="space-y-6">
          {(credentialType === 'BASIC' || credentialType === 'SESSION') && (
            <div className="grid grid-cols-1 gap-6">
              <div>
                <label htmlFor="cred-username" className="block text-sm font-medium text-text-secondary mb-2">
                  Username
                </label>
                <input
                  id="cred-username"
                  type="text"
                  value={credentials.username || ''}
                  onChange={(e) => setCredentials({ ...credentials, username: e.target.value })}
                  className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  placeholder="Enter username"
                  required
                />
              </div>
              <div>
                <label htmlFor="cred-password" className="block text-sm font-medium text-text-secondary mb-2">
                  Password
                </label>
                <input
                  id="cred-password"
                  type="password"
                  value={credentials.password || ''}
                  onChange={(e) => setCredentials({ ...credentials, password: e.target.value })}
                  className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                  placeholder="Enter password"
                  required
                />
              </div>
              {credentialType === 'SESSION' && (
                <div className="p-4 bg-blue-500/10 border border-blue-500/20 rounded-xl flex gap-3">
                  <AlertTriangle className="h-5 w-5 text-blue-400 flex-shrink-0" />
                  <p className="text-sm text-blue-200">
                    These credentials will be used to authenticate with the login endpoint above.
                    A session token will be automatically obtained and managed for you.
                  </p>
                </div>
              )}
            </div>
          )}

          {credentialType === 'API_KEY' && (
            <div>
              <label htmlFor="cred-api-key" className="block text-sm font-medium text-text-secondary mb-2">
                API Key
              </label>
              <input
                id="cred-api-key"
                type="password"
                value={credentials.api_key || ''}
                onChange={(e) => setCredentials({ ...credentials, api_key: e.target.value })}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm"
                placeholder="Enter API key"
                required
              />
            </div>
          )}

          {credentialType === 'OAUTH2' && (
            <div className="p-6 bg-yellow-500/10 border border-yellow-500/20 rounded-xl text-center">
              <p className="text-yellow-200 font-medium">
                OAuth2 authentication will be implemented in a future update.
              </p>
            </div>
          )}

          {/* Action Buttons */}
          {credentialType !== 'OAUTH2' && (
            <div className="flex gap-3 pt-4">
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
                    Save Credentials
                  </>
                )}
              </button>
            </div>
          )}
        </form>
      )}

      {/* Security Note */}
      <div className="mt-6 flex items-center gap-3 p-4 bg-white/5 border border-white/10 rounded-xl">
        <Lock className="h-5 w-5 text-text-tertiary" />
        <p className="text-xs text-text-secondary">
          <strong>Security:</strong> Your credentials are encrypted and stored securely.
          They are only used when making API calls on your behalf and are never shared with other users.
        </p>
      </div>

      {/* Service Credential Section (Phase 74 -- admin only) */}
      {isAdmin && (
        <div className="border-t border-white/10 pt-6 mt-6">
          <div className="flex items-center gap-4 mb-6">
            <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20">
              <Lock className="h-5 w-5 text-primary" />
            </div>
            <div>
              <h3 className="text-lg font-medium text-white">
                Service Credential
              </h3>
              <p className="text-sm text-text-secondary">
                Admin-managed credential for automated sessions. Not tied to any user.
              </p>
            </div>
          </div>

          {svcCredLoading ? (
            <div className="flex items-center justify-center py-8">
              <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-primary"></div>
            </div>
          ) : svcCredFormOpen ? (
            /* Service Credential Form */
            <form onSubmit={handleSvcCredSave} className="space-y-6">
              <h4 className="text-sm font-medium text-white flex items-center gap-2">
                <div className="w-1 h-4 bg-accent rounded-full" />
                Service Credential
              </h4>

              {(svcCredType === 'PASSWORD' || svcCredType === 'SESSION') && (
                <div className="grid grid-cols-1 gap-6">
                  <div>
                    <label htmlFor="svc-cred-username" className="block text-sm font-medium text-text-secondary mb-2">
                      Username
                    </label>
                    <input
                      id="svc-cred-username"
                      type="text"
                      value={svcCredentials.username || ''}
                      onChange={(e) => setSvcCredentials({ ...svcCredentials, username: e.target.value })}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      placeholder="Enter service username"
                      required
                    />
                  </div>
                  <div>
                    <label htmlFor="svc-cred-password" className="block text-sm font-medium text-text-secondary mb-2">
                      Password
                    </label>
                    <input
                      id="svc-cred-password"
                      type="password"
                      value={svcCredentials.password || ''}
                      onChange={(e) => setSvcCredentials({ ...svcCredentials, password: e.target.value })}
                      className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      placeholder="Enter service password"
                      required
                    />
                  </div>
                </div>
              )}

              {svcCredType === 'API_KEY' && (
                <div>
                  <label htmlFor="svc-cred-api-key" className="block text-sm font-medium text-text-secondary mb-2">
                    API Key
                  </label>
                  <input
                    id="svc-cred-api-key"
                    type="password"
                    value={svcCredentials.api_key || ''}
                    onChange={(e) => setSvcCredentials({ ...svcCredentials, api_key: e.target.value })}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm"
                    placeholder="Enter service API key"
                    required
                  />
                </div>
              )}

              {!svcCredType && (
                <div>
                  <label htmlFor="svc-cred-type-select" className="block text-sm font-medium text-text-secondary mb-2">
                    Credential Type
                  </label>
                  <select
                    id="svc-cred-type-select"
                    value={svcCredType}
                    onChange={(e) => {
                      setSvcCredType(e.target.value);
                      setSvcCredentials({});
                    }}
                    className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                    required
                  >
                    <option value="" className="bg-surface">Select credential type...</option>
                    <option value="PASSWORD" className="bg-surface">Username / Password</option>
                    <option value="API_KEY" className="bg-surface">API Key</option>
                    <option value="OAUTH2_TOKEN" className="bg-surface">OAuth2 Token</option>
                  </select>
                </div>
              )}

              <div className="flex gap-3 pt-4">
                <button
                  type="submit"
                  disabled={svcCredSaving}
                  className="flex items-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {svcCredSaving ? (
                    <>
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                      Saving...
                    </>
                  ) : (
                    <>
                      <Save className="h-4 w-4" />
                      Save Service Credential
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setSvcCredFormOpen(false)}
                  className="px-4 py-2 text-sm font-medium text-text-secondary hover:text-white border border-white/10 hover:border-white/20 rounded-xl transition-all"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : svcCredStatus?.has_service_credential ? (
            /* Service credential configured */
            <div className="p-6 bg-primary/5 border border-primary/10 rounded-xl relative overflow-hidden">
              <div className="absolute top-0 right-0 w-32 h-32 bg-primary/10 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2" />
              <div className="relative z-10 flex items-center justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-2">
                    <ShieldCheck className="h-5 w-5 text-primary" />
                    <p className="text-base font-medium text-white">
                      Service Credential Configured
                    </p>
                  </div>
                  <p className="text-sm text-text-secondary mb-3">
                    Automated sessions will use this credential for connector access.
                  </p>
                  <div className="flex items-center gap-4 text-xs text-text-tertiary">
                    <span className="px-2 py-1 bg-white/5 rounded-lg border border-white/10">
                      Type: {svcCredStatus.credential_type}
                    </span>
                    {svcCredStatus.updated_at && (
                      <span>Updated: {new Date(svcCredStatus.updated_at).toLocaleString()}</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={openSvcCredForm}
                    className="px-4 py-2 text-sm font-medium text-text-secondary hover:text-white border border-white/10 hover:border-white/20 rounded-xl transition-all"
                  >
                    Update Credential
                  </button>
                  <button
                    type="button"
                    onClick={handleSvcCredDelete}
                    disabled={svcCredDeleting}
                    className="px-4 py-2 text-sm font-medium text-red-400 hover:bg-red-500/10 border border-transparent hover:border-red-500/20 rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                  >
                    <Trash2 className="h-4 w-4" />
                    {svcCredDeleting ? 'Removing...' : 'Remove'}
                  </button>
                </div>
              </div>
            </div>
          ) : (
            /* No service credential */
            <div className="p-6 bg-white/5 border border-white/10 rounded-xl">
              <div className="flex items-start gap-4">
                <Info className="h-5 w-5 text-text-tertiary flex-shrink-0 mt-0.5" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-white mb-1">
                    No service credential configured
                  </p>
                  <p className="text-sm text-text-secondary mb-4">
                    Automated sessions will fall back to the event creator's delegated credentials. Configure a service credential for reliable, user-independent automation.
                  </p>
                  <button
                    type="button"
                    onClick={openSvcCredForm}
                    className="flex items-center gap-2 px-4 py-2 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium text-sm transition-all"
                  >
                    <Lock className="h-4 w-4" />
                    Set Service Credential
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
