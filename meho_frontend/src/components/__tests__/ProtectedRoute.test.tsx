// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for ProtectedRoute component
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { ProtectedRoute } from '../ProtectedRoute';

// Mock the useAuth hook from the unified AuthContext
vi.mock('../../contexts/AuthContext', async () => {
  const actual = await vi.importActual('../../contexts/AuthContext');
  return {
    ...actual,
    useAuth: vi.fn(),
  };
});

import { useAuth } from '../../contexts/AuthContext';

/** Helper to create a mock return value for useAuth with sensible defaults */
function mockAuth(overrides: Partial<ReturnType<typeof useAuth>> = {}) {
  return {
    isInitialized: true,
    isAuthenticated: false,
    token: null,
    user: null,
    login: vi.fn(),
    logout: vi.fn(),
    refreshToken: vi.fn().mockResolvedValue(null),
    isGlobalAdmin: false,
    tenantId: '',
    roles: [],
    userId: '',
    email: '',
    userName: '',
    sessionExpired: false,
    handleReAuth: vi.fn().mockResolvedValue(undefined),
    isLoading: false,
    ...overrides,
  } as ReturnType<typeof useAuth>;
}

describe('ProtectedRoute', () => {
  const renderWithRouter = (component: React.ReactElement) => {
    return render(
      <BrowserRouter>
        {component}
      </BrowserRouter>
    );
  };

  it('shows loading state when auth is loading', () => {
    vi.mocked(useAuth).mockReturnValue(mockAuth({ isLoading: true }));

    renderWithRouter(
      <ProtectedRoute>
        <div>Protected Content</div>
      </ProtectedRoute>
    );

    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('renders children when authenticated', () => {
    vi.mocked(useAuth).mockReturnValue(mockAuth({
      isAuthenticated: true,
      isLoading: false,
      user: {
        sub: 'test@example.com',
        tenant_id: 'test-tenant',
        roles: ['user'],
        groups: [],
        exp: 9999999999,
        iat: 1700000000,
        isGlobalAdmin: false,
      },
      token: 'test-token',
    }));

    renderWithRouter(
      <ProtectedRoute>
        <div>Protected Content</div>
      </ProtectedRoute>
    );

    expect(screen.getByText('Protected Content')).toBeInTheDocument();
  });

  it('redirects to login when not authenticated', () => {
    vi.mocked(useAuth).mockReturnValue(mockAuth({
      isAuthenticated: false,
      isLoading: false,
    }));

    renderWithRouter(
      <ProtectedRoute>
        <div>Protected Content</div>
      </ProtectedRoute>
    );

    // Should not show protected content
    expect(screen.queryByText('Protected Content')).not.toBeInTheDocument();
  });
});
