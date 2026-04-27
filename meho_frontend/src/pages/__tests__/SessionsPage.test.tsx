// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SessionsPage Tests
 *
 * Tests for the Sessions list page including:
 * - Rendering session list
 * - Pagination controls
 * - Status filtering
 * - Loading and error states
 * - Navigation to transcript detail
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, MemoryRouter, Routes, Route } from 'react-router-dom';
import { SessionsPage } from '../SessionsPage';
import * as observabilityModule from '@/features/observability';

// Mock the useSessionList hook
vi.mock('@/features/observability', () => ({
  useSessionList: vi.fn(),
}));

const mockUseSessionList = vi.mocked(observabilityModule.useSessionList);

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

const createMemoryWrapper = (initialRoute = '/sessions') => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialRoute]}>
        <Routes>
          <Route path="/sessions" element={children} />
          <Route path="/sessions/:sessionId" element={<div data-testid="transcript-page">Transcript</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
};

describe('SessionsPage', () => {
  const mockSessions = [
    {
      session_id: 'session-1',
      created_at: '2024-01-15T10:00:00Z',
      status: 'completed',
      user_query: 'List all VMs',
      total_llm_calls: 5,
      total_tokens: 1500,
      total_duration_ms: 25000,
    },
    {
      session_id: 'session-2',
      created_at: '2024-01-15T11:00:00Z',
      status: 'active',
      user_query: 'Check cluster status',
      total_llm_calls: 2,
      total_tokens: 500,
      total_duration_ms: 15000,
    },
    {
      session_id: 'session-3',
      created_at: '2024-01-15T12:00:00Z',
      status: 'error',
      user_query: 'Failed operation',
      total_llm_calls: 1,
      total_tokens: 200,
      total_duration_ms: 5000,
    },
  ];

  const defaultHookReturn = {
    sessions: mockSessions,
    total: 50,
    page: 1,
    pageSize: 12,
    hasMore: true,
    loading: false,
    error: null,
    nextPage: vi.fn(),
    prevPage: vi.fn(),
    goToPage: vi.fn(),
    setPageSize: vi.fn(),
    refetch: vi.fn(),
    clear: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockUseSessionList.mockReturnValue(defaultHookReturn);
  });

  describe('Rendering', () => {
    it('renders page title', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByText('Session Transcripts')).toBeInTheDocument();
    });

    it('renders page description', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByText('Browse and analyze past session executions')).toBeInTheDocument();
    });

    it('renders refresh button', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByRole('button', { name: /refresh/i })).toBeInTheDocument();
    });

    it('renders session cards', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // Should have links to each session
      expect(screen.getByRole('link', { name: /completed/i })).toHaveAttribute('href', '/sessions/session-1');
      expect(screen.getByRole('link', { name: /running/i })).toHaveAttribute('href', '/sessions/session-2');
      expect(screen.getByRole('link', { name: /failed/i })).toHaveAttribute('href', '/sessions/session-3');
    });

    it('renders session query previews', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByText('List all VMs')).toBeInTheDocument();
      expect(screen.getByText('Check cluster status')).toBeInTheDocument();
      expect(screen.getByText('Failed operation')).toBeInTheDocument();
    });

    it('renders session stats', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // Check that LLM call counts are rendered
      expect(screen.getAllByText('5').length).toBeGreaterThanOrEqual(1); // session-1 total_llm_calls
      expect(screen.getAllByText('2').length).toBeGreaterThanOrEqual(1); // session-2 total_llm_calls
      expect(screen.getAllByText('1').length).toBeGreaterThanOrEqual(1); // session-3 total_llm_calls
    });

    it('formats token counts correctly', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // 1500 tokens should be formatted as "1.5k"
      expect(screen.getByText('1.5k')).toBeInTheDocument();
      // 500 tokens should be just "500"
      expect(screen.getByText('500')).toBeInTheDocument();
      // 200 tokens should be just "200"
      expect(screen.getByText('200')).toBeInTheDocument();
    });
  });

  describe('Status Badges', () => {
    it('renders completed status badge', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByText('Completed')).toBeInTheDocument();
    });

    it('renders running status badge', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByText('Running')).toBeInTheDocument();
    });

    it('renders failed status badge', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      expect(screen.getByText('Failed')).toBeInTheDocument();
    });
  });

  describe('Status Filtering', () => {
    it('renders filter badges', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByText('All Sessions')).toBeInTheDocument();
      expect(screen.getByText('completed')).toBeInTheDocument();
      expect(screen.getByText('active')).toBeInTheDocument();
      expect(screen.getByText('error')).toBeInTheDocument();
    });

    it('calls useSessionList with filter when clicking completed filter', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByText('completed'));

      await waitFor(() => {
        expect(mockUseSessionList).toHaveBeenCalledWith(
          expect.objectContaining({ status: 'completed' }),
          expect.any(Object)
        );
      });
    });

    it('calls useSessionList with filter when clicking active filter', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByText('active'));

      await waitFor(() => {
        expect(mockUseSessionList).toHaveBeenCalledWith(
          expect.objectContaining({ status: 'active' }),
          expect.any(Object)
        );
      });
    });

    it('calls useSessionList with filter when clicking error filter', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByText('error'));

      await waitFor(() => {
        expect(mockUseSessionList).toHaveBeenCalledWith(
          expect.objectContaining({ status: 'error' }),
          expect.any(Object)
        );
      });
    });

    it('clears filter when clicking All Sessions', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      // First click a filter
      await user.click(screen.getByText('completed'));
      // Then click All Sessions
      await user.click(screen.getByText('All Sessions'));

      await waitFor(() => {
        expect(mockUseSessionList).toHaveBeenCalledWith(
          expect.objectContaining({ status: undefined }),
          expect.any(Object)
        );
      });
    });
  });

  describe('Pagination', () => {
    it('renders pagination controls when there are multiple pages', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByText(/Page 1 of/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /previous/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /next/i })).toBeInTheDocument();
    });

    it('shows correct page count', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // total=50, pageSize=12 => 5 pages
      expect(screen.getByText(/Page 1 of 5/)).toBeInTheDocument();
      expect(screen.getByText(/50 sessions/)).toBeInTheDocument();
    });

    it('disables previous button on first page', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      const prevButton = screen.getByRole('button', { name: /previous/i });
      expect(prevButton).toBeDisabled();
    });

    it('enables next button when hasMore is true', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      const nextButton = screen.getByRole('button', { name: /next/i });
      expect(nextButton).not.toBeDisabled();
    });

    it('calls nextPage when next button is clicked', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByRole('button', { name: /next/i }));
      
      expect(defaultHookReturn.nextPage).toHaveBeenCalled();
    });

    it('calls prevPage when previous button is clicked', async () => {
      const user = userEvent.setup();
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        page: 2,
      });

      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByRole('button', { name: /previous/i }));
      
      expect(defaultHookReturn.prevPage).toHaveBeenCalled();
    });

    it('hides pagination when only one page', () => {
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        total: 5, // Less than pageSize of 12
        hasMore: false,
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.queryByText(/Page 1 of/)).not.toBeInTheDocument();
    });
  });

  describe('Loading State', () => {
    it('shows spinner when loading and no sessions', () => {
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        loading: true,
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // The Spinner component should be present
      expect(screen.getByRole('status')).toBeInTheDocument();
    });
  });

  describe('Error State', () => {
    it('shows error message when there is an error', () => {
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        error: 'Failed to fetch sessions',
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByText('Failed to fetch sessions')).toBeInTheDocument();
    });

    it('shows try again button on error', () => {
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        error: 'Failed to fetch sessions',
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
    });

    it('calls refetch when try again is clicked', async () => {
      const user = userEvent.setup();
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        error: 'Failed to fetch sessions',
      });

      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByRole('button', { name: /try again/i }));
      
      expect(defaultHookReturn.refetch).toHaveBeenCalled();
    });
  });

  describe('Empty State', () => {
    it('shows empty message when no sessions', () => {
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        total: 0,
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      expect(screen.getByText('No sessions found')).toBeInTheDocument();
    });

    it('shows "Show All Sessions" button when filtered and empty', async () => {
      const user = userEvent.setup();
      mockUseSessionList.mockReturnValue({
        ...defaultHookReturn,
        sessions: [],
        total: 0,
      });

      render(<SessionsPage />, { wrapper: createWrapper() });
      
      // Click a filter first
      await user.click(screen.getByText('completed'));

      expect(screen.getByRole('button', { name: /show all sessions/i })).toBeInTheDocument();
    });
  });

  describe('Refresh', () => {
    it('calls refetch when refresh button is clicked', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createWrapper() });

      await user.click(screen.getByRole('button', { name: /refresh/i }));
      
      expect(defaultHookReturn.refetch).toHaveBeenCalled();
    });
  });

  describe('Navigation', () => {
    it('renders session cards as links', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      const links = screen.getAllByRole('link');
      expect(links.length).toBeGreaterThan(0);
      
      // Check that links point to session transcript pages
      const sessionLink = links.find(link => 
        link.getAttribute('href')?.includes('/sessions/session-1')
      );
      expect(sessionLink).toBeInTheDocument();
    });

    it('navigates to transcript page when session card is clicked', async () => {
      const user = userEvent.setup();
      render(<SessionsPage />, { wrapper: createMemoryWrapper() });

      const links = screen.getAllByRole('link');
      const sessionLink = links.find(link => 
        link.getAttribute('href')?.includes('/sessions/session-1')
      );
      
      if (sessionLink) {
        await user.click(sessionLink);
        expect(screen.getByTestId('transcript-page')).toBeInTheDocument();
      }
    });
  });

  describe('Accessibility', () => {
    it('has accessible session cards', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      const links = screen.getAllByRole('link');
      links.forEach(link => {
        // Links should be focusable
        expect(link).toHaveAttribute('href');
      });
    });

    it('has accessible buttons', () => {
      render(<SessionsPage />, { wrapper: createWrapper() });
      
      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });
  });
});
