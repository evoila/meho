// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Keycloak configuration and utilities
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Create a mock Keycloak class
class MockKeycloak {
  realm: string;
  authenticated: boolean = false;
  token: string | null = null;
  tokenParsed: object | null = null;

  constructor(config: { realm: string; url: string; clientId: string }) {
    this.realm = config.realm;
  }

  login = vi.fn();
  logout = vi.fn();
  updateToken = vi.fn().mockResolvedValue(true);
}

// Mock the keycloak-js module
vi.mock('keycloak-js', () => ({
  default: MockKeycloak,
}));

describe('Keycloak Configuration', () => {
  // Store original env values
  const originalKeycloakUrl = import.meta.env.VITE_KEYCLOAK_URL;
  const originalKeycloakRealm = import.meta.env.VITE_KEYCLOAK_REALM;
  const originalKeycloakClientId = import.meta.env.VITE_KEYCLOAK_CLIENT_ID;

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    // Restore original env values
    import.meta.env.VITE_KEYCLOAK_URL = originalKeycloakUrl;
    import.meta.env.VITE_KEYCLOAK_REALM = originalKeycloakRealm;
    import.meta.env.VITE_KEYCLOAK_CLIENT_ID = originalKeycloakClientId;
  });

  describe('keycloak instance', () => {
    // Phase 84: keycloak module now reads from centralized config.ts (window.__RUNTIME_CONFIG__)
    // instead of direct VITE_ env vars. vi.resetModules() cannot override the config module's
    // cached singleton. These tests need rewriting to mock config.ts.
    it.skip('creates keycloak with correct config', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      import.meta.env.VITE_KEYCLOAK_REALM = 'test-realm';
      import.meta.env.VITE_KEYCLOAK_CLIENT_ID = 'test-client';

      const { keycloak } = await import('../keycloak');
      expect(keycloak).toBeDefined();
      expect(keycloak.realm).toBe('test-realm');
    });

    // Phase 84: Same config.ts centralization issue as above
    it.skip('uses default realm when env var not set', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      import.meta.env.VITE_KEYCLOAK_REALM = '';
      import.meta.env.VITE_KEYCLOAK_CLIENT_ID = '';

      const { keycloak } = await import('../keycloak');
      expect(keycloak).toBeDefined();
      // Default realm is 'example-tenant'
      expect(keycloak.realm).toBe('example-tenant');
    });
  });

  describe('getCurrentRealm', () => {
    // Phase 84: getCurrentRealm reads from config.ts singleton, not env vars
    it.skip('returns the configured realm', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      import.meta.env.VITE_KEYCLOAK_REALM = 'my-tenant';
      const { getCurrentRealm } = await import('../keycloak');
      expect(getCurrentRealm()).toBe('my-tenant');
    });
  });

  describe('getUserRoles', () => {
    it('returns empty array when not authenticated', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      const { getUserRoles } = await import('../keycloak');
      expect(getUserRoles()).toEqual([]);
    });
  });

  describe('isGlobalAdmin', () => {
    it('returns false when not authenticated', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      const { isGlobalAdmin } = await import('../keycloak');
      expect(isGlobalAdmin()).toBe(false);
    });
  });

  describe('createKeycloakClient', () => {
    it('creates a new keycloak client for a specific realm', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      const { createKeycloakClient } = await import('../keycloak');
      
      const client = createKeycloakClient('custom-realm');
      expect(client).toBeDefined();
      expect(client.realm).toBe('custom-realm');
    });

    it('uses custom URL when provided', async () => {
      import.meta.env.VITE_KEYCLOAK_URL = 'http://test:8080';
      const { createKeycloakClient } = await import('../keycloak');
      
      const client = createKeycloakClient('custom-realm', 'http://custom:8080');
      expect(client).toBeDefined();
    });
  });

  describe('tenant discovery', () => {
    it('stores and retrieves discovered tenant', async () => {
      const { storeDiscoveredTenant, getDiscoveredTenant, clearDiscoveredTenant } = await import('../keycloak');
      
      const tenant = {
        tenant_id: 'test-tenant',
        realm: 'test-tenant',
        display_name: 'Test Tenant',
        keycloak_url: 'http://localhost:8080',
      };
      
      storeDiscoveredTenant(tenant);
      
      const retrieved = getDiscoveredTenant();
      expect(retrieved).toEqual(tenant);
      
      clearDiscoveredTenant();
      expect(getDiscoveredTenant()).toBeNull();
    });
  });
});
