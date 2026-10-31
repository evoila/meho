# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Aria Automation 8 image ops (image profiles).

The four ops run the handlers they share with VCF Automation 9
(``tests/test_connectors_vcf_automation_images.py`` covers the handler
details, such as the lock and the region checks). Here each op runs
through the real dispatcher against a respx-mocked vRA 8: the two-step
login, then the ``/iaas/api`` calls. All names and ids are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_automation import _images as images_module
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMAGE_OPS,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_image_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.settings import get_settings
from tests.test_vcfa_operation_examples import _REAL_LOOKING, _properties

_HOST = "vra-images.test.invalid"
_BASE_URL = f"https://{_HOST}"
_PASSWORD = "vra-pw-sentinel"
_REFRESH = "vra-refresh-sentinel"
_TOKEN = "vra-bearer-sentinel"

_PROFILES = "/iaas/api/image-profiles"
_PROFILE = "/iaas/api/image-profiles/ip-1"
_FABRIC = "/iaas/api/fabric-images"
_ENUM = "/iaas/api/cloud-accounts/ca-1/private-image-enumeration"
_TRACKER = "/iaas/api/request-tracker/rt-1"
_MAPPING_OP = "vcfa.vra8.image_profile.mapping.update"
_ENUM_OP = "vcfa.vra8.cloud_account.enumerate_images"

_EXPECTED_TIERS: dict[str, tuple[str, bool]] = {
    "vcfa.vra8.image_profile.list": ("safe", False),
    "vcfa.vra8.fabric_image.list": ("safe", False),
    _ENUM_OP: ("caution", True),
    _MAPPING_OP: ("dangerous", True),
}

_UBUNTU = {"id": "fi-ubuntu", "name": "ubuntu-template", "externalId": "vm-1"}
_WIN = {"id": "fi-win", "name": "win-template", "externalId": "vm-2"}
_CENTOS = {"id": "fi-centos", "name": "centos-template", "externalId": "vm-3"}
_RHEL = {
    "id": "fi-rhel",
    "name": "rhel9-template",
    "externalRegionId": "region-a",
    "cloudAccountIds": ["ca-1"],
}


def _profile(**mapping: dict[str, Any]) -> dict[str, Any]:
    """A profile of the second region of its cloud account (``rg-b``)."""
    return {
        "id": "ip-1",
        "name": "example-profile",
        "externalRegionId": "region-a",
        "cloudAccountId": "ca-1",
        "imageMappings": {"mapping": mapping},
        "_links": {"region": {"href": "/iaas/api/regions/rg-b"}},
    }


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    clear_registry()
    register_connector_v2(
        product=VRA8_PRODUCT, version=VRA8_VERSION, impl_id=VRA8_IMPL_ID, cls=Vra8Connector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


class _Target:
    """A vRA 8 target: version 8.18 resolves to the ``vcfa-vra8`` impl."""

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000045f0")
        self.name = "vra-images"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/vra-images"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_OPERATOR = Operator(
    sub="vra-images-test",
    name="VRA Images Test",
    email=None,
    raw_jwt="vra.images.jwt",
    tenant_id=UUID("00000000-0000-0000-0000-0000000045f0"),
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "vra-svc", "password": _PASSWORD}


@pytest.fixture
async def vra8() -> AsyncIterator[Vra8Connector]:
    await register_vra8_image_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    assert isinstance(instance, Vra8Connector)
    instance._credentials_loader = _stub_loader
    yield instance
    await instance.aclose()


async def _run(op_id: str, params: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _mount_login(mock: respx.MockRouter) -> None:
    mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": _REFRESH})
    mock.post("/iaas/api/login").respond(200, json={"token": _TOKEN})


def _assert_no_secret(result: dict[str, Any]) -> None:
    text = json.dumps(result)
    for secret in (_PASSWORD, _REFRESH, _TOKEN):
        assert secret not in text


# ---------------------------------------------------------------------------
# Metadata and registration
# ---------------------------------------------------------------------------


def test_image_op_risk_levels() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VRA8_IMAGE_OPS} == (
        _EXPECTED_TIERS
    )


@pytest.mark.parametrize("op", VRA8_IMAGE_OPS, ids=lambda op: op.op_id)
def test_image_op_contract(op: Any) -> None:
    """Examples pass the schema, look made up, and every response field is described."""
    validator = Draft202012Validator(op.parameter_schema)
    examples = op.parameter_schema["examples"]
    assert examples
    for example in examples:
        assert not list(validator.iter_errors(example)), example
        assert not _REAL_LOOKING.search(repr(example))
    undescribed = [
        name for name, sub in _properties(op.response_schema) if not sub.get("description")
    ]
    assert not undescribed
    keys = {"when_to_call", "output_shape", "next_step"}
    if op.safety_level != "safe":
        keys.add("preconditions")
    assert keys <= set(op.llm_instructions)
    assert "vcfa.tenant" not in json.dumps(op.llm_instructions)
    assert "vcfa.tenant" not in json.dumps(op.parameter_schema)


@pytest.mark.parametrize("op_id", [_ENUM_OP, _MAPPING_OP])
@pytest.mark.parametrize("bad", [".", ".."])
def test_path_ids_refuse_dot_segments(op_id: str, bad: str) -> None:
    op = next(op for op in VRA8_IMAGE_OPS if op.op_id == op_id)
    key = "cloud_account_id" if op_id == _ENUM_OP else "profile_id"
    params = {**op.parameter_schema["examples"][0], key: bad}
    assert list(Draft202012Validator(op.parameter_schema).iter_errors(params))


async def test_image_ops_register_as_typed_rows(vra8: Vra8Connector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.impl_id == VRA8_IMPL_ID,
                        EndpointDescriptor.op_id.in_(list(_EXPECTED_TIERS)),
                    )
                )
            )
            .scalars()
            .all()
        )
        group = (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.impl_id == VRA8_IMPL_ID,
                    OperationGroup.group_key == "vcfa-vra8-images",
                )
            )
        ).scalar_one()
    assert {row.op_id: (row.safety_level, row.requires_approval) for row in rows} == (
        _EXPECTED_TIERS
    )
    assert all(row.source_kind == "typed" for row in rows)
    # The handlers are methods of the mixin that Vra8Connector inherits.
    assert all(".Vra8ImageOpsMixin." in (row.handler_ref or "") for row in rows)
    assert "vcfa.vra8.image_profile.mapping.update" in group.when_to_use


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_image_profile_list_reads_the_names(vra8: Vra8Connector) -> None:
    listing = {"content": [_profile(**{"RHEL 9": _UBUNTU})], "totalElements": 1}
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.get(_PROFILES).respond(200, json=listing)
        result = await _run("vcfa.vra8.image_profile.list", {})
    assert result["status"] == "ok", result
    assert result["result"] == listing
    request = route.calls.last.request
    assert request.url.params["apiVersion"] == "2021-07-15"
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    _assert_no_secret(result)


async def test_fabric_image_list_forwards_odata(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        route = mock.get(_FABRIC).respond(200, json={"content": [_RHEL], "totalElements": 1})
        result = await _run(
            "vcfa.vra8.fabric_image.list", {"$filter": "name eq 'rhel9-template'", "$top": 5}
        )
    assert result["result"]["content"] == [_RHEL]
    params = route.calls.last.request.url.params
    assert params["$filter"] == "name eq 'rhel9-template'"
    assert params["$top"] == "5"


async def test_read_vendor_error_reaches_the_answer(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.get(_PROFILES).respond(403, json={"message": "not allowed to read profiles"})
        result = await _run("vcfa.vra8.image_profile.list", {})
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 403
    assert "not allowed to read profiles" in json.dumps(result)


# ---------------------------------------------------------------------------
# Image enumeration
# ---------------------------------------------------------------------------


async def test_enumerate_images_waits_for_approval(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post(_ENUM).respond(202, json={"id": "rt-1", "status": "INPROGRESS"})
        result = await _run(_ENUM_OP, {"cloud_account_id": "ca-1"}, approved=False)
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not post.called


async def test_enumerate_images_follows_the_tracker_to_its_end(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        post = mock.post(_ENUM).respond(
            202, json={"id": "rt-1", "status": "INPROGRESS", "progress": 0}
        )
        tracker = mock.get(_TRACKER)
        tracker.side_effect = [
            respx.MockResponse(200, json={"id": "rt-1", "status": "INPROGRESS", "progress": 40}),
            respx.MockResponse(
                200, json={"id": "rt-1", "status": "FINISHED", "progress": 100, "message": "done"}
            ),
        ]
        result = await _run(_ENUM_OP, {"cloud_account_id": "ca-1"})
    out = result["result"]
    assert out["status"] == "finished", out
    assert out["request"] == {
        "id": "rt-1",
        "status": "FINISHED",
        "progress": 100,
        "message": "done",
    }
    assert "vcfa.vra8.fabric_image.list" in out["guidance"]
    assert post.calls.last.request.url.params["apiVersion"] == "2021-07-15"
    assert tracker.call_count == 2
    _assert_no_secret(result)


async def test_enumerate_images_vendor_error_reaches_the_answer(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_login(mock)
        mock.post(_ENUM).respond(404, json={"message": "cloud account ca-1 not found"})
        result = await _run(_ENUM_OP, {"cloud_account_id": "ca-1"})
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 404
    assert "cloud account ca-1 not found" in json.dumps(result)


async def test_enumerate_images_refuses_a_dot_id_before_any_call(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        catch_all = mock.route().respond(200, json={})
        result = await _run(_ENUM_OP, {"cloud_account_id": ".."})
    assert result["status"] != "ok"
    assert not catch_all.called


# ---------------------------------------------------------------------------
# Image name change
# ---------------------------------------------------------------------------


def _mount_change(
    mock: respx.MockRouter, profile: dict[str, Any], *, patched: dict[str, Any]
) -> respx.Route:
    _mount_login(mock)
    mock.get(_PROFILE).respond(200, json=profile)
    mock.get("/iaas/api/fabric-images/fi-rhel").respond(200, json=_RHEL)
    return mock.patch(_PROFILE).respond(200, json=patched)


_THREE = {"RHEL 9": _UBUNTU, "Windows 2019": _WIN, "CentOS 7": _CENTOS}


async def test_mapping_update_waits_for_approval(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**_THREE), patched={})
        result = await _run(
            _MAPPING_OP,
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
            approved=False,
        )
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not patch.called


async def test_mapping_update_keeps_the_other_names_of_three(vra8: Vra8Connector) -> None:
    patched = _profile(**{**_THREE, "RHEL 9": _RHEL})
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**_THREE), patched=patched)
        result = await _run(
            _MAPPING_OP,
            {
                "profile_id": "ip-1",
                "image_name": "RHEL 9",
                "fabric_image_id": "fi-rhel",
                "expected_old_fabric_image_id": "fi-ubuntu",
            },
        )
    out = result["result"]
    assert out["status"] == "updated", out
    assert out["change"]["action"] == "repoint"
    assert out["change"]["old_target"]["name"] == "ubuntu-template"
    assert out["change"]["new_target"]["name"] == "rhel9-template"
    assert out["other_names"] == ["CentOS 7", "Windows 2019"]
    assert out["guidance"] is None
    body = json.loads(patch.calls.last.request.content)
    assert body["imageMapping"] == {
        "RHEL 9": {"id": "fi-rhel"},
        "Windows 2019": {"id": "fi-win"},
        "CentOS 7": {"id": "fi-centos"},
    }
    # The profile's own region, from its region link.
    assert body["regionId"] == "rg-b"
    assert body["name"] == "example-profile"
    assert patch.calls.last.request.url.params["apiVersion"] == "2021-07-15"
    _assert_no_secret(result)


async def test_mapping_update_refuses_a_changed_name(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**_THREE), patched={})
        changed = await _run(
            _MAPPING_OP,
            {
                "profile_id": "ip-1",
                "image_name": "RHEL 9",
                "fabric_image_id": "fi-rhel",
                "expected_old_fabric_image_id": "fi-other",
            },
        )
        added = await _run(
            _MAPPING_OP,
            {
                "profile_id": "ip-1",
                "image_name": "RHEL 9",
                "fabric_image_id": "fi-rhel",
                "expected_old_fabric_image_id": None,
            },
        )
    for result in (changed, added):
        assert result["result"]["status"] == "precondition_failed", result
        assert "nothing was written" in result["result"]["guidance"]
        assert "vcfa.vra8.image_profile.list" in result["result"]["guidance"]
    assert not patch.called


async def test_mapping_update_add_with_expected_absent_name(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(
            mock, _profile(**_THREE), patched=_profile(**_THREE, **{"RHEL 10": _RHEL})
        )
        result = await _run(
            _MAPPING_OP,
            {
                "profile_id": "ip-1",
                "image_name": "RHEL 10",
                "fabric_image_id": "fi-rhel",
                "expected_old_fabric_image_id": None,
            },
        )
    assert result["result"]["status"] == "updated"
    assert result["result"]["change"]["action"] == "add"
    assert set(json.loads(patch.calls.last.request.content)["imageMapping"]) == {
        *_THREE,
        "RHEL 10",
    }


async def test_mapping_update_unchanged_writes_nothing(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**{**_THREE, "RHEL 9": _RHEL}), patched={})
        result = await _run(
            _MAPPING_OP,
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    assert result["result"]["status"] == "unchanged"
    assert not patch.called


async def test_mapping_update_vendor_error_reaches_the_answer(vra8: Vra8Connector) -> None:
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**_THREE), patched={})
        mock.patch(_PROFILE).respond(400, json={"message": "imageMapping is not valid"})
        result = await _run(
            _MAPPING_OP,
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    assert result["status"] == "error"
    assert result["extras"]["http_status"] == 400
    assert "imageMapping is not valid" in json.dumps(result)


async def test_mapping_update_result_meets_its_response_schema(vra8: Vra8Connector) -> None:
    op = next(op for op in VRA8_IMAGE_OPS if op.op_id == _MAPPING_OP)
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**_THREE), patched=_profile(**{**_THREE, "RHEL 9": _RHEL}))
        result = await _run(
            _MAPPING_OP,
            {"profile_id": "ip-1", "image_name": "RHEL 9", "fabric_image_id": "fi-rhel"},
        )
    assert not list(Draft202012Validator(op.response_schema).iter_errors(result["result"]))


# ---------------------------------------------------------------------------
# Approval preview
# ---------------------------------------------------------------------------


async def _preview(params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.impl_id == VRA8_IMPL_ID,
                    EndpointDescriptor.op_id == _MAPPING_OP,
                )
            )
        ).scalar_one()
    return await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=_OPERATOR,
            target=_Target(),
            params=params,
        )
    )


async def test_preview_shows_the_name_and_its_old_and_new_template(vra8: Vra8Connector) -> None:
    change = {
        "profile_id": "ip-1",
        "image_name": "RHEL 9",
        "fabric_image_id": "fi-rhel",
        "expected_old_fabric_image_id": "fi-ubuntu",
    }
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        patch = _mount_change(mock, _profile(**_THREE), patched={})
        effect = await _preview(change, vra8)
    assert effect is not None
    preview = effect["preview"]
    assert preview["read"] == "done"
    assert preview["change"]["image_name"] == "RHEL 9"
    assert preview["change"]["old_target"]["name"] == "ubuntu-template"
    assert preview["change"]["new_target"]["name"] == "rhel9-template"
    assert preview["expected_old_fabric_image_id"] == "fi-ubuntu"
    assert "problem" not in preview
    assert not patch.called


async def test_preview_names_a_changed_name(vra8: Vra8Connector) -> None:
    change = {
        "profile_id": "ip-1",
        "image_name": "RHEL 9",
        "fabric_image_id": "fi-rhel",
        "expected_old_fabric_image_id": "fi-other",
    }
    with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mount_change(mock, _profile(**_THREE), patched={})
        effect = await _preview(change, vra8)
    assert effect is not None
    assert "fi-other" in effect["preview"]["problem"]
