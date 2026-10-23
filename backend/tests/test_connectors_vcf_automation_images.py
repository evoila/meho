# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA image ops: image profiles, fabric images, image enumeration, one-name change.

Drives each op through the real dispatcher against a respx-mocked
appliance. The change op is pinned on the two vendor traps: the PATCH
carries every other name of the profile, and the region comes from the
profile's own ``_links.region.href``. All names are synthetic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.connectors.vcf_automation import (
    VCFA_IMAGE_OPS,
    VCFA_IMPL_ID,
    VCFA_PRODUCT,
    VCFA_VERSION,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation import _images as images_module
from meho_backplane.connectors.vcf_automation._images import plan_mapping_change
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
    TENANT_TOKEN,
    mount_logins,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_PROFILES = "/iaas/api/image-profiles"
_PROFILE = "/iaas/api/image-profiles/ip-1"
_FABRIC = "/iaas/api/fabric-images"
_ENUM = "/iaas/api/cloud-accounts/ca-1/private-image-enumeration"
_TRACKER = "/iaas/api/request-tracker/rt-1"

_EXPECTED_TIERS: dict[str, tuple[str, bool]] = {
    "vcfa.tenant.image_profile.list": ("safe", False),
    "vcfa.tenant.fabric_image.list": ("safe", False),
    "vcfa.tenant.cloud_account.enumerate_images": ("safe", False),
    "vcfa.tenant.image_profile.mapping.update": ("caution", True),
}


def _profile(**mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "ip-1",
        "name": "example-profile",
        "description": "images of region a",
        "externalRegionId": "region-a",
        "cloudAccountId": "ca-1",
        "imageMappings": {"mapping": mapping},
        "_links": {"region": {"href": "/iaas/api/regions/rg-a"}},
    }


_UBUNTU = {"id": "fi-ubuntu", "name": "ubuntu-template", "externalId": "vm-1"}
_WIN = {
    "id": "fi-win",
    "name": "win-template",
    "externalId": "vm-2",
    "constraints": [{"expression": "env:lab", "mandatory": True}],
    "cloudConfig": "#cloud-config",
}
_RHEL = {"id": "fi-rhel", "name": "rhel9-template", "externalRegionId": "region-a"}


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
    monkeypatch.setattr(images_module, "ENUMERATION_POLL_SECONDS", 0.0)
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


def _body(route: respx.Route) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(route.calls.last.request.content)
    return parsed


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_image_op_tiers() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_IMAGE_OPS} == (
        _EXPECTED_TIERS
    )


async def test_image_ops_register_as_typed_rows() -> None:
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
    for op_id, (safety, approval) in _EXPECTED_TIERS.items():
        row = by_op[op_id]
        assert row.source_kind == "typed"
        assert (row.safety_level, row.requires_approval) == (safety, approval)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_image_profile_list_pins_api_version(vcfa: VcfAutomationConnector) -> None:
    listing = {"content": [_profile(**{"RHEL 9": _UBUNTU})], "totalElements": 1}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_PROFILES).respond(200, json=listing)
        result = await run("vcfa.tenant.image_profile.list", {})
    assert result["status"] == "ok", result
    assert result["result"] == listing
    request = route.calls.last.request
    assert request.url.params["apiVersion"] == "2021-07-15"
    assert request.headers["Authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_fabric_image_list_forwards_odata(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        route = mock.get(_FABRIC).respond(200, json={"content": [_RHEL], "totalElements": 1})
        result = await run(
            "vcfa.tenant.fabric_image.list", {"$filter": "name eq 'rhel9-template'", "$top": 5}
        )
    assert result["result"]["content"] == [_RHEL]
    params = route.calls.last.request.url.params
    assert params["$filter"] == "name eq 'rhel9-template'"
    assert params["$top"] == "5"
    assert params["apiVersion"] == "2021-07-15"


# ---------------------------------------------------------------------------
# Image enumeration
# ---------------------------------------------------------------------------


async def test_enumerate_images_waits_until_finished(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post(_ENUM).respond(
            202, json={"id": "rt-1", "status": "INPROGRESS", "progress": 0}
        )
        tracker = mock.get(_TRACKER)
        tracker.side_effect = [
            respx.MockResponse(200, json={"id": "rt-1", "status": "INPROGRESS", "progress": 50}),
            respx.MockResponse(
                200, json={"id": "rt-1", "status": "FINISHED", "progress": 100, "message": "ok"}
            ),
        ]
        result = await run(
            "vcfa.tenant.cloud_account.enumerate_images", {"cloud_account_id": "ca-1"}
        )
    out = result["result"]
    assert out["status"] == "finished", out
    assert out["request"] == {"id": "rt-1", "status": "FINISHED", "progress": 100, "message": "ok"}
    assert post.calls.last.request.url.params["apiVersion"] == "2021-07-15"
    assert tracker.call_count == 2


async def test_enumerate_images_failed_is_a_result(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_ENUM).respond(
            202, json={"id": "rt-1", "status": "FAILED", "progress": 100, "message": "no vCenter"}
        )
        tracker = mock.get(_TRACKER).respond(200, json={})
        result = await run(
            "vcfa.tenant.cloud_account.enumerate_images", {"cloud_account_id": "ca-1"}
        )
    assert result["result"]["status"] == "failed"
    assert result["result"]["request"]["message"] == "no vCenter"
    assert not tracker.called


async def test_enumerate_images_stops_waiting_at_the_deadline(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images_module, "ENUMERATION_WAIT_SECONDS", 0.0)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_ENUM).respond(202, json={"id": "rt-1", "status": "INPROGRESS", "progress": 0})
        result = await run(
            "vcfa.tenant.cloud_account.enumerate_images", {"cloud_account_id": "ca-1"}
        )
    assert result["result"]["status"] == "in_progress"
    assert "again" in result["result"]["guidance"]


async def test_enumerate_images_encodes_the_account_id(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        post = mock.post("/iaas/api/cloud-accounts/a%2F..%2Fb/private-image-enumeration").respond(
            202, json={"id": "rt-1", "status": "FINISHED", "progress": 100}
        )
        result = await run(
            "vcfa.tenant.cloud_account.enumerate_images", {"cloud_account_id": "a/../b"}
        )
    assert result["result"]["status"] == "finished", result
    assert post.called


async def test_enumerate_images_without_request_id_is_an_error(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_ENUM).respond(202, json={"status": "INPROGRESS"})
        result = await run(
            "vcfa.tenant.cloud_account.enumerate_images", {"cloud_account_id": "ca-1"}
        )
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# One-name change: the plan (no calls)
# ---------------------------------------------------------------------------


def test_plan_repoint_keeps_every_other_name_and_the_profile_region() -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    plan = plan_mapping_change(profile, "RHEL 9", fabric_image=_RHEL)
    assert plan.action == "repoint"
    assert plan.old_target == {
        "fabric_image_id": "fi-ubuntu",
        "name": "ubuntu-template",
        "externalId": "vm-1",
    }
    assert plan.new_target is not None and plan.new_target["fabric_image_id"] == "fi-rhel"
    assert plan.other_names == ["Windows 2019"]
    assert plan.body == {
        "name": "example-profile",
        "description": "images of region a",
        "regionId": "rg-a",
        "imageMapping": {
            "RHEL 9": {"id": "fi-rhel"},
            "Windows 2019": {
                "id": "fi-win",
                "constraints": [{"expression": "env:lab", "mandatory": True}],
                "cloudConfig": "#cloud-config",
            },
        },
    }


def test_plan_repoint_keeps_the_constraints_of_the_changed_name() -> None:
    plan = plan_mapping_change(
        _profile(**{"Windows 2019": _WIN}), "Windows 2019", fabric_image=_RHEL
    )
    assert plan.body["imageMapping"]["Windows 2019"] == {
        "id": "fi-rhel",
        "constraints": [{"expression": "env:lab", "mandatory": True}],
        "cloudConfig": "#cloud-config",
    }


def test_plan_add_remove_and_no_change() -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU})
    assert plan_mapping_change(profile, "Rocky 9", fabric_image=_RHEL).action == "add"
    removed = plan_mapping_change(profile, "RHEL 9", fabric_image=None)
    assert removed.action == "remove"
    assert removed.body["imageMapping"] == {}
    assert plan_mapping_change(profile, "Gone", fabric_image=None).action is None
    assert plan_mapping_change(profile, "RHEL 9", fabric_image=_UBUNTU).action is None


def test_plan_without_region_link_sends_no_region() -> None:
    profile = {**_profile(**{"RHEL 9": _UBUNTU}), "_links": {}}
    plan = plan_mapping_change(profile, "RHEL 9", fabric_image=_RHEL)
    assert plan.region_id is None
    assert "regionId" not in plan.body


def test_plan_entry_without_id_is_sent_back_by_name() -> None:
    profile = _profile(**{"Old": {"name": "old-template"}, "Older": {"externalId": "vm-9"}})
    plan = plan_mapping_change(profile, "RHEL 9", fabric_image=_RHEL)
    assert plan.body["imageMapping"]["Old"] == {"name": "old-template"}
    assert plan.body["imageMapping"]["Older"] == {"externalId": "vm-9"}


# ---------------------------------------------------------------------------
# One-name change: through the dispatcher
# ---------------------------------------------------------------------------


def _mount_change(mock: Any, profile: dict[str, Any], *, patched: dict[str, Any]) -> respx.Route:
    mount_logins(mock)
    mock.get(_PROFILE).respond(200, json=profile)
    mock.get("/iaas/api/fabric-images/fi-rhel").respond(200, json=_RHEL)
    mock.get("/iaas/api/fabric-images/fi-missing").respond(404, json={"message": "not found"})
    return mock.patch(_PROFILE).respond(200, json=patched)


async def test_mapping_update_repoints_one_name(vcfa: VcfAutomationConnector) -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    patched = _profile(**{"RHEL 9": _RHEL, "Windows 2019": _WIN})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, profile, patched=patched)
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    out = result["result"]
    assert out["status"] == "updated", out
    assert out["change"]["action"] == "repoint"
    assert out["change"]["old_target"]["name"] == "ubuntu-template"
    assert out["change"]["new_target"]["name"] == "rhel9-template"
    assert out["other_names"] == ["Windows 2019"]
    assert out["guidance"] is None
    request = patch.calls.last.request
    assert request.url.params["apiVersion"] == "2021-07-15"
    body = _body(patch)
    assert body["regionId"] == "rg-a"
    assert set(body["imageMapping"]) == {"RHEL 9", "Windows 2019"}


async def test_mapping_update_warns_when_the_answer_lost_a_name(
    vcfa: VcfAutomationConnector,
) -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, profile, patched=_profile(**{"RHEL 9": _RHEL}))
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    assert result["result"]["status"] == "updated"
    assert "Windows 2019" in result["result"]["guidance"]


async def test_mapping_update_warns_when_the_answer_has_an_empty_mapping(
    vcfa: VcfAutomationConnector,
) -> None:
    # An empty mapping after a nonempty write lacks every name; it is not "no answer".
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, profile, patched=_profile())
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    out = result["result"]
    assert out["status"] == "updated"
    assert "['RHEL 9', 'Windows 2019']" in out["guidance"]


async def test_mapping_update_warns_when_the_answer_has_no_mapping(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    out = result["result"]
    assert out["status"] == "updated"
    assert "without a mapping" in out["guidance"]


async def test_mapping_update_removing_the_last_name_accepts_an_empty_answer(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched=_profile())
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "remove": True},
        )
    assert result["result"]["status"] == "updated"
    assert result["result"]["guidance"] is None


async def test_mapping_update_remove(vcfa: VcfAutomationConnector) -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, profile, patched=_profile(**{"Windows 2019": _WIN}))
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "remove": True},
        )
    assert result["result"]["status"] == "updated"
    assert result["result"]["change"]["action"] == "remove"
    assert set(_body(patch)["imageMapping"]) == {"Windows 2019"}


def _mapping_update_schema() -> dict[str, Any]:
    op = next(op for op in VCFA_IMAGE_OPS if op.op_id == "vcfa.tenant.image_profile.mapping.update")
    schema: dict[str, Any] = op.response_schema
    Draft202012Validator.check_schema(schema)
    return schema


async def test_mapping_update_add_and_remove_results_meet_the_response_schema(
    vcfa: VcfAutomationConnector,
) -> None:
    # An add has no old target and a remove has no new target: both are null.
    validator = Draft202012Validator(_mapping_update_schema())
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched=_profile())
        added = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "Rocky 9", "fabric_image_id": "fi-rhel"},
        )
        removed = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "remove": True},
        )
    assert added["result"]["change"]["old_target"] is None
    assert removed["result"]["change"]["new_target"] is None
    for result in (added, removed):
        assert result["result"]["status"] == "updated", result
        assert list(validator.iter_errors(result["result"])) == []


class _Appliance:
    """A profile that keeps the mapping of the last ``PATCH``."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile = profile
        self.patch_bodies: list[dict[str, Any]] = []

    def get(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.profile)

    def patch(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.patch_bodies.append(body)
        mapping = {
            name: {"id": entry["id"], "name": f"{entry['id']}-template"}
            for name, entry in body["imageMapping"].items()
        }
        self.profile = {**self.profile, "imageMappings": {"mapping": mapping}}
        return httpx.Response(200, json=self.profile)

    @property
    def names(self) -> set[str]:
        return set(self.profile["imageMappings"]["mapping"])


def _pause_after_profile_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the profile read, wait a moment for the other call to read too.

    Without the lock both calls read the same profile and the barrier
    opens at once; with the lock the second read cannot happen before the
    first call wrote, so the wait runs out and the call goes on.
    """
    read = images_module._read_profile
    reads = 0
    both_read = asyncio.Event()

    async def paused(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal reads
        profile = await read(*args, **kwargs)
        reads += 1
        if reads >= 2:
            both_read.set()
        with suppress(TimeoutError):
            await asyncio.wait_for(both_read.wait(), 0.5)
        return profile

    monkeypatch.setattr(images_module, "_read_profile", paused)


async def test_mapping_update_serializes_two_changes_to_one_profile(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: two calls that each add a name must both keep their name.
    _pause_after_profile_read(monkeypatch)
    appliance = _Appliance(_profile(**{"RHEL 9": _UBUNTU}))
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROFILE).mock(side_effect=appliance.get)
        mock.get("/iaas/api/fabric-images/fi-rhel").respond(200, json=_RHEL)
        mock.get("/iaas/api/fabric-images/fi-rocky").respond(
            200, json={"id": "fi-rocky", "externalRegionId": "region-a"}
        )
        mock.patch(_PROFILE).mock(side_effect=appliance.patch)
        results = await asyncio.gather(
            run(
                "vcfa.tenant.image_profile.mapping.update",
                {"profile_id": "ip-1", "image_name": "Rocky 9", "fabric_image_id": "fi-rocky"},
            ),
            run(
                "vcfa.tenant.image_profile.mapping.update",
                {"profile_id": "ip-1", "image_name": "Debian 12", "fabric_image_id": "fi-rhel"},
            ),
        )
    assert [r["result"]["status"] for r in results] == ["updated", "updated"], results
    assert appliance.names == {"RHEL 9", "Rocky 9", "Debian 12"}
    assert len(appliance.patch_bodies) == 2
    assert set(appliance.patch_bodies[1]["imageMapping"]) == appliance.names
    assert [r["result"]["guidance"] for r in results] == [None, None]
    assert images_module._profile_locks == {}


def _record_advisory_lock(
    monkeypatch: pytest.MonkeyPatch, *, locked: bool
) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []

    @asynccontextmanager
    async def fake(key: int, **kwargs: Any) -> AsyncIterator[bool]:
        calls.append((key, kwargs))
        yield locked

    monkeypatch.setattr(images_module, "advisory_lock", fake)
    return calls


async def test_mapping_update_holds_the_cross_worker_lock_of_the_profile(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_advisory_lock(monkeypatch, locked=True)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched=_profile())
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "remove": True},
        )
    assert result["result"]["status"] == "updated"
    target = await resolved_target()
    key, kwargs = calls[0]
    assert key == images_module._lock_key(target, "ip-1")
    assert key != images_module._lock_key(target, "ip-2")
    assert kwargs["wait_seconds"] == images_module.PROFILE_LOCK_WAIT_SECONDS


async def test_mapping_update_fails_without_writing_when_the_lock_stays_busy(
    vcfa: VcfAutomationConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_advisory_lock(monkeypatch, locked=False)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched=_profile())
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "remove": True},
        )
    assert result["status"] == "error"
    assert "still running" in json.dumps(result)
    assert not patch.called
    assert images_module._profile_locks == {}


async def test_mapping_update_unchanged_writes_nothing(vcfa: VcfAutomationConnector) -> None:
    profile = _profile(**{"RHEL 9": _RHEL})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, profile, patched=profile)
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    assert result["result"]["status"] == "unchanged"
    assert not patch.called


@pytest.mark.parametrize(
    "params",
    [
        {"profile_id": "ip-1", "image_name": "RHEL 9"},
        {
            "profile_id": "ip-1",
            "image_name": "RHEL 9",
            "fabric_image_id": "fi-rhel",
            "remove": True,
        },
        {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-missing"},
    ],
    ids=["no-target", "target-and-remove", "unknown-fabric-image"],
)
async def test_mapping_update_invalid_request_writes_nothing(
    vcfa: VcfAutomationConnector, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        result = await run("vcfa.tenant.image_profile.mapping.update", params)
    assert result["result"]["status"] == "invalid_request", result
    assert "nothing was written" in result["result"]["guidance"]
    assert not patch.called


async def test_mapping_update_refuses_a_template_of_another_region(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        mock.get("/iaas/api/fabric-images/fi-other").respond(
            200, json={"id": "fi-other", "externalRegionId": "region-b"}
        )
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-other"},
        )
    assert result["result"]["status"] == "invalid_request"
    assert "region-b" in result["result"]["guidance"]
    assert not patch.called


async def test_mapping_update_refuses_a_template_of_another_cloud_account(
    vcfa: VcfAutomationConnector,
) -> None:
    # Same region id, but the template belongs to another vCenter account.
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        mock.get("/iaas/api/fabric-images/fi-other").respond(
            200,
            json={"id": "fi-other", "externalRegionId": "region-a", "cloudAccountIds": ["ca-2"]},
        )
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-other"},
        )
    assert result["result"]["status"] == "invalid_request"
    assert "ca-2" in result["result"]["guidance"]
    assert "nothing was written" in result["result"]["guidance"]
    assert not patch.called


async def test_mapping_update_fabric_image_500_is_connector_error(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        mock.get("/iaas/api/fabric-images/fi-broken").respond(500, json={"message": "boom"})
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-broken"},
        )
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 500


async def test_mapping_update_parks_for_approval_without_writing(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{"RHEL 9": _UBUNTU}), patched={})
        result = await run(
            "vcfa.tenant.image_profile.mapping.update",
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
            approved=False,
        )
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not patch.called


# ---------------------------------------------------------------------------
# Approval preview
# ---------------------------------------------------------------------------


async def _preview(params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == "vcfa.tenant.image_profile.mapping.update"
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
        )
    )


_CHANGE = {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"}


async def test_preview_shows_old_and_new_template(vcfa: VcfAutomationConnector) -> None:
    profile = _profile(**{"RHEL 9": _UBUNTU, "Windows 2019": _WIN})
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, profile, patched={})
        effect = await _preview(_CHANGE, vcfa)
    assert effect is not None
    preview = effect["preview"]
    assert preview["read"] == "done"
    assert preview["change"]["old_target"]["name"] == "ubuntu-template"
    assert preview["change"]["new_target"]["name"] == "rhel9-template"
    assert preview["other_names"] == ["Windows 2019"]
    assert not patch.called


async def test_preview_without_connector_echoes_the_request(vcfa: VcfAutomationConnector) -> None:
    effect = await _preview(_CHANGE, None)
    assert effect is not None
    assert effect["preview"]["read"] == "skipped"
    assert effect["preview"]["fabric_image_id"] == "fi-rhel"


async def test_preview_read_failure_degrades(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROFILE).respond(503, json={})
        failed = await _preview(_CHANGE, vcfa)
        problem = await _preview({"profile_id": "ip-1", "image_name": "RHEL 9"}, vcfa)
    assert failed is not None and failed["preview"]["read"] == "failed"
    assert problem is not None and "fabric_image_id" in problem["preview"]["problem"]
