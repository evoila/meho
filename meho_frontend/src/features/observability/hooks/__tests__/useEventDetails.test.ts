// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useEventDetails Hook Tests
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useEventDetails } from '../useEventDetails';
import { getAPIClient } from '@/lib/api-client';

// Mock the API client
vi.mock('@/lib/api-client', () => ({
  getAPIClient: vi.fn(),
}));

describe('useEventDetails', () => {
  const mockEventResponse = {
    id: 'event-123',
    timestamp: '2024-01-15T10:30:00Z',
    type: 'llm_call',
    summary: 'LLM reasoning step',
    details: {
      llm_prompt: 'You are a helpful assistant.',
      llm_response: 'I will help you.',
      token_usage: {
        prompt_tokens: 100,
        completion_tokens: 50,
        total_tokens: 150,
        estimated_cost_usd: 0.001,
      },
    },
    duration_ms: 234,
    step_number: 1,
    agent_name: 'planner',
  };

  const mockGetEventDetails = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    (getAPIClient as ReturnType<typeof vi.fn>).mockReturnValue({
      observability: {
        getEventDetails: mockGetEventDetails,
      },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('returns initial state with null sessionId', () => {
    const { result } = renderHook(() => useEventDetails(null, 'event-123'));

    expect(result.current.event).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('returns initial state with null eventId', () => {
    const { result } = renderHook(() => useEventDetails('session-123', null));

    expect(result.current.event).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('fetches event details when both IDs provided', async () => {
    mockGetEventDetails.mockResolvedValue(mockEventResponse);

    const { result } = renderHook(() => 
      useEventDetails('session-123', 'event-123')
    );

    expect(result.current.loading).toBe(true);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.event).toEqual(mockEventResponse);
    expect(mockGetEventDetails).toHaveBeenCalledWith('session-123', 'event-123');
  });

  it('handles API errors', async () => {
    mockGetEventDetails.mockRejectedValue(new Error('Event not found'));

    const { result } = renderHook(() => 
      useEventDetails('session-123', 'event-123')
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.error).toBe('Event not found');
    expect(result.current.event).toBeNull();
  });

  it('does not fetch when enabled is false', () => {
    const { result } = renderHook(() =>
      useEventDetails('session-123', 'event-123', { enabled: false })
    );

    expect(result.current.loading).toBe(false);
    expect(mockGetEventDetails).not.toHaveBeenCalled();
  });

  it('refetch function triggers new fetch', async () => {
    mockGetEventDetails.mockResolvedValue(mockEventResponse);

    const { result } = renderHook(() =>
      useEventDetails('session-123', 'event-123')
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(mockGetEventDetails).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.refetch();
    });

    expect(mockGetEventDetails).toHaveBeenCalledTimes(2);
  });

  it('clear function resets state', async () => {
    mockGetEventDetails.mockResolvedValue(mockEventResponse);

    const { result } = renderHook(() =>
      useEventDetails('session-123', 'event-123')
    );

    await waitFor(() => {
      expect(result.current.event).not.toBeNull();
    });

    act(() => {
      result.current.clear();
    });

    expect(result.current.event).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('refetches when sessionId changes', async () => {
    mockGetEventDetails.mockResolvedValue(mockEventResponse);

    const { result, rerender } = renderHook(
      ({ sessionId, eventId }) => useEventDetails(sessionId, eventId),
      { initialProps: { sessionId: 'session-1', eventId: 'event-123' } }
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(mockGetEventDetails).toHaveBeenCalledWith('session-1', 'event-123');

    rerender({ sessionId: 'session-2', eventId: 'event-123' });

    await waitFor(() => {
      expect(mockGetEventDetails).toHaveBeenCalledWith('session-2', 'event-123');
    });
  });

  it('refetches when eventId changes', async () => {
    mockGetEventDetails.mockResolvedValue(mockEventResponse);

    const { result, rerender } = renderHook(
      ({ sessionId, eventId }) => useEventDetails(sessionId, eventId),
      { initialProps: { sessionId: 'session-123', eventId: 'event-1' } }
    );

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(mockGetEventDetails).toHaveBeenCalledWith('session-123', 'event-1');

    rerender({ sessionId: 'session-123', eventId: 'event-2' });

    await waitFor(() => {
      expect(mockGetEventDetails).toHaveBeenCalledWith('session-123', 'event-2');
    });
  });
});
