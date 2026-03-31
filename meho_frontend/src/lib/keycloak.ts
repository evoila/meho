// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Keycloak Configuration
 * 
 * Initializes Keycloak client for OIDC authentication.
 * Configuration is read from environment variables.
 * 
 * Keycloak is the sole authentication provider for MEHO.
 * 
 * TASK-139 Phase 8: Added support for dynamic realm initialization
 * for email-based tenant discovery.
 */
import Keycloak from 'keycloak-js';
import { config } from './config';

/**
 * Session storage key for discovered tenant
 */
export const DISCOVERED_TENANT_KEY = 'meho_discovered_tenant';

/**
 * Discovered tenant info from email discovery
 */
export interface DiscoveredTenant {
  tenant_id: string;
  realm: string;
  display_name: string;
  keycloak_url: string;
}

/**
 * Keycloak configuration from centralized config
 */
const keycloakConfig = {
  url: config.keycloak.url,
  realm: config.keycloak.realm,
  clientId: config.keycloak.clientId,
};

/**
 * Keycloak instance
 * 
 * This is the main Keycloak client used throughout the application.
 * It handles authentication, token management, and session lifecycle.
 */
export const keycloak = new Keycloak(keycloakConfig);

/**
 * Get the current realm (tenant ID)
 */
export function getCurrentRealm(): string {
  return keycloak.realm || keycloakConfig.realm;
}

/**
 * Check if user is a global admin (master realm with global_admin role)
 */
export function isGlobalAdmin(): boolean {
  if (!keycloak.authenticated || !keycloak.tokenParsed) {
    return false;
  }
  
  const isMasterRealm = keycloak.realm === 'master';
  const roles = keycloak.tokenParsed.roles || 
                keycloak.tokenParsed.realm_access?.roles || 
                [];
  
  return isMasterRealm && roles.includes('global_admin');
}

/**
 * Get user roles from token
 */
export function getUserRoles(): string[] {
  if (!keycloak.authenticated || !keycloak.tokenParsed) {
    return [];
  }
  
  // Roles can be in different locations depending on Keycloak configuration
  return keycloak.tokenParsed.roles || 
         keycloak.tokenParsed.realm_access?.roles || 
         [];
}

/**
 * Create a new Keycloak client for a specific realm
 * 
 * TASK-139 Phase 8: Used for dynamic realm initialization
 * after email-based tenant discovery.
 * 
 * @param realm - Keycloak realm name (tenant_id)
 * @param url - Optional Keycloak server URL (defaults to configured URL)
 */
export function createKeycloakClient(realm: string, url?: string): Keycloak {
  return new Keycloak({
    url: url || keycloakConfig.url,
    realm,
    clientId: keycloakConfig.clientId,
  });
}

/**
 * Store discovered tenant info in session storage
 * 
 * TASK-139 Phase 8: Used to persist tenant info across redirect to Keycloak
 */
export function storeDiscoveredTenant(tenant: DiscoveredTenant): void {
  sessionStorage.setItem(DISCOVERED_TENANT_KEY, JSON.stringify(tenant));
}

/**
 * Get discovered tenant info from session storage
 * 
 * TASK-139 Phase 8: Used to retrieve tenant info after Keycloak redirect
 */
export function getDiscoveredTenant(): DiscoveredTenant | null {
  const stored = sessionStorage.getItem(DISCOVERED_TENANT_KEY);
  if (!stored) return null;
  
  try {
    return JSON.parse(stored) as DiscoveredTenant;
  } catch {
    return null;
  }
}

/**
 * Clear discovered tenant info from session storage
 */
export function clearDiscoveredTenant(): void {
  sessionStorage.removeItem(DISCOVERED_TENANT_KEY);
}
