# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 9 vmware-rest write-composite handler functions.

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

from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_patch_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_create_composite,
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
    # Folder GET forwards the name filter as a query param.
    assert conn.calls[0]["query"] == {"filter.names": ["Prod"]}
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
    # Filter forwarded as filter.power_states query on the listing GET.
    assert conn.calls[0]["query"] == {"filter.power_states": ["POWERED_OFF"]}
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
