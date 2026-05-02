// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useAuditEvents Hook
 *
 * React Query hooks for fetching audit events from the audit BFF API.
 * - useAuditEvents: Admin view -- GET /api/audit/events with filters
 * - useMyActivity: Personal activity -- GET /api/audit/my-activity
 */
import { useQuery } from '@tanstack/react-query';
import { getAdminClient } from '@/api/clients/admin';
import type { AuditEventsResponse, AuditEventFilters } from '@/api/types/audit';

/**
 * Fetch audit events for admin view with optional filters.
 *
 * The `resource_type` filter is critical for contextual audit sections
 * on ConnectorsPage (resource_type='connector') and KnowledgePage
 * (resource_type='knowledge_doc').
 */
export function useAuditEvents(filters?: AuditEventFilters) {
  const adminClient = getAdminClient();

  return useQuery<AuditEventsResponse>({
    queryKey: ['audit-events', filters],
    queryFn: () => adminClient.getAuditEvents(filters),
    staleTime: 30_000,
  });
}

/**
 * Fetch the current user's own activity log.
 */
export function useMyActivity(offset = 0, limit = 50) {
  const adminClient = getAdminClient();

  return useQuery<AuditEventsResponse>({
    queryKey: ['my-activity', offset, limit],
    queryFn: () => adminClient.getMyActivity(offset, limit),
    staleTime: 30_000,
  });
}
