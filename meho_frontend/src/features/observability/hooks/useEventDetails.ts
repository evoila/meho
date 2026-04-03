// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useEventDetails Hook
 *
 * Fetches detailed information for a specific event.
 * Includes LLM, HTTP, SQL, and tool details.
 */
import { useState, useCallback, useEffect } from 'react';
import { getAPIClient } from '@/lib/api-client';
import type { EventResponse } from '@/api/types';

interface UseEventDetailsState {
  event: EventResponse | null;
  loading: boolean;
  error: string | null;
}

interface UseEventDetailsReturn extends UseEventDetailsState {
  /** Refetch event details */
  refetch: () => Promise<void>;
  /** Clear current data */
  clear: () => void;
}

/**
 * Hook to fetch detailed event information.
 *
 * @param sessionId - The session containing the event
 * @param eventId - The event ID to fetch details for
 * @param options - Hook options
 * @returns Event details state and refetch function
 *
 * @example
 * ```tsx
 * const { event, loading, error, refetch } = useEventDetails(sessionId, eventId);
 *
 * if (loading) return <Spinner />;
 * if (error) return <ErrorState message={error} />;
 *
 * return <EventViewer event={event} />;
 * ```
 */
export function useEventDetails(
  sessionId: string | null,
  eventId: string | null,
  options?: { enabled?: boolean }
): UseEventDetailsReturn {
  const [state, setState] = useState<UseEventDetailsState>({
    event: null,
    loading: false,
    error: null,
  });

  const enabled = options?.enabled !== false && sessionId !== null && eventId !== null;

  const fetchEventDetails = useCallback(async () => {
    if (!sessionId || !eventId) return;

    setState((prev) => ({ ...prev, loading: true, error: null }));

    try {
      const apiClient = getAPIClient();
      const response = await apiClient.observability.getEventDetails(sessionId, eventId);

      setState({
        event: response as EventResponse,
        loading: false,
        error: null,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch event details';
      setState((prev) => ({
        ...prev,
        loading: false,
        error: message,
      }));
    }
  }, [sessionId, eventId]);

  const clear = useCallback(() => {
    setState({
      event: null,
      loading: false,
      error: null,
    });
  }, []);

  // Fetch on mount and when dependencies change
  useEffect(() => {
    if (enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Data fetching pattern
      fetchEventDetails();
    }
  }, [enabled, fetchEventDetails]);

  return {
    ...state,
    refetch: fetchEventDetails,
    clear,
  };
}
