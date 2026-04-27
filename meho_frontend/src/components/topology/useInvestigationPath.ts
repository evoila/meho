// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useInvestigationPath - Maps investigation steps to topology nodes/edges
 *
 * Subscribes to the orchestrator store (iterations, investigationStartTime) and
 * matches investigation steps to topology entities by name/canonical_id.
 * Produces path data consumed by InvestigationOverlay and MiniTopology.
 */

import { useMemo, useState, useCallback } from 'react';
import { useChatStore } from '../../features/chat/stores';
import type { TopologyEntity } from '../../lib/topologyApi';

// ============================================================================
// Types
// ============================================================================

export interface PathNode {
  entityId: string;
  entityName: string;
  connectorId: string;
  status: 'current' | 'visited';
  stepIndex: number;
}

export interface PathEdge {
  from: string;
  to: string;
}

export interface InvestigationPathData {
  nodes: PathNode[];
  edges: PathEdge[];
  isActive: boolean;
  currentNodeId: string | null;
}

// ============================================================================
// Entity name normalization for best-effort matching
// ============================================================================

/**
 * Normalize an entity/target name for fuzzy matching.
 * Strips common prefixes (ns/, pod/, deploy/, svc/, vm/) and lowercases.
 */
function normalizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/^(ns|pod|deploy|svc|vm)\//, '');
}

/**
 * Build a lookup index from topology entities.
 * Maps normalized name fragments to entity IDs for best-effort matching.
 */
function buildEntityIndex(entities: TopologyEntity[]): Map<string, string> {
  const index = new Map<string, string>();

  for (const entity of entities) {
    // Index by normalized name
    const normalizedName = normalizeName(entity.name);
    index.set(normalizedName, entity.id);

    // Index by normalized canonical_id
    if (entity.canonical_id) {
      const normalizedCanonical = normalizeName(entity.canonical_id);
      index.set(normalizedCanonical, entity.id);
    }
  }

  return index;
}

/**
 * Try to match a target entity string from an investigation step
 * to a topology entity ID using the lookup index.
 */
function matchTargetToEntity(
  target: string,
  index: Map<string, string>,
): string | null {
  const normalized = normalizeName(target);

  // 1. Exact match on canonical_id or name
  const exact = index.get(normalized);
  if (exact) return exact;

  // 2. Substring match: check if any indexed key contains the target or vice-versa
  for (const [key, entityId] of index) {
    if (key.includes(normalized) || normalized.includes(key)) {
      return entityId;
    }
  }

  return null;
}

// ============================================================================
// Hook
// ============================================================================

/**
 * Hook that subscribes to the Zustand orchestrator store and maps investigation
 * steps to topology nodes/edges for path visualization.
 *
 * @param topologyEntities - Current topology entities to match against
 * @returns path data and clearPath callback
 */
export function useInvestigationPath(topologyEntities: TopologyEntity[]) {
  const iterations = useChatStore((s) => s.iterations);
  const investigationStartTime = useChatStore((s) => s.investigationStartTime);

  // Local "cleared" state -- path persists until explicitly cleared
  const [cleared, setCleared] = useState(false);

  // Reset cleared flag when a new investigation starts
  const lastStartTime = useMemo(() => investigationStartTime, [investigationStartTime]);

  const path = useMemo<InvestigationPathData>(() => { // NOSONAR (cognitive complexity)
    // If user has cleared the path and no new investigation started, return empty
    if (cleared && !investigationStartTime) {
      return { nodes: [], edges: [], isActive: false, currentNodeId: null };
    }

    // If cleared but a NEW investigation started, auto-unset cleared
    // (handled below by checking investigationStartTime !== null)

    if (topologyEntities.length === 0 || iterations.length === 0) {
      return { nodes: [], edges: [], isActive: false, currentNodeId: null };
    }

    const entityIndex = buildEntityIndex(topologyEntities);
    const pathNodes: PathNode[] = [];
    const seenEntityIds = new Set<string>();
    let currentNodeId: string | null = null;
    let stepCounter = 0;
    let hasRunning = false;

    // Walk all iterations/connectors/steps in order
    for (const iter of iterations) {
      for (const [, connector] of iter.connectors) {
        for (const step of connector.steps) {
          if (step.type !== 'tool_call' || !step.targetEntity) continue;

          const entityId = matchTargetToEntity(step.targetEntity, entityIndex);
          if (!entityId) {
            console.debug(
              `[useInvestigationPath] Unmatched target entity: "${step.targetEntity}"`,
            );
            continue;
          }

          const isRunning = step.status === 'running';
          if (isRunning) hasRunning = true;

          if (!seenEntityIds.has(entityId)) {
            seenEntityIds.add(entityId);
            pathNodes.push({
              entityId,
              entityName: step.targetEntity,
              connectorId: connector.connectorId,
              status: isRunning ? 'current' : 'visited',
              stepIndex: stepCounter,
            });
          } else {
            // Update status if this step is running
            if (isRunning) {
              const existing = pathNodes.find((n) => n.entityId === entityId);
              if (existing) existing.status = 'current';
            }
          }

          if (isRunning) {
            currentNodeId = entityId;
          }

          stepCounter++;
        }
      }
    }

    // Mark all non-current nodes as visited
    for (const node of pathNodes) {
      if (node.entityId !== currentNodeId && node.status === 'current') {
        node.status = 'visited';
      }
    }

    // Build edges between consecutive path nodes
    const pathEdges: PathEdge[] = [];
    for (let i = 1; i < pathNodes.length; i++) {
      pathEdges.push({
        from: pathNodes[i - 1].entityId,
        to: pathNodes[i].entityId,
      });
    }

    return {
      nodes: pathNodes,
      edges: pathEdges,
      isActive: hasRunning || investigationStartTime !== null,
      currentNodeId,
    };
  }, [iterations, investigationStartTime, topologyEntities, cleared]);

  // Reset cleared flag when a new investigation starts
  useMemo(() => {
    if (lastStartTime !== null && cleared) {
      setCleared(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastStartTime]);

  const clearPath = useCallback(() => {
    setCleared(true);
  }, []);

  return { path, clearPath };
}
