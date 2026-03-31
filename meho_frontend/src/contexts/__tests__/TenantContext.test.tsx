// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for TenantContext
 * 
 * TASK-140 Phase 2: Tenant Context Switching
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TenantContextProvider, useTenantContext } from '../TenantContext';

// Mock the API client
const mockSetTenantContext = vi.fn();
const mockClearTenantContext = vi.fn();
const mockInvalidateQueries = vi.fn();

vi.mock('@/lib/api-client', () => ({
  getAPIClient: () => ({
    setTenantContext: mockSetTenantContext,
    clearTenantContext: mockClearTenantContext,
  }),
}));

vi.mock('@/lib/config', () => ({
  config: {
    apiURL: 'http://localhost:8000',
  },
}));

// Create a test query client that tracks invalidateQueries calls
function createTestQueryClient() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  // Spy on invalidateQueries
  vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(mockInvalidateQueries);
  return queryClient;
}

// Helper component to test the hook
function TestConsumer({ 
  onRender 
}: { 
  onRender?: (ctx: ReturnType<typeof useTenantContext>) => void 
}) {
  const context = useTenantContext();
  const location = useLocation();
  
  if (onRender) {
    onRender(context);
  }
  
  return (
    <div>
      <div data-testid="current-tenant">{context.currentTenant ?? 'none'}</div>
      <div data-testid="display-name">{context.tenantDisplayName ?? 'none'}</div>
      <div data-testid="is-in-context">{context.isInTenantContext ? 'yes' : 'no'}</div>
      <div data-testid="location">{location.pathname}</div>
      <button 
        data-testid="enter-btn" 
        onClick={() => context.enterTenant('acme', 'Acme Corp')}
      >
        Enter
      </button>
      <button 
        data-testid="exit-btn" 
        onClick={() => context.exitTenant()}
      >
        Exit
      </button>
    </div>
  );
}

describe('TenantContext', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    // Clear sessionStorage
    sessionStorage.clear();
    // Create fresh query client for each test
    queryClient = createTestQueryClient();
  });
  
  afterEach(() => {
    sessionStorage.clear();
  });

  // Helper function to render with all providers
  function renderWithProviders(
    ui: React.ReactElement,
    { initialEntries = ['/'] }: { initialEntries?: string[] } = {}
  ) {
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>
          {ui}
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  describe('TenantContextProvider', () => {
    it('renders children', () => {
      renderWithProviders(
        <TenantContextProvider>
          <div>Test Content</div>
        </TenantContextProvider>
      );
      
      expect(screen.getByText('Test Content')).toBeInTheDocument();
    });
    
    it('provides default context values', () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      expect(screen.getByTestId('current-tenant')).toHaveTextContent('none');
      expect(screen.getByTestId('display-name')).toHaveTextContent('none');
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('no');
    });
  });
  
  describe('useTenantContext', () => {
    it('throws error when used outside provider', () => {
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
      
      expect(() => {
        renderWithProviders(<TestConsumer />);
      }).toThrow('useTenantContext must be used within TenantContextProvider');
      
      consoleError.mockRestore();
    });
  });
  
  describe('enterTenant', () => {
    it('sets the tenant context', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // Initially not in context
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('no');
      
      // Enter tenant
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      // Now in context
      expect(screen.getByTestId('current-tenant')).toHaveTextContent('acme');
      expect(screen.getByTestId('display-name')).toHaveTextContent('Acme Corp');
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('yes');
    });
    
    it('calls API client setTenantContext', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(mockSetTenantContext).toHaveBeenCalledWith('acme');
    });
    
    it('navigates to /chat when entering tenant', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>,
        { initialEntries: ['/admin/tenants'] }
      );
      
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(screen.getByTestId('location')).toHaveTextContent('/chat');
    });
    
    it('saves to sessionStorage', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(sessionStorage.getItem('meho:tenant-context')).toBe('acme');
      expect(sessionStorage.getItem('meho:tenant-display-name')).toBe('Acme Corp');
    });
    
    it('invalidates all queries to force refetch with new tenant context', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(mockInvalidateQueries).toHaveBeenCalled();
    });
  });
  
  describe('exitTenant', () => {
    it('clears the tenant context', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // Enter then exit
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('yes');
      
      await act(async () => {
        screen.getByTestId('exit-btn').click();
      });
      
      expect(screen.getByTestId('current-tenant')).toHaveTextContent('none');
      expect(screen.getByTestId('display-name')).toHaveTextContent('none');
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('no');
    });
    
    it('calls API client clearTenantContext', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // Enter then exit
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      await act(async () => {
        screen.getByTestId('exit-btn').click();
      });
      
      expect(mockClearTenantContext).toHaveBeenCalled();
    });
    
    it('navigates to /admin when exiting tenant', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>,
        { initialEntries: ['/chat'] }
      );
      
      // Enter then exit
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      await act(async () => {
        screen.getByTestId('exit-btn').click();
      });
      
      expect(screen.getByTestId('location')).toHaveTextContent('/admin');
    });
    
    it('clears sessionStorage', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // Enter then exit
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      expect(sessionStorage.getItem('meho:tenant-context')).toBe('acme');
      
      await act(async () => {
        screen.getByTestId('exit-btn').click();
      });
      
      expect(sessionStorage.getItem('meho:tenant-context')).toBeNull();
      expect(sessionStorage.getItem('meho:tenant-display-name')).toBeNull();
    });
    
    it('invalidates all queries to refetch with superadmin context', async () => {
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // Enter then exit
      await act(async () => {
        screen.getByTestId('enter-btn').click();
      });
      
      // Clear mock to check exit specifically
      mockInvalidateQueries.mockClear();
      
      await act(async () => {
        screen.getByTestId('exit-btn').click();
      });
      
      expect(mockInvalidateQueries).toHaveBeenCalled();
    });
  });
  
  describe('sessionStorage restoration', () => {
    it('restores context from sessionStorage on mount', () => {
      // Pre-populate sessionStorage
      sessionStorage.setItem('meho:tenant-context', 'restored-tenant');
      sessionStorage.setItem('meho:tenant-display-name', 'Restored Tenant');
      
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      expect(screen.getByTestId('current-tenant')).toHaveTextContent('restored-tenant');
      expect(screen.getByTestId('display-name')).toHaveTextContent('Restored Tenant');
      expect(screen.getByTestId('is-in-context')).toHaveTextContent('yes');
    });
    
    it('restores API client state from sessionStorage', () => {
      // Pre-populate sessionStorage
      sessionStorage.setItem('meho:tenant-context', 'restored-tenant');
      sessionStorage.setItem('meho:tenant-display-name', 'Restored Tenant');
      
      renderWithProviders(
        <TenantContextProvider>
          <TestConsumer />
        </TenantContextProvider>
      );
      
      // The API client should have been called during initialization
      expect(mockSetTenantContext).toHaveBeenCalledWith('restored-tenant');
    });
  });
});

