// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EntityDetailsPanel - Enhanced entity detail sidebar (Phase 76 Plan 05)
 *
 * Shows entity details with:
 * - FreshnessLabel for entity timestamps in header
 * - Relationships grouped by type with FreshnessLabel per relationship
 * - EntitySameAsPanel for SAME_AS section with ConfidenceBadge
 * - Pending suggestions with inline approve/reject
 * - "Delete Entity" button for stale entities with confirmation
 * - Raw attributes section
 */

import { useCallback } from 'react';
import { X, AlertTriangle, Link, ArrowRight, Check } from 'lucide-react';
import { X as XIcon } from 'lucide-react';
import type {
  TopologyEntity,
  TopologyRelationship,
  TopologySameAs,
  SameAsSuggestion,
} from '../../lib/topologyApi';
import { ConnectorIcon, CONNECTOR_COLORS } from './ConnectorIcon';
import { FreshnessLabel } from './FreshnessLabel';
import { EntitySameAsPanel } from './EntitySameAsPanel';

const NODE_STYLE = { icon: '\uD83D\uDCCD', color: '#6B7280', label: 'Entity' };

interface EntityDetailsPanelProps {
  entity: TopologyEntity;
  relationships: TopologyRelationship[];
  sameAs: TopologySameAs[];
  allEntities: TopologyEntity[];
  connectorNames: Record<string, string>;
  onClose: () => void;
  onInvalidate?: (entityName: string) => void;
  onSelectEntity?: (entity: TopologyEntity) => void;
  onDeleteEntity?: (entityId: string) => void;
  pendingSuggestions?: SameAsSuggestion[];
  onApproveSuggestion?: (id: string) => void;
  onRejectSuggestion?: (id: string) => void;
}

function getEntityName(entityId: string, entities: TopologyEntity[]): string {
  const entity = entities.find((e) => e.id === entityId);
  return entity?.name || entityId.slice(0, 8);
}

function formatMatchType(matchType: string): string {
  return matchType
    .replaceAll('_', ' ')
    .replaceAll(/\b\w/g, (c) => c.toUpperCase());
}

function getConfidenceColor(confidence: number): string {
  if (confidence >= 0.9) return 'text-green-400';
  if (confidence >= 0.7) return 'text-yellow-400';
  return 'text-orange-400';
}

export function EntityDetailsPanel({
  entity,
  relationships,
  sameAs,
  allEntities,
  connectorNames,
  onClose,
  onInvalidate,
  onSelectEntity,
  onDeleteEntity,
  pendingSuggestions,
  onApproveSuggestion,
  onRejectSuggestion,
}: Readonly<EntityDetailsPanelProps>) {
  // Get relationships involving this entity
  const outgoingRels = relationships.filter(
    (r) => r.from_entity_id === entity.id,
  );
  const incomingRels = relationships.filter(
    (r) => r.to_entity_id === entity.id,
  );

  // Group relationships by type
  const groupedOutgoing = outgoingRels.reduce<
    Record<string, TopologyRelationship[]>
  >((acc, rel) => {
    const type = rel.relationship_type.replaceAll('_', ' ');
    if (!acc[type]) acc[type] = [];
    acc[type].push(rel);
    return acc;
  }, {});

  const groupedIncoming = incomingRels.reduce<
    Record<string, TopologyRelationship[]>
  >((acc, rel) => {
    const type = rel.relationship_type.replaceAll('_', ' ');
    if (!acc[type]) acc[type] = [];
    acc[type].push(rel);
    return acc;
  }, {});

  const handleDeleteEntity = useCallback(() => {
    if (onDeleteEntity) {
      const confirmed = confirm(
        `Permanently delete ${entity.name}? This cannot be undone.`,
      );
      if (confirmed) {
        onDeleteEntity(entity.id);
      }
    }
  }, [onDeleteEntity, entity]);

  return (
    <div className="w-80 bg-gray-900 border-l border-gray-700 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-gray-700 flex items-start justify-between">
        <div className="flex items-center gap-3">
          {entity.connector_type ? (
            <ConnectorIcon connectorType={entity.connector_type} size={28} />
          ) : (
            <span className="text-2xl">{NODE_STYLE.icon}</span>
          )}
          <div>
            <div
              className="text-xs font-medium uppercase tracking-wide"
              style={{
                color: entity.connector_type
                  ? CONNECTOR_COLORS[entity.connector_type.toLowerCase()] ||
                    NODE_STYLE.color
                  : NODE_STYLE.color,
              }}
            >
              {entity.entity_type || NODE_STYLE.label}
            </div>
            <div
              className="text-white font-semibold truncate max-w-[180px]"
              title={entity.name}
            >
              {entity.name}
            </div>
            {/* Freshness in header */}
            <div className="flex items-center gap-2 mt-1">
              <FreshnessLabel
                timestamp={
                  entity.last_verified_at || entity.discovered_at
                }
              />
            </div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="p-1 text-gray-400 hover:text-white hover:bg-gray-700 rounded"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Stale Warning */}
        {entity.stale_at && (
          <div className="bg-red-900/30 border border-red-500/50 rounded-lg p-3 flex items-start gap-2">
            <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
            <div>
              <div className="text-red-400 font-medium text-sm">
                Stale Entity
              </div>
              <div className="text-red-300 text-xs mt-1">
                Marked stale{' '}
                <FreshnessLabel timestamp={entity.stale_at} />
              </div>
            </div>
          </div>
        )}

        {/* Connector */}
        {entity.connector_id && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">
              Connector
            </div>
            <div className="flex items-center gap-2">
              <Link className="w-4 h-4 text-gray-500" />
              <span className="text-sm text-white">
                {connectorNames[entity.connector_id] ||
                  entity.connector_id}
              </span>
            </div>
          </div>
        )}

        {/* Description */}
        {entity.description && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">
              Description
            </div>
            <div className="text-sm text-gray-300 bg-gray-800 rounded p-2">
              {entity.description}
            </div>
          </div>
        )}

        {/* Timestamps */}
        <div>
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">
            Timeline
          </div>
          <div className="space-y-2 text-sm">
            <div className="flex items-center justify-between text-gray-300">
              <span>Discovered</span>
              <FreshnessLabel timestamp={entity.discovered_at} />
            </div>
            {entity.last_verified_at && (
              <div className="flex items-center justify-between text-gray-300">
                <span>Last verified</span>
                <FreshnessLabel timestamp={entity.last_verified_at} />
              </div>
            )}
          </div>
        </div>

        {/* Outgoing Relationships grouped by type */}
        {outgoingRels.length > 0 && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">
              Relationships ({outgoingRels.length})
            </div>
            {Object.entries(groupedOutgoing).map(([type, rels]) => (
              <div key={type} className="mb-2">
                <div className="text-xs text-blue-400 font-medium mb-1">
                  {type}
                </div>
                <div className="space-y-1">
                  {rels.map((rel) => (
                    <div
                      key={rel.id}
                      className="flex items-center justify-between text-sm bg-gray-800 rounded p-2"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <ArrowRight className="w-3.5 h-3.5 text-gray-500 flex-shrink-0" />
                        <span className="text-white truncate">
                          {getEntityName(rel.to_entity_id, allEntities)}
                        </span>
                      </div>
                      <FreshnessLabel
                        timestamp={
                          rel.last_verified_at || rel.discovered_at
                        }
                        className="flex-shrink-0 ml-2"
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Incoming Relationships grouped by type */}
        {incomingRels.length > 0 && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">
              Referenced By ({incomingRels.length})
            </div>
            {Object.entries(groupedIncoming).map(([type, rels]) => (
              <div key={type} className="mb-2">
                <div className="text-xs text-blue-400 font-medium mb-1">
                  {type}
                </div>
                <div className="space-y-1">
                  {rels.map((rel) => (
                    <div
                      key={rel.id}
                      className="flex items-center justify-between text-sm bg-gray-800 rounded p-2"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-white truncate">
                          {getEntityName(
                            rel.from_entity_id,
                            allEntities,
                          )}
                        </span>
                      </div>
                      <FreshnessLabel
                        timestamp={
                          rel.last_verified_at || rel.discovered_at
                        }
                        className="flex-shrink-0 ml-2"
                      />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* SAME_AS Identities -- delegated to EntitySameAsPanel */}
        <EntitySameAsPanel
          entity={entity}
          sameAs={sameAs}
          allEntities={allEntities}
          connectorNames={connectorNames}
          onSelectEntity={onSelectEntity}
        />

        {/* Pending Suggestions for this entity */}
        {pendingSuggestions && pendingSuggestions.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs text-gray-400 uppercase tracking-wide">
                Pending Matches
              </span>
              <span className="px-1.5 py-0.5 text-xs font-medium bg-amber-500/20 text-amber-400 rounded-full">
                {pendingSuggestions.length}
              </span>
            </div>
            <div className="space-y-2">
              {pendingSuggestions.map((suggestion) => {
                const isEntityA =
                  suggestion.entity_a_id === entity.id;
                const otherName = isEntityA
                  ? suggestion.entity_b_name
                  : suggestion.entity_a_name;
                const confidencePercent = Math.round(
                  suggestion.confidence * 100,
                );
                const confidenceColor = getConfidenceColor(
                  suggestion.confidence,
                );

                return (
                  <div
                    key={suggestion.id}
                    className="bg-amber-900/10 border border-amber-500/20 rounded-lg p-3"
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span
                        className="text-sm font-medium text-white truncate"
                        title={otherName}
                      >
                        {otherName}
                      </span>
                      <span
                        className={`text-xs font-medium ${confidenceColor}`}
                      >
                        {confidencePercent}%
                      </span>
                    </div>
                    <div className="text-xs text-gray-400 mb-2">
                      {formatMatchType(suggestion.match_type)}
                    </div>
                    <div className="flex items-center gap-2">
                      {onApproveSuggestion && (
                        <button
                          onClick={() =>
                            onApproveSuggestion(suggestion.id)
                          }
                          className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded text-xs font-medium bg-green-600/20 text-green-400 border border-green-500/30 hover:bg-green-600/30 transition-colors"
                        >
                          <Check className="w-3 h-3" />
                          Approve
                        </button>
                      )}
                      {onRejectSuggestion && (
                        <button
                          onClick={() =>
                            onRejectSuggestion(suggestion.id)
                          }
                          className="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded text-xs font-medium bg-red-600/20 text-red-400 border border-red-500/30 hover:bg-red-600/30 transition-colors"
                        >
                          <XIcon className="w-3 h-3" />
                          Reject
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Raw Attributes */}
        {entity.raw_attributes &&
          Object.keys(entity.raw_attributes).length > 0 && (
            <div>
              <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">
                Raw Attributes
              </div>
              <pre className="text-xs text-gray-300 bg-gray-800 rounded p-2 overflow-x-auto">
                {JSON.stringify(entity.raw_attributes, null, 2)}
              </pre>
            </div>
          )}
      </div>

      {/* Footer Actions */}
      <div className="p-4 border-t border-gray-700 space-y-2">
        {onInvalidate && !entity.stale_at && (
          <button
            onClick={() => onInvalidate(entity.name)}
            className="w-full px-4 py-2 text-sm font-medium text-red-400 border border-red-500/50 rounded-lg hover:bg-red-900/30 transition-colors"
          >
            Mark as Stale
          </button>
        )}
        {onDeleteEntity && entity.stale_at && (
          <button
            onClick={handleDeleteEntity}
            className="w-full px-4 py-2 text-sm font-medium text-red-400 bg-red-900/20 border border-red-500/50 rounded-lg hover:bg-red-900/40 transition-colors"
          >
            Delete Entity
          </button>
        )}
      </div>
    </div>
  );
}
