// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ConnectorRelationshipList - List of connector relationships with CRUD
 *
 * Uses useQuery to fetch from fetchConnectorRelationships API.
 * Uses useMutation for create/update/delete with queryClient.invalidateQueries.
 * Layout: "Add Relationship" button at top, each row shows
 * source-name --type--> target-name with edit (Pencil) and delete (Trash2) icons.
 * Delete triggers confirmation dialog. Empty state per UI-SPEC.
 *
 * Phase 76 Plan 05: Connector Map tab components.
 */

import { useState, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Pencil, Trash2, ArrowRight, RefreshCw, Network } from 'lucide-react';
import { toast } from 'sonner';

import { ConnectorRelationshipForm } from './ConnectorRelationshipForm';
import {
  fetchConnectorRelationships,
  createConnectorRelationship,
  updateConnectorRelationship,
  deleteConnectorRelationship,
  type ConnectorRelationship,
  type ConnectorRelationshipCreateRequest,
} from '../../lib/topologyApi';
import { getAPIClient } from '../../lib/api-client';

const RELATIONSHIP_LABELS: Record<string, string> = {
  monitors: 'monitors',
  logs_for: 'logs for',
  traces_for: 'traces for',
  deploys_to: 'deploys to',
  manages: 'manages',
  alerts_for: 'alerts for',
  tracks_issues_for: 'tracks issues for',
};

export function ConnectorRelationshipList() {
  const [showForm, setShowForm] = useState(false);
  const [editingRelationship, setEditingRelationship] = useState<ConnectorRelationship | null>(null);

  const queryClient = useQueryClient();

  // Fetch connector relationships
  const {
    data: relationshipsData,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['topology', 'connector-relationships'],
    queryFn: fetchConnectorRelationships,
    refetchInterval: 30000,
  });

  // Fetch connectors for dropdown
  const { data: connectors } = useQuery({
    queryKey: ['connectors'],
    queryFn: async () => {
      const client = getAPIClient();
      return client.listConnectors();
    },
  });

  const connectorList = (connectors ?? []).map((c) => ({ id: c.id, name: c.name }));

  // Create mutation
  const createMutation = useMutation({
    mutationFn: createConnectorRelationship,
    onSuccess: (rel) => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      toast.success(`Relationship created: ${rel.from_connector_name} ${RELATIONSHIP_LABELS[rel.relationship_type] ?? rel.relationship_type} ${rel.to_connector_name}`);
      setShowForm(false);
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : 'Failed to create relationship');
    },
  });

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ConnectorRelationshipCreateRequest }) =>
      updateConnectorRelationship(id, data),
    onSuccess: (rel) => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      toast.success(`Relationship updated: ${rel.from_connector_name} ${RELATIONSHIP_LABELS[rel.relationship_type] ?? rel.relationship_type} ${rel.to_connector_name}`);
      setEditingRelationship(null);
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : 'Failed to update relationship');
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: deleteConnectorRelationship,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['topology'] });
      toast.success('Relationship deleted');
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : 'Failed to delete relationship');
    },
  });

  const handleCreate = useCallback(
    (data: ConnectorRelationshipCreateRequest) => {
      createMutation.mutate(data);
    },
    [createMutation],
  );

  const handleUpdate = useCallback(
    (data: ConnectorRelationshipCreateRequest) => {
      if (!editingRelationship) return;
      updateMutation.mutate({ id: editingRelationship.id, data });
    },
    [editingRelationship, updateMutation],
  );

  const handleDelete = useCallback(
    (rel: ConnectorRelationship) => {
      const confirmed = confirm(
        `Remove this connector relationship? The link between ${rel.from_connector_name} and ${rel.to_connector_name} will be deleted.`
      );
      if (confirmed) {
        deleteMutation.mutate(rel.id);
      }
    },
    [deleteMutation],
  );

  const handleEdit = useCallback((rel: ConnectorRelationship) => {
    setEditingRelationship(rel);
    setShowForm(false); // Close create form if open
  }, []);

  const handleCancelCreate = useCallback(() => {
    setShowForm(false);
  }, []);

  const handleCancelEdit = useCallback(() => {
    setEditingRelationship(null);
  }, []);

  const relationships = relationshipsData?.relationships ?? [];

  // Loading state
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center text-[--color-text-secondary]">
          <RefreshCw className="w-8 h-8 mx-auto mb-4 animate-spin" />
          <div>Loading connector relationships...</div>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center text-red-400">
          <div className="text-lg font-medium">Failed to load relationships</div>
          <div className="text-sm mt-2">{error instanceof Error ? error.message : 'Unknown error'}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-6">
      {/* Header with Add button */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-[--color-text-secondary] uppercase tracking-wide">
          Connector Relationships
        </h2>
        {!showForm && !editingRelationship && (
          <button
            onClick={() => setShowForm(true)}
            className="flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg bg-[--color-primary] text-white hover:bg-[--color-primary-hover] transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Relationship
          </button>
        )}
      </div>

      {/* Create form */}
      {showForm && (
        <div className="mb-4">
          <ConnectorRelationshipForm
            connectors={connectorList}
            onSubmit={handleCreate}
            onCancel={handleCancelCreate}
            isSubmitting={createMutation.isPending}
          />
        </div>
      )}

      {/* Edit form */}
      {editingRelationship && (
        <div className="mb-4">
          <ConnectorRelationshipForm
            connectors={connectorList}
            onSubmit={handleUpdate}
            onCancel={handleCancelEdit}
            initialData={{
              from_connector_id: editingRelationship.from_connector_id,
              to_connector_id: editingRelationship.to_connector_id,
              relationship_type: editingRelationship.relationship_type,
            }}
            isSubmitting={updateMutation.isPending}
          />
        </div>
      )}

      {/* Empty state */}
      {relationships.length === 0 && !showForm ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center max-w-md">
            <Network className="w-12 h-12 text-[--color-text-tertiary] mx-auto mb-4" />
            <h3 className="text-base font-semibold text-[--color-text-primary] mb-2">
              No connector relationships defined
            </h3>
            <p className="text-sm text-[--color-text-secondary]">
              Define how your connectors relate to each other. For example, which Prometheus
              instance monitors which Kubernetes cluster. This helps MEHO traverse systems
              during investigations.
            </p>
          </div>
        </div>
      ) : (
        /* Relationship list */
        <div className="flex-1 overflow-y-auto space-y-2">
          {relationships.map((rel) => (
            <div
              key={rel.id}
              className="flex items-center justify-between p-3 bg-[--color-surface] rounded-lg border border-[--color-border] hover:border-[--color-border-hover] transition-colors"
            >
              {/* Relationship display */}
              <div className="flex items-center gap-3 text-sm min-w-0">
                <span className="font-semibold text-[--color-text-primary] truncate">
                  {rel.from_connector_name}
                </span>
                <span className="flex items-center gap-1 text-[--color-text-secondary] whitespace-nowrap">
                  <ArrowRight className="w-3.5 h-3.5" />
                  <span className="text-[--color-primary] font-medium">
                    {RELATIONSHIP_LABELS[rel.relationship_type] ?? rel.relationship_type}
                  </span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </span>
                <span className="font-semibold text-[--color-text-primary] truncate">
                  {rel.to_connector_name}
                </span>
              </div>

              {/* Actions */}
              <div className="flex items-center gap-1 ml-3 flex-shrink-0">
                <button
                  onClick={() => handleEdit(rel)}
                  title="Edit relationship"
                  aria-label="Edit relationship"
                  className="p-1.5 text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-surface-hover] rounded transition-colors"
                >
                  <Pencil className="w-4 h-4" />
                </button>
                <button
                  onClick={() => handleDelete(rel)}
                  title="Delete relationship"
                  aria-label="Delete relationship"
                  disabled={deleteMutation.isPending}
                  className="p-1.5 text-[--color-text-secondary] hover:text-red-400 hover:bg-red-900/20 rounded transition-colors disabled:opacity-50"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
