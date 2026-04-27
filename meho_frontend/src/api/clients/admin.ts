// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Admin domain client: dashboard + admin config + health + flattened
 * observability methods + audit events.
 *
 * The original monolith had a nested `observability = { ... }` namespace on
 * the client; Phase 4 flattens those onto this domain client with an
 * `observability` prefix (`observabilityListSessions`, etc.) to avoid
 * collision with other admin methods and to keep the method list flat and
 * greppable -- consistent with how we named things like
 * `getScheduledTaskRuns` or `listConnectorEvents`.
 *
 * Migrated from `lib/api-client.ts` in Phase 4 (#350).
 */
import type { AxiosInstance } from 'axios';
import type {
  DashboardStats,
  ActivityItem,
} from '../types';
import type {
  AuditEventsResponse,
  AuditEventFilters,
} from '../types/audit';
import { getTransport } from './transport';

export function createAdminClient(transport: AxiosInstance) {
  return {
    // ===== Dashboard =====

    async getDashboardStats(): Promise<DashboardStats> {
      const response = await transport.get<DashboardStats>('/api/admin/dashboard/stats');
      return response.data;
    },

    async getDashboardActivity(limit: number = 20): Promise<ActivityItem[]> {
      const response = await transport.get<ActivityItem[]>('/api/admin/dashboard/activity', {
        params: { limit },
      });
      return response.data;
    },

    // ===== Admin config (tenant settings) =====

    async getAdminConfig<T = Record<string, unknown>>(): Promise<T> {
      const response = await transport.get<T>('/api/admin/config');
      return response.data;
    },

    async updateAdminConfig<T = Record<string, unknown>>(data: {
      installation_context?: string;
      model_override?: string;
      temperature_override?: number;
    }): Promise<T> {
      const response = await transport.put<T>('/api/admin/config', data);
      return response.data;
    },

    async getAdminModels<T = Record<string, unknown>>(): Promise<T> {
      const response = await transport.get<T>('/api/admin/models');
      return response.data;
    },

    async getPromptPreview<T = Record<string, unknown>>(): Promise<T> {
      const response = await transport.get<T>('/api/admin/prompt/preview');
      return response.data;
    },

    async getConfigAudit<T = Record<string, unknown>>(): Promise<T> {
      const response = await transport.get<T>('/api/admin/config/audit');
      return response.data;
    },

    // ===== Health =====

    async healthCheck(): Promise<{ status: string; service: string; version: string }> {
      const response = await transport.get('/health');
      return response.data;
    },

    // ===== Observability (flattened from nested namespace) =====

    /**
     * List sessions with pagination and filtering.
     * Matches backend `list_sessions` in `routes_observability.py`.
     */
    async observabilityListSessions(params?: {
      limit?: number;
      offset?: number;
      status?: string;
    }): Promise<{
      sessions: Array<{
        session_id: string;
        created_at: string;
        status: string;
        user_query?: string | null;
        total_llm_calls: number;
        total_tokens: number;
        total_duration_ms: number;
      }>;
      total: number;
      offset: number;
      limit: number;
    }> {
      const urlParams = new URLSearchParams();
      if (params?.limit) urlParams.set('limit', params.limit.toString());
      if (params?.offset) urlParams.set('offset', params.offset.toString());
      if (params?.status && params.status !== 'all') urlParams.set('status', params.status);

      const response = await transport.get(
        `/api/observability/sessions?${urlParams.toString()}`,
      );
      return response.data;
    },

    /**
     * Get all transcripts for a session (multi-turn conversation support).
     * Returns multiple transcripts, one for each user message/execution.
     */
    async observabilityGetTranscript(
      sessionId: string,
      params?: {
        event_types?: string[];
        include_details?: boolean;
        limit?: number;
        offset?: number;
      },
    ): Promise<{
      session_id: string;
      transcripts: Array<{
        transcript_id: string;
        user_query?: string | null;
        created_at: string;
        status: string;
        summary: {
          session_id: string;
          status: string;
          created_at: string;
          completed_at?: string | null;
          total_llm_calls: number;
          total_operation_calls: number;
          total_sql_queries: number;
          total_tool_calls: number;
          total_tokens: number;
          total_cost_usd?: number | null;
          total_duration_ms: number;
          user_query?: string | null;
          agent_type?: string | null;
        };
        events: Array<{
          id: string;
          timestamp: string;
          type: string;
          summary: string;
          details: Record<string, unknown>;
          parent_event_id?: string | null;
          step_number?: number | null;
          node_name?: string | null;
          agent_name?: string | null;
          duration_ms?: number | null;
        }>;
      }>;
      total_transcripts: number;
    }> {
      const urlParams = new URLSearchParams();
      if (params?.event_types?.length)
        urlParams.set('event_types', params.event_types.join(','));
      if (params?.include_details !== undefined)
        urlParams.set('include_details', String(params.include_details));
      if (params?.limit) urlParams.set('limit', params.limit.toString());
      if (params?.offset) urlParams.set('offset', params.offset.toString());

      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/transcript?${urlParams.toString()}`,
      );
      return response.data;
    },

    async observabilityGetSummary(sessionId: string): Promise<{
      session_id: string;
      total_events: number;
      llm_calls: number;
      operation_calls: number;
      sql_queries: number;
      tool_calls: number;
      total_tokens: number;
      estimated_cost_usd: number | null;
      total_duration_ms: number | null;
      error_count: number;
      start_time: string | null;
      end_time: string | null;
    }> {
      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/summary`,
      );
      return response.data;
    },

    async observabilityGetEventDetails(
      sessionId: string,
      eventId: string,
    ): Promise<{
      id: string;
      timestamp: string;
      type: string;
      summary: string;
      details: Record<string, unknown>;
      parent_event_id?: string | null;
      step_number?: number | null;
      node_name?: string | null;
      agent_name?: string | null;
      duration_ms?: number | null;
    }> {
      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/events/${eventId}`,
      );
      return response.data;
    },

    async observabilityGetLLMCalls(
      sessionId: string,
      params?: {
        include_messages?: boolean;
        include_response?: boolean;
      },
    ): Promise<
      Array<{
        id: string;
        timestamp: string;
        type: string;
        summary: string;
        details: Record<string, unknown>;
        duration_ms?: number | null;
      }>
    > {
      const urlParams = new URLSearchParams();
      if (params?.include_messages !== undefined)
        urlParams.set('include_messages', String(params.include_messages));
      if (params?.include_response !== undefined)
        urlParams.set('include_response', String(params.include_response));

      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/llm-calls?${urlParams.toString()}`,
      );
      return response.data;
    },

    async observabilityGetOperationCalls(
      sessionId: string,
      params?: {
        include_headers?: boolean;
        include_body?: boolean;
        status_filter?: 'all' | 'success' | 'error';
      },
    ): Promise<
      Array<{
        id: string;
        timestamp: string;
        type: string;
        summary: string;
        details: Record<string, unknown>;
        duration_ms?: number | null;
      }>
    > {
      const urlParams = new URLSearchParams();
      if (params?.include_headers !== undefined)
        urlParams.set('include_headers', String(params.include_headers));
      if (params?.include_body !== undefined)
        urlParams.set('include_body', String(params.include_body));
      if (params?.status_filter && params.status_filter !== 'all')
        urlParams.set('status_filter', params.status_filter);

      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/operation-calls?${urlParams.toString()}`,
      );
      return response.data;
    },

    async observabilityGetSQLQueries(
      sessionId: string,
      params?: {
        include_results?: boolean;
        limit?: number;
      },
    ): Promise<
      Array<{
        id: string;
        timestamp: string;
        type: string;
        summary: string;
        details: Record<string, unknown>;
        duration_ms?: number | null;
      }>
    > {
      const urlParams = new URLSearchParams();
      if (params?.include_results !== undefined)
        urlParams.set('include_results', String(params.include_results));
      if (params?.limit) urlParams.set('limit', params.limit.toString());

      const response = await transport.get(
        `/api/observability/sessions/${sessionId}/sql-queries?${urlParams.toString()}`,
      );
      return response.data;
    },

    async observabilitySearch(params: {
      query: string;
      session_id?: string;
      event_types?: string[];
      from_date?: string;
      to_date?: string;
      limit?: number;
    }): Promise<{
      results: Array<{
        session_id: string;
        event_id: string;
        event_type: string;
        summary: string;
        timestamp: string;
        match_field: string;
        match_snippet: string;
        score: number;
      }>;
      total: number;
      query: string;
      took_ms: number;
    }> {
      const urlParams = new URLSearchParams();
      urlParams.set('query', params.query);
      if (params.session_id) urlParams.set('session_id', params.session_id);
      if (params.event_types?.length)
        urlParams.set('event_types', params.event_types.join(','));
      if (params.from_date) urlParams.set('from_date', params.from_date);
      if (params.to_date) urlParams.set('to_date', params.to_date);
      if (params.limit) urlParams.set('limit', params.limit.toString());

      const response = await transport.get(
        `/api/observability/search?${urlParams.toString()}`,
      );
      return response.data;
    },

    // ===== Audit events =====

    /**
     * Fetch audit events for admin view with optional filters.
     *
     * The `resource_type` filter is critical for contextual audit sections
     * on the Connectors and Knowledge pages.
     */
    async getAuditEvents(filters?: AuditEventFilters): Promise<AuditEventsResponse> {
      const params = new URLSearchParams();
      if (filters?.event_type) params.set('event_type', filters.event_type);
      if (filters?.resource_type) params.set('resource_type', filters.resource_type);
      if (filters?.user_id) params.set('user_id', filters.user_id);
      if (filters?.offset !== undefined) params.set('offset', String(filters.offset));
      if (filters?.limit !== undefined) params.set('limit', String(filters.limit));

      const qs = params.toString();
      const response = await transport.get<AuditEventsResponse>(
        `/api/audit/events${qs ? `?${qs}` : ''}`,
      );
      return response.data;
    },

    /**
     * Fetch the current user's own activity log (non-admin personal view).
     */
    async getMyActivity(offset = 0, limit = 50): Promise<AuditEventsResponse> {
      const params = new URLSearchParams();
      if (offset) params.set('offset', String(offset));
      if (limit !== 50) params.set('limit', String(limit));

      const qs = params.toString();
      const response = await transport.get<AuditEventsResponse>(
        `/api/audit/my-activity${qs ? `?${qs}` : ''}`,
      );
      return response.data;
    },
  };
}

let adminClient: ReturnType<typeof createAdminClient> | null = null;

export function getAdminClient(): ReturnType<typeof createAdminClient> {
  if (!adminClient) {
    adminClient = createAdminClient(getTransport());
  }
  return adminClient;
}
