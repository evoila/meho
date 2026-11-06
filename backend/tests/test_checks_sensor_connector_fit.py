# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Sensor's connector checked against its target's product and version.

Covers the check itself (:mod:`meho_backplane.checks.connector_fit`), the
create refusal and its ``force`` override, the list after a new probe
changed the target's version, the REST and MCP transports, and the
Sensors page row. The connectors are made-up ones registered for the test
only, so no shipped connector changes the result.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.connector_fit import connector_fit
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import SensorAdminService, SensorConnectorMismatchError
from meho_backplane.connectors import (
    Connector,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.resolver import connector_serves_target, resolve_connector_or_label
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import sensors as _sensor_tools  # noqa: F401  (registers the tools)
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.sensors.views import project_sensor_to_row
from meho_backplane.ui.templating import get_templates

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_PRODUCT = "fitprod"
_CONNECTOR_9 = "fitprod-rest-9.0"
_CONNECTOR_8 = "fitprod-rest-8.0"
_OP = "fitprod.item.list"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


class _FakeConnector(Connector):
    product = _PRODUCT
    impl_id = "fitprod-rest"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


class _Fit9(_FakeConnector):
    version = "9.0"
    supported_version_range = ">=9.0,<10.0"


class _Fit8(_FakeConnector):
    version = "8.0"
    supported_version_range = ">=8.0,<9.0"


@dataclass
class _Target:
    product: str
    version: str | None = None
    fingerprint: dict[str, Any] | None = None
    preferred_impl_id: str | None = None


@pytest.fixture(autouse=True)
def _connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the two made-up catalogs for this test only."""
    v2 = connector_registry._REGISTRY_V2
    monkeypatch.setitem(v2, (_PRODUCT, "9.0", "fitprod-rest"), _Fit9)
    monkeypatch.setitem(v2, (_PRODUCT, "8.0", "fitprod-rest"), _Fit8)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


async def _seed(tenant_id: uuid.UUID = _TENANT_A) -> None:
    """A tenant and the safe op on both made-up catalogs."""
    async with get_sessionmaker()() as session:
        if (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar() is None:
            slug = str(tenant_id)[:8]
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))
        for version in ("9.0", "8.0"):
            found = await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == _PRODUCT,
                    EndpointDescriptor.version == version,
                    EndpointDescriptor.op_id == _OP,
                )
            )
            if found.scalar() is None:
                session.add(
                    EndpointDescriptor(
                        product=_PRODUCT,
                        version=version,
                        impl_id="fitprod-rest",
                        op_id=_OP,
                        source_kind="ingested",
                        method="GET",
                        path="/items",
                        parameter_schema={"type": "object", "properties": {}},
                        safety_level="safe",
                    )
                )
        await session.commit()


async def _seed_target(
    name: str,
    *,
    probed: str | None,
    product: str = _PRODUCT,
    tenant_id: uuid.UUID = _TENANT_A,
) -> uuid.UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TargetORM(
                id=target_id,
                tenant_id=tenant_id,
                name=name,
                aliases=[],
                product=product,
                version=None,
                fingerprint=None if probed is None else {"version": probed},
                host="appliance.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return target_id


async def _probe_again(target_id: uuid.UUID, version: str) -> None:
    """What a new probe leaves behind: a new detected version on the row."""
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(TargetORM)
            .where(TargetORM.id == target_id)
            .values(fingerprint={"version": version})
        )


def _payload(name: str, connector_id: str, target: str | None, **extra: Any) -> SensorCreate:
    body: dict[str, Any] = {
        "name": name,
        "connector_id": connector_id,
        "op_id": _OP,
        "assertion": _ASSERTION,
        "cadence_kind": "interval",
        "interval_seconds": 60,
    }
    if target is not None:
        body["target"] = {"name": target}
    body.update(extra)
    return SensorCreate.model_validate(body)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_resolver_match_is_the_declared_range() -> None:
    assert connector_serves_target(_PRODUCT, "9.0", "fitprod-rest", _Target(_PRODUCT, "9.0.2"))
    assert not connector_serves_target(_PRODUCT, "9.0", "fitprod-rest", _Target(_PRODUCT, "8.0.3"))
    assert not connector_serves_target(_PRODUCT, "9.0", "fitprod-rest", _Target("other", "9.0.2"))


def test_fitting_connector_has_no_warning() -> None:
    fit = connector_fit(_CONNECTOR_9, _Target(_PRODUCT, fingerprint={"version": "9.0.2"}))
    assert fit.target_product == _PRODUCT
    assert fit.target_version == "9.0.2"
    assert fit.warning is None
    assert fit.matching_connector_id is None


def test_version_outside_range_names_the_matching_connector() -> None:
    fit = connector_fit(_CONNECTOR_9, _Target(_PRODUCT, fingerprint={"version": "8.0.3"}))
    assert fit.matching_connector_id == _CONNECTOR_8
    assert fit.warning is not None
    assert "fitprod 8.0.3" in fit.warning
    assert ">=9.0,<10.0" in fit.warning
    assert f"Connector {_CONNECTOR_8} fits this target." in fit.warning


def test_version_outside_every_range_says_no_connector_fits() -> None:
    fit = connector_fit(_CONNECTOR_9, _Target(_PRODUCT, version="7.0"))
    assert fit.matching_connector_id is None
    assert fit.warning is not None
    assert "No connector in MEHO fits this target." in fit.warning


def test_fallback_registration_sharing_a_class_is_not_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver may pick a fallback that accepts every version.

    It shares its class with the 8.0 registration, so the id built from the
    class is the 8.0 one. That id does not fit a 7.0 target, and a create
    with it would be refused, so the warning must not name it.
    """
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, "", ""), _Fit8)
    target = _Target(_PRODUCT, version="7.0")
    assert resolve_connector_or_label(target)[0] is _Fit8
    assert connector_fit(_CONNECTOR_8, target).warning is not None

    fit = connector_fit(_CONNECTOR_9, target)
    assert fit.matching_connector_id is None
    assert fit.warning is not None
    assert "No connector in MEHO fits this target." in fit.warning


def test_other_product_warns() -> None:
    fit = connector_fit(_CONNECTOR_9, _Target("otherprod", version="9.0.1"))
    assert fit.warning is not None
    assert "otherprod target" in fit.warning
    assert f"Connector {_CONNECTOR_9} is for {_PRODUCT}" in fit.warning


@pytest.mark.parametrize(
    ("connector_id", "target"),
    [
        # No known version: MEHO cannot tell, so it does not warn.
        (_CONNECTOR_9, _Target(_PRODUCT)),
        # A connector MEHO does not know: no warning either.
        ("fitprod-rest-5.0", _Target(_PRODUCT, version="8.0.3")),
        # A target with no product.
        (_CONNECTOR_9, _Target("")),
    ],
)
def test_no_warning_when_meho_cannot_tell(connector_id: str, target: _Target) -> None:
    assert connector_fit(connector_id, target).warning is None


# ---------------------------------------------------------------------------
# Service: create and list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_on_mismatched_connector_is_refused() -> None:
    await _seed()
    await _seed_target("old-appliance", probed="8.0.3")
    with pytest.raises(SensorConnectorMismatchError) as caught:
        await SensorAdminService().create(
            _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "old-appliance")
        )
    assert caught.value.matching_connector_id == _CONNECTOR_8
    assert "Set force to create the Sensor anyway." in str(caught.value)
    assert await SensorAdminService().list_(_TENANT_A) == []


@pytest.mark.asyncio
async def test_create_with_force_keeps_the_warning() -> None:
    await _seed()
    await _seed_target("old-appliance", probed="8.0.3")
    created = await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "old-appliance", force=True)
    )
    assert created.connector_fit is not None
    assert created.connector_fit.matching_connector_id == _CONNECTOR_8
    assert created.connector_fit.warning is not None


@pytest.mark.asyncio
async def test_create_on_fitting_connector_shows_target_version() -> None:
    await _seed()
    await _seed_target("new-appliance", probed="9.0.2")
    created = await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "new-appliance")
    )
    assert created.connector_fit is not None
    assert created.connector_fit.target_version == "9.0.2"
    assert created.connector_fit.warning is None


@pytest.mark.asyncio
async def test_create_with_unknown_target_is_not_refused() -> None:
    """A target MEHO cannot find is reported by the runner, as before."""
    await _seed()
    created = await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "not-there")
    )
    assert created.connector_fit is None


@pytest.mark.asyncio
async def test_list_warns_after_a_new_probe_changes_the_version() -> None:
    await _seed()
    target_id = await _seed_target("appliance", probed="9.0.2")
    await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "appliance")
    )
    await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("no-target", _CONNECTOR_9, None)
    )

    before = {s.name: s for s in await SensorAdminService().list_(_TENANT_A)}
    assert before["no-target"].connector_fit is None
    fit = before["s1"].connector_fit
    assert fit is not None
    assert fit.warning is None

    await _probe_again(target_id, "8.0.3")

    after = {s.name: s for s in await SensorAdminService().list_(_TENANT_A)}
    fit = after["s1"].connector_fit
    assert fit is not None
    assert fit.target_version == "8.0.3"
    assert fit.matching_connector_id == _CONNECTOR_8
    assert fit.warning is not None


@pytest.mark.asyncio
async def test_list_reads_targets_of_the_sensor_tenant_only() -> None:
    """A target of the same name in another tenant is never used."""
    await _seed(_TENANT_A)
    await _seed(_TENANT_B)
    await _seed_target("shared-name", probed="9.0.2", tenant_id=_TENANT_A)
    await _seed_target("shared-name", probed="8.0.3", tenant_id=_TENANT_B)
    await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "shared-name")
    )
    [sensor] = await SensorAdminService().list_(_TENANT_A)
    assert sensor.connector_fit is not None
    assert sensor.connector_fit.target_version == "9.0.2"
    assert sensor.connector_fit.warning is None


@pytest.mark.asyncio
async def test_list_does_not_leave_a_target_in_the_audit_context() -> None:
    await _seed()
    await _seed_target("appliance", probed="9.0.2")
    await SensorAdminService().create(
        _TENANT_A, "op-admin", _payload("s1", _CONNECTOR_9, "appliance")
    )
    structlog.contextvars.clear_contextvars()
    await SensorAdminService().list_(_TENANT_A)
    context = structlog.contextvars.get_contextvars()
    assert "target_id" not in context
    assert "target_name" not in context


# ---------------------------------------------------------------------------
# REST and MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _rest_body(name: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "connector_id": _CONNECTOR_9,
        "op_id": _OP,
        "target": {"name": "old-appliance"},
        "assertion": _ASSERTION,
        "cadence_kind": "interval",
        "interval_seconds": 60,
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_rest_create_refuses_then_force_creates_and_list_warns(client: TestClient) -> None:
    await _seed()
    await _seed_target("old-appliance", probed="8.0.3")
    key = make_rsa_keypair("kid-fit")
    token = mint_token(
        key, sub="op-admin", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT_A)
    )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {token}"}

        refused = client.post("/api/v1/sensors", json=_rest_body("s1"), headers=headers)
        assert refused.status_code == 422, refused.text
        [detail] = refused.json()["detail"]
        assert detail["type"] == "sensor_connector_mismatch"
        assert detail["loc"] == ["body", "connector_id"]
        assert f"Connector {_CONNECTOR_8} fits this target." in detail["msg"]

        created = client.post("/api/v1/sensors", json=_rest_body("s1", force=True), headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["connector_fit"]["matching_connector_id"] == _CONNECTOR_8

        [item] = client.get("/api/v1/sensors", headers=headers).json()["items"]
        assert item["connector_fit"]["target_product"] == _PRODUCT
        assert item["connector_fit"]["target_version"] == "8.0.3"
        assert _CONNECTOR_8 in item["connector_fit"]["warning"]


def _mcp_admin() -> Operator:
    return Operator(
        sub="mcp-admin",
        raw_jwt="dummy",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN,
        scopes=frozenset({"mcp:admin"}),
    )


@pytest.mark.asyncio
async def test_mcp_create_refuses_with_the_warning_and_accepts_force() -> None:
    await _seed()
    await _seed_target("old-appliance", probed="8.0.3")
    tool = get_tool("meho_sensor_create")
    assert tool is not None
    definition, handler = tool
    assert definition.inputSchema["properties"]["force"]["type"] == "boolean"
    arguments = _rest_body("s1")
    with pytest.raises(McpInvalidParamsError) as caught:
        await handler(_mcp_admin(), arguments)
    assert "sensor_connector_mismatch" in str(caught.value)
    assert _CONNECTOR_8 in str(caught.value)

    result = await handler(_mcp_admin(), {**arguments, "force": True})
    assert result["sensor"]["connector_fit"]["matching_connector_id"] == _CONNECTOR_8


# ---------------------------------------------------------------------------
# Sensors page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensors_page_row_shows_target_version_and_warning() -> None:
    await _seed()
    await _seed_target("old-appliance", probed="8.0.3")
    await _seed_target("new-appliance", probed="9.0.2")
    service = SensorAdminService()
    await service.create(
        _TENANT_A, "op-admin", _payload("bad", _CONNECTOR_9, "old-appliance", force=True)
    )
    await service.create(_TENANT_A, "op-admin", _payload("good", _CONNECTOR_9, "new-appliance"))
    rows = {s.name: project_sensor_to_row(s) for s in await service.list_(_TENANT_A)}
    assert rows["bad"]["target_label"] == f"{_PRODUCT} 8.0.3"
    assert rows["good"]["fit_warning"] is None

    html = (
        get_templates()
        .env.get_template("sensors/_table_rows.html")
        .render(rows=[rows["bad"], rows["good"]], now_utc=datetime.now(UTC))
    )
    assert "old-appliance" in html
    assert f"{_PRODUCT} 9.0.2" in html
    assert html.count("data-fit-warning") == 1
    assert f"Connector {_CONNECTOR_8} fits this target." in html
