// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useKnowledgeTree Hook
 *
 * Fetches the knowledge tree hierarchy from the backend.
 * Returns Global > Connector Type > Connector Instance structure
 * for the KnowledgePage tree view.
 */
import { useQuery } from '@tanstack/react-query';
import { getAPIClient } from '../../../lib/api-client';
import { config } from '../../../lib/config';
import type { KnowledgeTreeResponse } from '../../../api/types/knowledge';

export function useKnowledgeTree() {
  const apiClient = getAPIClient(config.apiURL);

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['knowledge-tree'],
    queryFn: (): Promise<KnowledgeTreeResponse> => apiClient.getKnowledgeTree(),
    staleTime: 30000,
    refetchInterval: 30000,
  });

  return {
    tree: data ?? null,
    isLoading,
    error,
    refetch,
  };
}
