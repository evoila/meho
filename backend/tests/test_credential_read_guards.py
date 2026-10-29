# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested reads that return credentials get the hand-written guards (#4617).

Two guards, one shared list:

* the catalog row ``sddc`` / ``9.0`` raises each such read to ``caution``
  with ``requires_approval=True`` through its ``safety_overlay``;
* the op id is on ``broadcast.events._CREDENTIAL_READ_OPS``, so the
  dispatcher replaces every secret-named field in the answer with
  ``[REDACTED:param_name]`` before the caller sees it.

``bind9.config.show`` is on the same list. Its answer is one text field,
so the ``bind_key_secret`` text rule replaces the value of every
``secret "..."`` line (TSIG and rndc keys) and the rest of the file stays
readable. Since #5132 it reads only config files (no key files), so it
runs without approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import meho_backplane.connectors.bind9  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.bind9 import BIND9_OPS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_REDACTED = "[REDACTED:param_name]"
# Assembled from fragments so gitleaks' built-in rules don't false-positive
# on the test source.
_PASSWORD_SECRET = "super-secret" + "-esxi-pw"
_FAKE_TSIG_VALUE = "exampleTsig" + "Value=="

_SDDC_PRODUCT = "sddc"
_SDDC_VERSION = "9.0"
_SDDC_IMPL_ID = "sddc-rest"
_SDDC_CONNECTOR_ID = "sddc-rest-9.0"

#: Every SDDC Manager read the catalog overlay and the credential-read list
#: cover. Spellings match the vendor spec (``vmware/vcf-api-specs``,
#: ``sddc-manager-openapi.json``, 9.0.0.0).
_SDDC_CREDENTIAL_PATHS: tuple[str, ...] = (
    "/v1/credentials",
    "/v1/credentials/{id}",
    "/v1/credentials/tasks",
    "/v1/credentials/tasks/{id}",
    "/v1/credentials/tasks/{id}/subtasks/{subtaskId}",
    "/v1/credentials/tasks/{id}/resource-credentials",
    "/v1/system/settings/depot",
    "/v1/system/backup-configuration",
    "/v1/system/backup-configuration/backup-locations",
    "/v1/system/proxy-configuration",
    "/v1/hosts/prechecks/{id}",
    "/v1/vasa-providers",
    "/v1/vasa-providers/{id}",
    "/v1/vasa-providers/{id}/users",
    "/v1/vcf-management-components/tasks/{taskId}/spec",
    "/v1/users",
    "/v1/users/ui",
    "/v1/users/local/admin",
    "/v1/identity-providers",
    "/v1/identity-providers/{id}",
)

#: A read with no credentials in its answer; it must stay ``safe``.
_SDDC_PLAIN_PATH = "/v1/domains"


def _sddc_fixture_spec() -> str:
    lines = [
        "openapi: 3.0.3",
        "info: {title: SDDC credential reads fixture, version: '9.0.0.0'}",
        "paths:",
    ]
    for index, path in enumerate((*_SDDC_CREDENTIAL_PATHS, _SDDC_PLAIN_PATH)):
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
        sub="op-4617-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-000000004617"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _parsed_sddc_ops() -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///sddc-credential-reads.yaml", content=_sddc_fixture_spec())
    return {row.path: row for row in rows}


# ---------------------------------------------------------------------------
# SDDC Manager -- guard 1: caution with approval (catalog overlay)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _SDDC_CREDENTIAL_PATHS)
def test_sddc_credential_read_is_caution_with_approval(path: str) -> None:
    """A parsed GET on the path comes out ``caution`` + ``requires_approval``."""
    proto = _parsed_sddc_ops()[path]
    assert proto.safety_level == "safe", "the parser alone must not raise a GET"
    assert proto.requires_approval is False
    floored = apply_safety_floor(
        product=_SDDC_PRODUCT, version=_SDDC_VERSION, impl_id=_SDDC_IMPL_ID, proto=proto
    )
    assert floored.safety_level == "caution"
    assert floored.requires_approval is True


def test_sddc_plain_read_stays_safe() -> None:
    """The overlay touches only the listed paths."""
    proto = _parsed_sddc_ops()[_SDDC_PLAIN_PATH]
    floored = apply_safety_floor(
        product=_SDDC_PRODUCT, version=_SDDC_VERSION, impl_id=_SDDC_IMPL_ID, proto=proto
    )
    assert floored is proto
    assert floored.safety_level == "safe"
    assert floored.requires_approval is False


# ---------------------------------------------------------------------------
# SDDC Manager -- guard 2: the op id is on the shared credential-read list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _SDDC_CREDENTIAL_PATHS)
def test_sddc_credential_read_op_id_classifies_credential_read(path: str) -> None:
    assert classify_op(f"GET:{path}") == "credential_read"


def test_sddc_plain_read_op_id_classifies_read() -> None:
    assert classify_op(f"GET:{_SDDC_PLAIN_PATH}") == "read"


class _NoOpSddcConnector(Connector):
    """Connector class satisfying the resolver lookup for the typed stand-in."""

    product = _SDDC_PRODUCT
    version = _SDDC_VERSION
    impl_id = _SDDC_IMPL_ID

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


async def _sddc_credentials_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in for the ingested ``GET:/v1/credentials`` answer."""
    return {
        "elements": [
            {
                "id": "cred-esxi-01",
                "resource": {"resourceName": "esx-01", "resourceType": "ESXI"},
                "accountType": "USER",
                "credentialType": "SSH",
                "username": "root",
                "password": _PASSWORD_SECRET,
            }
        ]
    }


async def _register_sddc_credentials_read(embedding_service: AsyncMock) -> None:
    register_connector_v2(product=_SDDC_PRODUCT, version="", impl_id="", cls=_NoOpSddcConnector)
    await register_typed_operation(
        product=_SDDC_PRODUCT,
        version=_SDDC_VERSION,
        impl_id=_SDDC_IMPL_ID,
        op_id="GET:/v1/credentials",
        handler=_sddc_credentials_handler,
        summary="Stand-in for the ingested credential list.",
        description="Returns one credential with its password.",
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def test_sddc_ingested_credential_read_response_is_scrubbed(
    stub_embedding_service: AsyncMock,
) -> None:
    """The caller gets the placeholder for the password; other fields stay."""
    await _register_sddc_credentials_read(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_SDDC_CONNECTOR_ID,
        op_id="GET:/v1/credentials",
        target=None,
        params={},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    element = result.result["elements"][0]
    assert element["password"] == _REDACTED
    assert element["username"] == "root"
    assert element["credentialType"] == "SSH"
    assert element["resource"]["resourceType"] == "ESXI"
    assert _PASSWORD_SECRET not in result.model_dump_json()


# ---------------------------------------------------------------------------
# bind9.config.show -- both guards plus the BIND key text rule
# ---------------------------------------------------------------------------


def test_bind9_config_show_is_safe_without_approval() -> None:
    """#5132: the read refuses key files and hides key secrets, so it runs at once."""
    op = next(o for o in BIND9_OPS if o.op_id == "bind9.config.show")
    assert op.safety_level == "safe"
    assert op.requires_approval is False


def test_bind9_config_show_classifies_credential_read() -> None:
    assert classify_op("bind9.config.show") == "credential_read"


_NAMED_CONF = (
    'key "rndc-key" {\n'
    "    algorithm hmac-sha256;\n"
    f'    secret "{_FAKE_TSIG_VALUE}";\n'
    "};\n"
    "options {\n"
    '    directory "/var/cache/bind";\n'
    "    allow-transfer { key rndc-key; };\n"
    "};\n"
)

_NAMED_CONF_REDACTED = _NAMED_CONF.replace(
    f'secret "{_FAKE_TSIG_VALUE}";', "secret [REDACTED:bind_key_secret];"
)


class _NoOpBind9Connector(Connector):
    """Connector class satisfying the resolver lookup for the typed stand-in."""

    product = "bind9"
    version = "9.x"
    impl_id = "bind9-ssh"

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


async def _bind9_config_show_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in for the real handler: the file text with a key secret in it."""
    return {"file": "/etc/bind/named.conf", "content": _NAMED_CONF}


async def _register_bind9_config_show(embedding_service: AsyncMock) -> None:
    register_connector_v2(product="bind9", version="", impl_id="", cls=_NoOpBind9Connector)
    await register_typed_operation(
        product="bind9",
        version="9.x",
        impl_id="bind9-ssh",
        op_id="bind9.config.show",
        handler=_bind9_config_show_handler,
        summary="Stand-in for the config read.",
        description="Returns a config file that holds a key secret.",
        parameter_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def test_bind9_config_show_key_secret_is_redacted(
    stub_embedding_service: AsyncMock,
) -> None:
    """The key value becomes the placeholder; the rest of the file is unchanged."""
    await _register_bind9_config_show(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.show",
        target=None,
        params={"path": "named.conf"},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["file"] == "/etc/bind/named.conf"
    assert result.result["content"] == _NAMED_CONF_REDACTED
    assert _FAKE_TSIG_VALUE not in result.model_dump_json()


async def test_bind9_config_show_key_secret_stays_redacted_with_reveal_secret(
    stub_embedding_service: AsyncMock,
) -> None:
    """``reveal_secret`` does not bring a key secret back: the text rule still runs."""
    await _register_bind9_config_show(stub_embedding_service)

    result = await dispatch(
        operator=_make_operator(),
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.show",
        target=None,
        params={"path": "named.conf", "reveal_secret": True},
    )
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result["content"] == _NAMED_CONF_REDACTED
    assert _FAKE_TSIG_VALUE not in result.model_dump_json()
