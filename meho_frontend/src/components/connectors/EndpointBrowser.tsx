// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Endpoint Browser Component
 * 
 * Browse, filter, and manage connector endpoints
 * Key feature: Edit endpoint metadata and safety settings
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, AlertCircle, Edit, TestTube, Upload, Filter, Tag, Shield, Activity, ChevronDown, ChevronUp, Code } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import { EndpointEditorModal } from './EndpointEditorModal';
import { TestEndpointModal } from './TestEndpointModal';
import type { Endpoint, ParameterField } from '../../lib/api-client';
import clsx from 'clsx';

interface EndpointBrowserProps {
  connectorId: string;
}

export function EndpointBrowser({ connectorId }: EndpointBrowserProps) {
  const [methodFilter, setMethodFilter] = useState<string>('');
  const [safetyFilter, setSafetyFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');
  const [editingEndpoint, setEditingEndpoint] = useState<Endpoint | null>(null);
  const [testingEndpoint, setTestingEndpoint] = useState<Endpoint | null>(null);

  const apiClient = getAPIClient(config.apiURL);

  // Fetch endpoints
  const { data: endpoints, isLoading, error, refetch } = useQuery({
    queryKey: ['endpoints', connectorId, methodFilter, safetyFilter, statusFilter],
    queryFn: () => apiClient.listEndpoints(connectorId, {
      method: methodFilter || undefined,
      safety_level: safetyFilter || undefined,
      is_enabled: statusFilter === 'enabled' ? true : statusFilter === 'disabled' ? false : undefined,
      limit: 500
    }),
  });

  // Filter by search query (client-side)
  const filteredEndpoints = endpoints?.filter((endpoint) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      endpoint.path.toLowerCase().includes(query) ||
      endpoint.summary?.toLowerCase().includes(query) ||
      endpoint.description?.toLowerCase().includes(query) ||
      endpoint.operation_id?.toLowerCase().includes(query)
    );
  }) || [];

  // Count by status
  const counts = {
    all: endpoints?.length || 0,
    enabled: endpoints?.filter(e => e.is_enabled).length || 0,
    disabled: endpoints?.filter(e => !e.is_enabled).length || 0,
    safe: endpoints?.filter(e => e.safety_level === 'safe').length || 0,
    caution: endpoints?.filter(e => e.safety_level === 'caution').length || 0,
    dangerous: endpoints?.filter(e => e.safety_level === 'dangerous').length || 0,
  };

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-lg font-bold text-white flex items-center gap-2">
            Endpoints
            <span className="px-2 py-0.5 bg-white/10 rounded-full text-xs font-medium text-text-secondary">
              {filteredEndpoints.length}
            </span>
          </h3>

          {/* Stats */}
          <div className="flex gap-4 text-xs font-medium text-text-secondary">
            <span className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 rounded-full bg-green-400" />
              {counts.enabled} Enabled
            </span>
            <span className="flex items-center gap-1.5">
              <div className="w-1.5 h-1.5 rounded-full bg-white/20" />
              {counts.disabled} Disabled
            </span>
          </div>
        </div>

        {/* Filters */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-6">
          <div className="relative group">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary group-focus-within:text-primary transition-colors" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search endpoints..."
              className="w-full pl-10 pr-3 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm"
            />
          </div>

          <div className="relative">
            <Filter className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary pointer-events-none" />
            <select
              value={methodFilter}
              onChange={(e) => setMethodFilter(e.target.value)}
              className="w-full pl-10 pr-3 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm appearance-none cursor-pointer hover:bg-white/10"
            >
              <option value="" className="bg-surface text-white">All Methods</option>
              <option value="GET" className="bg-surface text-white">GET</option>
              <option value="POST" className="bg-surface text-white">POST</option>
              <option value="PUT" className="bg-surface text-white">PUT</option>
              <option value="PATCH" className="bg-surface text-white">PATCH</option>
              <option value="DELETE" className="bg-surface text-white">DELETE</option>
            </select>
          </div>

          <div className="relative">
            <Shield className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary pointer-events-none" />
            <select
              value={safetyFilter}
              onChange={(e) => setSafetyFilter(e.target.value)}
              className="w-full pl-10 pr-3 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm appearance-none cursor-pointer hover:bg-white/10"
            >
              <option value="" className="bg-surface text-white">All Safety Levels</option>
              <option value="safe" className="bg-surface text-white">🟢 Safe ({counts.safe})</option>
              <option value="caution" className="bg-surface text-white">🟡 Caution ({counts.caution})</option>
              <option value="dangerous" className="bg-surface text-white">🔴 Dangerous ({counts.dangerous})</option>
            </select>
          </div>

          <div className="relative">
            <Activity className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary pointer-events-none" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="w-full pl-10 pr-3 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm appearance-none cursor-pointer hover:bg-white/10"
            >
              <option value="" className="bg-surface text-white">All Status</option>
              <option value="enabled" className="bg-surface text-white">✅ Enabled ({counts.enabled})</option>
              <option value="disabled" className="bg-surface text-white">❌ Disabled ({counts.disabled})</option>
            </select>
          </div>
        </div>
      </div>

      {/* Loading State */}
      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
        </div>
      )}

      {/* Error State */}
      {error && (
        <div className="flex items-center gap-3 p-4 bg-red-500/10 border border-red-500/20 text-red-200 rounded-xl">
          <AlertCircle className="h-5 w-5 text-red-400" />
          <span>Failed to load endpoints: {(error as Error).message}</span>
        </div>
      )}

      {/* Empty State */}
      {!isLoading && !error && filteredEndpoints.length === 0 && !searchQuery && (
        <div className="flex flex-col items-center justify-center py-16 bg-white/5 border border-white/10 rounded-2xl border-dashed">
          <div className="w-16 h-16 rounded-full bg-white/5 flex items-center justify-center mb-4">
            <Upload className="h-8 w-8 text-text-secondary" />
          </div>
          <p className="text-white font-medium mb-2">No endpoints yet</p>
          <p className="text-sm text-text-secondary">
            Upload an OpenAPI spec to extract endpoints
          </p>
        </div>
      )}

      {/* No search results */}
      {!isLoading && !error && filteredEndpoints.length === 0 && searchQuery && (
        <div className="flex flex-col items-center justify-center py-16 bg-white/5 border border-white/10 rounded-2xl border-dashed">
          <div className="w-16 h-16 rounded-full bg-white/5 flex items-center justify-center mb-4">
            <Search className="h-8 w-8 text-text-secondary" />
          </div>
          <p className="text-white font-medium">No endpoints match your search</p>
        </div>
      )}

      {/* Endpoint List */}
      <div className="space-y-3">
        <AnimatePresence mode="popLayout">
          {filteredEndpoints.map((endpoint) => (
            <motion.div
              key={endpoint.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              layout
            >
              <EndpointCard
                endpoint={endpoint}
                onEdit={() => setEditingEndpoint(endpoint)}
                onTest={() => setTestingEndpoint(endpoint)}
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>

      {/* Modals */}
      <AnimatePresence>
        {editingEndpoint && (
          <EndpointEditorModal
            connectorId={connectorId}
            endpoint={editingEndpoint}
            onClose={() => setEditingEndpoint(null)}
            onSuccess={() => {
              setEditingEndpoint(null);
              refetch();
            }}
          />
        )}

        {testingEndpoint && (
          <TestEndpointModal
            connectorId={connectorId}
            endpoint={testingEndpoint}
            onClose={() => setTestingEndpoint(null)}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

interface EndpointCardProps {
  endpoint: Endpoint;
  onEdit: () => void;
  onTest: () => void;
}

function EndpointCard({ endpoint, onEdit, onTest }: EndpointCardProps) {
  const [showDetails, setShowDetails] = useState(false);
  
  const methodColors: Record<string, string> = {
    GET: 'bg-green-500/10 text-green-400 border-green-500/20',
    POST: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    PUT: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    PATCH: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    DELETE: 'bg-red-500/10 text-red-400 border-red-500/20',
  };

  const safetyColors: Record<string, string> = {
    safe: 'bg-green-500/10 text-green-400 border-green-500/20',
    auto: 'bg-white/10 text-text-secondary border-white/20',
    read: 'bg-green-500/10 text-green-400 border-green-500/20',
    caution: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    write: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    dangerous: 'bg-red-500/10 text-red-400 border-red-500/20',
    destructive: 'bg-red-500/10 text-red-400 border-red-500/20',
  };

  const safetyIcons: Record<string, string> = {
    safe: 'Auto',
    auto: 'Auto',
    read: 'Read',
    caution: 'Write',
    write: 'Write',
    dangerous: 'Destructive',
    destructive: 'Destructive',
  };
  
  const hasSchemas = endpoint.path_params_schema && Object.keys(endpoint.path_params_schema).length > 0 ||
                     endpoint.query_params_schema && Object.keys(endpoint.query_params_schema).length > 0 ||
                     endpoint.body_schema && Object.keys(endpoint.body_schema).length > 0 ||
                     endpoint.response_schema && Object.keys(endpoint.response_schema).length > 0;

  return (
    <div className={clsx(
      "group relative border rounded-xl p-4 transition-all duration-200",
      endpoint.is_enabled
        ? "bg-white/5 border-white/10 hover:bg-white/[0.07] hover:border-white/20 hover:shadow-lg hover:shadow-black/20"
        : "bg-white/[0.02] border-white/5 opacity-60 hover:opacity-100"
    )}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          {/* Header */}
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <span className={clsx(
              "text-xs font-mono font-bold px-2 py-1 rounded-lg border",
              methodColors[endpoint.method] || 'bg-white/10 text-text-secondary border-white/10'
            )}>
              {endpoint.method}
            </span>

            <code className="text-sm font-mono text-white/90">{endpoint.path}</code>

            {!endpoint.is_enabled && (
              <span className="text-xs font-medium px-2 py-1 rounded-lg bg-white/10 text-text-secondary border border-white/10">
                Disabled
              </span>
            )}

            {endpoint.requires_approval && (
              <span className="text-xs font-medium px-2 py-1 rounded-lg bg-orange-500/10 text-orange-400 border border-orange-500/20 flex items-center gap-1">
                <AlertCircle className="h-3 w-3" />
                Approval
              </span>
            )}

            <span className={clsx(
              "text-xs font-medium px-2 py-1 rounded-lg border flex items-center gap-1",
              safetyColors[endpoint.safety_level] || 'bg-white/10 text-text-secondary border-white/20'
            )}>
              <Shield className="h-3 w-3" />
              {safetyIcons[endpoint.safety_level] || endpoint.safety_level}
            </span>

            {endpoint.safety_level && !['safe', 'auto'].includes(endpoint.safety_level) && (
              <span className={clsx(
                "px-1.5 py-0.5 text-[10px] font-bold uppercase rounded",
                endpoint.safety_level === 'read' && "bg-green-500/10 text-green-400",
                (endpoint.safety_level === 'write' || endpoint.safety_level === 'caution') && "bg-yellow-500/10 text-yellow-400",
                (endpoint.safety_level === 'destructive' || endpoint.safety_level === 'dangerous') && "bg-red-500/10 text-red-400",
              )}>
                override
              </span>
            )}
          </div>

          {/* Description */}
          {endpoint.custom_description && (
            <div className="mb-3 p-3 bg-primary/5 border border-primary/10 rounded-lg text-sm">
              <p className="text-primary font-medium mb-1 text-xs uppercase tracking-wider">Enhanced Description</p>
              <p className="text-text-secondary whitespace-pre-wrap text-sm">{endpoint.custom_description.substring(0, 200)}{endpoint.custom_description.length > 200 ? '...' : ''}</p>
            </div>
          )}

          <p className="text-sm text-text-secondary line-clamp-2">
            {endpoint.summary || endpoint.description || <span className="italic opacity-50">No description provided</span>}
          </p>

          {/* Tags */}
          {endpoint.tags.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-3">
              {endpoint.tags.map((tag) => (
                <span
                  key={tag}
                  className="inline-flex items-center gap-1 px-2 py-0.5 bg-white/5 border border-white/10 text-text-secondary rounded-md text-xs"
                >
                  <Tag className="h-3 w-3 opacity-50" />
                  {tag}
                </span>
              ))}
            </div>
          )}
          
          {/* Show Details Button */}
          {hasSchemas && (
            <button
              onClick={() => setShowDetails(!showDetails)}
              className="mt-3 flex items-center gap-2 text-xs text-primary hover:text-primary-hover transition-colors"
            >
              <Code className="h-3 w-3" />
              {showDetails ? 'Hide' : 'Show'} API Details
              {showDetails ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </button>
          )}
          
          {/* Expandable Schema Details */}
          {showDetails && hasSchemas && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="mt-4 space-y-3 border-t border-white/10 pt-4"
            >
              {/* Parameter Metadata - LLM-friendly structured format */}
              {endpoint.parameter_metadata && (
                <div className="bg-primary/5 border border-primary/10 rounded-lg p-3 space-y-3">
                  <p className="text-xs font-bold text-primary uppercase tracking-wider">
                    📋 Parameter Requirements
                  </p>
                  
                  {/* Path Parameters */}
                  {endpoint.parameter_metadata.path_params && (endpoint.parameter_metadata.path_params.required?.length > 0 || endpoint.parameter_metadata.path_params.optional?.length > 0) && (
                    <div className="space-y-1">
                      <p className="text-xs text-text-secondary">Path Parameters:</p>
                      {endpoint.parameter_metadata.path_params.required?.map((param: ParameterField) => (
                        <div key={param.name} className="flex items-baseline gap-2 text-xs ml-3">
                          <span className="text-red-400">✱</span>
                          <code className="text-primary font-mono">{param.name}</code>
                          <span className="text-text-tertiary">({param.type})</span>
                          <span className="text-red-400/70">required</span>
                          {param.description && <span className="text-text-secondary">- {param.description}</span>}
                        </div>
                      ))}
                      {endpoint.parameter_metadata.path_params.optional?.map((param: ParameterField) => (
                        <div key={param.name} className="flex items-baseline gap-2 text-xs ml-3">
                          <span className="text-text-tertiary">○</span>
                          <code className="text-primary font-mono">{param.name}</code>
                          <span className="text-text-tertiary">({param.type})</span>
                          <span className="text-text-tertiary/70">optional</span>
                          {param.description && <span className="text-text-secondary">- {param.description}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                  
                  {/* Query Parameters */}
                  {endpoint.parameter_metadata.query_params && (endpoint.parameter_metadata.query_params.required?.length > 0 || endpoint.parameter_metadata.query_params.optional?.length > 0) && (
                    <div className="space-y-1">
                      <p className="text-xs text-text-secondary">Query Parameters:</p>
                      {endpoint.parameter_metadata.query_params.required?.map((param: ParameterField) => (
                        <div key={param.name} className="flex items-baseline gap-2 text-xs ml-3">
                          <span className="text-red-400">✱</span>
                          <code className="text-primary font-mono">{param.name}</code>
                          <span className="text-text-tertiary">({param.type})</span>
                          <span className="text-red-400/70">required</span>
                          {param.description && <span className="text-text-secondary">- {param.description}</span>}
                        </div>
                      ))}
                      {endpoint.parameter_metadata.query_params.optional?.map((param: ParameterField) => (
                        <div key={param.name} className="flex items-baseline gap-2 text-xs ml-3">
                          <span className="text-text-tertiary">○</span>
                          <code className="text-primary font-mono">{param.name}</code>
                          <span className="text-text-tertiary">({param.type})</span>
                          <span className="text-text-tertiary/70">optional</span>
                          {param.description && <span className="text-text-secondary">- {param.description}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                  
                  {/* Body Requirements */}
                  {endpoint.parameter_metadata.body && endpoint.parameter_metadata.body.required && (
                    <div className="space-y-1">
                      <p className="text-xs text-text-secondary">
                        Request Body: <span className="text-red-400">required</span>
                      </p>
                      {endpoint.parameter_metadata.body.required_fields?.length > 0 && (
                        <div className="ml-3 space-y-1">
                          <p className="text-xs text-text-tertiary">Required fields:</p>
                          {endpoint.parameter_metadata.body.required_fields.map((field: ParameterField) => (
                            <div key={field.name} className="flex items-baseline gap-2 text-xs ml-3">
                              <span className="text-red-400">✱</span>
                              <code className="text-primary font-mono">{field.name}</code>
                              <span className="text-text-tertiary">({field.type})</span>
                              {field.description && <span className="text-text-secondary">- {field.description}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      {endpoint.parameter_metadata.body.optional_fields?.length > 0 && (
                        <div className="ml-3 space-y-1">
                          <p className="text-xs text-text-tertiary">Optional fields:</p>
                          {endpoint.parameter_metadata.body.optional_fields.map((field: ParameterField) => (
                            <div key={field.name} className="flex items-baseline gap-2 text-xs ml-3">
                              <span className="text-text-tertiary">○</span>
                              <code className="text-primary font-mono">{field.name}</code>
                              <span className="text-text-tertiary">({field.type})</span>
                              {field.description && <span className="text-text-secondary">- {field.description}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
              
              {/* Path Parameters */}
              {endpoint.path_params_schema && Object.keys(endpoint.path_params_schema).length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs text-accent uppercase tracking-wider flex items-center gap-2">
                    <Code className="h-3 w-3" />
                    Path Parameters
                  </p>
                  <div className="space-y-1">
                    {Object.entries(endpoint.path_params_schema).map(([name, rawSchema]: [string, unknown]) => {
                      const schema = rawSchema as Record<string, unknown>;
                      return (
                        <div key={name} className="flex items-baseline gap-2 text-xs">
                          <code className="text-primary font-mono">{name}</code>
                          <span className="text-text-tertiary">({(schema.type as string) || 'string'})</span>
                          {typeof schema.description === 'string' && <span className="text-text-secondary">- {schema.description}</span>}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              
              {/* Query Parameters */}
              {endpoint.query_params_schema && Object.keys(endpoint.query_params_schema).length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs text-accent uppercase tracking-wider flex items-center gap-2">
                    <Code className="h-3 w-3" />
                    Query Parameters
                  </p>
                  <div className="space-y-1">
                    {Object.entries(endpoint.query_params_schema).map(([name, rawSchema]: [string, unknown]) => {
                      const schema = rawSchema as Record<string, unknown>;
                      return (
                        <div key={name} className="flex items-baseline gap-2 text-xs">
                          <code className="text-primary font-mono">{name}</code>
                          <span className="text-text-tertiary">
                            ({(schema.type as string) || 'string'}
                            {schema.default !== undefined && `, default: ${JSON.stringify(schema.default)}`})
                          </span>
                          {typeof schema.description === 'string' && <span className="text-text-secondary">- {schema.description}</span>}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              
              {/* Request Body */}
              {endpoint.body_schema && Object.keys(endpoint.body_schema).length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs text-accent uppercase tracking-wider flex items-center gap-2">
                    <Code className="h-3 w-3" />
                    Request Body
                  </p>
                  <pre className="text-xs text-text-primary font-mono bg-black/40 p-3 rounded-lg overflow-x-auto">
                    {JSON.stringify(endpoint.body_schema, null, 2)}
                  </pre>
                </div>
              )}
              
              {/* Response Schema */}
              {endpoint.response_schema && Object.keys(endpoint.response_schema).length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs text-green-400 uppercase tracking-wider flex items-center gap-2">
                    <Code className="h-3 w-3" />
                    Response Schema
                  </p>
                  <pre className="text-xs text-text-primary font-mono bg-black/40 p-3 rounded-lg overflow-x-auto max-h-64">
                    {JSON.stringify(endpoint.response_schema, null, 2)}
                  </pre>
                </div>
              )}
            </motion.div>
          )}
        </div>

        {/* Actions */}
        <div className="flex flex-col gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={onEdit}
            className="p-2 text-text-secondary hover:text-white hover:bg-white/10 rounded-lg transition-colors"
            title="Edit endpoint"
          >
            <Edit className="h-4 w-4" />
          </button>
          <button
            onClick={onTest}
            className="p-2 text-text-secondary hover:text-primary hover:bg-primary/10 rounded-lg transition-colors"
            title="Test endpoint"
            disabled={!endpoint.is_enabled}
          >
            <TestTube className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
