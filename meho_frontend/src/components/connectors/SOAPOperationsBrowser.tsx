// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { FileCode, List, Loader2 } from 'lucide-react';
import { motion } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';

interface SOAPOperationsBrowserProps {
  connectorId: string;
}

export function SOAPOperationsBrowser({ connectorId }: Readonly<SOAPOperationsBrowserProps>) {
  const [searchQuery, setSearchQuery] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(50);
  const apiClient = getAPIClient(config.apiURL);

  const { data: operations, isLoading, error } = useQuery({
    queryKey: ['soap-operations', connectorId],
    queryFn: () => apiClient.listSOAPOperations(connectorId, { limit: 500 }),
  });

  const filteredOperations = operations?.filter((op) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      op.name.toLowerCase().includes(query) ||
      op.operation_name.toLowerCase().includes(query) ||
      op.service_name.toLowerCase().includes(query) ||
      op.description?.toLowerCase().includes(query)
    );
  }) || [];

  // Pagination calculations
  const totalPages = Math.ceil(filteredOperations.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedOperations = filteredOperations.slice(startIndex, endIndex);

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
        <p>Failed to load SOAP operations: {(error as Error).message}</p>
        <p className="text-sm mt-2 opacity-70">
          Make sure the WSDL has been ingested. Go to the "WSDL" tab to ingest it.
        </p>
      </div>
    );
  }

  if (!operations || operations.length === 0) {
    return (
      <div className="text-center py-12">
        <FileCode className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
        <h3 className="text-lg font-medium text-white mb-2">No operations discovered</h3>
        <p className="text-text-secondary mb-4">
          Ingest a WSDL file to discover available SOAP operations.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with search and items per page */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <h3 className="text-lg font-medium text-white">
          SOAP Operations ({filteredOperations.length}{operations.length !== filteredOperations.length ? ` of ${operations.length}` : ''})
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
                <option value={500}>All</option>
              </select>
            </label>
          </div>
          <div className="relative w-64">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => handleSearch(e.target.value)}
              placeholder="Search operations..."
              className="w-full pl-10 pr-4 py-2 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
            <List className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
          </div>
        </div>
      </div>

      {/* Operations list */}
      <div className="space-y-3">
        {paginatedOperations.map((op, idx) => (
          <motion.div
            key={`${op.service_name}-${op.operation_name}-${idx}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: Math.min(idx * 0.02, 0.5) }}
            className="p-4 bg-white/5 border border-white/10 rounded-xl hover:border-amber-500/30 transition-colors"
          >
            <div className="flex items-start justify-between">
              <div>
                <h4 className="font-medium text-white">{op.operation_name}</h4>
                <p className="text-sm text-text-secondary mt-1">
                  {op.service_name} / {op.port_name}
                </p>
                {op.description && (
                  <p className="text-sm text-text-tertiary mt-2 line-clamp-2">
                    {op.description}
                  </p>
                )}
              </div>
              {op.soap_action && (
                <span className="px-2 py-1 bg-amber-500/10 text-amber-400 text-xs rounded-lg font-mono">
                  {op.soap_action.split('/').pop()}
                </span>
              )}
            </div>
          </motion.div>
        ))}
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
    </div>
  );
}

