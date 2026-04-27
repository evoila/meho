// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenants Feature
 * 
 * Public exports for the tenant management feature module.
 * These operations are only available to global_admin users.
 */

// Hooks
export { useTenants, useTenant } from './hooks';

// Types
export type { TenantFilters } from './types';

// Re-export API types for convenience
export type {
  SubscriptionTier,
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
} from './types';

