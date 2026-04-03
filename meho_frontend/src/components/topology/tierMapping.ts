// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * tierMapping.ts - Entity-type to tier partition + icon mapping
 *
 * Maps entity types from all connectors (K8s, VMware, GCP, ArgoCD, GitHub, Proxmox)
 * to infrastructure tiers for swim lane layout and lucide-react icons for node display.
 */

import {
  Box,
  Server,
  Cloud,
  Globe,
  Layers,
  FolderTree,
  Container,
  Network,
  HardDrive,
  Database,
  GitBranch,
  Workflow,
  Package,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

// ============================================================================
// Tier Configuration
// ============================================================================

export interface TierConfig {
  partition: number;
  label: string;
  color: string; // Tailwind bg color for swim lane
}

/**
 * Infrastructure tiers from top (Application) to bottom (Cloud).
 * Partition index determines vertical ordering in hierarchical layout.
 */
export const TIER_CONFIG: Record<string, TierConfig> = {
  Application: { partition: 0, label: 'Application', color: 'bg-purple-500/5' },
  Service: { partition: 1, label: 'Service', color: 'bg-blue-500/5' },
  Workload: { partition: 2, label: 'Workload', color: 'bg-cyan-500/5' },
  Pod: { partition: 3, label: 'Pod', color: 'bg-green-500/5' },
  Node: { partition: 4, label: 'Node', color: 'bg-yellow-500/5' },
  VM: { partition: 5, label: 'VM', color: 'bg-orange-500/5' },
  Cloud: { partition: 6, label: 'Cloud', color: 'bg-red-500/5' },
};

// ============================================================================
// Entity Type Mapping
// ============================================================================

/**
 * Maps each known entity_type string to its infrastructure tier and lucide icon.
 *
 * Connector-agnostic: entity types from different connectors that represent
 * the same infrastructure concept share the same tier.
 */
export const ENTITY_TYPE_MAP: Record<string, { tier: string; icon: LucideIcon }> = {
  // Kubernetes
  Ingress: { tier: 'Application', icon: Globe },
  Service: { tier: 'Service', icon: Globe },
  Deployment: { tier: 'Workload', icon: Layers },
  StatefulSet: { tier: 'Workload', icon: Database },
  DaemonSet: { tier: 'Workload', icon: Workflow },
  ReplicaSet: { tier: 'Workload', icon: Package },
  Pod: { tier: 'Pod', icon: Box },
  Node: { tier: 'Node', icon: Server },
  Namespace: { tier: 'Cloud', icon: FolderTree },
  // VMware
  VM: { tier: 'VM', icon: Container },
  Host: { tier: 'Node', icon: Server },
  Cluster: { tier: 'Cloud', icon: Network },
  Datacenter: { tier: 'Cloud', icon: Cloud },
  Datastore: { tier: 'Cloud', icon: HardDrive },
  // GCP
  Instance: { tier: 'VM', icon: Container },
  GKECluster: { tier: 'Cloud', icon: Cloud },
  NodePool: { tier: 'Node', icon: Server },
  Subnet: { tier: 'Cloud', icon: Network },
  Disk: { tier: 'Cloud', icon: HardDrive },
  Snapshot: { tier: 'Cloud', icon: Database },
  // ArgoCD / GitHub
  Organization: { tier: 'Cloud', icon: GitBranch },
  Application: { tier: 'Application', icon: Globe },
  Repository: { tier: 'Cloud', icon: GitBranch },
};

/**
 * Default for unknown entity types -- placed in Workload tier with Box icon.
 */
export const DEFAULT_ENTITY: { tier: string; icon: LucideIcon } = {
  tier: 'Workload',
  icon: Box,
};

// ============================================================================
// Helpers
// ============================================================================

/**
 * Get the tier config for a given entity type.
 * Falls back to the default entity's tier if type is unknown.
 */
export function getTierForEntity(entityType: string): TierConfig {
  const mapping = ENTITY_TYPE_MAP[entityType];
  const tierName = mapping?.tier ?? DEFAULT_ENTITY.tier;
  return TIER_CONFIG[tierName];
}

/**
 * Get the lucide icon component for a given entity type.
 * Falls back to Box icon for unknown types.
 */
export function getIconForEntity(entityType: string): LucideIcon {
  return ENTITY_TYPE_MAP[entityType]?.icon ?? DEFAULT_ENTITY.icon;
}

// ============================================================================
// Swim Lane Types & Builder
// ============================================================================

export interface SwimLane {
  tier: string;
  label: string;
  color: string;
  yStart: number;
  yEnd: number;
}

/**
 * Build swim lane descriptors from tier bounds map (computed by elkjs layout).
 */
export function buildSwimLanes(
  tierBounds: Map<string, { yStart: number; yEnd: number }>,
): SwimLane[] {
  const lanes: SwimLane[] = [];

  for (const [tierName, bounds] of tierBounds) {
    const config = TIER_CONFIG[tierName];
    if (!config) continue;

    lanes.push({
      tier: tierName,
      label: config.label,
      color: config.color,
      yStart: bounds.yStart,
      yEnd: bounds.yEnd,
    });
  }

  // Sort by partition (top to bottom)
  lanes.sort((a, b) => {
    const ap = TIER_CONFIG[a.tier]?.partition ?? 99;
    const bp = TIER_CONFIG[b.tier]?.partition ?? 99;
    return ap - bp;
  });

  return lanes;
}
