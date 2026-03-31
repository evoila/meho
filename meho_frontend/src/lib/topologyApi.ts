// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Topology API Client (TASK-129)
 * 
 * API client for the topology explorer - fetches learned topology data.
 */

import { getAPIClient } from './api-client';
import { config } from './config';

// ============================================================================
// Types
// ============================================================================

export interface TopologyEntity {
  id: string;
  name: string;
  connector_id: string | null;
  description: string;
  raw_attributes: Record<string, any> | null;
  discovered_at: string;
  last_verified_at: string | null;
  stale_at: string | null;
  tenant_id: string;
  // Phase 17: Additional fields from backend TopologyGraphNode schema
  entity_type: string;
  connector_type: string;
  scope: Record<string, any> | null;
  canonical_id: string;
}

export interface TopologyRelationship {
  id: string;
  from_entity_id: string;
  to_entity_id: string;
  relationship_type: string;
  discovered_at: string;
  last_verified_at: string | null;
}

export interface TopologySameAs {
  id: string;
  entity_a_id: string;
  entity_b_id: string;
  similarity_score: number;
  verified_via: string[];
  discovered_at: string;
  last_verified_at: string | null;
}

export interface TopologyGraphResponse {
  nodes: TopologyEntity[];
  relationships: TopologyRelationship[];
  same_as: TopologySameAs[];
}

export interface TopologyLookupResult {
  found: boolean;
  entity?: TopologyEntity;
  topology_chain: TopologyChainItem[];
  connectors_traversed: string[];
  possibly_related: PossiblyRelatedEntity[];
}

export interface TopologyChainItem {
  depth: number;
  entity_id: string;
  entity_name: string;
  connector_id: string | null;
  relationship_type: string | null;
}

export interface PossiblyRelatedEntity {
  entity_id: string;
  entity_name: string;
  connector_id: string | null;
  similarity_score: number;
}

// ============================================================================
// SAME_AS Suggestion Types (TASK-144 Phase 4)
// ============================================================================

export type SuggestionStatus = 'pending' | 'approved' | 'rejected';
export type SuggestionMatchType = 'hostname_match' | 'ip_match' | 'llm_verified' | 'manual' | 'embedding_similarity';

export interface SameAsSuggestion {
  id: string;
  entity_a_id: string;
  entity_b_id: string;
  entity_a_name: string;
  entity_b_name: string;
  entity_a_connector_name?: string | null;
  entity_b_connector_name?: string | null;
  confidence: number;
  match_type: SuggestionMatchType;
  match_details?: Record<string, any> | null;
  status: SuggestionStatus;
  suggested_at: string;
  resolved_at?: string | null;
  resolved_by?: string | null;
  tenant_id: string;
  // LLM verification fields (Phase 3)
  llm_verification_attempted?: boolean;
  llm_verification_result?: Record<string, any> | null;
}

export interface SuggestionListResponse {
  suggestions: SameAsSuggestion[];
  total: number;
}

export interface SuggestionActionResponse {
  success: boolean;
  message: string;
  same_as_created?: boolean;
}

export interface VerificationResponse {
  success: boolean;
  suggestion_id: string;
  new_status: SuggestionStatus;
  llm_result?: Record<string, any> | null;
  message: string;
}

export interface DiscoveryResponse {
  success: boolean;
  suggestions_created: number;
  suggestions_skipped_existing: number;
  suggestions_skipped_ineligible: number;
  total_pairs_analyzed: number;
  message: string;
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Fetch a single topology entity by ID
 */
export async function fetchEntity(entityId: string): Promise<TopologyEntity> {
  const response = await fetch(`${getBaseUrl()}/api/topology/entities/${entityId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch entity: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch full topology graph (nodes + edges)
 */
export async function fetchTopologyGraph(filters?: {
  connector_id?: string;
  include_stale?: boolean;
}): Promise<TopologyGraphResponse> {
  const params = new URLSearchParams();
  
  if (filters?.connector_id) params.set('connector_id', filters.connector_id);
  if (filters?.include_stale !== undefined) params.set('include_stale', String(filters.include_stale));
  
  const response = await fetch(`${getBaseUrl()}/api/topology/graph?${params.toString()}`, {
    headers: getAuthHeaders(),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to fetch topology graph: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Lookup an entity and get its topology chain
 */
export async function lookupTopology(query: string): Promise<TopologyLookupResult> {
  const params = new URLSearchParams({ query });
  
  const response = await fetch(`${getBaseUrl()}/api/topology/lookup?${params.toString()}`, {
    headers: getAuthHeaders(),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to lookup topology: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Invalidate an entity (mark as stale)
 */
export async function invalidateTopologyEntity(
  entityName: string,
  reason: string
): Promise<{ invalidated: boolean; message: string }> {
  const response = await fetch(`${getBaseUrl()}/api/topology/entities/${encodeURIComponent(entityName)}/invalidate`, {
    method: 'POST',
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ reason }),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to invalidate entity: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Delete stale entities older than specified days
 */
export async function deleteStaleEntities(
  olderThanDays: number = 7
): Promise<{ deleted_count: number }> {
  const response = await fetch(`${getBaseUrl()}/api/topology/stale?older_than_days=${olderThanDays}`, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to delete stale entities: ${response.statusText}`);
  }
  
  return response.json();
}

// ============================================================================
// SAME_AS Suggestion API Functions (TASK-144 Phase 4)
// ============================================================================

/**
 * Fetch pending SAME_AS suggestions
 */
export async function fetchSuggestions(params?: {
  limit?: number;
  offset?: number;
}): Promise<SuggestionListResponse> {
  const urlParams = new URLSearchParams();
  
  if (params?.limit !== undefined) urlParams.set('limit', String(params.limit));
  if (params?.offset !== undefined) urlParams.set('offset', String(params.offset));
  
  const response = await fetch(`${getBaseUrl()}/api/topology/suggestions?${urlParams.toString()}`, {
    headers: getAuthHeaders(),
  });
  
  if (!response.ok) {
    throw new Error(`Failed to fetch suggestions: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Approve a SAME_AS suggestion (creates confirmed relationship)
 */
export async function approveSuggestion(
  suggestionId: string
): Promise<SuggestionActionResponse> {
  const response = await fetch(`${getBaseUrl()}/api/topology/suggestions/${suggestionId}/approve`, {
    method: 'POST',
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
    },
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to approve suggestion: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Reject a SAME_AS suggestion
 */
export async function rejectSuggestion(
  suggestionId: string
): Promise<SuggestionActionResponse> {
  const response = await fetch(`${getBaseUrl()}/api/topology/suggestions/${suggestionId}/reject`, {
    method: 'POST',
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
    },
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to reject suggestion: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Trigger LLM verification for a suggestion
 */
export async function verifySuggestion(
  suggestionId: string
): Promise<VerificationResponse> {
  const response = await fetch(`${getBaseUrl()}/api/topology/suggestions/${suggestionId}/verify`, {
    method: 'POST',
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
    },
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to verify suggestion: ${response.statusText}`);
  }
  
  return response.json();
}

/**
 * Trigger SAME_AS discovery (TASK-160 Phase 3)
 * 
 * Scans entity embeddings across different connectors to find
 * high-similarity pairs that might represent the same physical resource.
 */
export async function triggerDiscovery(params?: {
  min_similarity?: number;
  limit?: number;
  verify?: boolean;
}): Promise<DiscoveryResponse> {
  const urlParams = new URLSearchParams();
  
  if (params?.min_similarity !== undefined) urlParams.set('min_similarity', String(params.min_similarity));
  if (params?.limit !== undefined) urlParams.set('limit', String(params.limit));
  if (params?.verify !== undefined) urlParams.set('verify', String(params.verify));
  
  const queryString = urlParams.toString();
  const url = `${getBaseUrl()}/api/topology/suggestions/discover${queryString ? `?${queryString}` : ''}`;
  
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
    },
  });
  
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to trigger discovery: ${response.statusText}`);
  }
  
  return response.json();
}

// ============================================================================
// Connector Relationship Types (D-12)
// ============================================================================

export const CONNECTOR_RELATIONSHIP_TYPES = [
  'monitors',
  'logs_for',
  'traces_for',
  'deploys_to',
  'manages',
  'alerts_for',
  'tracks_issues_for',
] as const;

export type ConnectorRelationshipType = typeof CONNECTOR_RELATIONSHIP_TYPES[number];

export interface ConnectorRelationship {
  id: string;
  from_connector_id: string;
  from_connector_name: string;
  to_connector_id: string;
  to_connector_name: string;
  relationship_type: ConnectorRelationshipType;
  discovered_at: string;
  last_verified_at: string | null;
}

export interface ConnectorRelationshipListResponse {
  relationships: ConnectorRelationship[];
  total: number;
}

export interface ConnectorRelationshipCreateRequest {
  from_connector_id: string;
  to_connector_id: string;
  relationship_type: ConnectorRelationshipType;
}

// ============================================================================
// Entity List Types
// ============================================================================

export interface TopologyEntityListResponse {
  entities: TopologyEntity[];
  total: number;
}

// ============================================================================
// Connector Relationship API Functions
// ============================================================================

export async function fetchConnectorRelationships(): Promise<ConnectorRelationshipListResponse> {
  const response = await fetch(`${getBaseUrl()}/api/topology/connector-relationships`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error(`Failed to fetch connector relationships: ${response.statusText}`);
  return response.json();
}

export async function createConnectorRelationship(
  data: ConnectorRelationshipCreateRequest
): Promise<ConnectorRelationship> {
  const response = await fetch(`${getBaseUrl()}/api/topology/connector-relationships`, {
    method: 'POST',
    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to create relationship: ${response.statusText}`);
  }
  return response.json();
}

export async function updateConnectorRelationship(
  relationshipId: string,
  data: ConnectorRelationshipCreateRequest
): Promise<ConnectorRelationship> {
  const response = await fetch(`${getBaseUrl()}/api/topology/connector-relationships/${relationshipId}`, {
    method: 'PUT',
    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `Failed to update relationship: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteConnectorRelationship(relationshipId: string): Promise<void> {
  const response = await fetch(`${getBaseUrl()}/api/topology/connector-relationships/${relationshipId}`, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error(`Failed to delete relationship: ${response.statusText}`);
}

// ============================================================================
// Entity API Functions
// ============================================================================

export async function fetchEntities(params?: {
  include_stale?: boolean;
  limit?: number;
  offset?: number;
  connector_id?: string;
  entity_type?: string;
  search?: string;
}): Promise<TopologyEntityListResponse> {
  const urlParams = new URLSearchParams();
  if (params?.include_stale !== undefined) urlParams.set('include_stale', String(params.include_stale));
  if (params?.limit !== undefined) urlParams.set('limit', String(params.limit));
  if (params?.offset !== undefined) urlParams.set('offset', String(params.offset));

  const response = await fetch(`${getBaseUrl()}/api/topology/entities?${urlParams.toString()}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error(`Failed to fetch entities: ${response.statusText}`);
  return response.json();
}

export async function fetchEntityRelationships(entityId: string): Promise<TopologyRelationship[]> {
  const graph = await fetchTopologyGraph();
  return graph.relationships.filter(
    r => r.from_entity_id === entityId || r.to_entity_id === entityId
  );
}

export async function fetchEntitySameAs(entityId: string): Promise<TopologySameAs[]> {
  const graph = await fetchTopologyGraph();
  return graph.same_as.filter(
    s => s.entity_a_id === entityId || s.entity_b_id === entityId
  );
}

/**
 * Delete a topology entity
 */
export async function deleteTopologyEntity(entityId: string): Promise<void> {
  const response = await fetch(`${getBaseUrl()}/api/topology/entities/${entityId}`, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error(`Failed to delete entity: ${response.statusText}`);
}

// ============================================================================
// Helpers
// ============================================================================

function getBaseUrl(): string {
  return config.apiURL;
}

function getAuthHeaders(): Record<string, string> {
  const token = getAPIClient().getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
