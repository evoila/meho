// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for EventHistoryTable expand/collapse behavior.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { EventHistoryTable } from '../ConnectorEvents';
import { getConnectorsClient } from '../../../api/clients/connectors';

vi.mock('../../../api/clients/connectors');
vi.mock('../../shared/components/ui/CopyButton', () => ({
  CopyButton: ({ data }: { data: string }) => <button aria-label={`copy ${data}`}>Copy</button>,
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-router-dom')>();
  return { ...mod, useNavigate: () => mockNavigate };
});

const FULL_HASH = 'abc123def456abc123def456abc123def456abc123def456abc123def456abcd';

const MOCK_HISTORY = {
  events: [
    {
      id: 'evt-processed',
      status: 'processed' as const,
      payload_hash: FULL_HASH,
      payload_size_bytes: 128,
      session_id: 'sess-abc',
      error_message: null,
      duplicates_suppressed: 0,
      created_at: new Date().toISOString(),
    },
    {
      id: 'evt-failed',
      status: 'failed' as const,
      payload_hash: 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
      payload_size_bytes: 64,
      session_id: null,
      error_message: 'Prompt template rendering failed: unexpected token',
      duplicates_suppressed: 0,
      created_at: new Date().toISOString(),
    },
    {
      id: 'evt-dedup',
      status: 'deduplicated' as const,
      payload_hash: 'cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234cafe1234',
      payload_size_bytes: 96,
      session_id: null,
      error_message: null,
      duplicates_suppressed: 3,
      created_at: new Date().toISOString(),
    },
  ],
  total: 3,
  has_more: false,
};

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) => (
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </MemoryRouter>
  );
};

describe('EventHistoryTable', () => {
  const mockGetEventHistory = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockGetEventHistory.mockResolvedValue(MOCK_HISTORY);
    vi.mocked(getConnectorsClient).mockReturnValue({
      getEventHistory: mockGetEventHistory,
    } as unknown as ReturnType<typeof getConnectorsClient>);
  });

  function renderTable() {
    return render(
      <EventHistoryTable connectorId="conn-1" eventId="reg-1" />,
      { wrapper: createWrapper() },
    );
  }

  it('renders history rows after loading', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Delivered')).toBeInTheDocument());
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('Duplicate')).toBeInTheDocument();
  });

  it('clicking a row expands to show full payload hash', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Delivered')).toBeInTheDocument());

    const rows = screen.getAllByRole('button', { name: undefined }).filter(
      (el) => el.tagName === 'TR',
    );

    // Full hash is not visible before expanding
    expect(screen.queryByText(FULL_HASH)).not.toBeInTheDocument();

    fireEvent.click(rows[0]);

    expect(await screen.findByText(FULL_HASH)).toBeInTheDocument();
    expect(screen.getByText('Payload hash')).toBeInTheDocument();
  });

  it('clicking an expanded row collapses it', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Delivered')).toBeInTheDocument());

    const rows = screen.getAllByRole('button').filter((el) => el.tagName === 'TR');

    fireEvent.click(rows[0]);
    expect(await screen.findByText(FULL_HASH)).toBeInTheDocument();

    fireEvent.click(rows[0]);
    await waitFor(() => expect(screen.queryByText(FULL_HASH)).not.toBeInTheDocument());
  });

  it('shows error message in expanded row for failed events', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Failed')).toBeInTheDocument());

    const rows = screen.getAllByRole('button').filter((el) => el.tagName === 'TR');
    fireEvent.click(rows[1]);

    expect(await screen.findByText('Prompt template rendering failed: unexpected token')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
  });

  it('shows duplicates suppressed count in expanded row for deduplicated events', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Duplicate')).toBeInTheDocument());

    const rows = screen.getAllByRole('button').filter((el) => el.tagName === 'TR');
    fireEvent.click(rows[2]);

    expect(await screen.findByText('Duplicates suppressed')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('clicking View Session navigates without toggling row expansion', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('View Session')).toBeInTheDocument());

    const viewSessionBtn = screen.getByText('View Session');
    fireEvent.click(viewSessionBtn);

    expect(mockNavigate).toHaveBeenCalledWith('/chat?session=sess-abc');
    // Full hash should NOT appear — row was not expanded
    expect(screen.queryByText(FULL_HASH)).not.toBeInTheDocument();
  });

  it('rows have correct ARIA attributes for keyboard accessibility', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Delivered')).toBeInTheDocument());

    const rows = screen.getAllByRole('button').filter((el) => el.tagName === 'TR');
    expect(rows[0]).toHaveAttribute('aria-expanded', 'false');
    expect(rows[0]).toHaveAttribute('tabindex', '0');

    fireEvent.click(rows[0]);
    await waitFor(() => expect(rows[0]).toHaveAttribute('aria-expanded', 'true'));
  });

  it('Enter key expands a row', async () => {
    renderTable();
    await waitFor(() => expect(screen.getByText('Delivered')).toBeInTheDocument());

    const rows = screen.getAllByRole('button').filter((el) => el.tagName === 'TR');
    fireEvent.keyDown(rows[0], { key: 'Enter' });

    expect(await screen.findByText(FULL_HASH)).toBeInTheDocument();
  });
});
