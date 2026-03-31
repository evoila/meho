// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/* eslint-disable react-refresh/only-export-components -- applyInvestigationStyles is a pure utility co-located with InvestigationToolbar */
/**
 * InvestigationOverlay - Visual styling for investigation path on topology graph
 *
 * Provides:
 * - applyInvestigationStyles(): Modifies React Flow node/edge arrays to
 *   highlight investigation path (current=ripple, visited=glow, edges=gradient)
 * - InvestigationToolbar: "Clear path" button shown when path has nodes
 */

import { X } from 'lucide-react';
import type { Node, Edge } from '@xyflow/react';
import type { InvestigationPathData } from './useInvestigationPath';

// ============================================================================
// Style application utility
// ============================================================================

/**
 * Apply investigation path visual styles to React Flow nodes and edges.
 *
 * - Current node: emerald ring + pulse animation (_investigationStatus='current')
 * - Visited nodes: soft emerald glow (_investigationStatus='visited')
 * - Non-path nodes: dimmed to 60% opacity
 * - Path edges: emerald gradient, thicker stroke, animated dash
 * - Non-path edges: reduced to 30% opacity
 *
 * Returns new arrays (does not mutate inputs).
 */
export function applyInvestigationStyles(
  nodes: Node[],
  edges: Edge[],
  path: InvestigationPathData,
): { nodes: Node[]; edges: Edge[] } {
  // If no path active, return nodes/edges unchanged
  if (path.nodes.length === 0) {
    // Clear any leftover investigation styles
    const cleanNodes = nodes.map((node) => ({
      ...node,
      data: {
        ...node.data,
        _investigationStatus: undefined,
      },
      style: {
        ...node.style,
        opacity: node.style?.opacity === 0.6 ? undefined : node.style?.opacity,
      },
    }));
    return { nodes: cleanNodes, edges };
  }

  const pathNodeIds = new Set(path.nodes.map((n) => n.entityId));
  const pathEdgeSet = new Set(
    path.edges.map((e) => `${e.from}--${e.to}`),
  );

  // Style nodes
  const styledNodes = nodes.map((node) => {
    const pathNode = path.nodes.find((pn) => pn.entityId === node.id);

    if (pathNode) {
      return {
        ...node,
        data: {
          ...node.data,
          _investigationStatus: pathNode.status,
        },
        style: {
          ...node.style,
          opacity: 1,
          transition: 'opacity 0.3s ease',
        },
      };
    }

    // Non-path node: dim during active investigation
    if (path.isActive || path.nodes.length > 0) {
      return {
        ...node,
        data: {
          ...node.data,
          _investigationStatus: undefined,
        },
        style: {
          ...node.style,
          opacity: 0.6,
          transition: 'opacity 0.3s ease',
        },
      };
    }

    return node;
  });

  // Style edges
  const styledEdges = edges.map((edge) => {
    const edgeKey = `${edge.source}--${edge.target}`;
    const reverseKey = `${edge.target}--${edge.source}`;
    const isPathEdge = pathEdgeSet.has(edgeKey) || pathEdgeSet.has(reverseKey);

    if (isPathEdge) {
      return {
        ...edge,
        animated: true,
        style: {
          ...edge.style,
          stroke: '#34D399',
          strokeWidth: 3,
          opacity: 1,
        },
        markerEnd: edge.markerEnd && typeof edge.markerEnd === 'object'
          ? {
              ...edge.markerEnd,
              color: '#34D399',
            }
          : edge.markerEnd,
      };
    }

    // Non-path edge: dim
    if (pathNodeIds.size > 0) {
      return {
        ...edge,
        style: {
          ...edge.style,
          opacity: 0.3,
        },
      };
    }

    return edge;
  });

  return { nodes: styledNodes, edges: styledEdges };
}

// ============================================================================
// Investigation Toolbar Component
// ============================================================================

interface InvestigationToolbarProps {
  pathNodeCount: number;
  onClearPath: () => void;
}

/**
 * "Clear path" button shown in the topology toolbar area
 * when investigation path has nodes.
 */
export function InvestigationToolbar({ pathNodeCount, onClearPath }: InvestigationToolbarProps) {
  if (pathNodeCount === 0) return null;

  return (
    <button
      onClick={onClearPath}
      className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-400 border border-gray-600 rounded-lg hover:bg-gray-800 hover:text-gray-200 transition-colors"
      title="Clear investigation path overlay"
    >
      <X className="w-4 h-4" />
      Clear path
    </button>
  );
}
