# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the 8 vmware-rest write-composite handler functions.

Coverage matrix (G3.1-T6 / #509 acceptance criteria):

* Per-composite happy-path test: assert the correct sub-op_ids fire in
  the expected order with the right ``connector_id`` + ``params``;
  assert the returned dict shape matches the spec.
* Per-composite partial-failure test: mocked ``dispatch_child`` raises
  or returns an error-shaped result on a specific sub-op; assert the
  composite handles the failure per its documented rollback /
  partial-result semantics.
* ``host.evacuate`` recursion: assert the handler dispatches
  ``vmware.composite.vm.migrate`` per VM (proves the recursive
  composite pattern).
* Connector-id contract: every sub-op dispatches against
  ``vmware-rest-9.0``.

Each test mocks ``dispatch_child`` via a recording stub returning
canned :class:`OperationResult` objects keyed by op_id (or sequenced
when the same op_id fires multiple times).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_patch_composite,
    host_detach_from_vds_composite,
    host_evacuate_composite,
    vm_clone_composite,
    vm_create_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
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


def _ok(op_id: str, result: Any) -> OperationResult:
    """OK :class:`OperationResult` for a sub-op."""
    return OperationResult(status="ok", op_id=op_id, result=result, duration_ms=1.0)


def _err(op_id: str, error: str) -> OperationResult:
    """Error :class:`OperationResult` for partial-failure tests."""
    return OperationResult(status="error", op_id=op_id, error=error, duration_ms=1.0)


class _RecordingDispatchChild:
    """Records every ``dispatch_child`` call and serves canned results.

    Two modes match :mod:`._read`'s test stub:

    * ``dict`` -- ``op_id -> result``; same op_id served from the same
      slot every time.
    * ``list`` -- sequential :class:`OperationResult` values returned
      in order. Use when the same op_id is dispatched multiple times
      with different per-call expectations.
    """

    def __init__(self, responses: dict[str, Any] | list[Any]) -> None:
        self._responses = responses
        self._sequence_index = 0
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        connector_id: str,
        op_id: str,
        params: dict[str, Any],
        target: Any = None,
    ) -> OperationResult:
        self.calls.append(
            {
                "connector_id": connector_id,
                "op_id": op_id,
                "params": dict(params),
                "target": target,
            }
        )
        if isinstance(self._responses, dict):
            payload = self._responses[op_id]
        else:
            payload = self._responses[self._sequence_index]
            self._sequence_index += 1
        if isinstance(payload, OperationResult):
            return payload
        return _ok(op_id, payload)


# ===========================================================================
# vm.create
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_create_happy_path_dispatches_all_steps_in_order() -> None:
    """Folder lookup -> create -> per-NIC attach -> power-on; returns status=created."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/folder", [{"folder": "folder-7", "name": "Prod"}]),
            _ok("POST:/vcenter/vm", {"value": "vm-99"}),
            _ok("PATCH:/vcenter/vm/{vm}/network", {}),
            _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
        ]
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
        dispatch_child=dispatch,
    )

    assert [c["op_id"] for c in dispatch.calls] == [
        "GET:/vcenter/folder",
        "POST:/vcenter/vm",
        "PATCH:/vcenter/vm/{vm}/network",
        "POST:/vcenter/vm/{vm}/power?action=start",
    ]
    assert dispatch.calls[0]["params"] == {"filter.names": ["Prod"]}
    assert dispatch.calls[1]["params"]["spec"]["placement"]["folder"] == "folder-7"
    assert dispatch.calls[2]["params"] == {"vm": "vm-99", "spec": {"network": "net-3"}}
    assert dispatch.calls[3]["params"] == {"vm": "vm-99"}
    assert out["status"] == "created"
    assert out["vm_id"] == "vm-99"
    assert out["steps_succeeded"] == ["folder_lookup", "create", "nic_attach", "power_on"]
    assert out["failed_step"] is None


@pytest.mark.asyncio
async def test_vm_create_partial_failure_rolls_back_via_delete() -> None:
    """NIC-attach failure triggers DELETE rollback; returns status=rolled_back."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/folder", [{"folder": "folder-1", "name": "Prod"}]),
            _ok("POST:/vcenter/vm", {"value": "vm-77"}),
            _err("PATCH:/vcenter/vm/{vm}/network", "nic-attach failed"),
            _ok("DELETE:/vcenter/vm/{vm}", {}),
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
        dispatch_child=dispatch,
    )
    # DELETE fires after the NIC error.
    op_ids_dispatched = [c["op_id"] for c in dispatch.calls]
    assert "DELETE:/vcenter/vm/{vm}" in op_ids_dispatched
    assert dispatch.calls[-1]["params"] == {"vm": "vm-77"}
    assert out["status"] == "rolled_back"
    assert out["vm_id"] is None
    assert out["failed_step"] == "nic_attach"
    assert out["rollback_reason"]


@pytest.mark.asyncio
async def test_vm_create_folder_lookup_empty_returns_rolled_back_no_create() -> None:
    """Empty folder match returns rolled_back; POST:/vcenter/vm never fires."""
    dispatch = _RecordingDispatchChild([_ok("GET:/vcenter/folder", [])])
    out = await vm_create_composite(
        operator=_make_operator(),
        target=object(),
        params={"folder_name": "Missing", "name": "web", "guest_os": "UBUNTU_64"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "rolled_back"
    assert out["failed_step"] == "folder_lookup"
    assert len(dispatch.calls) == 1


# ===========================================================================
# vm.clone
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_clone_happy_path_polls_task_to_completion() -> None:
    """Source-config read -> deploy -> task poll until SUCCEEDED; status=completed."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm/{vm}", {"name": "src"}),
            _ok(
                "POST:/vcenter/vm-template/library-items?action=deploy",
                {"task": "task-42"},
            ),
            _ok("GET:/cis/tasks/{task}", {"status": "SUCCEEDED", "result": {"vm": "vm-clone-1"}}),
        ]
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
        dispatch_child=dispatch,
    )
    assert out["status"] == "completed"
    assert out["task_id"] == "task-42"
    assert out["vm_id"] == "vm-clone-1"
    assert [c["op_id"] for c in dispatch.calls] == [
        "GET:/vcenter/vm/{vm}",
        "POST:/vcenter/vm-template/library-items?action=deploy",
        "GET:/cis/tasks/{task}",
    ]
    # ``action=deploy`` lives on the op_id, not body params.
    assert "action" not in dispatch.calls[1]["params"]


@pytest.mark.asyncio
async def test_vm_clone_wait_false_returns_pending_with_task_id() -> None:
    """wait_for_completion=False returns immediately; no task poll fires."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm/{vm}", {"name": "src"}),
            _ok(
                "POST:/vcenter/vm-template/library-items?action=deploy",
                {"task": "task-99"},
            ),
        ]
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
        dispatch_child=dispatch,
    )
    assert out["status"] == "pending"
    assert out["task_id"] == "task-99"
    assert out["vm_id"] is None
    # Only 2 calls (no task poll).
    assert len(dispatch.calls) == 2


@pytest.mark.asyncio
async def test_vm_clone_task_failed_raises_runtime_error() -> None:
    """Task returning FAILED status raises -- dispatcher wraps as connector_error."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm/{vm}", {}),
            _ok(
                "POST:/vcenter/vm-template/library-items?action=deploy",
                {"task": "task-bad"},
            ),
            _ok("GET:/cis/tasks/{task}", {"status": "FAILED", "error": "deploy failed"}),
        ]
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
            dispatch_child=dispatch,
        )


# ===========================================================================
# vm.snapshot.revert
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_snapshot_revert_happy_path_dispatches_revert() -> None:
    """List + match + revert; returns status=reverted with snapshot_id."""
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/vm/{vm}/snapshot",
                [{"snapshot": "snap-1", "name": "before-patch"}],
            ),
            _ok("POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert", {}),
        ]
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "before-patch"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "reverted"
    assert out["snapshot_id"] == "snap-1"
    assert dispatch.calls[1]["op_id"] == "POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert"
    # ``action=revert`` lives on the op_id, not body params.
    assert dispatch.calls[1]["params"] == {"vm": "vm-1", "snap": "snap-1"}


@pytest.mark.asyncio
async def test_vm_snapshot_revert_ambiguous_name_does_not_dispatch_revert() -> None:
    """Multiple snapshots share the name -> status=ambiguous; no revert dispatched."""
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/vm/{vm}/snapshot",
                [
                    {"snapshot": "snap-1", "name": "x"},
                    {"snapshot": "snap-2", "name": "x"},
                ],
            )
        ]
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "x"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "ambiguous"
    assert out["snapshot_id"] is None
    assert len(out["candidates"]) == 2
    # Only the listing call fired -- no revert.
    assert len(dispatch.calls) == 1


@pytest.mark.asyncio
async def test_vm_snapshot_revert_not_found_returns_not_found() -> None:
    """Snapshot name not in tree -> status=not_found; no revert dispatched."""
    dispatch = _RecordingDispatchChild(
        [_ok("GET:/vcenter/vm/{vm}/snapshot", [{"snapshot": "s-1", "name": "other"}])]
    )
    out = await vm_snapshot_revert_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "snapshot_name": "missing"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "not_found"
    assert len(dispatch.calls) == 1


# ===========================================================================
# vm.migrate
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_migrate_drs_recommendation_dispatches_relocate() -> None:
    """DRS recommendation -> relocate dispatched against recommended host."""
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/cluster/{cluster}/drs/recommendations",
                [{"vm": "vm-1", "target_host": "host-A"}],
            ),
            _ok("POST:/vcenter/vm/{vm}?action=relocate", {}),
        ]
    )
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-1", "cluster": "cluster-7"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "migrated"
    assert out["target_host"] == "host-A"
    assert out["source"] == "drs"
    assert dispatch.calls[1]["op_id"] == "POST:/vcenter/vm/{vm}?action=relocate"
    assert dispatch.calls[1]["params"]["vm"] == "vm-1"
    # ``action=relocate`` lives on the op_id, not body params.
    assert "action" not in dispatch.calls[1]["params"]


@pytest.mark.asyncio
async def test_vm_migrate_explicit_target_bypasses_drs_lookup() -> None:
    """``target_host`` override skips the DRS sub-op; relocate dispatches directly."""
    dispatch = _RecordingDispatchChild([_ok("POST:/vcenter/vm/{vm}?action=relocate", {})])
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-2", "cluster": "cluster-9", "target_host": "host-Z"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "migrated"
    assert out["target_host"] == "host-Z"
    assert out["source"] == "operator"
    assert dispatch.calls[0]["op_id"] == "POST:/vcenter/vm/{vm}?action=relocate"
    assert len(dispatch.calls) == 1


@pytest.mark.asyncio
async def test_vm_migrate_no_recommendation_returns_status_no_recommendation() -> None:
    """DRS returns empty + no target_host override -> status=no_recommendation."""
    dispatch = _RecordingDispatchChild(
        [_ok("GET:/vcenter/cluster/{cluster}/drs/recommendations", [])]
    )
    out = await vm_migrate_composite(
        operator=_make_operator(),
        target=object(),
        params={"vm": "vm-3", "cluster": "cluster-1"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "no_recommendation"
    assert out["target_host"] is None
    assert out["source"] == "none"
    # Relocate did NOT dispatch.
    assert len(dispatch.calls) == 1


# ===========================================================================
# vm.power.bulk
# ===========================================================================


@pytest.mark.asyncio
async def test_vm_power_bulk_happy_path_aggregates_per_vm_results() -> None:
    """Filter listing + per-VM power; aggregate results + summary counts."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm", [{"vm": "vm-1"}, {"vm": "vm-2"}, {"vm": "vm-3"}]),
            _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
            _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
            _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
        ]
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"filter": {"power_states": ["POWERED_OFF"]}, "action": "start"},
        dispatch_child=dispatch,
    )
    assert out["summary"] == {"ok": 3, "error": 0}
    assert {r["vm"] for r in out["results"]} == {"vm-1", "vm-2", "vm-3"}
    assert all(r["status"] == "ok" for r in out["results"])
    assert out["aborted_on_failure"] is False
    # Filter forwarded as filter.power_states.
    assert dispatch.calls[0]["params"] == {"filter.power_states": ["POWERED_OFF"]}
    # Every per-VM call targets the ``?action=start`` descriptor row; action
    # verb lives on the op_id, not body params.
    for call in dispatch.calls[1:]:
        assert call["op_id"] == "POST:/vcenter/vm/{vm}/power?action=start"
        assert "action" not in call["params"]


@pytest.mark.asyncio
async def test_vm_power_bulk_partial_failure_continues_by_default() -> None:
    """One per-VM failure does not abort; summary reflects mixed outcome."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm", [{"vm": "vm-1"}, {"vm": "vm-2"}]),
            _err("POST:/vcenter/vm/{vm}/power?action=stop", "boom"),
            _ok("POST:/vcenter/vm/{vm}/power?action=stop", {}),
        ]
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"action": "stop"},
        dispatch_child=dispatch,
    )
    assert out["summary"] == {"ok": 1, "error": 1}
    assert out["aborted_on_failure"] is False
    # Both VMs were attempted.
    assert len(dispatch.calls) == 3
    # ``stop`` lives on the op_id.
    for call in dispatch.calls[1:]:
        assert call["op_id"] == "POST:/vcenter/vm/{vm}/power?action=stop"


@pytest.mark.asyncio
async def test_vm_power_bulk_fail_fast_aborts_on_first_failure() -> None:
    """fail_fast=True -> abort after first error; remaining VMs untouched."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm", [{"vm": "vm-1"}, {"vm": "vm-2"}, {"vm": "vm-3"}]),
            _err("POST:/vcenter/vm/{vm}/power?action=stop", "denied"),
        ]
    )
    out = await vm_power_bulk_composite(
        operator=_make_operator(),
        target=object(),
        params={"action": "stop", "fail_fast": True},
        dispatch_child=dispatch,
    )
    assert out["aborted_on_failure"] is True
    assert out["summary"] == {"ok": 0, "error": 1}
    assert len(out["results"]) == 1
    # Only listing + first power call fired -- the other two VMs never
    # got dispatched.
    assert len(dispatch.calls) == 2


# ===========================================================================
# host.evacuate -- recursive composite
# ===========================================================================


@pytest.mark.asyncio
async def test_host_evacuate_dispatches_vm_migrate_per_vm_then_maintenance() -> None:
    """host.evacuate recursively dispatches vm.migrate, then enters maintenance."""
    migrate_result = OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "migrated", "target_host": "h-2"},
        duration_ms=1.0,
    )
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/vm",
                # Per-VM cluster moids differ so the per-row resolution
                # gets exercised end to end (vm-a/c-1, vm-b/c-2).
                [{"vm": "vm-a", "cluster": "c-1"}, {"vm": "vm-b", "cluster": "c-2"}],
            ),
            migrate_result,
            migrate_result,
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
        ]
    )
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-1"},
        dispatch_child=dispatch,
    )
    op_ids = [c["op_id"] for c in dispatch.calls]
    assert op_ids == [
        "GET:/vcenter/vm",
        "vmware.composite.vm.migrate",
        "vmware.composite.vm.migrate",
        "PATCH:/vcenter/host/{host}/maintenance?action=enter",
    ]
    # Each recursive call carries that row's cluster moid — proves per-row
    # resolution (M1 fix from PR #529 iter-2).
    assert dispatch.calls[1]["params"] == {"vm": "vm-a", "cluster": "c-1"}
    assert dispatch.calls[2]["params"] == {"vm": "vm-b", "cluster": "c-2"}
    # Maintenance enter dispatches against the action-bearing descriptor row;
    # ``action`` is NOT a body param.
    assert dispatch.calls[3]["params"] == {"host": "host-1"}
    assert out["status"] == "evacuated"
    assert out["maintenance_entered"] is True
    assert out["migrated_vms"] == ["vm-a", "vm-b"]
    assert out["failed_vms"] == []


@pytest.mark.asyncio
async def test_host_evacuate_default_aborts_on_vm_migrate_failure() -> None:
    """tolerate_partial_failure=False -> a vm.migrate failure aborts before maintenance."""
    no_rec_result = OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "no_recommendation"},
        duration_ms=1.0,
    )
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/vm", [{"vm": "vm-x", "cluster": "c-1"}]),
            no_rec_result,
        ]
    )
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-3"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "aborted"
    assert out["maintenance_entered"] is False
    assert out["migrated_vms"] == []
    assert len(out["failed_vms"]) == 1
    # Maintenance-enter never dispatched.
    op_ids = [c["op_id"] for c in dispatch.calls]
    assert "PATCH:/vcenter/host/{host}/maintenance?action=enter" not in op_ids


@pytest.mark.asyncio
async def test_host_evacuate_tolerate_partial_failure_still_enters_maintenance() -> None:
    """tolerate_partial_failure=True -> maintenance enters even with VM failures."""
    ok_migrate = OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "migrated", "target_host": "h-2"},
        duration_ms=1.0,
    )
    err_migrate = OperationResult(
        status="ok",
        op_id="vmware.composite.vm.migrate",
        result={"status": "no_recommendation"},
        duration_ms=1.0,
    )
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/vm",
                [{"vm": "vm-a", "cluster": "c-1"}, {"vm": "vm-b", "cluster": "c-1"}],
            ),
            ok_migrate,
            err_migrate,
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
        ]
    )
    out = await host_evacuate_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-2", "tolerate_partial_failure": True},
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
async def test_host_detach_from_vds_happy_path_removes_host_after_nic_migration() -> None:
    """Discovery + per-VM NIC migration + DVS host-remove; status=detached."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/network/distributed-portgroup", []),
            _ok("GET:/vcenter/vm", [{"vm": "vm-1"}, {"vm": "vm-2"}]),
            _ok("PATCH:/vcenter/vm/{vm}/network", {}),
            _ok("PATCH:/vcenter/vm/{vm}/network", {}),
            _ok("POST:/vcenter/network/dvs/{dvs}?action=remove_host", {}),
        ]
    )
    out = await host_detach_from_vds_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-9", "dvs": "dvs-1", "fallback_network": "standard-net"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "detached"
    assert out["vms_migrated"] == ["vm-1", "vm-2"]
    assert out["vm_migration_failures"] == []
    # DVS remove fired against the action-bearing descriptor row.
    last_call = dispatch.calls[-1]
    assert last_call["op_id"] == "POST:/vcenter/network/dvs/{dvs}?action=remove_host"
    # ``action=remove_host`` lives on the op_id, not body params.
    assert last_call["params"] == {"dvs": "dvs-1", "host": "host-9"}


@pytest.mark.asyncio
async def test_host_detach_from_vds_incomplete_when_nic_migration_fails() -> None:
    """NIC migration failure -> status=incomplete; DVS remove skipped."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/network/distributed-portgroup", []),
            _ok("GET:/vcenter/vm", [{"vm": "vm-1"}, {"vm": "vm-2"}]),
            _ok("PATCH:/vcenter/vm/{vm}/network", {}),
            _err("PATCH:/vcenter/vm/{vm}/network", "nic move failed"),
        ]
    )
    out = await host_detach_from_vds_composite(
        operator=_make_operator(),
        target=object(),
        params={"host": "host-9", "dvs": "dvs-1", "fallback_network": "std"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "incomplete"
    assert out["vms_migrated"] == ["vm-1"]
    assert len(out["vm_migration_failures"]) == 1
    # No DVS remove call.
    op_ids = [c["op_id"] for c in dispatch.calls]
    assert "POST:/vcenter/network/dvs/{dvs}?action=remove_host" not in op_ids


# ===========================================================================
# cluster.patch
# ===========================================================================


@pytest.mark.asyncio
async def test_cluster_patch_happy_path_dispatches_sequential_maintenance_patch_exit() -> None:
    """Per-host: maintenance enter -> patch -> maintenance exit; status=completed."""
    dispatch = _RecordingDispatchChild(
        [
            _ok("GET:/vcenter/cluster/{cluster}/host", [{"host": "h1"}, {"host": "h2"}]),
            # h1: enter -> patch -> exit, action verbs on the op_id.
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
            _ok("POST:/vcenter/host/{host}?action=patch", {}),
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=exit", {}),
            # h2
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
            _ok("POST:/vcenter/host/{host}?action=patch", {}),
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=exit", {}),
        ]
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "completed"
    assert out["patched_hosts"] == ["h1", "h2"]
    assert out["remaining_hosts"] == []
    # Action sequence per host lives on the op_id (one descriptor row per verb).
    host_calls = list(dispatch.calls[1:])
    assert host_calls[0]["op_id"] == "PATCH:/vcenter/host/{host}/maintenance?action=enter"
    assert host_calls[1]["op_id"] == "POST:/vcenter/host/{host}?action=patch"
    assert host_calls[2]["op_id"] == "PATCH:/vcenter/host/{host}/maintenance?action=exit"
    # Body params drop the ``action`` field; patch carries a ``method`` body.
    assert host_calls[0]["params"] == {"host": "h1"}
    assert host_calls[1]["params"] == {"host": "h1", "method": "default"}
    assert host_calls[2]["params"] == {"host": "h1"}


@pytest.mark.asyncio
async def test_cluster_patch_per_host_failure_stops_loop() -> None:
    """A per-host failure stops the loop; status=stopped with remaining_hosts."""
    dispatch = _RecordingDispatchChild(
        [
            _ok(
                "GET:/vcenter/cluster/{cluster}/host",
                [{"host": "h1"}, {"host": "h2"}, {"host": "h3"}],
            ),
            # h1 completes cleanly.
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
            _ok("POST:/vcenter/host/{host}?action=patch", {}),
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=exit", {}),
            # h2 fails on patch.
            _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
            _err("POST:/vcenter/host/{host}?action=patch", "vendor patch failed"),
        ]
    )
    out = await cluster_patch_composite(
        operator=_make_operator(),
        target=object(),
        params={"cluster": "c-1"},
        dispatch_child=dispatch,
    )
    assert out["status"] == "stopped"
    assert out["patched_hosts"] == ["h1"]
    assert out["failed_host"] == "h2"
    assert out["remaining_hosts"] == ["h3"]
    assert out["failure_reason"]


# ===========================================================================
# Connector-id contract
# ===========================================================================


@pytest.mark.asyncio
async def test_every_write_composite_uses_vmware_rest_9_0_connector_id() -> None:
    """Every write composite dispatches sub-ops against ``vmware-rest-9.0`` exclusively.

    Load-bearing for the issue body's *dispatch_child not direct httpx*
    contract -- the connector_id is what routes recursive dispatch
    back to :class:`VmwareRestConnector` for sub-call authentication.
    """
    # Every case drives the composite through ALL of its sub-op_ids so the
    # connector_id contract is asserted on the full dispatch chain — empty
    # listings would short-circuit before the action-bearing typed ops fire
    # (M4 from PR #529 iter-2: vm_create_composite was exiting on an empty
    # folder match before its create / nic / power / rollback sub-ops ran).
    cases: tuple[tuple[Any, dict[str, Any], list[Any]], ...] = (
        (
            vm_create_composite,
            {
                "folder_name": "f",
                "name": "n",
                "guest_os": "g",
                "nics": [{"network": "net-a"}],
                "power_on_after_create": True,
            },
            [
                _ok("GET:/vcenter/folder", [{"folder": "folder-x", "name": "f"}]),
                _ok("POST:/vcenter/vm", {"value": "vm-x"}),
                _ok("PATCH:/vcenter/vm/{vm}/network", {}),
                _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
            ],
        ),
        (
            vm_clone_composite,
            {
                "source_vm": "v",
                "target_name": "t",
                "library_item": "l",
                "wait_for_completion": False,
            },
            [
                _ok("GET:/vcenter/vm/{vm}", {}),
                _ok("POST:/vcenter/vm-template/library-items?action=deploy", {"task": "t"}),
            ],
        ),
        (
            vm_snapshot_revert_composite,
            {"vm": "v", "snapshot_name": "n"},
            [
                _ok("GET:/vcenter/vm/{vm}/snapshot", [{"snapshot": "snap-1", "name": "n"}]),
                _ok("POST:/vcenter/vm/{vm}/snapshot/{snap}?action=revert", {}),
            ],
        ),
        (
            vm_migrate_composite,
            {"vm": "v", "cluster": "c", "target_host": "h"},
            [_ok("POST:/vcenter/vm/{vm}?action=relocate", {})],
        ),
        (
            vm_power_bulk_composite,
            {"action": "start"},
            [
                _ok("GET:/vcenter/vm", [{"vm": "vm-x"}]),
                _ok("POST:/vcenter/vm/{vm}/power?action=start", {}),
            ],
        ),
        (
            host_evacuate_composite,
            {"host": "h"},
            [
                _ok("GET:/vcenter/vm", []),
                _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
            ],
        ),
        (
            host_detach_from_vds_composite,
            {"host": "h", "dvs": "d", "fallback_network": "f"},
            [
                _ok("GET:/vcenter/network/distributed-portgroup", []),
                _ok("GET:/vcenter/vm", []),
                _ok("POST:/vcenter/network/dvs/{dvs}?action=remove_host", {}),
            ],
        ),
        (
            cluster_patch_composite,
            {"cluster": "c"},
            [
                _ok("GET:/vcenter/cluster/{cluster}/host", [{"host": "h1"}]),
                _ok("PATCH:/vcenter/host/{host}/maintenance?action=enter", {}),
                _ok("POST:/vcenter/host/{host}?action=patch", {}),
                _ok("PATCH:/vcenter/host/{host}/maintenance?action=exit", {}),
            ],
        ),
    )
    for handler, params, responses in cases:
        dispatch = _RecordingDispatchChild(list(responses))
        await handler(
            operator=_make_operator(),
            target=object(),
            params=params,
            dispatch_child=dispatch,
        )
        for call in dispatch.calls:
            assert call["connector_id"] == "vmware-rest-9.0", (
                f"{handler.__qualname__} dispatched to {call['connector_id']}"
            )
