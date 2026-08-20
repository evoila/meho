# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 18 vmware-rest write-composite handler functions.

Post-#2256 the write composites dispatch their sub-ops **directly on the
connector session** -- ``connector._get_json`` / ``connector._post_json``
mounted through ``connector.mount_op_path`` -- rather than through the
catalog-routed ``dispatch_child`` seam, and every mutating sub-call is
first routed through
:func:`~meho_backplane.operations.composite.enforce_subop_policy` (the
#2254 governance seam). These tests therefore:

* stub the connector session with a recording double and assert the
  call-shape contract: which HTTP verb, against which mounted path, with
  what query / body, in what order -- plus the aggregation each handler
  builds from the canned responses (byte-for-byte unchanged by the
  dispatch-mechanism swap);
* stub :func:`enforce_subop_policy` with a recorder and assert every
  *write* sub-op is gated with its declared ``dangerous`` /
  ``requires_approval=False`` governance and its logical params, while the
  read sub-ops are never gated;
* prove the gate short-circuits: when the seam returns an
  ``awaiting_approval`` result, the handler returns it verbatim and the
  write is never issued.

``host.evacuate`` additionally keeps its ``dispatch_child`` recursion into
``vmware.composite.vm.migrate`` (a registrar-guaranteed composite row,
out of scope for the ingested-dispatch migration per #2248), so its test
supplies a recording ``dispatch_child`` alongside the connector.

The end-to-end approval-queue proof against a real DB + real seam lives in
:mod:`tests.test_connectors_vmware_rest_composites_write_gate`; the
respx-transport parity proof lives in
:mod:`tests.integration.test_connectors_vmware_rest_vcsim`.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator, ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_drs_rule_create_composite,
    cluster_patch_composite,
    folder_create_composite,
    guest_customization_spec_create_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_customize_composite,
    vm_deploy_from_library_composite,
    vm_device_cdrom_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
    vm_nic_repoint_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_resize_composite,
    vm_snapshot_revert_composite,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-composite-write",
        name="Composite Write Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


class _RecordingConnector:
    """Stub connector session that records sub-calls and serves canned JSON.

    Stands in for :class:`VmwareRestConnector` on the direct-dispatch path:
    the handlers call ``mount_op_path`` to resolve the live mount and then
    ``_get_json`` / ``_post_json`` on the returned path. Every call is
    recorded as ``{"method", "path", "query", "body"}`` (``method`` is the
    write verb for ``_post_json``), and a response is served keyed either by
    the resolved (mounted) path or, in list form, sequentially. A canned
    value that is an :class:`Exception` is raised -- how the transport-fault
    partial-failure paths are exercised.
    """

    def __init__(
        self,
        responses: dict[str, Any] | list[Any],
        *,
        mount_prefix: str = "/api",
        vmomi: dict[str, Any] | None = None,
    ) -> None:
        self._responses = responses
        self._seq_index = 0
        self._mount_prefix = mount_prefix
        self._vmomi = vmomi or {}
        self.calls: list[dict[str, Any]] = []
        self.mount_calls: list[str] = []
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": "GET", "path": path, "query": params, "body": None})
        return self._serve(path)

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append({"method": verb, "path": path, "query": None, "body": json})
        return self._serve(path)

    def _serve(self, path: str) -> Any:
        if isinstance(self._responses, dict):
            payload = self._responses[path]
        else:
            payload = self._responses[self._seq_index]
            self._seq_index += 1
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        """Serve the vim (VI-JSON) sub-calls the #2970 composite steps issue.

        Recorded into ``vmomi_calls``. Responses are keyed by the vmomi
        method path (``/HostSystem/h-1/EnterMaintenanceMode_Task``);
        ``RetrievePropertiesEx`` bodies are instead keyed by the queried
        object type (``VirtualMachine`` / ``ClusterComputeResource`` /
        ``DistributedVirtualSwitch`` / ``Task`` -- the seam a real vCenter
        keys them by, mirroring ``_DiskGrowConnector``). An unkeyed
        ``Task`` read serves a terminal-success ``Task.info`` so tests
        only model the poll when a fault/timeout is the point.
        """
        self.vmomi_calls.append((path, json))
        if path.endswith("/RetrievePropertiesEx"):
            spec_type = json["specSet"][0]["propSet"][0]["type"]
            if spec_type in self._vmomi:
                payload = self._vmomi[spec_type]
                if isinstance(payload, Exception):
                    raise payload
                return payload
            if spec_type == "Task":
                task_moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
                return _task_info_result(task_moid, "success")
            raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")
        payload = self._vmomi[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


class _GateRecorder:
    """Recording stub for :func:`enforce_subop_policy`.

    Records every ``(op_id, safety_level, requires_approval, params)`` the
    handler gates and returns a canned verdict: ``None`` (auto-execute -->
    the handler proceeds with the direct write) by default, or a
    per-op_id :class:`OperationResult` (``awaiting_approval`` / ``denied``)
    when the test wants to prove the gate short-circuits.
    """

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "op_id": op_id,
                "connector_id": connector_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": dict(params),
            }
        )
        return self._gate_for.get(op_id)

    @property
    def gated_op_ids(self) -> list[str]:
        return [c["op_id"] for c in self.calls]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    """Install a default (auto-execute) gate recorder on the ``_write`` module."""
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _install_gate(monkeypatch: pytest.MonkeyPatch, recorder: _GateRecorder) -> _GateRecorder:
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


def _awaiting(op_id: str) -> OperationResult:
    """A canned ``awaiting_approval`` result the gate stub can return."""
    return OperationResult(
        status="awaiting_approval",
        op_id=op_id,
        result=None,
        duration_ms=1.0,
        extras={"approval_request_id": "00000000-0000-0000-0000-0000000000aa"},
    )


def _http_error(status: int, url: str) -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` whose ``str`` carries status + URL."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{url}'", request=request, response=response
    )


# --- vim (VI-JSON) canned-payload builders for the #2970 composite steps ---


def _task_moref(value: str) -> dict[str, str]:
    """A vim Task ``ManagedObjectReference`` as a ``*_Task`` method returns it."""
    return {"type": "Task", "value": value}


def _retrieve_result(obj_type: str, moid: str, prop: str, val: Any) -> dict[str, Any]:
    """A single-object ``RetrievePropertiesEx`` result carrying one property."""
    return {
        "objects": [
            {
                "obj": {"type": obj_type, "value": moid},
                "propSet": [{"name": prop, "val": val}],
            }
        ]
    }


def _task_info_result(task_moid: str, state: str, error: str | None = None) -> dict[str, Any]:
    """A ``Task.info`` RetrievePropertiesEx result in the requested state."""
    info: dict[str, Any] = {"state": state}
    if error is not None:
        info["error"] = {"localizedMessage": error}
    return _retrieve_result("Task", task_moid, "info", info)


def _snapshot_tree_node(
    moid: str, name: str, children: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A ``VirtualMachineSnapshotTree`` node as the ``snapshot`` property returns it."""
    return {
        "snapshot": {"type": "VirtualMachineSnapshot", "value": moid},
        "name": name,
        "childSnapshotList": children or [],
    }


# ===========================================================================
# vm.create
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_create_happy_path_direct_session(gate: _GateRecorder) -> None:
    """Folder GET -> create POST -> NIC adapter POST -> power POST; every write gated."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/folder": [{"folder": "folder-7", "name": "Prod"}],
            "/api/vcenter/vm": {"value": "vm-99"},
            "/api/vcenter/vm/vm-99/hardware/ethernet": {"value": "nic-1"},
            "/api/vcenter/vm/vm-99/power?action=start": {},
        }
    )
    out = await vm_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "folder_name": "Prod",
            "name": "web-01",
            "guest_os": "UBUNTU_64",
            "cpu_count": 2,
            "memory_mib": 4096,
            "nics": [{"network": "net-3"}],
            "power_on_after_create": True,
        },
        connector=conn,  # type: ignore[arg-type]
    )

    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/folder"),
        ("POST", "/api/vcenter/vm"),
        ("POST", "/api/vcenter/vm/vm-99/hardware/ethernet"),
        ("POST", "/api/vcenter/vm/vm-99/power?action=start"),
    ]
    # Folder GET forwards the name filter as a query param; bare on /api (#2298).
    assert conn.calls[0]["query"] == {"names": ["Prod"]}
    # Create body is the VM.CreateSpec at the top level (#2973); folder moid resolved in.
    assert conn.calls[1]["body"]["placement"]["folder"] == "folder-7"
    # NIC create body is the Ethernet.CreateSpec at the top level (#2973): the
    # network rides the backing spec (#2970); vm rides the path, not the body.
    assert conn.calls[2]["body"] == {"backing": {"type": "STANDARD_PORTGROUP", "network": "net-3"}}
    # Power POST carries no body (action rides the path).
    assert conn.calls[3]["body"] is None

    # Governance: exactly the 3 writes were gated (dangerous / no-approval);
    # the folder GET was never gated.
    assert gate.gated_op_ids == [
        "POST:/vcenter/vm",
        "POST:/vcenter/vm/{vm}/hardware/ethernet",
        "POST:/vcenter/vm/{vm}/power?action=start",
    ]
    for call in gate.calls:
        assert call["safety_level"] == "dangerous"
        assert call["requires_approval"] is False
        assert call["connector_id"] == "vmware-rest-9.0"

    assert out["status"] == "created"
    assert out["vm_id"] == "vm-99"
    assert out["steps_succeeded"] == ["folder_lookup", "create", "nic_attach", "power_on"]
    assert out["failed_step"] is None


@pytest.mark.asyncio
async def test_vm_create_nic_failure_rolls_back_via_delete(gate: _GateRecorder) -> None:
    """A NIC-attach transport error triggers DELETE rollback; status=rolled_back."""
    conn = _RecordingConnector(
        [
            [{"folder": "folder-1", "name": "Prod"}],  # folder GET
            {"value": "vm-77"},  # create POST
            _http_error(400, "https://vc/api/vcenter/vm/vm-77/network"),  # NIC PATCH
            {},  # DELETE rollback
        ]
    )
    out = await vm_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "folder_name": "Prod",
            "name": "web-02",
            "guest_os": "UBUNTU_64",
            "nics": [{"network": "net-x"}],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    verbs = [(c["method"], c["path"]) for c in conn.calls]
    assert ("DELETE", "/api/vcenter/vm/vm-77") in verbs
    assert out["status"] == "rolled_back"
    assert out["vm_id"] is None
    assert out["failed_step"] == "nic_attach"
    assert out["rollback_reason"]


@pytest.mark.asyncio
async def test_vm_create_folder_lookup_empty_returns_rolled_back_no_create(
    gate: _GateRecorder,
) -> None:
    """Empty folder match returns rolled_back; the create POST never fires."""
    conn = _RecordingConnector({"/api/vcenter/folder": []})
    out = await vm_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"folder_name": "Missing", "name": "web", "guest_os": "UBUNTU_64"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "rolled_back"
    assert out["failed_step"] == "folder_lookup"
    assert len(conn.calls) == 1
    # No write was attempted, so nothing was gated.
    assert gate.calls == []


@pytest.mark.asyncio
async def test_vm_create_gated_create_returns_awaiting_approval_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated create returns the seam's awaiting_approval verbatim; no create POST fires.

    The unit-level proof of the hard acceptance gate: when
    :func:`enforce_subop_policy` returns a non-None result for the write
    sub-op, the composite returns it verbatim (the dispatcher passes a
    handler-returned OperationResult straight through) and the direct
    ``_post_json`` is never reached.
    """
    _install_gate(
        monkeypatch, _GateRecorder(gate_for={"POST:/vcenter/vm": _awaiting("POST:/vcenter/vm")})
    )
    conn = _RecordingConnector({"/api/vcenter/folder": [{"folder": "f-1", "name": "Prod"}]})
    out = await vm_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"folder_name": "Prod", "name": "web", "guest_os": "UBUNTU_64"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == "POST:/vcenter/vm"
    # Only the folder GET hit the session; the create POST was gated off.
    assert [c["method"] for c in conn.calls] == ["GET"]


# ===========================================================================
# vm.clone
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_clone_happy_path_synchronous_deploy(gate: _GateRecorder) -> None:
    """Source GET -> per-item deploy POST (gated); 200 body IS the new VM id (#2970)."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {"name": "src"},
            "/api/vcenter/vm-template/library-items/li-7?action=deploy": "vm-clone-1",
        }
    )
    out = await vm_clone_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "source_vm": "vm-src",
            "target_name": "vm-clone-1",
            "library_item": "li-7",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "completed"
    assert out["task_id"] is None
    assert out["vm_id"] == "vm-clone-1"
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/vm/vm-src"),
        ("POST", "/api/vcenter/vm-template/library-items/li-7?action=deploy"),
    ]
    # The template item rides the path (#2970); the body is only the DeploySpec.
    assert conn.calls[1]["body"] == {"spec": {"name": "vm-clone-1"}}
    # Only the deploy write was gated, under the per-item op_id.
    assert gate.gated_op_ids == [
        "POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy"
    ]


@pytest.mark.asyncio
async def test_vm_clone_legacy_value_envelope_unwraps(gate: _GateRecorder) -> None:
    """A legacy ``{"value": ...}``-wrapped deploy response unwraps to the VM id."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {"name": "src"},
            "/api/vcenter/vm-template/library-items/li-3?action=deploy": {"value": "vm-88"},
        }
    )
    out = await vm_clone_composite(
        operator=_make_operator(),
        target=object(),
        params={"source_vm": "vm-src", "target_name": "tgt", "library_item": "li-3"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "completed"
    assert out["vm_id"] == "vm-88"
    assert len(conn.calls) == 2


@pytest.mark.asyncio
async def test_vm_clone_non_string_deploy_payload_raises(gate: _GateRecorder) -> None:
    """A deploy payload that is not the VM id string raises -- wrapped connector_error."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {},
            "/api/vcenter/vm-template/library-items/li-1?action=deploy": {"task": "task-bad"},
        }
    )
    with pytest.raises(RuntimeError, match="no VM id"):
        await vm_clone_composite(
            operator=_make_operator(),
            target=object(),
            params={"source_vm": "vm-src", "target_name": "tgt", "library_item": "li-1"},
            connector=conn,  # type: ignore[arg-type]
        )


# ===========================================================================
# vm.deploy_from_library (OVF/OVA content-library deploy -- #2909)
# ===========================================================================

_DEPLOY_OVF_OP = "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy"
_FIND_LIBRARY_PATH = "/api/content/library?action=find"
_FIND_ITEM_PATH = "/api/content/library/item?action=find"


def _deployment_result(
    *,
    succeeded: bool,
    vm_id: str = "vm-ovf-9",
    resource_type: str = "VirtualMachine",
    errors: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Canned ``Vcenter.Ovf.LibraryItem.DeploymentResult`` for the deploy stub."""
    result: dict[str, Any] = {"succeeded": succeeded}
    if succeeded:
        result["resource_id"] = {"type": resource_type, "id": vm_id}
    error_block: dict[str, Any] = {}
    if errors is not None:
        error_block["errors"] = errors
    if warnings is not None:
        error_block["warnings"] = warnings
    if error_block:
        result["error"] = error_block
    return result


@pytest.mark.asyncio
async def test_deploy_from_library_id_passthrough_deployed(gate: _GateRecorder) -> None:
    """library_item id passthrough → one gated deploy → status='deployed' with the VM id."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/ovf/library-item/li-ovf?action=deploy": _deployment_result(
                succeeded=True, vm_id="vm-ovf-1"
            ),
        }
    )
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deployed"
    assert out["vm_id"] == "vm-ovf-1"
    assert out["resource_type"] == "VirtualMachine"
    assert out["library_item_id"] == "li-ovf"
    assert out["powered_on"] is False
    assert out["issues"] == []
    # No name-resolution reads on the id path; only the deploy was gated.
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("POST", "/api/vcenter/ovf/library-item/li-ovf?action=deploy"),
    ]
    assert gate.gated_op_ids == [_DEPLOY_OVF_OP]


@pytest.mark.asyncio
async def test_deploy_from_library_deploy_body_shape(gate: _GateRecorder) -> None:
    """The deploy body carries the {deployment_spec, target} shape the pinned spec keys."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/ovf/library-item/li-ovf?action=deploy": _deployment_result(
                succeeded=True
            ),
        }
    )
    await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "library_item": "li-ovf",
            "name": "router-01",
            "resource_pool": "resgroup-8",
            "host": "host-19",
            "folder": "group-v10",
            "datastore": "datastore-15",
            "network_mappings": {"nat": "dvportgroup-42", "mgmt": "dvportgroup-43"},
            "storage_provisioning": "thin",
            "storage_profile": "profile-1",
            "ovf_properties": {"guestinfo.hostname": "router-01"},
        },
        connector=conn,  # type: ignore[arg-type]
    )
    body = conn.calls[0]["body"]
    assert body["deployment_spec"] == {
        "accept_all_eula": True,
        "name": "router-01",
        "network_mappings": {"nat": "dvportgroup-42", "mgmt": "dvportgroup-43"},
        "storage_provisioning": "thin",
        "storage_profile_id": "profile-1",
        "default_datastore_id": "datastore-15",
        "additional_parameters": [
            {
                "type": "PropertyParams",
                "properties": [{"id": "guestinfo.hostname", "value": "router-01"}],
            }
        ],
    }
    assert body["target"] == {
        "resource_pool_id": "resgroup-8",
        "host_id": "host-19",
        "folder_id": "group-v10",
    }


@pytest.mark.asyncio
async def test_deploy_from_library_name_resolution_scoped_by_library(gate: _GateRecorder) -> None:
    """library_item_name + library_name → find library → find item (type=ovf) → deploy."""
    conn = _RecordingConnector(
        {
            _FIND_LIBRARY_PATH: ["lib-7"],
            _FIND_ITEM_PATH: ["li-resolved"],
            "/api/vcenter/ovf/library-item/li-resolved?action=deploy": _deployment_result(
                succeeded=True, vm_id="vm-ovf-2"
            ),
        }
    )
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "library_item_name": "holorouter-ova",
            "library_name": "lab-templates",
            "resource_pool": "resgroup-8",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deployed"
    assert out["library_item_id"] == "li-resolved"
    # Library find, then item find (scoped + type=ovf), then the deploy.
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("POST", _FIND_LIBRARY_PATH),
        ("POST", _FIND_ITEM_PATH),
        ("POST", "/api/vcenter/ovf/library-item/li-resolved?action=deploy"),
    ]
    # The find bodies are the FindSpec at the top level of the /api body (#3071),
    # NOT the legacy /rest {"spec": {...}} envelope vCenter 8.x 400s.
    assert conn.calls[0]["body"] == {"name": "lab-templates"}
    assert conn.calls[1]["body"] == {"name": "holorouter-ova", "type": "ovf", "library_id": "lib-7"}
    # The finds are un-gated reads; only the deploy hit the policy seam.
    assert gate.gated_op_ids == [_DEPLOY_OVF_OP]


@pytest.mark.asyncio
async def test_deploy_from_library_ambiguous_item_refused(gate: _GateRecorder) -> None:
    """An item name matching >1 item → ambiguous_item with candidates; no deploy, no gate."""
    conn = _RecordingConnector({_FIND_ITEM_PATH: ["li-a", "li-b"]})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item_name": "dup-ova", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous_item"
    assert out["candidates"] == ["li-a", "li-b"]
    assert out["vm_id"] is None
    assert out["library_item_id"] is None
    assert [c["path"] for c in conn.calls] == [_FIND_ITEM_PATH]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_deploy_from_library_ambiguous_library_refused(gate: _GateRecorder) -> None:
    """A library name matching >1 library → ambiguous_library before any item lookup."""
    conn = _RecordingConnector({_FIND_LIBRARY_PATH: ["lib-1", "lib-2"]})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "library_item_name": "ova",
            "library_name": "dup-lib",
            "resource_pool": "resgroup-8",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous_library"
    assert out["candidates"] == ["lib-1", "lib-2"]
    # Stopped at the library find; the item find never fired.
    assert [c["path"] for c in conn.calls] == [_FIND_LIBRARY_PATH]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_deploy_from_library_item_not_found(gate: _GateRecorder) -> None:
    """An item name matching zero items → item_not_found; no deploy."""
    conn = _RecordingConnector({_FIND_ITEM_PATH: []})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item_name": "missing-ova", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "item_not_found"
    assert out["candidates"] is None
    assert gate.calls == []


@pytest.mark.asyncio
async def test_deploy_from_library_invalid_reference(gate: _GateRecorder) -> None:
    """Neither library_item nor library_item_name → invalid_reference; nothing touched."""
    conn = _RecordingConnector({})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "invalid_reference"
    assert conn.calls == []
    assert gate.calls == []


@pytest.mark.asyncio
async def test_deploy_from_library_deploy_failed_surfaces_report_issues(
    gate: _GateRecorder,
) -> None:
    """succeeded=false → deploy_failed with the report's per-issue messages (not a raw fault)."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/ovf/library-item/li-ovf?action=deploy": _deployment_result(
                succeeded=False,
                errors=[
                    {
                        "category": "INPUT",
                        "message": {"default_message": "network 'nat' not mapped"},
                    },
                    {
                        "category": "SERVER",
                        "error": {"messages": [{"default_message": "host busy"}]},
                    },
                ],
                warnings=[
                    {"category": "VALIDATION", "message": {"default_message": "deprecated hw"}}
                ],
            ),
        }
    )
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deploy_failed"
    assert out["vm_id"] is None
    assert out["library_item_id"] == "li-ovf"
    assert out["issues"] == [
        {"category": "INPUT", "severity": "error", "message": "network 'nat' not mapped"},
        {"category": "SERVER", "severity": "error", "message": "host busy"},
        {"category": "VALIDATION", "severity": "warning", "message": "deprecated hw"},
    ]


@pytest.mark.asyncio
async def test_deploy_from_library_deploy_error_on_http_fault(gate: _GateRecorder) -> None:
    """A 404 on the deploy (missing placement resource) → structured deploy_error, not a raise."""
    fault = httpx.HTTPStatusError(
        "not found",
        request=httpx.Request(
            "POST", "https://vc/api/vcenter/ovf/library-item/li-ovf?action=deploy"
        ),
        response=httpx.Response(
            404,
            json={
                "error_type": "NOT_FOUND",
                "messages": [{"default_message": "resource pool resgroup-missing not found"}],
            },
        ),
    )
    conn = _RecordingConnector({"/api/vcenter/ovf/library-item/li-ovf?action=deploy": fault})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-missing"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deploy_error"
    assert out["library_item_id"] == "li-ovf"
    assert len(out["issues"]) == 1
    assert out["issues"][0]["category"] == "placement"
    # The structured message carries the HTTP status, the vAPI error_type, and
    # the localized vendor message (#3071 diagnosability, #1649/#1804 pattern).
    assert "404" in out["issues"][0]["message"]
    assert "NOT_FOUND" in out["issues"][0]["message"]
    assert "resource pool resgroup-missing not found" in out["issues"][0]["message"]


@pytest.mark.asyncio
async def test_deploy_from_library_resolve_error_on_find_http_fault(gate: _GateRecorder) -> None:
    """A 4xx on the content-library find → structured resolve_error, not a bare fault.

    Regression guard for #3071: the name-resolution find call sits before the
    deploy gate, so a fault there used to escape the composite uncaught and
    surface as an opaque ``connector_error: HTTPStatusError`` with no status,
    url, or vendor message. It is now folded into the ``resolve_error`` arm
    carrying the vCenter HTTP status + error_type + localized message.
    """
    fault = httpx.HTTPStatusError(
        "bad request",
        request=httpx.Request("POST", "https://vc/api/content/library/item?action=find"),
        response=httpx.Response(
            400,
            json={
                "error_type": "INVALID_ARGUMENT",
                "messages": [{"default_message": "Invalid input for method: Find"}],
            },
        ),
    )
    conn = _RecordingConnector({_FIND_ITEM_PATH: fault})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item_name": "holorouter-ova", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "resolve_error"
    assert out["vm_id"] is None
    assert out["library_item_id"] is None
    assert out["candidates"] is None
    assert len(out["issues"]) == 1
    issue = out["issues"][0]
    assert issue["category"] == "resolve"
    assert issue["severity"] == "error"
    assert "400" in issue["message"]
    assert "INVALID_ARGUMENT" in issue["message"]
    assert "Invalid input for method: Find" in issue["message"]
    # The fault happened during un-gated resolution; nothing hit the policy seam.
    assert gate.calls == []
    assert [c["path"] for c in conn.calls] == [_FIND_ITEM_PATH]


@pytest.mark.asyncio
async def test_deploy_from_library_gate_short_circuits_before_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on the OVF deploy returns verbatim; the deploy never reaches the wire."""
    gate = _install_gate(
        monkeypatch,
        _GateRecorder(gate_for={_DEPLOY_OVF_OP: _awaiting(_DEPLOY_OVF_OP)}),
    )
    conn = _RecordingConnector({})
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-8"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _DEPLOY_OVF_OP
    # id passthrough → no resolution reads, and the deploy was gated off the wire.
    assert conn.calls == []
    assert gate.gated_op_ids == [_DEPLOY_OVF_OP]


@pytest.mark.asyncio
async def test_deploy_from_library_power_on_after_deploy(gate: _GateRecorder) -> None:
    """power_on=true → deploy then a gated power-start; powered_on=true."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/ovf/library-item/li-ovf?action=deploy": _deployment_result(
                succeeded=True, vm_id="vm-ovf-7"
            ),
            "/api/vcenter/vm/vm-ovf-7/power?action=start": {},
        }
    )
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-8", "power_on": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deployed"
    assert out["powered_on"] is True
    assert out["issues"] == []
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("POST", "/api/vcenter/ovf/library-item/li-ovf?action=deploy"),
        ("POST", "/api/vcenter/vm/vm-ovf-7/power?action=start"),
    ]
    assert gate.gated_op_ids == [_DEPLOY_OVF_OP, "POST:/vcenter/vm/{vm}/power?action=start"]


@pytest.mark.asyncio
async def test_deploy_from_library_power_on_failure_stays_deployed(gate: _GateRecorder) -> None:
    """A power-on fault leaves status='deployed' with powered_on=false + a warning issue."""
    fault = httpx.HTTPStatusError(
        "conflict",
        request=httpx.Request("POST", "https://vc/api/vcenter/vm/vm-ovf-7/power?action=start"),
        response=httpx.Response(500, json={}),
    )
    conn = _RecordingConnector(
        {
            "/api/vcenter/ovf/library-item/li-ovf?action=deploy": _deployment_result(
                succeeded=True, vm_id="vm-ovf-7"
            ),
            "/api/vcenter/vm/vm-ovf-7/power?action=start": fault,
        }
    )
    out = await vm_deploy_from_library_composite(
        operator=_make_operator(),
        target=object(),
        params={"library_item": "li-ovf", "resource_pool": "resgroup-8", "power_on": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "deployed"
    assert out["vm_id"] == "vm-ovf-7"
    assert out["powered_on"] is False
    assert len(out["issues"]) == 1
    assert out["issues"][0]["category"] == "power_on"
    assert out["issues"][0]["severity"] == "warning"


# ===========================================================================
# vm.snapshot.revert
# ===========================================================================


def _snapshot_info_vmomi(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Canned ``VirtualMachine.snapshot`` property read for the vmomi stub."""
    return _retrieve_result("VirtualMachine", "vm-1", "snapshot", {"rootSnapshotList": nodes})


@pytest.mark.asyncio
async def test_vm_snapshot_revert_happy_path(gate: _GateRecorder) -> None:
    """vim tree read + match + RevertToSnapshot_Task (gated, polled); status=reverted."""
    conn = _RecordingConnector(
        {},
        vmomi={
            "VirtualMachine": _snapshot_info_vmomi([_snapshot_tree_node("snap-1", "before-patch")]),
            "/VirtualMachineSnapshot/snap-1/RevertToSnapshot_Task": _task_moref("task-revert-1"),
        },
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "before-patch"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "reverted"
    assert out["snapshot_id"] == "snap-1"
    # No REST sub-call fired -- the snapshot surface is vim-only (#2970).
    assert conn.calls == []
    revert_calls = [(p, b) for p, b in conn.vmomi_calls if p.endswith("/RevertToSnapshot_Task")]
    assert revert_calls == [("/VirtualMachineSnapshot/snap-1/RevertToSnapshot_Task", {})]
    assert gate.gated_op_ids == ["POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task"]


@pytest.mark.asyncio
async def test_vm_snapshot_revert_ambiguous_no_revert(gate: _GateRecorder) -> None:
    """Snapshots sharing the name across the tree -> ambiguous; no revert dispatched.

    The duplicate lives in a ``childSnapshotList`` to prove the tree walk
    recurses (the flat pre-#2970 REST listing had no nesting).
    """
    conn = _RecordingConnector(
        {},
        vmomi={
            "VirtualMachine": _snapshot_info_vmomi(
                [_snapshot_tree_node("snap-1", "x", [_snapshot_tree_node("snap-2", "x")])]
            ),
        },
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "x"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous"
    assert out["candidates"] == [
        {"name": "x", "snapshot": "snap-1"},
        {"name": "x", "snapshot": "snap-2"},
    ]
    assert len(conn.vmomi_calls) == 1
    assert gate.calls == []


@pytest.mark.asyncio
async def test_vm_snapshot_revert_not_found(gate: _GateRecorder) -> None:
    """Snapshot name not in tree -> status=not_found; no revert dispatched."""
    conn = _RecordingConnector(
        {},
        vmomi={"VirtualMachine": _snapshot_info_vmomi([_snapshot_tree_node("s-1", "other")])},
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "missing"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "not_found"
    assert len(conn.vmomi_calls) == 1
    assert gate.calls == []


# ===========================================================================
# vm.migrate
# ===========================================================================


def _drs_recommendation_vmomi(vm_moid: str, destination: str) -> dict[str, Any]:
    """Canned ``ClusterComputeResource.drsRecommendation`` property read."""
    return _retrieve_result(
        "ClusterComputeResource",
        "cluster-7",
        "drsRecommendation",
        [
            {
                "key": "1",
                "migrationList": [
                    {
                        "vm": {"type": "VirtualMachine", "value": vm_moid},
                        "destination": {"type": "HostSystem", "value": destination},
                    }
                ],
            }
        ],
    )


@pytest.mark.asyncio
async def test_vm_migrate_drs_recommendation_dispatches_relocate(gate: _GateRecorder) -> None:
    """vim drsRecommendation read -> relocate POST (gated) against the recommended host."""
    conn = _RecordingConnector(
        {"/api/vcenter/vm/vm-1?action=relocate": {}},
        vmomi={"ClusterComputeResource": _drs_recommendation_vmomi("vm-1", "host-A")},
    )
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cluster": "cluster-7"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "migrated"
    assert out["target_host"] == "host-A"
    assert out["source"] == "drs"
    # The DRS read is vim (#2970 -- no DRS REST resource in the pinned spec).
    assert [p for p, _ in conn.vmomi_calls] == [
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    ]
    assert conn.calls[0]["path"] == "/api/vcenter/vm/vm-1?action=relocate"
    assert conn.calls[0]["body"] == {"placement": {"host": "host-A"}}
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}?action=relocate"]


@pytest.mark.asyncio
async def test_vm_migrate_explicit_target_bypasses_drs(gate: _GateRecorder) -> None:
    """``target_host`` override skips the DRS GET; relocate dispatches directly."""
    conn = _RecordingConnector({"/api/vcenter/vm/vm-2?action=relocate": {}})
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-2", "cluster": "cluster-9", "target_host": "host-Z"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "migrated"
    assert out["target_host"] == "host-Z"
    assert out["source"] == "operator"
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_vm_migrate_no_recommendation(gate: _GateRecorder) -> None:
    """DRS returns empty + no override -> status=no_recommendation; no relocate."""
    conn = _RecordingConnector(
        {},
        vmomi={
            "ClusterComputeResource": _retrieve_result(
                "ClusterComputeResource", "cluster-1", "drsRecommendation", []
            )
        },
    )
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-3", "cluster": "cluster-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "no_recommendation"
    assert out["source"] == "none"
    assert conn.calls == []
    assert len(conn.vmomi_calls) == 1
    assert gate.calls == []


# ===========================================================================
# vm.power.bulk
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_power_bulk_happy_path(gate: _GateRecorder) -> None:
    """Filter GET + per-VM power POST; aggregate results + summary."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": [{"vm": "vm-1"}, {"vm": "vm-2"}, {"vm": "vm-3"}],
            "/api/vcenter/vm/vm-1/power?action=start": {},
            "/api/vcenter/vm/vm-2/power?action=start": {},
            "/api/vcenter/vm/vm-3/power?action=start": {},
        }
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter": {"power_states": ["POWERED_OFF"]}, "action": "start"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["summary"] == {"ok": 3, "error": 0}
    assert {r["vm"] for r in out["results"]} == {"vm-1", "vm-2", "vm-3"}
    assert out["aborted_on_failure"] is False
    # Filter forwarded as the bare power_states query on the listing GET (/api, #2298).
    assert conn.calls[0]["query"] == {"power_states": ["POWERED_OFF"]}
    # Every per-VM power write was gated.
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}/power?action=start"] * 3


@pytest.mark.asyncio
async def test_vm_power_bulk_partial_failure_continues(gate: _GateRecorder) -> None:
    """One per-VM transport error does not abort; summary reflects mixed outcome."""
    conn = _RecordingConnector(
        [
            [{"vm": "vm-1"}, {"vm": "vm-2"}],  # listing GET
            _http_error(500, "https://vc/api/vcenter/vm/vm-1/power?action=stop"),
            {},  # vm-2 ok
        ]
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"action": "stop"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["summary"] == {"ok": 1, "error": 1}
    assert out["aborted_on_failure"] is False
    assert len(conn.calls) == 3


@pytest.mark.asyncio
async def test_vm_power_bulk_fail_fast_aborts(gate: _GateRecorder) -> None:
    """fail_fast=True -> abort after first transport error; remaining VMs untouched."""
    conn = _RecordingConnector(
        [
            [{"vm": "vm-1"}, {"vm": "vm-2"}, {"vm": "vm-3"}],
            _http_error(403, "https://vc/api/vcenter/vm/vm-1/power?action=stop"),
        ]
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"action": "stop", "fail_fast": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["aborted_on_failure"] is True
    assert out["summary"] == {"ok": 0, "error": 1}
    assert len(out["results"]) == 1
    assert len(conn.calls) == 2


@pytest.mark.asyncio
async def test_vm_power_bulk_gated_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied per-VM power short-circuits the whole batch with the seam's result."""
    op_id = "POST:/vcenter/vm/{vm}/power?action=start"
    denied = OperationResult(status="denied", op_id=op_id, error="policy denied", duration_ms=1.0)
    _install_gate(monkeypatch, _GateRecorder(gate_for={op_id: denied}))
    conn = _RecordingConnector({"/api/vcenter/vm": [{"vm": "vm-1"}, {"vm": "vm-2"}]})
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"action": "start"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "denied"
    # Only the listing GET hit the session; no power write executed.
    assert [c["method"] for c in conn.calls] == ["GET"]


# ===========================================================================
# vm.power (single VM)
# ===========================================================================


def _http_error_with_body(status: int, url: str, body: dict[str, Any]) -> httpx.HTTPStatusError:
    """An ``httpx.HTTPStatusError`` whose response carries a JSON vCenter error body."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(
        f"Server error '{status}' for url '{url}'", request=request, response=response
    )


@pytest.mark.asyncio
async def test_vm_power_hard_verb_happy_path(gate: _GateRecorder) -> None:
    """A hard verb dispatches the mapped power op and reports ``ok`` (Tools not consulted)."""
    conn = _RecordingConnector({"/api/vcenter/vm/vm-1/power?action=stop": {}})
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "off"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out == {
        "vm": "vm-1",
        "verb": "off",
        "status": "ok",
        "error": None,
        "error_type": None,
        "guest_tools": None,
    }
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}/power?action=stop"]
    assert [c["method"] for c in conn.calls] == ["POST"]


@pytest.mark.asyncio
async def test_vm_power_guest_shutdown_happy_path(gate: _GateRecorder) -> None:
    """A soft verb hits the guest-power endpoint and records ``guest_tools='ok'``."""
    conn = _RecordingConnector({"/api/vcenter/vm/vm-1/guest/power?action=shutdown": {}})
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "guest_shutdown"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ok"
    assert out["guest_tools"] == "ok"
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}/guest/power?action=shutdown"]


@pytest.mark.asyncio
async def test_vm_power_guest_shutdown_tools_unavailable_is_typed(gate: _GateRecorder) -> None:
    """Tools-down (HTTP 503 ServiceUnavailable) fails typed, surfacing the Tools state."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/guest/power?action=shutdown": _http_error_with_body(
                503,
                "https://vc/api/vcenter/vm/vm-1/guest/power?action=shutdown",
                {
                    "error_type": "SERVICE_UNAVAILABLE",
                    "messages": [{"default_message": "no tools"}],
                },
            )
        }
    )
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "guest_shutdown"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "tools_unavailable"
    assert out["guest_tools"] == "unavailable"
    assert out["error_type"] == "SERVICE_UNAVAILABLE"
    assert "503" in out["error"]


@pytest.mark.asyncio
async def test_vm_power_guest_reboot_non_tools_error_is_generic(gate: _GateRecorder) -> None:
    """A non-Tools guest fault (e.g. VM suspended, HTTP 400) is a plain typed error."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/guest/power?action=reboot": _http_error_with_body(
                400,
                "https://vc/api/vcenter/vm/vm-1/guest/power?action=reboot",
                {"error_type": "NOT_ALLOWED_IN_CURRENT_STATE"},
            )
        }
    )
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "guest_reboot"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "error"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    # Still a guest verb, so the Tools column reads 'unavailable' (the guest
    # request did not complete), but it is not the tools_unavailable status.
    assert out["guest_tools"] == "unavailable"


@pytest.mark.asyncio
async def test_vm_power_hard_verb_transport_error_is_generic(gate: _GateRecorder) -> None:
    """A hard-verb fault never classifies as tools_unavailable and carries no Tools column."""
    conn = _RecordingConnector(
        {"/api/vcenter/vm/vm-1/power?action=start": _http_error(503, "https://vc/x")}
    )
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "on"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "error"
    assert out["guest_tools"] is None


@pytest.mark.asyncio
async def test_vm_power_gated_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """An awaiting-approval gate returns the seam's result verbatim; no power op fires."""
    op_id = "POST:/vcenter/vm/{vm}/power?action=reset"
    _install_gate(monkeypatch, _GateRecorder(gate_for={op_id: _awaiting(op_id)}))
    conn = _RecordingConnector({})
    out = await vm_power_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "verb": "reset"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert conn.calls == []


# ===========================================================================
# host.evacuate -- recursive composite (dispatch_child kept for vm.migrate)
# ===========================================================================


class _RecordingDispatchChild:
    """Records ``dispatch_child`` calls; serves canned :class:`OperationResult`s."""

    def __init__(self, results: list[OperationResult]) -> None:
        self._results = results
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        self.calls.append({"connector_id": connector_id, "op_id": op_id, "params": dict(params)})
        result = self._results[self._i]
        self._i += 1
        return result


def _migrated() -> OperationResult:
    return OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "migrated", "target_host": "h-2"},
        duration_ms=1.0,
    )


def _no_rec() -> OperationResult:
    return OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "no_recommendation"},
        duration_ms=1.0,
    )


@pytest.mark.asyncio
async def test_host_evacuate_recurses_then_enters_maintenance(gate: _GateRecorder) -> None:
    """VM listing GET -> per-VM vm.migrate via dispatch_child -> vim maintenance-enter."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": [
                {"vm": "vm-a", "cluster": "c-1"},
                {"vm": "vm-b", "cluster": "c-2"},
            ],
        },
        vmomi={
            "/HostSystem/host-1/EnterMaintenanceMode_Task": _task_moref("task-mm-1"),
        },
    )
    dispatch = _RecordingDispatchChild([_migrated(), _migrated()])
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-1"},
        connector=conn,  # type: ignore[arg-type]
        dispatch_child=dispatch,
    )
    # Recursion routes through dispatch_child (composite->composite), NOT the
    # direct session -- proving the #2248 carve-out.
    assert [c["op_id"] for c in dispatch.calls] == [
        "vmware.composite.vm.migrate",
        "vmware.composite.vm.migrate",
    ]
    assert dispatch.calls[0]["params"] == {"vm": "vm-a", "cluster": "c-1"}
    assert dispatch.calls[1]["params"] == {"vm": "vm-b", "cluster": "c-2"}
    assert all(c["connector_id"] == "vmware-rest-9.0" for c in dispatch.calls)
    # The listing read is the only REST call; maintenance-enter is vim
    # (#2970) -- the write POST plus its Task.info poll.
    assert [(c["method"], c["path"]) for c in conn.calls] == [("GET", "/api/vcenter/vm")]
    enter_calls = [(p, b) for p, b in conn.vmomi_calls if p.endswith("/EnterMaintenanceMode_Task")]
    # ``EnterMaintenanceModeRequestType.timeout`` is required (int).
    assert enter_calls == [("/HostSystem/host-1/EnterMaintenanceMode_Task", {"timeout": 0})]
    # Only the maintenance-enter write was gated (the recursion self-gates).
    assert gate.gated_op_ids == ["POST:/HostSystem/{moId}/EnterMaintenanceMode_Task"]
    assert out["status"] == "evacuated"
    assert out["maintenance_entered"] is True
    assert out["migrated_vms"] == ["vm-a", "vm-b"]


@pytest.mark.asyncio
async def test_host_evacuate_default_aborts_on_migrate_failure(gate: _GateRecorder) -> None:
    """tolerate_partial_failure=False -> a vm.migrate failure aborts before maintenance."""
    conn = _RecordingConnector({"/api/vcenter/vm": [{"vm": "vm-x", "cluster": "c-1"}]})
    dispatch = _RecordingDispatchChild([_no_rec()])
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-3"},
        connector=conn,  # type: ignore[arg-type]
        dispatch_child=dispatch,
    )
    assert out["status"] == "aborted"
    assert out["maintenance_entered"] is False
    assert len(out["failed_vms"]) == 1
    # Maintenance-enter never fired -> the only direct call was the listing GET.
    assert [c["method"] for c in conn.calls] == ["GET"]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_host_evacuate_tolerate_partial_still_enters_maintenance(gate: _GateRecorder) -> None:
    """tolerate_partial_failure=True -> maintenance enters even with VM failures."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": [
                {"vm": "vm-a", "cluster": "c-1"},
                {"vm": "vm-b", "cluster": "c-1"},
            ],
        },
        vmomi={
            "/HostSystem/host-2/EnterMaintenanceMode_Task": _task_moref("task-mm-2"),
        },
    )
    dispatch = _RecordingDispatchChild([_migrated(), _no_rec()])
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-2", "tolerate_partial_failure": True},
        connector=conn,  # type: ignore[arg-type]
        dispatch_child=dispatch,
    )
    assert out["status"] == "partial"
    assert out["maintenance_entered"] is True
    assert out["migrated_vms"] == ["vm-a"]
    assert len(out["failed_vms"]) == 1


# ===========================================================================
# host.detach_from_vds
# ===========================================================================


@pytest.mark.asyncio
async def test_host_detach_from_vds_happy_path(gate: _GateRecorder) -> None:
    """Portgroup GET + VM GET + per-NIC repoint + vim DVS reconfigure; status=detached."""
    conn = _RecordingConnector(
        {
            # #1602 fix: distributed portgroups are listed via the generic
            # /vcenter/network resource (no dedicated portgroup path).
            "/api/vcenter/network": [],
            "/api/vcenter/vm": [{"vm": "vm-1"}, {"vm": "vm-2"}],
            "/api/vcenter/vm/vm-1/hardware/ethernet": [{"nic": "4000"}],
            "/api/vcenter/vm/vm-1/hardware/ethernet/4000": {},
            "/api/vcenter/vm/vm-2/hardware/ethernet": [{"nic": "4000"}, {"nic": "4001"}],
            "/api/vcenter/vm/vm-2/hardware/ethernet/4000": {},
            "/api/vcenter/vm/vm-2/hardware/ethernet/4001": {},
        },
        vmomi={
            "DistributedVirtualSwitch": _retrieve_result(
                "DistributedVirtualSwitch", "dvs-1", "config.configVersion", "42"
            ),
            "/DistributedVirtualSwitch/dvs-1/ReconfigureDvs_Task": _task_moref("task-dvs-1"),
        },
    )
    out = await host_detach_from_vds_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-9", "dvs": "dvs-1", "fallback_network": "standard-net"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "detached"
    assert out["vms_migrated"] == ["vm-1", "vm-2"]
    # Per-NIC repoint (#2970): adapter listing + per-adapter PATCH with the
    # standard-portgroup backing spec.
    nic_patches = [c for c in conn.calls if c["method"] == "PATCH"]
    assert [c["path"] for c in nic_patches] == [
        "/api/vcenter/vm/vm-1/hardware/ethernet/4000",
        "/api/vcenter/vm/vm-2/hardware/ethernet/4000",
        "/api/vcenter/vm/vm-2/hardware/ethernet/4001",
    ]
    assert nic_patches[0]["body"] == {
        "backing": {"type": "STANDARD_PORTGROUP", "network": "standard-net"}
    }
    # The DVS detach is the vim ReconfigureDvs_Task: configVersion echoed,
    # host member spec with operation=remove.
    reconfig = [(p, b) for p, b in conn.vmomi_calls if p.endswith("/ReconfigureDvs_Task")]
    assert reconfig == [
        (
            "/DistributedVirtualSwitch/dvs-1/ReconfigureDvs_Task",
            {
                "spec": {
                    "configVersion": "42",
                    "host": [
                        {
                            "operation": "remove",
                            "host": {"type": "HostSystem", "value": "host-9"},
                        }
                    ],
                }
            },
        )
    ]
    # 3 NIC writes + 1 vim DVS reconfigure gated; the reads were not.
    assert gate.gated_op_ids == [
        "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
        "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
        "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}",
        "POST:/DistributedVirtualSwitch/{moId}/ReconfigureDvs_Task",
    ]


@pytest.mark.asyncio
async def test_host_detach_from_vds_incomplete_on_nic_failure(gate: _GateRecorder) -> None:
    """A NIC migration transport error -> status=incomplete; DVS reconfigure skipped."""
    conn = _RecordingConnector(
        [
            [],  # portgroup GET
            [{"vm": "vm-1"}, {"vm": "vm-2"}],  # VM GET
            [{"nic": "4000"}],  # vm-1 adapter listing
            {},  # vm-1 nic 4000 ok
            [{"nic": "4000"}],  # vm-2 adapter listing
            _http_error(409, "https://vc/api/vcenter/vm/vm-2/hardware/ethernet/4000"),
        ]
    )
    out = await host_detach_from_vds_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-9", "dvs": "dvs-1", "fallback_network": "std"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "incomplete"
    assert out["vms_migrated"] == ["vm-1"]
    assert len(out["vm_migration_failures"]) == 1
    # No vim DVS reconfigure fired -- the detach was skipped.
    assert conn.vmomi_calls == []


# ===========================================================================
# cluster.patch
# ===========================================================================


@pytest.mark.asyncio
async def test_cluster_patch_happy_path(gate: _GateRecorder) -> None:
    """Per-host: vim maintenance-enter -> vLCM apply (cis poll) -> vim exit; completed."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/host": [{"host": "h1"}, {"host": "h2"}],
            "/api/esx/settings/hosts/h1/software?action=apply&vmw-task=true": "task-apply-1",
            "/api/cis/tasks/task-apply-1": {"status": "SUCCEEDED"},
            "/api/esx/settings/hosts/h2/software?action=apply&vmw-task=true": "task-apply-2",
            "/api/cis/tasks/task-apply-2": {"status": "SUCCEEDED"},
        },
        vmomi={
            "/HostSystem/h1/EnterMaintenanceMode_Task": _task_moref("t-enter-1"),
            "/HostSystem/h1/ExitMaintenanceMode_Task": _task_moref("t-exit-1"),
            "/HostSystem/h2/EnterMaintenanceMode_Task": _task_moref("t-enter-2"),
            "/HostSystem/h2/ExitMaintenanceMode_Task": _task_moref("t-exit-2"),
        },
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "completed"
    assert out["patched_hosts"] == ["h1", "h2"]
    # The host listing is the cluster-scoped Host_list (#2970 -- there is
    # no per-cluster /vcenter/cluster/{cluster}/host resource); bare filter
    # keys on /api (#2298).
    assert conn.calls[0]["path"] == "/api/vcenter/host"
    assert conn.calls[0]["query"] == {"clusters": ["c-1"]}
    # The vLCM apply carries an empty ApplySpec body (latest commit) and
    # its cis task is polled to SUCCEEDED before maintenance-exit.
    apply_call = next(c for c in conn.calls if "software?action=apply" in c["path"])
    assert apply_call["body"] == {"spec": {}}
    assert any(c["path"] == "/api/cis/tasks/task-apply-1" for c in conn.calls)
    # Maintenance transitions are vim *_Task methods with the required
    # int timeout, polled to terminal.
    maintenance_calls = [(p, b) for p, b in conn.vmomi_calls if "MaintenanceMode_Task" in p]
    assert maintenance_calls == [
        ("/HostSystem/h1/EnterMaintenanceMode_Task", {"timeout": 0}),
        ("/HostSystem/h1/ExitMaintenanceMode_Task", {"timeout": 0}),
        ("/HostSystem/h2/EnterMaintenanceMode_Task", {"timeout": 0}),
        ("/HostSystem/h2/ExitMaintenanceMode_Task", {"timeout": 0}),
    ]
    # 3 writes per host x 2 hosts were gated.
    assert len(gate.calls) == 6
    assert gate.gated_op_ids[:3] == [
        "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task",
        "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true",
        "POST:/HostSystem/{moId}/ExitMaintenanceMode_Task",
    ]


@pytest.mark.asyncio
async def test_cluster_patch_per_host_failure_stops_loop(gate: _GateRecorder) -> None:
    """A per-host apply transport error stops the loop; status=stopped."""
    conn = _RecordingConnector(
        [
            [{"host": "h1"}, {"host": "h2"}, {"host": "h3"}],  # host listing GET
            "task-apply-1",  # h1 apply -> cis task id
            {"status": "SUCCEEDED"},  # h1 cis poll
            _http_error(
                500, "https://vc/api/esx/settings/hosts/h2/software?action=apply&vmw-task=true"
            ),  # h2 apply fails
        ],
        vmomi={
            "/HostSystem/h1/EnterMaintenanceMode_Task": _task_moref("t-enter-1"),
            "/HostSystem/h1/ExitMaintenanceMode_Task": _task_moref("t-exit-1"),
            "/HostSystem/h2/EnterMaintenanceMode_Task": _task_moref("t-enter-2"),
        },
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "stopped"
    assert out["patched_hosts"] == ["h1"]
    assert out["failed_host"] == "h2"
    assert out["remaining_hosts"] == ["h3"]
    assert out["failure_reason"]


@pytest.mark.asyncio
async def test_cluster_patch_failed_cis_task_stops_loop(gate: _GateRecorder) -> None:
    """A FAILED vLCM apply cis task stops the loop before maintenance-exit."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/host": [{"host": "h1"}, {"host": "h2"}],
            "/api/esx/settings/hosts/h1/software?action=apply&vmw-task=true": "task-apply-1",
            "/api/cis/tasks/task-apply-1": {"status": "FAILED", "error": "image scan failed"},
        },
        vmomi={
            "/HostSystem/h1/EnterMaintenanceMode_Task": _task_moref("t-enter-1"),
        },
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "stopped"
    assert out["failed_host"] == "h1"
    assert "image scan failed" in out["failure_reason"]
    # Maintenance-exit never fired for h1 -- the loop stopped mid-host.
    assert not any(p.endswith("/ExitMaintenanceMode_Task") for p, _ in conn.vmomi_calls)


# ===========================================================================
# Governance contract across every write composite
# ===========================================================================


@pytest.mark.asyncio
async def test_reads_are_never_gated_only_writes(gate: _GateRecorder) -> None:
    """Resolution GETs never hit the governance seam; only mutating sub-ops do.

    Load-bearing for the two-world governance model: a read composite sub-op
    stays un-gated (it was ``safe`` under ``dispatch_child`` too), while every
    write is gated with the declared dangerous / no-approval posture.
    """
    conn = _RecordingConnector(
        {"/api/vcenter/vm/vm-1?action=relocate": {}},
        vmomi={"ClusterComputeResource": _drs_recommendation_vmomi("vm-1", "host-A")},
    )
    await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cluster": "c-9"},
        connector=conn,  # type: ignore[arg-type]
    )
    # The vim DRS-recommendation read fired but was not gated; only the
    # relocate write was gated.
    assert [p for p, _ in conn.vmomi_calls] == [
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    ]
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}?action=relocate"]


# ===========================================================================
# vm.disk.grow (mutating VI-JSON — keystone 2, #2893)
# ===========================================================================


_TEN_GIB = 10 * 1024**3
_TWENTY_GIB = 20 * 1024**3


def _virtual_disk(key: int = 2000, capacity_bytes: int | None = _TEN_GIB) -> dict[str, Any]:
    """A ``VirtualDisk`` device as ``config.hardware.device`` returns it."""
    device: dict[str, Any] = {
        "_typeName": "VirtualDisk",
        "key": key,
        "controllerKey": 1000,
        "unitNumber": 0,
        "backing": {
            "_typeName": "VirtualDiskFlatVer2BackingInfo",
            "fileName": "[datastore1] web-01/web-01.vmdk",
            "diskMode": "persistent",
        },
    }
    if capacity_bytes is not None:
        device["capacityInBytes"] = capacity_bytes
    return device


class _DiskGrowConnector:
    """Recording connector double serving the disk-grow VI-JSON sub-ops.

    ``vm.disk.grow`` reads ``config.hardware.device`` (vmomi
    ``RetrievePropertiesEx``), writes ``ReconfigVM_Task``, then polls
    ``Task.info`` (again ``RetrievePropertiesEx``). The config read and the
    Task poll share the ``RetrievePropertiesEx`` path, so this double
    distinguishes them by the request body's ``specSet`` object type
    (``VirtualMachine`` vs ``Task``) -- exactly the seam a real vCenter
    keys them by.
    """

    def __init__(
        self,
        *,
        devices: list[Any],
        task_state: str = "success",
        task_error: str | None = None,
        reconfig_task: str = "task-grow-1",
    ) -> None:
        self.devices = devices
        self.task_state = task_state
        self.task_error = task_error
        self.reconfig_task = reconfig_task
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/ReconfigVM_Task"):
            return {"type": "Task", "value": self.reconfig_task}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": "vm-1"},
                        "propSet": [{"name": "config.hardware.device", "val": self.devices}],
                    }
                ]
            }
        if spec_type == "Task":
            info: dict[str, Any] = {"state": self.task_state}
            if self.task_error is not None:
                info["error"] = {"localizedMessage": self.task_error}
            return {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": self.reconfig_task},
                        "propSet": [{"name": "info", "val": info}],
                    }
                ]
            }
        raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")

    @property
    def reconfig_bodies(self) -> list[Any]:
        return [body for path, body in self.vmomi_calls if path.endswith("/ReconfigVM_Task")]


async def test_vm_disk_grow_happy_path_edits_capacity_and_polls(gate: _GateRecorder) -> None:
    """Grow: read device -> gated ReconfigVM_Task edit -> poll to success -> status=grown."""
    conn = _DiskGrowConnector(devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)])
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "grown"
    assert out["from_capacity_bytes"] == _TEN_GIB
    assert out["to_capacity_bytes"] == _TWENTY_GIB
    assert out["delta_bytes"] == _TWENTY_GIB - _TEN_GIB
    assert out["task"] == "task-grow-1"

    # The single mutating sub-op was gated with its vi-json governance op_id
    # + the logical params that name the entity the write touches.
    assert gate.gated_op_ids == ["POST:/VirtualMachine/{moId}/ReconfigVM_Task"]
    gated = gate.calls[0]
    assert gated["safety_level"] == "dangerous"
    assert gated["requires_approval"] is False
    assert gated["params"] == {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB}

    # The ReconfigVM_Task body is a single-device edit raising capacityInBytes
    # on the matched device key, with the full VirtualDisk preserved.
    assert len(conn.reconfig_bodies) == 1
    change = conn.reconfig_bodies[0]["spec"]["deviceChange"][0]
    assert change["operation"] == "edit"
    assert change["device"]["key"] == 2000
    assert change["device"]["capacityInBytes"] == _TWENTY_GIB
    assert change["device"]["_typeName"] == "VirtualDisk"
    # The backing (fully-specified device) is carried through unchanged.
    assert change["device"]["backing"]["fileName"] == "[datastore1] web-01/web-01.vmdk"


async def test_vm_disk_grow_refuses_shrink_before_any_write(gate: _GateRecorder) -> None:
    """A request <= the current capacity is refused; no ReconfigVM_Task, no gate."""
    conn = _DiskGrowConnector(devices=[_virtual_disk(key=2000, capacity_bytes=_TWENTY_GIB)])
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TEN_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "invalid_shrink"
    assert out["from_capacity_bytes"] == _TWENTY_GIB
    assert out["to_capacity_bytes"] == _TEN_GIB
    assert out["delta_bytes"] == _TEN_GIB - _TWENTY_GIB
    assert out["task"] is None
    # Only the config read fired; the write was never attempted, never gated.
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_vm_disk_grow_refuses_a_no_op_equal_capacity(gate: _GateRecorder) -> None:
    """Growing to the exact current size is a no-op -> refused (grow-only contract)."""
    conn = _DiskGrowConnector(devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)])
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TEN_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "invalid_shrink"
    assert conn.reconfig_bodies == []


async def test_vm_disk_grow_disk_not_found(gate: _GateRecorder) -> None:
    """No VirtualDisk with the requested key -> disk_not_found; no write."""
    conn = _DiskGrowConnector(devices=[_virtual_disk(key=2001, capacity_bytes=_TEN_GIB)])
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "disk_not_found"
    assert out["from_capacity_bytes"] is None
    assert out["delta_bytes"] is None
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_vm_disk_grow_gate_short_circuits_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on the ReconfigVM_Task write returns verbatim; no write fires."""
    gate = _install_gate(
        monkeypatch,
        _GateRecorder(
            gate_for={
                "POST:/VirtualMachine/{moId}/ReconfigVM_Task": _awaiting(
                    "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
                )
            }
        ),
    )
    conn = _DiskGrowConnector(devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)])
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
    # The config read fired; the ReconfigVM_Task write was gated off the wire.
    assert conn.reconfig_bodies == []
    assert gate.gated_op_ids == ["POST:/VirtualMachine/{moId}/ReconfigVM_Task"]


async def test_vm_disk_grow_task_fault_raises(gate: _GateRecorder) -> None:
    """A terminal task error raises (the dispatcher wraps it connector_error)."""
    conn = _DiskGrowConnector(
        devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)],
        task_state="error",
        task_error="Insufficient disk space on datastore.",
    )
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        await vm_disk_grow_composite(
            operator=_make_operator(),
            target=object(),
            params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
            connector=conn,  # type: ignore[arg-type]
        )


async def test_vm_disk_grow_poll_timeout_returns_timeout_status(
    monkeypatch: pytest.MonkeyPatch, gate: _GateRecorder
) -> None:
    """A poll that never sees a terminal state returns status=timeout with the task id."""
    # Zero the poll bound so a still-``running`` task times out on the first read.
    monkeypatch.setattr(_write, "_DISK_GROW_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _DiskGrowConnector(
        devices=[_virtual_disk(key=2000, capacity_bytes=_TEN_GIB)],
        task_state="running",
    )
    out = await vm_disk_grow_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "timeout"
    assert out["task"] == "task-grow-1"
    assert out["to_capacity_bytes"] == _TWENTY_GIB


# ---------------------------------------------------------------------------
# respx-verified ReconfigVM_Task wire body (through a real connector)
# ---------------------------------------------------------------------------


@dataclass
class _StubTarget:
    """Minimal target the real connector's transport path reads from."""

    name: str = "vc-grow"
    host: str = "vc-grow.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-grow"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


async def _stub_loader(_target: Any, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


def _patch_no_revoke_aclose(connector: VmwareRestConnector) -> None:
    """Skip the session-revoke DELETE at teardown (mirrors the vmomi-mount tests)."""

    async def _aclose() -> None:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()

    connector.aclose = _aclose  # type: ignore[method-assign]


async def test_vm_disk_grow_reconfig_body_reaches_the_wire_respx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respx-verified: the ReconfigVM_Task POST body carries operation:edit + raised
    capacityInBytes on the right device key, mounted on the /sdk/vim25 base.

    Drives the real handler through a real ``VmwareRestConnector`` over an
    httpx (respx) transport, so the assertion is on the actual wire bytes,
    not a recording double. The #2254 gate is auto-executed here (its own
    governance is proven end-to-end in the gate + e2e lanes) so the write
    reaches the wire.
    """

    async def _auto_execute(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _auto_execute)

    base = "https://vc-grow.test.invalid"
    vijson = "/sdk/vim25/8.0.3.0"
    device = _virtual_disk(key=2000, capacity_bytes=_TEN_GIB)
    config_result = {
        "objects": [
            {
                "obj": {"type": "VirtualMachine", "value": "vm-1"},
                "propSet": [{"name": "config.hardware.device", "val": [device]}],
            }
        ]
    }
    task_result = {
        "objects": [
            {
                "obj": {"type": "Task", "value": "task-1"},
                "propSet": [{"name": "info", "val": {"state": "success"}}],
            }
        ]
    }
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=base) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            # config read then Task poll share the RetrievePropertiesEx URL.
            mock.post(f"{vijson}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=[
                    httpx.Response(200, json=config_result),
                    httpx.Response(200, json=task_result),
                ]
            )
            reconfig = mock.post(f"{vijson}/VirtualMachine/vm-1/ReconfigVM_Task").respond(
                200, json={"type": "Task", "value": "task-1"}
            )
            out = await vm_disk_grow_composite(
                operator=_make_operator(),
                target=_StubTarget(),
                params={"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
                connector=connector,
            )
        assert isinstance(out, dict)
        assert out["status"] == "grown"
        assert reconfig.called
        body = json.loads(reconfig.calls[0].request.content)
        change = body["spec"]["deviceChange"][0]
        assert change["operation"] == "edit"
        assert change["device"]["_typeName"] == "VirtualDisk"
        assert change["device"]["key"] == 2000
        assert change["device"]["capacityInBytes"] == _TWENTY_GIB
    finally:
        await connector.aclose()


# ===========================================================================
# vm.clone_from_template (folder-template CloneVM_Task — Task D, #2894)
# ===========================================================================


_CLONE_OP_ID = "POST:/VirtualMachine/{moId}/CloneVM_Task"
_TEMPLATE_ROWS = [{"vm": "vm-42", "name": "ubuntu-2404-template"}]
# A minimal-but-valid vim CustomizationSpec shape (identity + globalIPSettings
# are the required fields per vi-json.yaml); the handler embeds whatever
# GetCustomizationSpec returns verbatim, so the exact contents are opaque.
_RESOLVED_GOSC_SPEC = {
    "_typeName": "CustomizationSpec",
    "identity": {"_typeName": "CustomizationLinuxPrep", "hostName": {"name": "web-01"}},
    "globalIPSettings": {"_typeName": "CustomizationGlobalIPSettings"},
}


class _CloneFromTemplateConnector:
    """Recording double serving the clone-from-template REST + VI-JSON sub-ops.

    ``vm.clone_from_template`` resolves the template name (REST
    ``GET:/vcenter/vm``), asserts ``config.template`` (vmomi
    ``RetrievePropertiesEx`` on a VirtualMachine), optionally resolves a GOSC
    spec (``GetCustomizationSpec``), writes ``CloneVM_Task``, then polls
    ``Task.info`` (``RetrievePropertiesEx`` on a Task). The two
    ``RetrievePropertiesEx`` reads are keyed apart by the request body's
    ``specSet`` object type — exactly the seam a real vCenter keys them by.
    """

    _MOUNT = "/api"

    def __init__(
        self,
        *,
        template_rows: list[dict[str, Any]] | None = None,
        is_template: bool = True,
        task_state: str = "success",
        task_error: str | None = None,
        task_result: Any = None,
        customization_spec: dict[str, Any] | None = None,
        clone_task: str = "task-clone-1",
    ) -> None:
        self.template_rows = _TEMPLATE_ROWS if template_rows is None else template_rows
        self.is_template = is_template
        self.task_state = task_state
        self.task_error = task_error
        self.task_result = (
            task_result
            if task_result is not None
            else {
                "_typeName": "ManagedObjectReference",
                "type": "VirtualMachine",
                "value": "vm-99",
            }
        )
        self.customization_spec = customization_spec
        self.clone_task = clone_task
        self.rest_calls: list[tuple[str, Any]] = []
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    def _spec(self, path: str) -> str:
        return path[len(self._MOUNT) :] if path.startswith(self._MOUNT) else path

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        spec = self._spec(path)
        self.rest_calls.append((spec, params))
        if spec == "/vcenter/vm":
            return {"value": self.template_rows}
        return {"value": {}}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/CloneVM_Task"):
            return {"type": "Task", "value": self.clone_task}
        if path.endswith("/GetCustomizationSpec"):
            return {"spec": self.customization_spec}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "VirtualMachine":
            moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
            return {
                "objects": [
                    {
                        "obj": {"type": "VirtualMachine", "value": moid},
                        "propSet": [{"name": "config.template", "val": self.is_template}],
                    }
                ]
            }
        info: dict[str, Any] = {"state": self.task_state}
        if self.task_error is not None:
            info["error"] = {"localizedMessage": self.task_error}
        if self.task_state == "success":
            info["result"] = self.task_result
        return {
            "objects": [
                {
                    "obj": {"type": "Task", "value": self.clone_task},
                    "propSet": [{"name": "info", "val": info}],
                }
            ]
        }

    @property
    def clone_bodies(self) -> list[Any]:
        return [body for path, body in self.vmomi_calls if path.endswith("/CloneVM_Task")]

    @property
    def get_customization_calls(self) -> list[tuple[str, Any]]:
        return [(p, b) for p, b in self.vmomi_calls if p.endswith("/GetCustomizationSpec")]


def _clone_params(**overrides: Any) -> dict[str, Any]:
    """Schema-valid clone params with the required placement moids filled in."""
    params: dict[str, Any] = {
        "source_template": "ubuntu-2404-template",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
    }
    params.update(overrides)
    return params


async def test_vm_clone_from_template_happy_path_clones_and_polls(gate: _GateRecorder) -> None:
    """Resolve template -> assert template -> gated CloneVM_Task -> poll -> status=cloned."""
    conn = _CloneFromTemplateConnector()
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(power_on=True),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "cloned"
    assert out["source_template"] == "ubuntu-2404-template"
    assert out["source_template_id"] == "vm-42"
    assert out["new_vm_name"] == "web-01"
    assert out["new_vm_id"] == "vm-99"
    assert out["folder"] == "group-v10"
    assert out["task"] == "task-clone-1"
    assert out["customization_spec_name"] is None

    # The template name was resolved via GET:/vcenter/vm and the CloneVM_Task
    # was issued on the resolved template moid.
    assert ("/vcenter/vm", {"names": ["ubuntu-2404-template"]}) in conn.rest_calls
    clone_paths = [p for p, _ in conn.vmomi_calls if p.endswith("/CloneVM_Task")]
    assert clone_paths == ["/VirtualMachine/vm-42/CloneVM_Task"]

    # The single mutating sub-op was gated with the vi-json CloneVM_Task op_id
    # + identity-only params (never the resolved CustomizationSpec).
    assert gate.gated_op_ids == [_CLONE_OP_ID]
    gated = gate.calls[0]
    assert gated["safety_level"] == "dangerous"
    assert gated["requires_approval"] is False
    assert gated["params"] == {
        "source_template": "ubuntu-2404-template",
        "source_template_id": "vm-42",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
        "host": None,
        "power_on": True,
        "customization_spec_name": None,
    }

    # The CloneVM_Task body: folder MoRef + name + CloneSpec (template:false,
    # powerOn, relocate placement pool+datastore, no host pin, no customization).
    assert len(conn.clone_bodies) == 1
    body = conn.clone_bodies[0]
    assert body["folder"] == {"type": "Folder", "value": "group-v10"}
    assert body["name"] == "web-01"
    spec = body["spec"]
    assert spec["template"] is False
    assert spec["powerOn"] is True
    assert spec["location"]["pool"] == {"type": "ResourcePool", "value": "resgroup-8"}
    assert spec["location"]["datastore"] == {"type": "Datastore", "value": "datastore-15"}
    assert "host" not in spec["location"]
    assert "customization" not in spec


async def test_vm_clone_from_template_host_pin_included_when_given(gate: _GateRecorder) -> None:
    """An explicit host moid rides the RelocateSpec as a HostSystem MoRef."""
    conn = _CloneFromTemplateConnector()
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(host="host-19"),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "cloned"
    body = conn.clone_bodies[0]
    assert body["spec"]["location"]["host"] == {"type": "HostSystem", "value": "host-19"}
    assert gate.calls[0]["params"]["host"] == "host-19"


async def test_vm_clone_from_template_with_customization_embeds_resolved_spec(
    gate: _GateRecorder,
) -> None:
    """customization_spec_name -> GetCustomizationSpec -> spec embedded inline (composes #2892).

    Proves acceptance criterion 3: a clone with ``customization_spec_name``
    yields a customized clone in one dispatch — the resolved
    ``CustomizationSpec`` rides the ``CloneSpec.customization`` field, with no
    separate ``vm.customize`` call, and the gate params echo only the spec
    *name* (never the secret-bearing spec contents).
    """
    conn = _CloneFromTemplateConnector(customization_spec=_RESOLVED_GOSC_SPEC)
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(customization_spec_name="linux-web-gosc"),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "cloned"
    assert out["customization_spec_name"] == "linux-web-gosc"

    # The GOSC spec was resolved by name on the CustomizationSpecManager singleton.
    assert conn.get_customization_calls == [
        (
            "/CustomizationSpecManager/CustomizationSpecManager/GetCustomizationSpec",
            {"name": "linux-web-gosc"},
        )
    ]
    # The resolved spec is embedded verbatim into CloneSpec.customization.
    assert conn.clone_bodies[0]["spec"]["customization"] == _RESOLVED_GOSC_SPEC
    # The gate params carry the spec name, NOT the resolved secret-bearing spec.
    assert gate.calls[0]["params"]["customization_spec_name"] == "linux-web-gosc"
    assert "customization" not in gate.calls[0]["params"]


async def test_vm_clone_from_template_custom_manager_moid_overrides_default(
    gate: _GateRecorder,
) -> None:
    """An operator-supplied customization_spec_manager_moid is used for the resolve."""
    conn = _CloneFromTemplateConnector(customization_spec=_RESOLVED_GOSC_SPEC)
    await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(
            customization_spec_name="linux-web-gosc",
            customization_spec_manager_moid="custom-spec-mgr",
        ),
        connector=conn,  # type: ignore[arg-type]
    )
    assert conn.get_customization_calls[0][0] == (
        "/CustomizationSpecManager/custom-spec-mgr/GetCustomizationSpec"
    )


async def test_vm_clone_from_template_refuses_non_template_source(gate: _GateRecorder) -> None:
    """A resolved-but-non-template source is refused before any clone; no gate."""
    conn = _CloneFromTemplateConnector(is_template=False)
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "not_a_template"
    assert out["source_template_id"] == "vm-42"
    assert out["task"] is None
    # The config.template read fired; no CloneVM_Task, never gated.
    assert conn.clone_bodies == []
    assert gate.calls == []


async def test_vm_clone_from_template_template_not_found(gate: _GateRecorder) -> None:
    """An unresolvable source name -> template_not_found; no template read, no gate."""
    conn = _CloneFromTemplateConnector(template_rows=[])
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "template_not_found"
    assert out["source_template_id"] is None
    assert conn.clone_bodies == []
    assert conn.vmomi_calls == []
    assert gate.calls == []


async def test_vm_clone_from_template_ambiguous_template(gate: _GateRecorder) -> None:
    """A source name matching >1 VM -> ambiguous_template with candidate moids; no gate."""
    conn = _CloneFromTemplateConnector(
        template_rows=[{"vm": "vm-42", "name": "dup"}, {"vm": "vm-43", "name": "dup"}]
    )
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(source_template="dup"),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "ambiguous_template"
    assert out["candidates"] == ["vm-42", "vm-43"]
    assert out["source_template_id"] is None
    assert conn.clone_bodies == []
    assert gate.calls == []


async def test_vm_clone_from_template_gate_short_circuits_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on the CloneVM_Task write returns verbatim; no clone fires."""
    gate = _install_gate(
        monkeypatch,
        _GateRecorder(gate_for={_CLONE_OP_ID: _awaiting(_CLONE_OP_ID)}),
    )
    conn = _CloneFromTemplateConnector()
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == _CLONE_OP_ID
    # The resolve + template reads fired; the CloneVM_Task write was gated off the wire.
    assert conn.clone_bodies == []
    assert gate.gated_op_ids == [_CLONE_OP_ID]


async def test_vm_clone_from_template_task_fault_raises(gate: _GateRecorder) -> None:
    """A terminal CloneVM_Task error raises (the dispatcher wraps it connector_error)."""
    conn = _CloneFromTemplateConnector(
        task_state="error",
        task_error="The name 'web-01' already exists.",
    )
    with pytest.raises(RuntimeError, match="already exists"):
        await vm_clone_from_template_composite(
            operator=_make_operator(),
            target=object(),
            params=_clone_params(),
            connector=conn,  # type: ignore[arg-type]
        )


async def test_vm_clone_from_template_poll_timeout_returns_timeout_status(
    monkeypatch: pytest.MonkeyPatch, gate: _GateRecorder
) -> None:
    """A poll that never sees a terminal state returns status=timeout with the task id."""
    monkeypatch.setattr(_write, "_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _CloneFromTemplateConnector(task_state="running")
    out = await vm_clone_from_template_composite(
        operator=_make_operator(),
        target=object(),
        params=_clone_params(),
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "timeout"
    assert out["task"] == "task-clone-1"
    assert out["new_vm_id"] is None


# ---------------------------------------------------------------------------
# respx-verified CloneVM_Task wire body (through a real connector)
# ---------------------------------------------------------------------------


async def test_vm_clone_from_template_clone_body_reaches_the_wire_respx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respx-verified: the CloneVM_Task POST body carries template:false + the
    relocate placement, mounted on the /sdk/vim25 base.

    Drives the real handler through a real ``VmwareRestConnector`` over an
    httpx (respx) transport, so the assertion is on the actual wire bytes, not
    a recording double. The #2254 gate is auto-executed here (its governance
    is proven end-to-end in the gate + e2e lanes) so the write reaches the wire.
    """

    async def _auto_execute(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _auto_execute)

    base = "https://vc-clone.test.invalid"
    vijson = "/sdk/vim25/8.0.3.0"
    template_check = {
        "objects": [
            {
                "obj": {"type": "VirtualMachine", "value": "vm-42"},
                "propSet": [{"name": "config.template", "val": True}],
            }
        ]
    }
    task_poll = {
        "objects": [
            {
                "obj": {"type": "Task", "value": "task-1"},
                "propSet": [
                    {
                        "name": "info",
                        "val": {
                            "state": "success",
                            "result": {"type": "VirtualMachine", "value": "vm-99"},
                        },
                    }
                ],
            }
        ]
    }
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=base) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.get("/api/vcenter/vm").respond(200, json={"value": _TEMPLATE_ROWS})
            # config.template read then Task poll share the RetrievePropertiesEx URL.
            mock.post(f"{vijson}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=[
                    httpx.Response(200, json=template_check),
                    httpx.Response(200, json=task_poll),
                ]
            )
            clone = mock.post(f"{vijson}/VirtualMachine/vm-42/CloneVM_Task").respond(
                200, json={"type": "Task", "value": "task-1"}
            )
            out = await vm_clone_from_template_composite(
                operator=_make_operator(),
                target=_StubTarget(name="vc-clone", host="vc-clone.test.invalid"),
                params=_clone_params(power_on=True),
                connector=connector,
            )
        assert isinstance(out, dict)
        assert out["status"] == "cloned"
        assert out["new_vm_id"] == "vm-99"
        assert clone.called
        body = json.loads(clone.calls[0].request.content)
        assert body["folder"] == {"type": "Folder", "value": "group-v10"}
        assert body["name"] == "web-01"
        assert body["spec"]["template"] is False
        assert body["spec"]["powerOn"] is True
        assert body["spec"]["location"]["pool"] == {"type": "ResourcePool", "value": "resgroup-8"}
        assert body["spec"]["location"]["datastore"] == {
            "type": "Datastore",
            "value": "datastore-15",
        }
    finally:
        await connector.aclose()


# ===========================================================================
# cluster.drs_rule.create (vim ClusterComputeResource reconfigure — #2895)
# ===========================================================================


def _vm_row(vm: str, name: str) -> dict[str, Any]:
    """A VM listing row as ``GET:/vcenter/vm`` returns it (moid + display name)."""
    return {"vm": vm, "name": name, "power_state": "POWERED_ON"}


class _DrsRuleConnector:
    """Recording double for the drs_rule.create REST reads + vim writes.

    Serves the VM-name resolution (``GET:/vcenter/vm``), the existing-rules
    collision read + the ``ReconfigureComputeResource_Task`` write + the Task
    poll (the two ``RetrievePropertiesEx`` reads keyed apart by the request
    body's ``specSet`` object type — ``ClusterComputeResource`` vs ``Task``).
    """

    _MOUNT = "/api"

    def __init__(
        self,
        *,
        vms: list[dict[str, Any]] | None = None,
        existing_rules: list[dict[str, Any]] | None = None,
        task_state: str = "success",
        task_error: str | None = None,
        reconfig_task: str = "task-rule-1",
    ) -> None:
        self.vms = [_vm_row("vm-1", "web-01"), _vm_row("vm-2", "web-02")] if vms is None else vms
        self.existing_rules = existing_rules or []
        self.task_state = task_state
        self.task_error = task_error
        self.reconfig_task = reconfig_task
        self.rest_calls: list[tuple[str, Any]] = []
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.rest_calls.append((path, params))
        return {"value": self.vms}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/ReconfigureComputeResource_Task"):
            return {"type": "Task", "value": self.reconfig_task}
        spec_type = json["specSet"][0]["propSet"][0]["type"]
        if spec_type == "ClusterComputeResource":
            return {
                "objects": [
                    {
                        "obj": {"type": "ClusterComputeResource", "value": "domain-c1"},
                        "propSet": [{"name": "configurationEx.rule", "val": self.existing_rules}],
                    }
                ]
            }
        if spec_type == "Task":
            info: dict[str, Any] = {"state": self.task_state}
            if self.task_error is not None:
                info["error"] = {"localizedMessage": self.task_error}
            return {
                "objects": [
                    {
                        "obj": {"type": "Task", "value": self.reconfig_task},
                        "propSet": [{"name": "info", "val": info}],
                    }
                ]
            }
        raise AssertionError(f"unexpected RetrievePropertiesEx type {spec_type!r}")

    @property
    def reconfig_bodies(self) -> list[Any]:
        return [
            body
            for path, body in self.vmomi_calls
            if path.endswith("/ReconfigureComputeResource_Task")
        ]


async def test_drs_rule_create_happy_path_adds_anti_affinity_and_polls(gate: _GateRecorder) -> None:
    """Anti-affinity: resolve VMs -> gated reconfigure add -> poll success -> status=created."""
    conn = _DrsRuleConnector()
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "created"
    assert out["task"] == "task-rule-1"
    assert out["rule_type"] == "anti_affinity"
    assert out["resolved_vms"] == [
        {"vm": "vm-1", "name": "web-01"},
        {"vm": "vm-2", "name": "web-02"},
    ]

    # The single mutating sub-op was gated with its vi-json governance op_id +
    # logical params (resolved moids name the entities the write touches).
    assert gate.gated_op_ids == [
        "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task"
    ]
    gated = gate.calls[0]
    assert gated["safety_level"] == "dangerous"
    assert gated["requires_approval"] is False
    assert gated["params"]["vms"] == ["vm-1", "vm-2"]

    # The reconfigure body is a single-rule add with modify:true + MoRefs.
    assert len(conn.reconfig_bodies) == 1
    body = conn.reconfig_bodies[0]
    assert body["modify"] is True
    assert body["spec"]["_typeName"] == "ClusterConfigSpecEx"
    rules_spec = body["spec"]["rulesSpec"]
    assert len(rules_spec) == 1
    assert rules_spec[0]["operation"] == "add"
    info = rules_spec[0]["info"]
    assert info["_typeName"] == "ClusterAntiAffinityRuleSpec"
    assert info["name"] == "keep-apart"
    assert info["enabled"] is True
    assert info["vm"] == [
        {"type": "VirtualMachine", "value": "vm-1"},
        {"type": "VirtualMachine", "value": "vm-2"},
    ]


async def test_drs_rule_create_affinity_uses_affinity_type(gate: _GateRecorder) -> None:
    """rule_type=affinity tags the rule info ``ClusterAffinityRuleSpec``."""
    conn = _DrsRuleConnector()
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-together",
            "rule_type": "affinity",
            "vms": ["web-01", "web-02"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "created"
    info = conn.reconfig_bodies[0]["spec"]["rulesSpec"][0]["info"]
    assert info["_typeName"] == "ClusterAffinityRuleSpec"


async def test_drs_rule_create_name_collision_returns_structured_status(
    gate: _GateRecorder,
) -> None:
    """An existing rule with the same name -> status=rule_exists; no write, no gate."""
    conn = _DrsRuleConnector(existing_rules=[{"name": "keep-apart", "key": 1}])
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "rule_exists"
    assert out["task"] is None
    # The reconfigure never fired, and the gate was never consulted.
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_drs_rule_create_insufficient_vms_refused_before_write(gate: _GateRecorder) -> None:
    """Fewer than two VMs resolve -> status=insufficient_vms; no write, no gate."""
    conn = _DrsRuleConnector(vms=[_vm_row("vm-1", "web-01")])
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "ghost"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "insufficient_vms"
    assert out["resolved_vms"] == [{"vm": "vm-1", "name": "web-01"}]
    assert conn.reconfig_bodies == []
    assert gate.calls == []


async def test_drs_rule_create_gate_short_circuits_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on the reconfigure returns verbatim; no write fires."""
    op_id = "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task"
    _install_gate(monkeypatch, _GateRecorder(gate_for={op_id: _awaiting(op_id)}))
    conn = _DrsRuleConnector()
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == op_id
    assert conn.reconfig_bodies == []


async def test_drs_rule_create_task_fault_raises(gate: _GateRecorder) -> None:
    """A terminal task error raises (the dispatcher wraps it connector_error)."""
    conn = _DrsRuleConnector(task_state="error", task_error="Cluster rule conflict.")
    with pytest.raises(RuntimeError, match="Cluster rule conflict"):
        await cluster_drs_rule_create_composite(
            operator=_make_operator(),
            target=object(),
            params={
                "cluster": "domain-c1",
                "rule_name": "keep-apart",
                "rule_type": "anti_affinity",
                "vms": ["web-01", "web-02"],
            },
            connector=conn,  # type: ignore[arg-type]
        )


async def test_drs_rule_create_poll_timeout_returns_timeout_status(
    monkeypatch: pytest.MonkeyPatch, gate: _GateRecorder
) -> None:
    """A poll that never sees a terminal state returns status=timeout with the task id."""
    monkeypatch.setattr(_write, "_DRS_RULE_TASK_TIMEOUT_SECONDS", 0.0)
    conn = _DrsRuleConnector(task_state="running")
    out = await cluster_drs_rule_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "timeout"
    assert out["task"] == "task-rule-1"


async def test_drs_rule_create_reconfig_body_reaches_the_wire_respx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respx-verified: the ReconfigureComputeResource_Task POST body carries
    modify:true + a single ClusterRuleSpec add with the resolved VM MoRefs,
    mounted on the /sdk/vim25 base.
    """

    async def _auto_execute(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _auto_execute)

    base = "https://vc-grow.test.invalid"
    vijson = "/sdk/vim25/8.0.3.0"
    vm_list = {"value": [_vm_row("vm-1", "web-01"), _vm_row("vm-2", "web-02")]}
    rules_result = {
        "objects": [
            {
                "obj": {"type": "ClusterComputeResource", "value": "domain-c1"},
                "propSet": [{"name": "configurationEx.rule", "val": []}],
            }
        ]
    }
    task_result = {
        "objects": [
            {
                "obj": {"type": "Task", "value": "task-1"},
                "propSet": [{"name": "info", "val": {"state": "success"}}],
            }
        ]
    }
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=base) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.get("/api/vcenter/vm").respond(200, json=vm_list)
            # collision read then Task poll share the RetrievePropertiesEx URL.
            mock.post(f"{vijson}/PropertyCollector/propertyCollector/RetrievePropertiesEx").mock(
                side_effect=[
                    httpx.Response(200, json=rules_result),
                    httpx.Response(200, json=task_result),
                ]
            )
            reconfig = mock.post(
                f"{vijson}/ClusterComputeResource/domain-c1/ReconfigureComputeResource_Task"
            ).respond(200, json={"type": "Task", "value": "task-1"})
            out = await cluster_drs_rule_create_composite(
                operator=_make_operator(),
                target=_StubTarget(),
                params={
                    "cluster": "domain-c1",
                    "rule_name": "keep-apart",
                    "rule_type": "anti_affinity",
                    "vms": ["web-01", "web-02"],
                },
                connector=connector,
            )
        assert isinstance(out, dict)
        assert out["status"] == "created"
        assert reconfig.called
        body = json.loads(reconfig.calls[0].request.content)
        assert body["modify"] is True
        assert body["spec"]["_typeName"] == "ClusterConfigSpecEx"
        assert len(body["spec"]["rulesSpec"]) == 1
        assert body["spec"]["rulesSpec"][0]["operation"] == "add"
        info = body["spec"]["rulesSpec"][0]["info"]
        assert info["_typeName"] == "ClusterAntiAffinityRuleSpec"
        assert info["vm"] == [
            {"type": "VirtualMachine", "value": "vm-1"},
            {"type": "VirtualMachine", "value": "vm-2"},
        ]
    finally:
        await connector.aclose()


# ===========================================================================
# folder.create (synchronous vim Folder.CreateFolder — #2895)
# ===========================================================================


class _FolderCreateConnector:
    """Recording double for folder.create: parent REST read + vim CreateFolder.

    Serves the parent-folder resolution (``GET:/vcenter/folder``) and the
    synchronous ``Folder.CreateFolder`` vim POST — which returns the new
    Folder MoRef directly, so **no** ``RetrievePropertiesEx`` poll ever fires.
    """

    _MOUNT = "/api"

    def __init__(
        self,
        *,
        parents: list[dict[str, Any]] | None = None,
        new_folder_moid: str = "group-v99",
    ) -> None:
        self.parents = [{"folder": "group-v1", "name": "prod"}] if parents is None else parents
        self.new_folder_moid = new_folder_moid
        self.rest_calls: list[tuple[str, Any]] = []
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        self.rest_calls.append((path, params))
        return {"value": self.parents}

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        return {"type": "Folder", "value": self.new_folder_moid}


async def test_folder_create_happy_path_is_synchronous_no_poll(gate: _GateRecorder) -> None:
    """Resolve parent -> gated CreateFolder -> new folder MoRef returned; NO task poll."""
    conn = _FolderCreateConnector()
    out = await folder_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"parent_folder": "prod", "folder_name": "cluster-nodes"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "created"
    assert out["folder"] == "group-v99"
    assert out["parent_folder"] == "prod"
    assert out["parent_folder_id"] == "group-v1"
    assert out["new_folder_name"] == "cluster-nodes"

    # Gated with the CreateFolder governance op_id + resolved-parent params.
    assert gate.gated_op_ids == ["POST:/Folder/{moId}/CreateFolder"]
    assert gate.calls[0]["params"] == {"parent_folder": "group-v1", "folder_name": "cluster-nodes"}

    # Exactly one vmomi call — the CreateFolder itself. No RetrievePropertiesEx
    # poll: CreateFolder is synchronous and returns the folder MoRef directly.
    assert len(conn.vmomi_calls) == 1
    path, body = conn.vmomi_calls[0]
    assert path == "/Folder/group-v1/CreateFolder"
    assert body == {"name": "cluster-nodes"}


async def test_folder_create_parent_not_found_refused_before_write(gate: _GateRecorder) -> None:
    """No parent match -> status=parent_not_found; no write, no gate."""
    conn = _FolderCreateConnector(parents=[])
    out = await folder_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"parent_folder": "ghost", "folder_name": "cluster-nodes"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "parent_not_found"
    assert out["folder"] is None
    assert conn.vmomi_calls == []
    assert gate.calls == []


async def test_folder_create_ambiguous_parent_refused_before_write(gate: _GateRecorder) -> None:
    """A parent name matching >1 folder -> status=ambiguous_parent; no write."""
    conn = _FolderCreateConnector(
        parents=[{"folder": "group-v1", "name": "prod"}, {"folder": "group-v2", "name": "prod"}]
    )
    out = await folder_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"parent_folder": "prod", "folder_name": "cluster-nodes"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, dict)
    assert out["status"] == "ambiguous_parent"
    assert conn.vmomi_calls == []
    assert gate.calls == []


async def test_folder_create_gate_short_circuits_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on CreateFolder returns verbatim; no folder is created."""
    op_id = "POST:/Folder/{moId}/CreateFolder"
    _install_gate(monkeypatch, _GateRecorder(gate_for={op_id: _awaiting(op_id)}))
    conn = _FolderCreateConnector()
    out = await folder_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"parent_folder": "prod", "folder_name": "cluster-nodes"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert out.op_id == op_id
    assert conn.vmomi_calls == []


async def test_folder_create_body_reaches_the_wire_respx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """respx-verified: the CreateFolder POST body carries {name}, mounted on
    /sdk/vim25, and the returned Folder MoRef is unwrapped into the result.
    """

    async def _auto_execute(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _auto_execute)

    base = "https://vc-grow.test.invalid"
    vijson = "/sdk/vim25/8.0.3.0"
    parents = {"value": [{"folder": "group-v1", "name": "prod"}]}
    connector = VmwareRestConnector(session_loader=_stub_loader)
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=base) as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/about").respond(200, json={"version": "8.0.3"})
            mock.get("/api/vcenter/folder").respond(200, json=parents)
            create = mock.post(f"{vijson}/Folder/group-v1/CreateFolder").respond(
                200, json={"type": "Folder", "value": "group-v42"}
            )
            out = await folder_create_composite(
                operator=_make_operator(),
                target=_StubTarget(),
                params={"parent_folder": "prod", "folder_name": "cluster-nodes"},
                connector=connector,
            )
        assert isinstance(out, dict)
        assert out["status"] == "created"
        assert out["folder"] == "group-v42"
        assert create.called
        body = json.loads(create.calls[0].request.content)
        assert body == {"name": "cluster-nodes"}
    finally:
        await connector.aclose()


# ===========================================================================
# vm.resize (#2891)
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_resize_powered_off_patches_cpu_and_memory(gate: _GateRecorder) -> None:
    """Read sizing -> PATCH cpu -> PATCH memory (both gated, spec-wrapped bodies)."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1": {
                "name": "web-1",
                "power_state": "POWERED_OFF",
                "cpu": {"count": 1, "cores_per_socket": 1, "hot_add_enabled": False},
                "memory": {"size_MiB": 1024, "hot_add_enabled": False},
            },
            "/api/vcenter/vm/vm-1/hardware/cpu": {},
            "/api/vcenter/vm/vm-1/hardware/memory": {},
        }
    )
    out = await vm_resize_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cpu_count": 4, "cores_per_socket": 2, "memory_mib": 8192},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "resized"
    assert out["name"] == "web-1"
    assert out["applied"] == {"cpu": True, "memory": True}
    assert out["from"] == {"cpu_count": 1, "cores_per_socket": 1, "memory_MiB": 1024}
    assert out["to"] == {"cpu_count": 4, "cores_per_socket": 2, "memory_MiB": 8192}
    # Read is not gated; both PATCHes are, with the spec-wrapped bodies.
    assert gate.gated_op_ids == [
        "PATCH:/vcenter/vm/{vm}/hardware/cpu",
        "PATCH:/vcenter/vm/{vm}/hardware/memory",
    ]
    cpu_call = next(c for c in conn.calls if c["path"].endswith("/hardware/cpu"))
    mem_call = next(c for c in conn.calls if c["path"].endswith("/hardware/memory"))
    assert cpu_call["method"] == "PATCH"
    assert cpu_call["body"] == {"count": 4, "cores_per_socket": 2}
    assert mem_call["body"] == {"size_MiB": 8192}


@pytest.mark.asyncio
async def test_vm_resize_powered_on_without_hot_add_requires_power_off(
    gate: _GateRecorder,
) -> None:
    """A powered-on VM with hot-add disabled surfaces requires_power_off, never a 400."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1": {
                "name": "web-1",
                "power_state": "POWERED_ON",
                "cpu": {"count": 2, "cores_per_socket": 1, "hot_add_enabled": False},
                "memory": {"size_MiB": 2048, "hot_add_enabled": False},
            }
        }
    )
    out = await vm_resize_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cpu_count": 4},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "requires_power_off"
    assert out["applied"] == {"cpu": False, "memory": False}
    assert "power off" in out["guidance"]
    # Only the read fired; no PATCH was gated or issued.
    assert gate.calls == []
    assert [c["method"] for c in conn.calls] == ["GET"]


@pytest.mark.asyncio
async def test_vm_resize_no_requested_change_is_no_change(gate: _GateRecorder) -> None:
    """Requested values matching current -> no_change; no PATCH dispatched."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1": {
                "name": "web-1",
                "power_state": "POWERED_OFF",
                "cpu": {"count": 4, "cores_per_socket": 2},
                "memory": {"size_MiB": 8192},
            }
        }
    )
    out = await vm_resize_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cpu_count": 4, "memory_mib": 8192},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "no_change"
    assert gate.calls == []
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_vm_resize_memory_failure_after_cpu_is_partial(gate: _GateRecorder) -> None:
    """CPU PATCH lands, memory PATCH faults -> status=partial (CPU already applied)."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1": {
                "name": "web-1",
                "power_state": "POWERED_OFF",
                "cpu": {"count": 1},
                "memory": {"size_MiB": 1024},
            },
            "/api/vcenter/vm/vm-1/hardware/cpu": {},
            "/api/vcenter/vm/vm-1/hardware/memory": _http_error(
                400, "https://vc/api/vcenter/vm/vm-1/hardware/memory"
            ),
        }
    )
    out = await vm_resize_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cpu_count": 4, "memory_mib": 8192},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "partial"
    assert out["applied"] == {"cpu": True, "memory": False}
    assert "memory update failed" in out["guidance"]


@pytest.mark.asyncio
async def test_vm_resize_gate_short_circuits_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked CPU gate returns the awaiting result verbatim; no PATCH is issued."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1": {
                "power_state": "POWERED_OFF",
                "cpu": {"count": 1},
                "memory": {"size_MiB": 1024},
            }
        }
    )
    gate = _install_gate(
        monkeypatch,
        _GateRecorder(gate_for={"PATCH:/vcenter/vm/{vm}/hardware/cpu": _awaiting("resize")}),
    )
    out = await vm_resize_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cpu_count": 4},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    # Only the read + the gated (but not issued) CPU PATCH attempt; no memory.
    assert [c["method"] for c in conn.calls] == ["GET"]
    assert gate.gated_op_ids == ["PATCH:/vcenter/vm/{vm}/hardware/cpu"]


# ===========================================================================
# vm.nic.repoint (#2891)
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_nic_repoint_happy_path(gate: _GateRecorder) -> None:
    """Read NIC -> resolve portgroup by name -> PATCH backing (gated, spec-wrapped)."""
    # The NIC GET and the NIC PATCH mount to the same path (the PATCH carries
    # no ?action suffix); one canned value serves both -- the handler ignores
    # the PATCH's return payload.
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "mac_address": "00:50:56:aa:bb:cc",
                "backing": {
                    "type": "DISTRIBUTED_PORTGROUP",
                    "network": "dvportgroup-1",
                    "network_name": "old-net",
                },
            },
            "/api/vcenter/network": [
                {"network": "dvportgroup-9", "name": "prod-net", "type": "DISTRIBUTED_PORTGROUP"}
            ],
        }
    )
    out = await vm_nic_repoint_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "nic": "4000", "portgroup_name": "prod-net"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "repointed"
    assert out["mac_address"] == "00:50:56:aa:bb:cc"
    assert out["current_backing"]["network"] == "dvportgroup-1"
    assert out["requested_backing"] == {
        "portgroup_id": "dvportgroup-9",
        "portgroup_name": "prod-net",
    }
    # The NIC read + the network resolve read are not gated; only the PATCH is.
    assert gate.gated_op_ids == ["PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}"]
    patch_call = next(c for c in conn.calls if c["method"] == "PATCH")
    assert patch_call["body"] == {
        "backing": {"type": "DISTRIBUTED_PORTGROUP", "network": "dvportgroup-9"}
    }
    # The portgroup resolve used the corrected /vcenter/network path (#1602 fix).
    net_read = next(c for c in conn.calls if c["path"] == "/api/vcenter/network")
    assert net_read["method"] == "GET"


@pytest.mark.asyncio
async def test_vm_nic_repoint_portgroup_not_found(gate: _GateRecorder) -> None:
    """No portgroup matches the name -> status=not_found; no PATCH dispatched."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "mac_address": "00:50:56:aa:bb:cc",
                "backing": {"type": "STANDARD_PORTGROUP"},
            },
            "/api/vcenter/network": [],
        }
    )
    out = await vm_nic_repoint_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "nic": "4000", "portgroup_name": "ghost"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "not_found"
    assert out["requested_backing"] == {"portgroup_id": None, "portgroup_name": "ghost"}
    assert gate.calls == []


@pytest.mark.asyncio
async def test_vm_nic_repoint_ambiguous_portgroup(gate: _GateRecorder) -> None:
    """Two portgroups share the name -> status=ambiguous with candidates; no PATCH."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/ethernet/4000": {"mac_address": "aa", "backing": {}},
            "/api/vcenter/network": [
                {"network": "dvportgroup-1", "name": "dup", "type": "DISTRIBUTED_PORTGROUP"},
                {"network": "dvportgroup-2", "name": "dup", "type": "DISTRIBUTED_PORTGROUP"},
            ],
        }
    )
    out = await vm_nic_repoint_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "nic": "4000", "portgroup_name": "dup"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) == 2
    assert gate.calls == []


# ===========================================================================
# vm.device.cdrom (#2891)
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_device_cdrom_remove(gate: _GateRecorder) -> None:
    """Read backing -> DELETE the device (gated); status=removed, backing surfaced."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "backing": {"type": "ISO_FILE", "iso_file": "[datastore1] installer.iso"},
                "state": "CONNECTED",
            },
        }
    )
    out = await vm_device_cdrom_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cdrom": "16000", "action": "remove"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "removed"
    assert out["current_backing"]["iso_file"] == "[datastore1] installer.iso"
    assert out["state"] == "CONNECTED"
    assert gate.gated_op_ids == ["DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"]
    delete_call = next(c for c in conn.calls if c["method"] == "DELETE")
    assert delete_call["body"] is None


@pytest.mark.asyncio
async def test_vm_device_cdrom_disconnect(gate: _GateRecorder) -> None:
    """action=disconnect -> POST ?action=disconnect (gated); status=disconnected."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "backing": {"type": "HOST_DEVICE", "host_device": "/dev/cdrom"},
                "state": "CONNECTED",
            },
            "/api/vcenter/vm/vm-1/hardware/cdrom/16000?action=disconnect": {},
        }
    )
    out = await vm_device_cdrom_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cdrom": "16000", "action": "disconnect"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "disconnected"
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect"]
    post_call = next(c for c in conn.calls if c["method"] == "POST")
    assert post_call["path"] == "/api/vcenter/vm/vm-1/hardware/cdrom/16000?action=disconnect"
    assert post_call["body"] is None


@pytest.mark.asyncio
async def test_vm_device_cdrom_update_patches_backing(gate: _GateRecorder) -> None:
    """action=update with backing -> PATCH backing (gated, spec-wrapped); status=updated."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "backing": {"type": "ISO_FILE", "iso_file": "[local] pinned.iso"},
                "state": "CONNECTED",
            },
        }
    )
    out = await vm_device_cdrom_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "vm": "vm-1",
            "cdrom": "16000",
            "action": "update",
            "backing": {"type": "CLIENT_DEVICE"},
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "updated"
    assert out["requested_backing"] == {"type": "CLIENT_DEVICE"}
    assert gate.gated_op_ids == ["PATCH:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"]
    patch_call = next(c for c in conn.calls if c["method"] == "PATCH")
    assert patch_call["body"] == {"backing": {"type": "CLIENT_DEVICE"}}


@pytest.mark.asyncio
async def test_vm_device_cdrom_update_without_backing_is_invalid_request(
    gate: _GateRecorder,
) -> None:
    """action=update with no backing -> invalid_request; no write issued."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "backing": {"type": "ISO_FILE"},
                "state": "NOT_CONNECTED",
            },
        }
    )
    out = await vm_device_cdrom_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cdrom": "16000", "action": "update"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "invalid_request"
    assert gate.calls == []
    assert [c["method"] for c in conn.calls] == ["GET"]


# ===========================================================================
# guest.customization_spec.create (GOSC create)
# ===========================================================================


@pytest.mark.asyncio
async def test_guest_customization_spec_create_linux_body(gate: _GateRecorder) -> None:
    """Linux GOSC: one POST whose spec-wrapped body maps the agent subset to vCenter."""
    conn = _RecordingConnector({"/api/vcenter/guest/customization-specs": {"value": {}}})
    out = await guest_customization_spec_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "spec_name": "gosc-lin",
            "description": "web tier",
            "os_type": "linux",
            "hostname": "web-01",
            "domain": "corp.test",
            "time_zone": "Europe/Vienna",
            "interfaces": [
                {"ip_address": "10.0.0.5", "prefix": 24, "gateways": ["10.0.0.1"]},
                {},  # a NIC with no ip_address configures DHCP
            ],
            "dns_servers": ["10.0.0.2"],
            "dns_suffix_list": ["corp.test"],
        },
        connector=conn,  # type: ignore[arg-type]
    )

    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("POST", "/api/vcenter/guest/customization-specs"),
    ]
    body = conn.calls[0]["body"]
    assert body["name"] == "gosc-lin"
    assert body["description"] == "web tier"
    linux = body["spec"]["configuration_spec"]["linux_config"]
    assert linux["hostname"] == {"type": "FIXED", "fixed_name": "web-01"}
    assert linux["domain"] == "corp.test"
    assert linux["time_zone"] == "Europe/Vienna"
    # windows_config is absent on the linux branch.
    assert "windows_config" not in body["spec"]["configuration_spec"]
    # Interfaces: one STATIC ipv4, one DHCP.
    interfaces = body["spec"]["interfaces"]
    assert interfaces[0] == {
        "adapter": {
            "ipv4": {
                "type": "STATIC",
                "ip_address": "10.0.0.5",
                "prefix": 24,
                "gateways": ["10.0.0.1"],
            }
        }
    }
    assert interfaces[1] == {"adapter": {"ipv4": {"type": "DHCP"}}}
    assert body["spec"]["global_dns_settings"] == {
        "dns_servers": ["10.0.0.2"],
        "dns_suffix_list": ["corp.test"],
    }
    # The single write was gated dangerous / no-approval.
    assert gate.gated_op_ids == ["POST:/vcenter/guest/customization-specs"]
    assert gate.calls[0]["safety_level"] == "dangerous"
    assert gate.calls[0]["requires_approval"] is False
    assert out == {"status": "created", "spec_name": "gosc-lin", "os_type": "linux"}


@pytest.mark.asyncio
async def test_guest_customization_spec_create_windows_sysprep_body(gate: _GateRecorder) -> None:
    """Windows GOSC: the sysprep body carries the credentials (the real vCenter call).

    Secret hygiene is about *reviewer* surfaces (proven in the e2e lane); the
    actual customization-specs POST body legitimately carries the sysprep
    credentials -- that IS the API call that provisions the guest.
    """
    conn = _RecordingConnector({"/api/vcenter/guest/customization-specs": {"value": {}}})
    out = await guest_customization_spec_create_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "spec_name": "gosc-win",
            "os_type": "windows",
            "hostname": "win-01",
            "windows_admin_password": "pw-admin",
            "windows_product_key": "KEY-123",
            "windows_organization": "evoila",
            "windows_join_domain": "corp.test",
            "windows_domain_admin_username": "svc-join",
            "windows_domain_admin_password": "pw-join",
        },
        connector=conn,  # type: ignore[arg-type]
    )
    sysprep = conn.calls[0]["body"]["spec"]["configuration_spec"]["windows_config"]["sysprep"]
    assert sysprep["user_data"]["computer_name"] == {"type": "FIXED", "fixed_name": "win-01"}
    assert sysprep["user_data"]["product_key"] == "KEY-123"
    assert sysprep["user_data"]["organization"] == "evoila"
    # UserData.full_name is REQUIRED -> always emitted (default "") even unset.
    assert sysprep["user_data"]["full_name"] == ""
    assert sysprep["gui_unattended"]["password"] == "pw-admin"
    # GuiUnattended.auto_logon_count + time_zone are REQUIRED: count derives
    # from auto_logon (unset -> False -> 0); time_zone is the integer MS index
    # default (85), NOT the Linux tz-name string.
    assert sysprep["gui_unattended"]["auto_logon"] is False
    assert sysprep["gui_unattended"]["auto_logon_count"] == 0
    assert sysprep["gui_unattended"]["time_zone"] == 85
    # B1: domain join is the REST ``domain`` block (Vcenter.Guest.Domain),
    # NOT the pyvmomi ``identification`` key.
    assert "identification" not in sysprep
    assert sysprep["domain"] == {
        "type": "DOMAIN",
        "domain": "corp.test",
        "domain_username": "svc-join",
        "domain_password": "pw-join",
    }
    assert out["status"] == "created"
    assert out["os_type"] == "windows"


# ---------------------------------------------------------------------------
# GOSC create-body contract vs. the pinned CustomizationSpec schema (M1 #2892)
# ---------------------------------------------------------------------------
#
# CI was green while the create body was wrong (B1/B2) because the
# ingest-reconcile lane checks sub-op PATHS only and the unit test above had
# encoded the broken Windows shape as its expected value. This lane closes that
# gap: it validates the built ``POST /vcenter/guest/customization-specs`` body
# against a JSON-Schema mirror of the pinned vCenter
# ``Vcenter.Guest.CustomizationSpecs.CreateSpec``, transcribed field-for-field
# from the connector's pinned 9.0 spec
# (claude-rdc-hetzner-dc/docs/vcenter-9.0/vcenter.yaml, cited line anchors per
# ``$defs`` entry). ``additionalProperties: False`` on every object turns a
# stray pyvmomi/SOAP field name (e.g. ``identification``) into a validation
# failure; the per-object ``required`` lists turn an omitted mandatory field
# (e.g. ``auto_logon_count`` / ``description`` / ``domain`` / ``product_key``)
# into one. So a wrong field name or a missing required field now fails CI --
# for both the Linux and the Windows/sysprep branch.
_PINNED_CREATE_SPEC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$ref": "#/$defs/CreateSpec",
    "$defs": {
        "HostnameGenerator": {  # vcenter.yaml:125992
            "type": "object",
            "required": ["type"],
            "additionalProperties": False,
            "properties": {
                "type": {"enum": ["FIXED", "PREFIX", "VIRTUAL_MACHINE", "USER_INPUT_REQUIRED"]},
                "fixed_name": {"type": "string"},
                "prefix": {"type": "string"},
            },
        },
        "Ipv4": {  # vcenter.yaml:126513
            "type": "object",
            "required": ["type"],
            "additionalProperties": False,
            "properties": {
                "type": {"enum": ["DHCP", "STATIC", "USER_INPUT_REQUIRED"]},
                "ip_address": {"type": "string"},
                "prefix": {"type": "integer"},
                "gateways": {"type": "array", "items": {"type": "string"}},
            },
        },
        "IPSettings": {  # vcenter.yaml:126728 -- ipv4 is "currently required".
            "type": "object",
            "required": ["ipv4"],
            "additionalProperties": False,
            "properties": {"ipv4": {"$ref": "#/$defs/Ipv4"}},
        },
        "AdapterMapping": {  # vcenter.yaml:126762
            "type": "object",
            "required": ["adapter"],
            "additionalProperties": False,
            "properties": {
                "mac_address": {"type": "string"},
                "adapter": {"$ref": "#/$defs/IPSettings"},
            },
        },
        "GlobalDNSSettings": {  # vcenter.yaml:126486
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dns_suffix_list": {"type": "array", "items": {"type": "string"}},
                "dns_servers": {"type": "array", "items": {"type": "string"}},
            },
        },
        "UserData": {  # vcenter.yaml:126067
            "type": "object",
            "required": ["computer_name", "full_name", "organization", "product_key"],
            "additionalProperties": False,
            "properties": {
                "computer_name": {"$ref": "#/$defs/HostnameGenerator"},
                "full_name": {"type": "string"},
                "organization": {"type": "string"},
                "product_key": {"type": "string"},
            },
        },
        "Domain": {  # vcenter.yaml:126104
            "type": "object",
            "required": ["type"],
            "additionalProperties": False,
            "properties": {
                "type": {"enum": ["WORKGROUP", "DOMAIN"]},
                "workgroup": {"type": "string"},
                "domain": {"type": "string"},
                "domain_username": {"type": "string"},
                "domain_password": {"type": "string"},
                "domain_ou": {"type": "string"},
            },
        },
        "GuiUnattended": {  # vcenter.yaml:126181
            "type": "object",
            "required": ["auto_logon", "auto_logon_count", "time_zone"],
            "additionalProperties": False,
            "properties": {
                "auto_logon": {"type": "boolean"},
                "auto_logon_count": {"type": "integer"},
                "password": {"type": "string"},
                "time_zone": {"type": "integer"},
            },
        },
        "WindowsSysprep": {  # vcenter.yaml:126221
            "type": "object",
            "required": ["gui_unattended", "user_data"],
            "additionalProperties": False,
            "properties": {
                "gui_run_once_commands": {"type": "array", "items": {"type": "string"}},
                "user_data": {"$ref": "#/$defs/UserData"},
                "domain": {"$ref": "#/$defs/Domain"},
                "gui_unattended": {"$ref": "#/$defs/GuiUnattended"},
            },
        },
        "WindowsConfiguration": {  # vcenter.yaml:126264
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reboot": {"enum": ["REBOOT", "NO_REBOOT", "SHUTDOWN"]},
                "sysprep": {"$ref": "#/$defs/WindowsSysprep"},
                "sysprep_xml": {"type": "string"},
            },
        },
        "LinuxConfiguration": {  # vcenter.yaml:126320
            "type": "object",
            "required": ["domain", "hostname"],
            "additionalProperties": False,
            "properties": {
                "hostname": {"$ref": "#/$defs/HostnameGenerator"},
                "domain": {"type": "string"},
                "time_zone": {"type": "string"},
                "script_text": {"type": "string"},
                "compatible_customization_method": {"type": "string"},
            },
        },
        # ConfigurationSpec (vcenter.yaml:126452) also allows cloud_config; the
        # provisioning-subset builder only emits windows_config / linux_config.
        "ConfigurationSpec": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "windows_config": {"$ref": "#/$defs/WindowsConfiguration"},
                "linux_config": {"$ref": "#/$defs/LinuxConfiguration"},
            },
        },
        "CustomizationSpec": {  # vcenter.yaml:126790
            "type": "object",
            "required": ["configuration_spec", "global_dns_settings", "interfaces"],
            "additionalProperties": False,
            "properties": {
                "configuration_spec": {"$ref": "#/$defs/ConfigurationSpec"},
                "global_dns_settings": {"$ref": "#/$defs/GlobalDNSSettings"},
                "interfaces": {"type": "array", "items": {"$ref": "#/$defs/AdapterMapping"}},
            },
        },
        "CreateSpec": {  # vcenter.yaml:126873 -- the POST body.
            "type": "object",
            "required": ["description", "name", "spec"],
            "additionalProperties": False,
            "properties": {
                "spec": {"$ref": "#/$defs/CustomizationSpec"},
                "description": {"type": "string"},
                "name": {"type": "string"},
            },
        },
    },
}

_GOSC_LINUX_MINIMAL: dict[str, Any] = {
    "spec_name": "gosc-lin",
    "os_type": "linux",
    "hostname": "web-01",
}
_GOSC_LINUX_FULL: dict[str, Any] = {
    "spec_name": "gosc-lin",
    "description": "web tier",
    "os_type": "linux",
    "hostname": "web-01",
    "domain": "corp.test",
    "time_zone": "Europe/Vienna",
    "interfaces": [
        {"ip_address": "10.0.0.5", "prefix": 24, "gateways": ["10.0.0.1"]},
        {},  # a NIC with no ip_address -> DHCP
    ],
    "dns_servers": ["10.0.0.2"],
    "dns_suffix_list": ["corp.test"],
}
_GOSC_WINDOWS_MINIMAL: dict[str, Any] = {
    "spec_name": "gosc-win",
    "os_type": "windows",
    "hostname": "win-01",
}
_GOSC_WINDOWS_DOMAIN_JOIN: dict[str, Any] = {
    "spec_name": "gosc-win",
    "os_type": "windows",
    "hostname": "win-01",
    "windows_admin_password": "pw-admin",
    "windows_product_key": "KEY-123",
    "windows_organization": "evoila",
    "windows_full_name": "Ops Team",
    "windows_time_zone": 110,
    "windows_auto_logon": True,
    "windows_join_domain": "corp.test",
    "windows_domain_admin_username": "svc-join",
    "windows_domain_admin_password": "pw-join",
    "interfaces": [{"ip_address": "10.0.0.5", "prefix": 24, "gateways": ["10.0.0.1"]}],
}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(_GOSC_LINUX_MINIMAL, id="linux-minimal"),
        pytest.param(_GOSC_LINUX_FULL, id="linux-static-and-dhcp"),
        pytest.param(_GOSC_WINDOWS_MINIMAL, id="windows-minimal"),
        pytest.param(_GOSC_WINDOWS_DOMAIN_JOIN, id="windows-domain-join"),
    ],
)
def test_gosc_create_body_conforms_to_pinned_customization_spec_schema(
    params: dict[str, Any],
) -> None:
    """The built create body validates against the pinned CreateSpec schema.

    Guards the create BODY shape (field names + required fields) that the
    ingest-reconcile lane -- which checks sub-op PATHS only -- cannot. The
    minimal Linux and minimal Windows cases exercise the always-emitted
    required fields (``description`` / ``domain`` / ``UserData`` /
    ``GuiUnattended``) that ``_put_if_str`` used to drop.
    """
    body = _write._build_customization_create_body(params)
    # The builder returns the CreateSpec at the top level of the request body (#2973);
    # the only ``spec`` key is the inner CustomizationSpec field, not a /rest envelope.
    Draft202012Validator(_PINNED_CREATE_SPEC_SCHEMA).validate(body)


def test_gosc_create_body_schema_rejects_pyvmomi_shape_and_missing_required() -> None:
    """The contract lane bites: the pre-fix B1/B2 regressions fail validation.

    Proves the schema mirror is not vacuously green -- the exact broken shapes
    this iteration fixes (the pyvmomi ``identification`` key; an omitted
    required ``GuiUnattended.auto_logon_count``; an omitted required
    ``CreateSpec.description``) are each rejected.
    """
    validator = Draft202012Validator(_PINNED_CREATE_SPEC_SCHEMA)
    good = _write._build_customization_create_body(_GOSC_WINDOWS_DOMAIN_JOIN)
    validator.validate(good)  # sanity: the corrected body is valid.

    # B1 regression: the pyvmomi ``identification`` block is not a
    # WindowsSysprep property -> additionalProperties rejects it.
    b1 = copy.deepcopy(good)
    b1_sysprep = b1["spec"]["configuration_spec"]["windows_config"]["sysprep"]
    b1_sysprep.pop("domain", None)
    b1_sysprep["identification"] = {
        "joined_domain": "corp.test",
        "domain_admin_username": "svc-join",
        "domain_admin_password": "pw-join",
    }
    with pytest.raises(ValidationError):
        validator.validate(b1)

    # B2 regression: GuiUnattended.auto_logon_count is required -> dropping it
    # (as the old _put_if_str-built body did) fails.
    b2 = copy.deepcopy(good)
    del b2["spec"]["configuration_spec"]["windows_config"]["sysprep"]["gui_unattended"][
        "auto_logon_count"
    ]
    with pytest.raises(ValidationError):
        validator.validate(b2)

    # B2 regression: CreateSpec.description is required -> dropping it fails.
    b3 = copy.deepcopy(good)
    del b3["description"]
    with pytest.raises(ValidationError):
        validator.validate(b3)


@pytest.mark.asyncio
async def test_guest_customization_spec_create_gate_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked gate on the create returns the OperationResult; no POST fires."""
    conn = _RecordingConnector({})
    _install_gate(
        monkeypatch,
        _GateRecorder(
            gate_for={
                "POST:/vcenter/guest/customization-specs": _awaiting(
                    "POST:/vcenter/guest/customization-specs"
                )
            }
        ),
    )
    out = await guest_customization_spec_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"spec_name": "gosc-lin", "os_type": "linux", "hostname": "web-01"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    assert conn.calls == [], "no write may fire once the gate parks"


# ===========================================================================
# vm.customize (GOSC apply)
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_customize_powered_off_sets_customization(gate: _GateRecorder) -> None:
    """Resolve by name -> PUT the named spec on a powered-off VM."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": {
                "value": [{"vm": "vm-7", "name": "app", "power_state": "POWERED_OFF"}]
            },
            "/api/vcenter/vm/vm-7/guest/customization": {"value": {}},
        }
    )
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "app", "spec_name": "gosc-lin"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/vm"),
        ("PUT", "/api/vcenter/vm/vm-7/guest/customization"),
    ]
    # The resolve read forwards the name filter; PUT body is the named spec ref.
    assert conn.calls[0]["query"] == {"names": ["app"]}
    assert conn.calls[1]["body"] == {"name": "gosc-lin"}
    # Only the PUT was gated (the resolve GET is never gated).
    assert gate.gated_op_ids == ["PUT:/vcenter/vm/{vm}/guest/customization"]
    assert out["status"] == "customization_set"
    assert out["vm"] == "vm-7"
    assert out["power_state"] == "POWERED_OFF"
    assert out["applies_on"] == "next_power_on"


@pytest.mark.asyncio
async def test_vm_customize_powered_on_refused(gate: _GateRecorder) -> None:
    """A powered-on VM is refused with a structured precondition status; no PUT fires."""
    conn = _RecordingConnector(
        {"/api/vcenter/vm": {"value": [{"vm": "vm-9", "name": "db", "power_state": "POWERED_ON"}]}}
    )
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "db", "spec_name": "gosc-lin"},
        connector=conn,  # type: ignore[arg-type]
    )
    # Only the resolve GET fired; the PUT never did.
    assert [c["method"] for c in conn.calls] == ["GET"]
    assert gate.gated_op_ids == []
    assert out["status"] == "precondition_failed"
    assert out["vm"] == "vm-9"
    assert out["power_state"] == "POWERED_ON"
    assert out["applies_on"] is None


@pytest.mark.asyncio
async def test_vm_customize_power_on_after(gate: _GateRecorder) -> None:
    """power_on=True: PUT the customization then start the VM."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": {
                "value": [{"vm": "vm-7", "name": "app", "power_state": "POWERED_OFF"}]
            },
            "/api/vcenter/vm/vm-7/guest/customization": {"value": {}},
            "/api/vcenter/vm/vm-7/power?action=start": {"value": {}},
        }
    )
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "app", "spec_name": "gosc-lin", "power_on": True},
        connector=conn,  # type: ignore[arg-type]
    )
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/vm"),
        ("PUT", "/api/vcenter/vm/vm-7/guest/customization"),
        ("POST", "/api/vcenter/vm/vm-7/power?action=start"),
    ]
    assert gate.gated_op_ids == [
        "PUT:/vcenter/vm/{vm}/guest/customization",
        "POST:/vcenter/vm/{vm}/power?action=start",
    ]
    assert out["status"] == "powered_on"
    assert out["applies_on"] == "next_power_on"


@pytest.mark.asyncio
async def test_vm_customize_not_found(gate: _GateRecorder) -> None:
    """An empty resolve listing yields not_found; no write."""
    conn = _RecordingConnector({"/api/vcenter/vm": {"value": []}})
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "ghost", "spec_name": "gosc-lin"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert gate.gated_op_ids == []
    assert out["status"] == "not_found"
    assert out["vm"] is None


@pytest.mark.asyncio
async def test_vm_customize_ambiguous(gate: _GateRecorder) -> None:
    """Multiple name matches yield ambiguous with candidates; no write."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": {
                "value": [
                    {"vm": "vm-1", "name": "dup", "power_state": "POWERED_OFF"},
                    {"vm": "vm-2", "name": "dup", "power_state": "POWERED_ON"},
                ]
            }
        }
    )
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "dup", "spec_name": "gosc-lin"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert gate.gated_op_ids == []
    assert out["status"] == "ambiguous"
    assert [c["vm"] for c in out["candidates"]] == ["vm-1", "vm-2"]


@pytest.mark.asyncio
async def test_vm_customize_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parked gate on the PUT returns the OperationResult; the PUT never fires."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": {
                "value": [{"vm": "vm-7", "name": "app", "power_state": "POWERED_OFF"}]
            }
        }
    )
    _install_gate(
        monkeypatch,
        _GateRecorder(
            gate_for={
                "PUT:/vcenter/vm/{vm}/guest/customization": _awaiting(
                    "PUT:/vcenter/vm/{vm}/guest/customization"
                )
            }
        ),
    )
    out = await vm_customize_composite(
        operator=_make_operator(),
        target=object(),
        params={"name": "app", "spec_name": "gosc-lin"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert isinstance(out, OperationResult)
    assert out.status == "awaiting_approval"
    # The resolve GET fired but the PUT did not.
    assert [c["method"] for c in conn.calls] == ["GET"]
