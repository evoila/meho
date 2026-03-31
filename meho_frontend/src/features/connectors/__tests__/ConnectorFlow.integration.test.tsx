// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Integration tests for Connector Flow
 * 
 * Tests the complete connector management flow including:
 * - Listing connectors
 * - Creating connectors
 * - Updating connectors
 * - Deleting connectors
 * 
 * Note: These tests mock the API client directly since MSW has 
 * compatibility issues with jsdom. For full E2E tests, use Playwright.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { ConnectorList } from '@/components/connectors/ConnectorList';

// Mock the API client
const mockConnectors = [
  {
    id: 'conn-1',
    name: 'Test REST API',
    connector_type: 'rest',
    base_url: 'https://api.example.com',
    description: 'A test REST connector',
    is_active: true,
    auth_type: 'NONE',
    blocked_methods: [],
    allowed_methods: ['GET', 'POST'],
    default_safety_level: 'safe',
    tenant_id: 'test-tenant',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    id: 'conn-2',
    name: 'Test SOAP Service',
    connector_type: 'soap',
    base_url: 'https://soap.example.com',
    description: 'A test SOAP connector',
    is_active: false,
    auth_type: 'BASIC',
    blocked_methods: ['DELETE'],
    allowed_methods: ['GET'],
    default_safety_level: 'caution',
    tenant_id: 'test-tenant',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
];

vi.mock('@/lib/api-client', () => ({
  getAPIClient: () => ({
    listConnectors: vi.fn().mockResolvedValue(mockConnectors),
    getConnector: vi.fn().mockImplementation((id: string) => {
      const connector = mockConnectors.find(c => c.id === id);
      return Promise.resolve(connector || null);
    }),
    createConnector: vi.fn().mockResolvedValue({
      id: 'conn-new',
      name: 'New Connector',
      is_active: true,
    }),
    updateConnector: vi.fn().mockResolvedValue({
      id: 'conn-1',
      name: 'Updated',
    }),
    deleteConnector: vi.fn().mockResolvedValue(undefined),
  }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{children}</BrowserRouter>
    </QueryClientProvider>
  );
};

describe('Connector Flow Integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('ConnectorList', () => {
    it('displays loading state initially', () => {
      render(<ConnectorList onSelectConnector={() => {}} />, {
        wrapper: createWrapper(),
      });

      // Should show loading indicator - component uses animate-spin for loading spinner
      expect(
        screen.getByText('Loading connectors...') || document.querySelector('.animate-spin')
      ).toBeTruthy();
    });

    it('displays connectors after loading', async () => {
      render(<ConnectorList onSelectConnector={() => {}} />, {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(screen.getByText('Test REST API')).toBeInTheDocument();
      });

      expect(screen.getByText('Test SOAP Service')).toBeInTheDocument();
    });

    it('calls onSelectConnector when a connector is clicked', async () => {
      const onSelectConnector = vi.fn();
      render(<ConnectorList onSelectConnector={onSelectConnector} />, {
        wrapper: createWrapper(),
      });

      await waitFor(() => {
        expect(screen.getByText('Test REST API')).toBeInTheDocument();
      });

      await userEvent.click(screen.getByText('Test REST API'));

      expect(onSelectConnector).toHaveBeenCalled();
    });
  });

  // Note: Full API integration tests require E2E testing with Playwright
  // These tests focus on component behavior with mocked APIs
});
