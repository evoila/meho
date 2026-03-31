// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Admin API Types
 * 
 * Types for superadmin dashboard and administration features.
 */

/**
 * System-wide statistics for the superadmin dashboard.
 */
export interface DashboardStats {
  /** Total number of tenants */
  total_tenants: number;
  /** Number of active tenants */
  active_tenants: number;
  /** Total connectors across all tenants */
  total_connectors: number;
  /** Chat sessions/workflows started today */
  workflows_today: number;
  /** Total knowledge chunks across all tenants */
  knowledge_chunks: number;
  /** Failed ingestion jobs today */
  errors_today: number;
}

/**
 * Activity types for the activity feed.
 */
export type ActivityType = 
  | 'tenant_created' 
  | 'connector_added' 
  | 'workflow_run' 
  | 'error';

/**
 * Single activity item for the activity feed.
 */
export interface ActivityItem {
  /** Unique identifier */
  id: string;
  /** Activity type */
  type: ActivityType;
  /** Human-readable description */
  description: string;
  /** Associated tenant ID */
  tenant_id?: string;
  /** When the activity occurred (ISO string) */
  timestamp: string;
}

