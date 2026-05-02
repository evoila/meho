// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for the shared HTTP transport module.
 *
 * This module owns the cross-cutting state every domain client reads via
 * the request interceptor: auth token, tenant context, and the Axios
 * singleton itself. These tests target that state-management contract.
 *
 * Under the #263 refactor this file replaces the `MEHOAPIClient`-level
 * token/tenant tests that used to live in `lib/__tests__/api-client.test.ts`.
 *
 * Also covers `bootstrapTransport` — the explicit app-startup entrypoint
 * that locks the axios `baseURL` before any component renders. Regression
 * guard for https://github.com/evoila-bosnia/MEHO.X/pull/399#discussion_r3120249024:
 * without the bootstrap, any `getConnectorsClient()` / `getChatClient()` /
 * `getKnowledgeClient()` call fired during initial render would pin the
 * transport to the hardcoded localhost default instead of the
 * runtime-injected `config.apiURL`.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import type { InternalAxiosRequestConfig } from 'axios';
import {
  bootstrapTransport,
  clearAuthToken,
  clearTenantContext,
  createTransport,
  getAuthToken,
  getTransport,
  resetTransport,
  setAuthToken,
  setTenantContext,
} from '../transport';
import { getConnectorsClient } from '../connectors';
import { getChatClient } from '../chat';
import { getKnowledgeClient } from '../knowledge';

const RUNTIME_URL = 'https://runtime.example/api';
const DEFAULT_URL = 'http://127.0.0.1:8000';

describe('transport', () => {
  beforeEach(() => {
    resetTransport();
  });

  describe('transport factory', () => {
    it('createTransport returns a fresh Axios instance with the configured baseURL', () => {
      const instance = createTransport('http://custom-url:9000');
      expect(instance).toBeDefined();
      expect(instance.defaults.baseURL).toBe('http://custom-url:9000');
    });

    it('createTransport falls back to the default baseURL', () => {
      const instance = createTransport();
      expect(instance.defaults.baseURL).toBe(DEFAULT_URL);
    });
  });

  describe('bootstrapTransport', () => {
    it('locks the singleton to the runtime baseURL when called before any accessor', () => {
      bootstrapTransport(RUNTIME_URL);
      expect(getTransport().defaults.baseURL).toBe(RUNTIME_URL);
    });

    it('is idempotent when called again with the same URL', () => {
      const first = bootstrapTransport(RUNTIME_URL);
      const second = bootstrapTransport(RUNTIME_URL);
      expect(second).toBe(first);
      expect(getTransport().defaults.baseURL).toBe(RUNTIME_URL);
    });

    it('throws when called a second time with a different URL', () => {
      bootstrapTransport(RUNTIME_URL);
      expect(() => bootstrapTransport('https://other.example')).toThrow(
        /bootstrapTransport called twice with different baseURL/,
      );
    });

    it('propagates the baseURL to every domain-client accessor', () => {
      bootstrapTransport(RUNTIME_URL);

      // Invoking accessors must not swap the URL out from under us — this
      // is the regression Copilot flagged on #399.
      void getConnectorsClient();
      void getChatClient();
      void getKnowledgeClient();
      expect(getTransport().defaults.baseURL).toBe(RUNTIME_URL);
    });
  });

  describe('singleton pattern', () => {
    it('getTransport returns the same instance on subsequent calls', () => {
      const first = getTransport();
      const second = getTransport();
      expect(first).toBe(second);
    });

    it('getTransport returns a new instance after resetTransport', () => {
      const first = getTransport();
      resetTransport();
      const second = getTransport();
      expect(first).not.toBe(second);
    });

    it('getTransport respects a custom baseURL on first construction', () => {
      const instance = getTransport('http://singleton:8080');
      expect(instance.defaults.baseURL).toBe('http://singleton:8080');
    });

    it('getTransport ignores the baseURL arg once the singleton is alive', () => {
      const first = getTransport('http://first:8000');
      const second = getTransport('http://second:9000');
      expect(first).toBe(second);
      expect(second.defaults.baseURL).toBe('http://first:8000');
    });

    it('falls back to the hardcoded default when no bootstrap happened', () => {
      // Pre-bootstrap behavior stays working for the `MEHOAPIClient` facade
      // path (which passes an explicit baseURL) and for the test harness.
      // Production paths must go through `bootstrapTransport`.
      expect(getTransport().defaults.baseURL).toBe(DEFAULT_URL);
    });
  });

  describe('auth token state', () => {
    it('getAuthToken returns null by default', () => {
      expect(getAuthToken()).toBeNull();
    });

    it('setAuthToken stores the token', () => {
      setAuthToken('test-token');
      expect(getAuthToken()).toBe('test-token');
    });

    it('clearAuthToken resets the token to null', () => {
      setAuthToken('test-token');
      expect(getAuthToken()).toBe('test-token');

      clearAuthToken();
      expect(getAuthToken()).toBeNull();
    });

    it('setAuthToken replaces the existing token', () => {
      setAuthToken('token-1');
      setAuthToken('token-2');
      expect(getAuthToken()).toBe('token-2');
    });
  });

  describe('tenant context state (TASK-140 Phase 2)', () => {
    // Drive the request interceptor so we can observe whether the
    // X-Acting-As-Tenant header is attached under the current state.
    async function runRequestInterceptor(): Promise<Record<string, string>> {
      const instance = createTransport();
      const interceptor =
        // Axios exposes interceptor handlers on `.handlers` at runtime.
        (instance.interceptors.request as unknown as {
          handlers: Array<{
            fulfilled?: (config: InternalAxiosRequestConfig) => InternalAxiosRequestConfig;
          }>;
        }).handlers[0];

      const initial = {
        headers: {} as Record<string, string>,
      } as unknown as InternalAxiosRequestConfig;

      if (!interceptor.fulfilled) {
        throw new Error(
          'transport request interceptor has no fulfilled handler — transport.ts changed?',
        );
      }

      const out = interceptor.fulfilled(initial);
      return (out.headers as unknown) as Record<string, string>;
    }

    it('does not attach X-Acting-As-Tenant by default', async () => {
      const headers = await runRequestInterceptor();
      expect(headers['X-Acting-As-Tenant']).toBeUndefined();
    });

    it('setTenantContext causes the interceptor to attach the header', async () => {
      setTenantContext('acme-tenant');
      const headers = await runRequestInterceptor();
      expect(headers['X-Acting-As-Tenant']).toBe('acme-tenant');
    });

    it('clearTenantContext removes the header from subsequent requests', async () => {
      setTenantContext('acme-tenant');
      clearTenantContext();
      const headers = await runRequestInterceptor();
      expect(headers['X-Acting-As-Tenant']).toBeUndefined();
    });

    it('supports switching between tenants mid-session', async () => {
      setTenantContext('tenant-1');
      let headers = await runRequestInterceptor();
      expect(headers['X-Acting-As-Tenant']).toBe('tenant-1');

      setTenantContext('tenant-2');
      headers = await runRequestInterceptor();
      expect(headers['X-Acting-As-Tenant']).toBe('tenant-2');
    });
  });

  describe('resetTransport', () => {
    it('clears auth token, tenant context, and the singleton itself', () => {
      setAuthToken('token');
      setTenantContext('tenant-1');
      const before = getTransport();

      resetTransport();

      expect(getAuthToken()).toBeNull();
      expect(getTransport()).not.toBe(before);
    });

    it('unlocks the singleton so a subsequent bootstrap takes effect', () => {
      bootstrapTransport(RUNTIME_URL);
      resetTransport();
      bootstrapTransport('https://fresh.example');
      expect(getTransport().defaults.baseURL).toBe('https://fresh.example');
    });
  });
});
