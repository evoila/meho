// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connector List Component
 * 
 * Shows all connectors for the tenant with create/edit actions
 */
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Search, Plug, AlertCircle, ChevronRight, Shield, ShieldAlert, ShieldCheck, FileCode, Globe, Server, Download, Upload } from 'lucide-react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import { CredentialHealthBadge } from './CredentialHealthBadge';
import { CreateConnectorModal } from './CreateConnectorModal';
import { ExportConnectorsModal } from './ExportConnectorsModal';
import { ImportConnectorsModal } from './ImportConnectorsModal';
import type { Connector, ConnectorHealth } from '../../lib/api-client';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';

interface ConnectorListProps {
  onSelectConnector: (connectorId: string) => void;
}

export function ConnectorList({ onSelectConnector }: Readonly<ConnectorListProps>) {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showExportModal, setShowExportModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Fetch connectors
  const { data: connectors, isLoading, error } = useQuery({
    queryKey: ['connectors'],
    queryFn: () => apiClient.listConnectors(),
  });

  // Fetch connector health (poll every 60s)
  const { data: healthData } = useQuery({
    queryKey: ['connectors-health'],
    queryFn: () => apiClient.getConnectorsHealth(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  // Build lookup map for health status
  const healthMap = new Map(
    healthData?.map(h => [h.connector_id, h]) ?? []
  );

  // Filter connectors by search
  const filteredConnectors = connectors?.filter((connector) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      connector.name.toLowerCase().includes(query) ||
      connector.base_url.toLowerCase().includes(query) ||
      connector.description?.toLowerCase().includes(query)
    );
  }) || [];

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white tracking-tight" data-testid="connectors-page-title">API Connectors</h1>
          <p className="text-text-secondary mt-1">
            Manage external API connections and endpoints
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setShowExportModal(true)}
            data-testid="export-connectors-button"
            className="flex items-center gap-2 px-4 py-2.5 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all"
          >
            <Download className="h-5 w-5" />
            Export
          </button>
          <button
            onClick={() => setShowImportModal(true)}
            data-testid="import-connectors-button"
            className="flex items-center gap-2 px-4 py-2.5 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all"
          >
            <Upload className="h-5 w-5" />
            Import
          </button>
          <button
            onClick={() => setShowCreateModal(true)}
            data-testid="new-connector-button"
            className="flex items-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] text-white rounded-xl font-medium transition-all"
          >
            <Plus className="h-5 w-5" />
            New Connector
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="relative max-w-md">
        <Search className="absolute left-4 top-1/2 -translate-y-1/2 h-5 w-5 text-text-tertiary" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search connectors..."
          data-testid="connectors-search-input"
          className="w-full pl-12 pr-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
        />
      </div>

      {/* Loading State */}
      {isLoading && (
        <div className="text-center py-12">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary mx-auto mb-4"></div>
          <p className="text-text-secondary">Loading connectors...</p>
        </div>
      )}

      {/* Error State */}
      {error && (
        <div className="flex items-center gap-2 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
          <AlertCircle className="h-5 w-5" />
          <span>Failed to load connectors: {(error as Error).message}</span>
        </div>
      )}

      {/* Empty State */}
      {!isLoading && !error && filteredConnectors.length === 0 && !searchQuery && (
        <div className="text-center py-16 bg-surface border border-white/10 rounded-2xl">
          <div className="p-4 bg-white/5 rounded-full w-fit mx-auto mb-6">
            <Plug className="h-12 w-12 text-text-tertiary" />
          </div>
          <h3 className="text-xl font-medium text-white mb-2">No connectors yet</h3>
          <p className="text-text-secondary mb-8 max-w-md mx-auto">
            Configure your first connector to let MEHO investigate live infrastructure. Connect Kubernetes, VMware, GCP, Prometheus, and more.
          </p>
          <button
            onClick={() => setShowCreateModal(true)}
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary hover:bg-primary-hover text-white rounded-xl font-medium transition-all"
          >
            <Plus className="h-5 w-5" />
            Create Connector
          </button>
        </div>
      )}

      {/* No search results */}
      {!isLoading && !error && filteredConnectors.length === 0 && searchQuery && (
        <div className="text-center py-12 bg-surface border border-white/10 rounded-2xl">
          <Search className="h-12 w-12 text-text-tertiary mx-auto mb-4" />
          <p className="text-text-secondary">No connectors match your search</p>
        </div>
      )}

      {/* Connector Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <AnimatePresence mode="popLayout">
          {filteredConnectors.map((connector) => (
            <ConnectorCard
              key={connector.id}
              connector={connector}
              health={healthMap.get(connector.id)}
              onClick={() => onSelectConnector(connector.id)}
            />
          ))}
        </AnimatePresence>
      </div>

      {/* Create Modal */}
      <AnimatePresence>
        {showCreateModal && (
          <CreateConnectorModal
            onClose={() => setShowCreateModal(false)}
            onSuccess={(connector) => {
              setShowCreateModal(false);
              queryClient.invalidateQueries({ queryKey: ['connectors'] });
              onSelectConnector(connector.id);
            }}
          />
        )}
      </AnimatePresence>

      {/* Export Modal */}
      <AnimatePresence>
        {showExportModal && (
          <ExportConnectorsModal
            onClose={() => setShowExportModal(false)}
            onSuccess={() => setShowExportModal(false)}
          />
        )}
      </AnimatePresence>

      {/* Import Modal */}
      <AnimatePresence>
        {showImportModal && (
          <ImportConnectorsModal
            onClose={() => setShowImportModal(false)}
            onSuccess={() => {
              setShowImportModal(false);
              queryClient.invalidateQueries({ queryKey: ['connectors'] });
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

interface ConnectorCardProps {
  connector: Connector;
  health?: ConnectorHealth;
  onClick: () => void;
}

function ConnectorCard({ connector, health, onClick }: Readonly<ConnectorCardProps>) {
  const hasBlockedMethods = connector.blocked_methods.length > 0;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.95 }}
      onClick={onClick}
      data-testid={`connector-card-${connector.id}`}
      className="glass rounded-2xl p-6 cursor-pointer hover:border-primary/30 hover:shadow-lg hover:shadow-primary/5 transition-all group relative overflow-hidden"
    >
      <div className="absolute inset-0 bg-gradient-to-br from-white/5 to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />

      <div className="relative z-10">
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-4">
            <div className="relative p-3 bg-primary/10 rounded-xl text-primary group-hover:scale-110 transition-transform duration-300">
              <Plug className="h-6 w-6" />
              {health && (
                <span
                  className={clsx(
                    "absolute -top-1 -right-1 w-3 h-3 rounded-full border-2 border-surface",
                    health.status === 'reachable' ? "bg-green-400" : "bg-red-400"
                  )}
                  title={
                    health.status === 'reachable'
                      ? `Reachable (${health.latency_ms}ms)`
                      : `Unreachable: ${health.error || 'Connection failed'}`
                  }
                />
              )}
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="font-semibold text-white truncate text-lg">{connector.name}</h3>
              <p className="text-sm text-text-secondary truncate">{connector.base_url}</p>
            </div>
          </div>
          <div className="p-2 rounded-lg hover:bg-white/5 text-text-tertiary hover:text-white transition-colors">
            <ChevronRight className="h-5 w-5" />
          </div>
        </div>

        {connector.description && (
          <p className="text-sm text-text-secondary mb-6 line-clamp-2 h-10 leading-relaxed">
            {connector.description}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2 text-xs">
          {/* Connector type badge */}
          {(() => {
            const connectorType = connector.connector_type || 'rest';
            const typeBadgeColor = ({
              vmware: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
              soap: "bg-amber-500/10 text-amber-400 border-amber-500/20",
              graphql: "bg-pink-500/10 text-pink-400 border-pink-500/20",
              grpc: "bg-blue-500/10 text-blue-400 border-blue-500/20",
            } as Record<string, string>)[connectorType] ?? "bg-sky-500/10 text-sky-400 border-sky-500/20";
            const typeIconMap: Record<string, React.ReactNode> = {
              vmware: <Server className="h-3 w-3" />,
              soap: <FileCode className="h-3 w-3" />,
            };
            const typeIcon = typeIconMap[connectorType] ?? <Globe className="h-3 w-3" />;
            return (
              <span className={clsx(
                "flex items-center gap-1.5 px-2.5 py-1 rounded-lg font-medium border",
                typeBadgeColor
              )}>
                {typeIcon}
                {connectorType.toUpperCase()}
              </span>
            );
          })()}

          <span className={clsx(
            "px-2.5 py-1 rounded-lg font-medium border",
            connector.is_active
              ? "bg-green-400/10 text-green-400 border-green-400/20"
              : "bg-white/5 text-text-secondary border-white/10"
          )}>
            {connector.is_active ? 'Active' : 'Inactive'}
          </span>

          <span className="px-2.5 py-1 bg-white/5 text-text-secondary border border-white/10 rounded-lg">
            {connector.auth_type}
          </span>

          {hasBlockedMethods && (
            <span className="px-2.5 py-1 bg-orange-500/10 text-orange-400 border border-orange-500/20 rounded-lg" title={`Blocked: ${connector.blocked_methods.join(', ')}`}>
              {connector.blocked_methods.length} blocked
            </span>
          )}

          {(() => {
            const safetyColorMap: Record<string, string> = {
              safe: "bg-green-400/10 text-green-400 border-green-400/20",
              caution: "bg-amber-500/10 text-amber-400 border-amber-500/20",
            };
            const safetyColor = safetyColorMap[connector.default_safety_level] ?? "bg-red-500/10 text-red-400 border-red-500/20";
            const safetyIconMap: Record<string, React.ReactNode> = {
              safe: <ShieldCheck className="h-3 w-3" />,
              caution: <Shield className="h-3 w-3" />,
            };
            const safetyIcon = safetyIconMap[connector.default_safety_level] ?? <ShieldAlert className="h-3 w-3" />;
            return (
              <span className={clsx("flex items-center gap-1 px-2.5 py-1 rounded-lg border", safetyColor)}>
                {safetyIcon}
                <span className="capitalize">{connector.default_safety_level}</span>
              </span>
            );
          })()}
        </div>

        <div className="mt-4 pt-4 border-t border-white/5 text-xs text-text-tertiary flex justify-between items-center">
          <span>Created {new Date(connector.created_at).toLocaleDateString()}</span>
          <CredentialHealthBadge
            health={null}
            updatedAt={connector.updated_at}
          />
        </div>
      </div>
    </motion.div>
  );
}

