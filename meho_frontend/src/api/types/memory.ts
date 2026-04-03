// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Memory Types
 *
 * TypeScript types matching the backend MemoryResponse schema.
 * Used by the Memory tab in ConnectorDetails (Phase 13 - Memory UI).
 */

export type MemoryType = 'entity' | 'pattern' | 'outcome' | 'config';
export type ConfidenceLevel = 'operator' | 'confirmed_outcome' | 'auto_extracted';

export interface MemoryResponse {
  id: string;
  tenant_id: string;
  connector_id: string;
  title: string;
  body: string;
  memory_type: MemoryType;
  tags: string[];
  confidence_level: ConfidenceLevel;
  source_type: string;
  created_by: string | null;
  provenance_trail: Array<{
    conversation_id?: string | null;
    timestamp: string;
    source: string;
  }>;
  occurrence_count: number;
  last_accessed: string | null;
  last_seen: string;
  created_at: string;
  updated_at: string;
  merged: boolean;
}

/**
 * Memory update payload.
 * Note: confidence_level is intentionally omitted -- it is system-managed.
 * Even though the backend MemoryUpdate schema allows it, the frontend
 * must NOT expose it per CONTEXT.md decision.
 */
export interface MemoryUpdate {
  title?: string;
  body?: string;
  memory_type?: MemoryType;
  tags?: string[];
}
