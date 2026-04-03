// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Server, Cpu, Database, Network, List, Loader2, X, ChevronRight, Power, RotateCcw, Copy } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient, type ConnectorOperation } from '../../lib/api-client';
import { config } from '../../lib/config';
import { OperationBadge } from './OperationBadge';
import clsx from 'clsx';

interface TypedOperationsBrowserProps {
  connectorId: string;
  connectorType: string;
}

// Category icons
const getCategoryIcon = (category?: string) => {
  switch (category) {
    case 'compute': return <Cpu className="h-4 w-4" />;
    case 'storage': return <Database className="h-4 w-4" />;
    case 'network': return <Network className="h-4 w-4" />;
    case 'cluster': return <Server className="h-4 w-4" />;
    default: return <List className="h-4 w-4" />;
  }
};

// Category colors
const getCategoryColor = (category?: string) => {
  switch (category) {
    case 'compute': return 'bg-blue-500/10 text-blue-400 border-blue-500/20';
    case 'storage': return 'bg-purple-500/10 text-purple-400 border-purple-500/20';
    case 'network': return 'bg-green-500/10 text-green-400 border-green-500/20';
    case 'cluster': return 'bg-orange-500/10 text-orange-400 border-orange-500/20';
    case 'event': return 'bg-pink-500/10 text-pink-400 border-pink-500/20';
    default: return 'bg-gray-500/10 text-gray-400 border-gray-500/20';
  }
};

export function TypedOperationsBrowser({ connectorId, connectorType }: TypedOperationsBrowserProps) { // NOSONAR (cognitive complexity)
  const [searchQuery, setSearchQuery] = useState('');
  const [categoryFilter, setCategoryFilter] = useState<string>('');
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(50);
  const [selectedOperation, setSelectedOperation] = useState<ConnectorOperation | null>(null);
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const { data: operations, isLoading, error } = useQuery({
    queryKey: ['connector-operations', connectorId, connectorType],
    queryFn: () => apiClient.listConnectorOperations(connectorId, { limit: 500 }),
  });

  // Toggle mutation
  const toggleMutation = useMutation({
    mutationFn: (opId: string) => apiClient.toggleConnectorOperation(connectorId, opId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-operations', connectorId] });
    },
  });

  // Override mutation (creates instance override from type-level op)
  const overrideMutation = useMutation({
    mutationFn: ({ opId, overrides }: { opId: string; overrides: { description?: string } }) =>
      apiClient.overrideConnectorOperation(connectorId, opId, overrides),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-operations', connectorId] });
    },
  });

  // Reset mutation (reverts custom override back to type-level)
  const resetMutation = useMutation({
    mutationFn: (opId: string) => apiClient.resetConnectorOperationOverride(connectorId, opId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connector-operations', connectorId] });
    },
  });

  // Theme colors based on connector type
  const getConnectorTheme = (type: string) => {
    switch (type) {
      case 'proxmox': return { color: 'orange', label: 'Proxmox' };
      case 'gcp': return { color: 'sky', label: 'GCP' };
      case 'kubernetes': return { color: 'blue', label: 'Kubernetes' };
      case 'prometheus': return { color: 'red', label: 'Prometheus' };
      case 'loki': return { color: 'amber', label: 'Loki' };
      case 'tempo': return { color: 'cyan', label: 'Tempo' };
      case 'alertmanager': return { color: 'rose', label: 'Alertmanager' };
      case 'jira': return { color: 'blue', label: 'Jira' };
      case 'confluence': return { color: 'blue', label: 'Confluence' };
      case 'email': return { color: 'green', label: 'Email' };
      case 'argocd': return { color: 'orange', label: 'ArgoCD' };
      case 'github': return { color: 'violet', label: 'GitHub' };
      case 'vmware':
      default: return { color: 'emerald', label: 'VMware' };
    }
  };
  const { color: themeColor, label: connectorLabel } = getConnectorTheme(connectorType);

  // Get unique categories for filter dropdown
  const categories = operations
    ? [...new Set(operations.map(op => op.category).filter(Boolean))]
    : [];

  const filteredOperations = operations?.filter((op) => {
    if (categoryFilter && op.category !== categoryFilter) return false;
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      op.name.toLowerCase().includes(query) ||
      op.operation_id.toLowerCase().includes(query) ||
      op.description?.toLowerCase().includes(query) ||
      op.category?.toLowerCase().includes(query)
    );
  }) || [];

  // Pagination calculations
  const totalPages = Math.ceil(filteredOperations.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedOperations = filteredOperations.slice(startIndex, endIndex);

  // Reset to page 1 when search/filter changes
  const handleSearch = (value: string) => {
    setSearchQuery(value);
    setCurrentPage(1);
  };

  const handleCategoryChange = (value: string) => {
    setCategoryFilter(value);
    setCurrentPage(1);
  };

  // Action handlers
  const handleToggle = (e: React.MouseEvent, op: ConnectorOperation) => {
    e.stopPropagation();
    if (!op.id) return;
    toggleMutation.mutate(op.id);
  };

  const handleOverride = (e: React.MouseEvent, op: ConnectorOperation) => {
    e.stopPropagation();
    if (!op.id) return;
    overrideMutation.mutate({ opId: op.id, overrides: { description: op.description } });
  };

  const handleReset = (e: React.MouseEvent, op: ConnectorOperation) => {
    e.stopPropagation();
    if (!op.id) return;
    resetMutation.mutate(op.id);
  };

  if (isLoading) {
    const spinnerColor = themeColor === 'orange' ? 'text-orange-400' : themeColor === 'sky' ? 'text-sky-400' : themeColor === 'blue' ? 'text-blue-400' : themeColor === 'red' ? 'text-red-400' : themeColor === 'amber' ? 'text-amber-400' : themeColor === 'cyan' ? 'text-cyan-400' : themeColor === 'rose' ? 'text-rose-400' : 'text-emerald-400';
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className={`h-8 w-8 animate-spin ${spinnerColor}`} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl text-red-400">
        <p>Failed to load {connectorLabel} operations: {(error as Error).message}</p>
      </div>
    );
  }

  if (!operations || operations.length === 0) {
    return (
      <div className="text-center py-12">
        <Server className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
        <h3 className="text-lg font-medium text-white mb-2">No operations registered</h3>
        <p className="text-text-secondary mb-4">
          This {connectorLabel} connector has no operations. Try recreating the connector.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with search, category filter, and items per page */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <h3 className="text-lg font-medium text-white">
          {connectorLabel} Operations ({filteredOperations.length}{operations.length !== filteredOperations.length ? ` of ${operations.length}` : ''})
        </h3>
        <div className="flex items-center gap-4">
          {/* Category filter */}
          <select
            value={categoryFilter}
            onChange={(e) => handleCategoryChange(e.target.value)}
            className="px-3 py-2 bg-surface border border-white/10 rounded-xl text-white text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
          >
            <option value="">All Categories</option>
            {categories.map(cat => (
              <option key={cat} value={cat}>{cat}</option>
            ))}
          </select>
          {/* Items per page */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-text-secondary flex items-center gap-2">
              Show:
              <select
                value={itemsPerPage}
                onChange={(e) => {
                  setItemsPerPage(Number(e.target.value));
                  setCurrentPage(1);
                }}
                className="px-3 py-1.5 bg-surface border border-white/10 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
              >
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={250}>250</option>
              </select>
            </label>
          </div>
          {/* Search */}
          <div className="relative w-64">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => handleSearch(e.target.value)}
              placeholder="Search operations..."
              className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
            />
            <Cpu className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
          </div>
        </div>
      </div>

      {/* Operations list */}
      <div className="space-y-3">
        {paginatedOperations.map((op, idx) => {
          const isDisabled = op.is_enabled === false;

          return (
            <motion.div
              key={`${op.id || op.operation_id}-${idx}`}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(idx * 0.02, 0.5) }}
              onClick={() => setSelectedOperation(op)}
              className={clsx(
                'p-4 bg-white/5 border border-white/10 rounded-xl hover:border-emerald-500/30 hover:bg-white/[0.07] transition-colors cursor-pointer group',
                isDisabled && 'opacity-50'
              )}
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 flex-wrap">
                    <h4 className={clsx('font-medium text-white', isDisabled && 'line-through')}>{op.name}</h4>
                    {op.category && (
                      <span className={clsx(
                        "flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border",
                        getCategoryColor(op.category)
                      )}>
                        {getCategoryIcon(op.category)}
                        {op.category}
                      </span>
                    )}
                    {/* Operation inheritance badge */}
                    {op.source && (
                      <OperationBadge
                        source={op.source}
                        connectorType={connectorType}
                        isDisabled={isDisabled}
                      />
                    )}
                  </div>
                  <p className="text-sm text-text-tertiary mt-1 font-mono">
                    {op.operation_id}
                  </p>
                  {op.description && (
                    <p className="text-sm text-text-secondary mt-2 line-clamp-2">
                      {op.description}
                    </p>
                  )}
                  {op.parameters && op.parameters.length > 0 && (
                    <div className="flex items-center gap-2 mt-2">
                      <span className="text-xs text-text-tertiary">
                        {op.parameters.length} parameter{op.parameters.length !== 1 ? 's' : ''}
                      </span>
                      <span className="text-xs text-text-tertiary">&#x2022;</span>
                      <span className="text-xs text-text-tertiary truncate max-w-xs">
                        {op.parameters.slice(0, 3).map(p => p.name).join(', ')}
                        {op.parameters.length > 3 && `, +${op.parameters.length - 3} more`}
                      </span>
                    </div>
                  )}
                </div>

                {/* Action buttons */}
                <div className="flex items-center gap-1 ml-3 flex-shrink-0">
                  {op.source === 'type' && op.id && (
                    <>
                      <button
                        onClick={(e) => handleOverride(e, op)}
                        disabled={overrideMutation.isPending}
                        className="p-1.5 text-text-tertiary hover:text-blue-400 hover:bg-blue-500/10 rounded-lg transition-colors opacity-0 group-hover:opacity-100 disabled:opacity-50"
                        title="Create instance override"
                      >
                        <Copy className="w-3.5 h-3.5" />
                      </button>
                      <button
                        onClick={(e) => handleToggle(e, op)}
                        disabled={toggleMutation.isPending}
                        className={clsx(
                          'p-1.5 rounded-lg transition-colors opacity-0 group-hover:opacity-100 disabled:opacity-50',
                          isDisabled
                            ? 'text-green-400 hover:bg-green-500/10'
                            : 'text-text-tertiary hover:text-red-400 hover:bg-red-500/10'
                        )}
                        title={isDisabled ? 'Enable operation' : 'Disable operation'}
                      >
                        <Power className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                  {op.source === 'custom' && op.id && (
                    <>
                      <button
                        onClick={(e) => handleReset(e, op)}
                        disabled={resetMutation.isPending}
                        className="p-1.5 text-text-tertiary hover:text-amber-400 hover:bg-amber-500/10 rounded-lg transition-colors opacity-0 group-hover:opacity-100 disabled:opacity-50"
                        title="Reset to type-level default"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                      </button>
                      <button
                        onClick={(e) => handleToggle(e, op)}
                        disabled={toggleMutation.isPending}
                        className={clsx(
                          'p-1.5 rounded-lg transition-colors opacity-0 group-hover:opacity-100 disabled:opacity-50',
                          isDisabled
                            ? 'text-green-400 hover:bg-green-500/10'
                            : 'text-text-tertiary hover:text-red-400 hover:bg-red-500/10'
                        )}
                        title={isDisabled ? 'Enable operation' : 'Disable operation'}
                      >
                        <Power className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                  <ChevronRight className="h-5 w-5 text-text-tertiary group-hover:text-emerald-400 transition-colors flex-shrink-0" />
                </div>
              </div>
            </motion.div>
          );
        })}
      </div>

      {/* Pagination controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-4 border-t border-white/10">
          <p className="text-sm text-text-secondary">
            Showing {startIndex + 1}-{Math.min(endIndex, filteredOperations.length)} of {filteredOperations.length} operations
          </p>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCurrentPage(1)}
              disabled={currentPage === 1}
              className="px-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-white text-sm hover:bg-white/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              First
            </button>
            <button
              onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
              disabled={currentPage === 1}
              className="px-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-white text-sm hover:bg-white/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Prev
            </button>
            <span className="px-4 py-1.5 text-white text-sm">
              Page {currentPage} of {totalPages}
            </span>
            <button
              onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
              disabled={currentPage === totalPages}
              className="px-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-white text-sm hover:bg-white/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
            <button
              onClick={() => setCurrentPage(totalPages)}
              disabled={currentPage === totalPages}
              className="px-3 py-1.5 bg-white/5 border border-white/10 rounded-lg text-white text-sm hover:bg-white/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Last
            </button>
          </div>
        </div>
      )}

      {/* Operation Detail Modal */}
      <AnimatePresence>
        {selectedOperation && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setSelectedOperation(null)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              className="relative w-full max-w-2xl max-h-[80vh] overflow-hidden glass rounded-2xl border border-white/10 shadow-2xl flex flex-col"
            >
              {/* Header */}
              <div className="flex items-start justify-between p-6 border-b border-white/10">
                <div>
                  <div className="flex items-center gap-3 flex-wrap">
                    <Server className="h-5 w-5 text-emerald-400" />
                    <h3 className="text-lg font-bold text-white">{selectedOperation.name}</h3>
                    {selectedOperation.category && (
                      <span className={clsx(
                        "flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border",
                        getCategoryColor(selectedOperation.category)
                      )}>
                        {selectedOperation.category}
                      </span>
                    )}
                    {selectedOperation.source && (
                      <OperationBadge
                        source={selectedOperation.source}
                        connectorType={connectorType}
                        isDisabled={selectedOperation.is_enabled === false}
                      />
                    )}
                  </div>
                  <p className="text-sm text-text-secondary mt-1 font-mono">
                    {selectedOperation.operation_id}
                  </p>
                  {selectedOperation.description && (
                    <p className="text-sm text-text-secondary mt-2">
                      {selectedOperation.description}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => setSelectedOperation(null)}
                  className="p-2 hover:bg-white/10 rounded-xl transition-colors text-text-secondary hover:text-white"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>

              {/* Parameters list */}
              <div className="flex-1 overflow-y-auto p-6">
                <h4 className="text-sm font-medium text-text-secondary mb-4 uppercase tracking-wider">
                  Parameters ({selectedOperation.parameters?.length || 0})
                </h4>

                {!selectedOperation.parameters || selectedOperation.parameters.length === 0 ? (
                  <p className="text-text-tertiary text-sm">No parameters required</p>
                ) : (
                  <div className="space-y-2">
                    {selectedOperation.parameters.map((param, idx) => (
                      <div
                        key={`${param.name}-${idx}`}
                        className="flex items-start gap-3 p-3 bg-white/5 rounded-lg border border-white/5"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-sm text-white">{param.name}</span>
                            {param.required && (
                              <span className="text-xs text-amber-400">required</span>
                            )}
                          </div>
                          <div className="flex items-center gap-2 mt-1">
                            <span className="text-xs font-mono text-emerald-400">
                              {param.type}
                            </span>
                          </div>
                          {param.description && (
                            <p className="text-xs text-text-tertiary mt-1">{param.description}</p>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Example */}
                {selectedOperation.example && (
                  <div className="mt-6">
                    <h4 className="text-sm font-medium text-text-secondary mb-2 uppercase tracking-wider">
                      Example
                    </h4>
                    <pre className="p-3 bg-black/30 rounded-lg text-sm text-emerald-300 font-mono overflow-x-auto">
                      {selectedOperation.example}
                    </pre>
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="p-4 border-t border-white/10 bg-white/5">
                <button
                  onClick={() => setSelectedOperation(null)}
                  className="w-full px-4 py-2 bg-white/10 hover:bg-white/15 border border-white/10 rounded-xl text-white transition-colors"
                >
                  Close
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}
