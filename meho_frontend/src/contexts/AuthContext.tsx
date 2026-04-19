// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Unified Authentication Context
 *
 * Consolidates the old KeycloakContext + AuthContext into a single provider.
 * Uses keycloak-js directly (no @react-keycloak/web wrapper).
 *
 * Token management:
 * - Tokens are stored in React state + keycloak-js instance (memory only)
 * - NO localStorage / sessionStorage for JWT
 * - API client is updated immediately on token change
 * - 401 retry queue is wired via setRefreshTokenFn
 *
 * Session lifecycle:
 * - Silent SSO on page refresh (check-sso + PKCE)
 * - Proactive token refresh every 30s
 * - onTokenExpired callback for reactive refresh
 * - SessionExpiredModal on refresh failure (no hard logout)
 * - Popup re-auth flow for expired sessions
 */
import {
  createContext,
  useContext,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import Keycloak from 'keycloak-js';
import {
  keycloak as defaultKeycloak,
  getDiscoveredTenant,
  clearDiscoveredTenant,
  createKeycloakClient,
} from '@/lib/keycloak';
import { getAPIClient, setRefreshTokenFn, onSessionExpired as setSessionExpiredCallback } from '@/lib/api-client';
import { config } from '@/lib/config';
import { AuthLoadingScreen } from '@/components/AuthLoadingScreen';
import { SessionExpiredModal } from '@/components/SessionExpiredModal';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AuthUser {
  sub: string;
  tenant_id: string;
  roles: string[];
  groups: string[];
  exp: number;
  iat: number;
  email?: string;
  name?: string;
  isGlobalAdmin: boolean;
}

export interface AuthContextType {
  /** True once keycloak.init() has resolved */
  isInitialized: boolean;
  /** Whether the user has an active authenticated session */
  isAuthenticated: boolean;
  /** Access token held in memory only */
  token: string | null;
  /** Decoded user from the access token */
  user: AuthUser | null;
  /** Redirect to Keycloak login */
  login: () => void;
  /** End session and redirect to login */
  logout: () => void;
  /**
   * Attempt to refresh the access token.
   * Returns the new token on success, null on failure.
   */
  refreshToken: (minValidity?: number) => Promise<string | null>;
  /** Convenience: user.isGlobalAdmin */
  isGlobalAdmin: boolean;
  /** Current Keycloak realm (tenant ID) */
  tenantId: string;
  /** User roles from token */
  roles: string[];
  /** User ID (sub claim) */
  userId: string;
  /** User email */
  email: string;
  /** User display name */
  userName: string;
  /** True when silent token refresh has failed */
  sessionExpired: boolean;
  /** Trigger popup re-auth flow */
  handleReAuth: () => Promise<void>;
  /**
   * Back-compat alias for isInitialized inverted.
   * Components that used `isLoading` from the old AuthContext still work.
   */
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Keycloak init options -- shared for initial boot and re-init after popup */
const INIT_OPTIONS: Keycloak.KeycloakInitOptions = {
  onLoad: 'check-sso',
  silentCheckSsoRedirectUri:
    typeof globalThis.window !== 'undefined'
      ? `${globalThis.location.origin}/silent-check-sso.html`
      : undefined,
  pkceMethod: 'S256',
  checkLoginIframe: false,
};

/** Proactive refresh interval (ms) */
const TOKEN_REFRESH_INTERVAL = 30_000;
/** Minimum validity before proactive refresh (seconds) */
const MIN_TOKEN_VALIDITY = 60;

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: Readonly<{ children: ReactNode }>) {
  // Keycloak instance ref -- avoids stale closures in callbacks
  const kcRef = useRef<Keycloak | null>(null);

  const [isInitialized, setIsInitialized] = useState(false);
  const [currentToken, setCurrentToken] = useState<string | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);

  // Track last synced token to avoid unnecessary updates
  const lastSyncedTokenRef = useRef<string | null>(null);

  // SSE activity tracking -- when active, proactively refresh sooner
  const sseActiveRef = useRef(false);

  // Guard against React StrictMode double-mount: keycloak-js init must
  // run exactly once because two concurrent inits racing to process the
  // same OAuth callback code flood the browser with navigation requests,
  // triggering Chrome's navigation throttle and freezing the page.
  const initStartedRef = useRef(false);

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  /** Sync a new token to React state + API client (memory only, no storage) */
  const syncToken = useCallback((token: string | null) => {
    if (token === lastSyncedTokenRef.current) return;
    lastSyncedTokenRef.current = token;
    setCurrentToken(token);

    const client = getAPIClient(config.apiURL);
    if (token) {
      client.setToken(token);
    } else {
      client.clearToken();
    }
  }, []);

  // -----------------------------------------------------------------------
  // Keycloak init
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (initStartedRef.current) return;
    initStartedRef.current = true;

    async function initKeycloak() {
      const stored = getDiscoveredTenant();
      let kc: Keycloak;

      if (stored) {
        console.debug('[Auth] Using discovered tenant:', stored.tenant_id);
        kc = createKeycloakClient(stored.realm, stored.keycloak_url);
      } else {
        kc = defaultKeycloak;
      }

      kcRef.current = kc;

      kc.onTokenExpired = () => {
        console.debug('[Auth] Token expired, refreshing...');
        kc.updateToken(30).then((refreshed) => {
          if (refreshed) {
            console.debug('[Auth] Token refreshed after expiry');
            syncToken(kc.token || null);
          }
        }).catch(() => {
          console.warn('[Auth] Token refresh after expiry failed');
          setSessionExpired(true);
        });
      };

      kc.onAuthRefreshSuccess = () => {
        console.debug('[Auth] Token refresh successful');
        syncToken(kc.token || null);
      };

      kc.onAuthRefreshError = () => {
        console.warn('[Auth] Token refresh failed');
        setSessionExpired(true);
      };

      kc.onAuthLogout = () => {
        console.debug('[Auth] User logged out');
        syncToken(null);
      };

      try {
        const authenticated = await kc.init(INIT_OPTIONS);

        if (authenticated && kc.token) {
          console.debug('[Auth] Authenticated via silent SSO');
          syncToken(kc.token);
          clearDiscoveredTenant();
        } else {
          console.debug('[Auth] Not authenticated after init');
          syncToken(null);
        }
      } catch (error) {
        console.error('[Auth] Keycloak init error:', error);
        syncToken(null);
      }

      setIsInitialized(true);
    }

    initKeycloak();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -----------------------------------------------------------------------
  // Wire 401 retry queue
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (!isInitialized) return;

    // Register the refresh function used by the axios 401 interceptor
    setRefreshTokenFn(async () => {
      const kc = kcRef.current;
      if (!kc) return null;
      try {
        const refreshed = await kc.updateToken(5);
        const newToken = kc.token || null;
        if (refreshed) {
          syncToken(newToken);
        }
        return newToken;
      } catch {
        return null;
      }
    });

    // Register the session expired callback used when all retries fail
    setSessionExpiredCallback(() => {
      setSessionExpired(true);
    });
  }, [isInitialized, syncToken]);

  // -----------------------------------------------------------------------
  // Proactive token refresh
  // -----------------------------------------------------------------------

  useEffect(() => {
    const kc = kcRef.current;
    if (!isInitialized || !kc?.authenticated) return;

    const interval = setInterval(async () => {
      try {
        const minValidity = sseActiveRef.current ? 90 : MIN_TOKEN_VALIDITY;
        const refreshed = await kc.updateToken(minValidity);
        if (refreshed) {
          console.debug('[Auth] Token proactively refreshed');
          syncToken(kc.token || null);
        }
      } catch {
        console.warn('[Auth] Proactive token refresh failed');
        // Don't set sessionExpired here -- let the 401 handler deal with it
      }
    }, TOKEN_REFRESH_INTERVAL);

    return () => clearInterval(interval);
  }, [isInitialized, syncToken]);

  // -----------------------------------------------------------------------
  // Actions
  // -----------------------------------------------------------------------

  const login = useCallback(() => {
    kcRef.current?.login();
  }, []);

  const logout = useCallback(() => {
    clearDiscoveredTenant();
    syncToken(null);

    kcRef.current?.logout({
      redirectUri: globalThis.location.origin + '/login',
    });
  }, [syncToken]);

  const refreshToken = useCallback(
    async (minValidity: number = 30): Promise<string | null> => {
      const kc = kcRef.current;
      if (!kc) return null;

      try {
        const refreshed = await kc.updateToken(minValidity);
        const newToken = kc.token || null;
        if (refreshed) {
          syncToken(newToken);
        }
        return newToken;
      } catch (error) {
        console.error('[Auth] Failed to refresh token:', error);
        return null;
      }
    },
    [syncToken],
  );

  const handleReAuth = useCallback(async () => {
    const kc = kcRef.current;
    if (!kc) return;

    // Open Keycloak login in a popup
    const loginUrl = await kc.createLoginUrl({
      redirectUri: globalThis.location.origin + '/login-popup-callback.html',
    });

    const popup = globalThis.open(loginUrl, 'keycloak-login', 'width=500,height=650');
    if (!popup) {
      console.warn('[Auth] Popup blocked');
      return;
    }

    // Wait for the popup to post a success message or close
    await new Promise<void>((resolve) => {
      const handleMessage = (event: MessageEvent) => {
        if (
          event.origin === globalThis.location.origin &&
          event.data?.type === 'keycloak-login-success'
        ) {
          globalThis.removeEventListener('message', handleMessage);
          clearInterval(pollId);
          popup.close();
          resolve();
        }
      };

      // Also detect popup closed without success
      const pollId = setInterval(() => {
        if (popup.closed) {
          clearInterval(pollId);
          globalThis.removeEventListener('message', handleMessage);
          resolve();
        }
      }, 500);

      globalThis.addEventListener('message', handleMessage);
    });

    // Re-init keycloak with check-sso to pick up the new session
    try {
      const authenticated = await kc.init({
        ...INIT_OPTIONS,
        onLoad: 'check-sso',
      });

      if (authenticated && kc.token) {
        syncToken(kc.token);
        setSessionExpired(false);
      }
    } catch (error) {
      console.error('[Auth] Re-init after popup login failed:', error);
    }
  }, [syncToken]);

  // SSE activity marking for proactive refresh
  const markActive = useCallback(() => {
    sseActiveRef.current = true;
  }, []);
  const markIdle = useCallback(() => {
    sseActiveRef.current = false;
  }, []);

  // -----------------------------------------------------------------------
  // User derivation
  // -----------------------------------------------------------------------

  const kc = kcRef.current;
  const tokenParsed = kc?.tokenParsed || {};
  const roles: string[] = useMemo(
    () => tokenParsed.roles || tokenParsed.realm_access?.roles || [],
    [tokenParsed.roles, tokenParsed.realm_access?.roles],
  );
  const isGlobalAdmin =
    kc?.realm === 'master' && roles.includes('global_admin');
  const tenantId = kc?.realm || '';
  const userId = tokenParsed.sub || '';
  const email = tokenParsed.email || '';
  const userName =
    tokenParsed.preferred_username || tokenParsed.name || email;

  const user = useMemo((): AuthUser | null => {
    if (!kc?.authenticated) return null;
    return {
      sub: userId,
      tenant_id: tenantId,
      roles,
      groups: [],
      exp: 0, // Managed by Keycloak
      iat: 0,
      email,
      name: userName,
      isGlobalAdmin,
    };
  }, [kc?.authenticated, userId, tenantId, roles, email, userName, isGlobalAdmin]);

  // -----------------------------------------------------------------------
  // Context value
  // -----------------------------------------------------------------------

  const value: AuthContextType = useMemo(
    () => ({
      isInitialized,
      isAuthenticated: !!kc?.authenticated,
      token: currentToken,
      user,
      login,
      logout,
      refreshToken,
      isGlobalAdmin,
      tenantId,
      roles,
      userId,
      email,
      userName,
      sessionExpired,
      handleReAuth,
      isLoading: !isInitialized,
      // Expose SSE activity markers for streaming hooks
      _markActive: markActive,
      _markIdle: markIdle,
    }),
    [
      isInitialized,
      kc?.authenticated,
      currentToken,
      user,
      login,
      logout,
      refreshToken,
      isGlobalAdmin,
      tenantId,
      roles,
      userId,
      email,
      userName,
      sessionExpired,
      handleReAuth,
      markActive,
      markIdle,
    ],
  );

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (!isInitialized) {
    return <AuthLoadingScreen />;
  }

  return (
    <AuthContext.Provider value={value}>
      {children}
      {sessionExpired && (
        <SessionExpiredModal onReAuth={handleReAuth} />
      )}
    </AuthContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Hook to access the unified auth context.
 */
export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
