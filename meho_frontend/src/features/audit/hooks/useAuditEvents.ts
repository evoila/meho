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
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import type { AuditEventsResponse, AuditEventFilters } from '@/api/types/audit';

/**
 * Fetch audit events for admin view with optional filters.
 *
 * The `resource_type` filter is critical for contextual audit sections
 * on ConnectorsPage (resource_type='connector') and KnowledgePage
 * (resource_type='knowledge_doc').
 */
export function useAuditEvents(filters?: AuditEventFilters) {
  const apiClient = getAPIClient(config.apiURL);

  return useQuery<AuditEventsResponse>({
    queryKey: ['audit-events', filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters?.event_type) params.set('event_type', filters.event_type);
      if (filters?.resource_type) params.set('resource_type', filters.resource_type);
      if (filters?.user_id) params.set('user_id', filters.user_id);
      if (filters?.offset !== undefined) params.set('offset', String(filters.offset));
      if (filters?.limit !== undefined) params.set('limit', String(filters.limit));

      const qs = params.toString();
      const url = `/api/audit/events${qs ? `?${qs}` : ''}`;
      const response = await apiClient.client.get(url);
      return response.data;
    },
    staleTime: 30_000,
  });
}

/**
 * Fetch the current user's own activity log.
 */
export function useMyActivity(offset = 0, limit = 50) {
  const apiClient = getAPIClient(config.apiURL);

  return useQuery<AuditEventsResponse>({
    queryKey: ['my-activity', offset, limit],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (offset) params.set('offset', String(offset));
      if (limit !== 50) params.set('limit', String(limit));

      const qs = params.toString();
      const url = `/api/audit/my-activity${qs ? `?${qs}` : ''}`;
      const response = await apiClient.client.get(url);
      return response.data;
    },
    staleTime: 30_000,
  });
}
