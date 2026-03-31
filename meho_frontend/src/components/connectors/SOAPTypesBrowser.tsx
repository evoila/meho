// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Boxes, Loader2, X, ChevronRight } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient, type SOAPTypeDefinition } from '../../lib/api-client';
import { config } from '../../lib/config';

interface SOAPTypesBrowserProps {
  connectorId: string;
}

export function SOAPTypesBrowser({ connectorId }: SOAPTypesBrowserProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(50);
  const [selectedType, setSelectedType] = useState<SOAPTypeDefinition | null>(null);
  const apiClient = getAPIClient(config.apiURL);

  const { data: types, isLoading, error } = useQuery({
    queryKey: ['soap-types', connectorId],
    queryFn: () => apiClient.listSOAPTypes(connectorId, { limit: 500 }),
  });

  const filteredTypes = types?.filter((t) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      t.type_name.toLowerCase().includes(query) ||
      t.namespace?.toLowerCase().includes(query) ||
      t.base_type?.toLowerCase().includes(query) ||
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
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-xl text-red-400">
        <p>Failed to load SOAP types: {(error as Error).message}</p>
        <p className="text-sm mt-2 opacity-70">
          Make sure the WSDL has been ingested. Go to the "WSDL" tab to ingest it.
        </p>
      </div>
    );
  }

  if (!types || types.length === 0) {
    return (
      <div className="text-center py-12">
        <Boxes className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
        <h3 className="text-lg font-medium text-white mb-2">No type definitions discovered</h3>
        <p className="text-text-secondary mb-4">
          Ingest a WSDL file to discover available SOAP type definitions.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with search and items per page */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <h3 className="text-lg font-medium text-white">
          SOAP Types ({filteredTypes.length}{types.length !== filteredTypes.length ? ` of ${types.length}` : ''})
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
                className="px-3 py-1.5 bg-surface border border-white/10 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              >
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
                <option value={250}>250</option>
                <option value={500}>500</option>
              </select>
            </label>
          </div>
          <div className="relative w-64">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => handleSearch(e.target.value)}
              placeholder="Search types..."
              className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
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
            className="p-4 bg-white/5 border border-white/10 rounded-xl hover:border-purple-500/30 hover:bg-white/[0.07] transition-colors cursor-pointer group"
          >
            <div className="flex items-start justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <h4 className="font-medium text-white truncate">{type.type_name}</h4>
                  {type.base_type && (
                    <span className="text-xs text-text-tertiary">
                      extends <span className="text-purple-400">{type.base_type}</span>
                    </span>
                  )}
                </div>
                <p className="text-sm text-text-secondary mt-1">
                  {type.namespace || 'No namespace'}
                </p>
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
              <ChevronRight className="h-5 w-5 text-text-tertiary group-hover:text-purple-400 transition-colors flex-shrink-0" />
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
                    <Boxes className="h-5 w-5 text-purple-400" />
                    <h3 className="text-lg font-bold text-white">{selectedType.type_name}</h3>
                  </div>
                  {selectedType.namespace && (
                    <p className="text-sm text-text-secondary mt-1 font-mono">
                      {selectedType.namespace}
                    </p>
                  )}
                  {selectedType.base_type && (
                    <p className="text-sm text-text-secondary mt-1">
                      Extends: <span className="text-purple-400">{selectedType.base_type}</span>
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
                            {prop.is_required && (
                              <span className="text-xs text-amber-400">required</span>
                            )}
                          </div>
                          <div className="flex items-center gap-2 mt-1">
                            <span className="text-xs font-mono text-purple-400">
                              {prop.type_name}{prop.is_array && '[]'}
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

