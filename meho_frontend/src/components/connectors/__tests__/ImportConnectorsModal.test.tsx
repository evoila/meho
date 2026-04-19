// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for ImportConnectorsModal Component
 * 
 * TASK-142: Connector Import/Export
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ImportConnectorsModal } from '../ImportConnectorsModal';
import { getAPIClient } from '../../../lib/api-client';

vi.mock('../../../lib/api-client');

describe('ImportConnectorsModal', () => {
  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();
  const mockImportConnectors = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAPIClient).mockReturnValue({
      importConnectors: mockImportConnectors,
    } as unknown as ReturnType<typeof getAPIClient>);
  });

  it('renders modal with all required elements', () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Header
    expect(screen.getByTestId('import-modal-title')).toHaveTextContent('Import Connectors');

    // File upload dropzone
    expect(screen.getByTestId('import-dropzone')).toBeInTheDocument();
    expect(screen.getByText(/Click to browse/)).toBeInTheDocument();
    expect(screen.getByText(/drag and drop/)).toBeInTheDocument();

    // Password input
    expect(screen.getByTestId('import-password')).toBeInTheDocument();

    // Conflict strategy options
    expect(screen.getByTestId('import-conflict-skip')).toBeInTheDocument();
    expect(screen.getByTestId('import-conflict-overwrite')).toBeInTheDocument();
    expect(screen.getByTestId('import-conflict-rename')).toBeInTheDocument();

    // Buttons
    expect(screen.getByText('Cancel')).toBeInTheDocument();
    expect(screen.getByTestId('import-submit')).toBeInTheDocument();
  });

  it('closes modal when close button clicked', () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    fireEvent.click(screen.getByTestId('import-modal-close'));

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('closes modal when cancel button clicked', () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    fireEvent.click(screen.getByText('Cancel'));

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('disables import button when no file selected', () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const importButton = screen.getByTestId('import-submit');
    expect(importButton).toBeDisabled();
  });

  it('disables import button when no password entered', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Create a mock JSON file
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });

    // Get file input and upload file
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('test.json');
    });

    // Import button should still be disabled without password
    const importButton = screen.getByTestId('import-submit');
    expect(importButton).toBeDisabled();
  });

  it('enables import button when file and password provided', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Upload file
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('test.json');
    });

    // Enter password
    const passwordInput = screen.getByTestId('import-password');
    fireEvent.change(passwordInput, { target: { value: 'testpassword' } });

    // Import button should now be enabled
    const importButton = screen.getByTestId('import-submit');
    expect(importButton).not.toBeDisabled();
  });

  it('shows file name and size after selection', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const file = new File(['{"connectors":[]}'], 'my-connectors.json', { type: 'application/json' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('my-connectors.json');
    });
  });

  it('removes file when remove button clicked', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Upload file
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toBeInTheDocument();
    });

    // Click remove button
    fireEvent.click(screen.getByTestId('import-remove-file'));

    // File should be removed
    expect(screen.queryByTestId('import-file-name')).not.toBeInTheDocument();
    expect(screen.getByText(/Click to browse/)).toBeInTheDocument();
  });

  it('shows error for invalid file extension', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Try to upload invalid file type
    const file = new File(['invalid'], 'test.txt', { type: 'text/plain' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByText(/Invalid file type/)).toBeInTheDocument();
    });
  });

  it('accepts JSON files', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('test.json');
    });

    expect(screen.queryByText(/Invalid file type/)).not.toBeInTheDocument();
  });

  it('accepts YAML files', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const file = new File(['connectors: []'], 'test.yaml', { type: 'application/x-yaml' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('test.yaml');
    });
  });

  it('accepts YML files', async () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    const file = new File(['connectors: []'], 'test.yml', { type: 'application/x-yaml' });
    const fileInput = screen.getByTestId('import-file-input');
    fireEvent.change(fileInput, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByTestId('import-file-name')).toHaveTextContent('test.yml');
    });
  });

  it('allows selecting conflict strategy', () => {
    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Default should be skip
    const skipOption = screen.getByTestId('import-conflict-skip');
    expect(skipOption.querySelector('input')).toBeChecked();

    // Select overwrite
    const overwriteOption = screen.getByTestId('import-conflict-overwrite');
    const overwriteInput = overwriteOption.querySelector('input');
    if (overwriteInput) fireEvent.click(overwriteInput);
    expect(overwriteOption.querySelector('input')).toBeChecked();

    // Select rename
    const renameOption = screen.getByTestId('import-conflict-rename');
    const renameInput = renameOption.querySelector('input');
    if (renameInput) fireEvent.click(renameInput);
    expect(renameOption.querySelector('input')).toBeChecked();
  });

  it('shows loading state during import', async () => {
    // Make import take a while
    mockImportConnectors.mockImplementation(() => new Promise(() => {}));

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'password123' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    // Click import
    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(screen.getByText('Importing...')).toBeInTheDocument();
    });
  });

  it('shows success message after import', async () => {
    mockImportConnectors.mockResolvedValue({
      imported: 3,
      skipped: 1,
      errors: [],
      connectors: ['VMware Dev', 'Kubernetes Prod', 'GCP Staging'],
      warnings: [],
      operations_synced: 200,
    });

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'password123' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    // Click import
    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(screen.getByTestId('import-success')).toBeInTheDocument();
      expect(screen.getByText(/Imported 3 connectors/)).toBeInTheDocument();
      expect(screen.getByText(/Skipped 1 existing/)).toBeInTheDocument();
    });

    // Should show imported connector names
    expect(screen.getByText('VMware Dev')).toBeInTheDocument();
    expect(screen.getByText('Kubernetes Prod')).toBeInTheDocument();
    expect(screen.getByText('GCP Staging')).toBeInTheDocument();
  });

  it('shows error message on API failure', async () => {
    mockImportConnectors.mockRejectedValue(new Error('Invalid password or corrupted file'));

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'wrongpassword' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    // Click import
    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(screen.getByTestId('import-error')).toHaveTextContent('Invalid password or corrupted file');
    });
  });

  it('shows errors from import response', async () => {
    mockImportConnectors.mockResolvedValue({
      imported: 2,
      skipped: 0,
      errors: ['Connector "Old API" skipped due to incompatible version'],
      connectors: ['VMware Dev', 'Kubernetes Prod'],
      warnings: [],
      operations_synced: 150,
    });

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'password123' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(screen.getByText('Import Errors:')).toBeInTheDocument();
      expect(screen.getByText(/Connector "Old API" skipped/)).toBeInTheDocument();
    });
  });

  it('shows operations sync warnings from import response', async () => {
    mockImportConnectors.mockResolvedValue({
      imported: 2,
      skipped: 0,
      errors: [],
      connectors: ['VMware Dev', 'REST API'],
      warnings: ['REST API: Could not fetch OpenAPI spec. You can manually upload the spec later.'],
      operations_synced: 100,
    });

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{"connectors":[]}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'password123' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(screen.getByText('Operations Sync Warnings:')).toBeInTheDocument();
      expect(screen.getByText(/Could not fetch OpenAPI spec/)).toBeInTheDocument();
    });
  });

  it('calls importConnectors with correct parameters', async () => {
    mockImportConnectors.mockResolvedValue({
      imported: 1,
      skipped: 0,
      errors: [],
      connectors: ['Test Connector'],
      warnings: [],
      operations_synced: 50,
    });

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const fileContent = '{"meho_export":{"version":"1.0"},"connectors":[]}';
    const file = new File([fileContent], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'mypassword' } });

    // Select overwrite strategy
    const overwriteOption = screen.getByTestId('import-conflict-overwrite');
    const overwriteInputEl = overwriteOption.querySelector('input');
    if (overwriteInputEl) fireEvent.click(overwriteInputEl);

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    fireEvent.click(screen.getByTestId('import-submit'));

    await waitFor(() => {
      expect(mockImportConnectors).toHaveBeenCalledWith({
        file_content: expect.any(String), // base64 encoded
        password: 'mypassword',
        conflict_strategy: 'overwrite',
      });
    });
  });

  it('calls onSuccess callback after successful import', async () => {
    mockImportConnectors.mockResolvedValue({
      imported: 1,
      skipped: 0,
      errors: [],
      connectors: ['Test'],
      warnings: [],
      operations_synced: 50,
    });

    render(<ImportConnectorsModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

    // Setup file and password
    const file = new File(['{}'], 'test.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.change(screen.getByTestId('import-password'), { target: { value: 'password' } });

    await waitFor(() => {
      expect(screen.getByTestId('import-submit')).not.toBeDisabled();
    });

    fireEvent.click(screen.getByTestId('import-submit'));

    // Wait for success message to appear (API call completed)
    await waitFor(() => {
      expect(screen.getByTestId('import-success')).toBeInTheDocument();
    });

    // Wait for the delayed onSuccess/onClose callbacks (2 second timeout in component)
    await waitFor(() => {
      expect(mockOnSuccess).toHaveBeenCalled();
      expect(mockOnClose).toHaveBeenCalled();
    }, { timeout: 3000 });
  });
});

