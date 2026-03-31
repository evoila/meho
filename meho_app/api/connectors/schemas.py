# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector API schemas.

Pydantic models for connector request/response data structures.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# =============================================================================
# Core Connector Schemas
# =============================================================================


class CreateConnectorRequest(BaseModel):
    """Request to create a new connector."""

    name: str
    base_url: str
    auth_type: str = "API_KEY"  # API_KEY, BASIC, OAUTH2, NONE, SESSION
    description: str | None = None
    routing_description: str | None = None  # For orchestrator LLM routing
    connector_type: Literal[
        "rest",
        "soap",
        "graphql",
        "grpc",
        "vmware",
        "proxmox",
        "kubernetes",
        "gcp",
        "prometheus",
        "loki",
        "tempo",
        "alertmanager",
        "jira",
        "confluence",
        "email",
        "argocd",
        "github",
        "azure",
        "mcp",
        "slack",
    ] = "rest"
    skill_name: str | None = Field(
        default=None,
        description="Skill file name for SpecialistAgent (e.g., 'custom_crm.md'). Defaults to connector type skill.",
    )
    protocol_config: dict[str, Any] | None = None
    # Safety policies
    allowed_methods: list[str] = Field(default=["GET", "POST", "PUT", "PATCH", "DELETE"])
    blocked_methods: list[str] = Field(default_factory=list)
    default_safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    # SESSION auth fields
    login_url: str | None = None
    login_method: str | None = None
    login_config: dict[str, Any] | None = None
    # Related connectors for cross-connector topology correlation
    related_connector_ids: list[str] = Field(default_factory=list)


class ConnectorResponse(BaseModel):
    """Connector response model."""

    id: str
    name: str
    base_url: str
    auth_type: str
    description: str | None
    routing_description: str | None = None  # For orchestrator LLM routing
    tenant_id: str
    connector_type: str = "rest"
    skill_name: str | None = Field(
        default=None,
        description="Skill file name for SpecialistAgent (e.g., 'custom_crm.md'). Defaults to connector type skill.",
    )
    generated_skill: str | None = Field(
        default=None, description="Auto-generated skill from operations pipeline"
    )
    custom_skill: str | None = Field(default=None, description="Operator-customized skill content")
    skill_quality_score: int | None = Field(
        default=None, description="Quality score 1-5 based on operation metadata completeness"
    )
    protocol_config: dict[str, Any] | None = None
    allowed_methods: list[str]
    blocked_methods: list[str]
    default_safety_level: str
    is_active: bool
    # SESSION auth fields
    login_url: str | None = None
    login_method: str | None = None
    login_config: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    # Related connectors for cross-connector topology correlation
    related_connector_ids: list[str] = Field(default_factory=list)

    # Phase 75: automation toggle for automated sessions
    automation_enabled: bool = True

    # Credential masking indicators (Phase 3 - TASK-140)
    # Set to True when superadmin views tenant connector and credentials are masked
    auth_config_masked: bool = False
    login_config_masked: bool = False
    protocol_config_masked: bool = False


class UpdateConnectorRequest(BaseModel):
    """Request to update a connector."""

    name: str | None = None
    description: str | None = None
    routing_description: str | None = None  # For orchestrator LLM routing
    base_url: str | None = None
    auth_type: str | None = None  # API_KEY, BASIC, OAUTH2, NONE, SESSION
    connector_type: (
        Literal[
            "rest",
            "soap",
            "graphql",
            "grpc",
            "vmware",
            "proxmox",
            "kubernetes",
            "gcp",
            "prometheus",
            "loki",
            "tempo",
            "alertmanager",
            "jira",
            "confluence",
            "email",
            "argocd",
            "github",
            "azure",
            "mcp",
        ]
        | None
    ) = None
    skill_name: str | None = Field(
        default=None,
        description="Skill file name for SpecialistAgent (e.g., 'custom_crm.md'). Defaults to connector type skill.",
    )
    custom_skill: str | None = Field(
        default=None, description="Operator-customized skill content (markdown)"
    )
    protocol_config: dict[str, Any] | None = None
    allowed_methods: list[str] | None = None
    blocked_methods: list[str] | None = None
    default_safety_level: Literal["safe", "caution", "dangerous"] | None = None
    is_active: bool | None = None
    # SESSION auth configuration
    login_url: str | None = None
    login_method: str | None = None
    login_config: dict[str, Any] | None = None
    # Related connectors for cross-connector topology correlation
    related_connector_ids: list[str] | None = None
    # Phase 75: automation toggle for automated sessions
    automation_enabled: bool | None = None


# =============================================================================
# Skill API Schemas (Phase 7 - Skill Editor UI)
# =============================================================================


class SaveSkillRequest(BaseModel):
    """Request to save custom skill content."""

    custom_skill: str = Field(..., description="Markdown skill content")


class RegenerateSkillResponse(BaseModel):
    """Response from skill regeneration."""

    generated_skill: str
    skill_quality_score: int | None = None
    operation_count: int


# =============================================================================
# Endpoint Schemas
# =============================================================================


class EndpointResponse(BaseModel):
    """Endpoint response model."""

    id: str
    connector_id: str
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tags: list[str]
    # Enhancement fields
    is_enabled: bool
    safety_level: str
    requires_approval: bool
    custom_description: str | None
    custom_notes: str | None
    usage_examples: dict[str, Any] | None
    last_modified_by: str | None
    last_modified_at: datetime | None
    created_at: datetime
    # Schema fields for data visibility
    path_params_schema: dict[str, Any] | None = None
    query_params_schema: dict[str, Any] | None = None
    body_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    required_params: list[str] | None = None
    # Parameter metadata for LLM guidance
    parameter_metadata: dict[str, Any] | None = None


class UpdateEndpointRequest(BaseModel):
    """Request to update an endpoint."""

    is_enabled: bool | None = None
    safety_level: (
        Literal["safe", "caution", "dangerous", "read", "write", "destructive", "auto"] | None
    ) = None
    requires_approval: bool | None = None
    custom_description: str | None = None
    custom_notes: str | None = None
    usage_examples: dict[str, Any] | None = None


class TestEndpointRequest(BaseModel):
    """Request to test an endpoint."""

    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None
    use_system_credentials: bool = True


class TestEndpointResponse(BaseModel):
    """Response from endpoint test."""

    status_code: int
    headers: dict[str, str]
    body: Any
    duration_ms: int
    error: str | None = None


# =============================================================================
# VMware Connector Schemas
# =============================================================================


class CreateVMwareConnectorRequest(BaseModel):
    """Request to create a VMware vSphere connector."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    vcenter_host: str = Field(..., description="vCenter Server hostname or IP")
    port: int = Field(default=443, description="vCenter Server port")
    disable_ssl_verification: bool = Field(
        default=False,
        description="Disable SSL certificate verification (not recommended for production)",
    )
    username: str = Field(..., description="vCenter username (e.g., administrator@vsphere.local)")
    password: str = Field(..., description="vCenter password")


class VMwareConnectorResponse(BaseModel):
    """Response after creating a VMware connector."""

    id: str
    name: str
    vcenter_host: str
    connector_type: str = "vmware"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Proxmox Connector Schemas
# =============================================================================


class CreateProxmoxConnectorRequest(BaseModel):
    """Request to create a Proxmox VE connector."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    host: str = Field(..., description="Proxmox VE hostname or IP")
    port: int = Field(default=8006, description="Proxmox VE API port")
    disable_ssl_verification: bool = Field(
        default=False,
        description="Disable SSL certificate verification (not recommended for production)",
    )
    # Authentication - either API token or username/password
    # API Token auth (recommended)
    api_token_id: str | None = Field(
        default=None, description="API token ID (e.g., user@realm!tokenname)"
    )
    api_token_secret: str | None = Field(default=None, description="API token secret")
    # Username/password auth
    username: str | None = Field(default=None, description="Username (e.g., root@pam)")
    password: str | None = Field(default=None, description="Password")


class ProxmoxConnectorResponse(BaseModel):
    """Response after creating a Proxmox connector."""

    id: str
    name: str
    host: str
    connector_type: str = "proxmox"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Kubernetes Connector Schemas
# =============================================================================


class CreateKubernetesConnectorRequest(BaseModel):
    """Request to create a Kubernetes connector.

    Creates a REST connector that auto-fetches OpenAPI specs from the
    Kubernetes API server and uses Bearer token authentication.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production K8s cluster in Graz datacenter')",
    )
    server_url: str = Field(
        ..., description="Kubernetes API server URL (e.g., https://10.5.27.3:6443)"
    )
    token: str = Field(..., description="Service Account Bearer token")
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )
    ca_certificate: str | None = Field(
        None, description="Optional CA certificate (PEM format, base64 encoded)"
    )


class KubernetesConnectorResponse(BaseModel):
    """Response after creating a Kubernetes connector."""

    id: str
    name: str
    server_url: str
    connector_type: str = "kubernetes"
    kubernetes_version: str | None = None
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# GCP Connector Schemas
# =============================================================================


class CreateGCPConnectorRequest(BaseModel):
    """Request to create a Google Cloud Platform connector.

    Creates a typed connector using native Google Cloud SDKs for access to:
    - Compute Engine (VMs, disks, snapshots)
    - GKE (Kubernetes clusters)
    - Networking (VPCs, subnets, firewalls)
    - Cloud Monitoring (metrics, alerts)
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    project_id: str = Field(..., description="GCP project ID")
    default_region: str = Field(
        default="us-central1", description="Default region for regional resources"
    )
    default_zone: str = Field(
        default="us-central1-a", description="Default zone for zonal resources (VMs, disks)"
    )
    service_account_json: str = Field(
        ..., description="Service Account JSON key content (raw JSON or base64 encoded)"
    )


class GCPConnectorResponse(BaseModel):
    """Response after creating a GCP connector."""

    id: str
    name: str
    project_id: str
    connector_type: str = "gcp"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Azure Connector Schemas
# =============================================================================


class CreateAzureConnectorRequest(BaseModel):
    """Request to create a Microsoft Azure connector.

    Creates a typed connector using native Azure async SDKs for access to:
    - Compute (VMs, managed disks)
    - Monitor (metrics, alerts, activity log)
    - AKS (Kubernetes clusters, node pools)
    - Networking (VNets, subnets, NSGs, load balancers)
    - Storage (storage accounts, containers)
    - Web (App Service, Function Apps)
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    tenant_id: str = Field(..., description="Azure AD tenant ID")
    client_id: str = Field(..., description="Service principal application (client) ID")
    client_secret: str = Field(..., description="Service principal client secret")
    subscription_id: str = Field(..., description="Azure subscription ID")
    resource_group_filter: str | None = Field(
        None, description="Optional: limit to specific resource group"
    )


class AzureConnectorResponse(BaseModel):
    """Response after creating an Azure connector."""

    id: str
    name: str
    subscription_id: str
    connector_type: str = "azure"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# AWS Connector Schemas
# =============================================================================


class CreateAWSConnectorRequest(BaseModel):
    """Request to create an Amazon Web Services connector."""

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    default_region: str = Field(default="us-east-1", description="Default AWS region")
    aws_access_key_id: str | None = Field(None, description="AWS access key ID (optional if using IAM role)")
    aws_secret_access_key: str | None = Field(
        None, description="AWS secret access key (optional if using IAM role)"
    )


class AWSConnectorResponse(BaseModel):
    """Response after creating an AWS connector."""

    id: str
    name: str
    default_region: str
    connector_type: str = "aws"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Prometheus Connector Schemas
# =============================================================================


class CreatePrometheusConnectorRequest(BaseModel):
    """Request to create a Prometheus connector.

    Creates a typed connector for Prometheus HTTP API access with
    configurable auth (none/basic/bearer) and test connection.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production Prometheus monitoring K8s cluster')",
    )
    base_url: str = Field(..., description="Prometheus server URL (e.g., http://prometheus:9090)")
    auth_type: str = Field(
        default="none", description="Authentication type: none, basic, or bearer"
    )
    username: str | None = Field(None, description="Username for basic auth")
    password: str | None = Field(None, description="Password for basic auth")
    token: str | None = Field(None, description="Bearer token for bearer auth")
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )


class PrometheusConnectorResponse(BaseModel):
    """Response after creating a Prometheus connector."""

    id: str
    name: str
    base_url: str
    connector_type: str = "prometheus"
    prometheus_version: str | None = None
    auth_type: str
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Loki Connector Schemas
# =============================================================================


class CreateLokiConnectorRequest(BaseModel):
    """Request to create a Loki connector.

    Creates a typed connector for Loki HTTP API access with
    configurable auth (none/basic/bearer) and test connection.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production Loki receiving logs from K8s cluster')",
    )
    base_url: str = Field(..., description="Loki server URL (e.g., http://loki:3100)")
    auth_type: str = Field(
        default="none", description="Authentication type: none, basic, or bearer"
    )
    username: str | None = Field(None, description="Username for basic auth")
    password: str | None = Field(None, description="Password for basic auth")
    token: str | None = Field(None, description="Bearer token for bearer auth")
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )


class LokiConnectorResponse(BaseModel):
    """Response after creating a Loki connector."""

    id: str
    name: str
    base_url: str
    connector_type: str = "loki"
    loki_version: str | None = None
    auth_type: str
    operations_registered: int
    message: str


# =============================================================================
# Tempo Connector Schemas
# =============================================================================


class CreateTempoConnectorRequest(BaseModel):
    """Request to create a Tempo connector.

    Creates a typed connector for Tempo HTTP API access with
    configurable auth (none/basic/bearer), multi-tenant org_id,
    and test connection.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production Tempo receiving traces from K8s microservices')",
    )
    base_url: str = Field(..., description="Tempo server URL (e.g., http://tempo:3200)")
    auth_type: str = Field(
        default="none", description="Authentication type: none, basic, or bearer"
    )
    username: str | None = Field(None, description="Username for basic auth")
    password: str | None = Field(None, description="Password for basic auth")
    token: str | None = Field(None, description="Bearer token for bearer auth")
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )
    org_id: str | None = Field(
        None, description="Tenant org ID for multi-tenant Tempo (sets X-Scope-OrgID header)"
    )


class TempoConnectorResponse(BaseModel):
    """Response after creating a Tempo connector."""

    id: str
    name: str
    base_url: str
    connector_type: str = "tempo"
    tempo_version: str | None = None
    auth_type: str
    operations_registered: int
    message: str


# =============================================================================
# Alertmanager Connector Schemas
# =============================================================================


class CreateAlertmanagerConnectorRequest(BaseModel):
    """Request to create an Alertmanager connector.

    Creates a typed connector for Alertmanager v2 HTTP API access with
    configurable auth (none/basic/bearer) and test connection.
    No org_id field -- Alertmanager does not use multi-tenant headers.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production Alertmanager managing K8s cluster alerts')",
    )
    base_url: str = Field(
        ..., description="Alertmanager server URL (e.g., http://alertmanager:9093)"
    )
    auth_type: str = Field(
        default="none", description="Authentication type: none, basic, or bearer"
    )
    username: str | None = Field(None, description="Username for basic auth")
    password: str | None = Field(None, description="Password for basic auth")
    token: str | None = Field(None, description="Bearer token for bearer auth")
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )


class AlertmanagerConnectorResponse(BaseModel):
    """Response after creating an Alertmanager connector."""

    id: str
    name: str
    base_url: str
    connector_type: str = "alertmanager"
    alertmanager_version: str | None = None
    auth_type: str
    operations_registered: int
    message: str


# =============================================================================
# Jira Connector Schemas
# =============================================================================


class CreateJiraConnectorRequest(BaseModel):
    """Request to create a Jira Cloud connector.

    Creates a typed connector for Jira Cloud REST API v3 access using
    email + API token Basic Auth.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Production Jira tracking engineering tasks')",
    )
    site_url: str = Field(
        ..., description="Jira Cloud site URL (e.g., https://yoursite.atlassian.net)"
    )
    email: str = Field(..., description="Atlassian account email")
    api_token: str = Field(..., description="Atlassian API token")


class JiraConnectorResponse(BaseModel):
    """Response after creating a Jira connector."""

    id: str
    name: str
    site_url: str
    connector_type: str = "jira"
    jira_user: str | None = None
    accessible_projects: int = 0
    operations_registered: int = 0
    message: str


# =============================================================================
# Confluence Connector Schemas
# =============================================================================


class CreateConfluenceConnectorRequest(BaseModel):
    """Request to create a Confluence Cloud connector.

    Creates a typed connector for Confluence Cloud REST API v2 access using
    email + API token Basic Auth. Same Atlassian site as Jira.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Engineering documentation wiki')",
    )
    site_url: str = Field(
        ..., description="Atlassian Cloud site URL (e.g., https://yoursite.atlassian.net)"
    )
    email: str = Field(..., description="Atlassian account email")
    api_token: str = Field(..., description="Atlassian API token")


class ConfluenceConnectorResponse(BaseModel):
    """Response after creating a Confluence connector."""

    id: str
    name: str
    site_url: str
    connector_type: str = "confluence"
    confluence_user: str | None = None
    accessible_spaces: int = 0
    operations_registered: int = 0
    message: str


# =============================================================================
# ArgoCD Connector Schemas
# =============================================================================


class CreateArgoConnectorRequest(BaseModel):
    """Request to create an ArgoCD connector.

    Creates a typed connector for ArgoCD REST API access using
    Bearer token (PAT) authentication with configurable SSL verification.
    """

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    routing_description: str | None = None
    server_url: str = Field(..., description="ArgoCD server URL (e.g., https://argocd.example.com)")
    api_token: str = Field(
        ...,
        description="ArgoCD API token (generated via 'argocd account generate-token' or the UI)",
    )
    skip_tls_verification: bool = Field(
        default=False, description="Skip TLS certificate verification (for self-signed certs)"
    )


class ArgoConnectorResponse(BaseModel):
    """Response after creating an ArgoCD connector."""

    id: str
    name: str
    server_url: str
    connector_type: str = "argocd"
    operations_registered: int = 0
    message: str


# =============================================================================
# GitHub Connector Schemas
# =============================================================================


class CreateGitHubConnectorRequest(BaseModel):
    """Request to create a GitHub connector.

    Creates a typed connector for GitHub REST API access using
    PAT Bearer token authentication with rate limit tracking.
    """

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    routing_description: str | None = None
    organization: str = Field(..., description="GitHub organization name")
    personal_access_token: str = Field(
        ..., description="GitHub Personal Access Token (Classic PAT with repo, read:org scopes)"
    )
    base_url: str | None = Field(
        default="https://api.github.com",
        description="GitHub API base URL (use default for github.com, change for GitHub Enterprise)",
    )


class GitHubConnectorResponse(BaseModel):
    """Response after creating a GitHub connector."""

    id: str
    name: str
    base_url: str
    organization: str
    connector_type: str = "github"
    operations_registered: int = 0
    message: str


# =============================================================================
# MCP Connector Schemas (Phase 93)
# =============================================================================


class CreateMCPConnectorRequest(BaseModel):
    """Request to create an MCP connector (connects to external MCP server)."""

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    server_url: str | None = Field(None, description="MCP server URL (required for Streamable HTTP)")
    transport_type: Literal["streamable_http", "stdio"] = Field(
        default="streamable_http", description="Transport type"
    )
    command: str | None = Field(None, description="Command to run (required for stdio transport)")
    args: list[str] = Field(default_factory=list, description="Command arguments (for stdio)")
    api_key: str | None = Field(None, description="API key for authenticating with the MCP server")


class MCPConnectorResponse(BaseModel):
    """Response after creating an MCP connector."""

    id: str
    name: str
    server_url: str | None
    transport_type: str
    connector_type: str = "mcp"
    tools_discovered: int
    operations_registered: int
    message: str


# =============================================================================
# Slack Connector Schemas (Phase 94.1)
# =============================================================================


class CreateSlackConnectorRequest(BaseModel):
    """Request to create a Slack connector."""

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    slack_bot_token: str = Field(..., description="Slack bot token (xoxb-*)")
    slack_app_token: str | None = Field(
        None, description="Slack app token (xapp-* for Socket Mode)"
    )
    slack_user_token: str | None = Field(
        None, description="Slack user token (xoxp-* for search.messages)"
    )


class SlackConnectorResponse(BaseModel):
    """Response after creating a Slack connector."""

    id: str
    name: str
    connector_type: str = "slack"
    operations_registered: int
    types_registered: int
    message: str


# =============================================================================
# Email Connector Schemas
# =============================================================================


class CreateEmailConnectorRequest(BaseModel):
    """Request to create an Email connector.

    Creates a typed connector for sending branded HTML email notifications.
    Supports 5 provider types: SMTP, SendGrid, Mailgun, Amazon SES, Generic HTTP.
    All provider-specific fields are optional -- only the fields for the selected
    provider_type are required.
    """

    name: str = Field(..., min_length=1, max_length=255, description="Connector display name")
    description: str | None = Field(None, description="Optional description")
    routing_description: str | None = Field(
        None,
        description="Description for the orchestrator to route queries (e.g., 'Send investigation reports to SRE team')",
    )
    provider_type: Literal["smtp", "sendgrid", "mailgun", "ses", "generic_http"] = Field(
        ..., description="Email provider type"
    )
    from_email: str = Field(..., description="Sender email address (e.g., meho@company.com)")
    from_name: str | None = Field(
        default="MEHO", description="Sender display name (e.g., 'MEHO Alerts')"
    )
    default_recipients: str = Field(
        ..., description="Comma-separated list of default recipient email addresses"
    )

    # SMTP provider fields
    smtp_host: str | None = Field(None, description="SMTP server hostname")
    smtp_port: int | None = Field(
        default=587, description="SMTP server port (587=STARTTLS, 465=TLS)"
    )
    smtp_tls: bool | None = Field(default=False, description="Use direct TLS (port 465)")
    smtp_username: str | None = Field(None, description="SMTP username")
    smtp_password: str | None = Field(None, description="SMTP password")

    # SendGrid provider fields
    sendgrid_api_key: str | None = Field(None, description="SendGrid API key")

    # Mailgun provider fields
    mailgun_api_key: str | None = Field(None, description="Mailgun API key")
    mailgun_domain: str | None = Field(None, description="Mailgun sending domain")

    # SES provider fields
    ses_access_key: str | None = Field(None, description="AWS access key ID")
    ses_secret_key: str | None = Field(None, description="AWS secret access key")
    ses_region: str | None = Field(
        default="us-east-1", description="AWS region for SES SMTP endpoint"
    )

    # Generic HTTP provider fields
    http_endpoint_url: str | None = Field(None, description="HTTP endpoint URL for sending email")
    http_auth_header: str | None = Field(None, description="Authorization header value")
    http_payload_template: str | None = Field(
        None,
        description="Jinja2 template for the HTTP payload (receives subject, html_body, text_body, to_emails, from_email)",
    )


class EmailConnectorResponse(BaseModel):
    """Response after creating an Email connector."""

    id: str
    name: str
    connector_type: Literal["email"] = "email"
    provider_type: str
    from_email: str
    test_email_sent: bool = False
    operations_registered: int = 0
    message: str


# =============================================================================
# Connection/Auth Testing Schemas
# =============================================================================


class TestConnectionRequest(BaseModel):
    """Request to test connection with optional credentials."""

    credentials: dict[str, str] | None = None  # Test with these credentials (not saved)
    use_stored_credentials: bool = True  # If True, use user's stored credentials


class TestConnectionResponse(BaseModel):
    """Response from connection test."""

    success: bool
    message: str
    response_time_ms: int | None = None
    tested_endpoint: str | None = None
    status_code: int | None = None
    error_detail: str | None = None


class TestAuthRequest(BaseModel):
    """Request to test authentication flow."""

    credentials: dict[str, str] | None = None  # For USER_PROVIDED strategy


class TestAuthResponse(BaseModel):
    """Response from authentication test."""

    success: bool
    message: str
    auth_type: str
    session_token_obtained: bool | None = None
    session_expires_at: datetime | None = None
    error_detail: str | None = None
    # Debug info
    request_url: str | None = None
    request_method: str | None = None
    response_status: int | None = None
    response_time_ms: int | None = None


# =============================================================================
# Operations Schemas
# =============================================================================


class ConnectorOperationResponse(BaseModel):
    """Response for a connector operation (VMware, etc.)."""

    id: str | None = None
    operation_id: str
    name: str
    description: str | None = None
    category: str | None = None
    parameters: list[dict[str, Any]] = []
    example: str | None = None
    # Operation inheritance (Phase 65)
    # Shows source for frontend badge rendering: "Inherited from Kubernetes" vs "Custom"
    source: str | None = Field(
        default=None,
        description="Operation source: 'type' = inherited from type-level definition, 'custom' = instance-specific",
    )
    is_enabled: bool | None = None
    safety_level: str | None = None


class CreateCustomOperationRequest(BaseModel):
    """Request to create a custom instance-level operation."""

    operation_id: str = Field(
        ..., description="Unique operation identifier (e.g., 'custom_health_check')"
    )
    name: str = Field(..., description="Human-readable operation name")
    description: str | None = None
    category: str | None = None
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    example: str | None = None
    safety_level: str = Field(default="safe")
    requires_approval: bool = Field(default=False)


class OverrideOperationRequest(BaseModel):
    """Request to override a type-level operation for this instance."""

    name: str | None = None
    description: str | None = None
    category: str | None = None
    parameters: list[dict[str, Any]] | None = None
    example: str | None = None
    safety_level: str | None = None
    requires_approval: bool | None = None
    is_enabled_override: bool | None = None


class SyncOperationsResponse(BaseModel):
    """Response for syncing connector operations."""

    connector_id: str
    operations_added: int
    operations_updated: int
    operations_total: int
    message: str


# =============================================================================
# Schema Type Schemas
# =============================================================================


class SchemaTypeResponse(BaseModel):
    """Response for an OpenAPI schema type."""

    type_name: str
    description: str | None = None
    category: str | None = None
    properties: list[dict[str, Any]] = []


# =============================================================================
# SOAP/WSDL Schemas
# =============================================================================


class IngestWSDLRequest(BaseModel):
    """Request to ingest a WSDL file."""

    wsdl_url: str = Field(..., description="URL or path to WSDL file")


class SOAPOperationResponse(BaseModel):
    """SOAP operation response."""

    name: str
    service_name: str
    port_name: str
    operation_name: str
    description: str | None = None
    soap_action: str | None = None
    style: str = "document"
    namespace: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True


class IngestWSDLResponse(BaseModel):
    """Response from WSDL ingestion."""

    message: str
    wsdl_url: str
    operations_count: int
    types_count: int = 0
    services: list[str]
    ports: list[str]


class CallSOAPRequest(BaseModel):
    """Request to call a SOAP operation."""

    params: dict[str, Any] = Field(default_factory=dict, description="Operation parameters")
    service_name: str | None = None
    port_name: str | None = None


class CallSOAPResponse(BaseModel):
    """Response from SOAP operation call."""

    success: bool
    status_code: int
    body: dict[str, Any]
    fault_code: str | None = None
    fault_string: str | None = None
    duration_ms: float | None = None


class SOAPTypeResponse(BaseModel):
    """Response for a SOAP type definition."""

    type_name: str
    namespace: str | None = None
    base_type: str | None = None
    properties: list[dict[str, Any]] = []
    description: str | None = None


# =============================================================================
# Export/Import Schemas
# =============================================================================


class ExportConnectorsRequest(BaseModel):
    """Request to export connectors to encrypted file."""

    connector_ids: list[str] = Field(
        default_factory=list, description="Connector IDs to export (empty = export all)"
    )
    password: str = Field(
        ..., min_length=8, description="Encryption password (minimum 8 characters)"
    )
    format: Literal["json", "yaml"] = Field(default="json", description="Output format")


class ImportConnectorsRequest(BaseModel):
    """Request to import connectors from encrypted file."""

    file_content: str = Field(..., description="Base64-encoded file content")
    password: str = Field(..., min_length=8, description="Decryption password")
    conflict_strategy: Literal["skip", "overwrite", "rename"] = Field(
        default="skip", description="How to handle name conflicts: skip, overwrite, or rename"
    )


class ImportConnectorsResponse(BaseModel):
    """Response from import operation."""

    imported: int = Field(description="Number of connectors imported")
    skipped: int = Field(description="Number of connectors skipped (conflicts)")
    errors: list[str] = Field(default_factory=list, description="Error messages")
    connectors: list[str] = Field(default_factory=list, description="Names of imported connectors")
    # Phase 9: Operations sync results
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings (e.g., operations sync failed but connector imported)",
    )
    operations_synced: int = Field(
        default=0, description="Total operations synced across all imported connectors"
    )
