// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useSessionList Hook Tests
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSessionList } from '../useSessionList';
import { getAPIClient } from '@/lib/api-client';

// Mock the API client
vi.mock('@/lib/api-client', () => ({
  getAPIClient: vi.fn(),
}));

describe('useSessionList', () => {
  const mockSessionsResponse = {
    sessions: [
      {
        session_id: 'session-1',
        created_at: '2024-01-15T10:00:00Z',
        status: 'completed',
        user_query: 'List all VMs',
        total_llm_calls: 5,
        total_tokens: 1000,
        total_duration_ms: 25000,
      },
      {
        session_id: 'session-2',
        created_at: '2024-01-15T11:00:00Z',
        status: 'active',
        user_query: 'Check status',
        total_llm_calls: 2,
        total_tokens: 500,
        total_duration_ms: 15000,
      },
    ],
    total: 50,
    offset: 0,
    limit: 20,
  };

  const mockListSessions = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (getAPIClient as ReturnType<typeof vi.fn>).mockReturnValue({
      observability: {
        listSessions: mockListSessions,
      },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('fetches sessions on mount', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    expect(result.current.loading).toBe(true);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.sessions).toHaveLength(2);
    expect(result.current.total).toBe(50);
    expect(result.current.hasMore).toBe(true);
  });

  it('handles API errors', async () => {
    mockListSessions.mockRejectedValue(new Error('Failed to fetch'));

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.error).toBe('Failed to fetch');
    expect(result.current.sessions).toEqual([]);
  });

  it('does not fetch when enabled is false', () => {
    const { result } = renderHook(() => 
      useSessionList(undefined, { enabled: false })
    );

    expect(result.current.loading).toBe(false);
    expect(mockListSessions).not.toHaveBeenCalled();
  });

  it('nextPage increments page number', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.page).toBe(1);

    act(() => {
      result.current.nextPage();
    });

    // Page should increment since hasMore is true
    expect(result.current.page).toBe(2);
  });

  it('prevPage decrements page number', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    // Go to page 2 first
    act(() => {
      result.current.nextPage();
    });
    
    expect(result.current.page).toBe(2);

    act(() => {
      result.current.prevPage();
    });

    expect(result.current.page).toBe(1);
  });

  it('prevPage does not go below 1', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.page).toBe(1);

    act(() => {
      result.current.prevPage();
    });

    expect(result.current.page).toBe(1);
  });

  it('goToPage sets specific page', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    act(() => {
      result.current.goToPage(5);
    });

    expect(result.current.page).toBe(5);
  });

  it('setPageSize updates page size and resets to page 1', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    act(() => {
      result.current.nextPage();
    });
    
    expect(result.current.page).toBe(2);

    act(() => {
      result.current.setPageSize(50);
    });

    expect(result.current.pageSize).toBe(50);
    expect(result.current.page).toBe(1);
  });

  it('passes filter params to API', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    renderHook(() => useSessionList({ status: 'completed' }));

    await waitFor(() => {
      expect(mockListSessions).toHaveBeenCalledWith(expect.objectContaining({
        status: 'completed',
      }));
    });
  });

  it('clear function resets state', async () => {
    mockListSessions.mockResolvedValue(mockSessionsResponse);

    const { result } = renderHook(() => useSessionList());

    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(2);
    });

    act(() => {
      result.current.clear();
    });

    expect(result.current.sessions).toEqual([]);
    expect(result.current.total).toBe(0);
    expect(result.current.page).toBe(1);
  });
});
