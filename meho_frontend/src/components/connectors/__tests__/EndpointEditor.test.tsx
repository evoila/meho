// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Endpoint Editor Modal
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { EndpointEditorModal } from '../EndpointEditorModal';
import { getAPIClient } from '../../../lib/api-client';
import type { Endpoint } from '../../../lib/api-client';

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

describe('EndpointEditorModal', () => {
  const mockEndpoint: Endpoint = {
    id: 'ep-123',
    connector_id: 'conn-123',
    method: 'DELETE',
    path: '/repos/{owner}/{repo}',
    operation_id: 'repos/delete',
    summary: 'Delete repository',
    description: 'Delete a repository',
    tags: ['repos'],
    is_enabled: true,
    safety_level: 'dangerous',
    requires_approval: false,
    custom_description: undefined,
    custom_notes: undefined,
    usage_examples: undefined,
    last_modified_by: undefined,
    last_modified_at: undefined,
    created_at: new Date().toISOString(),
  };

  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();
  const mockUpdateEndpoint = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAPIClient).mockReturnValue({
      updateEndpoint: mockUpdateEndpoint,
    } as unknown as ReturnType<typeof getAPIClient>);
  });

  it('renders endpoint editor modal', () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    expect(screen.getByText('DELETE')).toBeInTheDocument();
    expect(screen.getByText('/repos/{owner}/{repo}')).toBeInTheDocument();
  });

  it('shows current safety level', () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );

    // Phase 84: Safety levels renamed to Auto/Read/Write/Destructive trust levels
    expect(screen.getByText('Destructive')).toBeInTheDocument();
    expect(screen.getByText('Requires approval, red modal')).toBeInTheDocument();
  });

  it('allows changing safety level', async () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );

    // Phase 84: Safety levels renamed to Auto/Read/Write/Destructive
    const readLabel = screen.getByText('Read');
    const readRadio = readLabel.closest('label')?.querySelector('input[type="radio"]');
    if (readRadio) {
      fireEvent.click(readRadio);
      expect(readRadio).toBeChecked();
    }
  });

  it('allows enabling/disabling endpoint', async () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    const disabledRadio = screen.getByText(/Disabled \(hidden from agent\)/).closest('label')?.querySelector('input');
    if (disabledRadio) {
      fireEvent.click(disabledRadio);
      expect(disabledRadio).toBeChecked();
    }
  });

  it('allows adding custom description', async () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    const textarea = screen.getByPlaceholderText(/CRITICAL: Add important context/);
    fireEvent.change(textarea, {
      target: { value: '⚠️ This deletes everything permanently' }
    });
    
    expect(textarea).toHaveValue('⚠️ This deletes everything permanently');
  });

  it('saves changes when clicking save', async () => {
    mockUpdateEndpoint.mockResolvedValue({
      ...mockEndpoint,
      is_enabled: false,
      custom_description: 'Test description',
    });
    
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    // Disable endpoint
    const disabledRadio = screen.getByText(/Disabled/).closest('label')?.querySelector('input');
    if (disabledRadio) {
      fireEvent.click(disabledRadio);
    }
    
    // Add custom description
    const textarea = screen.getByPlaceholderText(/CRITICAL: Add important context/);
    fireEvent.change(textarea, {
      target: { value: 'Test description' }
    });
    
    // Click save
    const saveButton = screen.getByText('Save Changes');
    fireEvent.click(saveButton);
    
    await waitFor(() => {
      expect(mockUpdateEndpoint).toHaveBeenCalledWith(
        'conn-123',
        'ep-123',
        expect.objectContaining({
          is_enabled: false,
          custom_description: 'Test description',
        })
      );
    });
  });

  it('resets to original values when clicking reset', async () => {
    const modifiedEndpoint = {
      ...mockEndpoint,
      custom_description: 'Original description',
    };
    
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={modifiedEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    // Change description
    const textarea = screen.getByPlaceholderText(/CRITICAL: Add important context/);
    fireEvent.change(textarea, {
      target: { value: 'Modified description' }
    });
    
    expect(textarea).toHaveValue('Modified description');
    
    // Click reset
    const resetButton = screen.getByText('Reset');
    fireEvent.click(resetButton);
    
    await waitFor(() => {
      expect(textarea).toHaveValue('Original description');
    });
  });

  it('shows original description', () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    expect(screen.getByText('Delete a repository')).toBeInTheDocument();
  });

  it('allows toggling requires approval', async () => {
    render(
      <EndpointEditorModal
        connectorId="conn-123"
        endpoint={mockEndpoint}
        onClose={mockOnClose}
        onSuccess={mockOnSuccess}
      />,
      { wrapper: createWrapper() }
    );
    
    const approvalCheckbox = screen.getByText(/Require explicit approval before execution/).closest('label')?.querySelector('input');
    if (approvalCheckbox) {
      expect(approvalCheckbox).not.toBeChecked();
      
      fireEvent.click(approvalCheckbox);
      
      expect(approvalCheckbox).toBeChecked();
    }
  });
});

