// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Hook for fetching team sessions (group/tenant visibility)
 *
 * Phase 38: Group Session Foundation
 * Polls the team sessions endpoint to keep the Team tab updated
 * with session statuses and pending approval counts.
 *
 * Accepts `enabled` param so callers can gate on license edition --
 * the endpoint is not registered in community mode.
 */
import { useQuery } from '@tanstack/react-query';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';

export function useTeamSessions(enabled = true) {
  const apiClient = getAPIClient(config.apiURL);

  return useQuery({
    queryKey: ['team-sessions'],
    queryFn: () => apiClient.listTeamSessions(),
    enabled,
    refetchInterval: 15000,
  });
}
