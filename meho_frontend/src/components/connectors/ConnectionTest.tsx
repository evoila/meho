// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connection Test Component (Task 29)
 * 
 * Test connection to a connector:
 * - For SESSION auth: Test login flow (calls /test-auth)
 * - For other auth: Test connection (calls /test-connection)
 * - Show success/failure with details
 * - Display response time / session info
 * - Only available when credentials are stored
 */

import { useState } from 'react';
import {
  CheckCircle,
  XCircle,
  Clock,
  RefreshCw,
  LogIn,
  AlertCircle,
  Activity
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import type { Connector, CredentialStatus, MEHOAPIClient, TestConnectionResponse, TestAuthResponse } from '../../lib/api-client';
import clsx from 'clsx';

interface ConnectionTestProps {
  connector: Connector;
  apiClient: MEHOAPIClient;
  credentialStatus: CredentialStatus | null;
}

export default function ConnectionTest({ // NOSONAR (cognitive complexity)
  connector,
  apiClient,
  credentialStatus
}: Readonly<ConnectionTestProps>) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<Partial<TestConnectionResponse & TestAuthResponse> & { success: boolean; message: string; error_detail?: string } | null>(null);

  const isSessionAuth = connector.auth_type === 'SESSION';
  const hasCredentials = credentialStatus?.has_credentials || false;

  const handleTest = async () => {
    setTesting(true);
    setResult(null);

    try {
      let testResult;

      if (isSessionAuth) {
        // For SESSION auth: test login flow using stored credentials
        testResult = await apiClient.testAuth(connector.id, {});
      } else {
        // For other auth types: test connection
        testResult = await apiClient.testConnection(connector.id, {
          use_stored_credentials: true,
        });
      }

      setResult(testResult);
    } catch (err: unknown) {
      // Parse error response if available
      const errObj = err instanceof Error ? err : null;
      let errorDetail = errObj?.message ?? 'Unknown error';
      if (err && typeof err === 'object' && 'response' in err) {
        const resp = (err as { response?: { data?: { detail?: string; message?: string } } }).response;
        if (resp?.data) {
          errorDetail = resp.data.detail ?? resp.data.message ?? errorDetail;
        }
      }

      setResult({
        success: false,
        message: isSessionAuth ? 'Login test failed' : 'Connection test failed',
        error_detail: errorDetail,
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center border border-primary/20">
            <Activity className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h4 className="text-lg font-bold text-white">
              {isSessionAuth ? 'Login Test' : 'Connection Test'}
            </h4>
            <p className="text-sm text-text-secondary">
              Verify your configuration and credentials
            </p>
          </div>
        </div>

        {hasCredentials ? (
          <button
            onClick={handleTest}
            disabled={testing}
            className="px-4 py-2 text-sm font-medium text-white bg-primary hover:bg-primary-light rounded-xl transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2 shadow-lg shadow-primary/20"
          >
            {testing ? (
              <>
                <RefreshCw className="h-4 w-4 animate-spin" />
                {isSessionAuth ? 'Logging in...' : 'Testing...'}
              </>
            ) : (
              <>
                {isSessionAuth ? <LogIn className="h-4 w-4" /> : <Activity className="h-4 w-4" />}
                {isSessionAuth ? 'Test Login' : 'Test Connection'}
              </>
            )}
          </button>
        ) : (
          <div className="px-4 py-2 text-sm text-text-secondary bg-white/5 border border-white/10 rounded-xl flex items-center gap-2">
            <AlertCircle className="h-4 w-4" />
            Not available
          </div>
        )}
      </div>

      {/* Message when no credentials */}
      {!hasCredentials && (
        <div className="p-4 bg-yellow-500/10 border border-yellow-500/20 rounded-xl flex items-center gap-3">
          <AlertCircle className="h-5 w-5 text-yellow-400 flex-shrink-0" />
          <p className="text-sm text-yellow-200">
            Please save your credentials first before testing {isSessionAuth ? 'login' : 'connection'}.
          </p>
        </div>
      )}

      <AnimatePresence>
        {result && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className={clsx(
              "p-6 rounded-xl border relative overflow-hidden",
              result.success
                ? "bg-green-500/10 border-green-500/20"
                : "bg-red-500/10 border-red-500/20"
            )}
          >
            {/* Background Glow */}
            <div className={clsx(
              "absolute top-0 right-0 w-32 h-32 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2 opacity-20 pointer-events-none",
              result.success ? "bg-green-500" : "bg-red-500"
            )} />

            <div className="relative z-10 flex items-start gap-4">
              {result.success ? (
                <div className="w-10 h-10 rounded-full bg-green-500/20 flex items-center justify-center border border-green-500/30 flex-shrink-0">
                  <CheckCircle className="h-5 w-5 text-green-400" />
                </div>
              ) : (
                <div className="w-10 h-10 rounded-full bg-red-500/20 flex items-center justify-center border border-red-500/30 flex-shrink-0">
                  <XCircle className="h-5 w-5 text-red-400" />
                </div>
              )}

              <div className="flex-1 min-w-0">
                <p className={clsx(
                  "text-base font-bold mb-1",
                  result.success ? "text-green-200" : "text-red-200"
                )}>
                  {result.message}
                </p>

                {/* Request Details */}
                {result.request_url && (
                  <div className="mt-4 p-4 bg-black/20 rounded-lg border border-white/5">
                    <p className="text-xs font-bold text-white/70 mb-3 uppercase tracking-wider">
                      Request Details
                    </p>
                    <div className="space-y-2 text-xs font-mono">
                      <div className="flex gap-2">
                        <span className="text-text-tertiary w-20">Method:</span>
                        <span className="text-white bg-white/10 px-1.5 py-0.5 rounded">
                          {result.request_method}
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <span className="text-text-tertiary w-20">URL:</span>
                        <span className="text-white bg-white/10 px-1.5 py-0.5 rounded break-all">
                          {result.request_url}
                        </span>
                      </div>
                      {result.response_status && (
                        <div className="flex gap-2">
                          <span className="text-text-tertiary w-20">Status:</span>
                          <span className={clsx(
                            "px-1.5 py-0.5 rounded",
                            result.response_status >= 200 && result.response_status < 300
                              ? "bg-green-500/20 text-green-400"
                              : "bg-red-500/20 text-red-400"
                          )}>
                            {result.response_status}
                          </span>
                        </div>
                      )}
                      {result.response_time_ms !== undefined && (
                        <div className="flex gap-2 items-center">
                          <span className="text-text-tertiary w-20">Time:</span>
                          <span className="text-text-secondary flex items-center gap-1">
                            <Clock className="h-3 w-3" />
                            {result.response_time_ms}ms
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* SESSION auth specific info */}
                {isSessionAuth && result.session_token_obtained && (
                  <div className="mt-4 p-4 bg-green-500/10 border border-green-500/20 rounded-lg">
                    <p className="text-xs font-bold text-green-400 mb-2 uppercase tracking-wider">
                      Session Info
                    </p>
                    <div className="space-y-1 text-xs text-green-300">
                      <p>✓ Access token obtained successfully</p>
                      {result.session_expires_at && (
                        <p>✓ Expires: {new Date(result.session_expires_at).toLocaleString()}</p>
                      )}
                    </div>
                  </div>
                )}

                {/* Tested Endpoint (non-SESSION) */}
                {!isSessionAuth && result.tested_endpoint && (
                  <p className="mt-2 text-xs text-text-secondary">
                    Tested: <code className="bg-white/10 px-1 py-0.5 rounded text-white">
                      {result.tested_endpoint}
                    </code>
                  </p>
                )}

                {/* Error Details */}
                {result.error_detail && (
                  <div className="mt-4">
                    <details className="group">
                      <summary className="text-xs text-red-300 cursor-pointer hover:text-red-200 flex items-center gap-1 select-none">
                        <AlertCircle className="h-3 w-3" />
                        Show error details
                      </summary>
                      <pre className="mt-2 text-xs bg-black/30 p-3 rounded-lg overflow-x-auto text-red-200 border border-red-500/20 font-mono whitespace-pre-wrap">
                        {result.error_detail}
                      </pre>
                    </details>
                  </div>
                )}
              </div>
            </div>

            {/* Interpretation */}
            {!result.success && (
              <div className="mt-4 pt-4 border-t border-red-500/20">
                <p className="text-xs font-bold text-red-300 mb-2">
                  Possible causes:
                </p>
                <ul className="text-xs text-red-200/80 list-disc list-inside space-y-1">
                  {result.status_code === 404 && (
                    <li>The base URL may be incorrect. Try adding or removing path segments (e.g., /api/).</li>
                  )}
                  {(result.status_code === 401 || result.status_code === 403) && (
                    <li>Authentication failed. Check your credentials.</li>
                  )}
                  {!result.status_code && (
                    <>
                      <li>The service may be unreachable or down.</li>
                      <li>Check network connectivity and firewall settings.</li>
                      <li>Verify the base URL is correct.</li>
                    </>
                  )}
                </ul>
              </div>
            )}

            {result.success && (
              <div className="mt-4 pt-4 border-t border-green-500/20">
                <p className="text-sm text-green-300">
                  {isSessionAuth
                    ? 'Login successful! Session authentication is working correctly.'
                    : 'Connection successful! You can now use this connector in workflows.'
                  }
                </p>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {!result && !testing && (
        <div className="p-4 bg-white/5 border border-white/10 rounded-xl text-sm text-text-secondary">
          {isSessionAuth
            ? 'Enter your credentials above and click "Test Login" to verify the login endpoint is configured correctly.'
            : 'Click "Test Connection" to verify the connector is configured correctly. This will make a test API call to check connectivity and authentication.'
          }
        </div>
      )}
    </div>
  );
}
