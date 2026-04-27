// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Topology Components
 */

export { EntityNode } from './EntityNode';
export type { EntityNodeData } from './EntityNode';

export { TopologyGraph } from './TopologyGraph';
export { TopologyFilters } from './TopologyFilters';
export { EntityDetailsPanel } from './EntityDetailsPanel';

// SAME_AS Suggestion Components
export { SuggestionCard } from './SuggestionCard';
export { SuggestionsPanel } from './SuggestionsPanel';

// Phase 61 Plan 02: Context menu
export { TopologyContextMenu } from './ContextMenu';

// Phase 61 Plan 03: Investigation path visualization
export { useInvestigationPath } from './useInvestigationPath';
export type { PathNode, PathEdge, InvestigationPathData } from './useInvestigationPath';
export { InvestigationToolbar, applyInvestigationStyles } from './InvestigationOverlay';
export { MiniTopology } from './MiniTopology';

// Phase 76: Shared display components
export { FreshnessLabel } from './FreshnessLabel';
export { ConfidenceBadge } from './ConfidenceBadge';
export { TopologyTabNav } from './TopologyTabNav';
export type { TopologyTab } from './TopologyTabNav';

// Phase 76: Entity table
export { TopologyEntityTable } from './TopologyEntityTable';

// Phase 76 Plan 05: Connector Map components
export { ConnectorRelationshipList } from './ConnectorRelationshipList';
export { ConnectorRelationshipForm } from './ConnectorRelationshipForm';
export { RelationshipTypeSelect } from './RelationshipTypeSelect';

// Phase 76 Plan 05: Enhanced entity panels
export { EntitySameAsPanel } from './EntitySameAsPanel';
