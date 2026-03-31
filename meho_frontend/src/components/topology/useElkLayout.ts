// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useElkLayout.ts - Async elkjs layout computation with partitioning
 *
 * Replaces dagre with elkjs for all topology layout computation.
 * Supports three layout modes: hierarchical (layered with swim lane partitions),
 * force-directed, and radial.
 */

import { useCallback, useRef } from 'react';
import ELK from 'elkjs/lib/elk.bundled.js';
import type { Node, Edge } from '@xyflow/react';
import { getTierForEntity, TIER_CONFIG } from './tierMapping';

// ============================================================================
// Types
// ============================================================================

export type LayoutMode = 'hierarchical' | 'force' | 'radial';

export interface TierBound {
  yStart: number;
  yEnd: number;
}

export interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
  tierBounds: Map<string, TierBound> | null;
}

// ============================================================================
// Singleton ELK instance
// ============================================================================

const elk = new ELK();

// Node dimensions for layout computation
const NODE_WIDTH = 220;
const NODE_HEIGHT = 80;

// ============================================================================
// Layout algorithm configs
// ============================================================================

const ALGORITHM_OPTIONS: Record<LayoutMode, Record<string, string>> = {
  hierarchical: {
    'elk.algorithm': 'layered',
    'elk.direction': 'DOWN',
    'elk.partitioning.activate': 'true',
    'elk.layered.spacing.nodeNodeBetweenLayers': '120',
    'elk.spacing.nodeNode': '60',
    'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
    'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
  },
  force: {
    'elk.algorithm': 'force',
    'elk.force.iterations': '300',
    'elk.spacing.nodeNode': '80',
  },
  radial: {
    'elk.algorithm': 'radial',
    'elk.spacing.nodeNode': '60',
  },
};

// ============================================================================
// Core layout function
// ============================================================================

/**
 * Compute layout positions for nodes using elkjs.
 *
 * For hierarchical mode, nodes are assigned to partitions based on their
 * entity type tier (Application=0 through Cloud=6). This produces the
 * swim-lane effect where nodes are grouped by infrastructure tier.
 *
 * Returns positioned nodes, edges, and (for hierarchical mode) tier bounds
 * computed from actual node positions.
 */
export async function computeElkLayout(
  nodes: Node[],
  edges: Edge[],
  mode: LayoutMode = 'hierarchical',
): Promise<LayoutResult> {
  if (nodes.length === 0) {
    return { nodes: [], edges, tierBounds: null };
  }

  const isLayered = mode === 'hierarchical';

  const elkGraph = {
    id: 'root',
    layoutOptions: ALGORITHM_OPTIONS[mode],
    children: nodes.map((node) => {
      const entityType = (node.data as Record<string, unknown>).entityType as string | undefined;
      const tierConfig = entityType ? getTierForEntity(entityType) : undefined;

      return {
        id: node.id,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        ...(isLayered && tierConfig && {
          layoutOptions: {
            'elk.partitioning.partition': String(tierConfig.partition),
          },
        }),
      };
    }),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  const layouted = await elk.layout(elkGraph);

  // Map computed positions back onto React Flow nodes
  const layoutedNodes = nodes.map((node) => {
    const elkNode = layouted.children?.find((n) => n.id === node.id);
    return {
      ...node,
      position: {
        x: elkNode?.x ?? 0,
        y: elkNode?.y ?? 0,
      },
    };
  });

  // Compute tier bounds from actual node positions (hierarchical only)
  let tierBounds: Map<string, TierBound> | null = null;

  if (isLayered) {
    const tierNodes = new Map<string, { minY: number; maxY: number }>();

    for (const node of layoutedNodes) {
      const entityType = (node.data as Record<string, unknown>).entityType as string | undefined;
      const tierConfig = entityType ? getTierForEntity(entityType) : undefined;
      const tierName = tierConfig
        ? Object.entries(TIER_CONFIG).find(([, c]) => c.partition === tierConfig.partition)?.[0]
        : undefined;

      if (!tierName) continue;

      const y = node.position.y;
      const existing = tierNodes.get(tierName);

      if (existing) {
        existing.minY = Math.min(existing.minY, y);
        existing.maxY = Math.max(existing.maxY, y + NODE_HEIGHT);
      } else {
        tierNodes.set(tierName, { minY: y, maxY: y + NODE_HEIGHT });
      }
    }

    // Convert to tier bounds with padding
    const LANE_PADDING = 30;
    const MIN_LANE_HEIGHT = 100;

    tierBounds = new Map();
    for (const [tier, { minY, maxY }] of tierNodes) {
      const height = maxY - minY;
      const paddedHeight = Math.max(height + LANE_PADDING * 2, MIN_LANE_HEIGHT);
      const yStart = minY - LANE_PADDING;
      tierBounds.set(tier, {
        yStart,
        yEnd: yStart + paddedHeight,
      });
    }
  }

  return { nodes: layoutedNodes, edges, tierBounds };
}

// ============================================================================
// Hook
// ============================================================================

/**
 * Hook wrapper around computeElkLayout with debouncing (300ms)
 * to prevent layout thrashing on rapid data changes.
 */
export function useElkLayout() {
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingResolve = useRef<((result: LayoutResult) => void) | null>(null);

  const debouncedLayout = useCallback(
    (nodes: Node[], edges: Edge[], mode: LayoutMode = 'hierarchical'): Promise<LayoutResult> => {
      // Cancel any pending debounced call
      if (debounceTimer.current) {
        clearTimeout(debounceTimer.current);
      }

      // If there's a pending promise, reject it silently by replacing the resolve
      return new Promise<LayoutResult>((resolve) => {
        pendingResolve.current = resolve;

        debounceTimer.current = setTimeout(async () => {
          try {
            const result = await computeElkLayout(nodes, edges, mode);
            // Only resolve the most recent promise
            if (pendingResolve.current === resolve) {
              resolve(result);
            }
          } catch (error) {
            console.error('elkjs layout failed:', error);
            resolve({ nodes, edges, tierBounds: null });
          }
        }, 300);
      });
    },
    [],
  );

  return { computeLayout: debouncedLayout };
}
