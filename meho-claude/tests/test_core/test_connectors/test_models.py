"""Tests for connector framework Pydantic models."""

import pytest
from pydantic import ValidationError

from meho_claude.core.connectors.models import (
    AuthConfig,
    ConnectorConfig,
    Operation,
    TrustOverride,
)


# --- AuthConfig ---


class TestAuthConfig:
    def test_valid_bearer(self):
        cfg = AuthConfig(method="bearer", credential_name="my-api")
        assert cfg.method == "bearer"
        assert cfg.credential_name == "my-api"

    def test_valid_basic(self):
        cfg = AuthConfig(method="basic", credential_name="my-creds")
        assert cfg.method == "basic"

    def test_valid_api_key_header(self):
        cfg = AuthConfig(method="api_key", credential_name="key-store", header_name="X-Custom-Key")
        assert cfg.header_name == "X-Custom-Key"
        assert cfg.in_query is False

    def test_valid_api_key_query(self):
        cfg = AuthConfig(
            method="api_key",
            credential_name="key-store",
            in_query=True,
            query_param="api_key",
        )
        assert cfg.in_query is True
        assert cfg.query_param == "api_key"

    def test_valid_oauth2(self):
        cfg = AuthConfig(
            method="oauth2_client_credentials",
            credential_name="oauth-creds",
            token_url="https://auth.example.com/token",
        )
        assert cfg.token_url == "https://auth.example.com/token"

    def test_rejects_invalid_method(self):
        with pytest.raises(ValidationError):
            AuthConfig(method="cookie", credential_name="x")

    def test_requires_credential_name(self):
        with pytest.raises(ValidationError):
            AuthConfig(method="bearer")


# --- TrustOverride ---


class TestTrustOverride:
    def test_valid_override(self):
        o = TrustOverride(operation_id="delete_vm", trust_tier="DESTRUCTIVE")
        assert o.operation_id == "delete_vm"
        assert o.trust_tier == "DESTRUCTIVE"

    def test_rejects_invalid_tier(self):
        with pytest.raises(ValidationError):
            TrustOverride(operation_id="x", trust_tier="ADMIN")


# --- ConnectorConfig ---


class TestConnectorConfig:
    @pytest.fixture()
    def valid_auth(self):
        return AuthConfig(method="bearer", credential_name="my-api")

    def test_valid_rest_config(self, valid_auth):
        cfg = ConnectorConfig(
            name="my-rest-api",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.name == "my-rest-api"
        assert cfg.connector_type == "rest"
        assert cfg.timeout == 30
        assert cfg.trust_overrides == []
        assert cfg.tags == {}
        assert cfg.description == ""

    def test_all_connector_types(self, valid_auth):
        for ct in ("rest", "kubernetes", "vmware", "proxmox", "gcp", "soap"):
            cfg = ConnectorConfig(
                name=f"test-{ct}",
                connector_type=ct,
                base_url="https://x.example.com",
                auth=valid_auth,
            )
            assert cfg.connector_type == ct

    def test_rejects_invalid_connector_type(self, valid_auth):
        with pytest.raises(ValidationError):
            ConnectorConfig(
                name="bad",
                connector_type="graphql",
                base_url="https://x.example.com",
                auth=valid_auth,
            )

    def test_requires_name(self, valid_auth):
        with pytest.raises(ValidationError):
            ConnectorConfig(
                connector_type="rest",
                base_url="https://x.example.com",
                auth=valid_auth,
            )

    def test_base_url_defaults_to_empty_string(self, valid_auth):
        cfg = ConnectorConfig(
            name="no-url",
            connector_type="kubernetes",
            auth=valid_auth,
        )
        assert cfg.base_url == ""

    def test_kubernetes_config_with_kubeconfig_fields(self):
        cfg = ConnectorConfig(
            name="k8s-prod",
            connector_type="kubernetes",
            kubeconfig_path="/home/user/.kube/config",
            kubeconfig_context="prod-cluster",
        )
        assert cfg.kubeconfig_path == "/home/user/.kube/config"
        assert cfg.kubeconfig_context == "prod-cluster"
        assert cfg.auth is None
        assert cfg.base_url == ""

    def test_kubernetes_config_with_auth(self):
        auth = AuthConfig(method="bearer", credential_name="k8s-token")
        cfg = ConnectorConfig(
            name="k8s-staging",
            connector_type="kubernetes",
            auth=auth,
        )
        assert cfg.auth is not None
        assert cfg.auth.credential_name == "k8s-token"

    def test_vmware_config_with_verify_ssl(self):
        auth = AuthConfig(method="basic", credential_name="vcenter-creds")
        cfg = ConnectorConfig(
            name="vcenter-prod",
            connector_type="vmware",
            base_url="vcenter.local",
            verify_ssl=False,
            auth=auth,
        )
        assert cfg.verify_ssl is False

    def test_verify_ssl_defaults_true(self, valid_auth):
        cfg = ConnectorConfig(
            name="default-ssl",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.verify_ssl is True

    def test_auth_optional_defaults_none(self):
        cfg = ConnectorConfig(
            name="kubeconfig-only",
            connector_type="kubernetes",
        )
        assert cfg.auth is None

    def test_kubeconfig_fields_default_none(self, valid_auth):
        cfg = ConnectorConfig(
            name="rest-api",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.kubeconfig_path is None
        assert cfg.kubeconfig_context is None

    def test_rest_backward_compat_with_explicit_base_url_and_auth(self, valid_auth):
        """Existing REST configs with base_url and auth still work."""
        cfg = ConnectorConfig(
            name="my-rest-api",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.base_url == "https://api.example.com"
        assert cfg.auth is not None

    def test_trust_overrides(self, valid_auth):
        cfg = ConnectorConfig(
            name="with-overrides",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
            trust_overrides=[
                TrustOverride(operation_id="delete_vm", trust_tier="DESTRUCTIVE"),
            ],
        )
        assert len(cfg.trust_overrides) == 1
        assert cfg.trust_overrides[0].trust_tier == "DESTRUCTIVE"

    def test_optional_spec_fields(self, valid_auth):
        cfg = ConnectorConfig(
            name="with-spec",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
            spec_url="https://api.example.com/openapi.json",
            spec_path="/tmp/spec.yaml",
        )
        assert cfg.spec_url == "https://api.example.com/openapi.json"
        assert cfg.spec_path == "/tmp/spec.yaml"

    # --- Phase 7: GCP, Proxmox, SOAP config fields ---

    def test_gcp_config_with_project_id(self):
        """GCP connector config with project_id validates successfully."""
        cfg = ConnectorConfig(
            name="gcp-prod",
            connector_type="gcp",
            project_id="my-project",
        )
        assert cfg.project_id == "my-project"
        assert cfg.service_account_path is None

    def test_gcp_config_with_service_account_path(self):
        """GCP connector config with service account JSON path."""
        cfg = ConnectorConfig(
            name="gcp-dev",
            connector_type="gcp",
            project_id="dev-project",
            service_account_path="/home/user/.gcp/sa-key.json",
        )
        assert cfg.service_account_path == "/home/user/.gcp/sa-key.json"

    def test_proxmox_config_with_token_id(self):
        """Proxmox connector config with proxmox_token_id validates."""
        cfg = ConnectorConfig(
            name="prox-1",
            connector_type="proxmox",
            base_url="10.0.0.1",
            proxmox_token_id="user@pam!meho",
        )
        assert cfg.proxmox_token_id == "user@pam!meho"

    def test_soap_config_reuses_spec_url_for_wsdl(self):
        """SOAP connector reuses spec_url for WSDL location."""
        cfg = ConnectorConfig(
            name="sap",
            connector_type="soap",
            spec_url="https://sap.example.com/service?wsdl",
        )
        assert cfg.spec_url == "https://sap.example.com/service?wsdl"

    def test_new_fields_default_none(self, valid_auth):
        """New Phase 7 fields default to None (backward compatible)."""
        cfg = ConnectorConfig(
            name="rest-api",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.project_id is None
        assert cfg.service_account_path is None
        assert cfg.proxmox_token_id is None

    def test_backward_compat_existing_configs_still_work(self, valid_auth):
        """Existing connector configs with no new fields still validate."""
        # REST
        cfg = ConnectorConfig(
            name="legacy-rest",
            connector_type="rest",
            base_url="https://api.example.com",
            auth=valid_auth,
        )
        assert cfg.name == "legacy-rest"

        # Kubernetes
        cfg2 = ConnectorConfig(
            name="k8s",
            connector_type="kubernetes",
            kubeconfig_path="/home/user/.kube/config",
        )
        assert cfg2.kubeconfig_path == "/home/user/.kube/config"

        # VMware
        cfg3 = ConnectorConfig(
            name="vcenter",
            connector_type="vmware",
            base_url="vcenter.local",
            auth=AuthConfig(method="basic", credential_name="vc"),
        )
        assert cfg3.verify_ssl is True


# --- Operation ---


class TestOperation:
    def test_valid_operation(self):
        op = Operation(
            connector_name="my-api",
            operation_id="list_users",
            display_name="List Users",
        )
        assert op.connector_name == "my-api"
        assert op.operation_id == "list_users"
        assert op.trust_tier == "READ"
        assert op.tags == []
        assert op.input_schema == {}
        assert op.output_schema == {}
        assert op.example_params == {}
        assert op.related_operations == []

    def test_full_operation(self):
        op = Operation(
            connector_name="my-api",
            operation_id="delete_user",
            display_name="Delete User",
            description="Permanently deletes a user",
            trust_tier="DESTRUCTIVE",
            http_method="DELETE",
            url_template="/users/{user_id}",
            input_schema={"user_id": {"type": "string"}},
            output_schema={"status": {"type": "string"}},
            tags=["user", "admin"],
            example_params={"user_id": "abc123"},
            related_operations=["list_users", "get_user"],
        )
        assert op.trust_tier == "DESTRUCTIVE"
        assert op.http_method == "DELETE"
        assert len(op.tags) == 2

    def test_rejects_invalid_trust_tier(self):
        with pytest.raises(ValidationError):
            Operation(
                connector_name="x",
                operation_id="x",
                display_name="x",
                trust_tier="ADMIN",
            )
