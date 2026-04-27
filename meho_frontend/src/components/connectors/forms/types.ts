// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import type { KubeConnectionInfo } from '../../../lib/kubeconfig';
import type { EmailProviderType } from '../../../api/types/connector';

export const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const;

export interface ConnectorFormBaseProps {
  submitting: boolean;
}

// ─── REST ────────────────────────────────────────────────────────────────────

export interface RestFormState {
  baseUrl: string;
  openapiUrl: string;
  authType: 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION';
  allowedMethods: string[];
  defaultSafetyLevel: 'safe' | 'caution' | 'dangerous';
  showKubeconfigImport: boolean;
  kubeconfigText: string;
  kubeconfigContexts: string[];
  selectedKubeContext: string;
  kubeconfigInfo: KubeConnectionInfo | null;
  kubeconfigError: string | null;
  pendingCredentials: { access_token?: string; username?: string; password?: string } | null;
  loginUrl: string;
  loginMethod: 'POST' | 'GET';
  loginAuthType: 'body' | 'basic';
  customLoginHeaders: Array<{ key: string; value: string }>;
  tokenLocation: 'header' | 'cookie' | 'body';
  tokenName: string;
  tokenPath: string;
  headerName: string;
  sessionDuration: number;
}

export const DEFAULT_REST_STATE: RestFormState = {
  baseUrl: '',
  openapiUrl: '',
  authType: 'API_KEY',
  allowedMethods: [...HTTP_METHODS],
  defaultSafetyLevel: 'safe',
  showKubeconfigImport: false,
  kubeconfigText: '',
  kubeconfigContexts: [],
  selectedKubeContext: '',
  kubeconfigInfo: null,
  kubeconfigError: null,
  pendingCredentials: null,
  loginUrl: '/api/v1/auth/login',
  loginMethod: 'POST',
  loginAuthType: 'body',
  customLoginHeaders: [],
  tokenLocation: 'header',
  tokenName: 'X-Auth-Token',
  tokenPath: '$.token',
  headerName: '',
  sessionDuration: 3600,
};

// ─── SOAP ────────────────────────────────────────────────────────────────────

export interface SoapFormState {
  wsdlUrl: string;
  authType: 'none' | 'basic' | 'session';
  timeout: number;
  verifySsl: boolean;
}

export const DEFAULT_SOAP_STATE: SoapFormState = {
  wsdlUrl: '',
  authType: 'none',
  timeout: 30,
  verifySsl: true,
};

// ─── VMWARE ──────────────────────────────────────────────────────────────────

export interface VmwareFormState {
  host: string;
  port: number;
  disableSsl: boolean;
  username: string;
  password: string;
}

export const DEFAULT_VMWARE_STATE: VmwareFormState = {
  host: '',
  port: 443,
  disableSsl: false,
  username: '',
  password: '',
};

// ─── PROXMOX ─────────────────────────────────────────────────────────────────

export interface ProxmoxFormState {
  host: string;
  port: number;
  disableSsl: boolean;
  authType: 'token' | 'password';
  username: string;
  password: string;
  tokenId: string;
  tokenSecret: string;
}

export const DEFAULT_PROXMOX_STATE: ProxmoxFormState = {
  host: '',
  port: 8006,
  disableSsl: false,
  authType: 'password',
  username: '',
  password: '',
  tokenId: '',
  tokenSecret: '',
};

// ─── KUBERNETES ──────────────────────────────────────────────────────────────

export interface KubernetesFormState {
  serverUrl: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_KUBERNETES_STATE: KubernetesFormState = {
  serverUrl: '',
  token: '',
  skipTls: false,
  routingDescription: '',
};

// ─── GCP ─────────────────────────────────────────────────────────────────────

export interface GcpFormState {
  projectId: string;
  defaultRegion: string;
  defaultZone: string;
  serviceAccountJson: string;
}

export const DEFAULT_GCP_STATE: GcpFormState = {
  projectId: '',
  defaultRegion: 'us-central1',
  defaultZone: 'us-central1-a',
  serviceAccountJson: '',
};

// ─── AZURE ───────────────────────────────────────────────────────────────────

export interface AzureFormState {
  tenantId: string;
  clientId: string;
  clientSecret: string;
  subscriptionId: string;
  resourceGroupFilter: string;
}

export const DEFAULT_AZURE_STATE: AzureFormState = {
  tenantId: '',
  clientId: '',
  clientSecret: '',
  subscriptionId: '',
  resourceGroupFilter: '',
};

// ─── AWS ─────────────────────────────────────────────────────────────────────

export interface AwsFormState {
  accessKeyId: string;
  secretAccessKey: string;
  defaultRegion: string;
}

export const DEFAULT_AWS_STATE: AwsFormState = {
  accessKeyId: '',
  secretAccessKey: '',
  defaultRegion: 'us-east-1',
};

// ─── PROMETHEUS ──────────────────────────────────────────────────────────────

export interface PrometheusFormState {
  baseUrl: string;
  authType: 'none' | 'basic' | 'bearer';
  username: string;
  password: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_PROMETHEUS_STATE: PrometheusFormState = {
  baseUrl: '',
  authType: 'none',
  username: '',
  password: '',
  token: '',
  skipTls: false,
  routingDescription: '',
};

// ─── LOKI ────────────────────────────────────────────────────────────────────

export interface LokiFormState {
  baseUrl: string;
  authType: 'none' | 'basic' | 'bearer';
  username: string;
  password: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_LOKI_STATE: LokiFormState = {
  baseUrl: '',
  authType: 'none',
  username: '',
  password: '',
  token: '',
  skipTls: false,
  routingDescription: '',
};

// ─── TEMPO ───────────────────────────────────────────────────────────────────

export interface TempoFormState {
  baseUrl: string;
  authType: 'none' | 'basic' | 'bearer';
  username: string;
  password: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
  orgId: string;
}

export const DEFAULT_TEMPO_STATE: TempoFormState = {
  baseUrl: '',
  authType: 'none',
  username: '',
  password: '',
  token: '',
  skipTls: false,
  routingDescription: '',
  orgId: '',
};

// ─── ALERTMANAGER ────────────────────────────────────────────────────────────

export interface AlertmanagerFormState {
  baseUrl: string;
  authType: 'none' | 'basic' | 'bearer';
  username: string;
  password: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_ALERTMANAGER_STATE: AlertmanagerFormState = {
  baseUrl: '',
  authType: 'none',
  username: '',
  password: '',
  token: '',
  skipTls: false,
  routingDescription: '',
};

// ─── JIRA ────────────────────────────────────────────────────────────────────

export interface JiraFormState {
  siteUrl: string;
  email: string;
  apiToken: string;
  routingDescription: string;
}

export const DEFAULT_JIRA_STATE: JiraFormState = {
  siteUrl: '',
  email: '',
  apiToken: '',
  routingDescription: '',
};

// ─── CONFLUENCE ──────────────────────────────────────────────────────────────

export interface ConfluenceFormState {
  siteUrl: string;
  email: string;
  apiToken: string;
  routingDescription: string;
}

export const DEFAULT_CONFLUENCE_STATE: ConfluenceFormState = {
  siteUrl: '',
  email: '',
  apiToken: '',
  routingDescription: '',
};

// ─── ARGOCD ──────────────────────────────────────────────────────────────────

export interface ArgocdFormState {
  serverUrl: string;
  apiToken: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_ARGOCD_STATE: ArgocdFormState = {
  serverUrl: '',
  apiToken: '',
  skipTls: false,
  routingDescription: '',
};

// ─── GITHUB ──────────────────────────────────────────────────────────────────

export interface GithubFormState {
  organization: string;
  pat: string;
  baseUrl: string;
  routingDescription: string;
}

export const DEFAULT_GITHUB_STATE: GithubFormState = {
  organization: '',
  pat: '',
  baseUrl: 'https://api.github.com',
  routingDescription: '',
};

// ─── MCP ─────────────────────────────────────────────────────────────────────

export interface McpFormState {
  serverUrl: string;
  transportType: 'streamable_http' | 'stdio';
  command: string;
  apiKey: string;
}

export const DEFAULT_MCP_STATE: McpFormState = {
  serverUrl: '',
  transportType: 'streamable_http',
  command: '',
  apiKey: '',
};

// ─── SLACK ───────────────────────────────────────────────────────────────────

export interface SlackFormState {
  botToken: string;
  appToken: string;
  userToken: string;
}

export const DEFAULT_SLACK_STATE: SlackFormState = {
  botToken: '',
  appToken: '',
  userToken: '',
};

// ─── EMAIL ───────────────────────────────────────────────────────────────────

export interface EmailFormState {
  fromEmail: string;
  fromName: string;
  defaultRecipients: string;
  routingDescription: string;
  providerType: EmailProviderType;
  smtpHost: string;
  smtpPort: number;
  smtpTls: boolean;
  smtpUsername: string;
  smtpPassword: string;
  sendgridApiKey: string;
  mailgunApiKey: string;
  mailgunDomain: string;
  sesAccessKey: string;
  sesSecretKey: string;
  sesRegion: string;
  httpEndpointUrl: string;
  httpAuthHeader: string;
  httpPayloadTemplate: string;
}

export const DEFAULT_EMAIL_STATE: EmailFormState = {
  fromEmail: '',
  fromName: 'MEHO',
  defaultRecipients: '',
  routingDescription: '',
  providerType: 'smtp',
  smtpHost: '',
  smtpPort: 587,
  smtpTls: true,
  smtpUsername: '',
  smtpPassword: '',
  sendgridApiKey: '',
  mailgunApiKey: '',
  mailgunDomain: '',
  sesAccessKey: '',
  sesSecretKey: '',
  sesRegion: 'us-east-1',
  httpEndpointUrl: '',
  httpAuthHeader: '',
  httpPayloadTemplate: '',
};
