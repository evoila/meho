// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useSessionTranscript Hook Tests
 * 
 * Tests for the multi-turn transcript hook.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useSessionTranscript } from '../useSessionTranscript';
import { getAPIClient } from '@/lib/api-client';

// Mock the API client
vi.mock('@/lib/api-client', () => ({
  getAPIClient: vi.fn(),
}));

describe('useSessionTranscript', () => {
  // Mock response matching the multi-turn API format
  const mockTranscriptResponse = {
    session_id: 'session-123',
    transcripts: [
      {
        transcript_id: 'transcript-1',
        user_query: 'What is the status?',
        created_at: '2024-01-15T10:30:00Z',
        status: 'completed',
        summary: {
          session_id: 'session-123',
          status: 'completed',
          created_at: '2024-01-15T10:30:00Z',
          completed_at: '2024-01-15T10:30:30Z',
          total_llm_calls: 1,
          total_operation_calls: 0,
          total_sql_queries: 0,
          total_tool_calls: 0,
          total_tokens: 100,
          total_cost_usd: 0.001,
          total_duration_ms: 500,
          user_query: 'What is the status?',
          agent_type: 'generic',
        },
        events: [
          {
            id: 'event-1',
            timestamp: '2024-01-15T10:30:00Z',
            type: 'llm_call',
            summary: 'LLM reasoning',
            details: { llm_response: 'test' },
            parent_event_id: null,
            step_number: 1,
            node_name: 'planner',
            agent_name: 'generic',
            duration_ms: 500,
          },
        ],
      },
    ],
    total_transcripts: 1,
  };

  const mockGetTranscript = vi.fn();
  
  beforeEach(() => {
    vi.clearAllMocks();
    (getAPIClient as ReturnType<typeof vi.fn>).mockReturnValue({
      observability: {
        getTranscript: mockGetTranscript,
      },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('returns initial state with null sessionId', () => {
    const { result } = renderHook(() => useSessionTranscript(null));
    
    expect(result.current.transcripts).toEqual([]);
    expect(result.current.totalTranscripts).toBe(0);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('fetches transcript when sessionId is provided', async () => {
    mockGetTranscript.mockResolvedValue(mockTranscriptResponse);
    
    const { result } = renderHook(() => useSessionTranscript('session-123'));
    
    // Initially loading
    expect(result.current.loading).toBe(true);
    
    // Wait for fetch to complete
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    
    expect(result.current.transcripts).toHaveLength(1);
    expect(result.current.transcripts[0].transcript_id).toBe('transcript-1');
    expect(result.current.totalTranscripts).toBe(1);
    expect(mockGetTranscript).toHaveBeenCalledWith('session-123', { include_details: undefined });
  });

  it('handles API errors', async () => {
    mockGetTranscript.mockRejectedValue(new Error('Network error'));
    
    const { result } = renderHook(() => useSessionTranscript('session-123'));
    
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    
    expect(result.current.error).toBe('Network error');
    expect(result.current.transcripts).toEqual([]);
  });

  it('does not fetch when enabled is false', () => {
    const { result } = renderHook(() => 
      useSessionTranscript('session-123', undefined, { enabled: false })
    );
    
    expect(result.current.loading).toBe(false);
    expect(mockGetTranscript).not.toHaveBeenCalled();
  });

  it('passes include_details param to API call', async () => {
    mockGetTranscript.mockResolvedValue(mockTranscriptResponse);
    
    const params = { include_details: true };
    
    renderHook(() => useSessionTranscript('session-123', params));
    
    await waitFor(() => {
      expect(mockGetTranscript).toHaveBeenCalledWith('session-123', { include_details: true });
    });
  });

  it('refetch function triggers new fetch', async () => {
    mockGetTranscript.mockResolvedValue(mockTranscriptResponse);
    
    const { result } = renderHook(() => useSessionTranscript('session-123'));
    
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
    
    expect(mockGetTranscript).toHaveBeenCalledTimes(1);
    
    // Call refetch
    await act(async () => {
      await result.current.refetch();
    });
    
    expect(mockGetTranscript).toHaveBeenCalledTimes(2);
  });

  it('clear function resets state', async () => {
    mockGetTranscript.mockResolvedValue(mockTranscriptResponse);
    
    const { result } = renderHook(() => useSessionTranscript('session-123'));
    
    await waitFor(() => {
      expect(result.current.transcripts).toHaveLength(1);
    });
    
    act(() => {
      result.current.clear();
    });
    
    expect(result.current.transcripts).toEqual([]);
    expect(result.current.totalTranscripts).toBe(0);
  });
});
