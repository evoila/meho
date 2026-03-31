// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Boxes, Loader2, X, ChevronRight } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient, type ConnectorEntityType } from '../../lib/api-client';
import { config } from '../../lib/config';

interface TypedTypesBrowserProps {
  connectorId: string;
  connectorType: string;
}

export function TypedTypesBrowser({ connectorId, connectorType }: TypedTypesBrowserProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(50);
  const [selectedType, setSelectedType] = useState<ConnectorEntityType | null>(null);
  const apiClient = getAPIClient(config.apiURL);

  const { data: types, isLoading, error } = useQuery({
    queryKey: ['connector-types', connectorId, connectorType],
    queryFn: () => apiClient.listConnectorTypes(connectorId, { limit: 500 }),
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

  const filteredTypes = types?.filter((t) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      t.type_name.toLowerCase().includes(query) ||
      t.category?.toLowerCase().includes(query) ||
      t.description?.toLowerCase().includes(query) ||
      t.properties.some(p => p.name.toLowerCase().includes(query))
    );
  }) || [];

  // Pagination calculations
  const totalPages = Math.ceil(filteredTypes.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedTypes = filteredTypes.slice(startIndex, endIndex);

  // Reset to page 1 when search changes
  const handleSearch = (value: string) => {
    setSearchQuery(value);
    setCurrentPage(1);
  };

  if (isLoading) {
    const spinnerColor = themeColor === 'orange' ? 'text-orange-400' : themeColor === 'sky' ? 'text-sky-400' : themeColor === 'blue' ? 'text-blue-400' : themeColor === 'red' ? 'text-red-400' : themeColor === 'amber' ? 'text-amber-400' : themeColor === 'cyan' ? 'text-cyan-400' : themeColor === 'rose' ? 'text-rose-400' : themeColor === 'green' ? 'text-green-400' : themeColor === 'violet' ? 'text-violet-400' : 'text-emerald-400';
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className={`h-8 w-8 animate-spin ${spinnerColor}`} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl text-red-400">
        <p>Failed to load {connectorLabel} types: {(error as Error).message}</p>
      </div>
    );
  }

  if (!types || types.length === 0) {
    return (
      <div className="text-center py-12">
        <Boxes className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
        <h3 className="text-lg font-medium text-white mb-2">No types registered</h3>
        <p className="text-text-secondary mb-4">
          This {connectorLabel} connector has no type definitions.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with search and items per page */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <h3 className="text-lg font-medium text-white">
          {connectorLabel} Types ({filteredTypes.length}{types.length !== filteredTypes.length ? ` of ${types.length}` : ''})
        </h3>
        <div className="flex items-center gap-4">
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
              </select>
            </label>
          </div>
          <div className="relative w-64">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => handleSearch(e.target.value)}
              placeholder="Search types..."
              className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/50"
            />
            <Boxes className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
          </div>
        </div>
      </div>

      {/* Types list */}
      <div className="space-y-3">
        {paginatedTypes.map((type, idx) => (
          <motion.div
            key={`${type.type_name}-${idx}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: Math.min(idx * 0.02, 0.5) }}
            onClick={() => setSelectedType(type)}
            className="p-4 bg-white/5 border border-white/10 rounded-xl hover:border-emerald-500/30 hover:bg-white/[0.07] transition-colors cursor-pointer group"
          >
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <h4 className="font-medium text-white truncate">{type.type_name}</h4>
                  {type.category && (
                    <span className="text-xs px-2 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-md">
                      {type.category}
                    </span>
                  )}
                </div>
                {type.description && (
                  <p className="text-sm text-text-secondary mt-1 line-clamp-2">
                    {type.description}
                  </p>
                )}
                <div className="flex items-center gap-4 mt-2">
                  <span className="text-xs text-text-tertiary">
                    {type.properties.length} {type.properties.length === 1 ? 'property' : 'properties'}
                  </span>
                  {type.properties.length > 0 && (
                    <span className="text-xs text-text-tertiary truncate max-w-md">
                      {type.properties.slice(0, 5).map(p => p.name).join(', ')}
                      {type.properties.length > 5 && `, +${type.properties.length - 5} more`}
                    </span>
                  )}
                </div>
              </div>
              <ChevronRight className="h-5 w-5 text-text-tertiary group-hover:text-emerald-400 transition-colors flex-shrink-0" />
            </div>
          </motion.div>
        ))}
      </div>

      {/* Pagination controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-4 border-t border-white/10">
          <p className="text-sm text-text-secondary">
            Showing {startIndex + 1}-{Math.min(endIndex, filteredTypes.length)} of {filteredTypes.length} types
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

      {/* Type Detail Modal */}
      <AnimatePresence>
        {selectedType && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setSelectedType(null)}
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
                  <div className="flex items-center gap-2">
                    <Boxes className="h-5 w-5 text-emerald-400" />
                    <h3 className="text-lg font-bold text-white">{selectedType.type_name}</h3>
                  </div>
                  {selectedType.category && (
                    <p className="text-sm text-emerald-400 mt-1">
                      Category: {selectedType.category}
                    </p>
                  )}
                  {selectedType.description && (
                    <p className="text-sm text-text-secondary mt-2">
                      {selectedType.description}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => setSelectedType(null)}
                  className="p-2 hover:bg-white/10 rounded-xl transition-colors text-text-secondary hover:text-white"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>

              {/* Properties list */}
              <div className="flex-1 overflow-y-auto p-6">
                <h4 className="text-sm font-medium text-text-secondary mb-4 uppercase tracking-wider">
                  Properties ({selectedType.properties.length})
                </h4>
                
                {selectedType.properties.length === 0 ? (
                  <p className="text-text-tertiary text-sm">No properties defined</p>
                ) : (
                  <div className="space-y-2">
                    {selectedType.properties.map((prop, idx) => (
                      <div
                        key={`${prop.name}-${idx}`}
                        className="flex items-start gap-3 p-3 bg-white/5 rounded-lg border border-white/5"
                      >
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-sm text-white">{prop.name}</span>
                            {prop.required && (
                              <span className="text-xs text-amber-400">required</span>
                            )}
                          </div>
                          <div className="flex items-center gap-2 mt-1">
                            <span className="text-xs font-mono text-emerald-400">
                              {prop.type}
                            </span>
                          </div>
                          {prop.description && (
                            <p className="text-xs text-text-tertiary mt-1">{prop.description}</p>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="p-4 border-t border-white/10 bg-white/5">
                <button
                  onClick={() => setSelectedType(null)}
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

