// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Create Connector Modal
 * 
 * Form to create a new API connector with safety policies
 */
import { useState, useCallback } from 'react';
import { X, Plug, Loader2, CheckCircle, AlertCircle, Shield, ShieldAlert, ShieldCheck, Globe, Key, Lock, FileCode, Server, ChevronDown, ChevronRight, Upload, Mail } from 'lucide-react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import type { Connector, CreateConnectorRequest, CreateVMwareConnectorRequest, CreateProxmoxConnectorRequest, CreateKubernetesConnectorRequest, CreateGCPConnectorRequest, CreateAzureConnectorRequest, CreateAWSConnectorRequest, CreatePrometheusConnectorRequest, CreateLokiConnectorRequest, CreateTempoConnectorRequest, CreateAlertmanagerConnectorRequest, CreateJiraConnectorRequest, CreateConfluenceConnectorRequest, CreateEmailConnectorRequest, CreateArgoConnectorRequest, CreateGitHubConnectorRequest, CreateMCPConnectorRequest, CreateSlackConnectorRequest, ConnectorType } from '../../lib/api-client';
import type { EmailProviderType } from '../../api/types/connector';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { parseKubeconfig, getKubeconfigContexts, getCurrentContext, type KubeConnectionInfo } from '../../lib/kubeconfig';

interface CreateConnectorModalProps {
  onClose: () => void;
  onSuccess: (connector: Connector) => void;
}

const HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

export function CreateConnectorModal({ onClose, onSuccess }: CreateConnectorModalProps) { // NOSONAR (cognitive complexity)
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [authType, setAuthType] = useState<'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION'>('API_KEY');
  const [description, setDescription] = useState('');
  const [allowedMethods, setAllowedMethods] = useState<string[]>(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
  const [defaultSafetyLevel, setDefaultSafetyLevel] = useState<'safe' | 'caution' | 'dangerous'>('safe');

  // Connector type (REST, SOAP, GraphQL, gRPC, VMware, Kubernetes)
  const [connectorType, setConnectorType] = useState<ConnectorType>('rest');

  // REST-specific configuration
  const [openapiUrl, setOpenapiUrl] = useState('');

  // Kubeconfig import
  const [showKubeconfigImport, setShowKubeconfigImport] = useState(false);
  const [kubeconfigText, setKubeconfigText] = useState('');
  const [kubeconfigContexts, setKubeconfigContexts] = useState<string[]>([]);
  const [selectedKubeContext, setSelectedKubeContext] = useState('');
  const [kubeconfigInfo, setKubeconfigInfo] = useState<KubeConnectionInfo | null>(null);
  const [kubeconfigError, setKubeconfigError] = useState<string | null>(null);
  const [pendingCredentials, setPendingCredentials] = useState<{ access_token?: string; username?: string; password?: string } | null>(null);

  // SOAP-specific configuration
  const [wsdlUrl, setWsdlUrl] = useState('');
  const [soapAuthType, setSoapAuthType] = useState<'none' | 'basic' | 'session'>('none');
  const [soapTimeout, setSoapTimeout] = useState(30);
  const [soapVerifySsl, setSoapVerifySsl] = useState(true);

  // VMware-specific configuration (TASK-97)
  const [vcenterHost, setVcenterHost] = useState('');
  const [vcenterPort, setVcenterPort] = useState(443);
  const [vcenterDisableSsl, setVcenterDisableSsl] = useState(false);
  const [vcenterUsername, setVcenterUsername] = useState('');
  const [vcenterPassword, setVcenterPassword] = useState('');

  // Proxmox-specific configuration (TASK-100)
  const [proxmoxHost, setProxmoxHost] = useState('');
  const [proxmoxPort, setProxmoxPort] = useState(8006);
  const [proxmoxDisableSsl, setProxmoxDisableSsl] = useState(false);
  const [proxmoxAuthType, setProxmoxAuthType] = useState<'token' | 'password'>('password');
  const [proxmoxUsername, setProxmoxUsername] = useState('');
  const [proxmoxPassword, setProxmoxPassword] = useState('');
  const [proxmoxTokenId, setProxmoxTokenId] = useState('');
  const [proxmoxTokenSecret, setProxmoxTokenSecret] = useState('');

  // Kubernetes-specific configuration (TASK-107)
  const [k8sServerUrl, setK8sServerUrl] = useState('');
  const [k8sToken, setK8sToken] = useState('');
  const [k8sSkipTls, setK8sSkipTls] = useState(false);
  const [k8sRoutingDescription, setK8sRoutingDescription] = useState('');

  // GCP-specific configuration (TASK-102)
  const [gcpProjectId, setGcpProjectId] = useState('');
  const [gcpDefaultRegion, setGcpDefaultRegion] = useState('us-central1');
  const [gcpDefaultZone, setGcpDefaultZone] = useState('us-central1-a');
  const [gcpServiceAccountJson, setGcpServiceAccountJson] = useState('');

  // Azure-specific configuration (Phase 92)
  const [azureTenantId, setAzureTenantId] = useState('');
  const [azureClientId, setAzureClientId] = useState('');
  const [azureClientSecret, setAzureClientSecret] = useState('');
  const [azureSubscriptionId, setAzureSubscriptionId] = useState('');
  const [azureResourceGroupFilter, setAzureResourceGroupFilter] = useState('');

  // AWS-specific configuration (Phase 91)
  const [awsAccessKeyId, setAwsAccessKeyId] = useState('');
  const [awsSecretAccessKey, setAwsSecretAccessKey] = useState('');
  const [awsDefaultRegion, setAwsDefaultRegion] = useState('us-east-1');

  // Prometheus-specific configuration
  const [prometheusBaseUrl, setPrometheusBaseUrl] = useState('');
  const [prometheusAuthType, setPrometheusAuthType] = useState<'none' | 'basic' | 'bearer'>('none');
  const [prometheusUsername, setPrometheusUsername] = useState('');
  const [prometheusPassword, setPrometheusPassword] = useState('');
  const [prometheusToken, setPrometheusToken] = useState('');
  const [prometheusSkipTls, setPrometheusSkipTls] = useState(false);
  const [prometheusRoutingDescription, setPrometheusRoutingDescription] = useState('');

  // Loki-specific configuration
  const [lokiBaseUrl, setLokiBaseUrl] = useState('');
  const [lokiAuthType, setLokiAuthType] = useState<'none' | 'basic' | 'bearer'>('none');
  const [lokiUsername, setLokiUsername] = useState('');
  const [lokiPassword, setLokiPassword] = useState('');
  const [lokiToken, setLokiToken] = useState('');
  const [lokiSkipTls, setLokiSkipTls] = useState(false);
  const [lokiRoutingDescription, setLokiRoutingDescription] = useState('');

  // Tempo-specific configuration
  const [tempoBaseUrl, setTempoBaseUrl] = useState('');
  const [tempoAuthType, setTempoAuthType] = useState<'none' | 'basic' | 'bearer'>('none');
  const [tempoUsername, setTempoUsername] = useState('');
  const [tempoPassword, setTempoPassword] = useState('');
  const [tempoToken, setTempoToken] = useState('');
  const [tempoSkipTls, setTempoSkipTls] = useState(false);
  const [tempoRoutingDescription, setTempoRoutingDescription] = useState('');
  const [tempoOrgId, setTempoOrgId] = useState('');

  // Alertmanager-specific configuration
  const [alertmanagerBaseUrl, setAlertmanagerBaseUrl] = useState('');
  const [alertmanagerAuthType, setAlertmanagerAuthType] = useState<'none' | 'basic' | 'bearer'>('none');
  const [alertmanagerUsername, setAlertmanagerUsername] = useState('');
  const [alertmanagerPassword, setAlertmanagerPassword] = useState('');
  const [alertmanagerToken, setAlertmanagerToken] = useState('');
  const [alertmanagerSkipTls, setAlertmanagerSkipTls] = useState(false);
  const [alertmanagerRoutingDescription, setAlertmanagerRoutingDescription] = useState('');

  // Jira-specific configuration
  const [jiraSiteUrl, setJiraSiteUrl] = useState('');
  const [jiraEmail, setJiraEmail] = useState('');
  const [jiraApiToken, setJiraApiToken] = useState('');
  const [jiraRoutingDescription, setJiraRoutingDescription] = useState('');

  // Confluence-specific configuration
  const [confluenceSiteUrl, setConfluenceSiteUrl] = useState('');
  const [confluenceEmail, setConfluenceEmail] = useState('');
  const [confluenceApiToken, setConfluenceApiToken] = useState('');
  const [confluenceRoutingDescription, setConfluenceRoutingDescription] = useState('');

  // ArgoCD-specific configuration
  const [argoServerUrl, setArgoServerUrl] = useState('');
  const [argoApiToken, setArgoApiToken] = useState('');
  const [argoSkipTls, setArgoSkipTls] = useState(false);
  const [argoRoutingDescription, setArgoRoutingDescription] = useState('');

  // GitHub-specific configuration
  const [githubOrganization, setGithubOrganization] = useState('');
  const [githubPat, setGithubPat] = useState('');
  const [githubBaseUrl, setGithubBaseUrl] = useState('https://api.github.com');
  const [githubRoutingDescription, setGithubRoutingDescription] = useState('');

  // MCP-specific configuration (Phase 93)
  const [mcpServerUrl, setMcpServerUrl] = useState('');
  const [mcpTransportType, setMcpTransportType] = useState<'streamable_http' | 'stdio'>('streamable_http');
  const [mcpCommand, setMcpCommand] = useState('');
  const [mcpApiKey, setMcpApiKey] = useState('');

  // Slack-specific configuration (Phase 94.1)
  const [slackBotToken, setSlackBotToken] = useState('');
  const [slackAppToken, setSlackAppToken] = useState('');
  const [slackUserToken, setSlackUserToken] = useState('');

  // Email-specific configuration (Phase 44)
  const [emailFromEmail, setEmailFromEmail] = useState('');
  const [emailFromName, setEmailFromName] = useState('MEHO');
  const [emailDefaultRecipients, setEmailDefaultRecipients] = useState('');
  const [emailRoutingDescription, setEmailRoutingDescription] = useState('');
  const [emailProviderType, setEmailProviderType] = useState<EmailProviderType>('smtp');
  // SMTP
  const [emailSmtpHost, setEmailSmtpHost] = useState('');
  const [emailSmtpPort, setEmailSmtpPort] = useState(587);
  const [emailSmtpTls, setEmailSmtpTls] = useState(true);
  const [emailSmtpUsername, setEmailSmtpUsername] = useState('');
  const [emailSmtpPassword, setEmailSmtpPassword] = useState('');
  // SendGrid
  const [emailSendgridApiKey, setEmailSendgridApiKey] = useState('');
  // Mailgun
  const [emailMailgunApiKey, setEmailMailgunApiKey] = useState('');
  const [emailMailgunDomain, setEmailMailgunDomain] = useState('');
  // SES
  const [emailSesAccessKey, setEmailSesAccessKey] = useState('');
  const [emailSesSecretKey, setEmailSesSecretKey] = useState('');
  const [emailSesRegion, setEmailSesRegion] = useState('us-east-1');
  // Generic HTTP
  const [emailHttpEndpointUrl, setEmailHttpEndpointUrl] = useState('');
  const [emailHttpAuthHeader, setEmailHttpAuthHeader] = useState('');
  const [emailHttpPayloadTemplate, setEmailHttpPayloadTemplate] = useState('');

  // SESSION auth fields
  const [loginUrl, setLoginUrl] = useState('/api/v1/auth/login');
  const [loginMethod, setLoginMethod] = useState<'POST' | 'GET'>('POST');
  const [loginAuthType, setLoginAuthType] = useState<'body' | 'basic'>('body');
  const [customLoginHeaders, setCustomLoginHeaders] = useState<Array<{ key: string; value: string }>>([]);
  const [tokenLocation, setTokenLocation] = useState<'header' | 'cookie' | 'body'>('header');
  const [tokenName, setTokenName] = useState('X-Auth-Token');
  const [tokenPath, setTokenPath] = useState('$.token');
  const [headerName, setHeaderName] = useState('');
  const [sessionDuration, setSessionDuration] = useState(3600);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apiClient = getAPIClient(config.apiURL);

  const handleMethodToggle = useCallback((method: string) => {
    if (allowedMethods.includes(method)) {
      setAllowedMethods(allowedMethods.filter(m => m !== method));
    } else {
      setAllowedMethods([...allowedMethods, method]);
    }
  }, [allowedMethods]);

  const handleAddHeader = useCallback(() => {
    setCustomLoginHeaders([...customLoginHeaders, { key: '', value: '' }]);
  }, [customLoginHeaders]);

  const handleRemoveHeader = useCallback((index: number) => {
    setCustomLoginHeaders(customLoginHeaders.filter((_, i) => i !== index));
  }, [customLoginHeaders]);

  const handleHeaderChange = useCallback((index: number, field: 'key' | 'value', value: string) => {
    const updated = [...customLoginHeaders];
    updated[index] = { ...updated[index], [field]: value };
    setCustomLoginHeaders(updated);
  }, [customLoginHeaders]);

  // Parse kubeconfig and apply to form
  const parseAndApplyKubeconfig = useCallback((text: string, context: string) => {
    const result = parseKubeconfig(text, context);

    if (!result.success || !result.info) {
      setKubeconfigError(result.error || 'Failed to parse kubeconfig');
      setKubeconfigInfo(null);
      return;
    }

    const info = result.info;
    setKubeconfigInfo(info);
    setKubeconfigError(null);

    // Apply to form fields
    setName(info.name);
    setBaseUrl(info.server);
    setOpenapiUrl(info.openapiUrl);

    // Set auth type based on detected auth
    if (info.authType === 'token' && info.token) {
      setAuthType('OAUTH2');
      setPendingCredentials({ access_token: info.token });
    } else if (info.authType === 'basic' && info.username && info.password) {
      setAuthType('BASIC');
      setPendingCredentials({ username: info.username, password: info.password });
    } else {
      // For exec/client-cert/unknown, default to OAUTH2 (user will paste token manually)
      setAuthType('OAUTH2');
      setPendingCredentials(null);
    }
  }, []);

  // Handle kubeconfig text change - extract contexts
  const handleKubeconfigChange = useCallback((text: string) => {
    setKubeconfigText(text);
    setKubeconfigError(null);
    setKubeconfigInfo(null);
    setPendingCredentials(null);

    if (!text.trim()) {
      setKubeconfigContexts([]);
      setSelectedKubeContext('');
      return;
    }

    // Get available contexts
    const contexts = getKubeconfigContexts(text);
    setKubeconfigContexts(contexts);

    // Auto-select current context
    const currentCtx = getCurrentContext(text);
    if (currentCtx && contexts.includes(currentCtx)) {
      setSelectedKubeContext(currentCtx);
      // Parse with current context
      parseAndApplyKubeconfig(text, currentCtx);
    } else if (contexts.length > 0) {
      setSelectedKubeContext(contexts[0]);
      parseAndApplyKubeconfig(text, contexts[0]);
    }
  }, [parseAndApplyKubeconfig]);

  // Handle context selection change
  const handleKubeContextChange = useCallback((context: string) => {
    setSelectedKubeContext(context);
    if (kubeconfigText && context) {
      parseAndApplyKubeconfig(kubeconfigText, context);
    }
  }, [kubeconfigText, parseAndApplyKubeconfig]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => { // NOSONAR (cognitive complexity)
    e.preventDefault();

    if (!name.trim()) {
      setError('Name is required');
      return;
    }

    if (connectorType === 'rest' && !baseUrl.trim()) {
      setError('Base URL is required');
      return;
    }

    // REST-specific validation
    if (connectorType === 'rest' && allowedMethods.length === 0) {
      setError('At least one HTTP method must be allowed');
      return;
    }

    // SOAP-specific validation
    if (connectorType === 'soap' && !wsdlUrl.trim()) {
      setError('WSDL URL is required for SOAP connectors');
      return;
    }

    // VMware-specific validation (TASK-97)
    if (connectorType === 'vmware') {
      if (!vcenterHost.trim()) {
        setError('vCenter host is required');
        return;
      }
      if (!vcenterUsername.trim()) {
        setError('vCenter username is required');
        return;
      }
      if (!vcenterPassword.trim()) {
        setError('vCenter password is required');
        return;
      }
    }

    // Proxmox-specific validation (TASK-100)
    if (connectorType === 'proxmox') {
      if (!proxmoxHost.trim()) {
        setError('Proxmox host is required');
        return;
      }
      if (proxmoxAuthType === 'password') {
        if (!proxmoxUsername.trim()) {
          setError('Proxmox username is required');
          return;
        }
        if (!proxmoxPassword.trim()) {
          setError('Proxmox password is required');
          return;
        }
      } else {
        if (!proxmoxTokenId.trim()) {
          setError('Proxmox API Token ID is required');
          return;
        }
        if (!proxmoxTokenSecret.trim()) {
          setError('Proxmox API Token Secret is required');
          return;
        }
      }
    }

    // Kubernetes-specific validation (TASK-107)
    if (connectorType === 'kubernetes') {
      if (!k8sServerUrl.trim()) {
        setError('Kubernetes API server URL is required');
        return;
      }
      if (!k8sToken.trim()) {
        setError('Service Account token is required');
        return;
      }
    }

    // GCP-specific validation (TASK-102)
    if (connectorType === 'gcp') {
      if (!gcpProjectId.trim()) {
        setError('GCP Project ID is required');
        return;
      }
      if (!gcpServiceAccountJson.trim()) {
        setError('Service Account JSON is required');
        return;
      }
      // Validate JSON format
      try {
        JSON.parse(gcpServiceAccountJson);
      } catch {
        setError('Invalid Service Account JSON format');
        return;
      }
    }

    // Azure-specific validation (Phase 92)
    if (connectorType === 'azure') {
      if (!azureTenantId.trim()) {
        setError('Azure Tenant ID is required');
        return;
      }
      if (!azureClientId.trim()) {
        setError('Azure Client ID is required');
        return;
      }
      if (!azureClientSecret.trim()) {
        setError('Azure Client Secret is required');
        return;
      }
      if (!azureSubscriptionId.trim()) {
        setError('Azure Subscription ID is required');
        return;
      }
    }

    // Prometheus-specific validation
    if (connectorType === 'prometheus') {
      if (!prometheusBaseUrl.trim()) {
        setError('Prometheus base URL is required');
        return;
      }
      if (prometheusAuthType === 'basic') {
        if (!prometheusUsername.trim() || !prometheusPassword.trim()) {
          setError('Username and password are required for basic auth');
          return;
        }
      }
      if (prometheusAuthType === 'bearer') {
        if (!prometheusToken.trim()) {
          setError('Bearer token is required for bearer auth');
          return;
        }
      }
    }

    // Loki-specific validation
    if (connectorType === 'loki') {
      if (!lokiBaseUrl.trim()) {
        setError('Loki base URL is required');
        return;
      }
      if (lokiAuthType === 'basic') {
        if (!lokiUsername.trim() || !lokiPassword.trim()) {
          setError('Username and password are required for basic auth');
          return;
        }
      }
      if (lokiAuthType === 'bearer') {
        if (!lokiToken.trim()) {
          setError('Bearer token is required for bearer auth');
          return;
        }
      }
    }

    // Tempo-specific validation
    if (connectorType === 'tempo') {
      if (!tempoBaseUrl.trim()) {
        setError('Tempo base URL is required');
        return;
      }
      if (tempoAuthType === 'basic') {
        if (!tempoUsername.trim() || !tempoPassword.trim()) {
          setError('Username and password are required for basic auth');
          return;
        }
      }
      if (tempoAuthType === 'bearer') {
        if (!tempoToken.trim()) {
          setError('Bearer token is required for bearer auth');
          return;
        }
      }
    }

    // Alertmanager-specific validation
    if (connectorType === 'alertmanager') {
      if (!alertmanagerBaseUrl.trim()) {
        setError('Alertmanager base URL is required');
        return;
      }
      if (alertmanagerAuthType === 'basic') {
        if (!alertmanagerUsername.trim() || !alertmanagerPassword.trim()) {
          setError('Username and password are required for basic auth');
          return;
        }
      }
      if (alertmanagerAuthType === 'bearer') {
        if (!alertmanagerToken.trim()) {
          setError('Bearer token is required for bearer auth');
          return;
        }
      }
    }

    // Jira-specific validation
    if (connectorType === 'jira') {
      if (!jiraSiteUrl.trim()) {
        setError('Jira site URL is required');
        return;
      }
      if (!jiraEmail.trim()) {
        setError('Atlassian account email is required');
        return;
      }
      if (!jiraApiToken.trim()) {
        setError('Atlassian API token is required');
        return;
      }
    }

    // Confluence-specific validation
    if (connectorType === 'confluence') {
      if (!confluenceSiteUrl.trim()) {
        setError('Confluence site URL is required');
        return;
      }
      if (!confluenceEmail.trim()) {
        setError('Atlassian account email is required');
        return;
      }
      if (!confluenceApiToken.trim()) {
        setError('Atlassian API token is required');
        return;
      }
    }

    // ArgoCD-specific validation
    if (connectorType === 'argocd') {
      if (!argoServerUrl.trim()) {
        setError('ArgoCD server URL is required');
        return;
      }
      if (!argoApiToken.trim()) {
        setError('ArgoCD API token is required');
        return;
      }
    }

    // GitHub-specific validation
    if (connectorType === 'github') {
      if (!githubOrganization.trim()) {
        setError('GitHub organization is required');
        return;
      }
      if (!githubPat.trim()) {
        setError('GitHub Personal Access Token is required');
        return;
      }
    }

    // MCP-specific validation (Phase 93)
    if (connectorType === 'mcp') {
      if (mcpTransportType === 'streamable_http' && !mcpServerUrl.trim()) {
        setError('MCP server URL is required for Streamable HTTP transport');
        return;
      }
      if (mcpTransportType === 'stdio' && !mcpCommand.trim()) {
        setError('Command is required for stdio transport');
        return;
      }
    }

    // Slack-specific validation (Phase 94.1)
    if (connectorType === 'slack') {
      if (!slackBotToken.trim()) {
        setError('Slack Bot Token (xoxb-*) is required');
        return;
      }
    }

    // Email-specific validation (Phase 44)
    if (connectorType === 'email') {
      if (!emailFromEmail.trim()) {
        setError('From email address is required');
        return;
      }
      if (!emailDefaultRecipients.trim()) {
        setError('Default recipients are required');
        return;
      }
      // Provider-specific required fields
      if (emailProviderType === 'smtp') {
        if (!emailSmtpHost.trim()) {
          setError('SMTP host is required');
          return;
        }
      }
      if (emailProviderType === 'sendgrid') {
        if (!emailSendgridApiKey.trim()) {
          setError('SendGrid API key is required');
          return;
        }
      }
      if (emailProviderType === 'mailgun') {
        if (!emailMailgunApiKey.trim()) {
          setError('Mailgun API key is required');
          return;
        }
        if (!emailMailgunDomain.trim()) {
          setError('Mailgun domain is required');
          return;
        }
      }
      if (emailProviderType === 'ses') {
        if (!emailSesAccessKey.trim()) {
          setError('AWS Access Key ID is required');
          return;
        }
        if (!emailSesSecretKey.trim()) {
          setError('AWS Secret Access Key is required');
          return;
        }
      }
      if (emailProviderType === 'generic_http') {
        if (!emailHttpEndpointUrl.trim()) {
          setError('HTTP endpoint URL is required');
          return;
        }
        if (!emailHttpPayloadTemplate.trim()) {
          setError('Payload template is required');
          return;
        }
      }
    }

    setSubmitting(true);
    setError(null);

    try {
      // TASK-97: VMware connector uses separate endpoint
      if (connectorType === 'vmware') {
        // Clean up vcenter_host - strip protocol prefix if user included it
        let cleanedHost = vcenterHost.trim();
        if (cleanedHost.startsWith('https://')) {
          cleanedHost = cleanedHost.slice(8);
        } else if (cleanedHost.startsWith('http://')) {
          cleanedHost = cleanedHost.slice(7);
        }
        cleanedHost = cleanedHost.replace(/\/+$/, ''); // Remove trailing slashes

        const vmwareRequest: CreateVMwareConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          vcenter_host: cleanedHost,
          port: vcenterPort,
          disable_ssl_verification: vcenterDisableSsl,
          username: vcenterUsername.trim(),
          password: vcenterPassword,
        };

        const vmwareResponse = await apiClient.createVMwareConnector(vmwareRequest);
        
        // Convert VMware response to Connector for onSuccess callback
        const connector: Connector = {
          id: vmwareResponse.id,
          name: vmwareResponse.name,
          base_url: `https://${vmwareResponse.vcenter_host}`,
          auth_type: 'SESSION',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'vmware',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // TASK-100: Proxmox connector uses separate endpoint
      if (connectorType === 'proxmox') {
        // Clean up proxmox host - strip protocol prefix if user included it
        let cleanedHost = proxmoxHost.trim();
        if (cleanedHost.startsWith('https://')) {
          cleanedHost = cleanedHost.slice(8);
        } else if (cleanedHost.startsWith('http://')) {
          cleanedHost = cleanedHost.slice(7);
        }
        cleanedHost = cleanedHost.replace(/\/+$/, ''); // Remove trailing slashes

        const proxmoxRequest: CreateProxmoxConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          host: cleanedHost,
          port: proxmoxPort,
          disable_ssl_verification: proxmoxDisableSsl,
          ...(proxmoxAuthType === 'token' ? {
            api_token_id: proxmoxTokenId.trim(),
            api_token_secret: proxmoxTokenSecret,
          } : {
            username: proxmoxUsername.trim(),
            password: proxmoxPassword,
          }),
        };

        const proxmoxResponse = await apiClient.createProxmoxConnector(proxmoxRequest);
        
        // Convert Proxmox response to Connector for onSuccess callback
        const connector: Connector = {
          id: proxmoxResponse.id,
          name: proxmoxResponse.name,
          base_url: `https://${proxmoxResponse.host}:${proxmoxPort}`,
          auth_type: proxmoxAuthType === 'token' ? 'API_KEY' : 'BASIC',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'proxmox',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // TASK-107: Kubernetes connector uses separate endpoint
      if (connectorType === 'kubernetes') {
        const k8sRequest: CreateKubernetesConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: k8sRoutingDescription.trim() || undefined,
          server_url: k8sServerUrl.trim(),
          token: k8sToken,
          skip_tls_verification: k8sSkipTls,
        };

        const k8sResponse = await apiClient.createKubernetesConnector(k8sRequest);
        
        // Convert Kubernetes response to Connector for onSuccess callback
        const connector: Connector = {
          id: k8sResponse.id,
          name: k8sResponse.name,
          base_url: k8sResponse.server_url,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          routing_description: k8sRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'kubernetes',
          allowed_methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // TASK-102: GCP connector uses separate endpoint
      if (connectorType === 'gcp') {
        const gcpRequest: CreateGCPConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          project_id: gcpProjectId.trim(),
          default_region: gcpDefaultRegion,
          default_zone: gcpDefaultZone,
          service_account_json: gcpServiceAccountJson,
        };

        const gcpResponse = await apiClient.createGCPConnector(gcpRequest);
        
        // Convert GCP response to Connector for onSuccess callback
        const connector: Connector = {
          id: gcpResponse.id,
          name: gcpResponse.name,
          base_url: `https://console.cloud.google.com/home/dashboard?project=${gcpResponse.project_id}`,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'gcp',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Phase 92: Azure connector uses separate endpoint
      if (connectorType === 'azure') {
        const azureRequest: CreateAzureConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          tenant_id: azureTenantId.trim(),
          client_id: azureClientId.trim(),
          client_secret: azureClientSecret.trim(),
          subscription_id: azureSubscriptionId.trim(),
          resource_group_filter: azureResourceGroupFilter.trim() || undefined,
        };

        const azureResponse = await apiClient.createAzureConnector(azureRequest);

        // Convert Azure response to Connector for onSuccess callback
        const connector: Connector = {
          id: azureResponse.id,
          name: azureResponse.name,
          base_url: `https://portal.azure.com`,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'azure',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Phase 91: AWS connector uses separate endpoint
      if (connectorType === 'aws') {
        const awsRequest: CreateAWSConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          default_region: awsDefaultRegion,
          aws_access_key_id: awsAccessKeyId.trim() || undefined,
          aws_secret_access_key: awsSecretAccessKey.trim() || undefined,
        };

        const awsResponse = await apiClient.createAWSConnector(awsRequest);

        // Convert AWS response to Connector for onSuccess callback
        const connector: Connector = {
          id: awsResponse.id,
          name: awsResponse.name,
          base_url: `https://${awsDefaultRegion}.console.aws.amazon.com`,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'aws',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Prometheus connector uses separate endpoint
      if (connectorType === 'prometheus') {
        const promRequest: CreatePrometheusConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: prometheusRoutingDescription.trim() || undefined,
          base_url: prometheusBaseUrl.trim(),
          auth_type: prometheusAuthType,
          username: prometheusAuthType === 'basic' ? prometheusUsername : undefined,
          password: prometheusAuthType === 'basic' ? prometheusPassword : undefined,
          token: prometheusAuthType === 'bearer' ? prometheusToken : undefined,
          skip_tls_verification: prometheusSkipTls,
        };

        const promResponse = await apiClient.createPrometheusConnector(promRequest);

        // Convert Prometheus response to Connector for onSuccess callback
        const connector: Connector = {
          id: promResponse.id,
          name: promResponse.name,
          base_url: promResponse.base_url,
          auth_type: 'NONE',
          description: description.trim() || undefined,
          routing_description: prometheusRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'prometheus',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Loki connector uses separate endpoint
      if (connectorType === 'loki') {
        const lokiRequest: CreateLokiConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: lokiRoutingDescription.trim() || undefined,
          base_url: lokiBaseUrl.trim(),
          auth_type: lokiAuthType,
          username: lokiAuthType === 'basic' ? lokiUsername : undefined,
          password: lokiAuthType === 'basic' ? lokiPassword : undefined,
          token: lokiAuthType === 'bearer' ? lokiToken : undefined,
          skip_tls_verification: lokiSkipTls,
        };

        const lokiResponse = await apiClient.createLokiConnector(lokiRequest);

        // Convert Loki response to Connector for onSuccess callback
        const connector: Connector = {
          id: lokiResponse.id,
          name: lokiResponse.name,
          base_url: lokiResponse.base_url,
          auth_type: 'NONE',
          description: description.trim() || undefined,
          routing_description: lokiRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'loki',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Tempo connector uses separate endpoint
      if (connectorType === 'tempo') {
        const tempoRequest: CreateTempoConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: tempoRoutingDescription.trim() || undefined,
          base_url: tempoBaseUrl.trim(),
          auth_type: tempoAuthType,
          username: tempoAuthType === 'basic' ? tempoUsername : undefined,
          password: tempoAuthType === 'basic' ? tempoPassword : undefined,
          token: tempoAuthType === 'bearer' ? tempoToken : undefined,
          skip_tls_verification: tempoSkipTls,
          org_id: tempoOrgId.trim() || undefined,
        };

        const tempoResponse = await apiClient.createTempoConnector(tempoRequest);

        // Convert Tempo response to Connector for onSuccess callback
        const connector: Connector = {
          id: tempoResponse.id,
          name: tempoResponse.name,
          base_url: tempoResponse.base_url,
          auth_type: 'NONE',
          description: description.trim() || undefined,
          routing_description: tempoRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'tempo',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Alertmanager connector uses separate endpoint
      if (connectorType === 'alertmanager') {
        const alertmanagerRequest: CreateAlertmanagerConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: alertmanagerRoutingDescription.trim() || undefined,
          base_url: alertmanagerBaseUrl.trim(),
          auth_type: alertmanagerAuthType,
          username: alertmanagerAuthType === 'basic' ? alertmanagerUsername : undefined,
          password: alertmanagerAuthType === 'basic' ? alertmanagerPassword : undefined,
          token: alertmanagerAuthType === 'bearer' ? alertmanagerToken : undefined,
          skip_tls_verification: alertmanagerSkipTls,
        };

        const alertmanagerResponse = await apiClient.createAlertmanagerConnector(alertmanagerRequest);

        // Convert Alertmanager response to Connector for onSuccess callback
        const connector: Connector = {
          id: alertmanagerResponse.id,
          name: alertmanagerResponse.name,
          base_url: alertmanagerResponse.base_url,
          auth_type: 'NONE',
          description: description.trim() || undefined,
          routing_description: alertmanagerRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'alertmanager',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Jira connector uses separate endpoint
      if (connectorType === 'jira') {
        const jiraRequest: CreateJiraConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: jiraRoutingDescription.trim() || undefined,
          site_url: jiraSiteUrl.trim(),
          email: jiraEmail.trim(),
          api_token: jiraApiToken.trim(),
        };

        const jiraResponse = await apiClient.createJiraConnector(jiraRequest);

        // Convert Jira response to Connector for onSuccess callback
        const connector: Connector = {
          id: jiraResponse.id,
          name: jiraResponse.name,
          base_url: jiraResponse.site_url,
          auth_type: 'BASIC',
          description: description.trim() || undefined,
          routing_description: jiraRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'jira',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Confluence connector uses separate endpoint
      if (connectorType === 'confluence') {
        const confluenceRequest: CreateConfluenceConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: confluenceRoutingDescription.trim() || undefined,
          site_url: confluenceSiteUrl.trim(),
          email: confluenceEmail.trim(),
          api_token: confluenceApiToken.trim(),
        };

        const confluenceResponse = await apiClient.createConfluenceConnector(confluenceRequest);

        // Convert Confluence response to Connector for onSuccess callback
        const connector: Connector = {
          id: confluenceResponse.id,
          name: confluenceResponse.name,
          base_url: confluenceSiteUrl.trim(),
          auth_type: 'BASIC',
          description: description.trim() || undefined,
          routing_description: confluenceRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'confluence',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // ArgoCD connector uses separate endpoint
      if (connectorType === 'argocd') {
        const argoRequest: CreateArgoConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: argoRoutingDescription.trim() || undefined,
          server_url: argoServerUrl.trim(),
          api_token: argoApiToken.trim(),
          skip_tls_verification: argoSkipTls,
        };

        const argoResponse = await apiClient.createArgoConnector(argoRequest);

        const connector: Connector = {
          id: argoResponse.id,
          name: argoResponse.name,
          base_url: argoServerUrl.trim(),
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          routing_description: argoRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'argocd',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // GitHub connector uses separate endpoint
      if (connectorType === 'github') {
        const githubRequest: CreateGitHubConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: githubRoutingDescription.trim() || undefined,
          organization: githubOrganization.trim(),
          personal_access_token: githubPat.trim(),
          base_url: githubBaseUrl.trim() || 'https://api.github.com',
        };

        const githubResponse = await apiClient.createGitHubConnector(githubRequest);

        const connector: Connector = {
          id: githubResponse.id,
          name: githubResponse.name,
          base_url: githubResponse.base_url,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          routing_description: githubRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'github',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // MCP connector uses separate endpoint (Phase 93)
      if (connectorType === 'mcp') {
        const mcpRequest: CreateMCPConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          server_url: mcpServerUrl.trim() || undefined,
          transport_type: mcpTransportType,
          command: mcpCommand.trim() || undefined,
          api_key: mcpApiKey.trim() || undefined,
        };

        const mcpResponse = await apiClient.createMCPConnector(mcpRequest);

        const connector: Connector = {
          id: mcpResponse.id,
          name: mcpResponse.name,
          base_url: mcpResponse.server_url || '',
          auth_type: mcpRequest.api_key ? 'API_KEY' : 'NONE',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'mcp',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'safe',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Slack connector uses separate endpoint (Phase 94.1)
      if (connectorType === 'slack') {
        const slackRequest: CreateSlackConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          slack_bot_token: slackBotToken.trim(),
          slack_app_token: slackAppToken.trim() || undefined,
          slack_user_token: slackUserToken.trim() || undefined,
        };

        const slackResponse = await apiClient.createSlackConnector(slackRequest);

        // Convert Slack response to Connector for onSuccess callback
        const connector: Connector = {
          id: slackResponse.id,
          name: slackResponse.name,
          base_url: 'https://slack.com/api',
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          tenant_id: '',
          connector_type: 'slack',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      // Email connector uses separate endpoint (Phase 44)
      if (connectorType === 'email') {
        const emailRequest: CreateEmailConnectorRequest = {
          name: name.trim(),
          description: description.trim() || undefined,
          routing_description: emailRoutingDescription.trim() || undefined,
          provider_type: emailProviderType,
          from_email: emailFromEmail.trim(),
          from_name: emailFromName.trim() || undefined,
          default_recipients: emailDefaultRecipients.trim(),
          // SMTP fields
          ...(emailProviderType === 'smtp' && {
            smtp_host: emailSmtpHost.trim(),
            smtp_port: emailSmtpPort,
            smtp_tls: emailSmtpTls,
            smtp_username: emailSmtpUsername.trim() || undefined,
            smtp_password: emailSmtpPassword || undefined,
          }),
          // SendGrid fields
          ...(emailProviderType === 'sendgrid' && {
            sendgrid_api_key: emailSendgridApiKey,
          }),
          // Mailgun fields
          ...(emailProviderType === 'mailgun' && {
            mailgun_api_key: emailMailgunApiKey,
            mailgun_domain: emailMailgunDomain.trim(),
          }),
          // SES fields
          ...(emailProviderType === 'ses' && {
            ses_access_key: emailSesAccessKey.trim(),
            ses_secret_key: emailSesSecretKey,
            ses_region: emailSesRegion,
          }),
          // Generic HTTP fields
          ...(emailProviderType === 'generic_http' && {
            http_endpoint_url: emailHttpEndpointUrl.trim(),
            http_auth_header: emailHttpAuthHeader || undefined,
            http_payload_template: emailHttpPayloadTemplate,
          }),
        };

        const emailResponse = await apiClient.createEmailConnector(emailRequest);

        // Convert Email response to Connector for onSuccess callback
        const connector: Connector = {
          id: emailResponse.id,
          name: emailResponse.name,
          base_url: emailProviderType === 'smtp' ? `smtp://${emailSmtpHost.trim()}:${emailSmtpPort}` : emailProviderType,
          auth_type: 'API_KEY',
          description: description.trim() || undefined,
          routing_description: emailRoutingDescription.trim() || undefined,
          tenant_id: '',
          connector_type: 'email',
          allowed_methods: [],
          blocked_methods: [],
          default_safety_level: 'caution',
          is_active: true,
          automation_enabled: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        };
        onSuccess(connector);
        return;
      }

      const blockedMethods = HTTP_METHODS.filter(m => !allowedMethods.includes(m));

      // Build custom login headers object
      const loginHeadersObj = customLoginHeaders.reduce((acc, header) => {
        if (header.key.trim() && header.value.trim()) {
          acc[header.key.trim()] = header.value.trim();
        }
        return acc;
      }, {} as Record<string, string>);

      // For SOAP, derive base_url from WSDL URL
      const derivedBaseUrl = connectorType === 'soap' && wsdlUrl.trim()
        ? new URL(wsdlUrl.trim()).origin
        : baseUrl.trim();

      const request: CreateConnectorRequest = {
        name: name.trim(),
        base_url: derivedBaseUrl,
        // Map SOAP auth type to connector auth type for credentials form
        auth_type: connectorType === 'soap' 
          ? (soapAuthType === 'basic' ? 'BASIC' : soapAuthType === 'session' ? 'SESSION' : 'NONE')
          : authType,
        description: description.trim() || undefined,
        allowed_methods: connectorType === 'soap' ? ['POST'] : allowedMethods,
        blocked_methods: connectorType === 'soap' ? [] : blockedMethods,
        default_safety_level: connectorType === 'soap' ? 'caution' : defaultSafetyLevel,
        // Connector type
        connector_type: connectorType,
        // SOAP-specific configuration
        ...(connectorType === 'soap' && wsdlUrl.trim() && {
          protocol_config: {
            wsdl_url: wsdlUrl.trim(),
            auth_type: soapAuthType,
            timeout: soapTimeout,
            verify_ssl: soapVerifySsl,
          },
        }),
        // REST-specific configuration (OpenAPI URL for auto-fetch)
        ...(connectorType === 'rest' && openapiUrl.trim() && {
          protocol_config: {
            openapi_url: openapiUrl.trim(),
          },
        }),
        // REST SESSION auth configuration
        ...(connectorType === 'rest' && authType === 'SESSION' && {
          login_url: loginUrl.trim(),
          login_method: loginMethod,
          login_config: {
            login_auth_type: loginAuthType,
            ...(loginAuthType === 'body' && {
              body_template: {
                username: '{{username}}',
                password: '{{password}}',
              },
            }),
            ...(Object.keys(loginHeadersObj).length > 0 && {
              login_headers: loginHeadersObj,
            }),
            token_location: tokenLocation,
            token_name: tokenName.trim(),
            ...(tokenLocation === 'body' && { token_path: tokenPath.trim() }),
            ...(headerName.trim() && { header_name: headerName.trim() }),
            session_duration_seconds: sessionDuration,
          },
        }),
      };

      const connector = await apiClient.createConnector(request);

      // If we have pending credentials from kubeconfig import, save them
      if (pendingCredentials && Object.keys(pendingCredentials).length > 0) {
        try {
          await apiClient.setUserCredentials(connector.id, pendingCredentials);
        } catch (credErr: unknown) {
          console.warn('Failed to save credentials from kubeconfig:', credErr);
          // Don't fail the whole operation - connector was created successfully
        }
      }

      onSuccess(connector);

    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to create connector');
    } finally {
      setSubmitting(false);
    }
  }, [name, baseUrl, authType, description, allowedMethods, defaultSafetyLevel, loginUrl, loginMethod, loginAuthType, customLoginHeaders, tokenLocation, tokenName, tokenPath, headerName, sessionDuration, connectorType, wsdlUrl, soapAuthType, soapTimeout, soapVerifySsl, vcenterHost, vcenterPort, vcenterDisableSsl, vcenterUsername, vcenterPassword, proxmoxHost, proxmoxPort, proxmoxDisableSsl, proxmoxAuthType, proxmoxUsername, proxmoxPassword, proxmoxTokenId, proxmoxTokenSecret, k8sServerUrl, k8sToken, k8sSkipTls, k8sRoutingDescription, gcpProjectId, gcpDefaultRegion, gcpDefaultZone, gcpServiceAccountJson, awsAccessKeyId, awsSecretAccessKey, awsDefaultRegion, azureTenantId, azureClientId, azureClientSecret, azureSubscriptionId, azureResourceGroupFilter, mcpServerUrl, mcpTransportType, mcpCommand, mcpApiKey, slackBotToken, slackAppToken, slackUserToken, prometheusBaseUrl, prometheusAuthType, prometheusUsername, prometheusPassword, prometheusToken, prometheusSkipTls, prometheusRoutingDescription, lokiBaseUrl, lokiAuthType, lokiUsername, lokiPassword, lokiToken, lokiSkipTls, lokiRoutingDescription, tempoBaseUrl, tempoAuthType, tempoUsername, tempoPassword, tempoToken, tempoSkipTls, tempoRoutingDescription, tempoOrgId, alertmanagerBaseUrl, alertmanagerAuthType, alertmanagerUsername, alertmanagerPassword, alertmanagerToken, alertmanagerSkipTls, alertmanagerRoutingDescription, openapiUrl, pendingCredentials, emailFromEmail, emailFromName, emailDefaultRecipients, emailRoutingDescription, emailProviderType, emailSmtpHost, emailSmtpPort, emailSmtpTls, emailSmtpUsername, emailSmtpPassword, emailSendgridApiKey, emailMailgunApiKey, emailMailgunDomain, emailSesAccessKey, emailSesSecretKey, emailSesRegion, emailHttpEndpointUrl, emailHttpAuthHeader, emailHttpPayloadTemplate, apiClient, onSuccess, argoApiToken, argoRoutingDescription, argoServerUrl, argoSkipTls, confluenceApiToken, confluenceEmail, confluenceRoutingDescription, confluenceSiteUrl, githubBaseUrl, githubOrganization, githubPat, githubRoutingDescription, jiraApiToken, jiraEmail, jiraRoutingDescription, jiraSiteUrl]);

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

              {/* Base URL - REST only (SOAP derives from WSDL) */}
              {connectorType === 'rest' && (
              <div>
                <label htmlFor="create-connector-base-url" className="block text-sm font-medium text-text-secondary mb-2">
                  Base URL *
                </label>
                <input
                  id="create-connector-base-url"
                  type="url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.github.com"
                  disabled={submitting}
                  data-testid="connector-base-url-input"
                  className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                />
              </div>
              )}

              {/* OpenAPI Spec URL - REST only */}
              {connectorType === 'rest' && (
              <div>
                <label htmlFor="create-connector-openapi-url" className="block text-sm font-medium text-text-secondary mb-2">
                  OpenAPI Spec URL <span className="text-text-tertiary">(optional)</span>
                </label>
                <input
                  id="create-connector-openapi-url"
                  type="url"
                  value={openapiUrl}
                  onChange={(e) => setOpenapiUrl(e.target.value)}
                  placeholder="https://api.example.com/openapi.json"
                  disabled={submitting}
                  className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                />
                <p className="text-xs text-text-tertiary mt-1">
                  If provided, the OpenAPI spec will be fetched and ingested automatically
                </p>
              </div>
              )}

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

            {/* Kubeconfig Import - REST only */}
            {connectorType === 'rest' && (
              <div className="mt-6">
                <button
                  type="button"
                  onClick={() => setShowKubeconfigImport(!showKubeconfigImport)}
                  className="flex items-center gap-2 text-sm text-primary hover:text-primary/80 transition-colors"
                >
                  {showKubeconfigImport ? (
                    <ChevronDown className="h-4 w-4" />
                  ) : (
                    <ChevronRight className="h-4 w-4" />
                  )}
                  <Upload className="h-4 w-4" />
                  Import from Kubeconfig
                </button>

                <AnimatePresence>
                  {showKubeconfigImport && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      exit={{ opacity: 0, height: 0 }}
                      className="mt-4 p-4 bg-blue-500/5 rounded-xl border border-blue-500/20 space-y-4"
                    >
                      <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
                        <Upload className="h-4 w-4" />
                        Kubernetes Cluster Import
                      </div>

                      <div>
                        <label htmlFor="create-kubeconfig-contents" className="block text-sm font-medium text-text-secondary mb-2">
                          Paste kubeconfig contents
                        </label>
                        <textarea
                          id="create-kubeconfig-contents"
                          value={kubeconfigText}
                          onChange={(e) => handleKubeconfigChange(e.target.value)}
                          placeholder={`apiVersion: v1
kind: Config
clusters:
- name: my-cluster
  cluster:
    server: https://...`}
                          rows={6}
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm resize-none"
                        />
                      </div>

                      {/* Context selector */}
                      {kubeconfigContexts.length > 1 && (
                        <div>
                          <label htmlFor="create-kube-context" className="block text-sm font-medium text-text-secondary mb-2">
                            Select Context
                          </label>
                          <select
                            id="create-kube-context"
                            value={selectedKubeContext}
                            onChange={(e) => handleKubeContextChange(e.target.value)}
                            disabled={submitting}
                            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all appearance-none"
                          >
                            {kubeconfigContexts.map((ctx) => (
                              <option key={ctx} value={ctx}>{ctx}</option>
                            ))}
                          </select>
                        </div>
                      )}

                      {/* Parse result */}
                      {kubeconfigInfo && (
                        <div className="p-3 bg-green-500/10 rounded-lg border border-green-500/20 text-green-300 text-sm space-y-2">
                          <p className="font-medium flex items-center gap-2">
                            <CheckCircle className="h-4 w-4" />
                            Kubeconfig parsed successfully
                          </p>
                          <div className="text-xs space-y-1 opacity-80">
                            <p><span className="text-text-tertiary">Server:</span> {kubeconfigInfo.server}</p>
                            <p><span className="text-text-tertiary">Auth Type:</span> {kubeconfigInfo.authType}</p>
                            {kubeconfigInfo.authWarning && (
                              <p className="text-amber-300 mt-2">{kubeconfigInfo.authWarning}</p>
                            )}
                          </div>
                        </div>
                      )}

                      {/* Parse error */}
                      {kubeconfigError && (
                        <div className="p-3 bg-red-500/10 rounded-lg border border-red-500/20 text-red-300 text-sm flex items-start gap-2">
                          <AlertCircle className="h-4 w-4 mt-0.5 flex-shrink-0" />
                          <span>{kubeconfigError}</span>
                        </div>
                      )}

                      <p className="text-xs text-text-tertiary">
                        Your kubeconfig will be parsed locally. Only the server URL and token are extracted and sent to the server.
                      </p>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}
          </div>

          {/* TASK-75: Protocol Type */}
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

            {/* SOAP Configuration */}
            {connectorType === 'soap' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-amber-500/5 rounded-xl border border-amber-500/20"
              >
                <div className="flex items-center gap-2 text-amber-400 text-sm font-medium">
                  <FileCode className="h-4 w-4" />
                  SOAP Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-wsdl-url" className="block text-sm font-medium text-text-secondary mb-2">
                      WSDL URL *
                    </label>
                    <input
                      id="create-wsdl-url"
                      type="url"
                      value={wsdlUrl}
                      onChange={(e) => setWsdlUrl(e.target.value)}
                      placeholder="https://vcenter.local/sdk/vimService.wsdl"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">URL to the WSDL service description file</p>
                  </div>

                  <div>
                    <label htmlFor="create-soap-auth-type" className="block text-sm font-medium text-text-secondary mb-2">
                      SOAP Auth Type
                    </label>
                    <select
                      id="create-soap-auth-type"
                      value={soapAuthType}
                      onChange={(e) => setSoapAuthType(e.target.value as 'none' | 'basic' | 'session')}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all appearance-none"
                    >
                      <option value="none">No Auth</option>
                      <option value="basic">HTTP Basic Auth</option>
                      <option value="session">Session Based (VMware)</option>
                    </select>
                    <p className="text-xs text-text-tertiary mt-1">
                      Session-based is recommended for VMware VIM API
                    </p>
                  </div>

                  <div>
                    <label htmlFor="create-soap-timeout" className="block text-sm font-medium text-text-secondary mb-2">
                      Timeout (seconds)
                    </label>
                    <input
                      id="create-soap-timeout"
                      type="number"
                      value={soapTimeout}
                      onChange={(e) => setSoapTimeout(parseInt(e.target.value) || 30)}
                      min="5"
                      max="300"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
                    />
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={soapVerifySsl}
                        onChange={(e) => setSoapVerifySsl(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-primary focus:ring-primary/50"
                      />
                      <span className="text-sm text-text-secondary">Verify SSL Certificate</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Disable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-amber-500/10 rounded-lg border border-amber-500/20 text-amber-300 text-sm">
                  <p className="font-medium">📋 SOAP Connector Setup</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Create the connector with WSDL URL</li>
                    <li>Set your credentials in the Credentials section</li>
                    <li>MEHO will parse the WSDL and discover all operations</li>
                    <li>The agent can then call SOAP operations via natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* VMware Configuration (TASK-97) */}
            {connectorType === 'vmware' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-emerald-500/5 rounded-xl border border-emerald-500/20"
              >
                <div className="flex items-center gap-2 text-emerald-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  VMware vSphere Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2 md:col-span-1">
                    <label htmlFor="create-vcenter-host" className="block text-sm font-medium text-text-secondary mb-2">
                      vCenter Host *
                    </label>
                    <input
                      id="create-vcenter-host"
                      type="text"
                      value={vcenterHost}
                      onChange={(e) => setVcenterHost(e.target.value)}
                      placeholder="vcenter.example.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Hostname or IP only (no https://)</p>
                  </div>

                  <div>
                    <label htmlFor="create-vcenter-port" className="block text-sm font-medium text-text-secondary mb-2">
                      Port
                    </label>
                    <input
                      id="create-vcenter-port"
                      type="number"
                      value={vcenterPort}
                      onChange={(e) => setVcenterPort(parseInt(e.target.value) || 443)}
                      min="1"
                      max="65535"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
                    />
                  </div>

                  <div>
                    <label htmlFor="create-vcenter-username" className="block text-sm font-medium text-text-secondary mb-2">
                      Username *
                    </label>
                    <input
                      id="create-vcenter-username"
                      type="text"
                      value={vcenterUsername}
                      onChange={(e) => setVcenterUsername(e.target.value)}
                      placeholder="administrator@vsphere.local"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
                    />
                  </div>

                  <div>
                    <label htmlFor="create-vcenter-password" className="block text-sm font-medium text-text-secondary mb-2">
                      Password *
                    </label>
                    <input
                      id="create-vcenter-password"
                      type="password"
                      value={vcenterPassword}
                      onChange={(e) => setVcenterPassword(e.target.value)}
                      placeholder="••••••••"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500/50 transition-all"
                    />
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={vcenterDisableSsl}
                        onChange={(e) => setVcenterDisableSsl(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-emerald-500 focus:ring-emerald-500/50"
                      />
                      <span className="text-sm text-text-secondary">Disable SSL Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-emerald-500/10 rounded-lg border border-emerald-500/20 text-emerald-300 text-sm">
                  <p className="font-medium">🖥️ VMware vSphere Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your vCenter Server details and credentials</li>
                    <li>MEHO will connect and register 174+ VMware operations</li>
                    <li>Operations include: VM power, snapshots, vMotion, DRS, HA, and more</li>
                    <li>The agent can manage your vSphere environment via natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Proxmox Configuration (TASK-100) */}
            {connectorType === 'proxmox' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-orange-500/5 rounded-xl border border-orange-500/20"
              >
                <div className="flex items-center gap-2 text-orange-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Proxmox VE Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2 md:col-span-1">
                    <label htmlFor="create-proxmox-host" className="block text-sm font-medium text-text-secondary mb-2">
                      Proxmox Host *
                    </label>
                    <input
                      id="create-proxmox-host"
                      type="text"
                      value={proxmoxHost}
                      onChange={(e) => setProxmoxHost(e.target.value)}
                      placeholder="proxmox.example.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Hostname or IP only (no https://)</p>
                  </div>

                  <div>
                    <label htmlFor="create-proxmox-port" className="block text-sm font-medium text-text-secondary mb-2">
                      Port
                    </label>
                    <input
                      id="create-proxmox-port"
                      type="number"
                      value={proxmoxPort}
                      onChange={(e) => setProxmoxPort(parseInt(e.target.value) || 8006)}
                      min="1"
                      max="65535"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                    />
                  </div>

                  <div className="col-span-2">
                    <span className="block text-sm font-medium text-text-secondary mb-2">
                      Authentication Method
                    </span>
                    <div className="grid grid-cols-2 gap-3">
                      <button
                        type="button"
                        onClick={() => setProxmoxAuthType('password')}
                        className={clsx(
                          "px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                          proxmoxAuthType === 'password'
                            ? "bg-orange-500/10 border-orange-500/50 text-white"
                            : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                        )}
                      >
                        Username/Password
                      </button>
                      <button
                        type="button"
                        onClick={() => setProxmoxAuthType('token')}
                        className={clsx(
                          "px-4 py-3 rounded-xl text-sm font-medium transition-all border",
                          proxmoxAuthType === 'token'
                            ? "bg-orange-500/10 border-orange-500/50 text-white"
                            : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                        )}
                      >
                        API Token (Recommended)
                      </button>
                    </div>
                  </div>

                  {proxmoxAuthType === 'password' ? (
                    <>
                      <div>
                        <label htmlFor="create-proxmox-username" className="block text-sm font-medium text-text-secondary mb-2">
                          Username *
                        </label>
                        <input
                          id="create-proxmox-username"
                          type="text"
                          value={proxmoxUsername}
                          onChange={(e) => setProxmoxUsername(e.target.value)}
                          placeholder="root@pam"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                        />
                      </div>

                      <div>
                        <label htmlFor="create-proxmox-password" className="block text-sm font-medium text-text-secondary mb-2">
                          Password *
                        </label>
                        <input
                          id="create-proxmox-password"
                          type="password"
                          value={proxmoxPassword}
                          onChange={(e) => setProxmoxPassword(e.target.value)}
                          placeholder="••••••••"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <label htmlFor="create-proxmox-token-id" className="block text-sm font-medium text-text-secondary mb-2">
                          API Token ID *
                        </label>
                        <input
                          id="create-proxmox-token-id"
                          type="text"
                          value={proxmoxTokenId}
                          onChange={(e) => setProxmoxTokenId(e.target.value)}
                          placeholder="user@realm!tokenname"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                        />
                        <p className="text-xs text-text-tertiary mt-1">Format: user@realm!tokenname</p>
                      </div>

                      <div>
                        <label htmlFor="create-proxmox-token-secret" className="block text-sm font-medium text-text-secondary mb-2">
                          API Token Secret *
                        </label>
                        <input
                          id="create-proxmox-token-secret"
                          type="password"
                          value={proxmoxTokenSecret}
                          onChange={(e) => setProxmoxTokenSecret(e.target.value)}
                          placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                        />
                      </div>
                    </>
                  )}

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={proxmoxDisableSsl}
                        onChange={(e) => setProxmoxDisableSsl(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-orange-500 focus:ring-orange-500/50"
                      />
                      <span className="text-sm text-text-secondary">Disable SSL Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-orange-500/10 rounded-lg border border-orange-500/20 text-orange-300 text-sm">
                  <p className="font-medium">🖥️ Proxmox VE Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Proxmox VE host details and credentials</li>
                    <li>MEHO will connect and register 40+ Proxmox operations</li>
                    <li>Operations include: VMs, LXC containers, snapshots, storage, and more</li>
                    <li>The agent can manage your Proxmox environment via natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Kubernetes Configuration (TASK-107) */}
            {connectorType === 'kubernetes' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
              >
                <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Kubernetes Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-k8s-server-url" className="block text-sm font-medium text-text-secondary mb-2">
                      API Server URL *
                    </label>
                    <input
                      id="create-k8s-server-url"
                      type="text"
                      value={k8sServerUrl}
                      onChange={(e) => setK8sServerUrl(e.target.value)}
                      placeholder="https://10.5.27.3:6443"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">The Kubernetes API server endpoint (e.g., from kubectl cluster-info)</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-k8s-token" className="block text-sm font-medium text-text-secondary mb-2">
                      Service Account Token *
                    </label>
                    <textarea
                      id="create-k8s-token"
                      value={k8sToken}
                      onChange={(e) => setK8sToken(e.target.value)}
                      placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
                      rows={4}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Bearer token from a Kubernetes Service Account (use kubectl create token)</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-k8s-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-k8s-routing-desc"
                      value={k8sRoutingDescription}
                      onChange={(e) => setK8sRoutingDescription(e.target.value)}
                      placeholder="Production Kubernetes cluster (RKE2) in Graz datacenter. Query for pods, deployments, services, nodes, namespaces."
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route queries to this connector. Describe what this cluster manages.</p>
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={k8sSkipTls}
                        onChange={(e) => setK8sSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-blue-500 focus:ring-blue-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates in lab/dev environments (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-blue-500/10 rounded-lg border border-blue-500/20 text-blue-300 text-sm">
                  <p className="font-medium">☸️ Kubernetes Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your K8s API server URL and Service Account token</li>
                    <li>MEHO will connect and register 49 Kubernetes operations</li>
                    <li>Operations include: pods, deployments, services, nodes, and more</li>
                    <li>The agent can query and manage your cluster via natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* GCP Configuration (TASK-102) */}
            {connectorType === 'gcp' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-sky-500/5 rounded-xl border border-sky-500/20"
              >
                <div className="flex items-center gap-2 text-sky-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Google Cloud Platform Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2 md:col-span-1">
                    <label htmlFor="create-gcp-project-id" className="block text-sm font-medium text-text-secondary mb-2">
                      GCP Project ID *
                    </label>
                    <input
                      id="create-gcp-project-id"
                      type="text"
                      value={gcpProjectId}
                      onChange={(e) => setGcpProjectId(e.target.value)}
                      placeholder="my-gcp-project"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Your Google Cloud project identifier</p>
                  </div>

                  <div>
                    <label htmlFor="create-gcp-default-region" className="block text-sm font-medium text-text-secondary mb-2">
                      Default Region
                    </label>
                    <select
                      id="create-gcp-default-region"
                      value={gcpDefaultRegion}
                      onChange={(e) => {
                        setGcpDefaultRegion(e.target.value);
                        // Update zone to match region
                        const zone = e.target.value + '-a';
                        setGcpDefaultZone(zone);
                      }}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all appearance-none"
                    >
                      <option value="us-central1">us-central1 (Iowa)</option>
                      <option value="us-east1">us-east1 (South Carolina)</option>
                      <option value="us-west1">us-west1 (Oregon)</option>
                      <option value="europe-west1">europe-west1 (Belgium)</option>
                      <option value="europe-west2">europe-west2 (London)</option>
                      <option value="europe-west3">europe-west3 (Frankfurt)</option>
                      <option value="asia-east1">asia-east1 (Taiwan)</option>
                      <option value="asia-southeast1">asia-southeast1 (Singapore)</option>
                    </select>
                  </div>

                  <div>
                    <label htmlFor="create-gcp-default-zone" className="block text-sm font-medium text-text-secondary mb-2">
                      Default Zone
                    </label>
                    <input
                      id="create-gcp-default-zone"
                      type="text"
                      value={gcpDefaultZone}
                      onChange={(e) => setGcpDefaultZone(e.target.value)}
                      placeholder="us-central1-a"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Default zone for VMs and disks (e.g., us-central1-a)</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-gcp-service-account-json" className="block text-sm font-medium text-text-secondary mb-2">
                      Service Account JSON *
                    </label>
                    <div className="space-y-3">
                      <textarea
                        id="create-gcp-service-account-json"
                        value={gcpServiceAccountJson}
                        onChange={(e) => setGcpServiceAccountJson(e.target.value)}
                        placeholder={`{
  "type": "service_account",
  "project_id": "my-project",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n",
  "client_email": "my-sa@my-project.iam.gserviceaccount.com",
  ...
}`}
                        rows={6}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all font-mono text-sm resize-none"
                      />
                      <div className="flex items-center gap-3">
                        <label className="flex-1">
                          <input
                            type="file"
                            accept=".json"
                            onChange={(e) => {
                              const file = e.target.files?.[0];
                              if (file) {
                                const reader = new FileReader();
                                reader.onload = (event) => {
                                  const content = event.target?.result as string;
                                  setGcpServiceAccountJson(content);
                                  // Try to extract project ID from the JSON
                                  try {
                                    const parsed = JSON.parse(content);
                                    if (parsed.project_id && !gcpProjectId) {
                                      setGcpProjectId(parsed.project_id);
                                    }
                                    if (!name && parsed.project_id) {
                                      setName(`GCP - ${parsed.project_id}`);
                                    }
                                  } catch {
                                    // Ignore parse errors
                                  }
                                };
                                reader.readAsText(file);
                              }
                            }}
                            disabled={submitting}
                            className="hidden"
                          />
                          <span className="flex items-center justify-center gap-2 px-4 py-2 bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 rounded-lg cursor-pointer transition-colors text-sm font-medium">
                            <Upload className="h-4 w-4" />
                            Upload JSON Key File
                          </span>
                        </label>
                        {gcpServiceAccountJson && (
                          <button
                            type="button"
                            onClick={() => setGcpServiceAccountJson('')}
                            className="px-3 py-2 text-sm text-text-tertiary hover:text-red-400 transition-colors"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    </div>
                    <p className="text-xs text-text-tertiary mt-2">
                      Paste the JSON key file content or upload the file. 
                      Create a Service Account in GCP Console with required permissions.
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-sky-500/10 rounded-lg border border-sky-500/20 text-sky-300 text-sm">
                  <p className="font-medium">☁️ Google Cloud Platform Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Create a Service Account in GCP Console with appropriate roles</li>
                    <li>Required roles: Compute Viewer, Container Viewer, Monitoring Viewer</li>
                    <li>Download the JSON key file and paste/upload it above</li>
                    <li>MEHO will register 40+ GCP operations for: Compute Engine, GKE, Networking, Monitoring</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Azure Configuration (Phase 92) */}
            {connectorType === 'azure' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
              >
                <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Microsoft Azure Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label htmlFor="create-azure-tenant-id" className="block text-sm font-medium text-text-secondary mb-2">
                      Tenant ID *
                    </label>
                    <input
                      id="create-azure-tenant-id"
                      type="text"
                      value={azureTenantId}
                      onChange={(e) => setAzureTenantId(e.target.value)}
                      placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Azure Active Directory tenant ID</p>
                  </div>

                  <div>
                    <label htmlFor="create-azure-subscription-id" className="block text-sm font-medium text-text-secondary mb-2">
                      Subscription ID *
                    </label>
                    <input
                      id="create-azure-subscription-id"
                      type="text"
                      value={azureSubscriptionId}
                      onChange={(e) => setAzureSubscriptionId(e.target.value)}
                      placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Target Azure subscription</p>
                  </div>

                  <div>
                    <label htmlFor="create-azure-client-id" className="block text-sm font-medium text-text-secondary mb-2">
                      Client ID (Application ID) *
                    </label>
                    <input
                      id="create-azure-client-id"
                      type="text"
                      value={azureClientId}
                      onChange={(e) => setAzureClientId(e.target.value)}
                      placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Service principal application (client) ID</p>
                  </div>

                  <div>
                    <label htmlFor="create-azure-client-secret" className="block text-sm font-medium text-text-secondary mb-2">
                      Client Secret *
                    </label>
                    <input
                      id="create-azure-client-secret"
                      type="password"
                      value={azureClientSecret}
                      onChange={(e) => setAzureClientSecret(e.target.value)}
                      placeholder="Service principal client secret"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Service principal client secret value</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-azure-resource-group-filter" className="block text-sm font-medium text-text-secondary mb-2">
                      Resource Group Filter (optional)
                    </label>
                    <input
                      id="create-azure-resource-group-filter"
                      type="text"
                      value={azureResourceGroupFilter}
                      onChange={(e) => setAzureResourceGroupFilter(e.target.value)}
                      placeholder="Leave empty for all resource groups"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Limit operations to a specific resource group (optional)</p>
                  </div>
                </div>

                <div className="text-xs text-text-tertiary space-y-1 mt-2">
                  <p className="font-medium text-text-secondary">Setup instructions:</p>
                  <ol className="list-decimal list-inside space-y-0.5 ml-1">
                    <li>Create a Service Principal in Azure AD (App registrations)</li>
                    <li>Assign Reader role on the target subscription</li>
                    <li>Create a client secret and copy the value</li>
                    <li>MEHO will register 42 Azure operations for: Compute, Monitor, AKS, Networking, Storage, Web</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* AWS Configuration (Phase 91) */}
            {connectorType === 'aws' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-orange-500/5 rounded-xl border border-orange-500/20"
              >
                <div className="flex items-center gap-2 text-orange-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Amazon Web Services Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label htmlFor="create-aws-access-key-id" className="block text-sm font-medium text-text-secondary mb-2">
                      Access Key ID
                    </label>
                    <input
                      id="create-aws-access-key-id"
                      type="text"
                      value={awsAccessKeyId}
                      onChange={(e) => setAwsAccessKeyId(e.target.value)}
                      placeholder="AKIAIOSFODNN7EXAMPLE"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Leave blank to use IAM role or environment credentials</p>
                  </div>

                  <div>
                    <label htmlFor="create-aws-secret-access-key" className="block text-sm font-medium text-text-secondary mb-2">
                      Secret Access Key
                    </label>
                    <input
                      id="create-aws-secret-access-key"
                      type="password"
                      value={awsSecretAccessKey}
                      onChange={(e) => setAwsSecretAccessKey(e.target.value)}
                      placeholder="AWS secret access key"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Leave blank to use IAM role or environment credentials</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-aws-default-region" className="block text-sm font-medium text-text-secondary mb-2">
                      Default Region
                    </label>
                    <input
                      id="create-aws-default-region"
                      type="text"
                      value={awsDefaultRegion}
                      onChange={(e) => setAwsDefaultRegion(e.target.value)}
                      placeholder="us-east-1"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Default AWS region for API calls (e.g., us-east-1, eu-west-1)</p>
                  </div>
                </div>

                <div className="text-xs text-text-tertiary space-y-1 mt-2">
                  <p className="font-medium text-text-secondary">Setup instructions:</p>
                  <ol className="list-decimal list-inside space-y-0.5 ml-1">
                    <li>Create an IAM user in AWS Console with programmatic access</li>
                    <li>Attach ReadOnlyAccess policy or specific service policies (EC2, ECS, EKS, S3, Lambda, RDS, CloudWatch, VPC)</li>
                    <li>Copy the Access Key ID and Secret Access Key</li>
                    <li>MEHO will register 25 AWS operations for: EC2, ECS, EKS, S3, Lambda, RDS, CloudWatch, VPC</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Prometheus Configuration */}
            {connectorType === 'prometheus' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-red-500/5 rounded-xl border border-red-500/20"
              >
                <div className="flex items-center gap-2 text-red-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Prometheus Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-prometheus-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Prometheus URL *
                    </label>
                    <input
                      id="create-prometheus-url"
                      type="text"
                      value={prometheusBaseUrl}
                      onChange={(e) => setPrometheusBaseUrl(e.target.value)}
                      placeholder="http://prometheus:9090"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">The Prometheus server URL (e.g., http://prometheus:9090)</p>
                  </div>

                  <div className="col-span-2">
                    <span className="block text-sm font-medium text-text-secondary mb-2">
                      Authentication Type
                    </span>
                    <div className="grid grid-cols-3 gap-3">
                      {[
                        { id: 'none' as const, label: 'No Auth' },
                        { id: 'basic' as const, label: 'Basic Auth' },
                        { id: 'bearer' as const, label: 'Bearer Token' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => setPrometheusAuthType(option.id)}
                          disabled={submitting}
                          className={clsx(
                            'px-4 py-2.5 rounded-lg border text-sm font-medium transition-all',
                            prometheusAuthType === option.id
                              ? 'border-red-500/50 bg-red-500/10 text-red-300'
                              : 'border-white/10 bg-surface text-text-secondary hover:border-white/20'
                          )}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {prometheusAuthType === 'basic' && (
                    <>
                      <div>
                        <label htmlFor="create-prometheus-username" className="block text-sm font-medium text-text-secondary mb-2">
                          Username *
                        </label>
                        <input
                          id="create-prometheus-username"
                          type="text"
                          value={prometheusUsername}
                          onChange={(e) => setPrometheusUsername(e.target.value)}
                          placeholder="prometheus"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                        />
                      </div>
                      <div>
                        <label htmlFor="create-prometheus-password" className="block text-sm font-medium text-text-secondary mb-2">
                          Password *
                        </label>
                        <input
                          id="create-prometheus-password"
                          type="password"
                          value={prometheusPassword}
                          onChange={(e) => setPrometheusPassword(e.target.value)}
                          placeholder="Enter password"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                        />
                      </div>
                    </>
                  )}

                  {prometheusAuthType === 'bearer' && (
                    <div className="col-span-2">
                      <label htmlFor="create-prometheus-token" className="block text-sm font-medium text-text-secondary mb-2">
                        Bearer Token *
                      </label>
                      <textarea
                        id="create-prometheus-token"
                        value={prometheusToken}
                        onChange={(e) => setPrometheusToken(e.target.value)}
                        placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
                        rows={3}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all font-mono text-sm resize-none"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
                    </div>
                  )}

                  <div className="col-span-2">
                    <label htmlFor="create-prometheus-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-prometheus-routing-desc"
                      value={prometheusRoutingDescription}
                      onChange={(e) => setPrometheusRoutingDescription(e.target.value)}
                      placeholder="Production Prometheus monitoring K8s cluster in Graz datacenter. Metrics for pods, nodes, services."
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route metric queries to this Prometheus instance.</p>
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={prometheusSkipTls}
                        onChange={(e) => setPrometheusSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-red-500 focus:ring-red-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-red-500/10 rounded-lg border border-red-500/20 text-red-300 text-sm">
                  <p className="font-medium">Prometheus Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Prometheus server URL and authentication details</li>
                    <li>MEHO will verify connectivity via /api/v1/status/buildinfo</li>
                    <li>Operations include: CPU/memory metrics, RED metrics, scrape targets, alerts</li>
                    <li>The agent can investigate infrastructure health through natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Loki Configuration */}
            {connectorType === 'loki' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-amber-500/5 rounded-xl border border-amber-500/20"
              >
                <div className="flex items-center gap-2 text-amber-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Loki Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-loki-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Loki URL *
                    </label>
                    <input
                      id="create-loki-url"
                      type="text"
                      value={lokiBaseUrl}
                      onChange={(e) => setLokiBaseUrl(e.target.value)}
                      placeholder="http://loki:3100"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">The Loki server URL (e.g., http://loki:3100)</p>
                  </div>

                  <div className="col-span-2">
                    <span className="block text-sm font-medium text-text-secondary mb-2">
                      Authentication Type
                    </span>
                    <div className="grid grid-cols-3 gap-3">
                      {[
                        { id: 'none' as const, label: 'No Auth' },
                        { id: 'basic' as const, label: 'Basic Auth' },
                        { id: 'bearer' as const, label: 'Bearer Token' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => setLokiAuthType(option.id)}
                          disabled={submitting}
                          className={clsx(
                            'px-4 py-2.5 rounded-lg border text-sm font-medium transition-all',
                            lokiAuthType === option.id
                              ? 'border-amber-500/50 bg-amber-500/10 text-amber-300'
                              : 'border-white/10 bg-surface text-text-secondary hover:border-white/20'
                          )}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {lokiAuthType === 'basic' && (
                    <>
                      <div>
                        <label htmlFor="create-loki-username" className="block text-sm font-medium text-text-secondary mb-2">
                          Username *
                        </label>
                        <input
                          id="create-loki-username"
                          type="text"
                          value={lokiUsername}
                          onChange={(e) => setLokiUsername(e.target.value)}
                          placeholder="loki"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
                        />
                      </div>
                      <div>
                        <label htmlFor="create-loki-password" className="block text-sm font-medium text-text-secondary mb-2">
                          Password *
                        </label>
                        <input
                          id="create-loki-password"
                          type="password"
                          value={lokiPassword}
                          onChange={(e) => setLokiPassword(e.target.value)}
                          placeholder="Enter password"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
                        />
                      </div>
                    </>
                  )}

                  {lokiAuthType === 'bearer' && (
                    <div className="col-span-2">
                      <label htmlFor="create-loki-token" className="block text-sm font-medium text-text-secondary mb-2">
                        Bearer Token *
                      </label>
                      <textarea
                        id="create-loki-token"
                        value={lokiToken}
                        onChange={(e) => setLokiToken(e.target.value)}
                        placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
                        rows={3}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all font-mono text-sm resize-none"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
                    </div>
                  )}

                  <div className="col-span-2">
                    <label htmlFor="create-loki-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-loki-routing-desc"
                      value={lokiRoutingDescription}
                      onChange={(e) => setLokiRoutingDescription(e.target.value)}
                      placeholder="Production Loki receiving logs from K8s cluster in Graz datacenter"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route log queries to this Loki instance.</p>
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={lokiSkipTls}
                        onChange={(e) => setLokiSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-amber-500 focus:ring-amber-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-amber-500/10 rounded-lg border border-amber-500/20 text-amber-300 text-sm">
                  <p className="font-medium">Loki Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Loki server URL and authentication details</li>
                    <li>MEHO will verify connectivity via /loki/api/v1/status/buildinfo or /ready</li>
                    <li>Operations include: log search, error logs, volume analysis, label discovery</li>
                    <li>The agent can investigate application behavior through natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Tempo Configuration */}
            {connectorType === 'tempo' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-cyan-500/5 rounded-xl border border-cyan-500/20"
              >
                <div className="flex items-center gap-2 text-cyan-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Tempo Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-tempo-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Tempo URL *
                    </label>
                    <input
                      id="create-tempo-url"
                      type="text"
                      value={tempoBaseUrl}
                      onChange={(e) => setTempoBaseUrl(e.target.value)}
                      placeholder="http://tempo:3200"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">The Tempo server URL (e.g., http://tempo:3200)</p>
                  </div>

                  <div className="col-span-2">
                    <span className="block text-sm font-medium text-text-secondary mb-2">
                      Authentication Type
                    </span>
                    <div className="grid grid-cols-3 gap-3">
                      {[
                        { id: 'none' as const, label: 'No Auth' },
                        { id: 'basic' as const, label: 'Basic Auth' },
                        { id: 'bearer' as const, label: 'Bearer Token' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => setTempoAuthType(option.id)}
                          disabled={submitting}
                          className={clsx(
                            'px-4 py-2.5 rounded-lg border text-sm font-medium transition-all',
                            tempoAuthType === option.id
                              ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-300'
                              : 'border-white/10 bg-surface text-text-secondary hover:border-white/20'
                          )}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {tempoAuthType === 'basic' && (
                    <>
                      <div>
                        <label htmlFor="create-tempo-username" className="block text-sm font-medium text-text-secondary mb-2">
                          Username *
                        </label>
                        <input
                          id="create-tempo-username"
                          type="text"
                          value={tempoUsername}
                          onChange={(e) => setTempoUsername(e.target.value)}
                          placeholder="tempo"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                        />
                      </div>
                      <div>
                        <label htmlFor="create-tempo-password" className="block text-sm font-medium text-text-secondary mb-2">
                          Password *
                        </label>
                        <input
                          id="create-tempo-password"
                          type="password"
                          value={tempoPassword}
                          onChange={(e) => setTempoPassword(e.target.value)}
                          placeholder="Enter password"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                        />
                      </div>
                    </>
                  )}

                  {tempoAuthType === 'bearer' && (
                    <div className="col-span-2">
                      <label htmlFor="create-tempo-token" className="block text-sm font-medium text-text-secondary mb-2">
                        Bearer Token *
                      </label>
                      <textarea
                        id="create-tempo-token"
                        value={tempoToken}
                        onChange={(e) => setTempoToken(e.target.value)}
                        placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
                        rows={3}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all font-mono text-sm resize-none"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
                    </div>
                  )}

                  <div className="col-span-2">
                    <label htmlFor="create-tempo-org-id" className="block text-sm font-medium text-text-secondary mb-2">
                      Org ID (Multi-Tenant)
                    </label>
                    <input
                      id="create-tempo-org-id"
                      type="text"
                      value={tempoOrgId}
                      onChange={(e) => setTempoOrgId(e.target.value)}
                      placeholder="my-tenant"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Optional tenant org ID for multi-tenant Tempo deployments (sets X-Scope-OrgID header)</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-tempo-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-tempo-routing-desc"
                      value={tempoRoutingDescription}
                      onChange={(e) => setTempoRoutingDescription(e.target.value)}
                      placeholder="Production Tempo receiving traces from K8s microservices in Graz datacenter"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route trace queries to this Tempo instance.</p>
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={tempoSkipTls}
                        onChange={(e) => setTempoSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-cyan-500 focus:ring-cyan-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-cyan-500/10 rounded-lg border border-cyan-500/20 text-cyan-300 text-sm">
                  <p className="font-medium">Tempo Connector</p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Tempo server URL and authentication details</li>
                    <li>MEHO will verify connectivity via /api/status/buildinfo or /ready</li>
                    <li>Operations include: trace search, service graph, tag discovery, span details</li>
                    <li>The agent can investigate distributed traces and latency through natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Alertmanager Configuration */}
            {connectorType === 'alertmanager' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-red-500/5 rounded-xl border border-red-500/20"
              >
                <div className="flex items-center gap-2 text-red-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Alertmanager Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-alertmanager-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Alertmanager URL *
                    </label>
                    <input
                      id="create-alertmanager-url"
                      type="text"
                      value={alertmanagerBaseUrl}
                      onChange={(e) => setAlertmanagerBaseUrl(e.target.value)}
                      placeholder="http://alertmanager:9093"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">The Alertmanager server URL (e.g., http://alertmanager:9093)</p>
                  </div>

                  <div className="col-span-2">
                    <span className="block text-sm font-medium text-text-secondary mb-2">
                      Authentication Type
                    </span>
                    <div className="grid grid-cols-3 gap-3">
                      {[
                        { id: 'none' as const, label: 'No Auth' },
                        { id: 'basic' as const, label: 'Basic Auth' },
                        { id: 'bearer' as const, label: 'Bearer Token' },
                      ].map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          onClick={() => setAlertmanagerAuthType(option.id)}
                          disabled={submitting}
                          className={clsx(
                            'px-4 py-2.5 rounded-lg border text-sm font-medium transition-all',
                            alertmanagerAuthType === option.id
                              ? 'border-red-500/50 bg-red-500/10 text-red-300'
                              : 'border-white/10 bg-surface text-text-secondary hover:border-white/20'
                          )}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {alertmanagerAuthType === 'basic' && (
                    <>
                      <div>
                        <label htmlFor="create-alertmanager-username" className="block text-sm font-medium text-text-secondary mb-2">
                          Username *
                        </label>
                        <input
                          id="create-alertmanager-username"
                          type="text"
                          value={alertmanagerUsername}
                          onChange={(e) => setAlertmanagerUsername(e.target.value)}
                          placeholder="alertmanager"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                        />
                      </div>
                      <div>
                        <label htmlFor="create-alertmanager-password" className="block text-sm font-medium text-text-secondary mb-2">
                          Password *
                        </label>
                        <input
                          id="create-alertmanager-password"
                          type="password"
                          value={alertmanagerPassword}
                          onChange={(e) => setAlertmanagerPassword(e.target.value)}
                          placeholder="Enter password"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all"
                        />
                      </div>
                    </>
                  )}

                  {alertmanagerAuthType === 'bearer' && (
                    <div className="col-span-2">
                      <label htmlFor="create-alertmanager-token" className="block text-sm font-medium text-text-secondary mb-2">
                        Bearer Token *
                      </label>
                      <textarea
                        id="create-alertmanager-token"
                        value={alertmanagerToken}
                        onChange={(e) => setAlertmanagerToken(e.target.value)}
                        placeholder="eyJhbGciOiJSUzI1NiIsImtpZCI6..."
                        rows={3}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all font-mono text-sm resize-none"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Bearer token for OAuth2 proxy or service mesh authentication</p>
                    </div>
                  )}

                  <div className="col-span-2">
                    <label htmlFor="create-alertmanager-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-alertmanager-routing-desc"
                      value={alertmanagerRoutingDescription}
                      onChange={(e) => setAlertmanagerRoutingDescription(e.target.value)}
                      placeholder="Production Alertmanager managing K8s cluster alerts in Graz datacenter"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-red-500/50 focus:border-red-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route alert queries to this Alertmanager instance.</p>
                  </div>

                  <div className="col-span-2">
                    <label className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={alertmanagerSkipTls}
                        onChange={(e) => setAlertmanagerSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="w-5 h-5 rounded border-white/20 bg-surface text-red-500 focus:ring-red-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS Certificate Verification</span>
                    </label>
                    <p className="text-xs text-text-tertiary mt-1 ml-8">
                      Enable for self-signed certificates (not recommended for production)
                    </p>
                  </div>
                </div>

                <div className="p-3 bg-red-500/10 rounded-lg border border-red-500/20 text-red-300 text-sm">
                  <p className="font-medium">Alertmanager Connector</p>
                  <p className="mt-2 text-xs opacity-80">
                    Connect to Alertmanager for alert investigation and silence management.
                    Supports active/silenced/inhibited alert listing, silence CRUD with trust approval,
                    and cluster status monitoring.
                  </p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Alertmanager server URL and authentication details</li>
                    <li>MEHO will verify connectivity via /api/v2/status or /-/ready</li>
                    <li>Operations include: alert listing, silence management, cluster status</li>
                    <li>The agent can investigate alerts and manage silences through natural language</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Jira Configuration */}
            {connectorType === 'jira' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-blue-500/20"
              >
                <div className="flex items-center gap-2 text-blue-400 text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Jira Cloud Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-jira-site-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Jira Site URL *
                    </label>
                    <input
                      id="create-jira-site-url"
                      type="text"
                      value={jiraSiteUrl}
                      onChange={(e) => setJiraSiteUrl(e.target.value)}
                      placeholder="https://yoursite.atlassian.net"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Your Jira Cloud site URL</p>
                  </div>

                  <div>
                    <label htmlFor="create-jira-email" className="block text-sm font-medium text-text-secondary mb-2">
                      Email *
                    </label>
                    <input
                      id="create-jira-email"
                      type="email"
                      value={jiraEmail}
                      onChange={(e) => setJiraEmail(e.target.value)}
                      placeholder="user@company.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Atlassian account email</p>
                  </div>

                  <div>
                    <label htmlFor="create-jira-api-token" className="block text-sm font-medium text-text-secondary mb-2">
                      API Token *
                    </label>
                    <input
                      id="create-jira-api-token"
                      type="password"
                      value={jiraApiToken}
                      onChange={(e) => setJiraApiToken(e.target.value)}
                      placeholder="Your Atlassian API token"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all font-mono text-sm"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Generate at id.atlassian.com/manage-profile/security/api-tokens</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-jira-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-jira-routing-desc"
                      value={jiraRoutingDescription}
                      onChange={(e) => setJiraRoutingDescription(e.target.value)}
                      placeholder="Production Jira tracking engineering team issues"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route issue queries to this Jira instance</p>
                  </div>
                </div>

                <div className="p-3 bg-blue-500/10 rounded-lg border border-blue-500/20 text-blue-300 text-sm">
                  <p className="font-medium">Jira Cloud Connector</p>
                  <p className="mt-2 text-xs opacity-80">
                    Connect to Jira Cloud for issue search, creation, commenting, and status transitions.
                    Operations include: search, create, comment, transition, and project listing.
                  </p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Jira Cloud site URL</li>
                    <li>Enter your Atlassian email and API token</li>
                    <li>MEHO will verify connectivity and list accessible projects</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* Confluence Configuration */}
            {connectorType === 'confluence' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-blue-500/5 rounded-xl border border-[#1868DB]/30"
              >
                <div className="flex items-center gap-2 text-[#1868DB] text-sm font-medium">
                  <Server className="h-4 w-4" />
                  Confluence Cloud Configuration
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div className="col-span-2">
                    <label htmlFor="create-confluence-site-url" className="block text-sm font-medium text-text-secondary mb-2">
                      Confluence Site URL *
                    </label>
                    <input
                      id="create-confluence-site-url"
                      type="text"
                      value={confluenceSiteUrl}
                      onChange={(e) => setConfluenceSiteUrl(e.target.value)}
                      placeholder="https://your-domain.atlassian.net"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Your Atlassian site URL (same as Jira if on the same instance)</p>
                  </div>

                  <div>
                    <label htmlFor="create-confluence-email" className="block text-sm font-medium text-text-secondary mb-2">
                      Email *
                    </label>
                    <input
                      id="create-confluence-email"
                      type="email"
                      value={confluenceEmail}
                      onChange={(e) => setConfluenceEmail(e.target.value)}
                      placeholder="user@company.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Atlassian account email</p>
                  </div>

                  <div>
                    <label htmlFor="create-confluence-api-token" className="block text-sm font-medium text-text-secondary mb-2">
                      API Token *
                    </label>
                    <input
                      id="create-confluence-api-token"
                      type="password"
                      value={confluenceApiToken}
                      onChange={(e) => setConfluenceApiToken(e.target.value)}
                      placeholder="Your Atlassian API token"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all font-mono text-sm"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Generate at id.atlassian.com/manage-profile/security/api-tokens</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-confluence-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-confluence-routing-desc"
                      value={confluenceRoutingDescription}
                      onChange={(e) => setConfluenceRoutingDescription(e.target.value)}
                      placeholder="Confluence wiki for runbooks and documentation"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#1868DB]/50 focus:border-[#1868DB]/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator route documentation queries to this Confluence instance</p>
                  </div>
                </div>

                <div className="p-3 bg-[#1868DB]/10 rounded-lg border border-[#1868DB]/20 text-blue-300 text-sm">
                  <p className="font-medium">Confluence Cloud Connector</p>
                  <p className="mt-2 text-xs opacity-80">
                    Connect to Confluence Cloud for searching, reading, and creating wiki pages and spaces.
                    Operations include: search, get page, create page, update page, list spaces, and get space.
                  </p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Enter your Confluence Cloud site URL</li>
                    <li>Enter your Atlassian email and API token</li>
                    <li>MEHO will verify connectivity and list accessible spaces</li>
                  </ol>
                </div>
              </motion.div>
            )}

            {/* ArgoCD Configuration */}
            {connectorType === 'argocd' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="space-y-4"
              >
                <div className="p-4 bg-orange-500/5 border border-orange-500/20 rounded-xl">
                  <h3 className="text-orange-400 font-medium text-sm mb-3">ArgoCD Connection</h3>
                  <div className="space-y-4">
                    <div>
                      <label htmlFor="create-argo-server-url" className="block text-sm text-text-secondary mb-1.5">Server URL <span className="text-red-400">*</span></label>
                      <input
                        id="create-argo-server-url"
                        type="url"
                        value={argoServerUrl}
                        onChange={(e) => setArgoServerUrl(e.target.value)}
                        placeholder="https://argocd.example.com"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-argo-api-token" className="block text-sm text-text-secondary mb-1.5">API Token <span className="text-red-400">*</span></label>
                      <input
                        id="create-argo-api-token"
                        type="password"
                        value={argoApiToken}
                        onChange={(e) => setArgoApiToken(e.target.value)}
                        placeholder="Generated via argocd account generate-token or the UI"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-argo-routing-desc" className="block text-sm text-text-secondary mb-1.5">Routing Description</label>
                      <input
                        id="create-argo-routing-desc"
                        type="text"
                        value={argoRoutingDescription}
                        onChange={(e) => setArgoRoutingDescription(e.target.value)}
                        placeholder="ArgoCD server for production GitOps deployments"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
                      />
                    </div>
                    <label className="flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={argoSkipTls}
                        onChange={(e) => setArgoSkipTls(e.target.checked)}
                        disabled={submitting}
                        className="rounded border-white/20 bg-surface text-orange-500 focus:ring-orange-500/50"
                      />
                      <span className="text-sm text-text-secondary">Skip TLS verification (self-signed certs)</span>
                    </label>
                  </div>
                </div>
              </motion.div>
            )}

            {/* GitHub Configuration */}
            {connectorType === 'github' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="space-y-4"
              >
                <div className="p-4 bg-violet-500/5 border border-violet-500/20 rounded-xl">
                  <h3 className="text-violet-400 font-medium text-sm mb-3">GitHub Connection</h3>
                  <div className="space-y-4">
                    <div>
                      <label htmlFor="create-github-org" className="block text-sm text-text-secondary mb-1.5">Organization <span className="text-red-400">*</span></label>
                      <input
                        id="create-github-org"
                        type="text"
                        value={githubOrganization}
                        onChange={(e) => setGithubOrganization(e.target.value)}
                        placeholder="my-org"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-github-pat" className="block text-sm text-text-secondary mb-1.5">Personal Access Token <span className="text-red-400">*</span></label>
                      <input
                        id="create-github-pat"
                        type="password"
                        value={githubPat}
                        onChange={(e) => setGithubPat(e.target.value)}
                        placeholder="ghp_xxxxxxxxxxxx (Classic PAT with repo, read:org)"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-github-base-url" className="block text-sm text-text-secondary mb-1.5">API Base URL</label>
                      <input
                        id="create-github-base-url"
                        type="url"
                        value={githubBaseUrl}
                        onChange={(e) => setGithubBaseUrl(e.target.value)}
                        placeholder="https://api.github.com"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Change for GitHub Enterprise. Default: https://api.github.com</p>
                    </div>
                    <div>
                      <label htmlFor="create-github-routing-desc" className="block text-sm text-text-secondary mb-1.5">Routing Description</label>
                      <input
                        id="create-github-routing-desc"
                        type="text"
                        value={githubRoutingDescription}
                        onChange={(e) => setGithubRoutingDescription(e.target.value)}
                        placeholder="GitHub repos, PRs, Actions for CI/CD pipeline tracing"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-violet-500/50 focus:border-violet-500/50 transition-all"
                      />
                    </div>
                  </div>
                </div>
              </motion.div>
            )}

            {/* MCP Configuration (Phase 93) */}
            {connectorType === 'mcp' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="space-y-4"
              >
                <div className="p-4 bg-cyan-500/5 border border-cyan-500/20 rounded-xl">
                  <h3 className="text-cyan-400 font-medium text-sm mb-3">MCP Server Connection</h3>
                  <div className="space-y-4">
                    <div>
                      {/* eslint-disable-next-line jsx-a11y/label-has-associated-control -- transport type uses button group, not input */}
                      <label className="block text-sm text-text-secondary mb-1.5">Transport Type</label>
                      <div className="flex gap-3">
                        <button
                          type="button"
                          onClick={() => setMcpTransportType('streamable_http')}
                          disabled={submitting}
                          className={clsx(
                            'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium border transition-all',
                            mcpTransportType === 'streamable_http'
                              ? 'bg-cyan-500/10 border-cyan-500/40 text-cyan-400'
                              : 'bg-surface border-white/10 text-text-secondary hover:border-white/20'
                          )}
                        >
                          Streamable HTTP
                        </button>
                        <button
                          type="button"
                          onClick={() => setMcpTransportType('stdio')}
                          disabled={submitting}
                          className={clsx(
                            'flex-1 px-4 py-2.5 rounded-xl text-sm font-medium border transition-all',
                            mcpTransportType === 'stdio'
                              ? 'bg-cyan-500/10 border-cyan-500/40 text-cyan-400'
                              : 'bg-surface border-white/10 text-text-secondary hover:border-white/20'
                          )}
                        >
                          stdio
                        </button>
                      </div>
                    </div>
                    {mcpTransportType === 'streamable_http' && (
                      <div>
                        <label htmlFor="create-mcp-server-url" className="block text-sm text-text-secondary mb-1.5">Server URL <span className="text-red-400">*</span></label>
                        <input
                          id="create-mcp-server-url"
                          type="url"
                          value={mcpServerUrl}
                          onChange={(e) => setMcpServerUrl(e.target.value)}
                          placeholder="https://mcp-server.example.com/mcp"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                        />
                      </div>
                    )}
                    {mcpTransportType === 'stdio' && (
                      <div>
                        <label htmlFor="create-mcp-command" className="block text-sm text-text-secondary mb-1.5">Command <span className="text-red-400">*</span></label>
                        <input
                          id="create-mcp-command"
                          type="text"
                          value={mcpCommand}
                          onChange={(e) => setMcpCommand(e.target.value)}
                          placeholder="npx -y @modelcontextprotocol/server-filesystem"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                        />
                        <p className="text-xs text-text-tertiary mt-1">The command to launch the MCP server as a subprocess</p>
                      </div>
                    )}
                    <div>
                      <label htmlFor="create-mcp-api-key" className="block text-sm text-text-secondary mb-1.5">API Key</label>
                      <input
                        id="create-mcp-api-key"
                        type="password"
                        value={mcpApiKey}
                        onChange={(e) => setMcpApiKey(e.target.value)}
                        placeholder="Optional: Bearer token for MCP server authentication"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-cyan-500/50 focus:border-cyan-500/50 transition-all"
                      />
                    </div>
                  </div>
                </div>
              </motion.div>
            )}

            {/* Slack Configuration (Phase 94.1) */}
            {connectorType === 'slack' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="space-y-4"
              >
                <div className="p-4 bg-purple-500/5 border border-purple-500/20 rounded-xl">
                  <h3 className="text-purple-400 font-medium text-sm mb-3">Slack Connection</h3>
                  <div className="space-y-4">
                    <div>
                      <label htmlFor="create-slack-bot-token" className="block text-sm text-text-secondary mb-1.5">Bot Token <span className="text-red-400">*</span></label>
                      <input
                        id="create-slack-bot-token"
                        type="password"
                        value={slackBotToken}
                        onChange={(e) => setSlackBotToken(e.target.value)}
                        placeholder="xoxb-xxxxxxxxxxxx"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-slack-app-token" className="block text-sm text-text-secondary mb-1.5">App Token</label>
                      <input
                        id="create-slack-app-token"
                        type="password"
                        value={slackAppToken}
                        onChange={(e) => setSlackAppToken(e.target.value)}
                        placeholder="xapp-xxxxxxxxxxxx"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Required for Socket Mode (default)</p>
                    </div>
                    <div>
                      <label htmlFor="create-slack-user-token" className="block text-sm text-text-secondary mb-1.5">User Token</label>
                      <input
                        id="create-slack-user-token"
                        type="password"
                        value={slackUserToken}
                        onChange={(e) => setSlackUserToken(e.target.value)}
                        placeholder="xoxp-xxxxxxxxxxxx"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-purple-500/50 focus:border-purple-500/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Optional -- enables search.messages</p>
                    </div>
                  </div>
                </div>
              </motion.div>
            )}

            {/* Email Configuration (Phase 44) */}
            {connectorType === 'email' && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                className="space-y-6 p-6 bg-green-500/5 rounded-xl border border-[#22C55E]/30"
              >
                <div className="flex items-center gap-2 text-[#22C55E] text-sm font-medium">
                  <Mail className="h-4 w-4" />
                  Email Connector Configuration
                </div>

                {/* Common fields */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <div>
                    <label htmlFor="create-email-from" className="block text-sm font-medium text-text-secondary mb-2">
                      From Email *
                    </label>
                    <input
                      id="create-email-from"
                      type="email"
                      value={emailFromEmail}
                      onChange={(e) => setEmailFromEmail(e.target.value)}
                      placeholder="meho@company.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                    />
                  </div>

                  <div>
                    <label htmlFor="create-email-from-name" className="block text-sm font-medium text-text-secondary mb-2">
                      From Name
                    </label>
                    <input
                      id="create-email-from-name"
                      type="text"
                      value={emailFromName}
                      onChange={(e) => setEmailFromName(e.target.value)}
                      placeholder="MEHO Alerts"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                    />
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-email-recipients" className="block text-sm font-medium text-text-secondary mb-2">
                      Default Recipients *
                    </label>
                    <input
                      id="create-email-recipients"
                      type="text"
                      value={emailDefaultRecipients}
                      onChange={(e) => setEmailDefaultRecipients(e.target.value)}
                      placeholder="ops-team@company.com, sre@company.com"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Comma-separated email addresses</p>
                  </div>

                  <div className="col-span-2">
                    <label htmlFor="create-email-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
                      Routing Description
                    </label>
                    <textarea
                      id="create-email-routing-desc"
                      value={emailRoutingDescription}
                      onChange={(e) => setEmailRoutingDescription(e.target.value)}
                      placeholder="Email connector for sending investigation reports"
                      rows={2}
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm resize-none"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator decide when to send email notifications</p>
                  </div>
                </div>

                {/* Provider selector */}
                <div>
                  <label htmlFor="create-email-provider" className="block text-sm font-medium text-text-secondary mb-2">
                    Email Provider *
                  </label>
                  <select
                    id="create-email-provider"
                    value={emailProviderType}
                    onChange={(e) => {
                      setEmailProviderType(e.target.value as EmailProviderType);
                      // Reset provider-specific fields on change
                      setEmailSmtpHost(''); setEmailSmtpPort(587); setEmailSmtpTls(true);
                      setEmailSmtpUsername(''); setEmailSmtpPassword('');
                      setEmailSendgridApiKey('');
                      setEmailMailgunApiKey(''); setEmailMailgunDomain('');
                      setEmailSesAccessKey(''); setEmailSesSecretKey(''); setEmailSesRegion('us-east-1');
                      setEmailHttpEndpointUrl(''); setEmailHttpAuthHeader(''); setEmailHttpPayloadTemplate('');
                    }}
                    disabled={submitting}
                    className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all appearance-none text-sm"
                  >
                    <option value="smtp">SMTP</option>
                    <option value="sendgrid">SendGrid</option>
                    <option value="mailgun">Mailgun</option>
                    <option value="ses">Amazon SES</option>
                    <option value="generic_http">Generic HTTP</option>
                  </select>
                </div>

                {/* SMTP fields */}
                {emailProviderType === 'smtp' && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
                    <div>
                      <label htmlFor="create-smtp-host" className="block text-sm font-medium text-text-secondary mb-2">
                        SMTP Host *
                      </label>
                      <input
                        id="create-smtp-host"
                        type="text"
                        value={emailSmtpHost}
                        onChange={(e) => setEmailSmtpHost(e.target.value)}
                        placeholder="smtp.gmail.com"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <label htmlFor="create-smtp-port" className="block text-sm font-medium text-text-secondary mb-2">
                          Port
                        </label>
                        <input
                          id="create-smtp-port"
                          type="number"
                          value={emailSmtpPort}
                          onChange={(e) => setEmailSmtpPort(parseInt(e.target.value) || 587)}
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                        />
                      </div>
                      <div className="flex items-end">
                        <label className="flex items-center gap-2 cursor-pointer p-3">
                          <input
                            type="checkbox"
                            checked={emailSmtpTls}
                            onChange={(e) => setEmailSmtpTls(e.target.checked)}
                            disabled={submitting}
                            className="rounded border-white/20 bg-surface text-[#22C55E] focus:ring-[#22C55E]/50"
                          />
                          <span className="text-sm text-text-secondary">TLS</span>
                        </label>
                      </div>
                    </div>
                    <div>
                      <label htmlFor="create-smtp-username" className="block text-sm font-medium text-text-secondary mb-2">
                        Username
                      </label>
                      <input
                        id="create-smtp-username"
                        type="text"
                        value={emailSmtpUsername}
                        onChange={(e) => setEmailSmtpUsername(e.target.value)}
                        placeholder="user@gmail.com"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-smtp-password" className="block text-sm font-medium text-text-secondary mb-2">
                        Password
                      </label>
                      <input
                        id="create-smtp-password"
                        type="password"
                        value={emailSmtpPassword}
                        onChange={(e) => setEmailSmtpPassword(e.target.value)}
                        placeholder="App password"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                  </div>
                )}

                {/* SendGrid fields */}
                {emailProviderType === 'sendgrid' && (
                  <div className="p-4 bg-white/5 rounded-xl border border-white/10">
                    <label htmlFor="create-sendgrid-api-key" className="block text-sm font-medium text-text-secondary mb-2">
                      SendGrid API Key *
                    </label>
                    <input
                      id="create-sendgrid-api-key"
                      type="password"
                      value={emailSendgridApiKey}
                      onChange={(e) => setEmailSendgridApiKey(e.target.value)}
                      placeholder="SG.xxxxx"
                      disabled={submitting}
                      className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                    />
                    <p className="text-xs text-text-tertiary mt-1">Create at app.sendgrid.com/settings/api_keys</p>
                  </div>
                )}

                {/* Mailgun fields */}
                {emailProviderType === 'mailgun' && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
                    <div>
                      <label htmlFor="create-mailgun-api-key" className="block text-sm font-medium text-text-secondary mb-2">
                        Mailgun API Key *
                      </label>
                      <input
                        id="create-mailgun-api-key"
                        type="password"
                        value={emailMailgunApiKey}
                        onChange={(e) => setEmailMailgunApiKey(e.target.value)}
                        placeholder="key-xxxxx"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-mailgun-domain" className="block text-sm font-medium text-text-secondary mb-2">
                        Mailgun Domain *
                      </label>
                      <input
                        id="create-mailgun-domain"
                        type="text"
                        value={emailMailgunDomain}
                        onChange={(e) => setEmailMailgunDomain(e.target.value)}
                        placeholder="mg.company.com"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                  </div>
                )}

                {/* SES fields */}
                {emailProviderType === 'ses' && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
                    <div>
                      <label htmlFor="create-ses-access-key" className="block text-sm font-medium text-text-secondary mb-2">
                        Access Key ID *
                      </label>
                      <input
                        id="create-ses-access-key"
                        type="text"
                        value={emailSesAccessKey}
                        onChange={(e) => setEmailSesAccessKey(e.target.value)}
                        placeholder="AKIAIOSFODNN7EXAMPLE"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-ses-secret-key" className="block text-sm font-medium text-text-secondary mb-2">
                        Secret Access Key *
                      </label>
                      <input
                        id="create-ses-secret-key"
                        type="password"
                        value={emailSesSecretKey}
                        onChange={(e) => setEmailSesSecretKey(e.target.value)}
                        placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-ses-region" className="block text-sm font-medium text-text-secondary mb-2">
                        SES Region
                      </label>
                      <select
                        id="create-ses-region"
                        value={emailSesRegion}
                        onChange={(e) => setEmailSesRegion(e.target.value)}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all appearance-none text-sm"
                      >
                        <option value="us-east-1">US East (N. Virginia)</option>
                        <option value="us-east-2">US East (Ohio)</option>
                        <option value="us-west-2">US West (Oregon)</option>
                        <option value="eu-west-1">EU (Ireland)</option>
                        <option value="eu-west-2">EU (London)</option>
                        <option value="eu-central-1">EU (Frankfurt)</option>
                        <option value="ap-southeast-1">Asia Pacific (Singapore)</option>
                        <option value="ap-southeast-2">Asia Pacific (Sydney)</option>
                        <option value="ap-northeast-1">Asia Pacific (Tokyo)</option>
                      </select>
                    </div>
                  </div>
                )}

                {/* Generic HTTP fields */}
                {emailProviderType === 'generic_http' && (
                  <div className="grid grid-cols-1 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
                    <div>
                      <label htmlFor="create-http-endpoint-url" className="block text-sm font-medium text-text-secondary mb-2">
                        Endpoint URL *
                      </label>
                      <input
                        id="create-http-endpoint-url"
                        type="url"
                        value={emailHttpEndpointUrl}
                        onChange={(e) => setEmailHttpEndpointUrl(e.target.value)}
                        placeholder="https://api.email-service.com/send"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-http-auth-header" className="block text-sm font-medium text-text-secondary mb-2">
                        Auth Header
                      </label>
                      <input
                        id="create-http-auth-header"
                        type="password"
                        value={emailHttpAuthHeader}
                        onChange={(e) => setEmailHttpAuthHeader(e.target.value)}
                        placeholder="Bearer your-token"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
                      />
                    </div>
                    <div>
                      <label htmlFor="create-http-payload-template" className="block text-sm font-medium text-text-secondary mb-2">
                        Payload Template *
                      </label>
                      <textarea
                        id="create-http-payload-template"
                        value={emailHttpPayloadTemplate}
                        onChange={(e) => setEmailHttpPayloadTemplate(e.target.value)}
                        placeholder={'{\n  "from": "{{ from_email }}",\n  "to": "{{ to_emails }}",\n  "subject": "{{ subject }}",\n  "html": "{{ html_body }}"\n}'}
                        rows={6}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm font-mono resize-none"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Jinja2 template with {'{{ from_email }}'}, {'{{ subject }}'}, {'{{ html_body }}'} variables</p>
                    </div>
                  </div>
                )}

                <div className="p-3 bg-[#22C55E]/10 rounded-lg border border-[#22C55E]/20 text-green-300 text-sm">
                  <p className="font-medium">Email Connector</p>
                  <p className="mt-2 text-xs opacity-80">
                    Send email notifications and reports from MEHO investigations.
                    Operations include: send_email and check_status.
                  </p>
                  <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
                    <li>Configure your email provider and sender details</li>
                    <li>MEHO will verify connectivity with a test email</li>
                    <li>Use in investigations to email findings and reports</li>
                  </ol>
                </div>
              </motion.div>
            )}
          </div>

          {/* Authentication - REST only */}
          {connectorType === 'rest' && (
          <div className="space-y-6">
            <div className="flex items-center gap-2 text-white font-medium">
              <Key className="h-4 w-4 text-primary" />
              <h3>Authentication</h3>
            </div>

            <div className="space-y-6">
              <div>
                <span className="block text-sm font-medium text-text-secondary mb-2">
                  Auth Type
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {[
                    { id: 'API_KEY', label: 'API Key (Bearer)' },
                    { id: 'BASIC', label: 'Basic Auth' },
                    { id: 'OAUTH2', label: 'OAuth 2.0' },
                    { id: 'SESSION', label: 'Session Based' },
                    { id: 'NONE', label: 'No Auth' }
                  ].map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      onClick={() => setAuthType(option.id as 'API_KEY' | 'BASIC' | 'OAUTH2' | 'NONE' | 'SESSION')}
                      className={clsx(
                        "px-4 py-3 rounded-xl text-sm font-medium text-left transition-all border",
                        authType === option.id
                          ? "bg-primary/10 border-primary/50 text-white"
                          : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                      )}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* SESSION auth configuration */}
              {authType === 'SESSION' && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  className="space-y-6 p-6 bg-white/5 rounded-xl border border-white/10"
                >
                  <div className="flex items-center gap-2 text-primary text-sm font-medium">
                    <Lock className="h-4 w-4" />
                    Session Configuration
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <div className="col-span-2">
                      <label htmlFor="create-session-login-url" className="block text-sm font-medium text-text-secondary mb-2">
                        Login URL *
                      </label>
                      <input
                        id="create-session-login-url"
                        type="text"
                        value={loginUrl}
                        onChange={(e) => setLoginUrl(e.target.value)}
                        placeholder="/api/v1/auth/login"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Relative to base URL</p>
                    </div>

                    <div>
                      <label htmlFor="create-session-login-method" className="block text-sm font-medium text-text-secondary mb-2">
                        Login Method
                      </label>
                      <select
                        id="create-session-login-method"
                        value={loginMethod}
                        onChange={(e) => setLoginMethod(e.target.value as 'POST' | 'GET')}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                      >
                        <option value="POST">POST</option>
                        <option value="GET">GET</option>
                      </select>
                    </div>

                    <div>
                      <label htmlFor="create-session-login-auth-type" className="block text-sm font-medium text-text-secondary mb-2">
                        Login Auth Type
                      </label>
                      <select
                        id="create-session-login-auth-type"
                        value={loginAuthType}
                        onChange={(e) => setLoginAuthType(e.target.value as 'body' | 'basic')}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                      >
                        <option value="body">JSON Body (username/password)</option>
                        <option value="basic">HTTP Basic Auth</option>
                      </select>
                      <p className="text-xs text-text-tertiary mt-1">
                        {loginAuthType === 'basic' ? 'Credentials in Authorization header' : 'Credentials in JSON body'}
                      </p>
                    </div>

                    <div>
                      <label htmlFor="create-session-token-location" className="block text-sm font-medium text-text-secondary mb-2">
                        Token Location
                      </label>
                      <select
                        id="create-session-token-location"
                        value={tokenLocation}
                        onChange={(e) => setTokenLocation(e.target.value as 'header' | 'cookie' | 'body')}
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all appearance-none"
                      >
                        <option value="header">Response Header</option>
                        <option value="cookie">Cookie</option>
                        <option value="body">Response Body (JSON)</option>
                      </select>
                    </div>

                    <div>
                      <label htmlFor="create-session-token-name" className="block text-sm font-medium text-text-secondary mb-2">
                        Token Name *
                      </label>
                      <input
                        id="create-session-token-name"
                        type="text"
                        value={tokenName}
                        onChange={(e) => setTokenName(e.target.value)}
                        placeholder="X-Auth-Token"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Header/cookie name or JSON key in login response</p>
                    </div>

                    {tokenLocation === 'body' && (
                      <div>
                        <label htmlFor="create-session-token-path" className="block text-sm font-medium text-text-secondary mb-2">
                          Token Path
                        </label>
                        <input
                          id="create-session-token-path"
                          type="text"
                          value={tokenPath}
                          onChange={(e) => setTokenPath(e.target.value)}
                          placeholder="$.token"
                          disabled={submitting}
                          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                        />
                        <p className="text-xs text-text-tertiary mt-1">JSONPath for nested tokens (e.g., $.data.token)</p>
                      </div>
                    )}

                    <div>
                      <label htmlFor="create-session-header-name" className="block text-sm font-medium text-text-secondary mb-2">
                        Header Name (for API requests)
                      </label>
                      <input
                        id="create-session-header-name"
                        type="text"
                        value={headerName}
                        onChange={(e) => setHeaderName(e.target.value)}
                        placeholder="vmware-api-session-id"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      />
                      <p className="text-xs text-text-tertiary mt-1">Optional: Custom header name for sending token (default: Authorization Bearer)</p>
                    </div>

                    <div>
                      <label htmlFor="create-session-duration" className="block text-sm font-medium text-text-secondary mb-2">
                        Session Duration (s)
                      </label>
                      <input
                        id="create-session-duration"
                        type="number"
                        value={sessionDuration}
                        onChange={(e) => setSessionDuration(parseInt(e.target.value) || 3600)}
                        min="60"
                        max="86400"
                        disabled={submitting}
                        className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all"
                      />
                    </div>

                    {/* Custom Login Headers */}
                    <div className="col-span-2 space-y-3">
                      <div className="flex items-center justify-between">
                        <span className="block text-sm font-medium text-text-secondary">
                          Custom Login Headers
                        </span>
                        <button
                          type="button"
                          onClick={handleAddHeader}
                          disabled={submitting}
                          className="px-3 py-1 text-xs bg-primary/10 hover:bg-primary/20 text-primary rounded-lg transition-colors"
                        >
                          + Add Header
                        </button>
                      </div>
                      <p className="text-xs text-text-tertiary">
                        Optional headers to send with login request (e.g., vmware-use-header-authn: test)
                      </p>
                      {customLoginHeaders.length > 0 && (
                        <div className="space-y-2">
                          {customLoginHeaders.map((header, index) => (
                            <div key={index} className="flex gap-2">
                              <input
                                type="text"
                                value={header.key}
                                onChange={(e) => handleHeaderChange(index, 'key', e.target.value)}
                                placeholder="Header name"
                                disabled={submitting}
                                className="flex-1 px-3 py-2 bg-surface border border-white/10 rounded-lg text-white text-sm placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
                              />
                              <input
                                type="text"
                                value={header.value}
                                onChange={(e) => handleHeaderChange(index, 'value', e.target.value)}
                                placeholder="Header value"
                                disabled={submitting}
                                className="flex-1 px-3 py-2 bg-surface border border-white/10 rounded-lg text-white text-sm placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50"
                              />
                              <button
                                type="button"
                                onClick={() => handleRemoveHeader(index)}
                                disabled={submitting}
                                className="px-3 py-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg transition-colors text-sm"
                              >
                                Remove
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </motion.div>
              )}
            </div>
          </div>
          )}

          {/* Safety Policies - REST only (SOAP uses operations, not HTTP methods) */}
          {connectorType === 'rest' && (
          <div className="space-y-6">
            <div className="flex items-center gap-2 text-white font-medium">
              <Shield className="h-4 w-4 text-primary" />
              <h3>Safety Policies</h3>
            </div>

            <div className="space-y-6">
              {/* Allowed Methods */}
              <div>
                <span className="block text-sm font-medium text-text-secondary mb-3">
                  Allowed HTTP Methods
                </span>
                <div className="flex flex-wrap gap-3">
                  {HTTP_METHODS.map((method) => (
                    <label key={method} className={clsx(
                      "flex items-center gap-2 px-4 py-2 rounded-xl border cursor-pointer transition-all select-none",
                      allowedMethods.includes(method)
                        ? "bg-primary/10 border-primary/50 text-white"
                        : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                    )}>
                      <input
                        type="checkbox"
                        checked={allowedMethods.includes(method)}
                        onChange={() => handleMethodToggle(method)}
                        disabled={submitting}
                        className="hidden"
                      />
                      <span className="text-sm font-medium">{method}</span>
                    </label>
                  ))}
                </div>
              </div>

              {/* Default Safety Level */}
              <div>
                <span className="block text-sm font-medium text-text-secondary mb-3">
                  Default Safety Level
                </span>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <button
                    type="button"
                    onClick={() => setDefaultSafetyLevel('safe')}
                    className={clsx(
                      "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                      defaultSafetyLevel === 'safe'
                        ? "bg-green-400/10 border-green-400/50 text-green-400"
                        : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                    )}
                  >
                    <ShieldCheck className="h-6 w-6" />
                    <span className="text-sm font-medium">Safe</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => setDefaultSafetyLevel('caution')}
                    className={clsx(
                      "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                      defaultSafetyLevel === 'caution'
                        ? "bg-amber-500/10 border-amber-500/50 text-amber-400"
                        : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                    )}
                  >
                    <Shield className="h-6 w-6" />
                    <span className="text-sm font-medium">Caution</span>
                  </button>

                  <button
                    type="button"
                    onClick={() => setDefaultSafetyLevel('dangerous')}
                    className={clsx(
                      "flex flex-col items-center gap-2 p-4 rounded-xl border transition-all",
                      defaultSafetyLevel === 'dangerous'
                        ? "bg-red-500/10 border-red-500/50 text-red-400"
                        : "bg-surface border-white/10 text-text-secondary hover:bg-white/5"
                    )}
                  >
                    <ShieldAlert className="h-6 w-6" />
                    <span className="text-sm font-medium">Dangerous</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
          )}

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
              disabled={
                !name.trim() || 
                (connectorType === 'rest' && !baseUrl.trim()) ||
                (connectorType === 'rest' && allowedMethods.length === 0) ||
                (connectorType === 'soap' && !wsdlUrl.trim()) ||
                (connectorType === 'vmware' && (!vcenterHost.trim() || !vcenterUsername.trim() || !vcenterPassword.trim())) ||
                (connectorType === 'proxmox' && (!proxmoxHost.trim() || 
                  (proxmoxAuthType === 'password' && (!proxmoxUsername.trim() || !proxmoxPassword.trim())) ||
                  (proxmoxAuthType === 'token' && (!proxmoxTokenId.trim() || !proxmoxTokenSecret.trim()))
                )) ||
                (connectorType === 'kubernetes' && (!k8sServerUrl.trim() || !k8sToken.trim())) ||
                (connectorType === 'gcp' && (!gcpProjectId.trim() || !gcpServiceAccountJson.trim())) ||
                (connectorType === 'azure' && (!azureTenantId.trim() || !azureClientId.trim() || !azureClientSecret.trim() || !azureSubscriptionId.trim())) ||
                (connectorType === 'prometheus' && (!prometheusBaseUrl.trim() ||
                  (prometheusAuthType === 'basic' && (!prometheusUsername.trim() || !prometheusPassword.trim())) ||
                  (prometheusAuthType === 'bearer' && !prometheusToken.trim())
                )) ||
                (connectorType === 'loki' && (!lokiBaseUrl.trim() ||
                  (lokiAuthType === 'basic' && (!lokiUsername.trim() || !lokiPassword.trim())) ||
                  (lokiAuthType === 'bearer' && !lokiToken.trim())
                )) ||
                (connectorType === 'tempo' && (!tempoBaseUrl.trim() ||
                  (tempoAuthType === 'basic' && (!tempoUsername.trim() || !tempoPassword.trim())) ||
                  (tempoAuthType === 'bearer' && !tempoToken.trim())
                )) ||
                submitting
              }
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

