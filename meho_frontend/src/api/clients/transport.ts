// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Shared HTTP transport for the domain-split API clients.
 *
 * Owns every cross-cutting concern that every domain client needs:
 *   - The configured `AxiosInstance` (base URL, timeout, JSON headers).
 *   - Request interceptor: attaches `Authorization: Bearer <token>` and the
 *     superadmin `X-Acting-As-Tenant` header.
 *   - Response interceptor: the 401 retry queue plus `APIError` shaping.
 *   - Module-scoped auth/tenant state so the interceptors see every
 *     domain client's token and tenant context without per-instance plumbing.
 *
 * Also exposes the session-expiry extension points used by the
 * `AuthContext` provider and the SSE hooks (they can't reach React
 * context directly, so they go through these module-level callbacks).
 *
 * ## Contract: module-scoped state, not per-instance
 *
 * All the cross-cutting state — `authToken`, `tenantContext`, `refreshTokenFn`,
 * `sessionExpiredCallback`, `isRefreshing`, `failedQueue`, `transportSingleton` —
 * lives at module scope, not on any per-instance object. That means:
 *
 *   - Calling {@link createTransport} twice does **not** give you two
 *     independent transports for auth/tenant/refresh purposes; each call
 *     returns a fresh `AxiosInstance`, but the interceptor stack still
 *     reads the shared module-level state.
 *   - Production uses a single shared transport via {@link getTransport},
 *     so this is invisible at runtime.
 *   - Tests that mutate token or tenant state **must** call
 *     {@link resetTransport} between cases, and tests that want to
 *     verify per-baseURL `AxiosInstance` behavior must reset the
 *     singleton before constructing a new instance.
 */
import axios, {
  type AxiosInstance,
  type AxiosError,
  type InternalAxiosRequestConfig,
} from 'axios';
import type { APIError } from '../types';

// ---------------------------------------------------------------------------
// 401 Retry Queue (module-scoped, shared across every transport instance)
// ---------------------------------------------------------------------------

interface FailedRequest {
  resolve: (token: string) => void;
  reject: (error: unknown) => void;
}

let isRefreshing = false;
let failedQueue: FailedRequest[] = [];
let refreshTokenFn: (() => Promise<string | null>) | null = null;
let sessionExpiredCallback: (() => void) | null = null;

function processQueue(error: unknown, token: string | null = null): void {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else if (token) {
      prom.resolve(token);
    }
  });
  failedQueue = [];
}

/** Register the token-refresh function (called by `AuthProvider`). */
export function setRefreshTokenFn(fn: () => Promise<string | null>): void {
  refreshTokenFn = fn;
}

/** Register a callback invoked when all refresh attempts fail. */
export function onSessionExpired(fn: () => void): void {
  sessionExpiredCallback = fn;
}

/**
 * Trigger the session-expired callback from SSE hooks.
 *
 * SSE streams (`useSessionEvents`, `useChatStreaming`) cannot reach React
 * context, so they call this to surface `SessionExpiredModal`.
 */
export function triggerSessionExpired(): void {
  sessionExpiredCallback?.();
}

// ---------------------------------------------------------------------------
// Auth token (module-scoped, read by the request interceptor)
// ---------------------------------------------------------------------------

let authToken: string | null = null;

export function setAuthToken(token: string): void {
  authToken = token;
}

export function clearAuthToken(): void {
  authToken = null;
}

export function getAuthToken(): string | null {
  return authToken;
}

// ---------------------------------------------------------------------------
// Tenant context (TASK-140 Phase 2 — X-Acting-As-Tenant for superadmins)
// ---------------------------------------------------------------------------

let tenantContext: string | null = null;

/**
 * Set the tenant context for superadmin operations. All subsequent requests
 * will include the `X-Acting-As-Tenant` header until {@link clearTenantContext}
 * is called.
 */
export function setTenantContext(tenantId: string): void {
  tenantContext = tenantId;
}

export function clearTenantContext(): void {
  tenantContext = null;
}

// ---------------------------------------------------------------------------
// Transport factory + singleton
// ---------------------------------------------------------------------------

const DEFAULT_BASE_URL = 'http://127.0.0.1:8000';
// 5 minutes — some OpenAPI / WSDL ingestion calls are genuinely long-running.
const DEFAULT_TIMEOUT_MS = 300_000;

/**
 * Build a fresh, fully-configured `AxiosInstance` with the interceptor stack.
 *
 * Most callers should use {@link getTransport} (singleton). `createTransport`
 * gives you a new `AxiosInstance` (different `baseURL`, separate axios
 * request lifecycle), but the interceptor stack still reads the
 * **module-level** auth token, tenant context, refresh-token registration,
 * and session-expiry callback — so two transports share that state.
 *
 * For test isolation pair with {@link resetTransport}.
 */
export function createTransport(baseURL?: string): AxiosInstance {
  const resolvedBaseURL = baseURL ?? DEFAULT_BASE_URL;
  const instance = axios.create({
    baseURL: resolvedBaseURL,
    timeout: DEFAULT_TIMEOUT_MS,
    headers: {
      'Content-Type': 'application/json',
    },
  });

  instance.interceptors.request.use((config) => {
    if (authToken) {
      config.headers.Authorization = `Bearer ${authToken}`;
    }
    if (tenantContext) {
      config.headers['X-Acting-As-Tenant'] = tenantContext;
    }
    return config;
  });

  instance.interceptors.response.use(
    (response) => response,
    async (error: AxiosError) => {
      // NOSONAR (cognitive complexity) — 401 retry + error shaping in one place
      const originalRequest = error.config as
        | (InternalAxiosRequestConfig & { _retry?: boolean })
        | undefined;

      if (
        error.response?.status === 401 &&
        originalRequest &&
        !originalRequest._retry &&
        !originalRequest.url?.includes('/auth/') &&
        !originalRequest.url?.includes('/realms/')
      ) {
        if (isRefreshing) {
          return new Promise<string>((resolve, reject) => {
            failedQueue.push({ resolve, reject });
          }).then((newToken) => {
            originalRequest.headers.Authorization = `Bearer ${newToken}`;
            return instance(originalRequest);
          });
        }

        originalRequest._retry = true;
        isRefreshing = true;

        try {
          if (!refreshTokenFn) {
            throw new Error('No refresh token function registered');
          }
          const newToken = await refreshTokenFn();
          if (newToken) {
            authToken = newToken;
            processQueue(null, newToken);
            originalRequest.headers.Authorization = `Bearer ${newToken}`;
            return instance(originalRequest);
          }
          processQueue(new Error('Token refresh failed'));
          sessionExpiredCallback?.();
          return Promise.reject(error);
        } catch (refreshError) {
          processQueue(refreshError);
          sessionExpiredCallback?.();
          return Promise.reject(refreshError);
        } finally {
          isRefreshing = false;
        }
      }

      if (error.response) {
        const data = error.response.data as Record<string, unknown> | undefined;
        let errorMessage = 'An error occurred';

        const dataObj = data;
        const detail = dataObj?.detail;
        const errorField = dataObj?.error as Record<string, unknown> | undefined;

        if (typeof detail === 'string') {
          errorMessage = detail;
        } else if (
          typeof detail === 'object' &&
          detail !== null &&
          typeof (detail as Record<string, unknown>).message === 'string'
        ) {
          errorMessage = (detail as Record<string, unknown>).message as string;
        } else if (typeof errorField?.message === 'string') {
          errorMessage = errorField.message as string;
        } else if (typeof dataObj?.message === 'string') {
          errorMessage = dataObj.message as string;
        }

        const apiError: APIError =
          errorField && typeof errorField.message === 'string' && typeof errorField.type === 'string'
            ? {
                message: errorField.message as string,
                type: errorField.type as string,
                status_code: (errorField.status_code as number) ?? error.response.status,
              }
            : {
                message: errorMessage,
                type: 'UnknownError',
                status_code: error.response.status,
              };
        throw apiError;
      }

      if (error.request) {
        if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
          throw new Error(
            `Request to ${instance.defaults.baseURL} timed out after ${instance.defaults.timeout}ms. The operation may be taking longer than expected.`,
          );
        }
        throw new Error(
          `Cannot connect to API at ${instance.defaults.baseURL}. Make sure the backend is running.`,
        );
      }

      throw error;
    },
  );

  return instance;
}

let transportSingleton: AxiosInstance | null = null;

/**
 * Explicitly bootstrap the shared transport with the runtime `baseURL`.
 *
 * This **must** be called exactly once at app startup (from `main.tsx`)
 * *before* any component renders or any domain-client accessor runs.
 *
 * Rationale: `getTransport()` is a first-caller-wins singleton. Without an
 * explicit bootstrap, whoever calls `getChatClient()` / `getConnectorsClient()`
 * / etc. first locks the transport to the {@link DEFAULT_BASE_URL} fallback
 * (`http://127.0.0.1:8000`) — which silently breaks every deployment where
 * `config.apiURL` points somewhere else. Since domain-client accessors are
 * used inside `useMemo` / render bodies, they can fire before any other
 * priming happens, so wiring `bootstrapTransport(config.apiURL)` before
 * `createRoot(...).render(...)` makes the load order bulletproof.
 *
 * Idempotent: repeat calls with the *same* `baseURL` are no-ops. A repeat
 * call with a *different* `baseURL` throws — that almost always means two
 * bootstraps racing, which would produce an inconsistent singleton.
 */
export function bootstrapTransport(baseURL: string): AxiosInstance {
  if (transportSingleton) {
    if (transportSingleton.defaults.baseURL !== baseURL) {
      throw new Error(
        `bootstrapTransport called twice with different baseURL ` +
          `(existing="${transportSingleton.defaults.baseURL}", ` +
          `requested="${baseURL}"). ` +
          `Bootstrap exactly once at app startup.`,
      );
    }
    return transportSingleton;
  }
  transportSingleton = createTransport(baseURL);
  return transportSingleton;
}

/**
 * Get (or lazily create) the shared transport singleton.
 *
 * Prefer {@link bootstrapTransport} at app startup and then call
 * `getTransport()` without arguments everywhere else. If you pass a
 * `baseURL` here and the singleton already exists it is silently ignored
 * (first-caller-wins semantics preserved for backwards compatibility with
 * the pre-split singleton accessor).
 */
export function getTransport(baseURL?: string): AxiosInstance {
  if (!transportSingleton) {
    transportSingleton = createTransport(baseURL);
  }
  return transportSingleton;
}

/**
 * Reset every piece of module state managed by this file.
 *
 * Intended for unit tests — clears the transport singleton, the auth/tenant
 * state, the refresh queue, and the session-expiry callback.
 */
export function resetTransport(): void {
  transportSingleton = null;
  authToken = null;
  tenantContext = null;
  refreshTokenFn = null;
  sessionExpiredCallback = null;
  isRefreshing = false;
  failedQueue = [];
}
