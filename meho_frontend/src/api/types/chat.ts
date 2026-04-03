// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Types
 * 
 * Types for chat sessions and messages.
 */

export interface ChatRequest {
  message: string;
  stream?: boolean;
  session_mode?: 'ask' | 'agent';
}

export interface ChatResponse {
  response: string;
  workflow_id?: string;
  result?: import('./workflow').ExecutionResult;
}

export interface ChatSession {
  id: string;
  title: string | null;
  visibility?: 'private' | 'group' | 'tenant';
  session_mode?: 'ask' | 'agent';
  created_at: string;
  updated_at: string;
  message_count?: number;
  is_active?: boolean;  // Phase 59: whether agent is currently processing
  trigger_source?: string | null;  // Phase 75: null=human, automation trigger name
  created_by_name?: string | null;  // Phase 75: session creator display name
}

export interface TeamSession {
  id: string;
  title: string | null;
  visibility: 'group' | 'tenant';
  created_by_name: string | null;
  trigger_source: string | null;
  status: 'awaiting_approval' | 'active' | 'idle';
  pending_approval_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  workflow_id: string | null;
  created_at: string;
  // War room sender attribution (Phase 39)
  sender_id?: string;
  sender_name?: string;
}

export interface SessionWithMessages {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
  visibility?: string;  // Phase 39: session visibility for group detection
  is_active?: boolean;  // Phase 39: whether agent is currently processing
  session_mode?: 'ask' | 'agent';  // Phase 65-05: persisted session mode
  trigger_source?: string | null;  // Phase 75: null=human, automation trigger name
  created_by_name?: string | null;  // Phase 75: session creator display name
}

export interface CreateSessionRequest {
  title?: string;
}

export interface UpdateSessionRequest {
  title: string;
}

export interface AddMessageRequest {
  role: 'user' | 'assistant';
  content: string;
  workflow_id?: string;
}

