// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TopologyGraph - React Flow wrapper for topology visualization
 *
 * Phase 61: Replaced dagre with elkjs for layout computation.
 * - Supports hierarchical (swim lanes), force, and radial layouts
 * - Animated node transitions via useAnimatedLayout
 * - Swim lane tier backgrounds in hierarchical mode
 *
 * Features:
 * - elkjs layout (hierarchical/force/radial)
 * - Custom entity nodes with entity-type icons
 * - Relationship edges with labels
 * - SAME_AS edges (solid amber)
 * - Pending suggestion edges (dashed amber, animated)
 * - Pan, zoom, minimap
 * - Swim lane backgrounds (hierarchical mode)
 * - Smooth animated transitions between layouts
 */

import { useCallback, useMemo, useEffect, useState, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
  type NodeTypes,
  MarkerType,
  BackgroundVariant,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Network } from 'lucide-react';

import { EntityNode, type EntityNodeType, type EntityNodeData } from './EntityNode';
import { GroupNode, type GroupNodeType } from './GroupNode';
import { SwimLaneBackground } from './SwimLaneBackground';
import { TopologyContextMenu } from './ContextMenu';
import { applyInvestigationStyles } from './InvestigationOverlay';
import { computeElkLayout, type LayoutMode } from './useElkLayout';
import { useAnimatedLayout } from './useAnimatedLayout';
import { getTierForEntity, buildSwimLanes, type SwimLane } from './tierMapping';
import type { InvestigationPathData } from './useInvestigationPath';
import type {
  TopologyEntity,
  TopologyRelationship,
  TopologySameAs,
  SameAsSuggestion,
} from '../../lib/topologyApi';

// Register custom node types
const nodeTypes: NodeTypes = {
  entity: EntityNode,
  group: GroupNode,
};

// Group threshold - collapse if more than this many children
const GROUP_THRESHOLD = 3;

interface TopologyGraphProps {
  entities: TopologyEntity[];
  relationships: TopologyRelationship[];
  sameAs: TopologySameAs[];
  pendingSuggestions?: SameAsSuggestion[];
  onNodeClick?: (entity: TopologyEntity) => void;
  onNodeDoubleClick?: (entityId: string) => void;
  onInvestigate?: (entityName: string, entityType: string, scope?: string) => void;
  selectedEntityId?: string | null;
  connectorNames?: Record<string, string>;
  expandedGroups?: Set<string>;
  onToggleGroup?: (groupId: string) => void;
  layoutMode?: LayoutMode;
  searchTerm?: string;
  investigationPath?: InvestigationPathData;
}

// ============================================================================
// Grouping logic (preserved from original)
// ============================================================================

/**
 * Create grouped elements - collapse children under parent nodes
 */
function createGroupedElements(
  entities: TopologyEntity[],
  relationships: TopologyRelationship[],
  expandedGroups: Set<string>,
  onToggleGroup?: (groupId: string) => void,
): {
  visibleEntities: TopologyEntity[];
  groupNodes: GroupNodeType[];
  visibleRelationships: TopologyRelationship[];
  hiddenEntityIds: Set<string>;
} {
  const parentToChildren = new Map<string, TopologyEntity[]>();

  relationships.forEach((r) => {
    if (r.relationship_type === 'runs_on' || r.relationship_type === 'member_of') {
      const children = parentToChildren.get(r.to_entity_id) || [];
      const childEntity = entities.find((e) => e.id === r.from_entity_id);
      if (childEntity) {
        children.push(childEntity);
        parentToChildren.set(r.to_entity_id, children);
      }
    }
  });

  const hiddenEntityIds = new Set<string>();
  const groupNodes: GroupNodeType[] = [];

  parentToChildren.forEach((children, parentId) => {
    if (children.length > GROUP_THRESHOLD) {
      const groupId = `group-${parentId}`;
      const isExpanded = expandedGroups.has(groupId);

      if (!isExpanded) {
        groupNodes.push({
          id: groupId,
          type: 'group',
          position: { x: 0, y: 0 },
          data: {
            id: groupId,
            parentId,
            childIds: children.map((c) => c.id),
            count: children.length,
            expanded: false,
            onToggle: onToggleGroup,
          },
        });

        children.forEach((child) => hiddenEntityIds.add(child.id));
      }
    }
  });

  const visibleEntities = entities.filter((e) => !hiddenEntityIds.has(e.id));
  const visibleRelationships = relationships.filter((r) => {
    return !hiddenEntityIds.has(r.from_entity_id);
  });

  return {
    visibleEntities,
    groupNodes,
    visibleRelationships,
    hiddenEntityIds,
  };
}

// ============================================================================
// Convert topology data to React Flow nodes and edges (no layout applied)
// ============================================================================

function convertToFlowElements(
  entities: TopologyEntity[],
  relationships: TopologyRelationship[],
  sameAs: TopologySameAs[],
  connectorNames: Record<string, string>,
  pendingSuggestions?: SameAsSuggestion[],
): { nodes: EntityNodeType[]; edges: Edge[] } {
  const entityMap = new Map(entities.map((e) => [e.id, e]));

  // Convert entities to nodes (positions will be set by elkjs)
  const nodes: EntityNodeType[] = entities.map((entity) => ({
    id: entity.id,
    type: 'entity' as const,
    position: { x: 0, y: 0 },
    data: {
      id: entity.id,
      name: entity.name,
      connectorId: entity.connector_id,
      connectorName: entity.connector_id
        ? connectorNames[entity.connector_id]
        : undefined,
      connectorType: entity.connector_type,
      description: entity.description,
      isStale: !!entity.stale_at,
      discoveredAt: entity.discovered_at,
      lastVerifiedAt: entity.last_verified_at,
      rawAttributes: entity.raw_attributes,
      // Phase 61: New fields
      entityType: entity.entity_type,
      scope: entity.scope,
      health: null, // Populated later by investigation results
    },
  }));

  // Convert relationships to edges
  const relationshipEdges: Edge[] = relationships
    .filter(
      (r) => entityMap.has(r.from_entity_id) && entityMap.has(r.to_entity_id),
    )
    .map((rel) => ({
      id: rel.id,
      source: rel.from_entity_id,
      target: rel.to_entity_id,
      type: 'smoothstep',
      animated: false,
      label: rel.relationship_type.replaceAll('_', ' '),
      labelStyle: {
        fontSize: 10,
        fill: '#9CA3AF',
        fontWeight: 500,
      },
      labelBgStyle: {
        fill: '#1F2937',
        fillOpacity: 0.8,
      },
      labelBgPadding: [4, 2] as [number, number],
      style: {
        stroke: '#4B5563',
        strokeWidth: 2,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: '#4B5563',
        width: 15,
        height: 15,
      },
    }));

  // Confirmed SAME_AS edges (solid amber)
  const sameAsEdges: Edge[] = sameAs
    .filter(
      (s) => entityMap.has(s.entity_a_id) && entityMap.has(s.entity_b_id),
    )
    .map((same) => ({
      id: same.id,
      source: same.entity_a_id,
      target: same.entity_b_id,
      type: 'smoothstep',
      animated: false,
      label: 'SAME_AS',
      labelStyle: {
        fontSize: 10,
        fill: '#F59E0B',
        fontWeight: 600,
      },
      labelBgStyle: {
        fill: '#1F2937',
        fillOpacity: 0.9,
      },
      labelBgPadding: [4, 2] as [number, number],
      style: {
        stroke: '#F59E0B',
        strokeWidth: 2,
      },
    }));

  // Pending suggestion edges (dashed amber, animated)
  const pendingEdges: Edge[] = (pendingSuggestions ?? [])
    .filter(
      (s) => entityMap.has(s.entity_a_id) && entityMap.has(s.entity_b_id),
    )
    .map((s) => ({
      id: `pending-${s.id}`,
      source: s.entity_a_id,
      target: s.entity_b_id,
      type: 'smoothstep',
      animated: true,
      label: 'pending',
      labelStyle: {
        fontSize: 10,
        fill: '#F59E0B',
        fontWeight: 400,
        opacity: 0.6,
      },
      labelBgStyle: {
        fill: '#1F2937',
        fillOpacity: 0.7,
      },
      labelBgPadding: [4, 2] as [number, number],
      style: {
        stroke: '#F59E0B',
        strokeWidth: 1.5,
        strokeDasharray: '5,5',
        opacity: 0.5,
      },
    }));

  const edges = [...relationshipEdges, ...sameAsEdges, ...pendingEdges];

  return { nodes, edges };
}

// ============================================================================
// Inner component (uses useReactFlow, must be inside ReactFlowProvider context)
// ============================================================================

function TopologyGraphInner({
  entities,
  relationships,
  sameAs,
  pendingSuggestions,
  onNodeClick,
  onNodeDoubleClick,
  onInvestigate,
  selectedEntityId,
  connectorNames = {},
  layoutMode = 'hierarchical',
  searchTerm = '',
  investigationPath,
}: Readonly<TopologyGraphProps>) {
  // State for expanded groups
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // Swim lane state
  const [swimLanes, setSwimLanes] = useState<SwimLane[]>([]);
  // Context menu state
  const [contextMenu, setContextMenu] = useState<{
    nodeId: string;
    entityName: string;
    entityType: string;
    scope?: Record<string, unknown> | null;
    top?: number;
    left?: number;
    right?: number;
    bottom?: number;
  } | null>(null);
  // Track whether initial layout has been applied
  const initialLayoutDone = useRef(false);
  // Track data identity to detect real changes
  const lastDataKey = useRef<string>('');
  // Ref for the graph container
  const graphRef = useRef<HTMLDivElement>(null);
  // Auto-follow: track if user manually panned (skip auto-center until next node hop)
  const userHasPanned = useRef(false);
  // Track previous currentNodeId to detect hops
  const prevCurrentNodeId = useRef<string | null>(null);

  const { animateToPositions } = useAnimatedLayout();
  const reactFlowInstance = useReactFlow();

  // Toggle group expansion
  const handleToggleGroup = useCallback((groupId: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  }, []);

  // Build unlayouted nodes + edges from data
  const { rawNodes, rawEdges } = useMemo(() => {
    // Apply grouping
    const {
      visibleEntities,
      groupNodes,
      visibleRelationships,
      hiddenEntityIds,
    } = createGroupedElements(
      entities,
      relationships,
      expandedGroups,
      handleToggleGroup,
    );

    // Convert to flow elements (no positions yet)
    const { nodes: entityNodes, edges } = convertToFlowElements(
      visibleEntities,
      visibleRelationships,
      sameAs.filter(
        (s) =>
          !hiddenEntityIds.has(s.entity_a_id) &&
          !hiddenEntityIds.has(s.entity_b_id),
      ),
      connectorNames,
      pendingSuggestions?.filter(
        (s) =>
          !hiddenEntityIds.has(s.entity_a_id) &&
          !hiddenEntityIds.has(s.entity_b_id),
      ),
    );

    // Add group nodes
    const allNodes = [...entityNodes, ...groupNodes];

    // Group-to-parent edges
    const groupEdges: Edge[] = groupNodes.map((group) => ({
      id: `edge-${group.id}`,
      source: group.id,
      target: group.data.parentId,
      type: 'smoothstep',
      animated: false,
      label: 'runs on',
      labelStyle: {
        fontSize: 10,
        fill: '#9CA3AF',
        fontWeight: 500,
      },
      labelBgStyle: {
        fill: '#1F2937',
        fillOpacity: 0.8,
      },
      labelBgPadding: [4, 2] as [number, number],
      style: {
        stroke: '#4B5563',
        strokeWidth: 2,
        strokeDasharray: '3,3',
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: '#4B5563',
        width: 15,
        height: 15,
      },
    }));

    const allEdges = [...edges, ...groupEdges];

    return { rawNodes: allNodes as Node[], rawEdges: allEdges };
  }, [
    entities,
    relationships,
    sameAs,
    connectorNames,
    expandedGroups,
    handleToggleGroup,
    pendingSuggestions,
  ]);

  const [nodes, setNodes, onNodesChange] = useNodesState(rawNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(rawEdges);

  // Run elkjs layout when data or layout mode changes
  useEffect(() => {
    if (rawNodes.length === 0) {
      setNodes([]);
      setEdges(rawEdges);
      setSwimLanes([]);
      return;
    }

    // Generate a data key to detect real changes
    const dataKey = `${rawNodes.map((n) => n.id).sort().join(',')}:${layoutMode}:${rawNodes.length}`;
    const isFirstLayout = !initialLayoutDone.current;
    const dataChanged = dataKey !== lastDataKey.current;
    lastDataKey.current = dataKey;

    if (!isFirstLayout && !dataChanged) {
      return;
    }

    let cancelled = false;

    computeElkLayout(rawNodes, rawEdges, layoutMode).then((result) => {
      if (cancelled) return;

      if (isFirstLayout) {
        // First render: set positions directly (no animation)
        setNodes(result.nodes);
        setEdges(result.edges);
        initialLayoutDone.current = true;
      } else {
        // Subsequent renders: animate to new positions
        setEdges(result.edges);

        // Merge new node data with current positions for animation start
        setNodes((current) => {
          const currentMap = new Map(current.map((n) => [n.id, n]));
          return result.nodes.map((n) => ({
            ...n,
            position: currentMap.get(n.id)?.position ?? n.position,
          }));
        });

        // Animate after a tick to allow setNodes to settle
        requestAnimationFrame(() => {
          if (!cancelled) {
            animateToPositions(result.nodes);
          }
        });
      }

      // Update swim lanes
      if (layoutMode === 'hierarchical' && result.tierBounds) {
        setSwimLanes(buildSwimLanes(result.tierBounds));
      } else {
        setSwimLanes([]);
      }
    }).catch((error) => {
      console.error('Layout computation failed:', error);
      if (!cancelled) {
        setNodes(rawNodes);
        setEdges(rawEdges);
      }
    });

    return () => {
      cancelled = true;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawNodes, rawEdges, layoutMode]);

  // Update selected state
  useEffect(() => {
    setNodes((nds) =>
      nds.map((node) => ({
        ...node,
        selected: node.id === selectedEntityId,
      })),
    );
  }, [selectedEntityId, setNodes]);

  // Apply search highlighting (dim non-matching to ~30% opacity, highlight matching)
  useEffect(() => {
    setNodes((nds) => {
      if (!searchTerm) {
        // Clear all highlight styling
        return nds.map((node) => ({
          ...node,
          style: { ...node.style, opacity: undefined, transition: undefined },
          data: { ...node.data, _highlighted: undefined },
        }));
      }

      const lowerSearch = searchTerm.toLowerCase();
      return nds.map((node) => {
        const name = (node.data as { name?: string }).name ?? '';
        const description = (node.data as { description?: string }).description ?? '';
        const matches =
          name.toLowerCase().includes(lowerSearch) ||
          description.toLowerCase().includes(lowerSearch);

        return {
          ...node,
          style: {
            ...node.style,
            opacity: matches ? 1 : 0.3,
            transition: 'opacity 0.3s ease',
          },
          data: {
            ...node.data,
            _highlighted: matches ? true : undefined,
          },
        };
      });
    });
  }, [searchTerm, setNodes]);

  // Apply investigation path styles
  useEffect(() => {
    if (!investigationPath) return;

    setNodes((nds) => {
      const result = applyInvestigationStyles(nds, [], investigationPath);
      return result.nodes;
    });
    setEdges((eds) => {
      const result = applyInvestigationStyles([], eds, investigationPath);
      return result.edges;
    });
  }, [investigationPath, setNodes, setEdges]);

  // Auto-follow: pan to current node when it changes
  useEffect(() => {
    if (!investigationPath?.currentNodeId) return;

    const currentId = investigationPath.currentNodeId;

    // On node hop, reset userHasPanned flag
    if (currentId !== prevCurrentNodeId.current) {
      userHasPanned.current = false;
      prevCurrentNodeId.current = currentId;
    }

    // Skip if user has manually panned since last hop
    if (userHasPanned.current) return;

    // Find the node position and center on it
    const currentNode = reactFlowInstance.getNodes().find((n) => n.id === currentId);
    if (currentNode) {
      reactFlowInstance.setCenter(
        currentNode.position.x + 110, // offset by half node width
        currentNode.position.y + 40,  // offset by half node height
        { duration: 300, zoom: 1.2 },
      );
    }
  }, [investigationPath?.currentNodeId, reactFlowInstance, investigationPath]);

  // Track user-initiated pan to pause auto-follow
  const handleMoveStart = useCallback(
    (_event: unknown, _viewport: unknown) => {
      // Only set if investigation is active
      if (investigationPath?.isActive) {
        userHasPanned.current = true;
      }
    },
    [investigationPath?.isActive],
  );

  // Handle node click
  const handleNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const entity = entities.find((e) => e.id === node.id);
      if (entity && onNodeClick) {
        onNodeClick(entity);
      }
    },
    [entities, onNodeClick],
  );

  // Handle node double-click (focus mode)
  const handleNodeDoubleClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      if (onNodeDoubleClick) {
        onNodeDoubleClick(node.id);
      }
    },
    [onNodeDoubleClick],
  );

  // Handle node right-click (context menu)
  const handleNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: Node) => {
      event.preventDefault();
      const data = node.data as EntityNodeData;
      const pane = graphRef.current?.getBoundingClientRect();
      if (!pane) return;

      // Position menu away from edges of the graph container
      const MENU_WIDTH = 200;
      const MENU_HEIGHT = 180;

      setContextMenu({
        nodeId: node.id,
        entityName: data.name,
        entityType: data.entityType || 'Entity',
        scope: data.scope,
        top: event.clientY + MENU_HEIGHT < window.innerHeight
          ? event.clientY
          : undefined,
        left: event.clientX + MENU_WIDTH < window.innerWidth
          ? event.clientX
          : undefined,
        right: event.clientX + MENU_WIDTH >= window.innerWidth
          ? window.innerWidth - event.clientX
          : undefined,
        bottom: event.clientY + MENU_HEIGHT >= window.innerHeight
          ? window.innerHeight - event.clientY
          : undefined,
      });
    },
    [],
  );

  // Close context menu
  const handleCloseContextMenu = useCallback(() => {
    setContextMenu(null);
  }, []);

  // Context menu: show details (reuse onNodeClick)
  const handleContextShowDetails = useCallback(
    (nodeId: string) => {
      const entity = entities.find((e) => e.id === nodeId);
      if (entity && onNodeClick) {
        onNodeClick(entity);
      }
    },
    [entities, onNodeClick],
  );

  // Context menu: focus neighbors (reuse onNodeDoubleClick)
  const handleContextFocusNeighbors = useCallback(
    (nodeId: string) => {
      if (onNodeDoubleClick) {
        onNodeDoubleClick(nodeId);
      }
    },
    [onNodeDoubleClick],
  );

  // Context menu: investigate (delegate to parent)
  const handleContextInvestigate = useCallback(
    (entityName: string, entityType: string, scope?: string) => {
      if (onInvestigate) {
        onInvestigate(entityName, entityType, scope);
      }
    },
    [onInvestigate],
  );

  // Close context menu on pane click
  const handlePaneClick = useCallback(() => {
    setContextMenu(null);
  }, []);

  // Minimap node color
  const minimapNodeColor = useCallback((node: Node) => {
    const data = node.data as { entityType?: string; rawAttributes?: Record<string, unknown> | null } | undefined;
    // Color by tier
    if (data?.entityType) {
      const tier = getTierForEntity(data.entityType);
      if (tier) {
        const colorMap: Record<number, string> = {
          0: '#A855F7', // purple - Application
          1: '#3B82F6', // blue - Service
          2: '#06B6D4', // cyan - Workload
          3: '#22C55E', // green - Pod
          4: '#EAB308', // yellow - Node
          5: '#F97316', // orange - VM
          6: '#EF4444', // red - Cloud
        };
        return colorMap[tier.partition] ?? '#6B7280';
      }
    }
    // Fallback: connector entities get amber
    const attrs = data?.rawAttributes;
    if (attrs && 'connector_type' in attrs) {
      return '#F59E0B';
    }
    return '#6B7280';
  }, []);

  if (entities.length === 0) {
    return (
      <div className="flex items-center justify-center h-full bg-gray-900 rounded-lg border border-gray-700">
        <div className="text-center text-gray-400">
          <div className="text-4xl mb-4">
            <Network className="w-12 h-12 mx-auto text-gray-600" />
          </div>
          <div className="text-lg font-medium">No topology data yet</div>
          <div className="text-sm mt-2">
            MEHO learns topology as it investigates systems.
            <br />
            Ask it about your infrastructure to start building the graph!
          </div>
        </div>
      </div>
    );
  }

  return (
    <div ref={graphRef} className="h-full w-full relative">
      {/* Swim lane background (hierarchical mode only) */}
      {layoutMode === 'hierarchical' && swimLanes.length > 0 && (
        <SwimLaneBackground lanes={swimLanes} />
      )}

      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onNodeContextMenu={handleNodeContextMenu}
        onPaneClick={handlePaneClick}
        onMoveStart={handleMoveStart}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={2}
        defaultEdgeOptions={{
          type: 'smoothstep',
        }}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color="#374151"
        />
        <Controls
          className="!bg-gray-800 !border-gray-700 !rounded-lg"
          showInteractive={false}
        />
        <MiniMap
          nodeColor={minimapNodeColor}
          maskColor="rgba(0, 0, 0, 0.8)"
          className="!bg-gray-900 !border-gray-700 !rounded-lg"
          pannable
          zoomable
        />
      </ReactFlow>

      {/* Context menu (rendered above React Flow controls via z-index >= 100) */}
      {contextMenu && (
        <TopologyContextMenu
          nodeId={contextMenu.nodeId}
          entityName={contextMenu.entityName}
          entityType={contextMenu.entityType}
          scope={contextMenu.scope}
          top={contextMenu.top}
          left={contextMenu.left}
          right={contextMenu.right}
          bottom={contextMenu.bottom}
          onClose={handleCloseContextMenu}
          onInvestigate={handleContextInvestigate}
          onShowDetails={handleContextShowDetails}
          onFocusNeighbors={handleContextFocusNeighbors}
        />
      )}
    </div>
  );
}

// ============================================================================
// Exported wrapper (no ReactFlowProvider needed -- TopologyExplorerPage provides it)
// ============================================================================

export function TopologyGraph(props: Readonly<TopologyGraphProps>) {
  return <TopologyGraphInner {...props} />;
}
