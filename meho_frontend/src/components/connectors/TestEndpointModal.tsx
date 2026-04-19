// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Test Endpoint Modal
 * 
 * Interface for testing API endpoints with live requests
 */
import { useState, useCallback } from 'react';
import { X, Play, Loader2, AlertCircle, Copy, Clock, Code } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { Endpoint, TestEndpointRequest, TestEndpointResponse } from '../../lib/api-client';
import clsx from 'clsx';

interface TestEndpointModalProps {
  connectorId: string;
  endpoint: Endpoint;
  onClose: () => void;
}

export function TestEndpointModal({ connectorId, endpoint, onClose }: Readonly<TestEndpointModalProps>) {
  const [pathParams, setPathParams] = useState<Record<string, string>>({});
  const [queryParams, setQueryParams] = useState<Record<string, string>>({});
  const [bodyJson, setBodyJson] = useState('');
  const [useSystemCreds, setUseSystemCreds] = useState(true);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<TestEndpointResponse | null>(null);

  const apiClient = getAPIClient(config.apiURL);

  // Extract path parameters from path template
  const pathParamNames = (endpoint.path.match(/\{([^}]+)\}/g) || [])
    .map(p => p.slice(1, -1));

  const handleTest = useCallback(async () => {
    setTesting(true);
    setResult(null);

    try {
      let parsedBody = undefined;
      if (bodyJson.trim()) {
        try {
          parsedBody = JSON.parse(bodyJson);
        } catch (_e) {
          throw new Error('Invalid JSON in request body');
        }
      }

      const request: TestEndpointRequest = {
        path_params: pathParams,
        query_params: queryParams,
        body: parsedBody,
        use_system_credentials: useSystemCreds,
      };

      const response = await apiClient.testEndpoint(connectorId, endpoint.id, request);
      setResult(response);

    } catch (err: unknown) {
      setResult({
        status_code: 500,
        headers: {},
        body: null,
        duration_ms: 0,
        error: err instanceof Error ? err.message : 'Request failed',
      });
    } finally {
      setTesting(false);
    }
  }, [connectorId, endpoint.id, pathParams, queryParams, bodyJson, useSystemCreds, apiClient]);

  const copyResponse = useCallback(() => {
    if (result) {
      navigator.clipboard.writeText(JSON.stringify(result.body, null, 2));
    }
  }, [result]);

  const methodColors: Record<string, string> = {
    GET: 'bg-green-500/10 text-green-400 border-green-500/20',
    POST: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    PUT: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    PATCH: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    DELETE: 'bg-red-500/10 text-red-400 border-red-500/20',
  };

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50 p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="relative w-full max-w-4xl max-h-[90vh] overflow-y-auto glass rounded-2xl border border-white/10 shadow-2xl"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between p-6 border-b border-white/10 bg-surface/95 backdrop-blur-xl">
          <div className="flex-1 min-w-0">
            <h2 className="text-xl font-bold text-white mb-2">Test Endpoint</h2>
            <div className="flex items-center gap-3">
              <span className={clsx(
                "text-xs font-mono font-bold px-2 py-1 rounded-lg border",
                methodColors[endpoint.method] || 'bg-white/10 text-text-secondary border-white/10'
              )}>
                {endpoint.method}
              </span>
              <code className="text-sm font-mono text-white/90">{endpoint.path}</code>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-white/10 rounded-xl text-text-secondary hover:text-white transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Form */}
        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Path Parameters */}
            {pathParamNames.length > 0 && (
              <div className="md:col-span-2">
                <span className="block text-sm font-medium text-text-secondary mb-3">
                  Path Parameters
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {pathParamNames.map((paramName) => (
                    <div key={paramName}>
                      <label htmlFor={`test-path-param-${paramName}`} className="block text-xs text-text-tertiary mb-1.5">{paramName}</label>
                      <input
                        id={`test-path-param-${paramName}`}
                        type="text"
                        value={pathParams[paramName] || ''}
                        onChange={(e) => setPathParams({ ...pathParams, [paramName]: e.target.value })}
                        placeholder={`Value for ${paramName}`}
                        className="w-full px-4 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm"
                      />
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Query Parameters */}
            <div className={['POST', 'PUT', 'PATCH'].includes(endpoint.method) ? '' : 'md:col-span-2'}>
              <label htmlFor="test-query-params" className="block text-sm font-medium text-text-secondary mb-2">
                Query Parameters (JSON)
              </label>
              <textarea
                id="test-query-params"
                value={JSON.stringify(queryParams, null, 2)}
                onChange={(e) => {
                  try {
                    setQueryParams(JSON.parse(e.target.value || '{}'));
                  } catch { /* ignore invalid JSON while typing */ }
                }}
                placeholder="{}"
                rows={6}
                className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none font-mono text-sm"
              />
            </div>

            {/* Request Body */}
            {['POST', 'PUT', 'PATCH'].includes(endpoint.method) && (
              <div>
                <label htmlFor="test-request-body" className="block text-sm font-medium text-text-secondary mb-2">
                  Request Body (JSON)
                </label>
                <textarea
                  id="test-request-body"
                  value={bodyJson}
                  onChange={(e) => setBodyJson(e.target.value)}
                  placeholder={`{\n  "key": "value"\n}`}
                  rows={6}
                  className="w-full px-4 py-3 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none font-mono text-sm"
                />
              </div>
            )}
          </div>

          {/* Credentials */}
          <div className="flex items-center gap-3 p-4 bg-white/5 border border-white/10 rounded-xl">
            <label className="flex items-center gap-3 cursor-pointer group flex-1">
              <div className="relative flex items-center">
                <input
                  type="checkbox"
                  checked={useSystemCreds}
                  onChange={(e) => setUseSystemCreds(e.target.checked)}
                  className="peer h-5 w-5 rounded border-white/20 bg-white/5 text-primary focus:ring-primary/50 transition-all"
                />
              </div>
              <span className="text-sm font-medium text-text-secondary group-hover:text-white transition-colors">
                Use system credentials (if configured)
              </span>
            </label>
          </div>

          {/* Send Button */}
          <button
            onClick={handleTest}
            disabled={testing || (pathParamNames.length > 0 && Object.keys(pathParams).length < pathParamNames.length)}
            className="w-full flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-green-600 to-green-500 hover:shadow-lg hover:shadow-green-500/25 text-white rounded-xl font-bold transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {testing ? (
              <>
                <Loader2 className="h-5 w-5 animate-spin" />
                Sending request...
              </>
            ) : (
              <>
                <Play className="h-5 w-5 fill-current" />
                Send Request
              </>
            )}
          </button>

          {/* Response */}
          <AnimatePresence>
            {result && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="border-t border-white/10 pt-6"
              >
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-bold text-white uppercase tracking-wider">Response</h3>
                  <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1.5 text-xs text-text-secondary bg-white/5 px-2 py-1 rounded-lg border border-white/10">
                      <Clock className="h-3 w-3" />
                      {result.duration_ms}ms
                    </div>
                    <span className={clsx(
                      "px-2 py-1 rounded-lg text-xs font-bold border",
                      (() => {
                        if (result.status_code >= 200 && result.status_code < 300) return "bg-green-500/10 text-green-400 border-green-500/20";
                        if (result.status_code >= 400) return "bg-red-500/10 text-red-400 border-red-500/20";
                        return "bg-white/10 text-text-secondary border-white/10";
                      })()
                    )}>
                      {result.status_code} {result.error ? 'ERROR' : 'OK'}
                    </span>
                    {!!result.body && (
                      <button
                        onClick={copyResponse}
                        className="p-1.5 hover:bg-white/10 rounded-lg text-text-secondary hover:text-white transition-colors"
                        title="Copy response"
                      >
                        <Copy className="h-4 w-4" />
                      </button>
                    )}
                  </div>
                </div>

                {result.error ? (
                  <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl">
                    <div className="flex items-start gap-3">
                      <AlertCircle className="h-5 w-5 text-red-400 mt-0.5" />
                      <div className="flex-1">
                        <p className="font-bold text-red-200 mb-1">Request Failed</p>
                        <p className="text-sm text-red-300/80">{result.error}</p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    {/* Headers */}
                    {Object.keys(result.headers).length > 0 && (
                      <div className="bg-black/20 rounded-xl border border-white/5 overflow-hidden">
                        <div className="px-4 py-2 bg-white/5 border-b border-white/5 flex items-center gap-2">
                          <Code className="h-3 w-3 text-text-tertiary" />
                          <span className="text-xs font-medium text-text-secondary">Headers</span>
                        </div>
                        <div className="p-4 text-xs font-mono space-y-1">
                          {Object.entries(result.headers).map(([key, value]) => (
                            <div key={key} className="flex gap-2">
                              <span className="text-text-tertiary select-none">{key}:</span>
                              <span className="text-white/80 break-all">{value}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Body */}
                    <div className="bg-black/20 rounded-xl border border-white/5 overflow-hidden">
                      <div className="px-4 py-2 bg-white/5 border-b border-white/5 flex items-center gap-2">
                        <Code className="h-3 w-3 text-text-tertiary" />
                        <span className="text-xs font-medium text-text-secondary">Body</span>
                      </div>
                      <pre className="p-4 text-xs font-mono text-white/90 overflow-x-auto max-h-96 overflow-y-auto">
                        {JSON.stringify(result.body, null, 2)}
                      </pre>
                    </div>
                  </div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  );
}
