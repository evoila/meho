// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CreateConnectorModal } from '../CreateConnectorModal';
import { getConnectorsClient } from '@/api/clients/connectors';

vi.mock('@/api/clients/connectors');

const mockClient = {
  createConnector: vi.fn(),
  createVMwareConnector: vi.fn(),
  createProxmoxConnector: vi.fn(),
  createKubernetesConnector: vi.fn(),
  createGCPConnector: vi.fn(),
  createAzureConnector: vi.fn(),
  createAWSConnector: vi.fn(),
  createPrometheusConnector: vi.fn(),
  createLokiConnector: vi.fn(),
  createTempoConnector: vi.fn(),
  createAlertmanagerConnector: vi.fn(),
  createJiraConnector: vi.fn(),
  createConfluenceConnector: vi.fn(),
  createEmailConnector: vi.fn(),
  createArgoConnector: vi.fn(),
  createGitHubConnector: vi.fn(),
  createMCPConnector: vi.fn(),
  createSlackConnector: vi.fn(),
  setUserCredentials: vi.fn(),
};

describe('CreateConnectorModal', () => {
  const mockOnClose = vi.fn();
  const mockOnSuccess = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getConnectorsClient).mockReturnValue(
      mockClient as unknown as ReturnType<typeof getConnectorsClient>
    );
  });

  it('renders modal header', () => {
    render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
    expect(screen.getByTestId('create-connector-modal-title')).toHaveTextContent('Create Connector');
  });

  it('calls onClose when cancel button clicked', () => {
    render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
    fireEvent.click(screen.getByText('Cancel'));
    expect(mockOnClose).toHaveBeenCalled();
  });

  describe('field rendering per connector type', () => {
    function selectType(label: string) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(label, 'i') }));
    }

    it('renders REST base URL field when REST selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      expect(screen.getByLabelText(/Base URL/i)).toBeInTheDocument();
    });

    it('renders SOAP WSDL URL field when SOAP selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('SOAP');
      expect(screen.getByLabelText(/WSDL URL/i)).toBeInTheDocument();
    });

    it('renders VMware host field when VMware selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('VMware');
      expect(screen.getByLabelText(/vCenter Host/i)).toBeInTheDocument();
    });

    it('renders Proxmox host field when Proxmox selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Proxmox');
      expect(screen.getByLabelText(/Host/i)).toBeInTheDocument();
    });

    it('renders Kubernetes server URL field when K8s selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('K8s');
      expect(screen.getByLabelText(/API Server URL/i)).toBeInTheDocument();
    });

    it('renders GCP project ID field when GCP selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('GCP');
      expect(screen.getByLabelText(/Project ID/i)).toBeInTheDocument();
    });

    it('renders Azure tenant ID field when Azure selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Azure');
      expect(screen.getByLabelText(/Tenant ID/i)).toBeInTheDocument();
    });

    it('renders AWS region field when AWS selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('AWS');
      expect(screen.getByLabelText(/Default Region/i)).toBeInTheDocument();
    });

    it('renders Prometheus base URL field when Prometheus selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Prometheus');
      expect(screen.getByLabelText(/Prometheus URL/i)).toBeInTheDocument();
    });

    it('renders Loki base URL field when Loki selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Loki');
      expect(screen.getByLabelText(/Loki URL/i)).toBeInTheDocument();
    });

    it('renders Tempo base URL field when Tempo selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Tempo');
      expect(screen.getByLabelText(/Tempo URL/i)).toBeInTheDocument();
    });

    it('renders Alertmanager base URL field when Alertmanager selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Alertmanager');
      expect(screen.getByLabelText(/Alertmanager URL/i)).toBeInTheDocument();
    });

    it('renders Jira site URL field when Jira selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Jira');
      expect(screen.getByLabelText(/Site URL/i)).toBeInTheDocument();
    });

    it('renders Confluence site URL field when Confluence selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Confluence');
      expect(screen.getByLabelText(/Site URL/i)).toBeInTheDocument();
    });

    it('renders ArgoCD server URL field when ArgoCD selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('ArgoCD');
      expect(screen.getByLabelText(/Server URL/i)).toBeInTheDocument();
    });

    it('renders GitHub organization field when GitHub selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('GitHub');
      expect(screen.getByLabelText(/Organization/i)).toBeInTheDocument();
    });

    it('renders MCP transport type selector when MCP selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('MCP');
      expect(screen.getByRole('button', { name: /Streamable HTTP/i })).toBeInTheDocument();
    });

    it('renders Slack bot token field when Slack selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Slack');
      expect(screen.getByLabelText(/Bot Token/i)).toBeInTheDocument();
    });

    it('renders Email provider selector when Email selected', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      selectType('Email');
      const providerSelect = screen.getByLabelText(/Email Provider/i);
      expect(providerSelect).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /SMTP/i })).toBeInTheDocument();
    });
  });

  describe('submit button disabled when required fields empty', () => {
    it('disables submit when name is empty (REST)', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      expect(screen.getByTestId('create-connector-submit-button')).toBeDisabled();
    });

    it('disables submit when REST base URL empty even with name filled', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My API' },
      });
      expect(screen.getByTestId('create-connector-submit-button')).toBeDisabled();
    });

    it('enables submit when REST name and base URL are filled', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My API' },
      });
      fireEvent.change(screen.getByLabelText(/Base URL/i), {
        target: { value: 'https://api.example.com' },
      });
      expect(screen.getByTestId('create-connector-submit-button')).not.toBeDisabled();
    });

    it('disables submit when Kubernetes server URL is empty', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /K8s/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My K8s' },
      });
      expect(screen.getByTestId('create-connector-submit-button')).toBeDisabled();
    });

    it('disables submit when Slack bot token is empty', () => {
      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Slack/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My Slack' },
      });
      expect(screen.getByTestId('create-connector-submit-button')).toBeDisabled();
    });
  });

  describe('successful submission', () => {
    const baseConnector = {
      id: 'c1',
      name: 'Test',
      base_url: 'https://k8s.example.com',
      auth_type: 'API_KEY' as const,
      tenant_id: '',
      connector_type: 'kubernetes' as const,
      allowed_methods: [],
      blocked_methods: [],
      default_safety_level: 'caution' as const,
      is_active: true,
      automation_enabled: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };

    it('calls createKubernetesConnector with correct payload', async () => {
      mockClient.createKubernetesConnector.mockResolvedValue({
        id: 'c1',
        name: 'My K8s',
        server_url: 'https://k8s.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

      fireEvent.click(screen.getByRole('button', { name: /K8s/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My K8s' },
      });
      fireEvent.change(screen.getByLabelText(/API Server URL/i), {
        target: { value: 'https://k8s.example.com' },
      });
      fireEvent.change(screen.getByLabelText(/Service Account Token/i), {
        target: { value: 'my-token' },
      });

      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createKubernetesConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            name: 'My K8s',
            server_url: 'https://k8s.example.com',
            token: 'my-token',
          })
        );
      });

      expect(mockOnSuccess).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'c1', connector_type: 'kubernetes' })
      );
    });

    it('calls createConnector with correct payload for REST', async () => {
      mockClient.createConnector.mockResolvedValue({
        ...baseConnector,
        connector_type: 'rest',
        base_url: 'https://api.example.com',
        name: 'My REST',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My REST' },
      });
      fireEvent.change(screen.getByLabelText(/Base URL/i), {
        target: { value: 'https://api.example.com' },
      });

      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            name: 'My REST',
            base_url: 'https://api.example.com',
            connector_type: 'rest',
          })
        );
      });

      expect(mockOnSuccess).toHaveBeenCalledWith(
        expect.objectContaining({ connector_type: 'rest' })
      );
    });

    it('calls createSlackConnector with correct payload', async () => {
      mockClient.createSlackConnector.mockResolvedValue({
        id: 's1',
        name: 'My Slack',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

      fireEvent.click(screen.getByRole('button', { name: /^Slack/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My Slack' },
      });
      fireEvent.change(screen.getByLabelText(/Bot Token/i), {
        target: { value: 'xoxb-test-token' },
      });

      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createSlackConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            name: 'My Slack',
            slack_bot_token: 'xoxb-test-token',
          })
        );
      });
    });

    it('calls createConnector with correct payload for SOAP', async () => {
      mockClient.createConnector.mockResolvedValue({
        id: 's1', name: 'My SOAP', base_url: 'https://soap.example.com',
        auth_type: 'NONE', tenant_id: '', connector_type: 'soap',
        allowed_methods: ['POST'], blocked_methods: [], default_safety_level: 'caution',
        is_active: true, automation_enabled: false,
        created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^SOAP/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My SOAP' } });
      fireEvent.change(screen.getByLabelText(/WSDL URL/i), { target: { value: 'https://soap.example.com/service?wsdl' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My SOAP', connector_type: 'soap', allowed_methods: ['POST'] })
        );
      });
    });

    it('calls createVMwareConnector and strips https:// from host', async () => {
      mockClient.createVMwareConnector.mockResolvedValue({
        id: 'vm1', name: 'My VMware', vcenter_host: 'vcenter.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^VMware/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My VMware' } });
      fireEvent.change(screen.getByLabelText(/vCenter Host/i), { target: { value: 'https://vcenter.example.com/' } });
      fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'admin' } });
      fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'secret' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createVMwareConnector).toHaveBeenCalledWith(
          expect.objectContaining({ vcenter_host: 'vcenter.example.com', username: 'admin' })
        );
      });
      expect(mockOnSuccess).toHaveBeenCalledWith(expect.objectContaining({ connector_type: 'vmware' }));
    });

    it('calls createProxmoxConnector with password auth', async () => {
      mockClient.createProxmoxConnector.mockResolvedValue({
        id: 'px1', name: 'My Proxmox', host: 'pve.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Proxmox/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Proxmox' } });
      fireEvent.change(screen.getByLabelText(/Proxmox Host/i), { target: { value: 'pve.example.com' } });
      fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'root@pam' } });
      fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'secret' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createProxmoxConnector).toHaveBeenCalledWith(
          expect.objectContaining({ host: 'pve.example.com', username: 'root@pam' })
        );
      });
    });

    it('calls createProxmoxConnector with token auth when token mode selected', async () => {
      mockClient.createProxmoxConnector.mockResolvedValue({
        id: 'px2', name: 'My Proxmox Token', host: 'pve.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Proxmox/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Proxmox Token' } });
      fireEvent.change(screen.getByLabelText(/Proxmox Host/i), { target: { value: 'pve.example.com' } });
      fireEvent.click(screen.getByRole('button', { name: /API Token/i }));
      fireEvent.change(screen.getByLabelText(/API Token ID/i), { target: { value: 'root@pam!mytoken' } });
      fireEvent.change(screen.getByLabelText(/API Token Secret/i), { target: { value: 'secret-uuid' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createProxmoxConnector).toHaveBeenCalledWith(
          expect.objectContaining({ api_token_id: 'root@pam!mytoken', api_token_secret: 'secret-uuid' })
        );
      });
    });

    it('calls createGCPConnector with correct payload', async () => {
      mockClient.createGCPConnector.mockResolvedValue({
        id: 'g1', name: 'My GCP', project_id: 'my-project',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^GCP/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My GCP' } });
      fireEvent.change(screen.getByLabelText(/GCP Project ID/i), { target: { value: 'my-project' } });
      fireEvent.change(screen.getByLabelText(/Service Account JSON/i), { target: { value: '{"project_id":"my-project"}' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createGCPConnector).toHaveBeenCalledWith(
          expect.objectContaining({ project_id: 'my-project', service_account_json: '{"project_id":"my-project"}' })
        );
      });
    });

    it('calls createAzureConnector with correct payload', async () => {
      mockClient.createAzureConnector.mockResolvedValue({
        id: 'az1', name: 'My Azure',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Azure/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Azure' } });
      fireEvent.change(screen.getByLabelText(/Tenant ID/i), { target: { value: 'tenant-123' } });
      fireEvent.change(screen.getByLabelText(/Client ID/i), { target: { value: 'client-456' } });
      fireEvent.change(screen.getByLabelText(/Client Secret/i), { target: { value: 'secret-789' } });
      fireEvent.change(screen.getByLabelText(/Subscription ID/i), { target: { value: 'sub-abc' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createAzureConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            tenant_id: 'tenant-123', client_id: 'client-456',
            client_secret: 'secret-789', subscription_id: 'sub-abc',
          })
        );
      });
    });

    it('calls createAWSConnector with correct payload', async () => {
      mockClient.createAWSConnector.mockResolvedValue({
        id: 'aws1', name: 'My AWS',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^AWS/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My AWS' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createAWSConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My AWS' })
        );
      });
    });

    it('calls createPrometheusConnector with correct payload', async () => {
      mockClient.createPrometheusConnector.mockResolvedValue({
        id: 'p1', name: 'My Prometheus', base_url: 'https://prometheus.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Prometheus/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Prometheus' } });
      fireEvent.change(screen.getByLabelText(/Prometheus URL/i), { target: { value: 'https://prometheus.example.com' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createPrometheusConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My Prometheus', base_url: 'https://prometheus.example.com' })
        );
      });
    });

    it('calls createLokiConnector with correct payload', async () => {
      mockClient.createLokiConnector.mockResolvedValue({
        id: 'l1', name: 'My Loki', base_url: 'https://loki.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Loki/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Loki' } });
      fireEvent.change(screen.getByLabelText(/Loki URL/i), { target: { value: 'https://loki.example.com' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createLokiConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My Loki', base_url: 'https://loki.example.com' })
        );
      });
    });

    it('calls createTempoConnector with correct payload', async () => {
      mockClient.createTempoConnector.mockResolvedValue({
        id: 't1', name: 'My Tempo', base_url: 'https://tempo.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Tempo/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Tempo' } });
      fireEvent.change(screen.getByLabelText(/Tempo URL/i), { target: { value: 'https://tempo.example.com' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createTempoConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My Tempo', base_url: 'https://tempo.example.com' })
        );
      });
    });

    it('calls createAlertmanagerConnector with correct payload', async () => {
      mockClient.createAlertmanagerConnector.mockResolvedValue({
        id: 'am1', name: 'My Alertmanager', base_url: 'https://alertmanager.example.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Alertmanager/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Alertmanager' } });
      fireEvent.change(screen.getByLabelText(/Alertmanager URL/i), { target: { value: 'https://alertmanager.example.com' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createAlertmanagerConnector).toHaveBeenCalledWith(
          expect.objectContaining({ name: 'My Alertmanager', base_url: 'https://alertmanager.example.com' })
        );
      });
    });

    it('calls createJiraConnector with correct payload', async () => {
      mockClient.createJiraConnector.mockResolvedValue({
        id: 'j1', name: 'My Jira', site_url: 'https://myorg.atlassian.net',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Jira/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Jira' } });
      fireEvent.change(screen.getByLabelText(/Jira Site URL/i), { target: { value: 'https://myorg.atlassian.net' } });
      fireEvent.change(screen.getAllByLabelText(/Email/i)[0], { target: { value: 'user@example.com' } });
      fireEvent.change(screen.getByLabelText(/API Token/i), { target: { value: 'jira-token' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createJiraConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            site_url: 'https://myorg.atlassian.net',
            email: 'user@example.com',
            api_token: 'jira-token',
          })
        );
      });
    });

    it('calls createConfluenceConnector with correct payload', async () => {
      mockClient.createConfluenceConnector.mockResolvedValue({
        id: 'cf1', name: 'My Confluence',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Confluence/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Confluence' } });
      fireEvent.change(screen.getByLabelText(/Confluence Site URL/i), { target: { value: 'https://myorg.atlassian.net' } });
      fireEvent.change(screen.getAllByLabelText(/Email/i)[0], { target: { value: 'user@example.com' } });
      fireEvent.change(screen.getByLabelText(/API Token/i), { target: { value: 'conf-token' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createConfluenceConnector).toHaveBeenCalledWith(
          expect.objectContaining({ site_url: 'https://myorg.atlassian.net', api_token: 'conf-token' })
        );
      });
    });

    it('calls createArgoConnector with correct payload', async () => {
      mockClient.createArgoConnector.mockResolvedValue({
        id: 'a1', name: 'My ArgoCD',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^ArgoCD/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My ArgoCD' } });
      fireEvent.change(screen.getByLabelText(/Server URL/i), { target: { value: 'https://argocd.example.com' } });
      fireEvent.change(screen.getByLabelText(/API Token/i), { target: { value: 'argo-token' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createArgoConnector).toHaveBeenCalledWith(
          expect.objectContaining({ server_url: 'https://argocd.example.com', api_token: 'argo-token' })
        );
      });
    });

    it('calls createGitHubConnector with correct payload', async () => {
      mockClient.createGitHubConnector.mockResolvedValue({
        id: 'gh1', name: 'My GitHub', base_url: 'https://api.github.com',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^GitHub/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My GitHub' } });
      fireEvent.change(screen.getByLabelText(/Organization/i), { target: { value: 'my-org' } });
      fireEvent.change(screen.getByLabelText(/Personal Access Token/i), { target: { value: 'ghp_token' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createGitHubConnector).toHaveBeenCalledWith(
          expect.objectContaining({ organization: 'my-org', personal_access_token: 'ghp_token' })
        );
      });
    });

    it('calls createMCPConnector with correct payload', async () => {
      mockClient.createMCPConnector.mockResolvedValue({
        id: 'm1', name: 'My MCP', server_url: 'https://mcp.example.com/mcp',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^MCP/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My MCP' } });
      fireEvent.change(screen.getByLabelText(/Server URL/i), { target: { value: 'https://mcp.example.com/mcp' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createMCPConnector).toHaveBeenCalledWith(
          expect.objectContaining({ server_url: 'https://mcp.example.com/mcp', transport_type: 'streamable_http' })
        );
      });
    });

    it('calls createEmailConnector with SMTP payload', async () => {
      mockClient.createEmailConnector.mockResolvedValue({
        id: 'e1', name: 'My Email',
      });

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);
      fireEvent.click(screen.getByRole('button', { name: /^Email/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), { target: { value: 'My Email' } });
      fireEvent.change(screen.getByLabelText(/From Email/i), { target: { value: 'no-reply@example.com' } });
      fireEvent.change(screen.getByLabelText(/Default Recipients/i), { target: { value: 'ops@example.com' } });
      fireEvent.change(screen.getByLabelText(/SMTP Host/i), { target: { value: 'smtp.example.com' } });
      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(mockClient.createEmailConnector).toHaveBeenCalledWith(
          expect.objectContaining({
            provider_type: 'smtp',
            from_email: 'no-reply@example.com',
            default_recipients: 'ops@example.com',
            smtp_host: 'smtp.example.com',
          })
        );
      });
    });

    it('displays error message when submission fails', async () => {
      mockClient.createKubernetesConnector.mockRejectedValue(
        new Error('Connection refused')
      );

      render(<CreateConnectorModal onClose={mockOnClose} onSuccess={mockOnSuccess} />);

      fireEvent.click(screen.getByRole('button', { name: /K8s/i }));
      fireEvent.change(screen.getByTestId('connector-name-input'), {
        target: { value: 'My K8s' },
      });
      fireEvent.change(screen.getByLabelText(/API Server URL/i), {
        target: { value: 'https://k8s.example.com' },
      });
      fireEvent.change(screen.getByLabelText(/Service Account Token/i), {
        target: { value: 'my-token' },
      });

      fireEvent.click(screen.getByTestId('create-connector-submit-button'));

      await waitFor(() => {
        expect(screen.getByText('Connection refused')).toBeInTheDocument();
      });

      expect(mockOnSuccess).not.toHaveBeenCalled();
    });
  });
});
