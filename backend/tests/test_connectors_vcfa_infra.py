# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Automation 9 infrastructure ops: cloud accounts, cloud zones, profiles, members.

Drives each op through the real dispatcher against a respx-mocked appliance.
Pins the wire calls (path, ``apiVersion``, body), the flat rows of the reads,
the "safe to run twice" answers, the approval wait of every change, the vendor
error text in the answer, the Vault read of the cloud account password (never
in a result), and the preview of the cloud account remove. The handlers are
shared with Aria Automation 8 (``test_connectors_vra8_infra.py``). All names
and ids are made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_CONNECTOR_ID,
    VCFA_IMPL_ID,
    VCFA_INFRA_OPS,
    VCFA_PRODUCT,
    VCFA_TYPED_WHEN_TO_USE_BY_GROUP,
    VCFA_VERSION,
    VcfAutomationConnector,
    _infra,
    _infra_changes,
)
from meho_backplane.connectors.vcf_automation import _lookups as lookups_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import _PREVIEW_BUILDERS
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    PASSWORD,
    mount_logins,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_API_VERSION = "2021-07-15"
_ACCOUNTS = "/iaas/api/cloud-accounts"
_ACCOUNT = f"{_ACCOUNTS}/ca-1"
_ZONES = "/iaas/api/zones"
_FLAVORS = "/iaas/api/flavor-profiles"
_NETWORK_PROFILES = "/iaas/api/network-profiles"
_IP_RANGES = "/iaas/api/network-ip-ranges"
_STORAGE = "/iaas/api/storage-profiles"
_PROJECT = "/iaas/api/projects/p-1"

_EXPECTED: dict[str, tuple[str, bool]] = {
    "vcfa.tenant.cloud_account.list": ("safe", False),
    "vcfa.tenant.cloud_account.create": ("dangerous", True),
    "vcfa.tenant.cloud_account.delete": ("destructive", True),
    "vcfa.tenant.zone.set": ("dangerous", True),
    "vcfa.tenant.flavor_profile.list": ("safe", False),
    "vcfa.tenant.flavor_profile.set": ("dangerous", True),
    "vcfa.tenant.network_profile.list": ("safe", False),
    "vcfa.tenant.storage_profile.list": ("safe", False),
    "vcfa.tenant.project.members.set": ("dangerous", True),
}

_CREATE_PARAMS: dict[str, Any] = {
    "type": "vsphere",
    "name": "<account-name>",
    "host_name": "<vcenter-host>",
    "username": "<username>",
    "password_secret_ref": "example/vcenter-account",
    "regions": [{"external_region_id": "Datacenter:dc-1", "name": "<datacenter>"}],
}


@pytest.fixture(autouse=True)
def _env_and_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    get_settings.cache_clear()


@pytest.fixture
def vault_secret(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the Vault read behind ``password_secret_ref``."""
    data: dict[str, Any] = {"password": PASSWORD}

    async def _load(target: Any, _operator: Any, *, mount: str = "secret") -> dict[str, Any]:
        data["_last_ref"] = (target.secret_ref, mount)
        return {k: v for k, v in data.items() if not k.startswith("_")}

    monkeypatch.setattr(lookups_module, "load_vault_secret_data", _load)
    return data


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def _page(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"content": rows, "numberOfElements": len(rows), "totalElements": len(rows)}


def _links(**hrefs: str) -> dict[str, Any]:
    return {name: {"href": href} for name, href in hrefs.items()}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_each_op_has_the_risk_level_and_approval_of_the_table() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_INFRA_OPS} == (
        _EXPECTED
    )
    assert {op.plane for op in VCFA_INFRA_OPS} == {"tenant"}


async def test_registered_rows_carry_the_risk_level_and_approval() -> None:
    await VcfAutomationConnector.register_typed_operations()
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VCFA_PRODUCT,
                    EndpointDescriptor.version == VCFA_VERSION,
                    EndpointDescriptor.impl_id == VCFA_IMPL_ID,
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    for op_id, (safety, approval) in _EXPECTED.items():
        assert by_op[op_id].source_kind == "typed", op_id
        assert (by_op[op_id].safety_level, by_op[op_id].requires_approval) == (safety, approval)


def test_group_text_names_every_op_and_the_zone_list() -> None:
    text = VCFA_TYPED_WHEN_TO_USE_BY_GROUP["vcfa-tenant-infra"]
    for op_id in _EXPECTED:
        assert op_id in text, op_id
    assert "vcfa.tenant.zone.list" in text


def test_the_delete_has_a_preview_builder() -> None:
    assert _PREVIEW_BUILDERS["vcfa.tenant.cloud_account.delete"] is (
        _infra.cloud_account_delete_preview
    )


@pytest.mark.parametrize("bad", ["", ".", "..", "   "])
def test_a_path_id_must_name_one_object(bad: str) -> None:
    with pytest.raises(ValueError, match="not valid"):
        _infra.segment(bad)


def test_a_path_id_is_percent_encoded() -> None:
    assert _infra.segment("a/b c") == "a%2Fb%20c"


async def test_a_dot_dot_id_is_refused_before_any_call(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        catch_all = mock.route().respond(200, json={})
        result = await run(
            "vcfa.tenant.project.members.set", {"project_id": "..", "remove": [{"email": "<user>"}]}
        )
    assert result["status"] == "error"
    assert result["extras"]["error_code"] == "invalid_params", result
    assert catch_all.call_count == 0


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_cloud_account_list_is_flat_and_has_no_credentials(
    vcfa: VcfAutomationConnector,
) -> None:
    account = {
        "id": "ca-1",
        "name": "<account-name>",
        "cloudAccountType": "vsphere",
        "healthy": True,
        "inMaintenanceMode": False,
        "cloudAccountProperties": {"hostName": "<vcenter-host>", "privateKey": "pk-sentinel"},
        "customProperties": {"password": "pw-sentinel"},
        "enabledRegions": [{"id": "r-1", "externalRegionId": "Datacenter:dc-1", "name": "<dc>"}],
        "tags": [{"key": "env", "value": "lab"}],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_ACCOUNTS).respond(200, json=_page([account]))
        result = await run("vcfa.tenant.cloud_account.list", {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["totalElements"] == 1
    assert out["items"][0] == {
        "id": "ca-1",
        "name": "<account-name>",
        "type": "vsphere",
        "description": None,
        "hostName": "<vcenter-host>",
        "healthy": True,
        "inMaintenanceMode": False,
        "regions": [{"id": "r-1", "externalRegionId": "Datacenter:dc-1", "name": "<dc>"}],
        "tags": [{"key": "env", "value": "lab"}],
        "createdAt": None,
        "updatedAt": None,
    }
    assert "sentinel" not in json.dumps(result)
    query = route.calls.last.request.url.params
    assert query["apiVersion"] == _API_VERSION
    assert query["$top"] == "200" and query["$skip"] == "0"


async def test_a_list_reads_every_page(vcfa: VcfAutomationConnector) -> None:
    first = [{"id": f"ca-{n}", "name": f"<a{n}>"} for n in range(200)]

    def _answer(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["$skip"])
        rows = first if skip == 0 else [{"id": "ca-last", "name": "<last>"}]
        return httpx.Response(200, json={"content": rows, "totalElements": 201})

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_ACCOUNTS).mock(side_effect=_answer)
        result = await run("vcfa.tenant.cloud_account.list", {})
    assert len(result["result"]["items"]) == 201
    assert route.call_count == 2


async def test_network_profile_list_puts_ip_ranges_under_their_profile(
    vcfa: VcfAutomationConnector,
) -> None:
    profile = {
        "id": "np-1",
        "name": "<profile>",
        "externalRegionId": "Datacenter:dc-1",
        "isolationType": "NONE",
        "_links": {
            "fabric-networks": {
                "hrefs": [
                    "/iaas/api/fabric-networks/net-1",
                    "/iaas/api/fabric-networks-vsphere/net-2",
                ]
            },
            "self": {"href": "/iaas/api/network-profiles/np-1"},
        },
    }
    ranges = [
        {
            "id": "ipr-1",
            "name": "<range>",
            "startIPAddress": "192.0.2.10",
            "endIPAddress": "192.0.2.50",
            "_links": _links(**{"fabric-network": "/iaas/api/fabric-networks/net-1"}),
        },
        {
            "id": "ipr-9",
            "startIPAddress": "198.51.100.1",
            "endIPAddress": "198.51.100.9",
            "_links": _links(**{"fabric-network": "/iaas/api/fabric-networks/net-9"}),
        },
    ]
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_NETWORK_PROFILES).respond(200, json=_page([profile]))
        mock.get(_IP_RANGES).respond(200, json=_page(ranges))
        result = await run("vcfa.tenant.network_profile.list", {})
    row = result["result"]["items"][0]
    assert row["fabricNetworkIds"] == ["net-1", "net-2"]
    assert [r["id"] for r in row["ipRanges"]] == ["ipr-1"]
    assert row["ipRanges"][0]["fabricNetworkId"] == "net-1"


async def test_flavor_and_storage_profile_lists_are_flat(vcfa: VcfAutomationConnector) -> None:
    flavor = {
        "id": "fp-1",
        "name": "<flavors>",
        "externalRegionId": "Datacenter:dc-1",
        "flavorMappings": {"mapping": {"small": {"cpuCount": 1, "memoryInMB": 2048}}},
        "_links": _links(region="/iaas/api/regions/r-1"),
    }
    storage = {"id": "sp-1", "name": "<storage>", "defaultItem": True, "diskProperties": {"a": "b"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        flavors = mock.get(_FLAVORS).respond(200, json=_page([flavor]))
        mock.get(_STORAGE).respond(200, json=_page([storage]))
        flavor_result = await run("vcfa.tenant.flavor_profile.list", {})
        storage_result = await run("vcfa.tenant.storage_profile.list", {})
    flavor_row = flavor_result["result"]["items"][0]
    assert flavor_row["regionId"] == "r-1"
    assert flavor_row["flavors"] == [
        {"key": "small", "name": None, "cpuCount": 1, "memoryInMB": 2048}
    ]
    assert "$top" not in flavors.calls.last.request.url.params
    storage_row = storage_result["result"]["items"][0]
    assert storage_row["defaultItem"] is True
    assert storage_row["diskProperties"] == {"a": "b"}


# ---------------------------------------------------------------------------
# Add a cloud account
# ---------------------------------------------------------------------------


async def test_cloud_account_create_reads_the_password_from_vault(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([]))
        post = mock.post("/iaas/api/cloud-accounts-vsphere").respond(
            202, json={"id": "t-1", "status": "INPROGRESS", "progress": 0}
        )
        result = await run("vcfa.tenant.cloud_account.create", _CREATE_PARAMS)
    out = result["result"]
    assert out["status"] == "submitted", out
    assert out["request"]["id"] == "t-1"
    assert "vcfa.tenant.cloud_account.list" in out["guidance"]
    body = _body(post)
    assert body["password"] == PASSWORD
    assert body["hostName"] == "<vcenter-host>"
    assert body["regions"] == [{"externalRegionId": "Datacenter:dc-1", "name": "<datacenter>"}]
    assert body["acceptSelfSignedCertificate"] is False
    assert post.calls.last.request.url.params["apiVersion"] == _API_VERSION
    assert vault_secret["_last_ref"] == ("example/vcenter-account", "secret")
    assert PASSWORD not in json.dumps(result)


async def test_nsx_account_goes_to_the_nsx_path(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    params = {
        "type": "nsx-t",
        "name": "<nsx-account>",
        "host_name": "<nsx-host>",
        "username": "<username>",
        "password_secret_ref": "example/nsx-account",
        "associated_cloud_account_ids": ["ca-1"],
        "manager_mode": False,
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([]))
        post = mock.post("/iaas/api/cloud-accounts-nsx-t").respond(202, json={"id": "t-2"})
        result = await run("vcfa.tenant.cloud_account.create", params)
    assert result["result"]["status"] == "submitted", result
    body = _body(post)
    assert body["associatedCloudAccountIds"] == ["ca-1"]
    assert body["managerMode"] is False
    assert "regions" not in body


async def test_cloud_account_create_is_unchanged_when_the_name_exists(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    existing = {"id": "ca-1", "name": "<account-name>", "cloudAccountType": "vsphere"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([existing]))
        post = mock.post("/iaas/api/cloud-accounts-vsphere").respond(202, json={})
        result = await run("vcfa.tenant.cloud_account.create", _CREATE_PARAMS)
    assert result["result"]["status"] == "unchanged"
    assert result["result"]["cloud_account"]["id"] == "ca-1"
    assert not post.called
    assert "_last_ref" not in vault_secret


async def test_a_vcenter_account_without_regions_fails_the_schema(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {k: v for k, v in _CREATE_PARAMS.items() if k != "regions"}
    result = await run("vcfa.tenant.cloud_account.create", params)
    assert result["extras"]["error_code"] == "invalid_params", result


async def test_the_password_is_not_an_accepted_param(vcfa: VcfAutomationConnector) -> None:
    result = await run("vcfa.tenant.cloud_account.create", {**_CREATE_PARAMS, "password": "x"})
    assert result["extras"]["error_code"] == "invalid_params", result


async def test_a_vendor_error_never_shows_the_password(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([]))
        mock.post("/iaas/api/cloud-accounts-vsphere").respond(
            400, json={"message": f"Login failed for password {PASSWORD}"}
        )
        result = await run("vcfa.tenant.cloud_account.create", _CREATE_PARAMS)
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 400
    # The op sends a secret, so MEHO keeps the vendor text out of the answer.
    assert "withheld" in result["extras"]["upstream_message"]
    assert PASSWORD not in json.dumps(result)


async def test_cloud_account_create_waits_for_approval(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([]))
        post = mock.post("/iaas/api/cloud-accounts-vsphere").respond(202, json={})
        result = await run("vcfa.tenant.cloud_account.create", _CREATE_PARAMS, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not post.called


async def test_the_add_preview_shows_the_vault_place_not_the_password(
    vcfa: VcfAutomationConnector, vault_secret: dict[str, Any]
) -> None:
    assert _PREVIEW_BUILDERS["vcfa.tenant.cloud_account.create"] is (
        _infra_changes.cloud_account_create_preview
    )
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params=_CREATE_PARAMS,
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNTS).respond(200, json=_page([]))
        preview = await _infra_changes.cloud_account_create_preview(ctx)
    assert preview is not None
    assert preview["would"] == "create"
    assert preview["host_name"] == "<vcenter-host>"
    assert preview["password_source"] == {
        "secret_ref": "example/vcenter-account",
        "mount": "secret",
        "key": "password",
    }
    assert PASSWORD not in json.dumps(preview)


# ---------------------------------------------------------------------------
# Remove a cloud account
# ---------------------------------------------------------------------------


def _mount_delete_reads(mock: respx.MockRouter) -> None:
    mock.get(_ACCOUNT).respond(
        200, json={"id": "ca-1", "name": "<account-name>", "cloudAccountType": "vsphere"}
    )
    mock.get(_ZONES).respond(
        200,
        json=_page(
            [
                {"id": "z-1", "name": "<zone>", "cloudAccountId": "ca-1"},
                {"id": "z-9", "name": "<other-zone>", "cloudAccountId": "ca-9"},
            ]
        ),
    )
    mock.get(_FLAVORS).respond(
        200, json=_page([{"id": "fp-1", "name": "<flavors>", "cloudAccountId": "ca-1"}])
    )
    mock.get(_NETWORK_PROFILES).respond(200, json=_page([]))
    mock.get(_STORAGE).respond(
        200, json=_page([{"id": "sp-1", "name": "<storage>", "cloudAccountId": "ca-1"}])
    )


async def test_the_delete_preview_lists_what_else_is_affected(
    vcfa: VcfAutomationConnector,
) -> None:
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params={"id": "ca-1"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_delete_reads(mock)
        preview = await _infra.cloud_account_delete_preview(ctx)
    assert preview is not None
    radius = preview["blast_radius"]
    assert radius["object"] == {
        "kind": "cloud_account",
        "id": "ca-1",
        "name": "<account-name>",
        "type": "vsphere",
    }
    assert radius["children"] == [
        {"kind": "cloud_zone", "id": "z-1", "name": "<zone>"},
        {"kind": "flavor_profile", "id": "fp-1", "name": "<flavors>"},
        {"kind": "storage_profile", "id": "sp-1", "name": "<storage>"},
    ]
    assert radius["irreversibility"] == "permanent"


async def test_the_delete_preview_refuses_an_account_that_does_not_exist(
    vcfa: VcfAutomationConnector,
) -> None:
    ctx: Any = SimpleNamespace(
        connector_instance=vcfa,
        operator=OPERATOR,
        target=await resolved_target(),
        params={"id": "ca-1"},
    )
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ACCOUNT).respond(404, json={"message": "not found"})
        with pytest.raises(LookupError, match="no cloud account"):
            await _infra.cloud_account_delete_preview(ctx)


async def test_the_delete_preview_without_a_connector_names_the_id() -> None:
    ctx: Any = SimpleNamespace(connector_instance=None, params={"id": "ca-1"})
    assert await _infra.cloud_account_delete_preview(ctx) == {
        "action": "delete_cloud_account",
        "id": "ca-1",
    }


@pytest.mark.parametrize(
    ("preview_hash", "code"),
    [(None, "preview_binding_required"), ("0" * 64, "preview_hash_mismatch")],
)
async def test_the_delete_is_refused_without_a_matching_preview_hash(
    vcfa: VcfAutomationConnector, preview_hash: str | None, code: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_delete_reads(mock)
        delete = mock.delete(_ACCOUNT).respond(202, json={"id": "t-1"})
        result = await dispatch(
            operator=OPERATOR,
            connector_id=VCFA_CONNECTOR_ID,
            op_id="vcfa.tenant.cloud_account.delete",
            target=await resolved_target(),
            params={"id": "ca-1"},
            preview_hash=preview_hash,
        )
    assert result.status == "denied", result
    assert result.extras["error_code"] == code
    assert not delete.called


async def test_an_approved_delete_runs(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        delete = mock.delete(_ACCOUNT).respond(
            202, json={"id": "t-1", "status": "INPROGRESS", "progress": 0}
        )
        result = await run("vcfa.tenant.cloud_account.delete", {"id": "ca-1"})
    assert result["result"]["status"] == "submitted", result
    assert result["result"]["request"]["id"] == "t-1"
    assert delete.calls.last.request.url.params["apiVersion"] == _API_VERSION


async def test_a_second_delete_is_unchanged(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete(_ACCOUNT).respond(404, json={"message": "not found"})
        result = await run("vcfa.tenant.cloud_account.delete", {"id": "ca-1"})
    assert result["result"]["status"] == "unchanged", result


async def test_a_delete_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.delete(_ACCOUNT).respond(400, json={"message": "Account is in use"})
        result = await run("vcfa.tenant.cloud_account.delete", {"id": "ca-1"})
    assert result["status"] == "error"
    assert "Account is in use" in json.dumps(result)


# ---------------------------------------------------------------------------
# Cloud zones
# ---------------------------------------------------------------------------

_ZONE = {
    "id": "z-1",
    "name": "<zone>",
    "placementPolicy": "DEFAULT",
    "tagsToMatch": [{"key": "cluster", "value": "a"}],
    "_links": _links(region="/iaas/api/regions/r-1"),
}


async def test_zone_set_creates_a_zone(vcfa: VcfAutomationConnector) -> None:
    params = {"name": "<new-zone>", "region_id": "r-1", "placement_policy": "SPREAD"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ZONES).respond(200, json=_page([_ZONE]))
        post = mock.post(_ZONES).respond(201, json={"id": "z-2", "name": "<new-zone>"})
        result = await run("vcfa.tenant.zone.set", params)
    assert result["result"]["status"] == "created", result
    assert _body(post) == {"regionId": "r-1", "name": "<new-zone>", "placementPolicy": "SPREAD"}


async def test_zone_set_changes_only_what_differs(vcfa: VcfAutomationConnector) -> None:
    params = {"id": "z-1", "tags_to_match": [{"key": "cluster", "value": "b"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ZONES}/z-1").respond(200, json=_ZONE)
        patch = mock.patch(f"{_ZONES}/z-1").respond(200, json={**_ZONE, "tagsToMatch": []})
        result = await run("vcfa.tenant.zone.set", params)
    out = result["result"]
    assert out["status"] == "updated", out
    assert out["changed"] == ["tagsToMatch"]
    assert _body(patch) == {
        "name": "<zone>",
        "placementPolicy": "DEFAULT",
        "regionId": "r-1",
        "tagsToMatch": [{"key": "cluster", "value": "b"}],
    }


async def test_zone_set_is_unchanged_when_nothing_differs(vcfa: VcfAutomationConnector) -> None:
    params = {"id": "z-1", "placement_policy": "DEFAULT"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_ZONES}/z-1").respond(200, json=_ZONE)
        patch = mock.patch(f"{_ZONES}/z-1").respond(200, json=_ZONE)
        result = await run("vcfa.tenant.zone.set", params)
    assert result["result"]["status"] == "unchanged"
    assert not patch.called


async def test_zone_set_a_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_ZONES).respond(200, json=_page([]))
        mock.post(_ZONES).respond(400, json={"message": "Region not found"})
        result = await run("vcfa.tenant.zone.set", {"name": "<zone>", "region_id": "r-x"})
    assert result["status"] == "error"
    assert "Region not found" in json.dumps(result)


async def test_zone_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_ZONES).respond(201, json={})
        result = await run(
            "vcfa.tenant.zone.set", {"name": "<zone>", "region_id": "r-1"}, approved=False
        )
    assert result["status"] == "awaiting_approval", result
    assert not post.called


# ---------------------------------------------------------------------------
# Flavor profiles
# ---------------------------------------------------------------------------

_FLAVOR_PROFILE = {
    "id": "fp-1",
    "name": "<flavors>",
    "flavorMappings": {
        "mapping": {
            "small": {"cpuCount": 1, "memoryInMB": 2048},
            "large": {"cpuCount": 8, "memoryInMB": 32768},
        }
    },
}


async def test_flavor_profile_set_adds_and_removes_flavors(vcfa: VcfAutomationConnector) -> None:
    params = {
        "id": "fp-1",
        "flavors": {"medium": {"cpu_count": 2, "memory_mb": 4096}},
        "remove_flavors": ["large"],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_FLAVORS}/fp-1").respond(200, json=_FLAVOR_PROFILE)
        patch = mock.patch(f"{_FLAVORS}/fp-1").respond(200, json=_FLAVOR_PROFILE)
        result = await run("vcfa.tenant.flavor_profile.set", params)
    assert result["result"]["status"] == "updated", result
    assert _body(patch) == {
        "flavorMapping": {
            "small": {"cpuCount": 1, "memoryInMB": 2048},
            "medium": {"cpuCount": 2, "memoryInMB": 4096},
        }
    }


async def test_flavor_profile_set_is_unchanged_for_the_same_flavor(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {"id": "fp-1", "flavors": {"small": {"cpu_count": 1, "memory_mb": 2048}}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_FLAVORS}/fp-1").respond(200, json=_FLAVOR_PROFILE)
        patch = mock.patch(f"{_FLAVORS}/fp-1").respond(200, json={})
        result = await run("vcfa.tenant.flavor_profile.set", params)
    assert result["result"]["status"] == "unchanged"
    assert not patch.called


async def test_flavor_profile_set_creates_a_profile(vcfa: VcfAutomationConnector) -> None:
    params = {
        "name": "<flavors>",
        "region_id": "r-1",
        "flavors": {"small": {"cpu_count": 1, "memory_mb": 2048}},
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_FLAVORS).respond(200, json=_page([]))
        post = mock.post(_FLAVORS).respond(201, json={"id": "fp-2", "name": "<flavors>"})
        result = await run("vcfa.tenant.flavor_profile.set", params)
    assert result["result"]["status"] == "created", result
    assert _body(post) == {
        "name": "<flavors>",
        "regionId": "r-1",
        "flavorMapping": {"small": {"cpuCount": 1, "memoryInMB": 2048}},
    }


async def test_flavor_profile_set_a_vendor_error_reaches_the_answer(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(f"{_FLAVORS}/fp-1").respond(200, json=_FLAVOR_PROFILE)
        mock.patch(f"{_FLAVORS}/fp-1").respond(400, json={"message": "Invalid flavor"})
        result = await run(
            "vcfa.tenant.flavor_profile.set", {"id": "fp-1", "remove_flavors": ["small"]}
        )
    assert result["status"] == "error"
    assert "Invalid flavor" in json.dumps(result)


# ---------------------------------------------------------------------------
# Project members
# ---------------------------------------------------------------------------

_PROJECT_ROW = {
    "id": "p-1",
    "name": "<project>",
    "administrators": [{"email": "<admin>", "type": "user"}],
    "members": [{"email": "<member>", "type": "user"}],
    "viewers": [],
}


async def test_members_set_adds_moves_and_removes(vcfa: VcfAutomationConnector) -> None:
    params = {
        "project_id": "p-1",
        "add": [
            {"email": "<member>", "role": "administrator"},
            {"email": "<team>", "type": "group", "role": "viewer"},
        ],
        "remove": [{"email": "<admin>"}],
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECT).respond(200, json=_PROJECT_ROW)
        patch = mock.patch(_PROJECT).respond(200, json=_PROJECT_ROW)
        result = await run("vcfa.tenant.project.members.set", params)
    out = result["result"]
    assert out["status"] == "updated", out
    assert out["changed"] == ["administrators", "members", "viewers"]
    assert _body(patch) == {
        "name": "<project>",
        "administrators": [{"email": "<member>", "type": "user"}],
        "members": [],
        "viewers": [{"email": "<team>", "type": "group"}],
    }


async def test_members_set_is_unchanged_when_nothing_differs(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {"project_id": "p-1", "add": [{"email": "<member>", "role": "member"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECT).respond(200, json=_PROJECT_ROW)
        patch = mock.patch(_PROJECT).respond(200, json={})
        result = await run("vcfa.tenant.project.members.set", params)
    assert result["result"]["status"] == "unchanged", result
    assert not patch.called


async def test_members_set_needs_add_or_remove(vcfa: VcfAutomationConnector) -> None:
    result = await run("vcfa.tenant.project.members.set", {"project_id": "p-1"})
    assert result["extras"]["error_code"] == "invalid_params", result


async def test_members_set_a_vendor_error_reaches_the_answer(vcfa: VcfAutomationConnector) -> None:
    params = {"project_id": "p-1", "remove": [{"email": "<member>"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROJECT).respond(200, json=_PROJECT_ROW)
        mock.patch(_PROJECT).respond(403, json={"message": "Not a project administrator"})
        result = await run("vcfa.tenant.project.members.set", params)
    assert result["status"] == "error"
    assert "Not a project administrator" in json.dumps(result)


async def test_members_set_waits_for_approval(vcfa: VcfAutomationConnector) -> None:
    params = {"project_id": "p-1", "remove": [{"email": "<member>"}]}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        patch = mock.patch(_PROJECT).respond(200, json={})
        result = await run("vcfa.tenant.project.members.set", params, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert not patch.called
