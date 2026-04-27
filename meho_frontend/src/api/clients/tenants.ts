// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenants domain client (superadmin-only tenant management).
 *
 * Wraps `/api/tenants/*`: list/get/create/update, plus soft-disable and
 * enable. Tenant *context switching* (the `X-Acting-As-Tenant` header) lives
 * on the shared transport -- see `setTenantContext` in `./transport`.
 *
 * Migrated from `lib/api-client.ts` in Phase 4 (#350).
 */
import type { AxiosInstance } from 'axios';
import type {
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
} from '../types';
import { getTransport } from './transport';

export function createTenantsClient(transport: AxiosInstance) {
  return {
    async listTenants(includeInactive: boolean = false): Promise<TenantListResponse> {
      const params = new URLSearchParams();
      if (includeInactive) params.set('include_inactive', 'true');

      const response = await transport.get<TenantListResponse>(
        `/api/tenants?${params.toString()}`,
      );
      return response.data;
    },

    async getTenant(tenantId: string): Promise<Tenant> {
      const response = await transport.get<Tenant>(`/api/tenants/${tenantId}`);
      return response.data;
    },

    async createTenant(request: CreateTenantRequest): Promise<Tenant> {
      const response = await transport.post<Tenant>('/api/tenants', request);
      return response.data;
    },

    async updateTenant(tenantId: string, request: UpdateTenantRequest): Promise<Tenant> {
      const response = await transport.patch<Tenant>(`/api/tenants/${tenantId}`, request);
      return response.data;
    },

    async disableTenant(tenantId: string): Promise<Tenant> {
      const response = await transport.post<Tenant>(`/api/tenants/${tenantId}/disable`);
      return response.data;
    },

    async enableTenant(tenantId: string): Promise<Tenant> {
      const response = await transport.post<Tenant>(`/api/tenants/${tenantId}/enable`);
      return response.data;
    },
  };
}

let tenantsClient: ReturnType<typeof createTenantsClient> | null = null;

export function getTenantsClient(): ReturnType<typeof createTenantsClient> {
  if (!tenantsClient) {
    tenantsClient = createTenantsClient(getTransport());
  }
  return tenantsClient;
}
