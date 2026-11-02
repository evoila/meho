# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The six typed content-library reads, over mocked vCenter HTTP.

* ``vmware.content_library.get`` (by id and by name, local and subscribed);
* ``vmware.content_library.item.get``;
* ``vmware.content_library.item.files.list`` (and a long list behind a handle);
* ``vmware.content_library.item.ovf_options`` (the ``{"target": ...}`` body
  vCenter 8 and 9 want, and a saved vCenter 8 answer);
* ``vmware.content_library.subscribed.probe``;
* ``vmware.supervisor.content_library.list`` (vCenter 9 only).

Every test runs the real :class:`VmwareRestConnector` against saved vendor
answers; no live vCenter. The pinned-spec check of the paths is in
``test_connectors_vmware_rest_content_library_reconcile.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_content_library import (
    CONTENT_LIBRARY_OPS,
    VMWARE_CONTENT_LIBRARY_GET_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_GET_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP,
    VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP,
    VMWARE_SUPERVISOR_CONTENT_LIBRARY_LIST_OP,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_BASE = "https://vc.example.test"
_LIB = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
_SUB_LIB = "7a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"
_ITEM = "1e2d3c4b-5a69-4788-9a0b-1c2d3e4f5a6b"
_DS = "datastore-11"


def _operator() -> Operator:
    return Operator(
        sub="op-content-library",
        name="Content Library Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c1c1"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None
    version: str | None = None


@pytest.fixture
async def connector() -> AsyncIterator[VmwareRestConnector]:
    conn = VmwareRestConnector(session_loader=_stub_loader)
    yield conn
    conn._session_tokens.clear()
    for client in conn._clients.values():
        await client.aclose()
    conn._clients.clear()


@pytest.fixture
def vc() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post("/api/session").respond(200, json="tok")
        mock.get("/api/about").respond(200, json={"version": "9.0.0"})
        yield mock


def _validate(op: Any, out: dict[str, Any]) -> None:
    Draft202012Validator(op.response_schema).validate(out)


# ---------------------------------------------------------------------------
# Saved vendor answers (vCenter 8.0.3 and 9.0 send the same shapes)
# ---------------------------------------------------------------------------

_LOCAL_MODEL: dict[str, Any] = {
    "id": _LIB,
    "name": "templates",
    "type": "LOCAL",
    "description": "VM templates",
    "creation_time": "2026-09-01T10:00:00.000Z",
    "last_modified_time": "2026-10-01T10:00:00.000Z",
    "version": "4",
    "server_guid": "0a1b2c3d-0000-4000-8000-000000000001",
    "storage_backings": [{"type": "DATASTORE", "datastore_id": _DS}],
    "state_info": {"state": "ACTIVE"},
}
_LOCAL_TYPED: dict[str, Any] = {
    **_LOCAL_MODEL,
    "publish_info": {
        "published": True,
        "publish_url": "https://vc.example.test:443/cls/vcsp/lib/x/lib.json",
        "authentication_method": "BASIC",
        "user_name": "vcsp",
        "password": "should-never-show",
        "current_password": "should-never-show",
        "persist_json_enabled": False,
    },
}
_SUBSCRIBED_MODEL: dict[str, Any] = {
    "id": _SUB_LIB,
    "name": "kubernetes-releases",
    "type": "SUBSCRIBED",
    "storage_backings": [{"type": "DATASTORE", "datastore_id": _DS}],
    "last_sync_time": "2026-10-20T08:00:00.000Z",
}
_SUBSCRIBED_TYPED: dict[str, Any] = {
    **_SUBSCRIBED_MODEL,
    "subscription_info": {
        "subscription_url": "https://publisher.example.test/lib.json",
        "authentication_method": "NONE",
        "automatic_sync_enabled": True,
        "on_demand": True,
        "ssl_thumbprint": "AA:BB:CC",
        "password": "should-never-show",
        "source_info": {"source_library": "9b8a7c6d-0000-4000-8000-000000000002"},
    },
}
_ITEM_MODEL: dict[str, Any] = {
    "id": _ITEM,
    "library_id": _LIB,
    "name": "photon-5",
    "type": "ovf",
    "size": 1073741824,
    "cached": True,
    "content_version": "2",
    "metadata_version": "3",
    "version": "5",
    "creation_time": "2026-09-02T10:00:00.000Z",
    "last_modified_time": "2026-09-03T10:00:00.000Z",
    "security_compliance": True,
}


def _file(name: str, size: int) -> dict[str, Any]:
    return {
        "name": name,
        "size": size,
        "cached": True,
        "version": "1",
        "checksum_info": {"algorithm": "SHA256", "checksum": f"sum-{name}"},
    }


def _storage(name: str, size: int) -> dict[str, Any]:
    return {
        **_file(name, size),
        "storage_backing": {"type": "DATASTORE", "datastore_id": _DS},
        "storage_uris": [f"ds:///vmfs/volumes/ds-uuid/contentlib-x/{_ITEM}/{name}"],
    }


# ---------------------------------------------------------------------------
# vmware.content_library.get
# ---------------------------------------------------------------------------


async def test_get_local_library_by_id(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get(f"/api/content/library/{_LIB}").respond(200, json=_LOCAL_MODEL)
    typed = vc.get(f"/api/content/local-library/{_LIB}").respond(200, json=_LOCAL_TYPED)
    items = vc.get("/api/content/library/item", params={"library_id": _LIB}).respond(
        200, json=[_ITEM, "2e3d4c5b-0000-4000-8000-000000000003"]
    )

    out = await connector.content_library_get(_operator(), _StubTarget(), {"library_id": _LIB})

    assert typed.call_count == 1
    assert items.call_count == 1
    assert out["library_id"] == _LIB
    assert out["type"] == "LOCAL"
    assert out["state"] == "ACTIVE"
    assert out["item_count"] == 2
    assert out["storage_backings"] == [
        {"type": "DATASTORE", "datastore_id": _DS, "storage_uri": None}
    ]
    assert out["publish"] == {
        "published": True,
        "publish_url": "https://vc.example.test:443/cls/vcsp/lib/x/lib.json",
        "authentication_method": "BASIC",
        "user_name": "vcsp",
    }
    assert out["subscription"] is None
    assert "should-never-show" not in json.dumps(out)
    _validate(VMWARE_CONTENT_LIBRARY_GET_OP, out)


async def test_get_subscribed_library_by_name(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    find = vc.post("/api/content/library", params={"action": "find"}).respond(200, json=[_SUB_LIB])
    vc.get(f"/api/content/library/{_SUB_LIB}").respond(200, json=_SUBSCRIBED_MODEL)
    vc.get(f"/api/content/subscribed-library/{_SUB_LIB}").respond(200, json=_SUBSCRIBED_TYPED)
    vc.get("/api/content/library/item", params={"library_id": _SUB_LIB}).respond(200, json=[])

    out = await connector.content_library_get(
        _operator(), _StubTarget(), {"library_name": "kubernetes-releases"}
    )

    assert json.loads(find.calls.last.request.content) == {"name": "kubernetes-releases"}
    assert out["library_id"] == _SUB_LIB
    assert out["item_count"] == 0
    assert out["last_sync_time"] == "2026-10-20T08:00:00.000Z"
    assert out["subscription"]["subscription_url"] == "https://publisher.example.test/lib.json"
    assert out["subscription"]["source_library"] == "9b8a7c6d-0000-4000-8000-000000000002"
    assert out["publish"] is None
    assert "should-never-show" not in json.dumps(out)
    _validate(VMWARE_CONTENT_LIBRARY_GET_OP, out)


async def test_get_by_unknown_name_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.post("/api/content/library", params={"action": "find"}).respond(200, json=[])
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.content_library_get(_operator(), _StubTarget(), {"library_name": "nope"})


async def test_get_by_ambiguous_name_is_refused(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.post("/api/content/library", params={"action": "find"}).respond(200, json=[_LIB, _SUB_LIB])
    with pytest.raises(ValueError, match="matched 2 libraries"):
        await connector.content_library_get(_operator(), _StubTarget(), {"library_name": "dup"})


async def test_get_of_a_missing_library_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get(f"/api/content/library/{_LIB}").respond(404, json={"error_type": "NOT_FOUND"})
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.content_library_get(_operator(), _StubTarget(), {"library_id": _LIB})


@pytest.mark.parametrize("bad", ["../../vcenter/vm/vm-42", "lib-1", ""])
async def test_an_id_that_is_not_a_uuid_is_refused_before_any_call(
    connector: VmwareRestConnector, bad: str
) -> None:
    with respx.mock(base_url=_BASE) as mock:
        with pytest.raises(ValueError, match="UUID"):
            await connector.content_library_item_get(
                _operator(), _StubTarget(), {"library_item_id": bad}
            )
        with pytest.raises(ValueError, match="UUID"):
            await connector.content_library_get(_operator(), _StubTarget(), {"library_id": bad})
        assert mock.calls.call_count == 0


# ---------------------------------------------------------------------------
# vmware.content_library.item.get / item.files.list
# ---------------------------------------------------------------------------


async def test_item_get(connector: VmwareRestConnector, vc: respx.MockRouter) -> None:
    vc.get(f"/api/content/library/item/{_ITEM}").respond(200, json=_ITEM_MODEL)
    out = await connector.content_library_item_get(
        _operator(), _StubTarget(), {"library_item_id": _ITEM}
    )
    assert out["library_item_id"] == _ITEM
    assert out["library_id"] == _LIB
    assert out["type"] == "ovf"
    assert out["size"] == 1073741824
    assert out["cached"] is True
    assert out["source_id"] is None
    _validate(VMWARE_CONTENT_LIBRARY_ITEM_GET_OP, out)


async def test_missing_item_is_not_found(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get(f"/api/content/library/item/{_ITEM}").respond(404, json={"error_type": "NOT_FOUND"})
    with pytest.raises(ConnectorResourceNotFoundError):
        await connector.content_library_item_get(
            _operator(), _StubTarget(), {"library_item_id": _ITEM}
        )


async def test_files_list_joins_files_and_storage(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    files = [_file("photon.ovf", 9000), _file("photon-disk1.vmdk", 500_000_000)]
    storage = [_storage("photon.ovf", 9000), _storage("photon-disk1.vmdk", 500_000_000)]
    vc.get(f"/api/content/library/item/{_ITEM}/file").respond(200, json=files)
    vc.get(f"/api/content/library/item/{_ITEM}/storage").respond(200, json=storage)

    out = await connector.content_library_item_files_list(
        _operator(), _StubTarget(), {"library_item_id": _ITEM}
    )

    assert out["file_count"] == 2
    first = out["files"][0]
    assert first == {
        "name": "photon.ovf",
        "size": 9000,
        "cached": True,
        "version": "1",
        "checksum_algorithm": "SHA256",
        "checksum": "sum-photon.ovf",
        "datastore_ids": [_DS],
        "storage_uris": [f"ds:///vmfs/volumes/ds-uuid/contentlib-x/{_ITEM}/photon.ovf"],
    }
    _validate(VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP, out)


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The reducer reads its size limits from settings, which need these env vars."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_settings_env")
async def test_a_long_file_list_goes_behind_a_result_handle(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    names = [f"disk-{i}.vmdk" for i in range(60)]
    vc.get(f"/api/content/library/item/{_ITEM}/file").respond(
        200, json=[_file(n, 1000) for n in names]
    )
    vc.get(f"/api/content/library/item/{_ITEM}/storage").respond(
        200, json=[_storage(n, 1000) for n in names]
    )
    out = await connector.content_library_item_files_list(
        _operator(), _StubTarget(), {"library_item_id": _ITEM}
    )
    context = {"op_id": VMWARE_CONTENT_LIBRARY_ITEM_FILES_LIST_OP.op_id}
    reduced, handle = await JsonFluxReducer(sample_size=5).reduce(out, None, context)
    assert handle is not None
    assert reduced["row_count"] == 60
    assert reduced["file_count"] == 60
    assert reduced["library_item_id"] == _ITEM


# ---------------------------------------------------------------------------
# vmware.content_library.item.ovf_options
# ---------------------------------------------------------------------------

#: A saved vCenter 8.0.3 ``?action=filter`` answer (names and values neutral).
_OVF_SUMMARY_8_0: dict[str, Any] = {
    "name": "appliance",
    "annotation": "An example appliance",
    "eulas": ["End user licence text"],
    "networks": ["Network 1"],
    "storage_groups": [],
    "additional_params": [
        {
            "@class": "com.vmware.vcenter.ovf.deployment_option_params",
            "type": "DeploymentOptionParams",
            "deployment_options": [
                {"key": "small", "label": "Small", "description": "2 CPU", "default_choice": True},
                {"key": "large", "label": "Large", "description": "8 CPU", "default_choice": False},
            ],
        },
        {
            "@class": "com.vmware.vcenter.ovf.property_params",
            "type": "PropertyParams",
            "properties": [
                {
                    "id": "guestinfo.hostname",
                    "label": "Host name",
                    "type": "string",
                    "value": "appliance",
                    "class_id": "",
                    "instance_id": "",
                    "category": "Network",
                    "ui_optional": False,
                },
                {
                    "id": "guestinfo.root_password",
                    "label": "Root password",
                    "type": "password(8..)",
                    "value": "default-secret",
                    "class_id": "",
                    "instance_id": "",
                },
            ],
        },
        {
            "@class": "com.vmware.vcenter.ovf.size_params",
            "type": "SizeParams",
            "approximate_download_size": 900000000,
            "approximate_flat_deployment_size": 21474836480,
            "approximate_sparse_deployment_size": 1200000000,
            "variable_disk_size": False,
        },
        {
            "@class": "com.vmware.vcenter.ovf.ip_allocation_params",
            "type": "IpAllocationParams",
            "supported_ip_allocation_policy": ["DHCP", "STATIC_MANUAL"],
            "ip_allocation_policy": "DHCP",
            "supported_ip_protocol": ["IPV4"],
            "ip_protocol": "IPV4",
        },
    ],
}


@pytest.mark.parametrize("version", ["8.0.3", "9.0.0"])
async def test_ovf_options_sends_the_target_wrapper(
    connector: VmwareRestConnector, version: str
) -> None:
    with respx.mock(base_url=_BASE) as mock:
        mock.post("/api/session").respond(200, json="tok")
        route = mock.post(
            f"/api/vcenter/ovf/library-item/{_ITEM}", params={"action": "filter"}
        ).respond(200, json=_OVF_SUMMARY_8_0)
        out = await connector.content_library_item_ovf_options(
            _operator(),
            _StubTarget(fingerprint={"version": version}),
            {"library_item_id": _ITEM, "resource_pool": "resgroup-10", "folder": "group-v3"},
        )

    assert json.loads(route.calls.last.request.content) == {
        "target": {"resource_pool_id": "resgroup-10", "folder_id": "group-v3"}
    }
    assert out["eulas"] == ["End user licence text"]
    assert out["networks"] == ["Network 1"]
    assert [o["key"] for o in out["deployment_options"]] == ["small", "large"]
    assert out["default_deployment_option"] == "small"
    by_id = {p["id"]: p for p in out["properties"]}
    assert by_id["guestinfo.hostname"]["value"] == "appliance"
    assert by_id["guestinfo.root_password"]["value"] is None
    assert "default-secret" not in json.dumps(out)
    assert out["disk_size"]["approximate_flat_deployment_size"] == 21474836480
    assert out["ip_allocation"]["ip_allocation_policy"] == "DHCP"
    _validate(VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP, out)


async def test_ovf_options_of_an_item_with_no_extra_params(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.post(f"/api/vcenter/ovf/library-item/{_ITEM}", params={"action": "filter"}).respond(
        200, json={"eulas": [], "networks": []}
    )
    out = await connector.content_library_item_ovf_options(
        _operator(), _StubTarget(), {"library_item_id": _ITEM, "resource_pool": "resgroup-10"}
    )
    assert out["deployment_options"] == []
    assert out["default_deployment_option"] is None
    assert out["disk_size"] is None
    _validate(VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP, out)


# ---------------------------------------------------------------------------
# vmware.content_library.subscribed.probe
# ---------------------------------------------------------------------------


async def test_probe_returns_status_and_thumbprint(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    url = "https://publisher.example.test/lib.json"
    route = vc.post("/api/content/subscribed-library", params={"action": "probe"}).respond(
        200, json={"status": "SUCCESS", "ssl_thumbprint": "AA:BB:CC", "error_messages": []}
    )
    out = await connector.content_library_subscribed_probe(
        _operator(), _StubTarget(), {"subscription_url": url}
    )
    assert json.loads(route.calls.last.request.content) == {
        "subscription_info": {"subscription_url": url, "authentication_method": "NONE"}
    }
    assert out == {
        "subscription_url": url,
        "status": "SUCCESS",
        "reachable": True,
        "needs_login": False,
        "ssl_thumbprint": "AA:BB:CC",
        "error_messages": [],
    }
    _validate(VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP, out)


async def test_probe_of_a_source_that_wants_a_login_is_reachable(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    """The probe sends no login. ``INVALID_CREDENTIALS`` still means vCenter reached the source."""
    url = "https://publisher.example.test/private/lib.json"
    vc.post("/api/content/subscribed-library", params={"action": "probe"}).respond(
        200,
        json={
            "status": "INVALID_CREDENTIALS",
            "ssl_thumbprint": "AA:BB:CC",
            "error_messages": [{"id": "x.auth", "default_message": "Login required."}],
        },
    )
    out = await connector.content_library_subscribed_probe(
        _operator(), _StubTarget(), {"subscription_url": url}
    )
    assert out == {
        "subscription_url": url,
        "status": "INVALID_CREDENTIALS",
        "reachable": True,
        "needs_login": True,
        "ssl_thumbprint": "AA:BB:CC",
        "error_messages": ["Login required."],
    }
    _validate(VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP, out)


@pytest.mark.parametrize(
    "status", ["HOST_NOT_FOUND", "TIMED_OUT", "CERTIFICATE_ERROR", "RESOURCE_NOT_FOUND"]
)
async def test_probe_of_an_unreached_source(
    connector: VmwareRestConnector, vc: respx.MockRouter, status: str
) -> None:
    vc.post("/api/content/subscribed-library", params={"action": "probe"}).respond(
        200,
        json={
            "status": status,
            "error_messages": [{"id": "x.host", "default_message": "Host not found."}],
        },
    )
    out = await connector.content_library_subscribed_probe(
        _operator(), _StubTarget(), {"subscription_url": "https://nope.example.test/lib.json"}
    )
    assert out["status"] == status
    assert out["reachable"] is False
    assert out["needs_login"] is False
    assert out["ssl_thumbprint"] is None
    assert out["error_messages"] == ["Host not found."]
    _validate(VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP, out)


# ---------------------------------------------------------------------------
# vmware.supervisor.content_library.list
# ---------------------------------------------------------------------------


async def test_supervisor_libraries_on_vcenter_9(
    connector: VmwareRestConnector, vc: respx.MockRouter
) -> None:
    vc.get("/api/vcenter/namespace-management/lifecycle/content/libraries").respond(
        200, json={"libraries": [{"library": {"id": _SUB_LIB}, "status": "READY"}]}
    )
    out = await connector.supervisor_content_library_list(
        _operator(), _StubTarget(fingerprint={"version": "9.0.0"}), {}
    )
    assert out == {"library_count": 1, "libraries": [{"library_id": _SUB_LIB, "status": "READY"}]}
    _validate(VMWARE_SUPERVISOR_CONTENT_LIBRARY_LIST_OP, out)


async def test_supervisor_libraries_stop_early_on_vcenter_8(
    connector: VmwareRestConnector,
) -> None:
    with respx.mock(base_url=_BASE) as mock:
        with pytest.raises(NotImplementedError, match=r"needs vCenter 9\.0 or newer"):
            await connector.supervisor_content_library_list(
                _operator(), _StubTarget(fingerprint={"version": "8.0.3"}), {}
            )
        assert mock.calls.call_count == 0


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


_EXPECTED_IDS = {
    "vmware.content_library.get",
    "vmware.content_library.item.get",
    "vmware.content_library.item.files.list",
    "vmware.content_library.item.ovf_options",
    "vmware.content_library.subscribed.probe",
    "vmware.supervisor.content_library.list",
}


def test_the_six_reads_are_registered_typed_ops() -> None:
    assert {op.op_id for op in CONTENT_LIBRARY_OPS} == _EXPECTED_IDS
    for op in CONTENT_LIBRARY_OPS:
        assert op in VMWARE_TYPED_OPS
        assert callable(getattr(VmwareRestConnector, op.handler_attr))
        assert op.group_key == "content_library"
        assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()


@pytest.mark.parametrize("op", CONTENT_LIBRARY_OPS, ids=lambda op: op.op_id)
def test_each_read_is_safe_and_runs_without_approval(op: Any) -> None:
    assert op.safety_level == "safe"
    assert op.requires_approval is False


@pytest.mark.parametrize("op", CONTENT_LIBRARY_OPS, ids=lambda op: op.op_id)
def test_examples_validate(op: Any) -> None:
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []


def test_the_post_reads_say_they_change_nothing() -> None:
    for op in (
        VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP,
        VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP,
    ):
        assert "changes nothing" in op.description


def test_schemas_refuse_bad_input() -> None:
    get_schema = VMWARE_CONTENT_LIBRARY_GET_OP.parameter_schema
    assert validate_params(get_schema, {}) != []
    assert validate_params(get_schema, {"library_id": "../x"}) != []
    assert validate_params(get_schema, {"library_id": _LIB}) == []
    ovf_schema = VMWARE_CONTENT_LIBRARY_ITEM_OVF_OPTIONS_OP.parameter_schema
    assert validate_params(ovf_schema, {"library_item_id": _ITEM}) != []
    probe_schema = VMWARE_CONTENT_LIBRARY_SUBSCRIBED_PROBE_OP.parameter_schema
    assert validate_params(probe_schema, {"subscription_url": "file:///etc/x"}) != []
