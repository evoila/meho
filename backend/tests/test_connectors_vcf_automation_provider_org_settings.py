# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vcfa.provider.org.update`` and ``vcfa.provider.org.networking.set``.

Pins: both wait for approval; turning an organization off sends the whole
organization with ``isEnabled: false``, keeps its name, and warns that its
users cannot sign in; the approval card shows the current values and a state
hash, and the approved run stops when the organization changed; the short log
name is chosen once; vendor errors reach the answer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx

from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import approved_preview_var
from meho_backplane.operations.meta_tools import call_operation
from tests._vcfa_provider_network_support import (
    ORG_ID,
    ORG_NETWORKING_PATH,
    ORG_PATH,
    card,
    org,
)
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    TARGET_NAME,
    mount_logins,
    page,
    run,
    seed_target,
    wire_connector,
)

_UPDATE = "vcfa.provider.org.update"
_NETWORKING = "vcfa.provider.org.networking.set"


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


def _args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": "vcfa-rest-9.0",
        "op_id": op_id,
        "target": TARGET_NAME,
        "params": params,
    }


def _networking(*, enabled: bool = False, short: str = "") -> dict[str, Any]:
    return {"networkingTenancyEnabled": enabled, "orgNameForLogs": short}


# ---------------------------------------------------------------------------
# vcfa.provider.org.update
# ---------------------------------------------------------------------------


async def test_org_update_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        put = mock.put(ORG_PATH).respond(200, json=org(isEnabled=False))
        call = await call_operation(OPERATOR, _args(_UPDATE, {"org": ORG_ID, "is_enabled": False}))
    assert call["status"] == "awaiting_approval", call
    assert not put.called


async def test_turning_an_org_off_warns_that_its_users_cannot_sign_in(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get("/cloudapi/1.0.0/orgs").respond(200, json=page([org()]))
        mock.get(ORG_PATH).respond(200, json=org())
        put = mock.put(ORG_PATH).respond(200, json=org(isEnabled=False))
        result = await run(_UPDATE, {"org": "example-org", "is_enabled": False})
    out = result["result"]
    assert out["status"] == "changed", out
    assert out["previous"]["isEnabled"] is True
    assert out["org"]["isEnabled"] is False
    assert "signing in" in out["guidance"]
    body = json.loads(put.calls.last.request.content)
    assert body["isEnabled"] is False
    assert body["name"] == "example-org"
    assert body["isClassicTenant"] is True


async def test_org_update_card_warns_and_binds_the_state(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        effect = await card(_UPDATE, {"org": ORG_ID, "is_enabled": False}, vcfa)
    assert effect is not None
    preview = effect["preview"]
    assert preview["changes"] == {"isEnabled": False}
    assert preview["current"]["isEnabled"] is True
    assert "signing in" in preview["warning"]
    assert isinstance(preview["state_hash"], str) and preview["state_hash"] != "absent"


async def test_org_update_unchanged_writes_nothing(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        put = mock.put(ORG_PATH).respond(200, json=org())
        result = await run(_UPDATE, {"org": ORG_ID, "is_enabled": True})
    assert result["result"]["status"] == "unchanged"
    assert not put.called


async def test_org_update_202_is_accepted_with_the_task_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.put(ORG_PATH).respond(
            202, headers={"Location": "https://vcfa-prov.test.invalid/api/task/task-1"}
        )
        result = await run(_UPDATE, {"org": ORG_ID, "display_name": "New Name"})
    assert result["result"]["status"] == "accepted"
    assert result["result"]["job_id"] == "task-1"


async def test_org_update_stops_when_the_org_changed_after_approval(
    vcfa: VcfAutomationConnector,
) -> None:
    token = approved_preview_var.set((_UPDATE, {"state_hash": "seen-before-a-change"}))
    try:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
            mount_logins(mock)
            mock.get(ORG_PATH).respond(200, json=org())
            put = mock.put(ORG_PATH).respond(200, json=org(isEnabled=False))
            result = await run(_UPDATE, {"org": ORG_ID, "is_enabled": False})
    finally:
        approved_preview_var.reset(token)
    assert result["result"]["status"] == "precondition_failed"
    assert not put.called


async def test_org_update_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.put(ORG_PATH).respond(400, json={"message": "Display name is too long."})
        result = await run(_UPDATE, {"org": ORG_ID, "display_name": "x" * 200})
    assert result["status"] == "error", result
    assert "Display name is too long." in json.dumps(result)


async def test_org_update_unknown_org_is_invalid_request(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get("/cloudapi/1.0.0/orgs").respond(200, json=page([]))
        result = await run(_UPDATE, {"org": "no-such-org", "is_enabled": False})
    assert result["result"]["status"] == "invalid_request"
    assert "vcfa.provider.org.list" in result["result"]["guidance"]


# ---------------------------------------------------------------------------
# vcfa.provider.org.networking.set
# ---------------------------------------------------------------------------


async def test_networking_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking())
        put = mock.put(ORG_NETWORKING_PATH).respond(200, json=_networking(enabled=True, short="ex"))
        call = await call_operation(
            OPERATOR,
            _args(
                _NETWORKING, {"org": ORG_ID, "networking_enabled": True, "org_name_for_logs": "ex"}
            ),
        )
    assert call["status"] == "awaiting_approval", call
    assert not put.called


async def test_networking_set_turns_networking_on_with_a_short_name(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking())
        put = mock.put(ORG_NETWORKING_PATH).respond(
            200, json=_networking(enabled=True, short="exorg")
        )
        result = await run(
            _NETWORKING, {"org": ORG_ID, "networking_enabled": True, "org_name_for_logs": "exorg"}
        )
    out = result["result"]
    assert out["status"] == "changed", out
    assert out["settings"] == {"networking_enabled": True, "org_name_for_logs": "exorg"}
    assert out["previous"] == {"networking_enabled": False, "org_name_for_logs": None}
    assert json.loads(put.calls.last.request.content) == {
        "networkingTenancyEnabled": True,
        "orgNameForLogs": "exorg",
    }


async def test_networking_set_never_changes_a_short_name(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking(enabled=True, short="exorg"))
        put = mock.put(ORG_NETWORKING_PATH).respond(200, json=_networking())
        result = await run(
            _NETWORKING, {"org": ORG_ID, "networking_enabled": True, "org_name_for_logs": "other"}
        )
    assert result["result"]["status"] == "invalid_request"
    assert "chosen once" in result["result"]["guidance"]
    assert not put.called


async def test_networking_on_needs_a_short_name(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking())
        put = mock.put(ORG_NETWORKING_PATH).respond(200, json=_networking())
        result = await run(_NETWORKING, {"org": ORG_ID, "networking_enabled": True})
    assert result["result"]["status"] == "invalid_request"
    assert "org_name_for_logs" in result["result"]["guidance"]
    assert not put.called


async def test_networking_set_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking(enabled=True, short="exorg"))
        mock.put(ORG_NETWORKING_PATH).respond(
            400, json={"message": "Networking tenancy cannot be disabled while VPCs exist."}
        )
        result = await run(_NETWORKING, {"org": ORG_ID, "networking_enabled": False})
    assert result["status"] == "error", result
    assert "cannot be disabled while VPCs exist" in json.dumps(result)


async def test_networking_card_shows_the_current_settings(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(ORG_PATH).respond(200, json=org())
        mock.get(ORG_NETWORKING_PATH).respond(200, json=_networking())
        effect = await card(
            _NETWORKING,
            {"org": ORG_ID, "networking_enabled": True, "org_name_for_logs": "ex"},
            vcfa,
        )
    assert effect is not None
    preview = effect["preview"]
    assert preview["current"] == {"networking_enabled": False, "org_name_for_logs": None}
    assert preview["would"] == "set_if_different"
    assert "NSX project" in preview["note"]
