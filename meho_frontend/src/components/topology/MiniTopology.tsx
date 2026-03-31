// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * MiniTopology - Compact topology view for the chat agent pane
 *
 * Renders a small, non-interactive ReactFlow instance showing ONLY
 * the investigation path nodes and their edges. Embedded at the
 * bottom of AgentPane as a collapsible section.
 *
 * Phase 61 Plan 03: Investigation path spatial context in chat.
 */

import { useMemo, useState, useEffect, useRef, useCallback, createElement } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  type Node,
  type Edge,
  type NodeTypes,
  type NodeProps,
} from '@xyflow/react';
import { useQuery } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, Map as MapIcon } from 'lucide-react';
import '@xyflow/react/dist/style.css';

import { useInvestigationPath } from './useInvestigationPath';
import { applyInvestigationStyles } from './InvestigationOverlay';
import { computeElkLayout } from './useElkLayout';
import { getIconForEntity } from './tierMapping';
import { CONNECTOR_COLORS } from './ConnectorIcon';
import { fetchTopologyGraph, type TopologyEntity } from '../../lib/topologyApi';
import { useChatStore } from '../../features/chat/stores';

// ============================================================================
// Mini Entity Node
// ============================================================================

interface MiniEntityData extends Record<string, unknown> {
  name: string;
  entityType: string;
  connectorType?: string;
  _investigationStatus?: 'current' | 'visited';
}

type MiniEntityNodeType = Node<MiniEntityData, 'miniEntity'>;

function MiniEntityNodeComponent({ data }: NodeProps<MiniEntityNodeType>) {
  const borderColor = data.connectorType
    ? CONNECTOR_COLORS[data.connectorType] || '#6B7280'
    : '#6B7280';

  const icon = createElement(getIconForEntity(data.entityType), {
    className: 'w-4 h-4 flex-shrink-0',
  });

  const isRipple = data._investigationStatus === 'current';
  const isVisited = data._investigationStatus === 'visited';

  return (
    <div
      className={`
        flex items-center gap-2 px-2.5 py-1.5 rounded-md border
        bg-gray-900/90 text-xs min-w-[120px] max-w-[160px]
        ${isRipple ? 'ring-2 ring-emerald-400 border-emerald-400/60' : ''}
        ${isVisited ? 'ring-1 ring-emerald-400/40 border-gray-700 opacity-80' : ''}
        ${!isRipple && !isVisited ? 'border-gray-700' : ''}
      `}
      style={{
        borderLeftColor: borderColor,
        borderLeftWidth: '3px',
        ...(isRipple ? { animation: 'investigation-ripple 1.5s infinite' } : {}),
      }}
    >
      <span style={{ color: borderColor }}>{icon}</span>
      <span className="text-white truncate font-medium">{data.name}</span>
    </div>
  );
}

const miniNodeTypes: NodeTypes = {
  miniEntity: MiniEntityNodeComponent,
};

// ============================================================================
// Async layout hook for mini topology
// ============================================================================

/**
 * Computes elk layout for mini topology path nodes asynchronously.
 * Uses ref-based cancellation to avoid setState after unmount.
 */
function useMiniLayout(
  pathNodes: { entityId: string; entityName: string; status: 'current' | 'visited' }[],
  pathEdges: { from: string; to: string }[],
  entityMap: Map<string, TopologyEntity>,
  pathIsActive: boolean,
) {
  const [result, setResult] = useState<{ nodes: Node[]; edges: Edge[] }>({
    nodes: [],
    edges: [],
  });
  const cancelRef = useRef(false);
  const prevKeyRef = useRef('');

  // Stable serialization key to detect changes
  const pathKey = useMemo(
    () => pathNodes.map((n) => `${n.entityId}:${n.status}`).join(','),
    [pathNodes],
  );

  const computeLayout = useCallback(async () => {
    if (pathNodes.length === 0) {
      setResult({ nodes: [], edges: [] });
      return;
    }

    cancelRef.current = false;

    // Build mini-sized nodes
    const nodes: Node[] = pathNodes.map((pn, idx) => {
      const entity = entityMap.get(pn.entityId);
      return {
        id: pn.entityId,
        type: 'miniEntity',
        position: { x: idx * 180, y: 0 },
        data: {
          name: entity?.name ?? pn.entityName,
          entityType: entity?.entity_type ?? 'Entity',
          connectorType: entity?.connector_type,
          _investigationStatus: pn.status,
        } as MiniEntityData,
      };
    });

    const edges: Edge[] = pathEdges.map((pe, idx) => ({
      id: `mini-edge-${idx}`,
      source: pe.from,
      target: pe.to,
      type: 'smoothstep',
      animated: true,
      style: {
        stroke: '#34D399',
        strokeWidth: 2,
      },
    }));

    try {
      const layoutResult = await computeElkLayout(nodes, edges, 'hierarchical');
      if (!cancelRef.current) {
        const styled = applyInvestigationStyles(layoutResult.nodes, layoutResult.edges, {
          nodes: pathNodes.map((pn, i) => ({
            entityId: pn.entityId,
            entityName: pn.entityName,
            connectorId: '',
            status: pn.status,
            stepIndex: i,
          })),
          edges: pathEdges,
          isActive: pathIsActive,
          currentNodeId: pathNodes.find((n) => n.status === 'current')?.entityId ?? null,
        });
        setResult({ nodes: styled.nodes, edges: styled.edges });
      }
    } catch {
      if (!cancelRef.current) {
        setResult({ nodes, edges });
      }
    }
  }, [pathNodes, pathEdges, entityMap, pathIsActive]);

  // Trigger layout when path changes
  useEffect(() => {
    if (pathKey === prevKeyRef.current) return;
    prevKeyRef.current = pathKey;

    // eslint-disable-next-line react-hooks/set-state-in-effect -- Async layout computation pattern (elkjs)
    computeLayout();

    return () => {
      cancelRef.current = true;
    };
  }, [pathKey, computeLayout]);

  return result;
}

// ============================================================================
// MiniTopology Component
// ============================================================================

export function MiniTopology() {
  const investigationStartTime = useChatStore((s) => s.investigationStartTime);
  const isActive = investigationStartTime !== null;

  // Fetch topology entities (reuses same query key as TopologyExplorerPage)
  const { data: graphData } = useQuery({
    queryKey: ['topology', 'graph', false],
    queryFn: () => fetchTopologyGraph({ include_stale: false }),
    refetchInterval: 30000,
    enabled: isActive,
  });

  // Get investigation path
  const { path } = useInvestigationPath(graphData?.nodes ?? []);

  // Build a topology entity lookup for connector_type
  const entityMap = useMemo(() => {
    const map = new Map<string, TopologyEntity>();
    for (const entity of graphData?.nodes ?? []) {
      map.set(entity.id, entity);
    }
    return map;
  }, [graphData?.nodes]);

  // Compute layout asynchronously
  const { nodes: layoutedNodes, edges: layoutedEdges } = useMiniLayout(
    path.nodes,
    path.edges,
    entityMap,
    path.isActive,
  );

  return (
    <MiniTopologyInner
      nodes={layoutedNodes}
      edges={layoutedEdges}
      hasPath={path.nodes.length > 0}
      isActive={isActive}
    />
  );
}

// ============================================================================
// Inner component (needs its own ReactFlowProvider)
// ============================================================================

interface MiniTopologyInnerProps {
  nodes: Node[];
  edges: Edge[];
  hasPath: boolean;
  isActive: boolean;
}

function MiniTopologyInner({ nodes, edges, hasPath, isActive }: MiniTopologyInnerProps) {
  // Track user's explicit collapse preference; null = no preference (auto-expand)
  const [userCollapsed, setUserCollapsed] = useState<boolean | null>(null);
  // Auto-expand during active investigation unless user explicitly collapsed
  const collapsed = userCollapsed ?? !isActive;

  return (
    <div className="border-t border-gray-700">
      {/* Header with collapse toggle */}
      <button
        onClick={() => setUserCollapsed((prev) => prev === null ? true : !prev)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-gray-400 hover:text-gray-200 transition-colors"
      >
        <div className="flex items-center gap-1.5">
          <MapIcon className="w-3.5 h-3.5" />
          Investigation Path
        </div>
        {collapsed ? (
          <ChevronDown className="w-3.5 h-3.5" />
        ) : (
          <ChevronUp className="w-3.5 h-3.5" />
        )}
      </button>

      {/* Content */}
      {!collapsed && (
        <div className="h-[200px] px-2 pb-2">
          {hasPath ? (
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={miniNodeTypes}
                fitView
                fitViewOptions={{ padding: 0.3 }}
                panOnDrag={false}
                zoomOnScroll={false}
                zoomOnPinch={false}
                zoomOnDoubleClick={false}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable={false}
                proOptions={{ hideAttribution: true }}
                className="rounded-md bg-gray-900/50"
              />
            </ReactFlowProvider>
          ) : (
            <div className="flex items-center justify-center h-full text-xs text-gray-500">
              No active investigation
            </div>
          )}
        </div>
      )}
    </div>
  );
}
