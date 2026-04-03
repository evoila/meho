// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant Feature Types
 * 
 * Types specific to the tenant management feature.
 */

// Re-export API types for convenience
export type {
  SubscriptionTier,
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
} from '@/api/types';

/**
 * Filter options for tenant list
 */
export interface TenantFilters {
  includeInactive?: boolean;
  search?: string;
  subscriptionTier?: 'free' | 'pro' | 'enterprise';
}

