// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenants Hook
 * 
 * Provides tenant CRUD operations with React Query.
 * These operations are only available to global_admin users.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getAPIClient } from '@/lib/api-client';
import { config } from '@/lib/config';
import type { 
  CreateTenantRequest, 
  UpdateTenantRequest,
  Tenant,
} from '@/api/types';

const QUERY_KEY = 'tenants';

/**
 * Hook for tenant list and CRUD operations
 */
export function useTenants(includeInactive: boolean = false) {
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // List all tenants
  const {
    data,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: [QUERY_KEY, { includeInactive }],
    queryFn: () => apiClient.listTenants(includeInactive),
  });

  // Create tenant
  const createMutation = useMutation({
    mutationFn: (request: CreateTenantRequest) => apiClient.createTenant(request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY] });
    },
  });

  // Update tenant
  const updateMutation = useMutation({
    mutationFn: ({ tenantId, request }: { tenantId: string; request: UpdateTenantRequest }) =>
      apiClient.updateTenant(tenantId, request),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY] });
    },
  });

  // Disable tenant
  const disableMutation = useMutation({
    mutationFn: (tenantId: string) => apiClient.disableTenant(tenantId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY] });
    },
  });

  // Enable tenant
  const enableMutation = useMutation({
    mutationFn: (tenantId: string) => apiClient.enableTenant(tenantId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY] });
    },
  });

  return {
    tenants: data?.tenants ?? [],
    total: data?.total ?? 0,
    isLoading,
    error,
    refetch,
    createTenant: createMutation.mutateAsync,
    updateTenant: updateMutation.mutateAsync,
    disableTenant: disableMutation.mutateAsync,
    enableTenant: enableMutation.mutateAsync,
    isCreating: createMutation.isPending,
    isUpdating: updateMutation.isPending,
    isDisabling: disableMutation.isPending,
    isEnabling: enableMutation.isPending,
  };
}

/**
 * Hook for fetching a single tenant
 */
export function useTenant(tenantId: string | null) {
  const apiClient = getAPIClient(config.apiURL);

  return useQuery<Tenant>({
    queryKey: [QUERY_KEY, tenantId],
    queryFn: () => apiClient.getTenant(tenantId ?? ''),
    enabled: !!tenantId,
  });
}

