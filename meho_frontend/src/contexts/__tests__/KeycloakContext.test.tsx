// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for the unified AuthContext (replaced KeycloakContext)
 *
 * Tests core behaviors: initialization, token in memory, useAuth hook.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks -- must be declared before any import that triggers module resolution
// ---------------------------------------------------------------------------

const mockKcInstance = {
  authenticated: false,
  token: null as string | null,
  tokenParsed: null as Record<string, unknown> | null,
  realm: 'test-realm',
  login: vi.fn(),
  logout: vi.fn(),
  updateToken: vi.fn().mockResolvedValue(true),
  init: vi.fn().mockResolvedValue(false),
  onTokenExpired: null as (() => void) | null,
  onAuthRefreshSuccess: null as (() => void) | null,
  onAuthRefreshError: null as (() => void) | null,
  onAuthLogout: null as (() => void) | null,
  createLoginUrl: vi.fn().mockReturnValue('https://kc/login'),
};

vi.mock('keycloak-js', () => ({
  default: vi.fn().mockImplementation(() => mockKcInstance),
}));

vi.mock('@/lib/keycloak', () => ({
  keycloak: mockKcInstance,
  getDiscoveredTenant: vi.fn().mockReturnValue(null),
  clearDiscoveredTenant: vi.fn(),
  createKeycloakClient: vi.fn().mockReturnValue(mockKcInstance),
}));

const mockSetToken = vi.fn();
const mockClearToken = vi.fn();
vi.mock('@/lib/api-client', () => ({
  getAPIClient: vi.fn().mockReturnValue({
    setToken: mockSetToken,
    clearToken: mockClearToken,
    getToken: vi.fn(),
  }),
  setRefreshTokenFn: vi.fn(),
  onSessionExpired: vi.fn(),
}));

vi.mock('@/lib/config', () => ({
  config: {
    apiURL: 'http://localhost:8000',
    keycloak: { url: 'http://localhost:8080', realm: 'test-realm', clientId: 'meho-frontend' },
  },
}));

// Mock the child components so they don't break the test
vi.mock('@/components/AuthLoadingScreen', () => ({
  AuthLoadingScreen: () => <div data-testid="loading-screen">Loading...</div>,
}));

vi.mock('@/components/SessionExpiredModal', () => ({
  SessionExpiredModal: ({ onReAuth }: { onReAuth: () => void }) => (
    <div data-testid="session-expired-modal">
      <button onClick={onReAuth}>Re-authenticate</button>
    </div>
  ),
}));

vi.mock('@/assets/meho-logo.svg', () => ({ default: 'logo.svg' }));

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockKcInstance.authenticated = false;
    mockKcInstance.token = null;
    mockKcInstance.tokenParsed = null;
    mockKcInstance.init.mockResolvedValue(false);
  });

  describe('AuthProvider', () => {
    it('shows loading screen during initialization', async () => {
      // Make init never resolve
      mockKcInstance.init.mockReturnValue(new Promise(() => {}));

      const { AuthProvider } = await import('../AuthContext');

      render(
        <AuthProvider>
          <div>Protected Content</div>
        </AuthProvider>
      );

      expect(screen.getByTestId('loading-screen')).toBeInTheDocument();
    });

    it('renders children after init completes', async () => {
      mockKcInstance.init.mockResolvedValue(false);

      const { AuthProvider } = await import('../AuthContext');

      await act(async () => {
        render(
          <AuthProvider>
            <div>Protected Content</div>
          </AuthProvider>
        );
      });

      expect(screen.getByText('Protected Content')).toBeInTheDocument();
    });
  });

  describe('useAuth', () => {
    it('provides auth state to children', async () => {
      mockKcInstance.init.mockResolvedValue(true);
      mockKcInstance.authenticated = true;
      mockKcInstance.token = 'test-token';
      mockKcInstance.tokenParsed = {
        sub: 'user-123',
        email: 'test@example.com',
        roles: ['admin'],
      };

      const { AuthProvider, useAuth } = await import('../AuthContext');

      function TestComponent() {
        const auth = useAuth();
        return (
          <div>
            <span data-testid="authenticated">{auth.isAuthenticated ? 'yes' : 'no'}</span>
            <span data-testid="token">{auth.token || 'none'}</span>
          </div>
        );
      }

      await act(async () => {
        render(
          <AuthProvider>
            <TestComponent />
          </AuthProvider>
        );
      });

      expect(screen.getByTestId('authenticated')).toHaveTextContent('yes');
      expect(screen.getByTestId('token')).toHaveTextContent('test-token');
    });

    it('throws error when used outside provider', async () => {
      const { useAuth } = await import('../AuthContext');

      function TestComponent() {
        useAuth();
        return <div>Should not render</div>;
      }

      expect(() => render(<TestComponent />)).toThrow(
        'useAuth must be used within AuthProvider',
      );
    });
  });

});
