# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the two inventory reads: ``vmware.vm.list`` and ``vmware.inventory.find``.

* both against the container-view fake (``tests._vmware_view_fake``): the
  VIM calls, the rows, the scope and the filters, the names of hosts, clusters
  and folders (and a failed name read), paged answers, the view destroyed, an
  unknown scope;
* ``inventory.find`` by name, by pattern, by path, with a type, and the
  inventory paths;
* the metadata: safe reads in one group, schemas and examples;
* through the dispatcher on ``vmware-rest-9.0`` and ``8.0``: registered, and
  5,000 VMs come back in one call behind a result handle.

All names and ids are made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_inventory_find import (
    INVENTORY_GROUP_KEY,
    VMWARE_INVENTORY_FIND_OP,
    inventory_find_impl,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_list import (
    VM_LIST_PROPERTIES,
    VMWARE_VM_LIST_OP,
    vm_list_impl,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._vmware_view_fake import ROOT, ViewFake, small_inventory, vm_props
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _FakeVmwareTarget,
    _seed_connector,
)
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _make_operator as _make_e2e_operator,
)


def _operator() -> Operator:
    return Operator(
        sub="op-vm-inventory",
        name="VM Inventory Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a2"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _fake() -> ViewFake:
    fake = ViewFake()
    small_inventory(fake)
    return fake


async def _vms(fake: ViewFake, **params: Any) -> dict[str, Any]:
    return await vm_list_impl(fake, _operator(), _Target(), params)


async def _find(fake: ViewFake, **params: Any) -> dict[str, Any]:
    return await inventory_find_impl(fake, _operator(), _Target(), params)


# ---------------------------------------------------------------------------
# vmware.vm.list
# ---------------------------------------------------------------------------


async def test_vm_list_reads_every_vm_through_one_view() -> None:
    fake = _fake()
    out = await _vms(fake)

    assert fake.methods()[:3] == [
        "RetrieveServiceContent",
        "CreateContainerView",
        "RetrievePropertiesEx",
    ]
    [view] = fake.views.values()
    assert view["container"]["value"] == ROOT
    assert view["type"] == ["VirtualMachine"]
    assert view["recursive"] is True
    view_read = fake.calls[2][1]["specSet"][0]
    assert view_read["propSet"][0]["pathSet"] == list(VM_LIST_PROPERTIES)
    assert view_read["objectSet"][0]["obj"]["type"] == "ContainerView"
    assert fake.destroyed == list(fake.views)

    assert out["vm_count"] == 3
    assert out["read_note"] is None
    assert [r["name"] for r in out["vms"]] == ["db-01", "web-01", "web-02"]
    web1 = out["vms"][1]
    assert web1 == {
        "vm": "vm-101",
        "name": "web-01",
        "power_state": "poweredOn",
        "host": "host-11",
        "host_name": "esx-1",
        "cluster": "domain-c7",
        "cluster_name": "cl-1",
        "folder": "group-v10",
        "folder_name": "apps/prod",
        "guest_os": "Ubuntu Linux (64-bit)",
        "ip_address": "<guest-ip>",
        "cpu_count": 2,
        "memory_mb": 4096,
        "used_storage_bytes": 10_000,
        "provisioned_storage_bytes": 15_000,
        "tools_status": "toolsOk",
        "snapshot_count": 0,
        "template": False,
    }
    Draft202012Validator(VMWARE_VM_LIST_OP.response_schema).validate(out)


async def test_vm_list_counts_snapshots() -> None:
    fake = _fake()
    snapshot = {
        "rootSnapshotList": [
            {
                "snapshot": {"type": "VirtualMachineSnapshot", "value": "snapshot-1"},
                "name": "a",
                "childSnapshotList": [
                    {"snapshot": {"type": "VirtualMachineSnapshot", "value": "snapshot-2"}}
                ],
            }
        ]
    }
    fake.objects[("VirtualMachine", "vm-103")]["snapshot"] = snapshot
    out = await _vms(fake, name_pattern="db-*")
    assert [(r["vm"], r["snapshot_count"]) for r in out["vms"]] == [("vm-103", 2)]


async def test_vm_list_unread_snapshot_property_is_null_not_zero() -> None:
    fake = _fake()
    fake.unreadable[("VirtualMachine", "vm-103")] = {"snapshot"}
    out = await _vms(fake)
    counts = {r["vm"]: r["snapshot_count"] for r in out["vms"]}
    assert counts == {"vm-101": 0, "vm-102": 0, "vm-103": None}
    assert out["read_note"] == (
        "the snapshot property of 1 VM(s) could not be read; their snapshot_count is null"
    )
    Draft202012Validator(VMWARE_VM_LIST_OP.response_schema).validate(out)


@pytest.mark.parametrize(
    ("params", "container", "names"),
    [
        ({"cluster": "domain-c7"}, "domain-c7", ["db-01", "web-01", "web-02"]),
        ({"host": "host-12"}, "host-12", ["db-01", "web-02"]),
        ({"folder": "group-v10"}, "group-v10", ["web-01", "web-02"]),
    ],
)
async def test_vm_list_scope_is_the_view_container(
    params: dict[str, str], container: str, names: list[str]
) -> None:
    fake = _fake()
    out = await _vms(fake, **params)
    [view] = fake.views.values()
    assert view["container"]["value"] == container
    assert [r["name"] for r in out["vms"]] == names


async def test_vm_list_filters_power_state_and_name_pattern() -> None:
    fake = _fake()
    on = await _vms(fake, power_state="poweredOn")
    assert [r["name"] for r in on["vms"]] == ["db-01", "web-01"]
    web = await _vms(fake, name_pattern="WEB-*", power_state="poweredOff")
    assert [r["name"] for r in web["vms"]] == ["web-02"]
    none = await _vms(fake, name_pattern="nothing-*")
    assert none == {"vm_count": 0, "read_note": None, "vms": []}


async def test_vm_list_unknown_scope_is_not_found() -> None:
    with pytest.raises(ConnectorResourceNotFoundError):
        await _vms(_fake(), cluster="domain-c999")


async def test_vm_list_keeps_rows_when_names_cannot_be_read() -> None:
    fake = _fake()
    fake.fail_reads["HostSystem"] = RuntimeError("read failed")
    out = await _vms(fake)
    assert out["vm_count"] == 3
    assert all(r["host_name"] is None and r["cluster"] is None for r in out["vms"])
    assert out["read_note"] == "host, cluster and folder names could not be read (RuntimeError)"


async def test_vm_list_reads_every_page() -> None:
    fake = _fake()
    fake.page_size = 1
    out = await _vms(fake)
    assert out["vm_count"] == 3
    assert len(fake.continue_calls) >= 2


async def test_vm_list_destroys_the_view_when_the_read_fails() -> None:
    fake = _fake()
    fake.page_size = 1
    fake.fail_continue = RuntimeError("page failed")
    with pytest.raises(RuntimeError, match="page failed"):
        await _vms(fake)
    assert fake.destroyed == list(fake.views)


async def test_vm_list_of_5000_vms_in_one_call() -> None:
    fake = ViewFake()
    small_inventory(fake)
    for i in range(5000):
        fake.add(
            "VirtualMachine",
            f"vm-{1000 + i}",
            parent=("Folder", "group-v3"),
            **vm_props(f"bulk-{i:04d}", "host-11", "poweredOff"),
        )
    fake.page_size = 1000
    out = await _vms(fake, name_pattern="bulk-*")
    assert out["vm_count"] == 5000
    assert out["vms"][0]["name"] == "bulk-0000"
    assert out["vms"][-1]["cluster_name"] == "cl-1"


# ---------------------------------------------------------------------------
# vmware.inventory.find
# ---------------------------------------------------------------------------


async def test_find_by_name_reads_every_entity_once_and_gives_paths() -> None:
    fake = _fake()
    out = await _find(fake, name="web-01")
    [view] = fake.views.values()
    assert view["type"] == []
    assert view["container"]["value"] == ROOT
    read = fake.calls[2][1]["specSet"][0]["propSet"][0]
    assert read == {
        "_typeName": "PropertySpec",
        "type": "ManagedEntity",
        "pathSet": ["name", "parent"],
    }
    assert out == {
        "match_count": 1,
        "matches": [
            {
                "kind": "vm",
                "vim_type": "VirtualMachine",
                "id": "vm-101",
                "name": "web-01",
                "path": "dc-a/vm/apps%2fprod/web-01",
            }
        ],
    }
    assert fake.destroyed == list(fake.views)
    Draft202012Validator(VMWARE_INVENTORY_FIND_OP.response_schema).validate(out)


async def test_find_by_pattern_with_a_type() -> None:
    fake = _fake()
    hosts = await _find(fake, name_pattern="ESX-*")
    assert [(m["kind"], m["id"], m["path"]) for m in hosts["matches"]] == [
        ("host", "host-11", "dc-a/host/cl-1/esx-1"),
        ("host", "host-12", "dc-a/host/cl-1/esx-2"),
    ]
    all_kinds = await _find(fake, name_pattern="*-1")
    assert {m["kind"] for m in all_kinds["matches"]} == {"cluster", "datastore", "host"}
    only = await _find(fake, name_pattern="*-1", type="datastore")
    assert [m["id"] for m in only["matches"]] == ["datastore-20"]


async def test_find_by_name_is_exact() -> None:
    assert (await _find(_fake(), name="WEB-01"))["match_count"] == 0


async def test_find_by_path_uses_the_search_index() -> None:
    fake = _fake()
    out = await _find(fake, path="/dc-a/host/cl-1/")
    assert fake.methods() == ["RetrieveServiceContent", "FindByInventoryPath"]
    assert fake.calls[1] == (
        "/SearchIndex/SearchIndex/FindByInventoryPath",
        {"inventoryPath": "/dc-a/host/cl-1/"},
    )
    assert out["matches"] == [
        {
            "kind": "cluster",
            "vim_type": "ClusterComputeResource",
            "id": "domain-c7",
            "name": "cl-1",
            "path": "dc-a/host/cl-1",
        }
    ]


async def test_find_by_path_decodes_a_slash_in_the_name() -> None:
    out = await _find(_fake(), path="dc-a/vm/apps%2fprod")
    assert [(m["id"], m["name"]) for m in out["matches"]] == [("group-v10", "apps/prod")]


async def test_find_by_path_without_a_match_is_empty() -> None:
    assert await _find(_fake(), path="dc-a/vm/missing") == {"match_count": 0, "matches": []}
    out = await _find(_fake(), path="dc-a/host/cl-1", type="vm")
    assert out["matches"] == []


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", [VMWARE_VM_LIST_OP, VMWARE_INVENTORY_FIND_OP])
def test_ops_are_safe_typed_reads_in_the_inventory_group(op: Any) -> None:
    assert op in VMWARE_TYPED_OPS
    assert (op.safety_level, op.requires_approval) == ("safe", False)
    assert op.group_key == INVENTORY_GROUP_KEY
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[INVENTORY_GROUP_KEY].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))
    for example in op.parameter_schema["examples"]:
        assert validate_params(op.parameter_schema, example) == []


def test_vm_list_takes_at_most_one_scope() -> None:
    schema = VMWARE_VM_LIST_OP.parameter_schema
    assert validate_params(schema, {}) == []
    assert validate_params(schema, {"host": "host-1", "power_state": "poweredOn"}) == []
    assert validate_params(schema, {"host": "host-1", "cluster": "domain-c1"}) != []
    assert validate_params(schema, {"power_state": "on"}) != []


def test_find_takes_one_of_name_pattern_and_path() -> None:
    schema = VMWARE_INVENTORY_FIND_OP.parameter_schema
    assert validate_params(schema, {"name": "a"}) == []
    assert validate_params(schema, {"path": "dc/vm"}) == []
    assert validate_params(schema, {}) != []
    assert validate_params(schema, {"name": "a", "path": "dc/vm"}) != []
    assert validate_params(schema, {"name": "a", "type": "planet"}) != []


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service(_settings_env: None) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


async def test_both_reads_are_registered_on_both_catalogs(
    stub_embedding_service: AsyncMock,
) -> None:
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    await register_vmware_typed_operations_8_0(embedding_service=stub_embedding_service)
    op_ids = [VMWARE_VM_LIST_OP.op_id, VMWARE_INVENTORY_FIND_OP.op_id]
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.op_id.in_(op_ids))
                )
            )
            .scalars()
            .all()
        )
    assert {(r.version, r.op_id) for r in rows} == {
        (v, op_id) for v in ("8.0", "9.0") for op_id in op_ids
    }
    assert {(r.source_kind, r.safety_level, r.requires_approval) for r in rows} == {
        ("typed", "safe", False)
    }


async def test_5000_vms_come_back_behind_a_result_handle(
    _clean_registry: None,
    stub_embedding_service: AsyncMock,
) -> None:
    fake = ViewFake()
    small_inventory(fake)
    for i in range(5000):
        fake.add(
            "VirtualMachine",
            f"vm-{1000 + i}",
            parent=("Folder", "group-v3"),
            **vm_props(f"bulk-{i:04d}", "host-12", "poweredOn"),
        )
    fake.page_size = 1000
    _seed_connector(fake)  # type: ignore[arg-type]
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    # The tests run with a pass-through reducer; the app installs this one at start.
    set_default_reducer(JsonFluxReducer())

    result = await dispatch(
        operator=_make_e2e_operator(sub="vm-lister"),
        connector_id="vmware-rest-9.0",
        op_id="vmware.vm.list",
        target=_FakeVmwareTarget(),
        params={"name_pattern": "bulk-*"},
    )
    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.handle.total_rows == 5000
    assert result.result["row_count"] == 5000
    assert "vms" not in result.result
