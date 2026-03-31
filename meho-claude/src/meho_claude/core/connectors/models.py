"""Pydantic v2 models for connector configuration and operations.

ConnectorConfig validates YAML connector configs.
Operation represents the universal operation model stored in meho.db.
AuthConfig defines authentication parameters for each connector.
TrustOverride allows per-operation trust tier overrides.
"""

from typing import Literal

from pydantic import BaseModel, Field


class AuthConfig(BaseModel):
    """Authentication configuration for a connector."""

    method: Literal["bearer", "basic", "api_key", "oauth2_client_credentials"]
    credential_name: str
    header_name: str | None = None
    in_query: bool = False
    query_param: str | None = None
    token_url: str | None = None


class TrustOverride(BaseModel):
    """Per-operation trust tier override."""

    operation_id: str
    trust_tier: Literal["READ", "WRITE", "DESTRUCTIVE"]


class ConnectorConfig(BaseModel):
    """Connector configuration validated from YAML files.

    Each YAML file in ~/.meho/connectors/ maps to one ConnectorConfig.
    """

    name: str
    connector_type: Literal["rest", "kubernetes", "vmware", "proxmox", "gcp", "soap"]
    description: str = ""
    base_url: str = ""
    spec_url: str | None = None
    spec_path: str | None = None
    auth: AuthConfig | None = None
    trust_overrides: list[TrustOverride] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    timeout: int = 30
    kubeconfig_path: str | None = None
    kubeconfig_context: str | None = None
    verify_ssl: bool = True

    # GCP-specific
    project_id: str | None = None
    service_account_path: str | None = None

    # Proxmox-specific (token secret stored in CredentialManager)
    proxmox_token_id: str | None = None


class Operation(BaseModel):
    """Universal operation model — every connector type produces these.

    Stored in meho.db operations table, searchable via FTS5 + ChromaDB.
    """

    connector_name: str
    operation_id: str
    display_name: str
    description: str = ""
    trust_tier: Literal["READ", "WRITE", "DESTRUCTIVE"] = "READ"
    http_method: str | None = None
    url_template: str | None = None
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    example_params: dict = Field(default_factory=dict)
    related_operations: list[str] = Field(default_factory=list)
