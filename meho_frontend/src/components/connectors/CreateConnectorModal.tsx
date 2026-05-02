// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { X, Plug, Loader2, CheckCircle, AlertCircle, Globe, FileCode } from 'lucide-react';
import { getConnectorsClient } from '@/api/clients/connectors';
import type { Connector, CreateConnectorRequest, CreateVMwareConnectorRequest, CreateProxmoxConnectorRequest, CreateKubernetesConnectorRequest, CreateGCPConnectorRequest, CreateAzureConnectorRequest, CreateAWSConnectorRequest, CreatePrometheusConnectorRequest, CreateLokiConnectorRequest, CreateTempoConnectorRequest, CreateAlertmanagerConnectorRequest, CreateJiraConnectorRequest, CreateConfluenceConnectorRequest, CreateEmailConnectorRequest, CreateArgoConnectorRequest, CreateGitHubConnectorRequest, CreateMCPConnectorRequest, CreateSlackConnectorRequest, ConnectorType } from '@/api/types';
import { motion } from 'motion/react';
import clsx from 'clsx';

import {
  HTTP_METHODS,
  DEFAULT_REST_STATE,
  DEFAULT_SOAP_STATE,
  DEFAULT_VMWARE_STATE,
  DEFAULT_PROXMOX_STATE,
  DEFAULT_KUBERNETES_STATE,
  DEFAULT_GCP_STATE,
  DEFAULT_AZURE_STATE,
  DEFAULT_AWS_STATE,
  DEFAULT_PROMETHEUS_STATE,
  DEFAULT_LOKI_STATE,
  DEFAULT_TEMPO_STATE,
  DEFAULT_ALERTMANAGER_STATE,
  DEFAULT_JIRA_STATE,
  DEFAULT_CONFLUENCE_STATE,
  DEFAULT_ARGOCD_STATE,
  DEFAULT_GITHUB_STATE,
  DEFAULT_MCP_STATE,
  DEFAULT_SLACK_STATE,
  DEFAULT_EMAIL_STATE,
} from './forms/types';
import type {
  RestFormState,
  SoapFormState,
  VmwareFormState,
  ProxmoxFormState,
  KubernetesFormState,
  GcpFormState,
  AzureFormState,
  AwsFormState,
  PrometheusFormState,
  LokiFormState,
  TempoFormState,
  AlertmanagerFormState,
  JiraFormState,
  ConfluenceFormState,
  ArgocdFormState,
  GithubFormState,
  McpFormState,
  SlackFormState,
  EmailFormState,
} from './forms/types';

import { RestForm, validateRestForm } from './forms/RestForm';
import { SoapForm, validateSoapForm } from './forms/SoapForm';
import { VmwareForm, validateVmwareForm } from './forms/VmwareForm';
import { ProxmoxForm, validateProxmoxForm } from './forms/ProxmoxForm';
import { KubernetesForm, validateKubernetesForm } from './forms/KubernetesForm';
import { GcpForm, validateGcpForm } from './forms/GcpForm';
import { AzureForm, validateAzureForm } from './forms/AzureForm';
import { AwsForm, validateAwsForm } from './forms/AwsForm';
import { PrometheusForm, validatePrometheusForm } from './forms/PrometheusForm';
import { LokiForm, validateLokiForm } from './forms/LokiForm';
import { TempoForm, validateTempoForm } from './forms/TempoForm';
import { AlertmanagerForm, validateAlertmanagerForm } from './forms/AlertmanagerForm';
import { JiraForm, validateJiraForm } from './forms/JiraForm';
import { ConfluenceForm, validateConfluenceForm } from './forms/ConfluenceForm';
import { ArgocdForm, validateArgocdForm } from './forms/ArgocdForm';
import { GithubForm, validateGithubForm } from './forms/GithubForm';
import { McpForm, validateMcpForm } from './forms/McpForm';
import { SlackForm, validateSlackForm } from './forms/SlackForm';
import { EmailForm, validateEmailForm } from './forms/EmailForm';

interface CreateConnectorModalProps {
  onClose: () => void;
  onSuccess: (connector: Connector) => void;
}

export function CreateConnectorModal({ onClose, onSuccess }: CreateConnectorModalProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [connectorType, setConnectorType] = useState<ConnectorType>('rest');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [restState, setRestState] = useState<RestFormState>(DEFAULT_REST_STATE);
  const [soapState, setSoapState] = useState<SoapFormState>(DEFAULT_SOAP_STATE);
  const [vmwareState, setVmwareState] = useState<VmwareFormState>(DEFAULT_VMWARE_STATE);
  const [proxmoxState, setProxmoxState] = useState<ProxmoxFormState>(DEFAULT_PROXMOX_STATE);
  const [k8sState, setK8sState] = useState<KubernetesFormState>(DEFAULT_KUBERNETES_STATE);
  const [gcpState, setGcpState] = useState<GcpFormState>(DEFAULT_GCP_STATE);
  const [azureState, setAzureState] = useState<AzureFormState>(DEFAULT_AZURE_STATE);
  const [awsState, setAwsState] = useState<AwsFormState>(DEFAULT_AWS_STATE);
  const [prometheusState, setPrometheusState] = useState<PrometheusFormState>(DEFAULT_PROMETHEUS_STATE);
  const [lokiState, setLokiState] = useState<LokiFormState>(DEFAULT_LOKI_STATE);
  const [tempoState, setTempoState] = useState<TempoFormState>(DEFAULT_TEMPO_STATE);
  const [alertmanagerState, setAlertmanagerState] = useState<AlertmanagerFormState>(DEFAULT_ALERTMANAGER_STATE);
  const [jiraState, setJiraState] = useState<JiraFormState>(DEFAULT_JIRA_STATE);
  const [confluenceState, setConfluenceState] = useState<ConfluenceFormState>(DEFAULT_CONFLUENCE_STATE);
  const [argocdState, setArgocdState] = useState<ArgocdFormState>(DEFAULT_ARGOCD_STATE);
  const [githubState, setGithubState] = useState<GithubFormState>(DEFAULT_GITHUB_STATE);
  const [mcpState, setMcpState] = useState<McpFormState>(DEFAULT_MCP_STATE);
  const [slackState, setSlackState] = useState<SlackFormState>(DEFAULT_SLACK_STATE);
  const [emailState, setEmailState] = useState<EmailFormState>(DEFAULT_EMAIL_STATE);

  const connectorsClient = getConnectorsClient();

  function isFormValid(): boolean {
    if (!name.trim()) return false;
    switch (connectorType) {
      case 'rest': return validateRestForm(restState) === null;
      case 'soap': return validateSoapForm(soapState) === null;
      case 'vmware': return validateVmwareForm(vmwareState) === null;
      case 'proxmox': return validateProxmoxForm(proxmoxState) === null;
      case 'kubernetes': return validateKubernetesForm(k8sState) === null;
      case 'gcp': return validateGcpForm(gcpState) === null;
      case 'azure': return validateAzureForm(azureState) === null;
      case 'aws': return validateAwsForm(awsState) === null;
      case 'prometheus': return validatePrometheusForm(prometheusState) === null;
      case 'loki': return validateLokiForm(lokiState) === null;
      case 'tempo': return validateTempoForm(tempoState) === null;
      case 'alertmanager': return validateAlertmanagerForm(alertmanagerState) === null;
      case 'jira': return validateJiraForm(jiraState) === null;
      case 'confluence': return validateConfluenceForm(confluenceState) === null;
      case 'argocd': return validateArgocdForm(argocdState) === null;
      case 'github': return validateGithubForm(githubState) === null;
      case 'mcp': return validateMcpForm(mcpState) === null;
      case 'slack': return validateSlackForm(slackState) === null;
      case 'email': return validateEmailForm(emailState) === null;
      default: return true;
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();

    if (!name.trim()) {
      setError('Name is required');
      return;
    }

    let typeError: string | null = null;
    switch (connectorType) {
      case 'rest': typeError = validateRestForm(restState); break;
      case 'soap': typeError = validateSoapForm(soapState); break;
      case 'vmware': typeError = validateVmwareForm(vmwareState); break;
      case 'proxmox': typeError = validateProxmoxForm(proxmoxState); break;
      case 'kubernetes': typeError = validateKubernetesForm(k8sState); break;
      case 'gcp': typeError = validateGcpForm(gcpState); break;
      case 'azure': typeError = validateAzureForm(azureState); break;
      case 'aws': typeError = validateAwsForm(awsState); break;
      case 'prometheus': typeError = validatePrometheusForm(prometheusState); break;
      case 'loki': typeError = validateLokiForm(lokiState); break;
      case 'tempo': typeError = validateTempoForm(tempoState); break;
      case 'alertmanager': typeError = validateAlertmanagerForm(alertmanagerState); break;
      case 'jira': typeError = validateJiraForm(jiraState); break;
      case 'confluence': typeError = validateConfluenceForm(confluenceState); break;
      case 'argocd': typeError = validateArgocdForm(argocdState); break;
      case 'github': typeError = validateGithubForm(githubState); break;
      case 'mcp': typeError = validateMcpForm(mcpState); break;
      case 'slack': typeError = validateSlackForm(slackState); break;
      case 'email': typeError = validateEmailForm(emailState); break;
    }
    if (typeError) {
      setError(typeError);
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const connector = await (async (): Promise<Connector> => {
        switch (connectorType) {
          case 'vmware': {
            let cleanedHost = vmwareState.host.trim();
            if (cleanedHost.startsWith('https://')) cleanedHost = cleanedHost.slice(8);
            else if (cleanedHost.startsWith('http://')) cleanedHost = cleanedHost.slice(7);
            cleanedHost = cleanedHost.replace(/\/+$/, '');
            const req: CreateVMwareConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              vcenter_host: cleanedHost,
              port: vmwareState.port,
              disable_ssl_verification: vmwareState.disableSsl,
              username: vmwareState.username.trim(),
              password: vmwareState.password,
            };
            const resp = await connectorsClient.createVMwareConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: `https://${resp.vcenter_host}`,
              auth_type: 'SESSION', description: description.trim() || undefined,
              tenant_id: '', connector_type: 'vmware',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'proxmox': {
            let cleanedHost = proxmoxState.host.trim();
            if (cleanedHost.startsWith('https://')) cleanedHost = cleanedHost.slice(8);
            else if (cleanedHost.startsWith('http://')) cleanedHost = cleanedHost.slice(7);
            cleanedHost = cleanedHost.replace(/\/+$/, '');
            const req: CreateProxmoxConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              host: cleanedHost,
              port: proxmoxState.port,
              disable_ssl_verification: proxmoxState.disableSsl,
              ...(proxmoxState.authType === 'token' ? {
                api_token_id: proxmoxState.tokenId.trim(),
                api_token_secret: proxmoxState.tokenSecret,
              } : {
                username: proxmoxState.username.trim(),
                password: proxmoxState.password,
              }),
            };
            const resp = await connectorsClient.createProxmoxConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: `https://${resp.host}:${proxmoxState.port}`,
              auth_type: proxmoxState.authType === 'token' ? 'API_KEY' : 'BASIC',
              description: description.trim() || undefined,
              tenant_id: '', connector_type: 'proxmox',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'kubernetes': {
            const req: CreateKubernetesConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: k8sState.routingDescription.trim() || undefined,
              server_url: k8sState.serverUrl.trim(),
              token: k8sState.token,
              skip_tls_verification: k8sState.skipTls,
            };
            const resp = await connectorsClient.createKubernetesConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: resp.server_url,
              auth_type: 'API_KEY',
              description: description.trim() || undefined,
              routing_description: k8sState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'kubernetes',
              allowed_methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
              blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'gcp': {
            const req: CreateGCPConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              project_id: gcpState.projectId.trim(),
              default_region: gcpState.defaultRegion,
              default_zone: gcpState.defaultZone,
              service_account_json: gcpState.serviceAccountJson,
            };
            const resp = await connectorsClient.createGCPConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: `https://console.cloud.google.com/home/dashboard?project=${resp.project_id}`,
              auth_type: 'API_KEY', description: description.trim() || undefined,
              tenant_id: '', connector_type: 'gcp',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'azure': {
            const req: CreateAzureConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              tenant_id: azureState.tenantId.trim(),
              client_id: azureState.clientId.trim(),
              client_secret: azureState.clientSecret.trim(),
              subscription_id: azureState.subscriptionId.trim(),
              resource_group_filter: azureState.resourceGroupFilter.trim() || undefined,
            };
            const resp = await connectorsClient.createAzureConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: 'https://portal.azure.com',
              auth_type: 'API_KEY', description: description.trim() || undefined,
              tenant_id: '', connector_type: 'azure',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'aws': {
            const req: CreateAWSConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              default_region: awsState.defaultRegion,
              aws_access_key_id: awsState.accessKeyId.trim() || undefined,
              aws_secret_access_key: awsState.secretAccessKey.trim() || undefined,
            };
            const resp = await connectorsClient.createAWSConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: `https://${awsState.defaultRegion}.console.aws.amazon.com`,
              auth_type: 'API_KEY', description: description.trim() || undefined,
              tenant_id: '', connector_type: 'aws',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'prometheus': {
            const req: CreatePrometheusConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: prometheusState.routingDescription.trim() || undefined,
              base_url: prometheusState.baseUrl.trim(),
              auth_type: prometheusState.authType,
              username: prometheusState.authType === 'basic' ? prometheusState.username : undefined,
              password: prometheusState.authType === 'basic' ? prometheusState.password : undefined,
              token: prometheusState.authType === 'bearer' ? prometheusState.token : undefined,
              skip_tls_verification: prometheusState.skipTls,
            };
            const resp = await connectorsClient.createPrometheusConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.base_url,
              auth_type: 'NONE', description: description.trim() || undefined,
              routing_description: prometheusState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'prometheus',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'loki': {
            const req: CreateLokiConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: lokiState.routingDescription.trim() || undefined,
              base_url: lokiState.baseUrl.trim(),
              auth_type: lokiState.authType,
              username: lokiState.authType === 'basic' ? lokiState.username : undefined,
              password: lokiState.authType === 'basic' ? lokiState.password : undefined,
              token: lokiState.authType === 'bearer' ? lokiState.token : undefined,
              skip_tls_verification: lokiState.skipTls,
            };
            const resp = await connectorsClient.createLokiConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.base_url,
              auth_type: 'NONE', description: description.trim() || undefined,
              routing_description: lokiState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'loki',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'tempo': {
            const req: CreateTempoConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: tempoState.routingDescription.trim() || undefined,
              base_url: tempoState.baseUrl.trim(),
              auth_type: tempoState.authType,
              username: tempoState.authType === 'basic' ? tempoState.username : undefined,
              password: tempoState.authType === 'basic' ? tempoState.password : undefined,
              token: tempoState.authType === 'bearer' ? tempoState.token : undefined,
              skip_tls_verification: tempoState.skipTls,
              org_id: tempoState.orgId.trim() || undefined,
            };
            const resp = await connectorsClient.createTempoConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.base_url,
              auth_type: 'NONE', description: description.trim() || undefined,
              routing_description: tempoState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'tempo',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'alertmanager': {
            const req: CreateAlertmanagerConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: alertmanagerState.routingDescription.trim() || undefined,
              base_url: alertmanagerState.baseUrl.trim(),
              auth_type: alertmanagerState.authType,
              username: alertmanagerState.authType === 'basic' ? alertmanagerState.username : undefined,
              password: alertmanagerState.authType === 'basic' ? alertmanagerState.password : undefined,
              token: alertmanagerState.authType === 'bearer' ? alertmanagerState.token : undefined,
              skip_tls_verification: alertmanagerState.skipTls,
            };
            const resp = await connectorsClient.createAlertmanagerConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.base_url,
              auth_type: 'NONE', description: description.trim() || undefined,
              routing_description: alertmanagerState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'alertmanager',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'jira': {
            const req: CreateJiraConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: jiraState.routingDescription.trim() || undefined,
              site_url: jiraState.siteUrl.trim(),
              email: jiraState.email.trim(),
              api_token: jiraState.apiToken.trim(),
            };
            const resp = await connectorsClient.createJiraConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.site_url,
              auth_type: 'BASIC', description: description.trim() || undefined,
              routing_description: jiraState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'jira',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'confluence': {
            const req: CreateConfluenceConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: confluenceState.routingDescription.trim() || undefined,
              site_url: confluenceState.siteUrl.trim(),
              email: confluenceState.email.trim(),
              api_token: confluenceState.apiToken.trim(),
            };
            const resp = await connectorsClient.createConfluenceConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: confluenceState.siteUrl.trim(),
              auth_type: 'BASIC', description: description.trim() || undefined,
              routing_description: confluenceState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'confluence',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'argocd': {
            const req: CreateArgoConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: argocdState.routingDescription.trim() || undefined,
              server_url: argocdState.serverUrl.trim(),
              api_token: argocdState.apiToken.trim(),
              skip_tls_verification: argocdState.skipTls,
            };
            const resp = await connectorsClient.createArgoConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: argocdState.serverUrl.trim(),
              auth_type: 'API_KEY', description: description.trim() || undefined,
              routing_description: argocdState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'argocd',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'github': {
            const req: CreateGitHubConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: githubState.routingDescription.trim() || undefined,
              organization: githubState.organization.trim(),
              personal_access_token: githubState.pat.trim(),
              base_url: githubState.baseUrl.trim() || 'https://api.github.com',
            };
            const resp = await connectorsClient.createGitHubConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.base_url,
              auth_type: 'API_KEY', description: description.trim() || undefined,
              routing_description: githubState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'github',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'mcp': {
            const req: CreateMCPConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              server_url: mcpState.serverUrl.trim() || undefined,
              transport_type: mcpState.transportType,
              command: mcpState.command.trim() || undefined,
              api_key: mcpState.apiKey.trim() || undefined,
            };
            const resp = await connectorsClient.createMCPConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: resp.server_url || '',
              auth_type: req.api_key ? 'API_KEY' : 'NONE',
              description: description.trim() || undefined,
              tenant_id: '', connector_type: 'mcp',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'safe',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'slack': {
            const req: CreateSlackConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              slack_bot_token: slackState.botToken.trim(),
              slack_app_token: slackState.appToken.trim() || undefined,
              slack_user_token: slackState.userToken.trim() || undefined,
            };
            const resp = await connectorsClient.createSlackConnector(req);
            return {
              id: resp.id, name: resp.name, base_url: 'https://slack.com/api',
              auth_type: 'API_KEY', description: description.trim() || undefined,
              tenant_id: '', connector_type: 'slack',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'email': {
            const req: CreateEmailConnectorRequest = {
              name: name.trim(),
              description: description.trim() || undefined,
              routing_description: emailState.routingDescription.trim() || undefined,
              provider_type: emailState.providerType,
              from_email: emailState.fromEmail.trim(),
              from_name: emailState.fromName.trim() || undefined,
              default_recipients: emailState.defaultRecipients.trim(),
              ...(emailState.providerType === 'smtp' && {
                smtp_host: emailState.smtpHost.trim(),
                smtp_port: emailState.smtpPort,
                smtp_tls: emailState.smtpTls,
                smtp_username: emailState.smtpUsername.trim() || undefined,
                smtp_password: emailState.smtpPassword || undefined,
              }),
              ...(emailState.providerType === 'sendgrid' && {
                sendgrid_api_key: emailState.sendgridApiKey,
              }),
              ...(emailState.providerType === 'mailgun' && {
                mailgun_api_key: emailState.mailgunApiKey,
                mailgun_domain: emailState.mailgunDomain.trim(),
              }),
              ...(emailState.providerType === 'ses' && {
                ses_access_key: emailState.sesAccessKey.trim(),
                ses_secret_key: emailState.sesSecretKey,
                ses_region: emailState.sesRegion,
              }),
              ...(emailState.providerType === 'generic_http' && {
                http_endpoint_url: emailState.httpEndpointUrl.trim(),
                http_auth_header: emailState.httpAuthHeader || undefined,
                http_payload_template: emailState.httpPayloadTemplate,
              }),
            };
            const resp = await connectorsClient.createEmailConnector(req);
            return {
              id: resp.id, name: resp.name,
              base_url: emailState.providerType === 'smtp'
                ? `smtp://${emailState.smtpHost.trim()}:${emailState.smtpPort}`
                : emailState.providerType,
              auth_type: 'API_KEY', description: description.trim() || undefined,
              routing_description: emailState.routingDescription.trim() || undefined,
              tenant_id: '', connector_type: 'email',
              allowed_methods: [], blocked_methods: [], default_safety_level: 'caution',
              is_active: true, automation_enabled: false,
              created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
            };
          }
          case 'soap': {
            const req: CreateConnectorRequest = {
              name: name.trim(),
              base_url: soapState.wsdlUrl.trim() ? new URL(soapState.wsdlUrl.trim()).origin : '',
              auth_type: soapState.authType === 'basic' ? 'BASIC' : soapState.authType === 'session' ? 'SESSION' : 'NONE',
              description: description.trim() || undefined,
              allowed_methods: ['POST'],
              blocked_methods: [],
              default_safety_level: 'caution',
              connector_type: 'soap',
              ...(soapState.wsdlUrl.trim() && {
                protocol_config: {
                  wsdl_url: soapState.wsdlUrl.trim(),
                  auth_type: soapState.authType,
                  timeout: soapState.timeout,
                  verify_ssl: soapState.verifySsl,
                },
              }),
            };
            return await connectorsClient.createConnector(req);
          }
          default: {
            const blockedMethods = HTTP_METHODS.filter((m) => !restState.allowedMethods.includes(m));
            const loginHeadersObj = restState.customLoginHeaders.reduce(
              (acc: Record<string, string>, header: { key: string; value: string }) => {
                if (header.key.trim() && header.value.trim()) {
                  acc[header.key.trim()] = header.value.trim();
                }
                return acc;
              },
              {} as Record<string, string>
            );
            const req: CreateConnectorRequest = {
              name: name.trim(),
              base_url: restState.baseUrl.trim(),
              auth_type: restState.authType,
              description: description.trim() || undefined,
              allowed_methods: restState.allowedMethods,
              blocked_methods: blockedMethods,
              default_safety_level: restState.defaultSafetyLevel,
              connector_type: 'rest',
              ...(restState.openapiUrl.trim() && {
                protocol_config: { openapi_url: restState.openapiUrl.trim() },
              }),
              ...(restState.authType === 'SESSION' && {
                login_url: restState.loginUrl.trim(),
                login_method: restState.loginMethod,
                login_config: {
                  login_auth_type: restState.loginAuthType,
                  ...(restState.loginAuthType === 'body' && {
                    body_template: { username: '{{username}}', password: '{{password}}' },
                  }),
                  ...(Object.keys(loginHeadersObj).length > 0 && {
                    login_headers: loginHeadersObj,
                  }),
                  token_location: restState.tokenLocation,
                  token_name: restState.tokenName.trim(),
                  ...(restState.tokenLocation === 'body' && { token_path: restState.tokenPath.trim() }),
                  ...(restState.headerName.trim() && { header_name: restState.headerName.trim() }),
                  session_duration_seconds: restState.sessionDuration,
                },
              }),
            };
            return await connectorsClient.createConnector(req);
          }
        }
      })();

      if (restState.pendingCredentials && Object.keys(restState.pendingCredentials).length > 0) {
        try {
          await connectorsClient.setUserCredentials(connector.id, restState.pendingCredentials);
        } catch (credErr: unknown) {
          console.warn('Failed to save credentials from kubeconfig:', credErr);
        }
      }

      onSuccess(connector);

    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to create connector');
    } finally {
      setSubmitting(false);
    }
  }

  function renderConnectorForm() {
    const base = { submitting };
    switch (connectorType) {
      case 'rest':
        return (
          <RestForm
            {...base}
            state={restState}
            onChange={(p) => setRestState((prev) => ({ ...prev, ...p }))}
            onApplyKubeconfig={({ name: n, baseUrl, authType }) => {
              setName(n);
              setRestState((prev) => ({ ...prev, baseUrl, authType }));
            }}
          />
        );
      case 'soap':
        return (
          <SoapForm
            {...base}
            state={soapState}
            onChange={(p) => setSoapState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'vmware':
        return (
          <VmwareForm
            {...base}
            state={vmwareState}
            onChange={(p) => setVmwareState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'proxmox':
        return (
          <ProxmoxForm
            {...base}
            state={proxmoxState}
            onChange={(p) => setProxmoxState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'kubernetes':
        return (
          <KubernetesForm
            {...base}
            state={k8sState}
            onChange={(p) => setK8sState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'gcp':
        return (
          <GcpForm
            {...base}
            state={gcpState}
            onChange={(p) => setGcpState((prev) => ({ ...prev, ...p }))}
            onAutoFill={({ name: n }) => { if (n) setName(n); }}
          />
        );
      case 'azure':
        return (
          <AzureForm
            {...base}
            state={azureState}
            onChange={(p) => setAzureState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'aws':
        return (
          <AwsForm
            {...base}
            state={awsState}
            onChange={(p) => setAwsState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'prometheus':
        return (
          <PrometheusForm
            {...base}
            state={prometheusState}
            onChange={(p) => setPrometheusState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'loki':
        return (
          <LokiForm
            {...base}
            state={lokiState}
            onChange={(p) => setLokiState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'tempo':
        return (
          <TempoForm
            {...base}
            state={tempoState}
            onChange={(p) => setTempoState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'alertmanager':
        return (
          <AlertmanagerForm
            {...base}
            state={alertmanagerState}
            onChange={(p) => setAlertmanagerState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'jira':
        return (
          <JiraForm
            {...base}
            state={jiraState}
            onChange={(p) => setJiraState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'confluence':
        return (
          <ConfluenceForm
            {...base}
            state={confluenceState}
            onChange={(p) => setConfluenceState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'argocd':
        return (
          <ArgocdForm
            {...base}
            state={argocdState}
            onChange={(p) => setArgocdState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'github':
        return (
          <GithubForm
            {...base}
            state={githubState}
            onChange={(p) => setGithubState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'mcp':
        return (
          <McpForm
            {...base}
            state={mcpState}
            onChange={(p) => setMcpState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'slack':
        return (
          <SlackForm
            {...base}
            state={slackState}
            onChange={(p) => setSlackState((prev) => ({ ...prev, ...p }))}
          />
        );
      case 'email':
        return (
          <EmailForm
            {...base}
            state={emailState}
            onChange={(p) => setEmailState((prev) => ({ ...prev, ...p }))}
          />
        );
      default:
        return null;
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        className="relative w-full max-w-2xl max-h-[90vh] overflow-y-auto glass rounded-2xl border border-white/10 shadow-2xl"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between p-6 border-b border-white/10 bg-surface/95 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <div className="p-3 bg-primary/10 rounded-xl text-primary">
              <Plug className="h-6 w-6" />
            </div>
            <div>
              <h2 className="text-xl font-bold text-white" data-testid="create-connector-modal-title">Create Connector</h2>
              <p className="text-sm text-text-secondary">Configure a new API integration</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 hover:bg-white/5 rounded-xl transition-colors text-text-secondary hover:text-white"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-6 space-y-8">
          {/* Basic Info */}
          <div className="space-y-6">
            <div className="flex items-center gap-2 text-white font-medium">
              <Globe className="h-4 w-4 text-primary" />
              <h3>Basic Information</h3>
            </div>

            <div className="grid grid-cols-1 gap-6">
              <div>
                <label htmlFor="create-connector-name" className="block text-sm font-medium text-text-secondary mb-2">
                  Name *
                </label>
                <input
                  id="create-connector-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="GitHub API"
                  disabled={submitting}
                  data-testid="connector-name-input"
                  className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                />
              </div>

              <div>
                <label htmlFor="create-connector-description" className="block text-sm font-medium text-text-secondary mb-2">
                  Description
                </label>
                <textarea
                  id="create-connector-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="GitHub REST API for repository management"
                  rows={2}
                  disabled={submitting}
                  className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all resize-none"
                />
              </div>
            </div>
          </div>

          {/* Protocol Type */}
          <div className="space-y-6">
            <div className="flex items-center gap-2 text-white font-medium">
              <FileCode className="h-4 w-4 text-primary" />
              <h3>Protocol Type</h3>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
              {[
                { id: 'rest', label: 'REST', desc: 'OpenAPI/REST APIs' },
                { id: 'soap', label: 'SOAP', desc: 'WSDL/SOAP services' },
                { id: 'vmware', label: 'VMware', desc: 'vSphere/vCenter' },
                { id: 'proxmox', label: 'Proxmox', desc: 'Proxmox VE' },
                { id: 'kubernetes', label: 'K8s', desc: 'Kubernetes' },
                { id: 'gcp', label: 'GCP', desc: 'Google Cloud' },
                { id: 'azure', label: 'Azure', desc: 'Microsoft Azure' },
                { id: 'aws', label: 'AWS', desc: 'Amazon Web Services' },
                { id: 'prometheus', label: 'Prometheus', desc: 'Metrics' },
                { id: 'loki', label: 'Loki', desc: 'Logs' },
                { id: 'tempo', label: 'Tempo', desc: 'Traces' },
                { id: 'alertmanager', label: 'Alertmanager', desc: 'Alerts' },
                { id: 'jira', label: 'Jira', desc: 'Issues' },
                { id: 'confluence', label: 'Confluence', desc: 'Wiki' },
                { id: 'email', label: 'Email', desc: 'Notifications' },
                { id: 'argocd', label: 'ArgoCD', desc: 'GitOps' },
                { id: 'github', label: 'GitHub', desc: 'CI/CD' },
                { id: 'mcp', label: 'MCP', desc: 'AI Tool Server' },
                { id: 'slack', label: 'Slack', desc: 'Messaging' },
                { id: 'graphql', label: 'GraphQL', desc: 'Coming soon', disabled: true },
                { id: 'grpc', label: 'gRPC', desc: 'Coming soon', disabled: true },
              ].map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => !option.disabled && setConnectorType(option.id as ConnectorType)}
                  disabled={option.disabled}
                  className={clsx(
                    "flex flex-col items-center gap-1 p-4 rounded-xl text-sm font-medium transition-all border",
                    option.disabled
                      ? "bg-surface/50 border-white/5 text-text-tertiary cursor-not-allowed opacity-50"
                      : connectorType === option.id
                        ? "bg-primary/10 border-primary/50 text-white"
                        : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                  )}
                >
                  <span className="font-semibold">{option.label}</span>
                  <span className="text-xs opacity-70">{option.desc}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Connector-specific form */}
          <div className="space-y-8">
            {renderConnectorForm()}
          </div>

          {/* Error */}
          {error && (
            <div className="flex items-center gap-2 p-4 bg-red-500/10 text-red-400 rounded-xl border border-red-500/20">
              <AlertCircle className="h-5 w-5" />
              <span>{error}</span>
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-3 pt-6 border-t border-white/10">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="flex-1 px-6 py-3 bg-surface hover:bg-surface-hover border border-white/10 rounded-xl text-text-secondary hover:text-white font-medium transition-all disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!isFormValid() || submitting}
              data-testid="create-connector-submit-button"
              className="flex-1 flex items-center justify-center gap-2 px-6 py-3 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98] text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? (
                <>
                  <Loader2 className="h-5 w-5 animate-spin" />
                  Creating...
                </>
              ) : (
                <>
                  <CheckCircle className="h-5 w-5" />
                  Create Connector
                </>
              )}
            </button>
          </div>
        </form>
      </motion.div>
    </div>
  );
}
