// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tenant API Types
 * 
 * TypeScript types for tenant management API.
 * Based on backend schemas in meho_app/api/routes_tenants.py
 * 
 * TASK-139 Phase 8: Added email_domains for tenant discovery
 */

/**
 * Subscription tier options
 */
export type SubscriptionTier = 'free' | 'pro' | 'enterprise';

/**
 * Tenant response from API
 */
export interface Tenant {
  tenant_id: string;
  display_name: string | null;
  is_active: boolean;
  subscription_tier: SubscriptionTier;
  
  // Email domains for tenant discovery (TASK-139 Phase 8)
  email_domains: string[];
  
  // Quotas
  max_connectors: number | null;
  max_knowledge_chunks: number | null;
  max_workflows_per_day: number | null;
  
  // LLM settings
  installation_context: string | null;
  model_override: string | null;
  temperature_override: number | null;
  features: Record<string, unknown>;
  
  // Metadata
  created_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
  
  // Keycloak realm status
  keycloak_realm_enabled: boolean | null;
}

/**
 * Response for tenant list endpoint
 */
export interface TenantListResponse {
  tenants: Tenant[];
  total: number;
}

/**
 * Request to create a new tenant
 */
export interface CreateTenantRequest {
  tenant_id: string;
  display_name: string;
  subscription_tier?: SubscriptionTier;
  
  // Email domains for tenant discovery (TASK-139 Phase 8)
  email_domains?: string[];
  
  // Optional quotas
  max_connectors?: number;
  max_knowledge_chunks?: number;
  max_workflows_per_day?: number;
  
  // Optional LLM settings
  installation_context?: string;
  model_override?: string;
  temperature_override?: number;
  features?: Record<string, unknown>;
  
  // Keycloak realm creation
  create_keycloak_realm?: boolean;
}

/**
 * Request to update a tenant
 */
export interface UpdateTenantRequest {
  display_name?: string;
  subscription_tier?: SubscriptionTier;
  is_active?: boolean;
  
  // Email domains for tenant discovery (TASK-139 Phase 8)
  email_domains?: string[];
  
  // Quotas
  max_connectors?: number;
  max_knowledge_chunks?: number;
  max_workflows_per_day?: number;
  
  // LLM settings
  installation_context?: string;
  model_override?: string;
  temperature_override?: number;
  features?: Record<string, unknown>;
}

/**
 * Request to discover tenant by email domain
 * TASK-139 Phase 8
 */
export interface DiscoverTenantRequest {
  email: string;
}

/**
 * Response from tenant discovery endpoint
 * TASK-139 Phase 8
 */
export interface DiscoverTenantResponse {
  tenant_id: string;
  realm: string;
  display_name: string;
  keycloak_url: string;
}

