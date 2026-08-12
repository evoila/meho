# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 11 vmware-rest write-composite handler functions.

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

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_patch_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_disk_grow_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
    vm_power_composite,
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
    ) -> None:
        self._responses = responses
        self._seq_index = 0
        self._mount_prefix = mount_prefix
        self.calls: list[dict[str, Any]] = []
        self.mount_calls: list[str] = []

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


# ===========================================================================
# vm.create
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_create_happy_path_direct_session(gate: _GateRecorder) -> None:
    """Folder GET -> create POST -> NIC PATCH -> power POST; every write gated."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/folder": [{"folder": "folder-7", "name": "Prod"}],
            "/api/vcenter/vm": {"value": "vm-99"},
            "/api/vcenter/vm/vm-99/network": {},
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
        ("PATCH", "/api/vcenter/vm/vm-99/network"),
        ("POST", "/api/vcenter/vm/vm-99/power?action=start"),
    ]
    # Folder GET forwards the name filter as a query param; bare on /api (#2298).
    assert conn.calls[0]["query"] == {"names": ["Prod"]}
    # Create body carries the spec; folder moid resolved in.
    assert conn.calls[1]["body"]["spec"]["placement"]["folder"] == "folder-7"
    # NIC PATCH body is the spec; vm rides the path, not the body.
    assert conn.calls[2]["body"] == {"spec": {"network": "net-3"}}
    # Power POST carries no body (action rides the path).
    assert conn.calls[3]["body"] is None

    # Governance: exactly the 3 writes were gated (dangerous / no-approval);
    # the folder GET was never gated.
    assert gate.gated_op_ids == [
        "POST:/vcenter/vm",
        "PATCH:/vcenter/vm/{vm}/network",
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
async def test_vm_clone_happy_path_polls_task_to_completion(gate: _GateRecorder) -> None:
    """Source GET -> deploy POST (gated) -> task-poll GET until SUCCEEDED."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {"name": "src"},
            "/api/vcenter/vm-template/library-items?action=deploy": {"task": "task-42"},
            "/api/cis/tasks/task-42": {"status": "SUCCEEDED", "result": {"vm": "vm-clone-1"}},
        }
    )
    out = await vm_clone_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "source_vm": "vm-src",
            "target_name": "vm-clone-1",
            "library_item": "li-7",
            "timeout_seconds": 30,
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "completed"
    assert out["task_id"] == "task-42"
    assert out["vm_id"] == "vm-clone-1"
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/vm/vm-src"),
        ("POST", "/api/vcenter/vm-template/library-items?action=deploy"),
        ("GET", "/api/cis/tasks/task-42"),
    ]
    # Deploy body carries library_item + spec (no ``action`` body key).
    assert conn.calls[1]["body"] == {"library_item": "li-7", "spec": {"name": "vm-clone-1"}}
    # Only the deploy write was gated.
    assert gate.gated_op_ids == ["POST:/vcenter/vm-template/library-items?action=deploy"]


@pytest.mark.asyncio
async def test_vm_clone_wait_false_returns_pending(gate: _GateRecorder) -> None:
    """wait_for_completion=False returns pending; no task poll fires."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {"name": "src"},
            "/api/vcenter/vm-template/library-items?action=deploy": {"task": "task-99"},
        }
    )
    out = await vm_clone_composite(
        operator=_make_operator(),
        target=object(),
        params={
            "source_vm": "vm-src",
            "target_name": "tgt",
            "library_item": "li-3",
            "wait_for_completion": False,
        },
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "pending"
    assert out["task_id"] == "task-99"
    assert out["vm_id"] is None
    assert len(conn.calls) == 2


@pytest.mark.asyncio
async def test_vm_clone_task_failed_raises_runtime_error(gate: _GateRecorder) -> None:
    """A FAILED task raises -- dispatcher wraps as connector_error."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-src": {},
            "/api/vcenter/vm-template/library-items?action=deploy": {"task": "task-bad"},
            "/api/cis/tasks/task-bad": {"status": "FAILED", "error": "deploy failed"},
        }
    )
    with pytest.raises(RuntimeError, match="FAILED"):
        await vm_clone_composite(
            operator=_make_operator(),
            target=object(),
            params={
                "source_vm": "vm-src",
                "target_name": "tgt",
                "library_item": "li-1",
                "timeout_seconds": 30,
            },
            connector=conn,  # type: ignore[arg-type]
        )


# ===========================================================================
# vm.snapshot.revert
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_snapshot_revert_happy_path(gate: _GateRecorder) -> None:
    """List GET + match + revert POST (gated); status=reverted."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/snapshot": [{"snapshot": "snap-1", "name": "before-patch"}],
            "/api/vcenter/vm/vm-1/snapshot/snap-1?action=revert": {},
        }
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "before-patch"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "reverted"
    assert out["snapshot_id"] == "snap-1"
    assert conn.calls[1]["method"] == "POST"
    assert conn.calls[1]["path"] == "/api/vcenter/vm/vm-1/snapshot/snap-1?action=revert"
    assert conn.calls[1]["body"] is None
    assert gate.gated_op_ids == ["POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert"]


@pytest.mark.asyncio
async def test_vm_snapshot_revert_ambiguous_no_revert(gate: _GateRecorder) -> None:
    """Multiple snapshots share the name -> status=ambiguous; no revert dispatched."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm/vm-1/snapshot": [
                {"snapshot": "snap-1", "name": "x"},
                {"snapshot": "snap-2", "name": "x"},
            ]
        }
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "x"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) == 2
    assert len(conn.calls) == 1
    assert gate.calls == []


@pytest.mark.asyncio
async def test_vm_snapshot_revert_not_found(gate: _GateRecorder) -> None:
    """Snapshot name not in tree -> status=not_found; no revert dispatched."""
    conn = _RecordingConnector(
        {"/api/vcenter/vm/vm-1/snapshot": [{"snapshot": "s-1", "name": "other"}]}
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "missing"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "not_found"
    assert len(conn.calls) == 1
    assert gate.calls == []


# ===========================================================================
# vm.migrate
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_migrate_drs_recommendation_dispatches_relocate(gate: _GateRecorder) -> None:
    """DRS recommendation GET -> relocate POST (gated) against the recommended host."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/cluster-7/drs/recommendations": [
                {"vm": "vm-1", "target_host": "host-A"}
            ],
            "/api/vcenter/vm/vm-1?action=relocate": {},
        }
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
    assert conn.calls[1]["path"] == "/api/vcenter/vm/vm-1?action=relocate"
    assert conn.calls[1]["body"] == {"spec": {"placement": {"host": "host-A"}}}
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
    conn = _RecordingConnector({"/api/vcenter/cluster/cluster-1/drs/recommendations": []})
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-3", "cluster": "cluster-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "no_recommendation"
    assert out["source"] == "none"
    assert len(conn.calls) == 1
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
    """VM listing GET -> per-VM vm.migrate via dispatch_child -> maintenance-enter write."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/vm": [
                {"vm": "vm-a", "cluster": "c-1"},
                {"vm": "vm-b", "cluster": "c-2"},
            ],
            "/api/vcenter/host/host-1/maintenance?action=enter": {},
        }
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
    # The listing read + the maintenance-enter write are the only direct calls.
    assert [(c["method"], c["path"]) for c in conn.calls] == [
        ("GET", "/api/vcenter/vm"),
        ("PATCH", "/api/vcenter/host/host-1/maintenance?action=enter"),
    ]
    # Only the maintenance-enter write was gated (the recursion self-gates).
    assert gate.gated_op_ids == ["PATCH:/vcenter/host/{host}/maintenance?action=enter"]
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
            "/api/vcenter/host/host-2/maintenance?action=enter": {},
        }
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
    """Portgroup GET + VM GET + per-VM NIC PATCH + DVS remove POST; status=detached."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/network/distributed-portgroup": [],
            "/api/vcenter/vm": [{"vm": "vm-1"}, {"vm": "vm-2"}],
            "/api/vcenter/vm/vm-1/network": {},
            "/api/vcenter/vm/vm-2/network": {},
            "/api/vcenter/network/dvs/dvs-1?action=remove_host": {},
        }
    )
    out = await host_detach_from_vds_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-9", "dvs": "dvs-1", "fallback_network": "standard-net"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "detached"
    assert out["vms_migrated"] == ["vm-1", "vm-2"]
    last = conn.calls[-1]
    assert last["method"] == "POST"
    assert last["path"] == "/api/vcenter/network/dvs/dvs-1?action=remove_host"
    assert last["body"] == {"host": "host-9"}
    # NIC PATCH body carries the fallback network spec.
    assert conn.calls[2]["body"] == {"spec": {"network": "standard-net"}}
    # 2 NIC writes + 1 DVS remove write gated; the two reads were not.
    assert gate.gated_op_ids == [
        "PATCH:/vcenter/vm/{vm}/network",
        "PATCH:/vcenter/vm/{vm}/network",
        "POST:/vcenter/network/dvs/{dvs}?action=remove_host",
    ]


@pytest.mark.asyncio
async def test_host_detach_from_vds_incomplete_on_nic_failure(gate: _GateRecorder) -> None:
    """A NIC migration transport error -> status=incomplete; DVS remove skipped."""
    conn = _RecordingConnector(
        [
            [],  # portgroup GET
            [{"vm": "vm-1"}, {"vm": "vm-2"}],  # VM GET
            {},  # vm-1 NIC ok
            _http_error(409, "https://vc/api/vcenter/vm/vm-2/network"),  # vm-2 NIC fails
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
    # No DVS remove call -- the last recorded call is the failed NIC PATCH.
    assert all("remove_host" not in c["path"] for c in conn.calls)


# ===========================================================================
# cluster.patch
# ===========================================================================


@pytest.mark.asyncio
async def test_cluster_patch_happy_path(gate: _GateRecorder) -> None:
    """Per-host: maintenance-enter -> patch -> maintenance-exit; status=completed."""
    conn = _RecordingConnector(
        {
            "/api/vcenter/cluster/c-1/host": [{"host": "h1"}, {"host": "h2"}],
            "/api/vcenter/host/h1/maintenance?action=enter": {},
            "/api/vcenter/host/h1?action=patch": {},
            "/api/vcenter/host/h1/maintenance?action=exit": {},
            "/api/vcenter/host/h2/maintenance?action=enter": {},
            "/api/vcenter/host/h2?action=patch": {},
            "/api/vcenter/host/h2/maintenance?action=exit": {},
        }
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        connector=conn,  # type: ignore[arg-type]
    )
    assert out["status"] == "completed"
    assert out["patched_hosts"] == ["h1", "h2"]
    # The patch step carries a ``method`` body; the maintenance verbs do not.
    patch_call = next(c for c in conn.calls if c["path"] == "/api/vcenter/host/h1?action=patch")
    assert patch_call["body"] == {"method": "default"}
    enter_call = next(
        c for c in conn.calls if c["path"] == "/api/vcenter/host/h1/maintenance?action=enter"
    )
    assert enter_call["body"] is None
    # 3 writes per host x 2 hosts were gated.
    assert len(gate.calls) == 6


@pytest.mark.asyncio
async def test_cluster_patch_per_host_failure_stops_loop(gate: _GateRecorder) -> None:
    """A per-host transport error stops the loop; status=stopped with remaining_hosts."""
    conn = _RecordingConnector(
        [
            [{"host": "h1"}, {"host": "h2"}, {"host": "h3"}],  # host listing GET
            {},  # h1 enter
            {},  # h1 patch
            {},  # h1 exit
            {},  # h2 enter
            _http_error(500, "https://vc/api/vcenter/host/h2?action=patch"),  # h2 patch fails
        ]
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
        {
            "/api/vcenter/cluster/c-9/drs/recommendations": [
                {"vm": "vm-1", "target_host": "host-A"}
            ],
            "/api/vcenter/vm/vm-1?action=relocate": {},
        }
    )
    await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cluster": "c-9"},
        connector=conn,  # type: ignore[arg-type]
    )
    # The DRS recommendations read fired but was not gated; only the relocate
    # write was gated.
    read_paths = [c["path"] for c in conn.calls if c["method"] == "GET"]
    assert read_paths == ["/api/vcenter/cluster/c-9/drs/recommendations"]
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
