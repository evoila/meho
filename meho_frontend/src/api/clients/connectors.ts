// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connectors domain client: CRUD, typed creators, endpoints, credentials,
 * memory, events, SOAP, operations, types, export/import.
 *
 * Migrated from `lib/api-client.ts` in Phase 3 (#349). Signatures, URLs,
 * and payloads match the originals byte-for-byte; `MEHOAPIClient` keeps
 * its facade methods alongside these until Phase 4 (#350) rewires the
 * versioning tests.
 *
 * Note on subject grouping: `listConnectorDocuments` / `deleteConnectorDocument`
 * hit `/api/knowledge/connectors/*` but live on the knowledge client (#283)
 * because they operate on documents. The event-prompt generator lives here
 * with the rest of the events surface.
 */
import type { AxiosInstance } from 'axios';

import type {
  AlertmanagerConnectorResponse,
  ArgoConnectorResponse,
  AWSConnectorResponse,
  AzureConnectorResponse,
  ConfluenceConnectorResponse,
  Connector,
  ConnectorEntityType,
  ConnectorHealth,
  ConnectorOperation,
  CreateAlertmanagerConnectorRequest,
  CreateArgoConnectorRequest,
  CreateAWSConnectorRequest,
  CreateAzureConnectorRequest,
  CreateConfluenceConnectorRequest,
  CreateConnectorRequest,
  CreateEmailConnectorRequest,
  CreateGCPConnectorRequest,
  CreateGitHubConnectorRequest,
  CreateJiraConnectorRequest,
  CreateKubernetesConnectorRequest,
  CreateLokiConnectorRequest,
  CreateMCPConnectorRequest,
  CreatePrometheusConnectorRequest,
  CreateProxmoxConnectorRequest,
  CreateSlackConnectorRequest,
  CreateTempoConnectorRequest,
  CreateVMwareConnectorRequest,
  CredentialStatus,
  EmailConnectorResponse,
  EmailDeliveryLogEntry,
  Endpoint,
  EventCreateResponse,
  EventHistoryResponse,
  EventRegistration,
  EventTestResponse,
  ExportConnectorsRequest,
  GCPConnectorResponse,
  GitHubConnectorResponse,
  ImportConnectorsRequest,
  ImportConnectorsResponse,
  JiraConnectorResponse,
  KubernetesConnectorResponse,
  LokiConnectorResponse,
  MCPConnectorResponse,
  MemoryResponse,
  MemoryUpdate,
  PrometheusConnectorResponse,
  ProxmoxConnectorResponse,
  RegenerateSkillResponse,
  SlackConnectorResponse,
  SOAPTypeDefinition,
  TempoConnectorResponse,
  TestAuthRequest,
  TestAuthResponse,
  TestConnectionRequest,
  TestConnectionResponse,
  TestEndpointRequest,
  TestEndpointResponse,
  UpdateConnectorRequest,
  UpdateEndpointRequest,
  VMwareConnectorResponse,
} from '../types';
import { getTransport } from './transport';

/**
 * Admin-managed service credential for automated sessions (Phase 74).
 *
 * Returned by `GET /api/connectors/:id/service-credential`. Nulls mean no
 * service credential is configured (automations fall back to the event
 * creator's delegated credential).
 */
export interface ServiceCredentialStatus {
  has_service_credential: boolean;
  credential_type: string | null;
  updated_at: string | null;
}

/**
 * Payload for `PUT /api/connectors/:id/service-credential`.
 *
 * `credential_type` mirrors the backend enum (`PASSWORD`, `API_KEY`,
 * `OAUTH2_TOKEN`); `credentials` carries whichever fields the backend
 * accepts for that type (`username`/`password`, `api_key`, etc.).
 */
export interface SetServiceCredentialRequest {
  credential_type: string;
  credentials: Record<string, string>;
}

export function createConnectorsClient(transport: AxiosInstance) {
  return {
    // ===== Connector CRUD =====

    async listConnectors(): Promise<Connector[]> {
      const response = await transport.get<Connector[]>('/api/connectors');
      return response.data;
    },

    /** Health / reachability for all connectors in the tenant. */
    async getConnectorsHealth(): Promise<ConnectorHealth[]> {
      const response = await transport.get<ConnectorHealth[]>('/api/connectors/health');
      return response.data;
    },

    async getConnector(connectorId: string): Promise<Connector> {
      const response = await transport.get<Connector>(`/api/connectors/${connectorId}`);
      return response.data;
    },

    async createConnector(request: CreateConnectorRequest): Promise<Connector> {
      const response = await transport.post<Connector>('/api/connectors', request);
      return response.data;
    },

    // ===== Typed creators (one per connector family) =====

    async createVMwareConnector(
      request: CreateVMwareConnectorRequest,
    ): Promise<VMwareConnectorResponse> {
      const response = await transport.post<VMwareConnectorResponse>(
        '/api/connectors/vmware',
        request,
      );
      return response.data;
    },

    async createProxmoxConnector(
      request: CreateProxmoxConnectorRequest,
    ): Promise<ProxmoxConnectorResponse> {
      const response = await transport.post<ProxmoxConnectorResponse>(
        '/api/connectors/proxmox',
        request,
      );
      return response.data;
    },

    async createKubernetesConnector(
      request: CreateKubernetesConnectorRequest,
    ): Promise<KubernetesConnectorResponse> {
      const response = await transport.post<KubernetesConnectorResponse>(
        '/api/connectors/kubernetes',
        request,
      );
      return response.data;
    },

    async createGCPConnector(
      request: CreateGCPConnectorRequest,
    ): Promise<GCPConnectorResponse> {
      const response = await transport.post<GCPConnectorResponse>(
        '/api/connectors/gcp',
        request,
      );
      return response.data;
    },

    async createAzureConnector(
      request: CreateAzureConnectorRequest,
    ): Promise<AzureConnectorResponse> {
      const response = await transport.post<AzureConnectorResponse>(
        '/api/connectors/azure',
        request,
      );
      return response.data;
    },

    async createAWSConnector(
      request: CreateAWSConnectorRequest,
    ): Promise<AWSConnectorResponse> {
      const response = await transport.post<AWSConnectorResponse>(
        '/api/connectors/aws',
        request,
      );
      return response.data;
    },

    async createPrometheusConnector(
      request: CreatePrometheusConnectorRequest,
    ): Promise<PrometheusConnectorResponse> {
      const response = await transport.post<PrometheusConnectorResponse>(
        '/api/connectors/prometheus',
        request,
      );
      return response.data;
    },

    async createLokiConnector(
      request: CreateLokiConnectorRequest,
    ): Promise<LokiConnectorResponse> {
      const response = await transport.post<LokiConnectorResponse>(
        '/api/connectors/loki',
        request,
      );
      return response.data;
    },

    async createTempoConnector(
      request: CreateTempoConnectorRequest,
    ): Promise<TempoConnectorResponse> {
      const response = await transport.post<TempoConnectorResponse>(
        '/api/connectors/tempo',
        request,
      );
      return response.data;
    },

    async createAlertmanagerConnector(
      request: CreateAlertmanagerConnectorRequest,
    ): Promise<AlertmanagerConnectorResponse> {
      const response = await transport.post<AlertmanagerConnectorResponse>(
        '/api/connectors/alertmanager',
        request,
      );
      return response.data;
    },

    async createJiraConnector(
      request: CreateJiraConnectorRequest,
    ): Promise<JiraConnectorResponse> {
      const response = await transport.post<JiraConnectorResponse>(
        '/api/connectors/jira',
        request,
      );
      return response.data;
    },

    async createConfluenceConnector(
      request: CreateConfluenceConnectorRequest,
    ): Promise<ConfluenceConnectorResponse> {
      const response = await transport.post<ConfluenceConnectorResponse>(
        '/api/connectors/confluence',
        request,
      );
      return response.data;
    },

    async createEmailConnector(
      request: CreateEmailConnectorRequest,
    ): Promise<EmailConnectorResponse> {
      const response = await transport.post<EmailConnectorResponse>(
        '/api/connectors/email',
        request,
      );
      return response.data;
    },

    async createArgoConnector(
      request: CreateArgoConnectorRequest,
    ): Promise<ArgoConnectorResponse> {
      const response = await transport.post<ArgoConnectorResponse>(
        '/api/connectors/argocd',
        request,
      );
      return response.data;
    },

    async createGitHubConnector(
      request: CreateGitHubConnectorRequest,
    ): Promise<GitHubConnectorResponse> {
      const response = await transport.post<GitHubConnectorResponse>(
        '/api/connectors/github',
        request,
      );
      return response.data;
    },

    async createMCPConnector(
      request: CreateMCPConnectorRequest,
    ): Promise<MCPConnectorResponse> {
      const response = await transport.post<MCPConnectorResponse>(
        '/api/connectors/mcp',
        request,
      );
      return response.data;
    },

    async createSlackConnector(
      request: CreateSlackConnectorRequest,
    ): Promise<SlackConnectorResponse> {
      const response = await transport.post<SlackConnectorResponse>(
        '/api/connectors/slack',
        request,
      );
      return response.data;
    },

    /** Email delivery log for a given email connector. */
    async getEmailHistory(connectorId: string): Promise<EmailDeliveryLogEntry[]> {
      const response = await transport.get<EmailDeliveryLogEntry[]>(
        `/api/connectors/${connectorId}/email-history`,
      );
      return response.data;
    },

    async updateConnector(
      connectorId: string,
      request: UpdateConnectorRequest,
    ): Promise<Connector> {
      const response = await transport.patch<Connector>(
        `/api/connectors/${connectorId}`,
        request,
      );
      return response.data;
    },

    /** Persist a custom skill override for a connector. */
    async saveCustomSkill(
      connectorId: string,
      customSkill: string,
    ): Promise<Connector> {
      const response = await transport.put<Connector>(
        `/api/connectors/${connectorId}/skill`,
        { custom_skill: customSkill },
      );
      return response.data;
    },

    /** Regenerate the LLM-authored skill from the current spec/operations. */
    async regenerateSkill(connectorId: string): Promise<RegenerateSkillResponse> {
      const response = await transport.post<RegenerateSkillResponse>(
        `/api/connectors/${connectorId}/skill/regenerate`,
      );
      return response.data;
    },

    async deleteConnector(connectorId: string): Promise<void> {
      await transport.delete(`/api/connectors/${connectorId}`);
    },

    // ===== OpenAPI spec + endpoints =====

    async downloadOpenAPISpec(connectorId: string): Promise<Blob> {
      const response = await transport.get(
        `/api/connectors/${connectorId}/openapi-spec/download`,
        { responseType: 'blob' },
      );
      return response.data;
    },

    async uploadOpenAPISpec(
      connectorId: string,
      file: File,
    ): Promise<{ message: string; endpoints_count: number }> {
      const formData = new FormData();
      formData.append('file', file);

      const response = await transport.post(
        `/api/connectors/${connectorId}/openapi-spec`,
        formData,
        { headers: { 'Content-Type': 'multipart/form-data' } },
      );
      return response.data;
    },

    async listEndpoints(
      connectorId: string,
      filters?: {
        method?: string;
        is_enabled?: boolean;
        safety_level?: string;
        tags?: string;
        search?: string;
        limit?: number;
      },
    ): Promise<Endpoint[]> {
      const params = new URLSearchParams();
      if (filters?.method) params.set('method', filters.method);
      if (filters?.is_enabled !== undefined)
        params.set('is_enabled', filters.is_enabled.toString());
      if (filters?.safety_level) params.set('safety_level', filters.safety_level);
      if (filters?.tags) params.set('tags', filters.tags);
      if (filters?.search) params.set('search', filters.search);
      if (filters?.limit) params.set('limit', filters.limit.toString());

      const response = await transport.get<Endpoint[]>(
        `/api/connectors/${connectorId}/endpoints?${params.toString()}`,
      );
      return response.data;
    },

    async updateEndpoint(
      connectorId: string,
      endpointId: string,
      request: UpdateEndpointRequest,
    ): Promise<Endpoint> {
      const response = await transport.patch<Endpoint>(
        `/api/connectors/${connectorId}/endpoints/${endpointId}`,
        request,
      );
      return response.data;
    },

    async testEndpoint(
      connectorId: string,
      endpointId: string,
      request: TestEndpointRequest,
    ): Promise<TestEndpointResponse> {
      const response = await transport.post<TestEndpointResponse>(
        `/api/connectors/${connectorId}/endpoints/${endpointId}/test`,
        request,
      );
      return response.data;
    },

    // ===== Credentials + connection tests =====

    async setUserCredentials(
      connectorId: string,
      credentials: Record<string, string>,
    ): Promise<void> {
      await transport.post(`/api/connectors/${connectorId}/credentials`, credentials);
    },

    async getCredentialStatus(connectorId: string): Promise<CredentialStatus> {
      const response = await transport.get<CredentialStatus>(
        `/api/connectors/${connectorId}/credentials/status`,
      );
      return response.data;
    },

    async deleteUserCredentials(connectorId: string): Promise<void> {
      await transport.delete(`/api/connectors/${connectorId}/credentials`);
    },

    // ===== Service credentials (Phase 74, admin-only) =====

    async getServiceCredentialStatus(
      connectorId: string,
    ): Promise<ServiceCredentialStatus> {
      const response = await transport.get<ServiceCredentialStatus>(
        `/api/connectors/${connectorId}/service-credential`,
      );
      return response.data;
    },

    async setServiceCredential(
      connectorId: string,
      request: SetServiceCredentialRequest,
    ): Promise<void> {
      await transport.put(
        `/api/connectors/${connectorId}/service-credential`,
        request,
      );
    },

    async deleteServiceCredential(connectorId: string): Promise<void> {
      await transport.delete(`/api/connectors/${connectorId}/service-credential`);
    },

    async testConnection(
      connectorId: string,
      request: TestConnectionRequest,
    ): Promise<TestConnectionResponse> {
      const response = await transport.post<TestConnectionResponse>(
        `/api/connectors/${connectorId}/test-connection`,
        request,
      );
      return response.data;
    },

    /** Exercises an auth flow (used primarily for SESSION-auth sanity checks). */
    async testAuth(
      connectorId: string,
      request: TestAuthRequest,
    ): Promise<TestAuthResponse> {
      const response = await transport.post<TestAuthResponse>(
        `/api/connectors/${connectorId}/test-auth`,
        request,
      );
      return response.data;
    },

    // ===== Export / import (TASK-142) =====

    /** Returns the encrypted export Blob for file download. */
    async exportConnectors(request: ExportConnectorsRequest): Promise<Blob> {
      const response = await transport.post('/api/connectors/export', request, {
        responseType: 'blob',
      });
      return response.data;
    },

    async importConnectors(
      request: ImportConnectorsRequest,
    ): Promise<ImportConnectorsResponse> {
      const response = await transport.post<ImportConnectorsResponse>(
        '/api/connectors/import',
        request,
      );
      return response.data;
    },

    // ===== Memories (Phase 13) =====

    async listConnectorMemories(
      connectorId: string,
      params: {
        memory_type?: string;
        confidence_level?: string;
        limit?: number;
        offset?: number;
      } = {},
    ): Promise<MemoryResponse[]> {
      const searchParams = new URLSearchParams();
      if (params.memory_type) searchParams.set('memory_type', params.memory_type);
      if (params.confidence_level)
        searchParams.set('confidence_level', params.confidence_level);
      if (params.limit) searchParams.set('limit', params.limit.toString());
      if (params.offset) searchParams.set('offset', params.offset.toString());
      const query = searchParams.toString();
      const response = await transport.get<MemoryResponse[]>(
        `/api/connectors/${connectorId}/memories${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    async updateConnectorMemory(
      connectorId: string,
      memoryId: string,
      updates: MemoryUpdate,
    ): Promise<MemoryResponse> {
      const response = await transport.patch<MemoryResponse>(
        `/api/connectors/${connectorId}/memories/${memoryId}`,
        updates,
      );
      return response.data;
    },

    async deleteConnectorMemory(
      connectorId: string,
      memoryId: string,
    ): Promise<{ deleted: boolean }> {
      const response = await transport.delete<{ deleted: boolean }>(
        `/api/connectors/${connectorId}/memories/${memoryId}`,
      );
      return response.data;
    },

    // ===== Events (Phase 94 — Events System + Response Channels) =====

    async listConnectorEvents(connectorId: string): Promise<EventRegistration[]> {
      const response = await transport.get<EventRegistration[]>(
        `/api/connectors/${connectorId}/events`,
      );
      return response.data;
    },

    /** Creates an event registration. The secret is returned once and never resurfaced. */
    async createConnectorEvent(
      connectorId: string,
      data: {
        name: string;
        prompt_template?: string;
        rate_limit_per_hour?: number;
        require_signature?: boolean;
        allowed_connector_ids?: string[] | null;
        notification_targets?: Array<{ connector_id: string; contact: string }> | null;
        response_config?: {
          connector_id: string;
          operation_id: string;
          parameter_mapping: Record<string, string>;
        } | null;
      },
    ): Promise<EventCreateResponse> {
      const response = await transport.post<EventCreateResponse>(
        `/api/connectors/${connectorId}/events`,
        data,
      );
      return response.data;
    },

    async getConnectorEvent(
      connectorId: string,
      eventId: string,
    ): Promise<EventRegistration> {
      const response = await transport.get<EventRegistration>(
        `/api/connectors/${connectorId}/events/${eventId}`,
      );
      return response.data;
    },

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
        response_config?: {
          connector_id: string;
          operation_id: string;
          parameter_mapping: Record<string, string>;
        } | null;
      },
    ): Promise<EventRegistration> {
      const response = await transport.patch<EventRegistration>(
        `/api/connectors/${connectorId}/events/${eventId}`,
        data,
      );
      return response.data;
    },

    async deleteConnectorEvent(
      connectorId: string,
      eventId: string,
    ): Promise<{ deleted: boolean }> {
      const response = await transport.delete<{ deleted: boolean }>(
        `/api/connectors/${connectorId}/events/${eventId}`,
      );
      return response.data;
    },

    async getEventHistory(
      connectorId: string,
      eventId: string,
      params?: { limit?: number; offset?: number },
    ): Promise<EventHistoryResponse> {
      const searchParams = new URLSearchParams();
      if (params?.limit) searchParams.set('limit', params.limit.toString());
      if (params?.offset) searchParams.set('offset', params.offset.toString());
      const query = searchParams.toString();
      const response = await transport.get<EventHistoryResponse>(
        `/api/connectors/${connectorId}/events/${eventId}/history${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    async testConnectorEvent(
      connectorId: string,
      eventId: string,
      payload: object,
    ): Promise<EventTestResponse> {
      const response = await transport.post<EventTestResponse>(
        `/api/connectors/${connectorId}/events/${eventId}/test`,
        { payload },
      );
      return response.data;
    },

    /** LLM-generated prompt template scoped to a specific connector type. */
    async generateEventPrompt(
      connectorId: string,
      userInstructions?: string,
    ): Promise<{ prompt: string }> {
      const response = await transport.post<{ prompt: string }>(
        `/api/connectors/${connectorId}/events/generate-prompt`,
        userInstructions ? { user_instructions: userInstructions } : {},
      );
      return response.data;
    },

    // ===== SOAP =====

    async ingestWSDL(
      connectorId: string,
      wsdlUrl?: string,
    ): Promise<{
      message: string;
      operations_count: number;
      types_count: number;
      services: string[];
      ports: string[];
    }> {
      const response = await transport.post(
        `/api/connectors/${connectorId}/wsdl`,
        wsdlUrl ? { wsdl_url: wsdlUrl } : {},
      );
      return response.data;
    },

    async listSOAPOperations(
      connectorId: string,
      filters?: { service?: string; search?: string; limit?: number },
    ): Promise<
      Array<{
        name: string;
        service_name: string;
        port_name: string;
        operation_name: string;
        description?: string;
        soap_action?: string;
        input_schema: Record<string, unknown>;
        output_schema: Record<string, unknown>;
      }>
    > {
      const params = new URLSearchParams();
      if (filters?.service) params.set('service', filters.service);
      if (filters?.search) params.set('search', filters.search);
      if (filters?.limit) params.set('limit', filters.limit.toString());

      const response = await transport.get(
        `/api/connectors/${connectorId}/soap-operations?${params.toString()}`,
      );
      return response.data;
    },

    async callSOAPOperation(
      connectorId: string,
      operationName: string,
      params: Record<string, unknown>,
    ): Promise<{
      success: boolean;
      status_code: number;
      body: unknown;
      duration_ms?: number;
    }> {
      const response = await transport.post(
        `/api/connectors/${connectorId}/soap-operations/${operationName}/call`,
        { params },
      );
      return response.data;
    },

    // ===== Operations / types =====

    async listConnectorOperations(
      connectorId: string,
      filters?: { search?: string; category?: string; limit?: number },
    ): Promise<Array<ConnectorOperation>> {
      const params = new URLSearchParams();
      if (filters?.search) params.set('search', filters.search);
      if (filters?.category) params.set('category', filters.category);
      if (filters?.limit) params.set('limit', filters.limit.toString());

      const response = await transport.get(
        `/api/connectors/${connectorId}/operations?${params.toString()}`,
      );
      return response.data;
    },

    /**
     * Toggle enable/disable for an operation on a specific connector instance.
     * For type-level ops this creates a disable override; for custom ops it
     * flips `is_enabled` on the operation row.
     */
    async toggleConnectorOperation(
      connectorId: string,
      operationId: string,
    ): Promise<ConnectorOperation> {
      const response = await transport.patch(
        `/api/connectors/${connectorId}/operations/${operationId}/toggle`,
      );
      return response.data;
    },

    /** Create or update an instance-level override of a type-level operation. */
    async overrideConnectorOperation(
      connectorId: string,
      operationId: string,
      overrides: {
        description?: string;
        safety_level?: string;
        parameters?: Array<{
          name: string;
          type: string;
          required?: boolean;
          description?: string;
        }>;
      },
    ): Promise<ConnectorOperation> {
      const response = await transport.put(
        `/api/connectors/${connectorId}/operations/${operationId}/override`,
        overrides,
      );
      return response.data;
    },

    async resetConnectorOperationOverride(
      connectorId: string,
      operationId: string,
    ): Promise<void> {
      await transport.delete(
        `/api/connectors/${connectorId}/operations/${operationId}/override`,
      );
    },

    async listConnectorTypes(
      connectorId: string,
      filters?: { search?: string; category?: string; limit?: number },
    ): Promise<Array<ConnectorEntityType>> {
      const params = new URLSearchParams();
      if (filters?.search) params.set('search', filters.search);
      if (filters?.category) params.set('category', filters.category);
      if (filters?.limit) params.set('limit', filters.limit.toString());

      const response = await transport.get(
        `/api/connectors/${connectorId}/types?${params.toString()}`,
      );
      return response.data;
    },

    // ===== SOAP type definitions =====

    async listSOAPTypes(
      connectorId: string,
      filters?: { search?: string; base_type?: string; limit?: number },
    ): Promise<Array<SOAPTypeDefinition>> {
      const params = new URLSearchParams();
      if (filters?.search) params.set('search', filters.search);
      if (filters?.base_type) params.set('base_type', filters.base_type);
      if (filters?.limit) params.set('limit', filters.limit.toString());

      const response = await transport.get(
        `/api/connectors/${connectorId}/soap-types?${params.toString()}`,
      );
      return response.data;
    },

    async getSOAPType(
      connectorId: string,
      typeName: string,
    ): Promise<SOAPTypeDefinition> {
      const response = await transport.get(
        `/api/connectors/${connectorId}/soap-types/${encodeURIComponent(typeName)}`,
      );
      return response.data;
    },
  };
}

let connectorsClient: ReturnType<typeof createConnectorsClient> | null = null;

export function getConnectorsClient(): ReturnType<typeof createConnectorsClient> {
  if (!connectorsClient) {
    connectorsClient = createConnectorsClient(getTransport());
  }
  return connectorsClient;
}
