// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Test Utilities
 * 
 * Provides wrapper components and helpers for testing React components
 * with all necessary providers (React Query, Router, Auth, etc.)
 */
/* eslint-disable react-refresh/only-export-components */
import { type ReactElement, type ReactNode } from 'react';
import { render, type RenderOptions } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, MemoryRouter } from 'react-router-dom';

/**
 * Create a test-specific QueryClient with optimized settings
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

interface WrapperProps {
  children: ReactNode;
}

/**
 * All providers wrapper for component testing
 * Use when testing components that need routing and React Query
 */
function AllProviders({ children }: WrapperProps) {
  const queryClient = createTestQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{children}</BrowserRouter>
    </QueryClientProvider>
  );
}

/**
 * Create a wrapper with MemoryRouter for controlled routing tests
 */
export function createMemoryRouterWrapper(initialEntries: string[] = ['/']) {
  const queryClient = createTestQueryClient();

  return function MemoryRouterWrapper({ children }: WrapperProps) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  };
}

/**
 * Create a wrapper for hook testing (no routing needed)
 */
export function createWrapper() {
  const queryClient = createTestQueryClient();

  return function HookWrapper({ children }: WrapperProps) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
  };
}

/**
 * Render with all providers
 * Use for component tests that need routing and React Query
 */
export function renderWithProviders(
  ui: ReactElement,
  options?: Omit<RenderOptions, 'wrapper'>
) {
  return render(ui, { wrapper: AllProviders, ...options });
}

/**
 * Render with MemoryRouter for controlled routing
 */
export function renderWithRouter(
  ui: ReactElement,
  { initialEntries = ['/'], ...options }: RenderOptions & { initialEntries?: string[] } = {}
) {
  return render(ui, {
    wrapper: createMemoryRouterWrapper(initialEntries),
    ...options,
  });
}

/**
 * Wait for async operations to complete
 * Useful for testing loading states
 */
export async function waitForLoadingToComplete(timeout = 5000): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, timeout));
}

// Re-export everything from testing-library
export * from '@testing-library/react';
export { default as userEvent } from '@testing-library/user-event';

