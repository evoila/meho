// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useSessionTranscript Hook
 *
 * Fetches and manages transcript data for a session.
 * Supports multi-turn conversations with multiple transcripts per session.
 */
import { useState, useCallback, useEffect, useRef } from 'react';
import { getAPIClient } from '@/lib/api-client';
import type { TranscriptItem } from '@/api/types';

interface UseSessionTranscriptState {
  /** All transcripts for the session (one per user message) */
  transcripts: TranscriptItem[];
  /** Total number of transcripts */
  totalTranscripts: number;
  /** Loading state */
  loading: boolean;
  /** Error message if any */
  error: string | null;
}

interface UseSessionTranscriptReturn extends UseSessionTranscriptState {
  /** Refetch transcript data */
  refetch: () => Promise<void>;
  /** Clear current data */
  clear: () => void;
}

interface TranscriptHookParams {
  include_details?: boolean;
}

/**
 * Hook to fetch session transcripts with events and summaries.
 * For multi-turn conversations, returns all transcripts (one per user message).
 *
 * @param sessionId - The session ID to fetch transcripts for
 * @param params - Optional parameters for filtering
 * @param options - Hook options
 * @returns Transcripts state and refetch function
 *
 * @example
 * ```tsx
 * const { transcripts, totalTranscripts, loading, error } = useSessionTranscript(sessionId);
 *
 * if (loading) return <Spinner />;
 * if (error) return <ErrorState message={error} />;
 *
 * return (
 *   <div>
 *     {transcripts.map(t => (
 *       <TranscriptCard key={t.transcript_id} transcript={t} />
 *     ))}
 *   </div>
 * );
 * ```
 */
export function useSessionTranscript(
  sessionId: string | null,
  params?: TranscriptHookParams,
  options?: { enabled?: boolean }
): UseSessionTranscriptReturn {
  const [state, setState] = useState<UseSessionTranscriptState>({
    transcripts: [],
    totalTranscripts: 0,
    loading: false,
    error: null,
  });

  const enabled = options?.enabled !== false && sessionId !== null;

  // Use refs to avoid recreating fetchTranscript on every render
  const sessionIdRef = useRef(sessionId);
  const includeDetailsRef = useRef(params?.include_details);

  // Update refs when values change (in effect to avoid render-time mutation)
  useEffect(() => {
    sessionIdRef.current = sessionId;
    includeDetailsRef.current = params?.include_details;
  }, [sessionId, params?.include_details]);

  const fetchTranscript = useCallback(async () => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId) return;

    setState((prev) => ({ ...prev, loading: true, error: null }));

    try {
      const apiClient = getAPIClient();
      const response = await apiClient.observability.getTranscript(currentSessionId, {
        include_details: includeDetailsRef.current,
      });

      // Map the response to TranscriptItem type
      const transcripts: TranscriptItem[] = response.transcripts.map((t) => ({
        transcript_id: t.transcript_id,
        user_query: t.user_query,
        created_at: t.created_at,
        status: t.status,
        summary: {
          session_id: t.summary.session_id,
          total_events: t.events.length,
          llm_calls: t.summary.total_llm_calls,
          operation_calls: t.summary.total_operation_calls,
          sql_queries: t.summary.total_sql_queries,
          tool_calls: t.summary.total_tool_calls,
          total_tokens: t.summary.total_tokens,
          estimated_cost_usd: t.summary.total_cost_usd ?? null,
          total_duration_ms: t.summary.total_duration_ms,
          error_count: 0, // Not provided by backend
          start_time: t.summary.created_at,
          end_time: t.summary.completed_at ?? null,
        },
        events: t.events.map((e) => ({
          id: e.id,
          timestamp: e.timestamp,
          type: e.type,
          summary: e.summary,
          details: e.details as TranscriptItem['events'][0]['details'],
          parent_event_id: e.parent_event_id,
          step_number: e.step_number,
          node_name: e.node_name,
          agent_name: e.agent_name,
          duration_ms: e.duration_ms,
        })),
      }));

      setState({
        transcripts,
        totalTranscripts: response.total_transcripts,
        loading: false,
        error: null,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch transcript';
      setState((prev) => ({
        ...prev,
        loading: false,
        error: message,
      }));
    }
  }, []); // No dependencies - uses refs

  const clear = useCallback(() => {
    setState({
      transcripts: [],
      totalTranscripts: 0,
      loading: false,
      error: null,
    });
  }, []);

  // Fetch on mount and when sessionId or include_details changes
  useEffect(() => {
    if (enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Data fetching pattern
      fetchTranscript();
    }
  }, [enabled, fetchTranscript, sessionId, params?.include_details]);

  return {
    ...state,
    refetch: fetchTranscript,
    clear,
  };
}
