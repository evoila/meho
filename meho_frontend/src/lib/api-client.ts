// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * API Client for MEHO Backend
 *
 * Provides typed methods for calling the MEHO API with authentication.
 *
 * Types are imported from @/api/types for better organization.
 *
 * 401 Retry Queue:
 * - When a 401 is received, the interceptor queues the request and
 *   calls refreshTokenFn (registered by AuthProvider).
 * - On success all queued requests are replayed with the new token.
 * - On failure the session expired callback fires.
 */

import axios, { type AxiosInstance, type AxiosError, type InternalAxiosRequestConfig } from 'axios';

// ---------------------------------------------------------------------------
// 401 Retry Queue (module-level, shared across all instances)
// ---------------------------------------------------------------------------

interface FailedRequest {
  resolve: (token: string) => void;
  reject: (error: unknown) => void;
}

let isRefreshing = false;
let failedQueue: FailedRequest[] = [];
let refreshTokenFn: (() => Promise<string | null>) | null = null;
let sessionExpiredCallback: (() => void) | null = null;

function processQueue(error: unknown, token: string | null = null) {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else if (token) {
      prom.resolve(token);
    }
  });
  failedQueue = [];
}

/**
 * Register the token refresh function (called by AuthProvider).
 */
export function setRefreshTokenFn(fn: () => Promise<string | null>) {
  refreshTokenFn = fn;
}

/**
 * Register callback for when all refresh attempts fail (session expired).
 */
export function onSessionExpired(fn: () => void) {
  sessionExpiredCallback = fn;
}

/**
 * Trigger the session expired callback from SSE hooks (Phase 66).
 * SSE streams (useSessionEvents, useChatStreaming) can't access React context
 * directly, so they call this to surface the SessionExpiredModal.
 */
export function triggerSessionExpired() {
  sessionExpiredCallback?.();
}

// Re-export all types for backwards compatibility
export type {
  Workflow,
  Plan,
  PlanStep,
  ExecutionResult,
  ChatRequest,
  ChatResponse,
  ChatSession,
  ChatMessage,
  SessionWithMessages,
  CreateSessionRequest,
  UpdateSessionRequest,
  AddMessageRequest,
  TeamSession,
  KnowledgeSearchRequest,
  KnowledgeChunk,
  // Orchestrator Skill types (Phase 52 - Orchestrator Skills Frontend)
  OrchestratorSkillSummary,
  OrchestratorSkill,
  CreateSkillRequest,
  UpdateSkillRequest,
  GenerateSkillRequest,
  GenerateSkillResponse,
  SearchKnowledgeResponse,
  UploadDocumentRequest,
  UploadDocumentResponse,
  IngestionJobStatus,
  IngestTextRequest,
  IngestTextResponse,
  KnowledgeDocument,
  KnowledgeChunkDetail,
  ListChunksRequest,
  ListChunksResponse,
  ListDocumentsRequest,
  ListDocumentsResponse,
  ConnectorType,
  SOAPConnectorConfig,
  SOAPTypeProperty,
  SOAPTypeDefinition,
  Connector,
  CreateConnectorRequest,
  UpdateConnectorRequest,
  CreateVMwareConnectorRequest,
  VMwareConnectorResponse,
  CreateProxmoxConnectorRequest,
  ProxmoxConnectorResponse,
  CreateKubernetesConnectorRequest,
  KubernetesConnectorResponse,
  CreateGCPConnectorRequest,
  GCPConnectorResponse,
  CreateAzureConnectorRequest,
  AzureConnectorResponse,
  CreateAWSConnectorRequest,
  AWSConnectorResponse,
  CreatePrometheusConnectorRequest,
  PrometheusConnectorResponse,
  CreateLokiConnectorRequest,
  LokiConnectorResponse,
  CreateTempoConnectorRequest,
  TempoConnectorResponse,
  CreateAlertmanagerConnectorRequest,
  AlertmanagerConnectorResponse,
  CreateJiraConnectorRequest,
  JiraConnectorResponse,
  CreateConfluenceConnectorRequest,
  ConfluenceConnectorResponse,
  EmailProviderType,
  CreateEmailConnectorRequest,
  EmailConnectorResponse,
  EmailDeliveryLogEntry,
  CreateArgoConnectorRequest,
  ArgoConnectorResponse,
  CreateGitHubConnectorRequest,
  GitHubConnectorResponse,
  CreateMCPConnectorRequest,
  MCPConnectorResponse,
  CreateSlackConnectorRequest,
  SlackConnectorResponse,
  Endpoint,
  ParameterField,
  ParameterMetadata,
  UpdateEndpointRequest,
  TestEndpointRequest,
  TestEndpointResponse,
  CredentialStatus,
  TestConnectionRequest,
  TestConnectionResponse,
  TestAuthRequest,
  TestAuthResponse,
  ConnectorOperation,
  ConnectorEntityType,
  RecipeParameter,
  Recipe,
  RecipeExecution,
  APIError,
  SubscriptionTier,
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
  DashboardStats,
  ActivityType,
  ActivityItem,
  // Export/Import types (TASK-142)
  ExportFormat,
  ConflictStrategy,
  ExportConnectorsRequest,
  ImportConnectorsRequest,
  ImportConnectorsResponse,
  // Skill types (Phase 7 - Skill Editor UI)
  RegenerateSkillResponse,
  // Health types (Phase 24 - Health Monitoring)
  ConnectorHealth,
  // Memory types (Phase 13 - Memory UI)
  MemoryType,
  ConfidenceLevel,
  MemoryResponse,
  MemoryUpdate,
  // Event types (Phase 94 - Events System + Response Channels)
  EventRegistration,
  EventCreateResponse,
  EventHistoryEntry,
  EventHistoryResponse,
  EventTestStep,
  EventTestResponse,
  // Scheduled Task types (Phase 45 - Scheduled Tasks)
  ScheduledTask,
  ScheduledTaskRun,
  CreateScheduledTaskRequest,
  UpdateScheduledTaskRequest,
  ParseScheduleResponse,
  ValidateCronResponse,
} from '../api/types';

// Import types for internal use
import type {
  ChatRequest,
  ChatResponse,
  ChatSession,
  SessionWithMessages,
  CreateSessionRequest,
  UpdateSessionRequest,
  AddMessageRequest,
  ChatMessage,
  TeamSession,
  KnowledgeSearchRequest,
  SearchKnowledgeResponse,
  UploadDocumentRequest,
  UploadDocumentResponse,
  IngestionJobStatus,
  IngestTextRequest,
  IngestTextResponse,
  ListChunksRequest,
  ListChunksResponse,
  ListDocumentsRequest,
  ListDocumentsResponse,
  Connector,
  CreateConnectorRequest,
  UpdateConnectorRequest,
  CreateVMwareConnectorRequest,
  VMwareConnectorResponse,
  CreateProxmoxConnectorRequest,
  ProxmoxConnectorResponse,
  CreateKubernetesConnectorRequest,
  KubernetesConnectorResponse,
  CreateGCPConnectorRequest,
  GCPConnectorResponse,
  CreateAzureConnectorRequest,
  AzureConnectorResponse,
  CreateAWSConnectorRequest,
  AWSConnectorResponse,
  CreatePrometheusConnectorRequest,
  PrometheusConnectorResponse,
  CreateLokiConnectorRequest,
  LokiConnectorResponse,
  CreateTempoConnectorRequest,
  TempoConnectorResponse,
  CreateAlertmanagerConnectorRequest,
  AlertmanagerConnectorResponse,
  CreateJiraConnectorRequest,
  JiraConnectorResponse,
  CreateConfluenceConnectorRequest,
  ConfluenceConnectorResponse,
  CreateEmailConnectorRequest,
  EmailConnectorResponse,
  EmailDeliveryLogEntry,
  CreateArgoConnectorRequest,
  ArgoConnectorResponse,
  CreateGitHubConnectorRequest,
  GitHubConnectorResponse,
  CreateMCPConnectorRequest,
  MCPConnectorResponse,
  CreateSlackConnectorRequest,
  SlackConnectorResponse,
  Endpoint,
  UpdateEndpointRequest,
  TestEndpointRequest,
  TestEndpointResponse,
  CredentialStatus,
  TestConnectionRequest,
  TestConnectionResponse,
  TestAuthRequest,
  TestAuthResponse,
  SOAPTypeDefinition,
  ConnectorOperation,
  ConnectorEntityType,
  Recipe,
  RecipeParameter,
  RecipeExecution,
  APIError,
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
  DashboardStats,
  ActivityItem,
  // Export/Import types (TASK-142)
  ExportConnectorsRequest,
  ImportConnectorsRequest,
  ImportConnectorsResponse,
  // Skill types (Phase 7 - Skill Editor UI)
  RegenerateSkillResponse,
  // Health types (Phase 24 - Health Monitoring)
  ConnectorHealth,
  // Memory types (Phase 13 - Memory UI)
  MemoryResponse,
  MemoryUpdate,
  // Event types (Phase 94 - Events System + Response Channels)
  EventRegistration,
  EventCreateResponse,
  EventHistoryResponse,
  EventTestResponse,
  // Scheduled Task types (Phase 45 - Scheduled Tasks)
  ScheduledTask,
  ScheduledTaskRun,
  CreateScheduledTaskRequest,
  UpdateScheduledTaskRequest,
  ParseScheduleResponse,
  ValidateCronResponse,
  // Orchestrator Skill types (Phase 52 - Orchestrator Skills Frontend)
  OrchestratorSkillSummary,
  OrchestratorSkill,
  CreateSkillRequest,
  UpdateSkillRequest,
  GenerateSkillRequest,
  GenerateSkillResponse,
} from '../api/types';

/**
 * MEHO API Client
 */
export class MEHOAPIClient {
  readonly client: AxiosInstance;
  private token: string | null = null;
  private tenantContext: string | null = null;

  constructor(baseURL: string = 'http://127.0.0.1:8000') {
    this.client = axios.create({
      baseURL,
      timeout: 300000, // 300 seconds (5 minutes) - Large OpenAPI specs can take time to ingest
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Add request interceptor to attach auth token and tenant context
    this.client.interceptors.request.use((config) => {
      if (this.token) {
        config.headers.Authorization = `Bearer ${this.token}`;
      }
      // Add tenant context header for superadmin tenant switching (TASK-140 Phase 2)
      if (this.tenantContext) {
        config.headers['X-Acting-As-Tenant'] = this.tenantContext;
      }
      return config;
    });

    // Add response interceptor with 401 retry queue
    this.client.interceptors.response.use(
      (response) => response,
      async (error: AxiosError) => { // NOSONAR (cognitive complexity)
        const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

        // 401 handling with retry queue
        if (
          error.response?.status === 401 &&
          originalRequest &&
          !originalRequest._retry &&
          // Don't intercept auth-related URLs
          !originalRequest.url?.includes('/auth/') &&
          !originalRequest.url?.includes('/realms/')
        ) {
          if (isRefreshing) {
            // Another refresh in progress -- queue this request
            return new Promise<string>((resolve, reject) => {
              failedQueue.push({ resolve, reject });
            }).then((newToken) => {
              originalRequest.headers.Authorization = `Bearer ${newToken}`;
              return this.client(originalRequest);
            });
          }

          originalRequest._retry = true;
          isRefreshing = true;

          try {
            if (!refreshTokenFn) {
              throw new Error('No refresh token function registered');
            }
            const newToken = await refreshTokenFn();
            if (newToken) {
              this.token = newToken;
              processQueue(null, newToken);
              originalRequest.headers.Authorization = `Bearer ${newToken}`;
              return this.client(originalRequest);
            } else {
              processQueue(new Error('Token refresh failed'));
              sessionExpiredCallback?.();
              return Promise.reject(error);
            }
          } catch (refreshError) {
            processQueue(refreshError);
            sessionExpiredCallback?.();
            return Promise.reject(refreshError);
          } finally {
            isRefreshing = false;
          }
        }

        // Standard error handling for non-401 or already-retried requests
        if (error.response) {
          const data = error.response.data as Record<string, unknown> | undefined;
          let errorMessage = 'An error occurred';

          const dataObj = data as Record<string, unknown> | undefined;
          const detail = dataObj?.detail;
          const errorField = dataObj?.error as Record<string, unknown> | undefined;

          if (typeof detail === 'string') {
            errorMessage = detail;
          } else if (typeof detail === 'object' && detail !== null && typeof (detail as Record<string, unknown>).message === 'string') {
            errorMessage = (detail as Record<string, unknown>).message as string;
          } else if (typeof errorField?.message === 'string') {
            errorMessage = errorField.message as string;
          } else if (typeof dataObj?.message === 'string') {
            errorMessage = dataObj.message as string;
          }

          const apiError: APIError = (errorField && typeof errorField.message === 'string' && typeof errorField.type === 'string')
            ? { message: errorField.message as string, type: errorField.type as string, status_code: (errorField.status_code as number) ?? error.response.status }
            : {
              message: errorMessage,
              type: 'UnknownError',
              status_code: error.response.status,
            };
          throw apiError;
        }

        // Network error (no response from server)
        if (error.request) {
          if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
            throw new Error(`Request to ${this.client.defaults.baseURL} timed out after ${this.client.defaults.timeout}ms. The operation may be taking longer than expected.`);
          }
          throw new Error(`Cannot connect to API at ${this.client.defaults.baseURL}. Make sure the backend is running.`);
        }

        throw error;
      }
    );
  }

  /**
   * Set authentication token
   */
  setToken(token: string) {
    this.token = token;
  }

  /**
   * Clear authentication token
   */
  clearToken() {
    this.token = null;
  }

  /**
   * Get the current in-memory auth token.
   * Used by code that needs the token for raw fetch() calls (e.g. topologyApi).
   */
  getToken(): string | null {
    return this.token;
  }

  // ===== Tenant Context Switching (TASK-140 Phase 2) =====

  /**
   * Set tenant context for superadmin operations.
   * When set, all API requests will include the X-Acting-As-Tenant header.
   * This allows superadmins to operate within a tenant's context.
   */
  setTenantContext(tenantId: string) {
    this.tenantContext = tenantId;
  }

  /**
   * Clear the tenant context.
   * After calling this, API requests will use the user's original tenant.
   */
  clearTenantContext() {
    this.tenantContext = null;
  }

  /**
   * Get the current tenant context (if any)
   */
  getTenantContext(): string | null {
    return this.tenantContext;
  }

  // ===== Chat Endpoints =====

  /**
   * Send a chat message (non-streaming)
   */
  async chat(request: ChatRequest): Promise<ChatResponse> {
    const response = await this.client.post<ChatResponse>('/api/chat', request);
    return response.data;
  }

  /**
   * Send a chat message with SSE streaming
   * Returns an EventSource for receiving real-time updates
   */
  createChatStream(request: ChatRequest): EventSource {
    const url = new URL('/api/chat/stream', this.client.defaults.baseURL);
    url.searchParams.set('message', request.message);
    
    // Note: Native EventSource does not support custom headers.
    // Auth should be handled via cookie/session or query param in production.
    const eventSource = new EventSource(url.toString());

    return eventSource;
  }

  // ===== Chat Session Endpoints =====

  /**
   * Create a new chat session
   */
  async createSession(request: CreateSessionRequest = {}): Promise<ChatSession> {
    const response = await this.client.post<ChatSession>('/api/chat/sessions', request);
    return response.data;
  }

  /**
   * List all chat sessions for current user
   */
  async listSessions(limit: number = 50): Promise<ChatSession[]> {
    const response = await this.client.get<ChatSession[]>('/api/chat/sessions', {
      params: { limit }
    });
    return response.data;
  }

  /**
   * Get a specific chat session with all messages
   */
  async getSession(sessionId: string): Promise<SessionWithMessages> {
    const response = await this.client.get<SessionWithMessages>(`/api/chat/sessions/${sessionId}`);
    return response.data;
  }

  /**
   * Update session metadata (e.g., title)
   */
  async updateSession(sessionId: string, request: UpdateSessionRequest): Promise<ChatSession> {
    const response = await this.client.patch<ChatSession>(`/api/chat/sessions/${sessionId}`, request);
    return response.data;
  }

  /**
   * Delete a chat session
   */
  async deleteSession(sessionId: string): Promise<void> {
    await this.client.delete(`/api/chat/sessions/${sessionId}`);
  }

  /**
   * Add a message to a chat session
   */
  async addMessageToSession(sessionId: string, request: AddMessageRequest): Promise<ChatMessage> {
    const response = await this.client.post<ChatMessage>(`/api/chat/sessions/${sessionId}/messages`, request);
    return response.data;
  }

  /**
   * Update session mode (ask/agent)
   * Phase 65-05: Persists mode toggle to backend
   */
  async updateSessionMode(sessionId: string, mode: 'ask' | 'agent'): Promise<ChatSession> {
    const response = await this.client.patch<ChatSession>(
      `/api/chat/sessions/${sessionId}/mode`,
      { session_mode: mode }
    );
    return response.data;
  }

  // ===== Team Session Endpoints (Phase 38 - Group Sessions) =====

  /**
   * List team sessions visible to the current user's tenant
   */
  async listTeamSessions(): Promise<TeamSession[]> {
    const response = await this.client.get<TeamSession[]>('/api/chat/sessions/team');
    return response.data;
  }

  /**
   * Update session visibility (upgrade only: private -> group -> tenant)
   */
  async updateSessionVisibility(sessionId: string, visibility: string): Promise<ChatSession> {
    const response = await this.client.patch<ChatSession>(
      `/api/chat/sessions/${sessionId}/visibility`,
      { visibility }
    );
    return response.data;
  }

  // ===== Knowledge Endpoints =====

  /**
   * Get knowledge tree hierarchy (Global > Type > Instance)
   */
  async getKnowledgeTree(): Promise<import('../api/types/knowledge').KnowledgeTreeResponse> {
    const response = await this.client.get<import('../api/types/knowledge').KnowledgeTreeResponse>('/api/knowledge/tree');
    return response.data;
  }

  /**
   * Search knowledge base
   */
  async searchKnowledge(request: KnowledgeSearchRequest): Promise<SearchKnowledgeResponse> {
    const response = await this.client.post<SearchKnowledgeResponse>('/api/knowledge/search', request);
    return response.data;
  }

  /**
   * Upload document to knowledge base (scope-aware)
   */
  async uploadDocument(request: UploadDocumentRequest): Promise<UploadDocumentResponse> {
    const formData = new FormData();
    formData.append('file', request.file);
    formData.append('knowledge_type', request.knowledge_type);
    formData.append('tags', JSON.stringify(request.tags));
    if (request.connector_id) formData.append('connector_id', request.connector_id);
    if (request.scope_type) formData.append('scope_type', request.scope_type);
    if (request.connector_type_scope) formData.append('connector_type_scope', request.connector_type_scope);

    const response = await this.client.post<UploadDocumentResponse>(
      '/api/knowledge/upload',
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    );
    return response.data;
  }

  /**
   * Ingest content from a URL into the knowledge base
   */
  async ingestUrl(request: import('../api/types/knowledge').IngestUrlRequest): Promise<UploadDocumentResponse> {
    const response = await this.client.post<UploadDocumentResponse>('/api/knowledge/ingest-url', request);
    return response.data;
  }

  /**
   * Get upload/ingestion job status
   */
  async getJobStatus(jobId: string): Promise<IngestionJobStatus> {
    const response = await this.client.get<IngestionJobStatus>(`/api/knowledge/jobs/${jobId}`);
    return response.data;
  }

  /**
   * Get all active (processing) ingestion jobs
   */
  async getActiveJobs(tenantId?: string): Promise<IngestionJobStatus[]> {
    const params = new URLSearchParams();
    if (tenantId) params.set('tenant_id', tenantId);
    
    const response = await this.client.get<IngestionJobStatus[]>(
      `/api/knowledge/jobs/active?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Ingest text as knowledge (procedures, lessons, notices)
   */
  async ingestText(request: IngestTextRequest): Promise<IngestTextResponse> {
    const response = await this.client.post<IngestTextResponse>('/api/knowledge/ingest-text', request);
    return response.data;
  }

  /**
   * List knowledge chunks
   */
  async listKnowledgeChunks(request: ListChunksRequest = {}): Promise<ListChunksResponse> {
    const params = new URLSearchParams();
    if (request.knowledge_type) params.set('knowledge_type', request.knowledge_type);
    if (request.tags) params.set('tags', request.tags);
    if (request.limit) params.set('limit', request.limit.toString());
    if (request.offset) params.set('offset', request.offset.toString());

    const response = await this.client.get<ListChunksResponse>(
      `/api/knowledge/chunks?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Delete knowledge chunk
   */
  async deleteKnowledgeChunk(chunkId: string): Promise<void> {
    await this.client.delete(`/api/knowledge/chunks/${chunkId}`);
  }

  /**
   * List uploaded knowledge documents
   */
  async listKnowledgeDocuments(request: ListDocumentsRequest = {}): Promise<ListDocumentsResponse> {
    const params = new URLSearchParams();
    if (request.status) params.set('status', request.status);
    if (request.limit) params.set('limit', request.limit.toString());
    if (request.offset) params.set('offset', request.offset.toString());

    const query = params.toString();
    const response = await this.client.get<ListDocumentsResponse>(
      `/api/knowledge/documents${query ? `?${query}` : ''}`
    );
    return response.data;
  }

  /**
   * Delete a knowledge document (all associated chunks) with progress tracking
   */
  async deleteKnowledgeDocument(documentId: string): Promise<{ job_id: string; chunks_to_delete: number }> {
    const response = await this.client.delete<{ job_id: string; chunks_to_delete: number }>(
      `/api/knowledge/documents/${documentId}`
    );
    return response.data;
  }

  /**
   * List knowledge documents for a specific connector
   */
  async listConnectorDocuments(connectorId: string, params: { limit?: number; offset?: number } = {}): Promise<ListDocumentsResponse> {
    const searchParams = new URLSearchParams();
    if (params.limit) searchParams.set('limit', params.limit.toString());
    if (params.offset) searchParams.set('offset', params.offset.toString());
    const query = searchParams.toString();
    const response = await this.client.get<ListDocumentsResponse>(
      `/api/knowledge/connectors/${connectorId}/documents${query ? `?${query}` : ''}`
    );
    return response.data;
  }

  /**
   * Delete a knowledge document from a specific connector
   */
  async deleteConnectorDocument(connectorId: string, documentId: string): Promise<{ message: string; document_id: string; connector_id: string }> {
    const response = await this.client.delete<{ message: string; document_id: string; connector_id: string }>(
      `/api/knowledge/connectors/${connectorId}/documents/${documentId}`
    );
    return response.data;
  }

  // ===== Connector Management =====

  /**
   * List all connectors
   */
  async listConnectors(): Promise<Connector[]> {
    const response = await this.client.get<Connector[]>('/api/connectors');
    return response.data;
  }

  /**
   * Get health/reachability status for all connectors
   */
  async getConnectorsHealth(): Promise<ConnectorHealth[]> {
    const response = await this.client.get<ConnectorHealth[]>('/api/connectors/health');
    return response.data;
  }

  /**
   * Get connector by ID
   */
  async getConnector(connectorId: string): Promise<Connector> {
    const response = await this.client.get<Connector>(`/api/connectors/${connectorId}`);
    return response.data;
  }

  /**
   * Create a new connector
   */
  async createConnector(request: CreateConnectorRequest): Promise<Connector> {
    const response = await this.client.post<Connector>('/api/connectors', request);
    return response.data;
  }

  /**
   * Create a VMware vSphere connector
   */
  async createVMwareConnector(request: CreateVMwareConnectorRequest): Promise<VMwareConnectorResponse> {
    const response = await this.client.post<VMwareConnectorResponse>('/api/connectors/vmware', request);
    return response.data;
  }

  /**
   * Create a Proxmox VE connector
   */
  async createProxmoxConnector(request: CreateProxmoxConnectorRequest): Promise<ProxmoxConnectorResponse> {
    const response = await this.client.post<ProxmoxConnectorResponse>('/api/connectors/proxmox', request);
    return response.data;
  }

  /**
   * Create a Kubernetes connector
   */
  async createKubernetesConnector(request: CreateKubernetesConnectorRequest): Promise<KubernetesConnectorResponse> {
    const response = await this.client.post<KubernetesConnectorResponse>('/api/connectors/kubernetes', request);
    return response.data;
  }

  /**
   * Create a GCP connector
   */
  async createGCPConnector(request: CreateGCPConnectorRequest): Promise<GCPConnectorResponse> {
    const response = await this.client.post<GCPConnectorResponse>('/api/connectors/gcp', request);
    return response.data;
  }

  /**
   * Create an Azure connector
   */
  async createAzureConnector(request: CreateAzureConnectorRequest): Promise<AzureConnectorResponse> {
    const response = await this.client.post<AzureConnectorResponse>('/api/connectors/azure', request);
    return response.data;
  }

  /**
   * Create an AWS connector
   */
  async createAWSConnector(request: CreateAWSConnectorRequest): Promise<AWSConnectorResponse> {
    const response = await this.client.post<AWSConnectorResponse>('/api/connectors/aws', request);
    return response.data;
  }

  /**
   * Create a Prometheus connector
   */
  async createPrometheusConnector(request: CreatePrometheusConnectorRequest): Promise<PrometheusConnectorResponse> {
    const response = await this.client.post<PrometheusConnectorResponse>('/api/connectors/prometheus', request);
    return response.data;
  }

  /**
   * Create a Loki connector
   */
  async createLokiConnector(request: CreateLokiConnectorRequest): Promise<LokiConnectorResponse> {
    const response = await this.client.post<LokiConnectorResponse>('/api/connectors/loki', request);
    return response.data;
  }

  /**
   * Create a Tempo connector
   */
  async createTempoConnector(request: CreateTempoConnectorRequest): Promise<TempoConnectorResponse> {
    const response = await this.client.post<TempoConnectorResponse>('/api/connectors/tempo', request);
    return response.data;
  }

  /**
   * Create an Alertmanager connector
   */
  async createAlertmanagerConnector(request: CreateAlertmanagerConnectorRequest): Promise<AlertmanagerConnectorResponse> {
    const response = await this.client.post<AlertmanagerConnectorResponse>('/api/connectors/alertmanager', request);
    return response.data;
  }

  /**
   * Create a Jira connector
   */
  async createJiraConnector(request: CreateJiraConnectorRequest): Promise<JiraConnectorResponse> {
    const response = await this.client.post<JiraConnectorResponse>('/api/connectors/jira', request);
    return response.data;
  }

  /**
   * Create a Confluence connector
   */
  async createConfluenceConnector(request: CreateConfluenceConnectorRequest): Promise<ConfluenceConnectorResponse> {
    const response = await this.client.post<ConfluenceConnectorResponse>('/api/connectors/confluence', request);
    return response.data;
  }

  /**
   * Create an Email connector
   */
  async createEmailConnector(request: CreateEmailConnectorRequest): Promise<EmailConnectorResponse> {
    const response = await this.client.post<EmailConnectorResponse>('/api/connectors/email', request);
    return response.data;
  }

  /**
   * Create an ArgoCD connector
   */
  async createArgoConnector(request: CreateArgoConnectorRequest): Promise<ArgoConnectorResponse> {
    const response = await this.client.post<ArgoConnectorResponse>('/api/connectors/argocd', request);
    return response.data;
  }

  /**
   * Create a GitHub connector
   */
  async createGitHubConnector(request: CreateGitHubConnectorRequest): Promise<GitHubConnectorResponse> {
    const response = await this.client.post<GitHubConnectorResponse>('/api/connectors/github', request);
    return response.data;
  }

  /**
   * Create an MCP connector
   */
  async createMCPConnector(request: CreateMCPConnectorRequest): Promise<MCPConnectorResponse> {
    const response = await this.client.post<MCPConnectorResponse>('/api/connectors/mcp', request);
    return response.data;
  }

  /**
   * Create a Slack connector
   */
  async createSlackConnector(request: CreateSlackConnectorRequest): Promise<SlackConnectorResponse> {
    const response = await this.client.post<SlackConnectorResponse>('/api/connectors/slack', request);
    return response.data;
  }

  /**
   * Get email delivery history for a connector
   */
  async getEmailHistory(connectorId: string): Promise<EmailDeliveryLogEntry[]> {
    const response = await this.client.get<EmailDeliveryLogEntry[]>(`/api/connectors/${connectorId}/email-history`);
    return response.data;
  }

  /**
   * Update connector
   */
  async updateConnector(connectorId: string, request: UpdateConnectorRequest): Promise<Connector> {
    const response = await this.client.patch<Connector>(`/api/connectors/${connectorId}`, request);
    return response.data;
  }

  /**
   * Save custom skill content for a connector
   */
  async saveCustomSkill(connectorId: string, customSkill: string): Promise<Connector> {
    const response = await this.client.put<Connector>(
      `/api/connectors/${connectorId}/skill`,
      { custom_skill: customSkill }
    );
    return response.data;
  }

  /**
   * Regenerate skill from current OpenAPI spec/operations
   */
  async regenerateSkill(connectorId: string): Promise<RegenerateSkillResponse> {
    const response = await this.client.post<RegenerateSkillResponse>(
      `/api/connectors/${connectorId}/skill/regenerate`
    );
    return response.data;
  }

  /**
   * Delete connector
   */
  async deleteConnector(connectorId: string): Promise<void> {
    await this.client.delete(`/api/connectors/${connectorId}`);
  }

  /**
   * Download OpenAPI specification file
   */
  async downloadOpenAPISpec(connectorId: string): Promise<Blob> {
    const response = await this.client.get(`/api/connectors/${connectorId}/openapi-spec/download`, {
      responseType: 'blob',
    });
    return response.data;
  }

  /**
   * Upload OpenAPI spec for connector
   */
  async uploadOpenAPISpec(connectorId: string, file: File): Promise<{ message: string; endpoints_count: number }> {
    const formData = new FormData();
    formData.append('file', file);

    const response = await this.client.post(
      `/api/connectors/${connectorId}/openapi-spec`,
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    );
    return response.data;
  }

  /**
   * List endpoints for a connector
   */
  async listEndpoints(
    connectorId: string,
    filters?: {
      method?: string;
      is_enabled?: boolean;
      safety_level?: string;
      tags?: string;
      search?: string;
      limit?: number;
    }
  ): Promise<Endpoint[]> {
    const params = new URLSearchParams();
    if (filters?.method) params.set('method', filters.method);
    if (filters?.is_enabled !== undefined) params.set('is_enabled', filters.is_enabled.toString());
    if (filters?.safety_level) params.set('safety_level', filters.safety_level);
    if (filters?.tags) params.set('tags', filters.tags);
    if (filters?.search) params.set('search', filters.search);
    if (filters?.limit) params.set('limit', filters.limit.toString());

    const response = await this.client.get<Endpoint[]>(
      `/api/connectors/${connectorId}/endpoints?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Update endpoint configuration
   */
  async updateEndpoint(
    connectorId: string,
    endpointId: string,
    request: UpdateEndpointRequest
  ): Promise<Endpoint> {
    const response = await this.client.patch<Endpoint>(
      `/api/connectors/${connectorId}/endpoints/${endpointId}`,
      request
    );
    return response.data;
  }

  /**
   * Test an endpoint with live API call
   */
  async testEndpoint(
    connectorId: string,
    endpointId: string,
    request: TestEndpointRequest
  ): Promise<TestEndpointResponse> {
    const response = await this.client.post<TestEndpointResponse>(
      `/api/connectors/${connectorId}/endpoints/${endpointId}/test`,
      request
    );
    return response.data;
  }

  /**
   * Set user credentials for a connector
   */
  async setUserCredentials(connectorId: string, credentials: Record<string, string>): Promise<void> {
    await this.client.post(`/api/connectors/${connectorId}/credentials`, credentials);
  }

  /**
   * Get credential status for a connector
   */
  async getCredentialStatus(connectorId: string): Promise<CredentialStatus> {
    const response = await this.client.get<CredentialStatus>(
      `/api/connectors/${connectorId}/credentials/status`
    );
    return response.data;
  }

  /**
   * Delete user credentials for a connector
   */
  async deleteUserCredentials(connectorId: string): Promise<void> {
    await this.client.delete(`/api/connectors/${connectorId}/credentials`);
  }

  /**
   * Test connection to a connector
   */
  async testConnection(
    connectorId: string,
    request: TestConnectionRequest
  ): Promise<TestConnectionResponse> {
    const response = await this.client.post<TestConnectionResponse>(
      `/api/connectors/${connectorId}/test-connection`,
      request
    );
    return response.data;
  }

  /**
   * Test authentication for a connector (especially for SESSION auth)
   */
  async testAuth(
    connectorId: string,
    request: TestAuthRequest
  ): Promise<TestAuthResponse> {
    const response = await this.client.post<TestAuthResponse>(
      `/api/connectors/${connectorId}/test-auth`,
      request
    );
    return response.data;
  }

  // ===== Connector Export/Import (TASK-142) =====

  /**
   * Export connectors to encrypted file
   * Returns a Blob for file download
   */
  async exportConnectors(request: ExportConnectorsRequest): Promise<Blob> {
    const response = await this.client.post('/api/connectors/export', request, {
      responseType: 'blob',
    });
    return response.data;
  }

  /**
   * Import connectors from encrypted file
   */
  async importConnectors(request: ImportConnectorsRequest): Promise<ImportConnectorsResponse> {
    const response = await this.client.post<ImportConnectorsResponse>(
      '/api/connectors/import',
      request
    );
    return response.data;
  }

  // ===== Connector Memories (Phase 13) =====

  /**
   * List memories for a connector
   */
  async listConnectorMemories(
    connectorId: string,
    params: {
      memory_type?: string;
      confidence_level?: string;
      limit?: number;
      offset?: number;
    } = {}
  ): Promise<MemoryResponse[]> {
    const searchParams = new URLSearchParams();
    if (params.memory_type) searchParams.set('memory_type', params.memory_type);
    if (params.confidence_level) searchParams.set('confidence_level', params.confidence_level);
    if (params.limit) searchParams.set('limit', params.limit.toString());
    if (params.offset) searchParams.set('offset', params.offset.toString());
    const query = searchParams.toString();
    const response = await this.client.get<MemoryResponse[]>(
      `/api/connectors/${connectorId}/memories${query ? `?${query}` : ''}`
    );
    return response.data;
  }

  /**
   * Update a memory (PATCH)
   */
  async updateConnectorMemory(
    connectorId: string,
    memoryId: string,
    updates: MemoryUpdate
  ): Promise<MemoryResponse> {
    const response = await this.client.patch<MemoryResponse>(
      `/api/connectors/${connectorId}/memories/${memoryId}`,
      updates
    );
    return response.data;
  }

  /**
   * Delete a memory
   */
  async deleteConnectorMemory(
    connectorId: string,
    memoryId: string
  ): Promise<{ deleted: boolean }> {
    const response = await this.client.delete<{ deleted: boolean }>(
      `/api/connectors/${connectorId}/memories/${memoryId}`
    );
    return response.data;
  }

  // ===== Events (Phase 94 - Events System + Response Channels) =====

  /**
   * List event registrations for a connector
   */
  async listConnectorEvents(connectorId: string): Promise<EventRegistration[]> {
    const response = await this.client.get<EventRegistration[]>(
      `/api/connectors/${connectorId}/events`
    );
    return response.data;
  }

  /**
   * Create an event registration (returns secret -- display-once)
   */
  async createConnectorEvent(
    connectorId: string,
    data: {
      name: string;
      prompt_template?: string;
      rate_limit_per_hour?: number;
      require_signature?: boolean;
      allowed_connector_ids?: string[] | null;
      notification_targets?: Array<{ connector_id: string; contact: string }> | null;
      response_config?: { connector_id: string; operation_id: string; parameter_mapping: Record<string, string> } | null;
    }
  ): Promise<EventCreateResponse> {
    const response = await this.client.post<EventCreateResponse>(
      `/api/connectors/${connectorId}/events`,
      data
    );
    return response.data;
  }

  /**
   * Get a single event registration
   */
  async getConnectorEvent(
    connectorId: string,
    eventId: string
  ): Promise<EventRegistration> {
    const response = await this.client.get<EventRegistration>(
      `/api/connectors/${connectorId}/events/${eventId}`
    );
    return response.data;
  }

  /**
   * Update an event registration (PATCH)
   */
  async updateConnectorEvent(
    connectorId: string,
    eventId: string,
    data: {
      name?: string;
      prompt_template?: string;
      rate_limit_per_hour?: number;
      is_active?: boolean;
      require_signature?: boolean;
      allowed_connector_ids?: string[] | null;
      notification_targets?: Array<{ connector_id: string; contact: string }> | null;
      response_config?: { connector_id: string; operation_id: string; parameter_mapping: Record<string, string> } | null;
    }
  ): Promise<EventRegistration> {
    const response = await this.client.patch<EventRegistration>(
      `/api/connectors/${connectorId}/events/${eventId}`,
      data
    );
    return response.data;
  }

  /**
   * Delete an event registration
   */
  async deleteConnectorEvent(
    connectorId: string,
    eventId: string
  ): Promise<{ deleted: boolean }> {
    const response = await this.client.delete<{ deleted: boolean }>(
      `/api/connectors/${connectorId}/events/${eventId}`
    );
    return response.data;
  }

  /**
   * Get paginated event history for an event registration
   */
  async getEventHistory(
    connectorId: string,
    eventId: string,
    params?: { limit?: number; offset?: number }
  ): Promise<EventHistoryResponse> {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.set('limit', params.limit.toString());
    if (params?.offset) searchParams.set('offset', params.offset.toString());
    const query = searchParams.toString();
    const response = await this.client.get<EventHistoryResponse>(
      `/api/connectors/${connectorId}/events/${eventId}/history${query ? `?${query}` : ''}`
    );
    return response.data;
  }

  /**
   * Test an event pipeline end-to-end
   */
  async testConnectorEvent(
    connectorId: string,
    eventId: string,
    payload: object
  ): Promise<EventTestResponse> {
    const response = await this.client.post<EventTestResponse>(
      `/api/connectors/${connectorId}/events/${eventId}/test`,
      { payload }
    );
    return response.data;
  }

  // ===== Scheduled Tasks (Phase 45 - Scheduled Tasks) =====

  /**
   * List all scheduled tasks for the current tenant
   */
  async getScheduledTasks(): Promise<ScheduledTask[]> {
    const response = await this.client.get<ScheduledTask[]>(
      '/api/scheduled-tasks'
    );
    return response.data;
  }

  /**
   * Create a new scheduled task
   */
  async createScheduledTask(
    data: CreateScheduledTaskRequest
  ): Promise<ScheduledTask> {
    const response = await this.client.post<ScheduledTask>(
      '/api/scheduled-tasks',
      data
    );
    return response.data;
  }

  /**
   * Get a specific scheduled task by ID
   */
  async getScheduledTask(taskId: string): Promise<ScheduledTask> {
    const response = await this.client.get<ScheduledTask>(
      `/api/scheduled-tasks/${taskId}`
    );
    return response.data;
  }

  /**
   * Update a scheduled task
   */
  async updateScheduledTask(
    taskId: string,
    data: UpdateScheduledTaskRequest
  ): Promise<ScheduledTask> {
    const response = await this.client.put<ScheduledTask>(
      `/api/scheduled-tasks/${taskId}`,
      data
    );
    return response.data;
  }

  /**
   * Delete a scheduled task
   */
  async deleteScheduledTask(taskId: string): Promise<void> {
    await this.client.delete(`/api/scheduled-tasks/${taskId}`);
  }

  /**
   * Toggle a scheduled task enabled/disabled
   */
  async toggleScheduledTask(taskId: string): Promise<ScheduledTask> {
    const response = await this.client.patch<ScheduledTask>(
      `/api/scheduled-tasks/${taskId}/toggle`
    );
    return response.data;
  }

  /**
   * Run a scheduled task immediately
   */
  async runScheduledTaskNow(
    taskId: string
  ): Promise<{ message: string; session_id: string }> {
    const response = await this.client.post<{
      message: string;
      session_id: string;
    }>(`/api/scheduled-tasks/${taskId}/run`);
    return response.data;
  }

  /**
   * Get run history for a scheduled task
   */
  async getScheduledTaskRuns(
    taskId: string,
    limit?: number,
    offset?: number
  ): Promise<ScheduledTaskRun[]> {
    const params = new URLSearchParams();
    if (limit !== undefined) params.set('limit', limit.toString());
    if (offset !== undefined) params.set('offset', offset.toString());
    const query = params.toString();
    const response = await this.client.get<ScheduledTaskRun[]>(
      `/api/scheduled-tasks/${taskId}/runs${query ? `?${query}` : ''}`
    );
    return response.data;
  }

  /**
   * Parse natural language schedule to cron expression
   */
  async parseSchedule(
    text: string,
    timezone: string
  ): Promise<ParseScheduleResponse> {
    const response = await this.client.post<ParseScheduleResponse>(
      '/api/scheduled-tasks/parse-schedule',
      { text, timezone }
    );
    return response.data;
  }

  /**
   * Generate a connector-type-aware event prompt via LLM
   */
  async generateEventPrompt(connectorId: string, userInstructions?: string): Promise<{ prompt: string }> {
    const response = await this.client.post<{ prompt: string }>(
      `/api/connectors/${connectorId}/events/generate-prompt`,
      userInstructions ? { user_instructions: userInstructions } : {}
    );
    return response.data;
  }

  /**
   * Generate a generic investigation prompt for scheduled tasks via LLM
   */
  async generateScheduledTaskPrompt(): Promise<{ prompt: string }> {
    const response = await this.client.post<{ prompt: string }>(
      '/api/scheduled-tasks/generate-prompt',
      {}
    );
    return response.data;
  }

  /**
   * Validate a cron expression and get next runs preview
   */
  async validateCron(
    cronExpression: string,
    timezone: string
  ): Promise<ValidateCronResponse> {
    const response = await this.client.post<ValidateCronResponse>(
      '/api/scheduled-tasks/validate-cron',
      { cron_expression: cronExpression, timezone }
    );
    return response.data;
  }

  /**
   * Get list of available IANA timezone names
   */
  async getTimezones(): Promise<string[]> {
    const response = await this.client.get<string[]>(
      '/api/scheduled-tasks/timezones'
    );
    return response.data;
  }

  // ===== Orchestrator Skills (Phase 52 - Orchestrator Skills Frontend) =====

  /**
   * List all orchestrator skills for the current tenant
   */
  async listOrchestratorSkills(): Promise<OrchestratorSkillSummary[]> {
    const response = await this.client.get<OrchestratorSkillSummary[]>(
      '/api/orchestrator-skills/'
    );
    return response.data;
  }

  /**
   * Get a single orchestrator skill by ID (full content)
   */
  async getOrchestratorSkill(id: string): Promise<OrchestratorSkill> {
    const response = await this.client.get<OrchestratorSkill>(
      `/api/orchestrator-skills/${id}`
    );
    return response.data;
  }

  /**
   * Create a new orchestrator skill
   */
  async createOrchestratorSkill(data: CreateSkillRequest): Promise<OrchestratorSkill> {
    const response = await this.client.post<OrchestratorSkill>(
      '/api/orchestrator-skills/',
      data
    );
    return response.data;
  }

  /**
   * Update an existing orchestrator skill
   */
  async updateOrchestratorSkill(id: string, data: UpdateSkillRequest): Promise<OrchestratorSkill> {
    const response = await this.client.put<OrchestratorSkill>(
      `/api/orchestrator-skills/${id}`,
      data
    );
    return response.data;
  }

  /**
   * Delete an orchestrator skill
   */
  async deleteOrchestratorSkill(id: string): Promise<void> {
    await this.client.delete(`/api/orchestrator-skills/${id}`);
  }

  /**
   * Generate orchestrator skill content via LLM
   */
  async generateOrchestratorSkill(data: GenerateSkillRequest): Promise<GenerateSkillResponse> {
    const response = await this.client.post<GenerateSkillResponse>(
      '/api/orchestrator-skills/generate',
      data
    );
    return response.data;
  }

  // ===== Recipes =====

  /**
   * List saved recipes
   */
  async listRecipes(filters?: {
    tag?: string;
    search?: string;
    connector_id?: string;
  }): Promise<Recipe[]> {
    const params = new URLSearchParams();
    if (filters?.tag) params.set('tag', filters.tag);
    if (filters?.search) params.set('search', filters.search);
    if (filters?.connector_id) params.set('connector_id', filters.connector_id);

    const response = await this.client.get<{ recipes: Recipe[]; total: number }>(
      `/api/recipes?${params.toString()}`
    );
    return response.data.recipes;
  }

  /**
   * Get recipe by ID
   */
  async getRecipe(recipeId: string): Promise<Recipe> {
    const response = await this.client.get<Recipe>(
      `/api/recipes/${recipeId}`
    );
    return response.data;
  }

  /**
   * Create a new recipe
   */
  async createRecipe(request: {
    name: string;
    description?: string;
    tags?: string[];
    connector_id?: string;
    query_template: string;
    parameters?: RecipeParameter[];
  }): Promise<Recipe> {
    const response = await this.client.post<Recipe>(
      '/api/recipes',
      request
    );
    return response.data;
  }

  /**
   * Delete a recipe
   */
  async deleteRecipe(recipeId: string): Promise<void> {
    await this.client.delete(`/api/recipes/${recipeId}`);
  }

  /**
   * Create a recipe from a chat session (Phase 63).
   * Analyzes the conversation with an LLM and creates a recipe draft.
   */
  async createRecipeFromSession(sessionId: string): Promise<Recipe> {
    const response = await this.client.post<Recipe>(
      `/api/recipes/create-from-session/${sessionId}`
    );
    return response.data;
  }

  /**
   * Update a recipe (Phase 63).
   * Supports partial updates to name, description, tags, and parameters.
   */
  async updateRecipe(
    recipeId: string,
    request: {
      name?: string;
      description?: string;
      tags?: string[];
      parameters?: RecipeParameter[];
    }
  ): Promise<Recipe> {
    const response = await this.client.patch<Recipe>(
      `/api/recipes/${recipeId}`,
      request
    );
    return response.data;
  }

  /**
   * Execute a recipe
   */
  async executeRecipe(
    recipeId: string,
    parameters: Record<string, unknown>
  ): Promise<RecipeExecution> {
    const response = await this.client.post<RecipeExecution>(
      `/api/recipes/${recipeId}/execute`,
      { parameters }
    );
    return response.data;
  }

  // ===== SOAP Support =====

  /**
   * Ingest WSDL for a SOAP connector
   */
  async ingestWSDL(connectorId: string, wsdlUrl?: string): Promise<{
    message: string;
    operations_count: number;
    types_count: number;
    services: string[];
    ports: string[];
  }> {
    const response = await this.client.post(
      `/api/connectors/${connectorId}/wsdl`,
      wsdlUrl ? { wsdl_url: wsdlUrl } : {}
    );
    return response.data;
  }

  /**
   * List SOAP operations for a connector
   */
  async listSOAPOperations(
    connectorId: string,
    filters?: { service?: string; search?: string; limit?: number }
  ): Promise<Array<{
    name: string;
    service_name: string;
    port_name: string;
    operation_name: string;
    description?: string;
    soap_action?: string;
    input_schema: Record<string, unknown>;
    output_schema: Record<string, unknown>;
  }>> {
    const params = new URLSearchParams();
    if (filters?.service) params.set('service', filters.service);
    if (filters?.search) params.set('search', filters.search);
    if (filters?.limit) params.set('limit', filters.limit.toString());

    const response = await this.client.get(
      `/api/connectors/${connectorId}/soap-operations?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Call a SOAP operation
   */
  async callSOAPOperation(
    connectorId: string,
    operationName: string,
    params: Record<string, unknown>
  ): Promise<{
    success: boolean;
    status_code: number;
    body: unknown;
    duration_ms?: number;
  }> {
    const response = await this.client.post(
      `/api/connectors/${connectorId}/soap-operations/${operationName}/call`,
      { params }
    );
    return response.data;
  }

  // ===== Connector Operations/Types =====

  /**
   * List operations for a typed connector (VMware, etc.)
   */
  async listConnectorOperations(
    connectorId: string,
    filters?: { search?: string; category?: string; limit?: number }
  ): Promise<Array<ConnectorOperation>> {
    const params = new URLSearchParams();
    if (filters?.search) params.set('search', filters.search);
    if (filters?.category) params.set('category', filters.category);
    if (filters?.limit) params.set('limit', filters.limit.toString());

    const response = await this.client.get(
      `/api/connectors/${connectorId}/operations?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Toggle enable/disable for an operation on a connector instance.
   * For type-level ops, creates a disable override. For custom ops, toggles is_enabled.
   */
  async toggleConnectorOperation(
    connectorId: string,
    operationId: string
  ): Promise<ConnectorOperation> {
    const response = await this.client.patch(
      `/api/connectors/${connectorId}/operations/${operationId}/toggle`
    );
    return response.data;
  }

  /**
   * Create or update an instance override of a type-level operation.
   */
  async overrideConnectorOperation(
    connectorId: string,
    operationId: string,
    overrides: { description?: string; safety_level?: string; parameters?: Array<{ name: string; type: string; required?: boolean; description?: string }> }
  ): Promise<ConnectorOperation> {
    const response = await this.client.put(
      `/api/connectors/${connectorId}/operations/${operationId}/override`,
      overrides
    );
    return response.data;
  }

  /**
   * Reset an instance override back to the type-level definition.
   */
  async resetConnectorOperationOverride(
    connectorId: string,
    operationId: string
  ): Promise<void> {
    await this.client.delete(
      `/api/connectors/${connectorId}/operations/${operationId}/override`
    );
  }

  /**
   * List types for a typed connector (VMware, REST schema types, etc.)
   */
  async listConnectorTypes(
    connectorId: string,
    filters?: { search?: string; category?: string; limit?: number }
  ): Promise<Array<ConnectorEntityType>> {
    const params = new URLSearchParams();
    if (filters?.search) params.set('search', filters.search);
    if (filters?.category) params.set('category', filters.category);
    if (filters?.limit) params.set('limit', filters.limit.toString());

    const response = await this.client.get(
      `/api/connectors/${connectorId}/types?${params.toString()}`
    );
    return response.data;
  }

  // ===== SOAP Type Definitions =====

  /**
   * List SOAP type definitions for a connector
   */
  async listSOAPTypes(
    connectorId: string,
    filters?: { search?: string; base_type?: string; limit?: number }
  ): Promise<Array<SOAPTypeDefinition>> {
    const params = new URLSearchParams();
    if (filters?.search) params.set('search', filters.search);
    if (filters?.base_type) params.set('base_type', filters.base_type);
    if (filters?.limit) params.set('limit', filters.limit.toString());

    const response = await this.client.get(
      `/api/connectors/${connectorId}/soap-types?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Get a specific SOAP type definition by name
   */
  async getSOAPType(
    connectorId: string,
    typeName: string
  ): Promise<SOAPTypeDefinition> {
    const response = await this.client.get(
      `/api/connectors/${connectorId}/soap-types/${encodeURIComponent(typeName)}`
    );
    return response.data;
  }

  // ===== Approval Flow =====

  /**
   * Approve a pending dangerous action
   */
  async approveAction(
    sessionId: string,
    approvalId: string,
    reason?: string
  ): Promise<{ status: string; message: string; approval_id?: string }> {
    const response = await this.client.post(
      `/api/chat/${sessionId}/approve/${approvalId}`,
      { approved: true, reason }
    );
    return response.data;
  }

  /**
   * Reject a pending dangerous action
   */
  async rejectAction(
    sessionId: string,
    approvalId: string,
    reason?: string
  ): Promise<{ status: string; message: string; approval_id?: string }> {
    const response = await this.client.post(
      `/api/chat/${sessionId}/approve/${approvalId}`,
      { approved: false, reason }
    );
    return response.data;
  }

  /**
   * Get pending approval requests for a session
   */
  async getPendingApprovals(sessionId: string): Promise<Array<{
    approval_id: string;
    tool_name: string;
    danger_level: string;
    method?: string;
    path?: string;
    description?: string;
    tool_args?: Record<string, unknown>;
    created_at: string;
  }>> {
    const response = await this.client.get(`/api/chat/${sessionId}/pending-approvals`);
    return response.data;
  }

  /**
   * Phase 63-02: Summarize a session and create a new one with the summary.
   *
   * Used by ContextBar "Start new chat" to hand off investigation context.
   */
  async summarizeSession(sessionId: string): Promise<{ new_session_id: string; summary: string }> {
    const response = await this.client.post(`/api/chat/sessions/${sessionId}/summarize`);
    return response.data;
  }

  // ===== Tenant Management (Global Admin Only) =====

  /**
   * List all tenants
   */
  async listTenants(includeInactive: boolean = false): Promise<TenantListResponse> {
    const params = new URLSearchParams();
    if (includeInactive) params.set('include_inactive', 'true');
    
    const response = await this.client.get<TenantListResponse>(
      `/api/tenants?${params.toString()}`
    );
    return response.data;
  }

  /**
   * Get a specific tenant
   */
  async getTenant(tenantId: string): Promise<Tenant> {
    const response = await this.client.get<Tenant>(`/api/tenants/${tenantId}`);
    return response.data;
  }

  /**
   * Create a new tenant
   */
  async createTenant(request: CreateTenantRequest): Promise<Tenant> {
    const response = await this.client.post<Tenant>('/api/tenants', request);
    return response.data;
  }

  /**
   * Update tenant settings
   */
  async updateTenant(tenantId: string, request: UpdateTenantRequest): Promise<Tenant> {
    const response = await this.client.patch<Tenant>(`/api/tenants/${tenantId}`, request);
    return response.data;
  }

  /**
   * Disable a tenant (soft delete)
   */
  async disableTenant(tenantId: string): Promise<Tenant> {
    const response = await this.client.post<Tenant>(`/api/tenants/${tenantId}/disable`);
    return response.data;
  }

  /**
   * Enable a disabled tenant
   */
  async enableTenant(tenantId: string): Promise<Tenant> {
    const response = await this.client.post<Tenant>(`/api/tenants/${tenantId}/enable`);
    return response.data;
  }

  // ===== Admin Dashboard (Global Admin Only) =====

  /**
   * Get dashboard statistics for superadmin
   */
  async getDashboardStats(): Promise<DashboardStats> {
    const response = await this.client.get<DashboardStats>('/api/admin/dashboard/stats');
    return response.data;
  }

  /**
   * Get activity feed for superadmin dashboard
   */
  async getDashboardActivity(limit: number = 20): Promise<ActivityItem[]> {
    const response = await this.client.get<ActivityItem[]>('/api/admin/dashboard/activity', {
      params: { limit },
    });
    return response.data;
  }

  // ===== Admin Config (Tenant Settings Page) =====

  /**
   * Get tenant agent configuration
   */
  async getAdminConfig<T = Record<string, unknown>>(): Promise<T> {
    const response = await this.client.get<T>('/api/admin/config');
    return response.data;
  }

  /**
   * Update tenant agent configuration
   */
  async updateAdminConfig<T = Record<string, unknown>>(data: {
    installation_context?: string;
    model_override?: string;
    temperature_override?: number;
  }): Promise<T> {
    const response = await this.client.put<T>('/api/admin/config', data);
    return response.data;
  }

  /**
   * Get allowed LLM models for tenant
   */
  async getAdminModels<T = Record<string, unknown>>(): Promise<T> {
    const response = await this.client.get<T>('/api/admin/models');
    return response.data;
  }

  /**
   * Get system prompt preview
   */
  async getPromptPreview<T = Record<string, unknown>>(): Promise<T> {
    const response = await this.client.get<T>('/api/admin/prompt/preview');
    return response.data;
  }

  /**
   * Get configuration audit log
   */
  async getConfigAudit<T = Record<string, unknown>>(): Promise<T> {
    const response = await this.client.get<T>('/api/admin/config/audit');
    return response.data;
  }

  // ===== Health Check =====

  /**
   * Check API health
   */
  async healthCheck(): Promise<{status: string; service: string; version: string}> {
    const response = await this.client.get('/health');
    return response.data;
  }

  // ===== Observability Endpoints (TASK-186) =====

  /**
   * Observability API namespace for deep introspection of agent execution.
   * Provides access to session transcripts, event details, and LLM/HTTP/SQL calls.
   */
  observability = {
    /**
     * List sessions with pagination and filtering.
     * Matches backend list_sessions endpoint in routes_observability.py
     */
    listSessions: async (params?: {
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
    }> => {
      const urlParams = new URLSearchParams();
      if (params?.limit) urlParams.set('limit', params.limit.toString());
      if (params?.offset) urlParams.set('offset', params.offset.toString());
      if (params?.status && params.status !== 'all') urlParams.set('status', params.status);

      const response = await this.client.get(`/api/observability/sessions?${urlParams.toString()}`);
      return response.data;
    },

    /**
     * Get all transcripts for a session (multi-turn conversation support).
     * Returns multiple transcripts, one for each user message/execution.
     */
    getTranscript: async (
      sessionId: string,
      params?: {
        event_types?: string[];
        include_details?: boolean;
        limit?: number;
        offset?: number;
      }
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
    }> => {
      const urlParams = new URLSearchParams();
      if (params?.event_types?.length) urlParams.set('event_types', params.event_types.join(','));
      if (params?.include_details !== undefined) urlParams.set('include_details', String(params.include_details));
      if (params?.limit) urlParams.set('limit', params.limit.toString());
      if (params?.offset) urlParams.set('offset', params.offset.toString());

      const response = await this.client.get(`/api/observability/sessions/${sessionId}/transcript?${urlParams.toString()}`);
      return response.data;
    },

    /**
     * Get summary statistics for a session.
     */
    getSummary: async (sessionId: string): Promise<{
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
    }> => {
      const response = await this.client.get(`/api/observability/sessions/${sessionId}/summary`);
      return response.data;
    },

    /**
     * Get detailed information for a specific event.
     */
    getEventDetails: async (
      sessionId: string,
      eventId: string
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
    }> => {
      const response = await this.client.get(`/api/observability/sessions/${sessionId}/events/${eventId}`);
      return response.data;
    },

    /**
     * Get LLM calls for a session.
     */
    getLLMCalls: async (
      sessionId: string,
      params?: {
        include_messages?: boolean;
        include_response?: boolean;
      }
    ): Promise<Array<{
      id: string;
      timestamp: string;
      type: string;
      summary: string;
      details: Record<string, unknown>;
      duration_ms?: number | null;
    }>> => {
      const urlParams = new URLSearchParams();
      if (params?.include_messages !== undefined) urlParams.set('include_messages', String(params.include_messages));
      if (params?.include_response !== undefined) urlParams.set('include_response', String(params.include_response));

      const response = await this.client.get(`/api/observability/sessions/${sessionId}/llm-calls?${urlParams.toString()}`);
      return response.data;
    },

    /**
     * Get operation calls for a session.
     */
    getOperationCalls: async (
      sessionId: string,
      params?: {
        include_headers?: boolean;
        include_body?: boolean;
        status_filter?: 'all' | 'success' | 'error';
      }
    ): Promise<Array<{
      id: string;
      timestamp: string;
      type: string;
      summary: string;
      details: Record<string, unknown>;
      duration_ms?: number | null;
    }>> => {
      const urlParams = new URLSearchParams();
      if (params?.include_headers !== undefined) urlParams.set('include_headers', String(params.include_headers));
      if (params?.include_body !== undefined) urlParams.set('include_body', String(params.include_body));
      if (params?.status_filter && params.status_filter !== 'all') urlParams.set('status_filter', params.status_filter);

      const response = await this.client.get(`/api/observability/sessions/${sessionId}/operation-calls?${urlParams.toString()}`);
      return response.data;
    },

    /**
     * Get SQL queries for a session.
     */
    getSQLQueries: async (
      sessionId: string,
      params?: {
        include_results?: boolean;
        limit?: number;
      }
    ): Promise<Array<{
      id: string;
      timestamp: string;
      type: string;
      summary: string;
      details: Record<string, unknown>;
      duration_ms?: number | null;
    }>> => {
      const urlParams = new URLSearchParams();
      if (params?.include_results !== undefined) urlParams.set('include_results', String(params.include_results));
      if (params?.limit) urlParams.set('limit', params.limit.toString());

      const response = await this.client.get(`/api/observability/sessions/${sessionId}/sql-queries?${urlParams.toString()}`);
      return response.data;
    },

    /**
     * Search across sessions and events.
     */
    search: async (params: {
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
    }> => {
      const urlParams = new URLSearchParams();
      urlParams.set('query', params.query);
      if (params.session_id) urlParams.set('session_id', params.session_id);
      if (params.event_types?.length) urlParams.set('event_types', params.event_types.join(','));
      if (params.from_date) urlParams.set('from_date', params.from_date);
      if (params.to_date) urlParams.set('to_date', params.to_date);
      if (params.limit) urlParams.set('limit', params.limit.toString());

      const response = await this.client.get(`/api/observability/search?${urlParams.toString()}`);
      return response.data;
    },
  };
}

// Singleton instance
let apiClient: MEHOAPIClient | null = null;

/**
 * Get or create API client singleton
 */
export function getAPIClient(baseURL?: string): MEHOAPIClient {
  if (!apiClient) {
    apiClient = new MEHOAPIClient(baseURL);
  }
  return apiClient;
}

/**
 * Reset API client (useful for testing)
 */
export function resetAPIClient() {
  apiClient = null;
}
