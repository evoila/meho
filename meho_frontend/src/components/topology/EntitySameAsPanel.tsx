// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EntitySameAsPanel - Expandable SAME_AS section with confidence badges
 *
 * For each SAME_AS relationship where entity is entity_a or entity_b:
 * shows OTHER entity name as clickable link, entity type and connector name,
 * ConfidenceBadge with verified_via, FreshnessLabel with last_verified_at
 * or discovered_at. Collapsible section header "SAME_AS Identities (N)".
 *
 * Phase 76 Plan 05: Enhanced entity detail panels.
 */

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { clsx } from 'clsx';
import type { TopologyEntity, TopologySameAs } from '../../lib/topologyApi';
import { ConfidenceBadge } from './ConfidenceBadge';
import { FreshnessLabel } from './FreshnessLabel';
import { ConnectorIcon, CONNECTOR_COLORS } from './ConnectorIcon';

interface EntitySameAsPanelProps {
  entity: TopologyEntity;
  sameAs: TopologySameAs[];
  allEntities: TopologyEntity[];
  connectorNames: Record<string, string>;
  onSelectEntity?: (entity: TopologyEntity) => void;
  defaultExpanded?: boolean;
}

export function EntitySameAsPanel({
  entity,
  sameAs,
  allEntities,
  connectorNames,
  onSelectEntity,
  defaultExpanded = true,
}: EntitySameAsPanelProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  // Filter SAME_AS involving this entity
  const entitySameAs = sameAs.filter(
    (s) => s.entity_a_id === entity.id || s.entity_b_id === entity.id
  );

  if (entitySameAs.length === 0) return null;

  return (
    <div>
      {/* Collapsible header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 w-full text-left mb-2"
      >
        {isExpanded ? (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronRight className="w-4 h-4 text-gray-400" />
        )}
        <span className="text-xs text-gray-400 uppercase tracking-wide">
          SAME_AS Identities ({entitySameAs.length})
        </span>
      </button>

      {/* Content */}
      {isExpanded && (
        <div className="space-y-2">
          {entitySameAs.map((same) => {
            const otherId =
              same.entity_a_id === entity.id
                ? same.entity_b_id
                : same.entity_a_id;
            const otherEntity = allEntities.find((e) => e.id === otherId);
            const otherConnectorType =
              otherEntity?.connector_type?.toLowerCase() || 'rest';
            const borderColor =
              CONNECTOR_COLORS[otherConnectorType] || CONNECTOR_COLORS.rest;

            return (
              <div
                key={same.id}
                className={clsx(
                  'bg-gray-800 border border-gray-700 rounded-lg p-3 transition-colors',
                  onSelectEntity && otherEntity
                    ? 'cursor-pointer hover:bg-gray-700/50'
                    : '',
                )}
                style={{
                  borderLeftWidth: '4px',
                  borderLeftColor: borderColor,
                }}
                onClick={() => {
                  if (otherEntity && onSelectEntity) {
                    onSelectEntity(otherEntity);
                  }
                }}
                role={onSelectEntity && otherEntity ? 'button' : undefined}
                tabIndex={onSelectEntity && otherEntity ? 0 : undefined}
                onKeyDown={(e) => {
                  if (
                    (e.key === 'Enter' || e.key === ' ') &&
                    otherEntity &&
                    onSelectEntity
                  ) {
                    e.preventDefault();
                    onSelectEntity(otherEntity);
                  }
                }}
              >
                {/* Header: icon + SAME_AS badge */}
                <div className="flex items-center gap-2 mb-1">
                  {otherEntity?.connector_type && (
                    <ConnectorIcon
                      connectorType={otherEntity.connector_type}
                      size={16}
                    />
                  )}
                  <span className="text-amber-400 font-medium text-xs">
                    SAME_AS
                  </span>
                  <ConfidenceBadge
                    verifiedVia={same.verified_via}
                    similarityScore={same.similarity_score}
                  />
                </div>

                {/* Entity name */}
                <div
                  className="text-sm font-semibold text-white truncate"
                  title={otherEntity?.name}
                >
                  {otherEntity?.name || otherId.slice(0, 8)}
                </div>

                {/* Type and connector */}
                <div className="text-xs text-gray-400 mt-1">
                  {otherEntity?.entity_type || 'Unknown'}
                  {otherEntity?.connector_id &&
                  connectorNames[otherEntity.connector_id]
                    ? ` \u2014 ${connectorNames[otherEntity.connector_id]}`
                    : ''}
                </div>

                {/* Freshness */}
                <div className="flex items-center gap-3 mt-1">
                  <FreshnessLabel
                    timestamp={
                      same.last_verified_at || same.discovered_at
                    }
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
