// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * API Types - Barrel Export
 * 
 * Centralized export of all API types.
 */

// Workflow types
export type {
  Workflow,
  Plan,
  PlanStep,
  ExecutionResult,
} from './workflow';

// Chat types
export type {
  ChatRequest,
  ChatResponse,
  ChatSession,
  ChatMessage,
  SessionWithMessages,
  CreateSessionRequest,
  UpdateSessionRequest,
  AddMessageRequest,
  TeamSession,
} from './chat';

// Knowledge types
export type {
  KnowledgeSearchRequest,
  KnowledgeChunk,
  SearchKnowledgeResponse,
  UploadDocumentRequest,
  UploadDocumentResponse,
  IngestionProgress,
  IngestionJobStatus,
  IngestTextRequest,
  IngestTextResponse,
  KnowledgeDocument,
  KnowledgeChunkDetail,
  ListChunksRequest,
  ListChunksResponse,
  ListDocumentsRequest,
  ListDocumentsResponse,
} from './knowledge';

// Connector types
export type {
  ConnectorType,
  SOAPConnectorConfig,
  SOAPTypeProperty,
  SOAPTypeDefinition,
  LoginConfig,
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
  CreateAzureConnectorRequest,
  AzureConnectorResponse,
  CreateArgoConnectorRequest,
  ArgoConnectorResponse,
  CreateGitHubConnectorRequest,
  GitHubConnectorResponse,
  CreateMCPConnectorRequest,
  MCPConnectorResponse,
  CreateSlackConnectorRequest,
  SlackConnectorResponse,
  ParameterField,
  ParameterMetadata,
  Endpoint,
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
} from './connector';

// Recipe types
export type {
  RecipeParameter,
  Recipe,
  RecipeExecution,
} from './recipe';

// Common types
export type { APIError } from './common';

// Tenant types
export type {
  SubscriptionTier,
  Tenant,
  TenantListResponse,
  CreateTenantRequest,
  UpdateTenantRequest,
} from './tenant';

// Admin/Dashboard types
export type {
  DashboardStats,
  ActivityType,
  ActivityItem,
} from './admin';

// Memory types (Phase 13 - Memory UI)
export type {
  MemoryType,
  ConfidenceLevel,
  MemoryResponse,
  MemoryUpdate,
} from './memory';

// Orchestrator types (TASK-181)
export type {
  AgentSource,
  WrappedAgentEvent,
  OrchestratorStartEvent,
  IterationStartEvent,
  DispatchStartEvent,
  ConnectorCompleteEvent,
  EarlyFindingsEvent,
  IterationCompleteEvent,
  SynthesisStartEvent,
  FinalAnswerEvent,
  OrchestratorCompleteEvent,
  OrchestratorErrorEvent,
  ConnectorStatus,
  ConnectorState,
  OrchestratorEvent,
} from './orchestrator';

export {
  isOrchestratorEvent,
  isOrchestratorMode,
} from './orchestrator';

// Observability types (TASK-186)
export type {
  TokenUsage,
  EventDetails,
  EventResponse,
  SessionSummary,
  TranscriptResponse,
  TranscriptItem,
  MultiTranscriptResponse,
  SessionListItem,
  SessionListResponse,
  SearchResultItem,
  SearchResponse,
  SessionListParams,
  TranscriptParams,
  LLMParams,
  HTTPParams,
  SQLParams,
  SearchParams,
  EventType,
} from './observability';

export {
  hasLLMDetails,
  hasHTTPDetails,
  hasSQLDetails,
  hasToolDetails,
} from './observability';

// Event types (Phase 94 - Events System + Response Channels)
export type {
  EventRegistration,
  EventCreateResponse,
  EventHistoryEntry,
  EventHistoryResponse,
  EventTestStep,
  EventTestResponse,
} from './event';

// Scheduled Task types (Phase 45 - Scheduled Tasks)
export type {
  ScheduledTask,
  ScheduledTaskRun,
  CreateScheduledTaskRequest,
  UpdateScheduledTaskRequest,
  ParseScheduleResponse,
  ValidateCronResponse,
} from './scheduledTask';

// Orchestrator Skill types (Phase 52 - Orchestrator Skills Frontend)
export type {
  OrchestratorSkillSummary,
  OrchestratorSkill,
  CreateSkillRequest,
  UpdateSkillRequest,
  GenerateSkillRequest,
  GenerateSkillResponse,
} from '../orchestratorSkills';

// Audit types (Phase 58 - Security Hardening)
export type {
  AuditEvent,
  AuditEventsResponse,
  AuditEventFilters,
} from './audit';

