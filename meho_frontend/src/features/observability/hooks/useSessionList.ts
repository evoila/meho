// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * useSessionList Hook
 *
 * Fetches paginated list of sessions with filtering.
 * Supports pagination and status filtering.
 */
import { useState, useCallback, useEffect, useRef } from 'react';
import { getAPIClient } from '@/lib/api-client';
import type { SessionListItem, SessionListResponse } from '@/api/types';

interface UseSessionListState {
  sessions: SessionListItem[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  loading: boolean;
  error: string | null;
}

interface UseSessionListReturn extends UseSessionListState {
  /** Refetch current page */
  refetch: () => Promise<void>;
  /** Go to next page */
  nextPage: () => void;
  /** Go to previous page */
  prevPage: () => void;
  /** Go to specific page */
  goToPage: (page: number) => void;
  /** Update page size */
  setPageSize: (size: number) => void;
  /** Clear sessions data */
  clear: () => void;
}

/**
 * Hook to fetch paginated session list.
 *
 * @param params - Optional filter parameters (status)
 * @param options - Hook options
 * @returns Session list state and pagination controls
 *
 * @example
 * ```tsx
 * const {
 *   sessions,
 *   loading,
 *   page,
 *   hasMore,
 *   nextPage,
 *   prevPage,
 * } = useSessionList({ status: 'completed' });
 *
 * return (
 *   <div>
 *     {sessions.map(s => <SessionCard key={s.session_id} session={s} />)}
 *     <button onClick={prevPage} disabled={page === 1}>Prev</button>
 *     <button onClick={nextPage} disabled={!hasMore}>Next</button>
 *   </div>
 * );
 * ```
 */
export function useSessionList(
  params?: { status?: string },
  options?: { enabled?: boolean; initialPageSize?: number }
): UseSessionListReturn {
  const [state, setState] = useState<UseSessionListState>({
    sessions: [],
    total: 0,
    page: 1,
    pageSize: options?.initialPageSize ?? 20,
    hasMore: false,
    loading: false,
    error: null,
  });

  const enabled = options?.enabled !== false;
  
  // Use refs to avoid recreating fetchSessions on every render
  const pageRef = useRef(state.page);
  const pageSizeRef = useRef(state.pageSize);
  const statusRef = useRef(params?.status);
  
  // Update refs when values change (in effect to avoid render-time mutation)
  useEffect(() => {
    pageRef.current = state.page;
    pageSizeRef.current = state.pageSize;
    statusRef.current = params?.status;
  }, [state.page, state.pageSize, params?.status]);

  const fetchSessions = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));

    try {
      const apiClient = getAPIClient();
      // Convert page to offset for backend API
      const offset = (pageRef.current - 1) * pageSizeRef.current;
      const response = await apiClient.observability.listSessions({
        status: statusRef.current,
        limit: pageSizeRef.current,
        offset,
      });

      const typedResponse = response as SessionListResponse;
      // Compute hasMore from offset + sessions.length < total
      const hasMore = typedResponse.offset + typedResponse.sessions.length < typedResponse.total;
      setState((prev) => ({
        ...prev,
        sessions: typedResponse.sessions,
        total: typedResponse.total,
        hasMore,
        loading: false,
        error: null,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch sessions';
      setState((prev) => ({
        ...prev,
        loading: false,
        error: message,
      }));
    }
  }, []); // No dependencies - uses refs

  const nextPage = useCallback(() => {
    setState((prev) => {
      if (prev.hasMore) {
        return { ...prev, page: prev.page + 1 };
      }
      return prev;
    });
  }, []);

  const prevPage = useCallback(() => {
    setState((prev) => {
      if (prev.page > 1) {
        return { ...prev, page: prev.page - 1 };
      }
      return prev;
    });
  }, []);

  const goToPage = useCallback((page: number) => {
    if (page >= 1) {
      setState((prev) => ({ ...prev, page }));
    }
  }, []);

  const setPageSize = useCallback((size: number) => {
    setState((prev) => ({ ...prev, pageSize: size, page: 1 }));
  }, []);

  const clear = useCallback(() => {
    setState({
      sessions: [],
      total: 0,
      page: 1,
      pageSize: options?.initialPageSize ?? 20,
      hasMore: false,
      loading: false,
      error: null,
    });
  }, [options?.initialPageSize]);

  // Fetch when enabled, page, pageSize, or status changes
  useEffect(() => {
    if (enabled) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Data fetching pattern
      fetchSessions();
    }
  }, [enabled, fetchSessions, state.page, state.pageSize, params?.status]);

  return {
    ...state,
    refetch: fetchSessions,
    nextPage,
    prevPage,
    goToPage,
    setPageSize,
    clear,
  };
}
