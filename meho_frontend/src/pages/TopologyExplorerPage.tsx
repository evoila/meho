// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Topology Explorer Page (Phase 76: Complete table-first overhaul)
 *
 * Tab-based layout replacing the graph-only view:
 * - Entities tab: TopologyEntityTable + optional graph toggle + EntityDetailsPanel sidebar
 * - Connector Map tab: ConnectorRelationshipList with full CRUD
 * - Suggestions tab: SuggestionsPanel with approve/reject cards
 *
 * Decisions: D-14 (graph UI inadequate), D-15 (table primary), D-16 (capabilities).
 */

import { useState, useMemo, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Network, RefreshCw, Trash2, AlertCircle, Eye, EyeOff } from 'lucide-react';
import { motion } from 'motion/react';
import { ReactFlowProvider } from '@xyflow/react';

import { TopologyGraph } from '../components/topology/TopologyGraph';
import { TopologyFilters } from '../components/topology/TopologyFilters';
import { TopologyEntityTable } from '../components/topology/TopologyEntityTable';
import { EntityDetailsPanel } from '../components/topology/EntityDetailsPanel';
import { TopologyTabNav, type TopologyTab } from '../components/topology/TopologyTabNav';
import { ConnectorRelationshipList } from '../components/topology/ConnectorRelationshipList';
import { SuggestionsPanel } from '../components/topology/SuggestionsPanel';
import {
  fetchTopologyGraph,
  invalidateTopologyEntity,
  deleteStaleEntities,
  deleteTopologyEntity,
  fetchSuggestions,
  fetchConnectorRelationships,
  approveSuggestion,
  rejectSuggestion,
  type TopologyEntity,
  type TopologyGraphResponse,
} from '../lib/topologyApi';
import { getAPIClient } from '../lib/api-client';

export function TopologyExplorerPage() {
  // Tab navigation
  const [activeTab, setActiveTab] = useState<TopologyTab>('entities');

  // Entity state
  const [selectedEntity, setSelectedEntity] = useState<TopologyEntity | null>(null);
  const [search, setSearch] = useState('');
  const [selectedConnectors, setSelectedConnectors] = useState<string[]>([]);
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [showStale, setShowStale] = useState(false);
  const [showGraph, setShowGraph] = useState(false);

  const queryClient = useQueryClient();

  // Fetch topology graph
  const {
    data: graphData,
    isLoading,
    error,
    refetch,
  } = useQuery<TopologyGraphResponse>({
    queryKey: ['topology', 'graph', showStale],
    queryFn: () => fetchTopologyGraph({ include_stale: showStale }),
    refetchInterval: 30000,
    retry: (failureCount, err) => {
      if (err instanceof Error && err.message.includes('Unauthorized')) {
        return false;
      }
      return failureCount < 3;
    },
  });

  // Fetch connectors for names
  const { data: connectors } = useQuery({
    queryKey: ['connectors'],
    queryFn: async () => {
      const client = getAPIClient();
      return client.listConnectors();
    },
  });

  // Build connector name lookup
  const connectorNames = useMemo(() => {
    if (!connectors) return {};
    return Object.fromEntries(connectors.map((c) => [c.id, c.name]));
  }, [connectors]);

  // Invalidate entity mutation
  const invalidateMutation = useMutation({
    mutationFn: ({ entityName, reason }: { entityName: string; reason: string }) =>
      invalidateTopologyEntity(entityName, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      setSelectedEntity(null);
    },
  });

  // Delete entity mutation
  const deleteEntityMutation = useMutation({
    mutationFn: (entityId: string) => deleteTopologyEntity(entityId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      setSelectedEntity(null);
    },
  });

  // Delete stale entities mutation
  const deleteStalesMutation = useMutation({
    mutationFn: (olderThanDays: number) => deleteStaleEntities(olderThanDays),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
    },
  });

  // Approve suggestion mutation (for entity detail panel inline actions)
  const approveFromDetailMutation = useMutation({
    mutationFn: approveSuggestion,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
    },
  });

  // Reject suggestion mutation (for entity detail panel inline actions)
  const rejectFromDetailMutation = useMutation({
    mutationFn: rejectSuggestion,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
    },
  });

  // Compute available types from graph data
  const availableTypes = useMemo(() => {
    if (!graphData) return [];
    return Array.from(new Set(graphData.nodes.map((n) => n.entity_type)))
      .filter(Boolean)
      .sort();
  }, [graphData]);

  // Get available connectors for filters
  const availableConnectors = useMemo(() => {
    if (!graphData || !connectorNames) return [];
    const connectorIds = new Set(
      graphData.nodes
        .filter((n) => n.connector_id)
        .map((n) => n.connector_id as string),
    );
    return Array.from(connectorIds).map((id) => ({
      id,
      name: connectorNames[id] || id.slice(0, 8),
    }));
  }, [graphData, connectorNames]);

  // Handle entity selection
  const handleSelectEntity = useCallback((entity: TopologyEntity) => {
    setSelectedEntity(entity);
  }, []);

  // Handle invalidate
  const handleInvalidate = useCallback(
    (entityName: string) => {
      if (confirm(`Mark "${entityName}" as stale? It will be re-discovered on next investigation.`)) {
        invalidateMutation.mutate({ entityName, reason: 'Manual invalidation from UI' });
      }
    },
    [invalidateMutation],
  );

  // Handle delete entity
  const handleDeleteEntity = useCallback(
    (entityId: string) => {
      deleteEntityMutation.mutate(entityId);
    },
    [deleteEntityMutation],
  );

  // Handle delete stale
  const handleDeleteStale = useCallback(() => {
    if (confirm('Delete all stale entities older than 7 days? This cannot be undone.')) {
      deleteStalesMutation.mutate(7);
    }
  }, [deleteStalesMutation]);

  // Count stale entities
  const staleCount = useMemo(() => {
    if (!graphData) return 0;
    return graphData.nodes.filter((n) => n.stale_at).length;
  }, [graphData]);

  // Fetch pending suggestions count
  const { data: suggestionsData } = useQuery({
    queryKey: ['topology', 'suggestions'],
    queryFn: () => fetchSuggestions({ limit: 100 }),
    refetchInterval: 60000,
  });
  const pendingSuggestionsCount = suggestionsData?.total ?? 0;

  // Fetch connector relationships count
  const { data: connectorRelationshipsData } = useQuery({
    queryKey: ['topology', 'connector-relationships'],
    queryFn: fetchConnectorRelationships,
    refetchInterval: 30000,
  });
  const connectorRelationshipCount = connectorRelationshipsData?.total ?? 0;

  // Filter pending suggestions for the selected entity (for entity detail panel)
  const entitySuggestions = useMemo(() => {
    if (!selectedEntity || !suggestionsData?.suggestions) return [];
    return suggestionsData.suggestions.filter(
      (s) =>
        s.entity_a_id === selectedEntity.id ||
        s.entity_b_id === selectedEntity.id,
    );
  }, [selectedEntity, suggestionsData]);

  // Tab counts
  const entityCount = graphData?.nodes.length ?? 0;

  return (
    <div className="flex flex-col h-screen bg-[--color-background] relative overflow-hidden">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-[--color-primary]/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-[--color-secondary]/5 rounded-full blur-[100px]" />
      </div>

      <div className="flex-1 flex flex-col z-10">
        {/* Header */}
        <div className="px-6 py-4 border-b border-[--color-border] flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Network className="w-6 h-6 text-[--color-primary]" />
            <div>
              <h1 className="text-xl font-semibold text-[--color-text-primary]">
                Topology Explorer
              </h1>
              <p className="text-sm text-[--color-text-secondary]">
                Browse and manage your infrastructure topology
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Stale count badge */}
            {staleCount > 0 && (
              <span className="px-2 py-1 text-xs bg-red-900/30 text-red-400 border border-red-500/30 rounded">
                {staleCount} stale
              </span>
            )}

            {/* Delete stale button */}
            {staleCount > 0 && showStale && (
              <button
                onClick={handleDeleteStale}
                disabled={deleteStalesMutation.isPending}
                className="flex items-center gap-2 px-3 py-2 text-sm text-red-400 border border-red-500/50 rounded-lg hover:bg-red-900/30 transition-colors disabled:opacity-50"
              >
                <Trash2 className="w-4 h-4" />
                Clean up
              </button>
            )}

            {/* Refresh button */}
            <button
              onClick={() => refetch()}
              className="flex items-center gap-2 px-3 py-2 text-sm text-[--color-text-secondary] border border-[--color-border] rounded-lg hover:bg-[--color-surface-hover] transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="px-6">
          <TopologyTabNav
            activeTab={activeTab}
            onTabChange={setActiveTab}
            entityCount={entityCount}
            relationshipCount={connectorRelationshipCount}
            suggestionCount={pendingSuggestionsCount}
          />
        </div>

        {/* Filter Bar (only on Entities tab) */}
        {activeTab === 'entities' && (
          <TopologyFilters
            search={search}
            onSearchChange={setSearch}
            selectedConnectors={selectedConnectors}
            onConnectorsChange={setSelectedConnectors}
            selectedTypes={selectedTypes}
            onTypesChange={setSelectedTypes}
            showStale={showStale}
            onShowStaleChange={setShowStale}
            availableConnectors={availableConnectors}
            availableTypes={availableTypes}
          />
        )}

        {/* Main Content */}
        <div className="flex-1 flex overflow-hidden">
          {/* Entities Tab */}
          {activeTab === 'entities' && (
            <>
              <motion.div
                className="flex-1 flex flex-col overflow-hidden"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.2 }}
              >
                {error ? (
                  <div className="flex items-center justify-center h-full">
                    <div className="text-center text-red-400">
                      <AlertCircle className="w-12 h-12 mx-auto mb-4" />
                      <div className="text-lg font-medium">
                        Failed to load topology data
                      </div>
                      <div className="text-sm mt-2 text-[--color-text-secondary]">
                        {error instanceof Error
                          ? error.message
                          : 'Check your network connection and try again.'}
                      </div>
                      <button
                        onClick={() => refetch()}
                        className="mt-4 px-4 py-2 bg-[--color-primary] text-white rounded-lg hover:bg-[--color-primary-hover]"
                      >
                        Retry
                      </button>
                    </div>
                  </div>
                ) : isLoading ? (
                  <div className="flex items-center justify-center h-full">
                    <div className="text-center text-[--color-text-secondary]">
                      <RefreshCw className="w-8 h-8 mx-auto mb-4 animate-spin" />
                      <div>Loading topology...</div>
                    </div>
                  </div>
                ) : graphData ? (
                  <div className="flex-1 flex flex-col overflow-hidden">
                    {/* Entity Table */}
                    <TopologyEntityTable
                      entities={graphData.nodes}
                      relationships={graphData.relationships}
                      sameAs={graphData.same_as}
                      selectedEntityId={selectedEntity?.id}
                      onSelectEntity={handleSelectEntity}
                      searchTerm={search}
                      connectorFilter={selectedConnectors}
                      typeFilter={selectedTypes}
                      showStale={showStale}
                    />

                    {/* Show Graph Toggle */}
                    <div className="px-4 py-2 border-t border-[--color-border] bg-[--color-surface]">
                      <button
                        onClick={() => setShowGraph(!showGraph)}
                        className="flex items-center gap-2 px-3 py-1.5 text-xs text-[--color-text-secondary] border border-[--color-border] rounded-lg hover:bg-[--color-surface-hover] transition-colors"
                      >
                        {showGraph ? (
                          <EyeOff className="w-3.5 h-3.5" />
                        ) : (
                          <Eye className="w-3.5 h-3.5" />
                        )}
                        {showGraph ? 'Hide Graph' : 'Show Graph'}
                      </button>
                    </div>

                    {/* Optional Graph Visualization */}
                    {showGraph && (
                      <div className="h-[300px] border-t border-[--color-border] overflow-hidden">
                        <ReactFlowProvider>
                          <TopologyGraph
                            entities={graphData.nodes}
                            relationships={graphData.relationships}
                            sameAs={graphData.same_as}
                            pendingSuggestions={suggestionsData?.suggestions}
                            onNodeClick={handleSelectEntity}
                            selectedEntityId={selectedEntity?.id}
                            connectorNames={connectorNames}
                            layoutMode="hierarchical"
                          />
                        </ReactFlowProvider>
                      </div>
                    )}
                  </div>
                ) : null}
              </motion.div>

              {/* Entity Details Panel (sidebar) */}
              {selectedEntity && graphData && (
                <motion.div
                  initial={{ x: 320, opacity: 0 }}
                  animate={{ x: 0, opacity: 1 }}
                  exit={{ x: 320, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                >
                  <EntityDetailsPanel
                    entity={selectedEntity}
                    relationships={graphData.relationships}
                    sameAs={graphData.same_as}
                    allEntities={graphData.nodes}
                    connectorNames={connectorNames}
                    onClose={() => setSelectedEntity(null)}
                    onInvalidate={handleInvalidate}
                    onSelectEntity={handleSelectEntity}
                    onDeleteEntity={handleDeleteEntity}
                    pendingSuggestions={entitySuggestions}
                    onApproveSuggestion={(id) => approveFromDetailMutation.mutate(id)}
                    onRejectSuggestion={(id) => rejectFromDetailMutation.mutate(id)}
                  />
                </motion.div>
              )}
            </>
          )}

          {/* Connector Map Tab */}
          {activeTab === 'connector-map' && (
            <ConnectorRelationshipList />
          )}

          {/* Suggestions Tab */}
          {activeTab === 'suggestions' && (
            <SuggestionsPanel />
          )}
        </div>
      </div>
    </div>
  );
}
