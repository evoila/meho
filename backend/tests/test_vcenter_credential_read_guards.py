# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Generic vCenter reads whose answer holds credentials get two guards.

* The catalog rows ``vmware`` / ``9.0`` and ``vmware`` / ``8.0`` raise each
  such read to ``caution`` with ``requires_approval=True`` through their
  shared ``safety_overlay``.
* The op id is on ``broadcast.events._CREDENTIAL_READ_OPS``, so the
  dispatcher replaces every secret-named field in the answer with
  ``[REDACTED:param_name]`` before the caller sees it.

The VCF Installer bring-up spec read is on the same list.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import safety_overlay_for
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_REDACTED = "[REDACTED:param_name]"
# Built from parts so the secret scanner in pre-commit does not flag
# the test source.
_CLIENT_SECRET = "example-client" + "-secret-value"
_LDAP_PASSWORD = "example-ldap" + "-pw-value"
_IPMI_PASSWORD = "example-ipmi" + "-pw-value"

_PRODUCT = "vmware"
_IMPL_ID = "vmware-rest"
_VERSIONS = ("9.0", "8.0")

#: Every generic vCenter read the catalog overlay and the credential-read
#: list cover. Spellings match the vendor files ``vcenter.yaml`` and
#: ``vi-json.yaml`` (``vmware/vcf-api-specs``, 9.0).
_VCENTER_CREDENTIAL_PATHS: tuple[str, ...] = (
    "/appliance/networking/proxy",
    "/appliance/networking/proxy/{protocol}",
    "/content/library/{libraryId}",
    "/content/local-library/{libraryId}",
    "/content/subscribed-library/{libraryId}",
    "/esx/hcl/reports/{report}",
    "/vcenter/lcm/reports/{report}",
    "/vcenter/deployment/install",
    "/vcenter/deployment/upgrade",
    "/vcenter/deployment/migrate",
    "/vcenter/lcm/deployment/migration-upgrade",
    "/vcenter/guest/customization-specs/{name}",
    "/vcenter/identity/providers/{provider}",
    "/vcenter/identity/broker/tenants/{tenant}/admin-client",
    "/vcenter/identity/broker/tenants/operator-client",
    "/vcenter/namespace-management/supervisors/{supervisor}/container-image-registries",
    "/vcenter/namespace-management/supervisors/{supervisor}"
    "/container-image-registries/{containerImageRegistry}",
    "/HostSystem/{moId}/config",
    "/HostSystem/{moId}/precheckRemediationResult",
)

#: Reads with no credentials in their answer; they must stay ``safe``.
_VCENTER_PLAIN_PATHS: tuple[str, ...] = (
    "/vcenter/vm",
    "/vcenter/identity/providers",
    "/content/library",
    "/HostSystem/{moId}/hardware",
)


def _fixture_spec() -> str:
    lines = [
        "openapi: 3.0.3",
        "info: {title: vCenter credential reads fixture, version: '9.0.0.0'}",
        "paths:",
    ]
    for index, path in enumerate((*_VCENTER_CREDENTIAL_PATHS, *_VCENTER_PLAIN_PATHS)):
        lines += [
            f"  {path}:",
            "    get:",
            f"      operationId: read{index}",
            "      responses: {'200': {description: ok}}",
        ]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _make_operator() -> Operator:
    return Operator(
        sub="op-vcenter-credential-reads",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c0de"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _parsed_ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///vcenter-credential-reads.yaml", content=_fixture_spec())
    return {row.path: row for row in rows}


# ---------------------------------------------------------------------------
# Guard 1: caution with approval (catalog overlay on both vCenter rows)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("path", _VCENTER_CREDENTIAL_PATHS)
def test_vcenter_credential_read_is_caution_with_approval(path: str, version: str) -> None:
    """A parsed GET on the path comes out ``caution`` + ``requires_approval``."""
    proto = _parsed_ops()[path]
    assert proto.safety_level == "safe", "the parser alone must not raise a GET"
    assert proto.requires_approval is False
    floored = apply_safety_floor(product=_PRODUCT, version=version, impl_id=_IMPL_ID, proto=proto)
    assert floored.safety_level == "caution"
    assert floored.requires_approval is True


@pytest.mark.parametrize("version", _VERSIONS)
@pytest.mark.parametrize("path", _VCENTER_PLAIN_PATHS)
def test_vcenter_plain_read_stays_safe(path: str, version: str) -> None:
    """The overlay touches only the listed paths."""
    proto = _parsed_ops()[path]
    floored = apply_safety_floor(product=_PRODUCT, version=version, impl_id=_IMPL_ID, proto=proto)
    assert floored.safety_level == "safe"
    assert floored.requires_approval is False


def test_vcenter_rows_share_one_overlay() -> None:
    """The 8.0 row reuses the 9.0 list, so the two cannot drift apart."""
    assert safety_overlay_for(_PRODUCT, "8.0") == safety_overlay_for(_PRODUCT, "9.0")


# ---------------------------------------------------------------------------
# Guard 2: the op id is on the shared credential-read list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", (*_VCENTER_CREDENTIAL_PATHS, "/v1/sddcs/{id}/spec"))
def test_credential_read_op_id_classifies_credential_read(path: str) -> None:
    assert classify_op(f"GET:{path}") == "credential_read"


@pytest.mark.parametrize("path", _VCENTER_PLAIN_PATHS)
def test_vcenter_plain_read_op_id_classifies_read(path: str) -> None:
    assert classify_op(f"GET:{path}") == "read"


class _NoOpVmwareConnector(Connector):
    """Connector class satisfying the resolver lookup for the typed stand-ins."""

    product = _PRODUCT
    version = "9.0"
    impl_id = _IMPL_ID

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _identity_provider_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in for the generic ``GET:/vcenter/identity/providers/{provider}`` answer."""
    return {
        "name": "corp-idp",
        "oidc": {
            "client_id": "vcenter",
            "client_secret": _CLIENT_SECRET,
            "token_endpoint": "https://idp.example.test/token",
        },
        "active_directory_over_ldap": {"user_name": "binder", "password": _LDAP_PASSWORD},
    }


async def _host_config_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in for the generic ``GET:/HostSystem/{moId}/config`` answer."""
    return {"name": "host-01", "ipmi": {"login": "admin", "password": _IPMI_PASSWORD}}


async def _register_read(embedding_service: AsyncMock, op_id: str, handler: Any) -> None:
    await register_typed_operation(
        product=_PRODUCT,
        version="9.0",
        impl_id=_IMPL_ID,
        op_id=op_id,
        handler=handler,
        summary="Stand-in for a generic vCenter read.",
        description="Returns an answer that holds credentials.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def test_vcenter_identity_provider_read_response_is_scrubbed(
    stub_embedding_service: AsyncMock,
) -> None:
    """The caller gets the placeholder for each secret; other fields stay."""
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_NoOpVmwareConnector)
    op_id = "GET:/vcenter/identity/providers/{provider}"
    await _register_read(stub_embedding_service, op_id, _identity_provider_handler)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vmware-rest-9.0",
        op_id=op_id,
        target=None,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["name"] == "corp-idp"
    assert result.result["oidc"]["client_secret"] == _REDACTED
    assert result.result["oidc"]["client_id"] == "vcenter"
    assert result.result["oidc"]["token_endpoint"] == "https://idp.example.test/token"
    assert result.result["active_directory_over_ldap"]["password"] == _REDACTED
    assert result.result["active_directory_over_ldap"]["user_name"] == "binder"
    dumped = result.model_dump_json()
    assert _CLIENT_SECRET not in dumped
    assert _LDAP_PASSWORD not in dumped


async def test_vcenter_host_config_read_response_is_scrubbed(
    stub_embedding_service: AsyncMock,
) -> None:
    """The older-API host config read hides the IPMI password."""
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_NoOpVmwareConnector)
    op_id = "GET:/HostSystem/{moId}/config"
    await _register_read(stub_embedding_service, op_id, _host_config_handler)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="vmware-rest-9.0",
        op_id=op_id,
        target=None,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["ipmi"]["password"] == _REDACTED
    assert result.result["ipmi"]["login"] == "admin"
    assert result.result["name"] == "host-01"
    assert _IPMI_PASSWORD not in result.model_dump_json()
