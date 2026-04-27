// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat + sessions + approvals + summarize domain client.
 *
 * Migrated from `lib/api-client.ts` in Phase 2 (#283). Method signatures,
 * URLs, and return types match the originals byte-for-byte.
 *
 * Migration note: during Phase 2 the facade `MEHOAPIClient` in
 * `lib/api-client.ts` still implements these chat methods directly (no
 * delegation to this client yet). Phase 4 (#350) deletes the facade and
 * promotes this module to the single source of truth. Until then,
 * callsites are migrated one-by-one to `getChatClient()` and the two
 * implementations coexist.
 *
 * Notable quirk: `createChatStream` uses the native `EventSource` which
 * does not support custom headers. Auth must therefore be carried via
 * cookie/session or a query parameter in production — same as before the
 * refactor.
 */
import type { AxiosInstance } from 'axios';

import type {
  AddMessageRequest,
  ChatMessage,
  ChatRequest,
  ChatResponse,
  ChatSession,
  CreateSessionRequest,
  SessionWithMessages,
  TeamSession,
  UpdateSessionRequest,
} from '../types';
import { getTransport } from './transport';

export function createChatClient(transport: AxiosInstance) {
  return {
    /** Send a chat message (non-streaming). */
    async chat(request: ChatRequest): Promise<ChatResponse> {
      const response = await transport.post<ChatResponse>('/api/chat', request);
      return response.data;
    },

    /**
     * Open an SSE chat stream.
     *
     * Native `EventSource` cannot carry custom headers, so auth is expected
     * to travel via cookie/session or a query parameter.
     */
    createChatStream(request: ChatRequest): EventSource {
      const url = new URL('/api/chat/stream', transport.defaults.baseURL);
      url.searchParams.set('message', request.message);
      return new EventSource(url.toString());
    },

    // ===== Sessions =====

    async createSession(request: CreateSessionRequest = {}): Promise<ChatSession> {
      const response = await transport.post<ChatSession>('/api/chat/sessions', request);
      return response.data;
    },

    async listSessions(limit: number = 50): Promise<ChatSession[]> {
      const response = await transport.get<ChatSession[]>('/api/chat/sessions', {
        params: { limit },
      });
      return response.data;
    },

    async getSession(sessionId: string): Promise<SessionWithMessages> {
      const response = await transport.get<SessionWithMessages>(
        `/api/chat/sessions/${sessionId}`,
      );
      return response.data;
    },

    async updateSession(
      sessionId: string,
      request: UpdateSessionRequest,
    ): Promise<ChatSession> {
      const response = await transport.patch<ChatSession>(
        `/api/chat/sessions/${sessionId}`,
        request,
      );
      return response.data;
    },

    async deleteSession(sessionId: string): Promise<void> {
      await transport.delete(`/api/chat/sessions/${sessionId}`);
    },

    async addMessageToSession(
      sessionId: string,
      request: AddMessageRequest,
    ): Promise<ChatMessage> {
      const response = await transport.post<ChatMessage>(
        `/api/chat/sessions/${sessionId}/messages`,
        request,
      );
      return response.data;
    },

    /** Phase 65-05: persist ask/agent mode toggle. */
    async updateSessionMode(
      sessionId: string,
      mode: 'ask' | 'agent',
    ): Promise<ChatSession> {
      const response = await transport.patch<ChatSession>(
        `/api/chat/sessions/${sessionId}/mode`,
        { session_mode: mode },
      );
      return response.data;
    },

    // ===== Team sessions (Phase 38 — Group Sessions) =====

    async listTeamSessions(): Promise<TeamSession[]> {
      const response = await transport.get<TeamSession[]>('/api/chat/sessions/team');
      return response.data;
    },

    /** Upgrade-only: private → group → tenant. */
    async updateSessionVisibility(
      sessionId: string,
      visibility: string,
    ): Promise<ChatSession> {
      const response = await transport.patch<ChatSession>(
        `/api/chat/sessions/${sessionId}/visibility`,
        { visibility },
      );
      return response.data;
    },

    // ===== Approvals =====

    async approveAction(
      sessionId: string,
      approvalId: string,
      reason?: string,
    ): Promise<{ status: string; message: string; approval_id?: string }> {
      const response = await transport.post(
        `/api/chat/${sessionId}/approve/${approvalId}`,
        { approved: true, reason },
      );
      return response.data;
    },

    async rejectAction(
      sessionId: string,
      approvalId: string,
      reason?: string,
    ): Promise<{ status: string; message: string; approval_id?: string }> {
      const response = await transport.post(
        `/api/chat/${sessionId}/approve/${approvalId}`,
        { approved: false, reason },
      );
      return response.data;
    },

    async getPendingApprovals(sessionId: string): Promise<
      Array<{
        approval_id: string;
        tool_name: string;
        danger_level: string;
        method?: string;
        path?: string;
        description?: string;
        tool_args?: Record<string, unknown>;
        created_at: string;
      }>
    > {
      const response = await transport.get(`/api/chat/${sessionId}/pending-approvals`);
      return response.data;
    },

    /**
     * Phase 63-02: summarize a session and create a new one with the summary.
     *
     * Powers the ContextBar "Start new chat" handoff.
     */
    async summarizeSession(
      sessionId: string,
    ): Promise<{ new_session_id: string; summary: string }> {
      const response = await transport.post(
        `/api/chat/sessions/${sessionId}/summarize`,
      );
      return response.data;
    },
  };
}

let chatClient: ReturnType<typeof createChatClient> | null = null;

export function getChatClient(): ReturnType<typeof createChatClient> {
  if (!chatClient) {
    chatClient = createChatClient(getTransport());
  }
  return chatClient;
}
