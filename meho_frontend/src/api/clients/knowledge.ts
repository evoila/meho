// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Knowledge domain client (tree, documents, chunks, ingestion, versions,
 * connector-scoped documents).
 *
 * Migrated from `lib/api-client.ts` in Phase 2 (#283). Method signatures,
 * URLs, and return types match the originals byte-for-byte.
 *
 * Migration note: during Phase 2 the facade `MEHOAPIClient` in
 * `lib/api-client.ts` still implements these knowledge methods directly
 * (no delegation to this client yet). Phase 4 (#350) deletes the facade
 * and promotes this module to the single source of truth. Until then,
 * callsites are migrated one-by-one to `getKnowledgeClient()` and the two
 * implementations coexist.
 *
 * Note on `listConnectorDocuments` / `deleteConnectorDocument`: these hit
 * `/api/knowledge/connectors/*` URLs (knowledge routes, connector-scoped
 * grouping) but operate on documents, so they live on the knowledge client
 * per the #263 subject-group rule.
 */
import type { AxiosInstance } from 'axios';

import type {
  IngestionJobStatus,
  IngestTextRequest,
  IngestTextResponse,
  KnowledgeSearchRequest,
  ListChunksRequest,
  ListChunksResponse,
  ListDocumentsRequest,
  ListDocumentsResponse,
  SearchKnowledgeResponse,
  UploadDocumentRequest,
  UploadDocumentResponse,
} from '../types';
import type {
  DocumentDetailResponse,
  DocumentFamilyVersionsResponse,
  IngestUrlRequest,
  KnowledgeTreeResponse,
  UploadDocumentVersionRequest,
} from '../types/knowledge';
import { getTransport } from './transport';

export function createKnowledgeClient(transport: AxiosInstance) {
  return {
    /** Knowledge tree hierarchy: Global → Type → Instance. */
    async getKnowledgeTree(): Promise<KnowledgeTreeResponse> {
      const response = await transport.get<KnowledgeTreeResponse>('/api/knowledge/tree');
      return response.data;
    },

    async searchKnowledge(
      request: KnowledgeSearchRequest,
    ): Promise<SearchKnowledgeResponse> {
      const response = await transport.post<SearchKnowledgeResponse>(
        '/api/knowledge/search',
        request,
      );
      return response.data;
    },

    /** Upload a document (multipart form). Scope-aware per `request`. */
    async uploadDocument(
      request: UploadDocumentRequest,
    ): Promise<UploadDocumentResponse> {
      const formData = new FormData();
      formData.append('file', request.file);
      formData.append('knowledge_type', request.knowledge_type);
      formData.append('tags', JSON.stringify(request.tags));
      if (request.connector_id) formData.append('connector_id', request.connector_id);
      if (request.scope_type) formData.append('scope_type', request.scope_type);
      if (request.connector_type_scope)
        formData.append('connector_type_scope', request.connector_type_scope);
      if (request.doc_version) formData.append('doc_version', request.doc_version);

      const response = await transport.post<UploadDocumentResponse>(
        '/api/knowledge/upload',
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } },
      );
      return response.data;
    },

    async ingestUrl(request: IngestUrlRequest): Promise<UploadDocumentResponse> {
      const response = await transport.post<UploadDocumentResponse>(
        '/api/knowledge/ingest-url',
        request,
      );
      return response.data;
    },

    // ===== Ingestion jobs =====

    async getJobStatus(jobId: string): Promise<IngestionJobStatus> {
      const response = await transport.get<IngestionJobStatus>(
        `/api/knowledge/jobs/${jobId}`,
      );
      return response.data;
    },

    async getActiveJobs(tenantId?: string): Promise<IngestionJobStatus[]> {
      const params = new URLSearchParams();
      if (tenantId) params.set('tenant_id', tenantId);

      const response = await transport.get<IngestionJobStatus[]>(
        `/api/knowledge/jobs/active?${params.toString()}`,
      );
      return response.data;
    },

    async resumeJob(
      jobId: string,
    ): Promise<{ job_id: string; status: string; message: string }> {
      const response = await transport.post<{
        job_id: string;
        status: string;
        message: string;
      }>(`/api/knowledge/jobs/${jobId}/resume`);
      return response.data;
    },

    async cancelJob(
      jobId: string,
    ): Promise<{ job_id: string; status: string; message: string }> {
      const response = await transport.post<{
        job_id: string;
        status: string;
        message: string;
      }>(`/api/knowledge/jobs/${jobId}/cancel`);
      return response.data;
    },

    /** Ingest text directly (procedures, lessons, notices). */
    async ingestText(request: IngestTextRequest): Promise<IngestTextResponse> {
      const response = await transport.post<IngestTextResponse>(
        '/api/knowledge/ingest-text',
        request,
      );
      return response.data;
    },

    // ===== Chunks =====

    async listKnowledgeChunks(
      request: ListChunksRequest = {},
    ): Promise<ListChunksResponse> {
      const params = new URLSearchParams();
      if (request.knowledge_type) params.set('knowledge_type', request.knowledge_type);
      if (request.tags) params.set('tags', request.tags);
      if (request.limit) params.set('limit', request.limit.toString());
      if (request.offset) params.set('offset', request.offset.toString());

      const response = await transport.get<ListChunksResponse>(
        `/api/knowledge/chunks?${params.toString()}`,
      );
      return response.data;
    },

    async deleteKnowledgeChunk(chunkId: string): Promise<void> {
      await transport.delete(`/api/knowledge/chunks/${chunkId}`);
    },

    // ===== Documents =====

    async listKnowledgeDocuments(
      request: ListDocumentsRequest = {},
    ): Promise<ListDocumentsResponse> {
      const params = new URLSearchParams();
      if (request.status) params.set('status', request.status);
      if (request.scope_type) params.set('scope_type', request.scope_type);
      if (request.connector_type_scope)
        params.set('connector_type_scope', request.connector_type_scope);
      if (request.limit) params.set('limit', request.limit.toString());
      if (request.offset) params.set('offset', request.offset.toString());

      const query = params.toString();
      const response = await transport.get<ListDocumentsResponse>(
        `/api/knowledge/documents${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    /** Delete a document with progress tracking (returns the job id). */
    async deleteKnowledgeDocument(
      documentId: string,
    ): Promise<{ job_id: string; chunks_to_delete: number }> {
      const response = await transport.delete<{
        job_id: string;
        chunks_to_delete: number;
      }>(`/api/knowledge/documents/${documentId}`);
      return response.data;
    },

    /** Fetch full document detail including chunks for preview rendering. */
    async getDocumentDetail(
      documentId: string,
      params: { chunk_offset?: number; chunk_limit?: number } = {},
    ): Promise<DocumentDetailResponse> {
      const searchParams = new URLSearchParams();
      if (params.chunk_offset != null)
        searchParams.set('chunk_offset', params.chunk_offset.toString());
      if (params.chunk_limit != null)
        searchParams.set('chunk_limit', params.chunk_limit.toString());
      const query = searchParams.toString();
      const response = await transport.get<DocumentDetailResponse>(
        `/api/knowledge/documents/${documentId}/detail${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    // ===== Document versions =====

    /** Upload a new version of an existing document into the same family. */
    async uploadDocumentVersion(
      documentId: string,
      request: UploadDocumentVersionRequest,
    ): Promise<UploadDocumentResponse> {
      const formData = new FormData();
      formData.append('file', request.file);
      formData.append('doc_version', request.doc_version);

      const response = await transport.post<UploadDocumentResponse>(
        `/api/knowledge/documents/${documentId}/versions`,
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } },
      );
      return response.data;
    },

    /** List non-deleted versions in a document family. */
    async listDocumentVersions(
      familyId: string,
    ): Promise<DocumentFamilyVersionsResponse> {
      const response = await transport.get<DocumentFamilyVersionsResponse>(
        `/api/knowledge/families/${familyId}/versions`,
      );
      return response.data;
    },

    // ===== Connector-scoped documents =====
    // URLs live under /api/knowledge/connectors/*; grouped here by subject.

    async listConnectorDocuments(
      connectorId: string,
      params: { limit?: number; offset?: number } = {},
    ): Promise<ListDocumentsResponse> {
      const searchParams = new URLSearchParams();
      if (params.limit) searchParams.set('limit', params.limit.toString());
      if (params.offset) searchParams.set('offset', params.offset.toString());
      const query = searchParams.toString();
      const response = await transport.get<ListDocumentsResponse>(
        `/api/knowledge/connectors/${connectorId}/documents${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    async deleteConnectorDocument(
      connectorId: string,
      documentId: string,
    ): Promise<{ message: string; document_id: string; connector_id: string }> {
      const response = await transport.delete<{
        message: string;
        document_id: string;
        connector_id: string;
      }>(`/api/knowledge/connectors/${connectorId}/documents/${documentId}`);
      return response.data;
    },
  };
}

let knowledgeClient: ReturnType<typeof createKnowledgeClient> | null = null;

export function getKnowledgeClient(): ReturnType<typeof createKnowledgeClient> {
  if (!knowledgeClient) {
    knowledgeClient = createKnowledgeClient(getTransport());
  }
  return knowledgeClient;
}
