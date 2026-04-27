// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connector Types
 * 
 * Types for connector management, endpoints, credentials, and authentication.
 */

// Connector type classification
export type ConnectorType = 'rest' | 'soap' | 'graphql' | 'grpc' | 'vmware' | 'kubernetes' | 'proxmox' | 'gcp' | 'azure' | 'aws' | 'prometheus' | 'loki' | 'tempo' | 'alertmanager' | 'jira' | 'confluence' | 'email' | 'argocd' | 'github' | 'mcp' | 'slack';

// SOAP connector configuration
export interface SOAPConnectorConfig {
  wsdl_url: string;
  auth_type?: 'none' | 'basic' | 'session' | 'ws_security' | 'certificate';
  login_operation?: string;
  logout_operation?: string;
  session_cookie_name?: string;
  timeout?: number;
  verify_ssl?: boolean;
}

// SOAP type definitions
export interface SOAPTypeProperty {
  name: string;
  type_name: string;
  is_array: boolean;
  is_required: boolean;
  description?: string;
}

export interface SOAPTypeDefinition {
  type_name: string;
  namespace?: string;
  base_type?: string;
  properties: SOAPTypeProperty[];
  description?: string;
}

// Login configuration for SESSION auth
export interface LoginConfig {
  login_auth_type?: 'basic' | 'body';
  login_headers?: Record<string, string>;
  body_template?: Record<string, string>;
  token_location?: 'header' | 'cookie' | 'body';
  token_name?: string;
  token_path?: string;
  header_name?: string;
  session_duration_seconds?: number;
  refresh_token_path?: string;
  refresh_url?: string;
  refresh_method?: string;
  refresh_token_expires_in?: number;
}

// Main Connector type
export interface Connector {
  id: string;
  name: string;
  base_url: string;
  auth_type: 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION';
  description?: string;
  // Routing description for orchestrator LLM routing decisions
  routing_description?: string;
  tenant_id: string;
  connector_type: ConnectorType;
  protocol_config?: SOAPConnectorConfig | Record<string, unknown>;
  allowed_methods: string[];
  blocked_methods: string[];
  default_safety_level: 'safe' | 'caution' | 'dangerous';
  is_active: boolean;
  login_url?: string;
  login_method?: 'POST' | 'GET';
  login_config?: LoginConfig;
  // Related connectors for cross-connector topology correlation
  // E.g., K8s connector's related_connector_ids includes the GCP connector that hosts it
  related_connector_ids?: string[];
  // Phase 75: Automation access toggle
  automation_enabled: boolean;
  created_at: string;
  updated_at: string;
  
  // Credential masking indicators (Phase 3 - TASK-140)
  // Set to true when superadmin views tenant connector and credentials are masked
  auth_config_masked?: boolean;
  login_config_masked?: boolean;
  protocol_config_masked?: boolean;

  // Skill fields (Phase 7 - Skill Editor UI)
  generated_skill?: string;
  custom_skill?: string;
  skill_quality_score?: number;
}

// Create/Update requests
export interface CreateConnectorRequest {
  name: string;
  base_url: string;
  auth_type: 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION';
  description?: string;
  routing_description?: string;
  connector_type?: ConnectorType;
  protocol_config?: SOAPConnectorConfig | Record<string, unknown>;
  allowed_methods?: string[];
  blocked_methods?: string[];
  default_safety_level?: 'safe' | 'caution' | 'dangerous';
  login_url?: string;
  login_method?: 'POST' | 'GET';
  login_config?: LoginConfig;
  // Related connectors for cross-connector topology correlation
  related_connector_ids?: string[];
}

export interface UpdateConnectorRequest {
  name?: string;
  description?: string;
  routing_description?: string;
  base_url?: string;
  auth_type?: 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION';
  connector_type?: ConnectorType;
  protocol_config?: SOAPConnectorConfig | Record<string, unknown>;
  allowed_methods?: string[];
  blocked_methods?: string[];
  default_safety_level?: 'safe' | 'caution' | 'dangerous';
  is_active?: boolean;
  login_url?: string;
  login_method?: 'POST' | 'GET';
  login_config?: Partial<LoginConfig>;
  // Related connectors for cross-connector topology correlation
  related_connector_ids?: string[];
  // Skill fields (Phase 7 - Skill Editor UI)
  custom_skill?: string;
  // Automation access toggle (Phase 75)
  automation_enabled?: boolean;
}

// Skill regeneration response (Phase 7 - Skill Editor UI)
export interface RegenerateSkillResponse {
  generated_skill: string;
  skill_quality_score?: number;
  operation_count: number;
}

// VMware connector
export interface CreateVMwareConnectorRequest {
  name: string;
  description?: string;
  vcenter_host: string;
  port?: number;
  disable_ssl_verification?: boolean;
  username: string;
  password: string;
}

export interface VMwareConnectorResponse {
  id: string;
  name: string;
  vcenter_host: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Proxmox connector
export interface CreateProxmoxConnectorRequest {
  name: string;
  description?: string;
  host: string;
  port?: number;
  disable_ssl_verification?: boolean;
  api_token_id?: string;
  api_token_secret?: string;
  username?: string;
  password?: string;
}

export interface ProxmoxConnectorResponse {
  id: string;
  name: string;
  host: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Kubernetes connector
export interface CreateKubernetesConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  server_url: string;
  token: string;
  skip_tls_verification?: boolean;
  ca_certificate?: string;
}

export interface KubernetesConnectorResponse {
  id: string;
  name: string;
  server_url: string;
  connector_type: 'kubernetes';
  kubernetes_version?: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// GCP connector
export interface CreateGCPConnectorRequest {
  name: string;
  description?: string;
  project_id: string;
  default_region?: string;
  default_zone?: string;
  service_account_json: string;
}

export interface GCPConnectorResponse {
  id: string;
  name: string;
  project_id: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Azure connector
export interface CreateAzureConnectorRequest {
  name: string;
  description?: string;
  tenant_id: string;
  client_id: string;
  client_secret: string;
  subscription_id: string;
  resource_group_filter?: string;
}

export interface AzureConnectorResponse {
  id: string;
  name: string;
  subscription_id: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// AWS connector
export interface CreateAWSConnectorRequest {
  name: string;
  description?: string;
  default_region: string;
  aws_access_key_id?: string;
  aws_secret_access_key?: string;
}

export interface AWSConnectorResponse {
  id: string;
  name: string;
  default_region: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Prometheus connector
export interface CreatePrometheusConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  base_url: string;
  auth_type?: 'none' | 'basic' | 'bearer';
  username?: string;
  password?: string;
  token?: string;
  skip_tls_verification?: boolean;
}

export interface PrometheusConnectorResponse {
  id: string;
  name: string;
  base_url: string;
  connector_type: 'prometheus';
  prometheus_version?: string;
  auth_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Loki connector
export interface CreateLokiConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  base_url: string;
  auth_type?: 'none' | 'basic' | 'bearer';
  username?: string;
  password?: string;
  token?: string;
  skip_tls_verification?: boolean;
}

export interface LokiConnectorResponse {
  id: string;
  name: string;
  base_url: string;
  connector_type: 'loki';
  loki_version?: string;
  auth_type: string;
  operations_registered: number;
  message: string;
}

// Tempo connector
export interface CreateTempoConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  base_url: string;
  auth_type?: 'none' | 'basic' | 'bearer';
  username?: string;
  password?: string;
  token?: string;
  skip_tls_verification?: boolean;
  org_id?: string;
}

export interface TempoConnectorResponse {
  id: string;
  name: string;
  base_url: string;
  connector_type: 'tempo';
  tempo_version?: string;
  auth_type: string;
  operations_registered: number;
  message: string;
}

// Alertmanager connector
export interface CreateAlertmanagerConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  base_url: string;
  auth_type?: 'none' | 'basic' | 'bearer';
  username?: string;
  password?: string;
  token?: string;
  skip_tls_verification?: boolean;
}

export interface AlertmanagerConnectorResponse {
  id: string;
  name: string;
  base_url: string;
  connector_type: 'alertmanager';
  alertmanager_version?: string;
  auth_type: string;
  operations_registered: number;
  message: string;
}

// Jira connector
export interface CreateJiraConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  site_url: string;
  email: string;
  api_token: string;
}

export interface JiraConnectorResponse {
  id: string;
  name: string;
  site_url: string;
  connector_type: 'jira';
  jira_user?: string;
  accessible_projects: number;
  operations_registered: number;
  message: string;
}

// Confluence connector
export interface CreateConfluenceConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  site_url: string;
  email: string;
  api_token: string;
}

export interface ConfluenceConnectorResponse {
  id: string;
  name: string;
  connector_type: 'confluence';
  confluence_user?: string;
  accessible_spaces?: number;
  operations_registered: number;
  message: string;
}

// Email connector
export type EmailProviderType = 'smtp' | 'sendgrid' | 'mailgun' | 'ses' | 'generic_http';

export interface CreateEmailConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  provider_type: EmailProviderType;
  from_email: string;
  from_name?: string;
  default_recipients: string;
  // SMTP fields
  smtp_host?: string;
  smtp_port?: number;
  smtp_tls?: boolean;
  smtp_username?: string;
  smtp_password?: string;
  // SendGrid fields
  sendgrid_api_key?: string;
  // Mailgun fields
  mailgun_api_key?: string;
  mailgun_domain?: string;
  // SES fields
  ses_access_key?: string;
  ses_secret_key?: string;
  ses_region?: string;
  // Generic HTTP fields
  http_endpoint_url?: string;
  http_auth_header?: string;
  http_payload_template?: string;
}

export interface EmailConnectorResponse {
  id: string;
  name: string;
  connector_type: 'email';
  provider_type: EmailProviderType;
  from_email: string;
  test_email_sent: boolean;
  operations_registered: number;
  message: string;
}

export interface EmailDeliveryLogEntry {
  id: string;
  from_email: string;
  to_emails: string[];
  subject: string;
  provider_type: EmailProviderType;
  provider_message_id: string | null;
  status: 'sent' | 'accepted' | 'failed';
  error_message: string | null;
  created_at: string;
}

// ArgoCD connector
export interface CreateArgoConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  server_url: string;
  api_token: string;
  skip_tls_verification?: boolean;
}

export interface ArgoConnectorResponse {
  id: string;
  name: string;
  server_url: string;
  connector_type: 'argocd';
  operations_registered: number;
  message: string;
}

// GitHub connector
export interface CreateGitHubConnectorRequest {
  name: string;
  description?: string;
  routing_description?: string;
  organization: string;
  personal_access_token: string;
  base_url?: string;
}

export interface GitHubConnectorResponse {
  id: string;
  name: string;
  base_url: string;
  organization: string;
  connector_type: 'github';
  operations_registered: number;
  message: string;
}

// MCP connector (Phase 93)
export interface CreateMCPConnectorRequest {
  name: string;
  description?: string;
  server_url?: string;
  transport_type: 'streamable_http' | 'stdio';
  command?: string;
  args?: string[];
  api_key?: string;
}

export interface MCPConnectorResponse {
  id: string;
  name: string;
  server_url: string | null;
  transport_type: string;
  connector_type: string;
  tools_discovered: number;
  operations_registered: number;
  message: string;
}

// Slack connector
export interface CreateSlackConnectorRequest {
  name: string;
  description?: string;
  slack_bot_token: string;
  slack_app_token?: string;
  slack_user_token?: string;
}

export interface SlackConnectorResponse {
  id: string;
  name: string;
  connector_type: string;
  operations_registered: number;
  types_registered: number;
  message: string;
}

// Endpoint parameter field descriptor
export interface ParameterField {
  name: string;
  type?: string;
  description?: string;
  schema?: Record<string, unknown>;
}

// Endpoint parameter metadata
export interface ParameterMetadata {
  path_params?: { required: ParameterField[]; optional: ParameterField[] };
  query_params?: { required: ParameterField[]; optional: ParameterField[] };
  header_params?: { required: ParameterField[]; optional: ParameterField[] };
  body?: { required: boolean; required_fields: ParameterField[]; optional_fields: ParameterField[] };
}

// Endpoint type
export interface Endpoint {
  id: string;
  connector_id: string;
  method: string;
  path: string;
  operation_id?: string;
  summary?: string;
  description?: string;
  tags: string[];
  is_enabled: boolean;
  safety_level: 'safe' | 'caution' | 'dangerous' | 'read' | 'write' | 'destructive' | 'auto';
  requires_approval: boolean;
  custom_description?: string;
  custom_notes?: string;
  usage_examples?: Record<string, unknown>;
  last_modified_by?: string;
  path_params_schema?: Record<string, unknown>;
  query_params_schema?: Record<string, unknown>;
  body_schema?: Record<string, unknown>;
  response_schema?: Record<string, unknown>;
  required_params?: string[];
  parameter_metadata?: ParameterMetadata;
  last_modified_at?: string;
  created_at: string;
}

export interface UpdateEndpointRequest {
  is_enabled?: boolean;
  safety_level?: 'safe' | 'caution' | 'dangerous' | 'read' | 'write' | 'destructive' | 'auto';
  requires_approval?: boolean;
  custom_description?: string;
  custom_notes?: string;
  usage_examples?: Record<string, unknown>;
}

export interface TestEndpointRequest {
  path_params?: Record<string, unknown>;
  query_params?: Record<string, unknown>;
  body?: unknown;
  use_system_credentials?: boolean;
}

export interface TestEndpointResponse {
  status_code: number;
  headers: Record<string, string>;
  body: unknown;
  duration_ms: number;
  error?: string;
}

// Credential management
export interface CredentialStatus {
  has_credentials: boolean;
  credential_type: string | null;
  last_used_at: string | null;
  // Phase 75: Credential health tracking
  credential_health: 'healthy' | 'unhealthy' | 'expired' | null;
  credential_health_message: string | null;
  credential_health_checked_at: string | null;
}

export interface TestConnectionRequest {
  credentials?: Record<string, string>;
  use_stored_credentials?: boolean;
}

export interface TestConnectionResponse {
  success: boolean;
  message: string;
  response_time_ms?: number;
  tested_endpoint?: string;
  status_code?: number;
  error_detail?: string;
}

export interface TestAuthRequest {
  credentials?: Record<string, string>;
}

export interface TestAuthResponse {
  success: boolean;
  message: string;
  auth_type: string;
  session_token_obtained?: boolean;
  session_expires_at?: string;
  error_detail?: string;
  request_url?: string;
  request_method?: string;
  response_status?: number;
  response_time_ms?: number;
}

// Connector health/reachability status (Phase 24 - Health Monitoring)
export interface ConnectorHealth {
  connector_id: string;
  name: string;
  connector_type: string;
  status: 'reachable' | 'unreachable';
  latency_ms: number | null;
  error: string | null;
  last_checked: string;
}

// Generic connector operations and types
export interface ConnectorOperation {
  id?: string;
  operation_id: string;
  name: string;
  description?: string;
  category?: string;
  parameters: Array<{
    name: string;
    type: string;
    required?: boolean;
    description?: string;
  }>;
  example?: string;
  /** Operation source: 'type' = inherited from type-level definition, 'custom' = instance-specific */
  source?: 'type' | 'custom';
  /** Whether the operation is enabled on this instance */
  is_enabled?: boolean;
  /** Safety level of the operation */
  safety_level?: string;
}

export interface ConnectorEntityType {
  type_name: string;
  description?: string;
  category?: string;
  properties: Array<{
    name: string;
    type: string;
    required?: boolean;
    description?: string;
  }>;
}

// =============================================================================
// Export/Import Types (TASK-142)
// =============================================================================

export type ExportFormat = 'json' | 'yaml';
export type ConflictStrategy = 'skip' | 'overwrite' | 'rename';

export interface ExportConnectorsRequest {
  connector_ids: string[];  // Empty = export all
  password: string;         // Min 8 chars
  format: ExportFormat;
}

export interface ImportConnectorsRequest {
  file_content: string;     // Base64 encoded
  password: string;
  conflict_strategy: ConflictStrategy;
}

export interface ImportConnectorsResponse {
  imported: number;
  skipped: number;
  errors: string[];
  connectors: string[];     // Names of imported connectors
  // Phase 9: Operations sync results
  warnings: string[];       // Warnings (e.g., operations sync failed)
  operations_synced: number; // Total operations synced across all connectors
}

