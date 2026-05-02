// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EntityNode - Custom React Flow node for topology entities
 *
 * Redesigned for Phase 61:
 * - Primary: Large lucide-react entity-type icon (from ENTITY_TYPE_MAP)
 * - Secondary: Small ConnectorIcon SVG badge (16px, top-right)
 * - Health badge: Only for degraded (amber) / critical (red) -- no badge for healthy/unknown
 * - Entity name + type/scope subtext
 */

import { memo, useMemo, createElement } from 'react';
import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import { clsx } from 'clsx';
import { ConnectorIcon, CONNECTOR_COLORS } from './ConnectorIcon';
import { getIconForEntity } from './tierMapping';

// ============================================================================
// Types
// ============================================================================

export interface HealthStatus {
  status: 'degraded' | 'critical';
  timestamp: string;
}

export interface EntityNodeData extends Record<string, unknown> {
  id: string;
  name: string;
  connectorId: string | null;
  connectorName?: string;
  connectorType?: string;
  description?: string;
  isStale: boolean;
  discoveredAt: string;
  lastVerifiedAt?: string | null;
  rawAttributes?: Record<string, unknown> | null;
  // Phase 61: New fields
  entityType: string;
  scope?: Record<string, unknown> | null;
  health?: HealthStatus | null;
  // Search highlight support (Plan 02)
  _highlighted?: boolean;
  // Investigation path status (Plan 03)
  _investigationStatus?: 'current' | 'visited';
}

export type EntityNodeType = Node<EntityNodeData, 'entity'>;

// ============================================================================
// Health Badge
// ============================================================================

const HEALTH_COLORS = {
  degraded: 'bg-amber-500',
  critical: 'bg-red-500',
} as const;

function HealthBadge({ health }: Readonly<{ health: HealthStatus | null | undefined }>) {
  if (!health) return null;

  return (
    <div
      className={`absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full ${HEALTH_COLORS[health.status]} border-2 border-gray-900`}
      title={`${health.status} since ${new Date(health.timestamp).toLocaleString()}`}
    />
  );
}

// ============================================================================
// Entity Node Component
// ============================================================================

function EntityNodeComponent({ data, selected }: NodeProps<EntityNodeType>) {
  // Get connector border color
  const effectiveConnectorType = data.connectorType ?? null;
  const borderColor = effectiveConnectorType
    ? CONNECTOR_COLORS[effectiveConnectorType] || '#6B7280'
    : '#6B7280';

  // Build scope string for display (e.g. "namespace: production")
  const scopeText = useMemo(() => {
    if (!data.scope || typeof data.scope !== 'object') return null;
    const entries = Object.entries(data.scope);
    if (entries.length === 0) return null;
    // Show the most relevant scope field (namespace, project, datacenter, etc.)
    const [key, value] = entries[0];
    return `${key}: ${String(value)}`;
  }, [data.scope]);

  // Entity type label
  const entityTypeLabel = data.entityType || 'Entity';

  // Render entity-type icon via createElement to avoid lint static-components rule
  const entityIcon = createElement(getIconForEntity(data.entityType), { className: 'w-6 h-6' });

  // Investigation path visual effects
  const investigationStyle = useMemo(() => {
    if (data._investigationStatus === 'current') {
      return {
        className: 'ring-2 ring-emerald-400',
        style: { animation: 'investigation-ripple 1.5s infinite' },
      };
    }
    if (data._investigationStatus === 'visited') {
      return {
        className: 'ring-1 ring-emerald-400/40 opacity-80',
        style: {},
      };
    }
    return { className: '', style: {} };
  }, [data._investigationStatus]);

  return (
    <div
      className={clsx(
        'relative px-4 py-3 rounded-lg border-2 shadow-lg min-w-[200px] max-w-[220px]',
        'transition-all duration-200 cursor-pointer',
        'bg-gray-900/90 backdrop-blur-sm',
        selected ? 'border-blue-400 ring-2 ring-blue-400/30' : 'border-gray-700',
        data.isStale && 'border-red-500/50 opacity-60',
        data._highlighted === true && 'ring-2 ring-blue-400/50 shadow-blue-500/20 shadow-lg',
        investigationStyle.className,
      )}
      style={{
        borderLeftColor: borderColor,
        borderLeftWidth: '4px',
        ...investigationStyle.style,
      }}
    >
      {/* Input handle (top) */}
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-gray-500 !w-3 !h-3 !border-2 !border-gray-800"
      />

      {/* Connector badge - small 16px icon in absolute top-right */}
      {effectiveConnectorType && (
        <div className="absolute top-1.5 right-1.5" title={data.connectorName || effectiveConnectorType}>
          <ConnectorIcon connectorType={effectiveConnectorType} size={16} />
        </div>
      )}

      {/* Health badge - only for degraded/critical */}
      <HealthBadge health={data.health} />

      {/* Main content: icon + text */}
      <div className="flex items-start gap-3 pr-5">
        {/* Entity-type icon (primary, large) */}
        <div
          className="flex-shrink-0 mt-0.5"
          style={{ color: borderColor }}
        >
          {entityIcon}
        </div>

        {/* Text content */}
        <div className="min-w-0 flex-1">
          {/* Entity name */}
          <div className="text-sm font-semibold text-white truncate" title={data.name}>
            {data.name}
          </div>

          {/* Entity type + scope */}
          <div className="flex items-center gap-1 mt-0.5">
            <span className="text-xs text-gray-400">{entityTypeLabel}</span>
            {scopeText && (
              <>
                <span className="text-xs text-gray-600">|</span>
                <span className="text-xs text-gray-500 truncate" title={scopeText}>
                  {scopeText}
                </span>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stale indicator */}
      {data.isStale && (
        <div className="mt-1.5">
          <span className="text-[10px] text-red-400 font-medium uppercase tracking-wide">Stale</span>
        </div>
      )}

      {/* Output handle (bottom) */}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-gray-500 !w-3 !h-3 !border-2 !border-gray-800"
      />
    </div>
  );
}

export const EntityNode = memo(EntityNodeComponent);
