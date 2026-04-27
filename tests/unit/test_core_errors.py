# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.core.errors
"""

import pytest

from meho_app.core.errors import (
    AuthError,
    ConfigError,
    CredentialError,
    IngestionError,
    MehoError,
    NotFoundError,
    UpstreamApiError,
    ValidationError,
    VectorStoreError,
    WorkflowError,
)


@pytest.mark.unit
def test_meho_error_base():
    """Test MehoError base exception"""
    error = MehoError("Test error message")

    assert str(error) == "Test error message"
    assert error.code == "MEHO_ERROR"
    assert error.message == "Test error message"


@pytest.mark.unit
def test_meho_error_with_details():
    """Test MehoError with additional details"""
    error = MehoError("Error", key1="value1", key2="value2")

    assert error.details == {"key1": "value1", "key2": "value2"}


@pytest.mark.unit
def test_meho_error_to_dict():
    """Test MehoError.to_dict() serialization"""
    error = MehoError("Test error", foo="bar")

    error_dict = error.to_dict()

    assert error_dict["code"] == "MEHO_ERROR"
    assert error_dict["message"] == "Test error"
    assert error_dict["details"] == {"foo": "bar"}


@pytest.mark.unit
def test_config_error():
    """Test ConfigError"""
    error = ConfigError("Config is invalid")

    assert isinstance(error, MehoError)
    assert error.code == "CONFIG_ERROR"
    assert str(error) == "Config is invalid"


@pytest.mark.unit
def test_auth_error():
    """Test AuthError"""
    error = AuthError("Authentication failed")

    assert isinstance(error, MehoError)
    assert error.code == "AUTH_ERROR"


@pytest.mark.unit
def test_not_found_error():
    """Test NotFoundError"""
    error = NotFoundError("Resource not found")

    assert isinstance(error, MehoError)
    assert error.code == "NOT_FOUND"


@pytest.mark.unit
def test_validation_error():
    """Test ValidationError"""
    error = ValidationError("Invalid data")

    assert isinstance(error, MehoError)
    assert error.code == "VALIDATION_ERROR"


@pytest.mark.unit
def test_upstream_api_error_basic():
    """Test UpstreamApiError with basic fields"""
    error = UpstreamApiError(status_code=404, url="https://api.example.com/endpoint")

    assert isinstance(error, MehoError)
    assert error.code == "UPSTREAM_API_ERROR"
    assert error.status_code == 404
    assert error.url == "https://api.example.com/endpoint"
    assert error.payload is None


@pytest.mark.unit
def test_upstream_api_error_with_payload():
    """Test UpstreamApiError with payload"""
    payload = {"error": "not found", "code": 404}
    error = UpstreamApiError(
        status_code=404, url="https://api.example.com/endpoint", payload=payload
    )

    assert error.status_code == 404
    assert error.url == "https://api.example.com/endpoint"
    assert error.payload == payload


@pytest.mark.unit
def test_upstream_api_error_to_dict():
    """Test UpstreamApiError serialization includes extra fields"""
    error = UpstreamApiError(
        status_code=500, url="https://api.example.com/endpoint", payload={"error": "server error"}
    )

    error_dict = error.to_dict()

    assert error_dict["code"] == "UPSTREAM_API_ERROR"
    assert error_dict["status_code"] == 500
    assert error_dict["url"] == "https://api.example.com/endpoint"


@pytest.mark.unit
def test_vector_store_error():
    """Test VectorStoreError"""
    error = VectorStoreError("Vector operation failed")

    assert isinstance(error, MehoError)
    assert error.code == "VECTOR_STORE_ERROR"


@pytest.mark.unit
def test_workflow_error():
    """Test WorkflowError"""
    error = WorkflowError("Workflow failed")

    assert isinstance(error, MehoError)
    assert error.code == "WORKFLOW_ERROR"


@pytest.mark.unit
def test_ingestion_error():
    """Test IngestionError"""
    error = IngestionError("Ingestion failed")

    assert isinstance(error, MehoError)
    assert error.code == "INGESTION_ERROR"


@pytest.mark.unit
def test_credential_error():
    """Test CredentialError"""
    error = CredentialError("Credential operation failed")

    assert isinstance(error, MehoError)
    assert error.code == "CREDENTIAL_ERROR"


@pytest.mark.unit
def test_error_can_be_caught():
    """Test that errors can be raised and caught"""
    with pytest.raises(NotFoundError):
        raise NotFoundError("Resource not found")

    with pytest.raises(MehoError):
        raise NotFoundError("Resource not found")


@pytest.mark.unit
def test_error_repr():
    """Test error __repr__ method"""
    error = ConfigError("Invalid config", source="env")

    repr_str = repr(error)
    assert "ConfigError" in repr_str
    assert "Invalid config" in repr_str
