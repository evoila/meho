# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Installer reuse checks, cancel, appliance info, releases and spec read.

Handler tests call the bound-method shims on :class:`InstallerConnector`
with ``respx``-mocked vendor answers written by hand from the vendor API
reference. Dispatch tests run the real dispatcher against the registered
ops: the cancel waits for approval, a vendor error reaches the answer, and
SDDC Manager discovery on a 9.0 Installer stops with an audited
``connector_unsupported`` before any vendor call.

All names, ids and secrets are made up. No network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vcf_installer import (
    InstallerConnector,
    InstallerTargetLike,
    register_installer_typed_operations,
)
from meho_backplane.connectors.vcf_installer import typed_lifecycle as _lifecycle
from meho_backplane.connectors.vcf_installer.typed_ops import (
    INSTALLER_TYPED_OPS,
    INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_HOST = "installer-a.test.invalid"
_BASE = f"https://{_HOST}"
_TOKEN_PATH = "/v1/tokens"
_SECRET = "made-up-Secret-42"
_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000a468")
_CONNECTOR_ID = "installer-rest-9.1"

_LIFECYCLE_OP_IDS = (
    "installer.discovery.vcenter",
    "installer.discovery.sddc_manager",
    "installer.discovery.vcf_operations",
    "installer.sddc.cancel",
    "installer.sddc.spec.get",
    "installer.appliance.info",
    "installer.release.list",
)

_OPERATOR = Operator(
    sub="installer-lifecycle-test",
    name="Installer Lifecycle Test Operator",
    email=None,
    raw_jwt="<test-raw-jwt>",
    tenant_id=_TENANT,
    tenant_role=TenantRole.TENANT_ADMIN,
)


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_installer_registry() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    register_connector_v2(
        product=InstallerConnector.product,
        version=InstallerConnector.version,
        impl_id=InstallerConnector.impl_id,
        cls=InstallerConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@dataclass
class _StubTarget:
    name: str = "installer-a"
    host: str = _HOST
    port: int | None = 443
    secret_ref: str = "installer/installer-a"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    version: str | None = None
    id: UUID = field(default_factory=lambda: UUID(int=1))
    tenant_id: UUID = field(default_factory=lambda: _TENANT)


async def _stub_loader(_target: InstallerTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "installer-login"}


def _make_connector() -> InstallerConnector:
    return InstallerConnector(credentials_loader=_stub_loader)


@pytest.fixture
def vault_login(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the Vault read of the call's ``secret_ref`` with a fixed login."""
    fake = AsyncMock(return_value={"username": "administrator@vsphere.local", "password": _SECRET})
    monkeypatch.setattr(_lifecycle, "load_param_secret_data", fake)
    return fake


def _discovery_params(**extra: Any) -> dict[str, Any]:
    return {
        "address": "<vcenter-fqdn>",
        "ssl_thumbprint": "AA:BB:CC",
        "secret_ref": "kv/made-up/vcenter-login",
        **extra,
    }


# ------------------------------------------------------------------ registration


def test_the_seven_ops_are_typed_ops_on_the_connector() -> None:
    by_id = {op.op_id: op for op in INSTALLER_TYPED_OPS}
    for op_id in _LIFECYCLE_OP_IDS:
        op = by_id[op_id]
        assert callable(getattr(InstallerConnector, op.handler_attr)), op_id
        assert op.group_key in INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP, op_id
        assert op_id in INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key], op_id


@pytest.mark.parametrize("op_id", _LIFECYCLE_OP_IDS)
def test_safety_and_approval_match_the_table(op_id: str) -> None:
    op = next(op for op in INSTALLER_TYPED_OPS if op.op_id == op_id)
    if op_id == "installer.sddc.cancel":
        assert (op.safety_level, op.requires_approval) == ("dangerous", True)
    else:
        assert (op.safety_level, op.requires_approval) == ("safe", False)


# ------------------------------------------------------------------ discovery


@pytest.mark.asyncio
async def test_vcenter_discovery_sends_the_vault_login_and_returns_the_result(
    vault_login: AsyncMock,
) -> None:
    connector = _make_connector()
    answer = {"vcenterVersion": "9.0.0", "vcenterBuildNumber": "100", "nsxInfo": {"address": "x"}}
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        route = mock.post("/v1/sddcs/vcenter-discovery").respond(200, json=answer)
        result = await connector.discovery_vcenter(_OPERATOR, _StubTarget(), _discovery_params())

    assert result == answer
    assert json.loads(route.calls[0].request.content) == {
        "address": "<vcenter-fqdn>",
        "sslThumbprint": "AA:BB:CC",
        "username": "administrator@vsphere.local",
        "password": _SECRET,
    }
    vault_call = vault_login.await_args
    assert vault_call is not None
    assert vault_call.kwargs["param"] == "secret_ref"
    assert vault_call.args[0].secret_ref == "kv/made-up/vcenter-login"
    await connector.aclose()


@pytest.mark.asyncio
async def test_vcf_operations_discovery_uses_admin_fields_and_may_skip_the_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _lifecycle, "load_param_secret_data", AsyncMock(return_value={"password": _SECRET})
    )
    connector = _make_connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        route = mock.post("/v1/sddcs/vcfops-discovery").respond(
            200, json={"vcfOperationsVersion": "9.0.0", "vcfOperationsNodes": []}
        )
        await connector.discovery_vcf_operations(_OPERATOR, _StubTarget(), _discovery_params())

    body = json.loads(route.calls[0].request.content)
    assert body == {
        "address": "<vcenter-fqdn>",
        "sslThumbprint": "AA:BB:CC",
        "adminPassword": _SECRET,
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_discovery_without_a_password_in_vault_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _lifecycle, "load_param_secret_data", AsyncMock(return_value={"username": "u"})
    )
    connector = _make_connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/v1/sddcs/vcenter-discovery")
        with pytest.raises(_lifecycle.DiscoverySecretError) as exc_info:
            await connector.discovery_vcenter(_OPERATOR, _StubTarget(), _discovery_params())

    assert route.call_count == 0
    assert "'password'" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vendor_body", "kept_message"),
    [
        ({"errorCode": "VCENTER_LOGIN_FAILED", "message": "Login failed."}, "Login failed."),
        ({"errorCode": "X", "message": f"bad password {_SECRET}"}, None),
    ],
)
async def test_discovery_vendor_error_keeps_the_message_but_never_the_password(
    vault_login: AsyncMock, vendor_body: dict[str, str], kept_message: str | None
) -> None:
    connector = _make_connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.post("/v1/sddcs/vcenter-discovery").respond(400, json=vendor_body)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.discovery_vcenter(_OPERATOR, _StubTarget(), _discovery_params())

    error = exc_info.value
    assert error.response.status_code == 400
    assert error.request.content == b""
    assert error.__context__ is None
    assert _SECRET not in error.response.text
    assert _SECRET not in str(error)
    if kept_message is None:
        assert error.response.content == b""
    else:
        assert error.response.json()["message"] == kept_message
    await connector.aclose()


@pytest.mark.asyncio
async def test_discovery_401_keeps_the_sent_token_for_the_relogin(vault_login: AsyncMock) -> None:
    connector = _make_connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.post("/v1/sddcs/vcenter-discovery").respond(401, json={"message": "expired"})
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await connector.discovery_vcenter(_OPERATOR, _StubTarget(), _discovery_params())

    assert exc_info.value.response.status_code == 401
    assert exc_info.value.request.headers["authorization"] == "Bearer tok-1"
    await connector.aclose()


@pytest.mark.asyncio
async def test_sddc_manager_discovery_on_9_0_stops_before_vault_and_vendor(
    vault_login: AsyncMock,
) -> None:
    connector = _make_connector()
    target = _StubTarget(fingerprint={"version": "9.0.2.0"})
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/v1/sddcs/sddcm-discovery")
        with pytest.raises(NotImplementedError) as exc_info:
            await connector.discovery_sddc_manager(_OPERATOR, target, _discovery_params())

    assert str(exc_info.value) == (
        "installer.discovery.sddc_manager: SDDC Manager discovery needs VCF Installer "
        "9.1 or newer; this target is 9.0.2.0."
    )
    assert route.call_count == 0
    vault_login.assert_not_awaited()
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["9.1.0.0", "9.1.1.0", None])
async def test_sddc_manager_discovery_runs_on_9_1_and_on_unknown_versions(
    vault_login: AsyncMock, version: str | None
) -> None:
    connector = _make_connector()
    target = _StubTarget(fingerprint={"version": version} if version else None)
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        route = mock.post("/v1/sddcs/sddcm-discovery").respond(
            200, json={"sddcManager": {"fqdn": "<sddc-manager-fqdn>", "version": "9.0.0"}}
        )
        result = await connector.discovery_sddc_manager(_OPERATOR, target, _discovery_params())

    assert result["sddcManager"]["version"] == "9.0.0"
    body = json.loads(route.calls[0].request.content)
    assert body["adminUsername"] == "administrator@vsphere.local"
    assert body["adminPassword"] == _SECRET
    await connector.aclose()


# ------------------------------------------------------------------ cancel


@pytest.mark.asyncio
async def test_cancel_deletes_the_task() -> None:
    connector = _make_connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        route = mock.delete("/v1/tasks/task-1").respond(200)
        result = await connector.sddc_cancel(_OPERATOR, _StubTarget(), {"id": "task-1"})

    assert result == {"id": "task-1", "status": "cancel_requested"}
    assert route.call_count == 1
    await connector.aclose()


# ------------------------------------------------------------------ reads


@pytest.mark.asyncio
async def test_appliance_info_returns_the_vendor_object() -> None:
    connector = _make_connector()
    answer = {"version": "9.1.0.0", "role": "INSTALLER", "dnsDomain": "example.test"}
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.get("/v1/system/appliance-info").respond(200, json=answer)
        result = await connector.appliance_info(_OPERATOR, _StubTarget(), {})

    assert result == answer
    await connector.aclose()


@pytest.mark.asyncio
async def test_release_list_returns_flat_rows_and_maps_the_filters() -> None:
    connector = _make_connector()
    release = {
        "product": "VCF",
        "version": "9.1.0.0",
        "releaseDate": "2026-01-01T00:00:00Z",
        "minInstallerVersion": "9.1.0.0",
        "description": "A long text the row leaves out.",
        "bom": [
            {"name": "ESX", "version": "9.1.0.0-1", "publicName": "VMware ESXi"},
            {"name": "VCENTER", "version": "9.1.0.0-2", "publicName": "vCenter"},
        ],
        "patchBundles": [{"bundleId": "b-1"}],
    }
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        route = mock.get("/v1/releases").respond(
            200, json={"elements": [release], "pageMetadata": {"totalElements": 1}}
        )
        result = await connector.release_list(
            _OPERATOR, _StubTarget(), {"version_ge": "9.0", "include_only_compatible": True}
        )

    assert result == {
        "elements": [
            {
                "product": "VCF",
                "version": "9.1.0.0",
                "releaseDate": "2026-01-01T00:00:00Z",
                "minInstallerVersion": "9.1.0.0",
                "bom": {"ESX": "9.1.0.0-1", "VCENTER": "9.1.0.0-2"},
            }
        ],
        "pageMetadata": {"totalElements": 1},
    }
    assert dict(route.calls[0].request.url.params) == {
        "versionGe": "9.0",
        "includeOnlyCompatible": "true",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_spec_get_replaces_every_secret_field() -> None:
    connector = _make_connector()
    spec = {
        "sddcId": "<sddc-id>",
        "vcenterSpec": {
            "vcenterHostname": "<vcenter-fqdn>",
            "rootVcenterPassword": "made-up-1",
            "adminUserSsoPassword": "made-up-2",
        },
        "hostSpecs": [{"hostname": "<esx-host>", "credentials": {"password": "made-up-3"}}],
        "nsxtSpec": {"nsxtAdminPassword": "made-up-4", "vipFqdn": "<nsx-vip-fqdn>"},
        "sddcManagerSpec": {"rootUserCredentials": {"password": "made-up-5"}},
    }
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.get("/v1/sddcs/task-1/spec").respond(200, json=spec)
        result = await connector.sddc_spec_get(_OPERATOR, _StubTarget(), {"id": "task-1"})

    text = json.dumps(result)
    for secret in ("made-up-1", "made-up-2", "made-up-3", "made-up-4", "made-up-5"):
        assert secret not in text
    assert result["vcenterSpec"]["rootVcenterPassword"] == SECRET_FIELD_MARKER
    assert result["hostSpecs"][0]["credentials"] == SECRET_FIELD_MARKER
    assert result["vcenterSpec"]["vcenterHostname"] == "<vcenter-fqdn>"
    assert result["nsxtSpec"]["vipFqdn"] == "<nsx-vip-fqdn>"
    await connector.aclose()


# ------------------------------------------------------------------ through the dispatcher


async def _seed_target(version: str) -> Any:
    sessionmaker = get_sessionmaker()
    name = f"installer-{uuid.uuid4().hex[:8]}"
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT,
                name=name,
                aliases=[],
                product="installer",
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="kv/made-up/installer",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": version},
                notes="seeded by test_connectors_vcf_installer_lifecycle",
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        return await resolve_target(session, _TENANT, name)


async def _setup_dispatch(version: str) -> Any:
    set_default_reducer(PassThroughReducer())
    stub_embedding = AsyncMock()
    stub_embedding.encode_one.return_value = [0.1] * 384
    await register_installer_typed_operations(embedding_service=stub_embedding)
    _CONNECTOR_INSTANCE_CACHE[InstallerConnector] = _make_connector()
    return await _seed_target(version)


async def _audit_rows(op_id: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_dispatch_cancel_waits_for_approval() -> None:
    target = await _setup_dispatch("9.1.0.0")
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.delete("/v1/tasks/task-1")
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id="installer.sddc.cancel",
            target=target,
            params={"id": "task-1"},
        )

    assert result.status == "awaiting_approval", result
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_dispatch_cancel_approved_shows_the_vendor_refusal() -> None:
    target = await _setup_dispatch("9.1.0.0")
    refusal = "Task can not be cancelled. Only a IN_PROGRESS task can be cancelled."
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.delete("/v1/tasks/task-1").respond(
            409, json={"errorCode": "TASK_NOT_CANCELLABLE", "message": refusal}
        )
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id="installer.sddc.cancel",
            target=target,
            params={"id": "task-1"},
            _approved=True,
        )

    assert result.status == "error"
    assert result.extras["http_status"] == 409
    assert refusal in json.dumps(result.extras)


@pytest.mark.asyncio
async def test_dispatch_cancel_approved_success() -> None:
    target = await _setup_dispatch("9.1.0.0")
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_TOKEN_PATH).respond(201, json={"accessToken": "tok-1"})
        mock.delete("/v1/tasks/task-1").respond(200)
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id="installer.sddc.cancel",
            target=target,
            params={"id": "task-1"},
            _approved=True,
        )

    assert result.status == "ok", result.error
    assert result.result == {"id": "task-1", "status": "cancel_requested"}


@pytest.mark.asyncio
async def test_dispatch_sddc_manager_discovery_on_9_0_is_connector_unsupported_and_audited(
    vault_login: AsyncMock,
) -> None:
    target = await _setup_dispatch("9.0.2.0")
    op_id = "installer.discovery.sddc_manager"
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.post("/v1/sddcs/sddcm-discovery")
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=target,
            params=_discovery_params(),
        )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_unsupported"
    assert result.extras["detail"] == (
        "installer.discovery.sddc_manager: SDDC Manager discovery needs VCF Installer "
        "9.1 or newer; this target is 9.0.2.0."
    )
    assert route.call_count == 0
    vault_login.assert_not_awaited()
    rows = await _audit_rows(op_id)
    assert len(rows) == 1
    assert rows[0].payload["result_status"] == "error"
