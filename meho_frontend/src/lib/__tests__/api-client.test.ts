// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for MEHO API Client
 * 
 * Note: These tests verify the client structure and singleton pattern.
 * Integration tests with real API are in the E2E suite (backend).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { MEHOAPIClient, getAPIClient, resetAPIClient } from '../api-client';

describe('MEHOAPIClient', () => {
  let client: MEHOAPIClient;

  beforeEach(() => {
    client = new MEHOAPIClient('http://localhost:8000');
    resetAPIClient();
  });

  describe('initialization', () => {
    it('creates client with default baseURL', () => {
      const defaultClient = new MEHOAPIClient();
      expect(defaultClient).toBeDefined();
    });

    it('creates client with custom baseURL', () => {
      const customClient = new MEHOAPIClient('http://custom-url:9000');
      expect(customClient).toBeDefined();
    });
  });

  describe('authentication', () => {
    it('sets token', () => {
      client.setToken('test-token');
      expect(client['token']).toBe('test-token');
    });

    it('clears token', () => {
      client.setToken('test-token');
      expect(client['token']).toBe('test-token');
      
      client.clearToken();
      expect(client['token']).toBeNull();
    });

    it('token is null by default', () => {
      expect(client['token']).toBeNull();
    });
  });

  describe('singleton pattern', () => {
    it('returns same instance on multiple calls', () => {
      const client1 = getAPIClient();
      const client2 = getAPIClient();

      expect(client1).toBe(client2);
    });

    it('creates new instance after reset', () => {
      const client1 = getAPIClient();
      resetAPIClient();
      const client2 = getAPIClient();

      expect(client1).not.toBe(client2);
    });

    it('uses custom baseURL if provided', () => {
      const customClient = getAPIClient('http://custom:8080');
      expect(customClient).toBeDefined();
    });
  });

  // TASK-140 Phase 2: Tenant Context Switching
  describe('tenant context', () => {
    it('tenant context is null by default', () => {
      expect(client.getTenantContext()).toBeNull();
    });

    it('sets tenant context', () => {
      client.setTenantContext('acme-tenant');
      expect(client.getTenantContext()).toBe('acme-tenant');
    });

    it('clears tenant context', () => {
      client.setTenantContext('acme-tenant');
      expect(client.getTenantContext()).toBe('acme-tenant');
      
      client.clearTenantContext();
      expect(client.getTenantContext()).toBeNull();
    });

    it('can switch between tenants', () => {
      client.setTenantContext('tenant-1');
      expect(client.getTenantContext()).toBe('tenant-1');
      
      client.setTenantContext('tenant-2');
      expect(client.getTenantContext()).toBe('tenant-2');
    });
  });
});


