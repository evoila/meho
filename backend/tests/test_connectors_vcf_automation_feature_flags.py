# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA provider feature-flag ops and the org-create flag check (evoila/meho#3893).

Drives each op through the real dispatcher (approval already granted)
against a respx-mocked appliance and pins: the list read; ``set`` →
``changed`` (``GET`` for the ``ETag``, ``PUT`` with ``If-Match``); ``set``
→ ``unchanged`` (no ``PUT``); an unknown flag → ``invalid_request`` (no
``PUT``); the park-time preview; and ``vcfa.provider.org.create`` answering
``invalid_request`` before any write while ``CLASSIC_TENANT_CREATION`` is off.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_FEATURE_FLAG_OPS,
    VcfAutomationConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_FLAGS = "/cloudapi/1.0.0/featureFlags"
_CLASSIC_ID = "urn:vcloud:featureflag:CLASSIC_TENANT_CREATION"
_CLASSIC = f"{_FLAGS}/urn%3Avcloud%3Afeatureflag%3ACLASSIC_TENANT_CREATION"
_ORGS = "/cloudapi/1.0.0/orgs"
_ETAG = '"etag-sentinel-1"'


def _flag(*, enabled: bool) -> dict[str, Any]:
    return {
        "id": _CLASSIC_ID,
        "name": "CLASSIC_TENANT_CREATION",
        "usage": "ALPHA",
        "enabled": enabled,
        "displayName": "Classic Tenant Creation",
        "displayDescription": "Allows classic tenants.",
    }


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _mount_flag(mock: respx.MockRouter, *, enabled: bool) -> None:
    mock.get(_FLAGS).respond(200, json=page([_flag(enabled=enabled)]))
    mock.get(_CLASSIC).respond(200, json=_flag(enabled=enabled), headers={"ETag": _ETAG})


async def _preview(params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vcfa.provider.feature_flag.set"
                )
            )
        ).scalar_one()
    return await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id="vcfa-rest-9.0",
        )
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_feature_flag_tiers() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_FEATURE_FLAG_OPS} == {
        "vcfa.provider.feature_flag.list": ("safe", False),
        "vcfa.provider.feature_flag.set": ("caution", True),
    }


async def test_feature_flag_ops_register_as_typed_rows(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id.in_([op.op_id for op in VCFA_FEATURE_FLAG_OPS])
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    assert by_op["vcfa.provider.feature_flag.list"].source_kind == "typed"
    assert by_op["vcfa.provider.feature_flag.set"].requires_approval is True


# ---------------------------------------------------------------------------
# List + set
# ---------------------------------------------------------------------------


async def test_feature_flag_list(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        listing = mock.get(_FLAGS).respond(200, json=page([_flag(enabled=False)]))
        result = await run("vcfa.provider.feature_flag.list", {"pageSize": 50})
    assert result["status"] == "ok", result
    assert result["result"]["values"][0]["name"] == "CLASSIC_TENANT_CREATION"
    assert result["result"]["values"][0]["enabled"] is False
    assert listing.calls.last.request.url.params["pageSize"] == "50"


async def test_feature_flag_set_changed_sends_if_match(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_flag(mock, enabled=False)
        put = mock.put(_CLASSIC).respond(200, json=_flag(enabled=True))
        # The display name resolves too, case-insensitively.
        result = await run(
            "vcfa.provider.feature_flag.set", {"name": "classic tenant creation", "enabled": True}
        )
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "changed"
    assert out["previous_enabled"] is False
    assert out["feature_flag"]["enabled"] is True
    assert out["feature_flag"]["id"] == _CLASSIC_ID
    request = put.calls.last.request
    assert request.headers["If-Match"] == _ETAG
    assert json.loads(request.content) == _flag(enabled=True)


async def test_feature_flag_set_unchanged_writes_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_flag(mock, enabled=True)
        put = mock.put(_CLASSIC).respond(200, json=_flag(enabled=True))
        result = await run(
            "vcfa.provider.feature_flag.set", {"name": "CLASSIC_TENANT_CREATION", "enabled": True}
        )
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["previous_enabled"] is True
    assert not put.called


async def test_feature_flag_set_unknown_name_is_invalid_request(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_flag(mock, enabled=False)
        put = mock.put(_CLASSIC).respond(200, json=_flag(enabled=True))
        result = await run("vcfa.provider.feature_flag.set", {"name": "NO_SUCH", "enabled": True})
    assert result["result"]["status"] == "invalid_request"
    assert result["result"]["feature_flag"] is None
    assert "vcfa.provider.feature_flag.list" in result["result"]["guidance"]
    assert not put.called


async def test_feature_flag_set_preview_shows_current_and_requested_state(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {"name": "CLASSIC_TENANT_CREATION", "enabled": True}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_flag(mock, enabled=False)
        put = mock.put(_CLASSIC).respond(200, json=_flag(enabled=True))
        parked = await _preview(params, vcfa)
    egress_free = await _preview(params, None)
    assert parked is not None and egress_free is not None
    preview = parked["preview"]
    assert preview["name"] == "CLASSIC_TENANT_CREATION"
    assert preview["display_name"] == "Classic Tenant Creation"
    assert preview["current_enabled"] is False
    assert preview["requested_enabled"] is True
    assert preview["would"] == "change"
    assert egress_free["preview"]["would"] == "set_if_different"
    assert egress_free["preview"]["current_enabled"] is None
    assert not put.called


# ---------------------------------------------------------------------------
# Org create: the classic-tenant flag check
# ---------------------------------------------------------------------------


async def test_org_create_vm_apps_with_flag_off_is_invalid_request(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).respond(200, json=page([]))
        _mount_flag(mock, enabled=False)
        post = mock.post(_ORGS).respond(201, json={"id": "x", "name": "example-org"})
        result = await run("vcfa.provider.org.create", {"name": "example-org"})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert out["org"] is None
    assert "vcfa.provider.feature_flag.set" in out["guidance"]
    assert "CLASSIC_TENANT_CREATION" in out["guidance"]
    assert not post.called


async def test_org_create_all_apps_does_not_read_the_flag(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).respond(200, json=page([]))
        flag = mock.get(_CLASSIC).respond(200, json=_flag(enabled=False))
        post = mock.post(_ORGS).respond(
            201, json={"id": "urn:vcloud:org:1", "name": "example-org", "isClassicTenant": False}
        )
        result = await run(
            "vcfa.provider.org.create", {"name": "example-org", "org_type": "all_apps"}
        )
    assert result["result"]["status"] == "created"
    assert post.called
    assert not flag.called


async def test_org_create_goes_ahead_when_the_flag_read_cannot_connect(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ORGS).respond(200, json=page([]))
        flag = mock.get(_CLASSIC).mock(side_effect=httpx.ConnectError("connection refused"))
        post = mock.post(_ORGS).respond(
            201, json={"id": "urn:vcloud:org:1", "name": "example-org", "isClassicTenant": True}
        )
        result = await run("vcfa.provider.org.create", {"name": "example-org"})
    assert result["status"] == "ok", result
    assert result["result"]["status"] == "created"
    assert flag.called
    assert post.called
