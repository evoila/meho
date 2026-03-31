// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for ExportConnectorsModal Component
 * 
 * TASK-142: Connector Import/Export
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ExportConnectorsModal } from '../ExportConnectorsModal';
import { getAPIClient } from '../../../lib/api-client';

vi.mock('../../../lib/api-client');

// Mock URL methods for download testing
const mockCreateObjectURL = vi.fn(() => 'blob:mock-url');
const mockRevokeObjectURL = vi.fn();

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

const mockConnectors = [
  {
    id: 'conn-1',
    name: 'VMware Dev',
    base_url: 'https://vcenter.dev.local',
    auth_type: 'BASIC',
    tenant_id: 'test-tenant',
    connector_type: 'vmware',
    allowed_methods: ['GET'],
    blocked_methods: [],
    default_safety_level: 'safe',
    is_active: true,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
  {
    id: 'conn-2',
    name: 'Kubernetes Prod',
    base_url: 'https://k8s.prod.local',
    auth_type: 'API_KEY',
    tenant_id: 'test-tenant',
    connector_type: 'kubernetes',
    allowed_methods: ['GET', 'POST'],
    blocked_methods: [],
    default_safety_level: 'caution',
    is_active: true,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  },
];

describe('ExportConnectorsModal', () => {
  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();
  const mockListConnectors = vi.fn();
  const mockExportConnectors = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAPIClient).mockReturnValue({
      listConnectors: mockListConnectors,
      exportConnectors: mockExportConnectors,
    } as unknown as ReturnType<typeof getAPIClient>);

    // Setup URL mocks
    (globalThis as typeof globalThis & { URL: typeof URL }).URL.createObjectURL = mockCreateObjectURL;
    (globalThis as typeof globalThis & { URL: typeof URL }).URL.revokeObjectURL = mockRevokeObjectURL;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // ==================== Rendering Tests ====================

  it('renders modal with title "Export Connectors"', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    expect(screen.getByTestId('export-modal-title')).toHaveTextContent('Export Connectors');
  });

  it('shows loading spinner while fetching connectors', async () => {
    // Never resolve to keep loading state
    mockListConnectors.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    // Should show loading state (animate-spin class on Loader2 icon)
    expect(container.querySelector('.animate-spin')).toBeTruthy();
  });

  it('displays connector list when loaded', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByText('VMware Dev')).toBeInTheDocument();
      expect(screen.getByText('Kubernetes Prod')).toBeInTheDocument();
    });
  });

  it('shows empty state when no connectors exist', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByText('No connectors found')).toBeInTheDocument();
    });
  });

  // ==================== Connector Selection Tests ====================

  it('Select All checkbox selects all connectors', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-select-all')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('export-select-all'));

    // Both connector checkboxes should be checked
    expect(screen.getByTestId('export-connector-conn-1')).toBeChecked();
    expect(screen.getByTestId('export-connector-conn-2')).toBeChecked();
  });

  it('Select All checkbox deselects all when all are selected', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-select-all')).toBeInTheDocument();
    });

    // Select all
    fireEvent.click(screen.getByTestId('export-select-all'));
    expect(screen.getByTestId('export-connector-conn-1')).toBeChecked();

    // Deselect all
    fireEvent.click(screen.getByTestId('export-select-all'));
    expect(screen.getByTestId('export-connector-conn-1')).not.toBeChecked();
    expect(screen.getByTestId('export-connector-conn-2')).not.toBeChecked();
  });

  it('individual connector toggle works', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Select first connector only
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    expect(screen.getByTestId('export-connector-conn-1')).toBeChecked();
    expect(screen.getByTestId('export-connector-conn-2')).not.toBeChecked();

    // Toggle it off
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    expect(screen.getByTestId('export-connector-conn-1')).not.toBeChecked();
  });

  it('shows warning when no connectors selected', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByText('VMware Dev')).toBeInTheDocument();
    });

    // No selection - should show warning
    expect(screen.getByText(/Select at least one connector/)).toBeInTheDocument();
  });

  // ==================== Password Validation Tests ====================

  it('export button disabled when password too short', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Select a connector
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));

    // Enter short password
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: '1234567' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: '1234567' } });

    // Button should be disabled
    expect(screen.getByTestId('export-submit')).toBeDisabled();
  });

  it('shows "Password must be at least 8 characters" validation error', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-password')).toBeInTheDocument();
    });

    // Enter short password
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'short' } });

    expect(screen.getByText(/Password must be at least 8 characters/)).toBeInTheDocument();
  });

  it('export button disabled when passwords do not match', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Select a connector
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));

    // Enter mismatched passwords
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'different456' } });

    // Button should be disabled
    expect(screen.getByTestId('export-submit')).toBeDisabled();
  });

  it('shows "Passwords do not match" validation error', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-password')).toBeInTheDocument();
    });

    // Enter valid password but different confirmation
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'different456' } });

    expect(screen.getByText(/Passwords do not match/)).toBeInTheDocument();
  });

  it('export button enabled when passwords valid and match', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Select a connector
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));

    // Enter valid matching passwords
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Button should be enabled
    expect(screen.getByTestId('export-submit')).not.toBeDisabled();
  });

  // ==================== Format Selection Tests ====================

  it('default format is JSON', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-format-json')).toBeInTheDocument();
    });

    // JSON button should have selected styling (contains primary color class)
    const jsonButton = screen.getByTestId('export-format-json');
    expect(jsonButton.className).toContain('bg-primary');
  });

  it('can switch to YAML format', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-format-yaml')).toBeInTheDocument();
    });

    // Click YAML
    fireEvent.click(screen.getByTestId('export-format-yaml'));

    // YAML should now be selected
    const yamlButton = screen.getByTestId('export-format-yaml');
    expect(yamlButton.className).toContain('bg-primary');
  });

  // ==================== Export Flow Tests ====================

  it('shows loading state during export', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    // Never resolve to keep loading state
    mockExportConnectors.mockImplementation(() => new Promise(() => {}));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Setup valid export
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    await waitFor(() => {
      expect(screen.getByText('Exporting...')).toBeInTheDocument();
    });
  });

  it('shows success message after export', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    mockExportConnectors.mockResolvedValue(new Blob(['test'], { type: 'application/json' }));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Setup valid export
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    await waitFor(() => {
      expect(screen.getByText(/Export successful/)).toBeInTheDocument();
    });
  });

  it('triggers file download on success', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    mockExportConnectors.mockResolvedValue(new Blob(['test'], { type: 'application/json' }));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Setup valid export
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    await waitFor(() => {
      expect(mockCreateObjectURL).toHaveBeenCalled();
    });
  });

  it('shows error message on API failure', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    mockExportConnectors.mockRejectedValue(new Error('Export failed'));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Setup valid export
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    await waitFor(() => {
      expect(screen.getByText('Export failed')).toBeInTheDocument();
    });
  });

  it('calls onSuccess callback after successful export', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    mockExportConnectors.mockResolvedValue(new Blob(['test'], { type: 'application/json' }));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Setup valid export
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'password123' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'password123' } });

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    // Wait for success and callback (has 1.5s delay)
    await waitFor(() => {
      expect(mockOnSuccess).toHaveBeenCalled();
    }, { timeout: 3000 });
  });

  it('calls onClose when Cancel clicked', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    fireEvent.click(screen.getByText('Cancel'));

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('calls onClose when X button clicked', async () => {
    mockListConnectors.mockResolvedValue([]);

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    fireEvent.click(screen.getByTestId('export-modal-close'));

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('calls exportConnectors with correct parameters', async () => {
    mockListConnectors.mockResolvedValue(mockConnectors);
    mockExportConnectors.mockResolvedValue(new Blob(['test'], { type: 'application/json' }));

    render(<ExportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />, {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(screen.getByTestId('export-connector-conn-1')).toBeInTheDocument();
    });

    // Select connector and set options
    fireEvent.click(screen.getByTestId('export-connector-conn-1'));
    fireEvent.change(screen.getByTestId('export-password'), { target: { value: 'mypassword' } });
    fireEvent.change(screen.getByTestId('export-password-confirm'), { target: { value: 'mypassword' } });
    fireEvent.click(screen.getByTestId('export-format-yaml'));

    // Click export
    fireEvent.click(screen.getByTestId('export-submit'));

    await waitFor(() => {
      expect(mockExportConnectors).toHaveBeenCalledWith({
        connector_ids: ['conn-1'],
        password: 'mypassword',
        format: 'yaml',
      });
    });
  });
});

