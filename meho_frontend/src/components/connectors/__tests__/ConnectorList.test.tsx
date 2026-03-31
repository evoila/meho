// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for ConnectorList Component
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConnectorList } from '../ConnectorList';
import { getAPIClient } from '../../../lib/api-client';

vi.mock('../../../lib/api-client');

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );
};

describe('ConnectorList', () => {
  const mockOnSelectConnector = vi.fn();
  const mockListConnectors = vi.fn();
  const mockCreateConnector = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAPIClient).mockReturnValue({
      listConnectors: mockListConnectors,
      createConnector: mockCreateConnector,
    } as unknown as ReturnType<typeof getAPIClient>);
  });

  it('renders connector list', async () => {
    mockListConnectors.mockResolvedValue([]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('API Connectors')).toBeInTheDocument();
    });
  });

  it('shows empty state when no connectors', async () => {
    mockListConnectors.mockResolvedValue([]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('No connectors yet')).toBeInTheDocument();
    });
  });

  it('displays connectors in cards', async () => {
    mockListConnectors.mockResolvedValue([
      {
        id: '1',
        name: 'GitHub API',
        base_url: 'https://api.github.com',
        auth_type: 'API_KEY',
        tenant_id: 'test-tenant',
        allowed_methods: ['GET', 'POST'],
        blocked_methods: ['DELETE'],
        default_safety_level: 'safe',
        is_active: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('GitHub API')).toBeInTheDocument();
      expect(screen.getByText('https://api.github.com')).toBeInTheDocument();
    });
  });

  it('shows blocked methods badge', async () => {
    mockListConnectors.mockResolvedValue([
      {
        id: '1',
        name: 'Test API',
        base_url: 'https://api.test.com',
        auth_type: 'API_KEY',
        tenant_id: 'test-tenant',
        allowed_methods: ['GET'],
        blocked_methods: ['DELETE', 'PUT'],
        default_safety_level: 'safe',
        is_active: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText(/2 blocked/)).toBeInTheDocument();
    });
  });

  it('allows searching connectors', async () => {
    mockListConnectors.mockResolvedValue([
      {
        id: '1',
        name: 'GitHub API',
        base_url: 'https://api.github.com',
        auth_type: 'API_KEY',
        tenant_id: 'test-tenant',
        allowed_methods: ['GET'],
        blocked_methods: [],
        default_safety_level: 'safe',
        is_active: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      {
        id: '2',
        name: 'Kubernetes API',
        base_url: 'https://k8s.example.com',
        auth_type: 'BASIC',
        tenant_id: 'test-tenant',
        allowed_methods: ['GET'],
        blocked_methods: [],
        default_safety_level: 'safe',
        is_active: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('GitHub API')).toBeInTheDocument();
      expect(screen.getByText('Kubernetes API')).toBeInTheDocument();
    });
    
    const searchInput = screen.getByPlaceholderText('Search connectors...');
    fireEvent.change(searchInput, { target: { value: 'github' } });
    
    await waitFor(() => {
      expect(screen.getByText('GitHub API')).toBeInTheDocument();
      expect(screen.queryByText('Kubernetes API')).not.toBeInTheDocument();
    });
  });

  it('opens create modal when clicking new connector', async () => {
    mockListConnectors.mockResolvedValue([]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('New Connector')).toBeInTheDocument();
    });
    
    fireEvent.click(screen.getByText('New Connector'));
    
    await waitFor(() => {
      // Modal title is "Create Connector"
      expect(screen.getByTestId('create-connector-modal-title')).toHaveTextContent('Create Connector');
    });
  });

  it('calls onSelectConnector when clicking card', async () => {
    mockListConnectors.mockResolvedValue([
      {
        id: 'test-id-123',
        name: 'Test API',
        base_url: 'https://api.test.com',
        auth_type: 'API_KEY',
        tenant_id: 'test-tenant',
        allowed_methods: ['GET'],
        blocked_methods: [],
        default_safety_level: 'safe',
        is_active: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    
    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });
    
    await waitFor(() => {
      expect(screen.getByText('Test API')).toBeInTheDocument();
    });
    
    const card = screen.getByText('Test API').closest('div[class*="cursor-pointer"]');
    if (card) {
      fireEvent.click(card);
      expect(mockOnSelectConnector).toHaveBeenCalledWith('test-id-123');
    }
  });

  // ==================== Export/Import Button Tests (TASK-142) ====================

  it('shows Export button in header', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connectors-button')).toBeInTheDocument();
    });
  });

  it('shows Import button in header', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('import-connectors-button')).toBeInTheDocument();
    });
  });

  it('opens export modal when Export button clicked', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connectors-button')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('export-connectors-button'));

    await waitFor(() => {
      expect(screen.getByTestId('export-modal-title')).toHaveTextContent('Export Connectors');
    });
  });

  it('opens import modal when Import button clicked', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ConnectorList onSelectConnector={mockOnSelectConnector} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('import-connectors-button')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('import-connectors-button'));

    await waitFor(() => {
      expect(screen.getByTestId('import-modal-title')).toHaveTextContent('Import Connectors');
    });
  });
});

