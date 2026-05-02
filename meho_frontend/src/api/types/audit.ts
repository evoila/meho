// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Audit API Types
 *
 * Types matching the backend AuditEventResponse schema from routes_audit.py.
 */

export interface AuditEvent {
  id: string;
  tenant_id: string;
  user_id: string;
  user_email: string | null;
  event_type: string; // 'connector.create', 'auth.login', etc.
  action: string; // 'create', 'update', 'delete', 'login', etc.
  resource_type: string; // 'connector', 'knowledge_doc', 'config', 'session'
  resource_id: string | null;
  resource_name: string | null;
  details: Record<string, unknown> | null;
  result: 'success' | 'failure' | 'error';
  ip_address: string | null;
  user_agent: string | null;
  created_at: string; // ISO timestamp
}

export interface AuditEventsResponse {
  events: AuditEvent[];
  total: number;
}

export interface AuditEventFilters {
  event_type?: string;
  resource_type?: string;
  user_id?: string;
  offset?: number;
  limit?: number;
}
