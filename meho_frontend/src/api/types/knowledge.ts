// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Knowledge Types
 * 
 * Types for knowledge base operations, documents, and ingestion.
 */

export interface KnowledgeSearchRequest {
  query: string;
  top_k?: number;
  connector_id?: string;
}

export interface KnowledgeChunk {
  id: string;
  text: string;
  metadata: Record<string, unknown>;
  score?: number;
}

export interface SearchKnowledgeResponse {
  chunks: KnowledgeSearchResult[];
  total: number;
}

export interface UploadDocumentRequest {
  file: File;
  knowledge_type: 'documentation' | 'procedure';
  tags: string[];
  connector_id?: string;
  scope_type?: 'global' | 'type' | 'instance';
  connector_type_scope?: string;
}

export interface UploadDocumentResponse {
  job_id: string;
  status: string;
}

export interface IngestionProgress {
  total_chunks: number;
  chunks_processed: number;
  chunks_created: number;
  percent: number;
  current_stage?: string;
  stage_progress?: number;
  overall_progress?: number;
  status_message?: string;
  estimated_completion?: string;
}

export interface IngestionJobStatus {
  id: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  progress: IngestionProgress;
  started_at: string;
  completed_at?: string;
  error?: string;
  error_stage?: string;
  error_chunk_index?: number;
  error_details?: Record<string, unknown>;
}

export interface IngestTextRequest {
  text: string;
  knowledge_type: 'documentation' | 'procedure' | 'event';
  tags: string[];
  priority?: number;
  expires_at?: string;
  system_id?: string;
  scope: 'global' | 'tenant' | 'system' | 'team' | 'private';
}

export interface IngestTextResponse {
  chunk_ids: string[];
  count: number;
}

export interface KnowledgeDocument {
  id: string;
  filename?: string;
  knowledge_type: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  tags: string[];
  file_size?: number;
  total_chunks?: number;
  chunks_created: number;
  chunks_processed: number;
  preview_text?: string;
  error?: string;
  started_at: string;
  completed_at?: string;
  progress?: IngestionProgress;
}

export interface KnowledgeChunkDetail {
  id: string;
  text: string;
  tenant_id?: string;
  system_id?: string;
  user_id?: string;
  tags: string[];
  knowledge_type: string;
  priority: number;
  created_at: string;
  expires_at?: string;
  source_uri?: string;
}

export interface ListChunksRequest {
  knowledge_type?: string;
  tags?: string;
  limit?: number;
  offset?: number;
}

export interface ListChunksResponse {
  chunks: KnowledgeChunkDetail[];
  total: number;
}

export interface ListDocumentsRequest {
  status?: string;
  limit?: number;
  offset?: number;
}

export interface ListDocumentsResponse {
  documents: KnowledgeDocument[];
  total: number;
}

export interface ConnectorKnowledgeDocument {
  id: string;
  filename: string;
  knowledge_type: string;
  tags: string[];
  status: string;
  total_chunks: number;
  chunks_created: number;
  chunks_processed: number;
  file_size?: number;
  created_at: string;
  connector_id: string;
  error?: string;
  progress?: IngestionProgress;
}

export interface KnowledgeSearchResult {
  id: string;
  text: string;
  score?: number;
  tags: string[];
  knowledge_type: string;
  connector_id: string;
  connector_name: string;
  connector_type: string;
}

// ---- Knowledge Tree Types (Phase 65) ----

export interface KnowledgeTreeInstanceNode {
  connector_id: string;
  connector_name: string;
  document_count: number;
  chunk_count: number;
}

export interface KnowledgeTreeTypeNode {
  connector_type: string;
  display_name: string;
  document_count: number;
  chunk_count: number;
  instances: KnowledgeTreeInstanceNode[];
}

export interface KnowledgeTreeConnectorType {
  value: string;
  display_name: string;
}

export interface KnowledgeTreeResponse {
  global: {
    document_count: number;
    chunk_count: number;
  };
  types: KnowledgeTreeTypeNode[];
  all_connector_types: KnowledgeTreeConnectorType[];
}

export interface IngestUrlRequest {
  url: string;
  scope_type: 'global' | 'type' | 'instance';
  connector_type_scope?: string;
  connector_id?: string;
  knowledge_type?: string;
  tags?: string[];
}

export type KnowledgeScope = {
  scope_type: 'global' | 'type' | 'instance';
  connector_type_scope?: string;
  connector_id?: string;
}

