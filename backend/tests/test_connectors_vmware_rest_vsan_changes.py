# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The five vSAN changes, against a recording fake of the connector.

* ``vsan.cluster.configure`` and ``vsan.encryption.set`` (vendor jobs): the
  reconfigure spec in the vendor field order, ``accepted`` with the task id
  and the status read, ``already_running`` when a vSAN job runs, a vendor
  fault, a parked gate, the refusals before any call.
* ``vsan.encryption.rekey``: the task followed to its end, a task fault, a
  poll timeout.
* ``vsan.disk.claim``: the ESA and the OSA spec, the eligibility check, the
  disks read again, a task fault.
* ``vsan.disk.wipe``: the plan and the approval preview (``blast_radius``),
  every refusal, the partition wipe one disk after the other (also a failure
  at step two), and the secure wipe.

The approval flow through the real dispatcher is in
``test_connectors_vmware_rest_vsan_changes_approval.py``; the real connector
on the vCenter 8 and 9 wire is in ``..._vsan_changes_wire.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_cluster,
    _vsan_common,
    _vsan_disk_claim,
    _vsan_disk_wipe,
    _write,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    VSAN_HOST_WIPE_DISK,
    host_vsan_system_ex,
    vsan_op_id,
)
from meho_backplane.operations._preview import blast_radius_missing_reason
from meho_backplane.operations._validate import validate_params
from tests._vmware_teardown_fake import GateRecorder, moref, operator, parked, preview_ctx
from tests._vmware_vsan_fake import CLUSTER, HOST, STORAGE_SYSTEM, VSAN_SYSTEM, VsanFake

_CONFIGURE_OP = "vmware.composite.vsan.cluster.configure"
_RECONFIG_SUB_OP = "POST:/vsan/VsanVcClusterConfigSystem/{moId}/VsanClusterReconfig"
_DISK_A = "naa.0000000000000a"
_DISK_B = "naa.0000000000000b"
_DISK_C = "naa.0000000000000c"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    """One recorder for both seams: the vSAN changes and the VIM writes."""
    recorder = GateRecorder()
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_vsan_cluster, "_TASK_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(_vsan_disk_claim, "_TASK_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(_vsan_disk_wipe, "_TASK_TIMEOUT_SECONDS", 0.0)


async def _run(handler: Any, conn: VsanFake, **params: Any) -> Any:
    return await handler(operator=operator(), target=object(), params=params, connector=conn)


# ===========================================================================
# vsan.cluster.configure / vsan.encryption.set -- vendor jobs
# ===========================================================================


async def test_configure_sends_the_spec_in_vendor_order_and_answers_accepted(
    gate: GateRecorder,
) -> None:
    conn = VsanFake()
    conn.add_cluster()
    out = await _run(
        _vsan_cluster.vsan_cluster_configure_composite,
        conn,
        cluster=CLUSTER,
        vsan_enabled=True,
        esa=True,
        performance_service=True,
        space_efficiency="compression",
    )

    assert out["status"] == "accepted"
    assert out["task"] == "task-1"
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": "task-1"}}
    assert out["cluster_name"] == "cluster-a"
    assert "not done yet" in out["guidance"]
    [(method, body)] = conn.vsan_calls
    assert (method.mo_type, method.moid, method.method) == (
        "VsanVcClusterConfigSystem",
        "vsan-cluster-config-system",
        "VsanClusterReconfig",
    )
    assert body["cluster"] == moref("ClusterComputeResource", CLUSTER)
    spec = body["vsanReconfigSpec"]
    # SOAP checks the field order of VimVsanReconfigSpec.
    assert list(spec) == [
        "_typeName",
        "vsanClusterConfig",
        "dataEfficiencyConfig",
        "modify",
        "perfsvcConfig",
    ]
    assert spec["vsanClusterConfig"] == {
        "_typeName": "VsanClusterConfigInfo",
        "enabled": True,
        "vsanEsaEnabled": True,
    }
    assert spec["dataEfficiencyConfig"] == {
        "_typeName": "VsanDataEfficiencyConfig",
        "dedupEnabled": False,
        "compressionEnabled": True,
    }
    assert spec["modify"] is True
    assert spec["perfsvcConfig"] == {"_typeName": "VsanPerfsvcConfig", "enabled": True}
    # The step is checked under the vSAN method's VI/JSON path.
    assert [c["op_id"] for c in gate.calls] == [_RECONFIG_SUB_OP]
    assert gate.calls[0]["params"]["cluster"] == CLUSTER


async def test_configure_returns_a_running_vsan_job_and_starts_nothing(
    gate: GateRecorder,
) -> None:
    conn = VsanFake()
    conn.add_cluster(recent=["task-77", "task-78"])
    conn._tasks["task-77"] = {"state": "running", "descriptionId": "VirtualMachine.powerOn"}
    conn.running_task("task-78", "com.vmware.vsan.clusterconfig.reconfigure")
    out = await _run(
        _vsan_cluster.vsan_cluster_configure_composite, conn, cluster=CLUSTER, vsan_enabled=True
    )

    assert out["status"] == "already_running"
    assert out["task"] == "task-78"
    assert out["status_read"]["params"] == {"task": "task-78"}
    assert conn.vsan_calls == []
    assert gate.calls == []


async def test_configure_ignores_a_vsan_job_that_ended(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster(recent=["task-78"])
    conn._tasks["task-78"] = {"state": "success", "descriptionId": "vsan.reconfigure"}
    out = await _run(
        _vsan_cluster.vsan_cluster_configure_composite, conn, cluster=CLUSTER, vsan_enabled=True
    )
    assert out["status"] == "accepted"
    assert conn.vsan_methods() == ["VsanClusterReconfig"]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"cluster": "cluster-a", "vsan_enabled": True}, "invalid_request"),
        ({"cluster": CLUSTER, "esa": True}, "invalid_request"),
        ({"cluster": CLUSTER, "vsan_enabled": False, "esa": True}, "invalid_request"),
        ({"cluster": "domain-c99", "vsan_enabled": True}, "not_found"),
    ],
)
async def test_configure_refuses_before_any_call(
    gate: GateRecorder, params: dict[str, Any], status: str
) -> None:
    conn = VsanFake()
    conn.add_cluster()
    out = await _run(_vsan_cluster.vsan_cluster_configure_composite, conn, **params)
    assert out["status"] == status
    assert out["task"] is None
    assert conn.vsan_calls == []
    assert gate.calls == []


async def test_configure_returns_a_parked_gate_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = parked(_RECONFIG_SUB_OP)
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", GateRecorder(waiting))
    conn = VsanFake()
    conn.add_cluster()
    out = await _run(
        _vsan_cluster.vsan_cluster_configure_composite, conn, cluster=CLUSTER, vsan_enabled=True
    )
    assert out is waiting
    assert conn.vsan_calls == []


async def test_configure_vendor_fault_reaches_the_caller(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    conn.vsan_fault = RuntimeError(
        "vmware vsan VsanClusterReconfig failed on target 'vc': VsanFault: "
        "Cannot enable vSAN ESA on hosts without NVMe disks."
    )
    with pytest.raises(RuntimeError, match="VsanFault: Cannot enable vSAN ESA"):
        await _run(
            _vsan_cluster.vsan_cluster_configure_composite,
            conn,
            cluster=CLUSTER,
            vsan_enabled=True,
            esa=True,
        )


async def test_encryption_set_sends_both_encryption_configs(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    out = await _run(
        _vsan_cluster.vsan_encryption_set_composite,
        conn,
        cluster=CLUSTER,
        data_at_rest={"enabled": True, "key_provider": "kp-a", "erase_disks_before_use": False},
        data_in_transit={"enabled": True, "rekey_interval_minutes": 1440},
        allow_reduced_redundancy=True,
    )

    assert out["status"] == "accepted"
    assert out["requested"]["data_at_rest"]["key_provider"] == "kp-a"
    [(method, body)] = conn.vsan_calls
    assert method.method == "VsanClusterReconfig"
    spec = body["vsanReconfigSpec"]
    assert list(spec) == [
        "_typeName",
        "modify",
        "allowReducedRedundancy",
        "dataEncryptionConfig",
        "dataInTransitEncryptionConfig",
    ]
    assert spec["dataEncryptionConfig"] == {
        "_typeName": "VsanDataEncryptionConfig",
        "encryptionEnabled": True,
        "kmsProviderId": {"_typeName": "KeyProviderId", "id": "kp-a"},
        "eraseDisksBeforeUse": False,
    }
    assert spec["dataInTransitEncryptionConfig"] == {
        "_typeName": "VsanDataInTransitEncryptionConfig",
        "enabled": True,
        "rekeyInterval": 1440,
    }


async def test_encryption_off_sends_only_the_flag(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    await _run(
        _vsan_cluster.vsan_encryption_set_composite,
        conn,
        cluster=CLUSTER,
        data_in_transit={"enabled": False},
    )
    spec = conn.vsan_calls[0][1]["vsanReconfigSpec"]
    assert spec == {
        "_typeName": "VimVsanReconfigSpec",
        "modify": True,
        "dataInTransitEncryptionConfig": {
            "_typeName": "VsanDataInTransitEncryptionConfig",
            "enabled": False,
        },
    }


# ===========================================================================
# vsan.encryption.rekey
# ===========================================================================


async def test_rekey_follows_the_task_to_its_end(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    out = await _run(
        _vsan_cluster.vsan_encryption_rekey_composite, conn, cluster=CLUSTER, deep_rekey=True
    )
    assert out["status"] == "rekeyed"
    assert out["task"] == "task-1"
    [(method, body)] = conn.vsan_calls
    assert method.method == "VsanEncryptedClusterRekey_Task"
    assert body == {
        "encryptedCluster": moref("ClusterComputeResource", CLUSTER),
        "deepRekey": True,
    }
    assert [c["op_id"] for c in gate.calls] == [
        "POST:/vsan/VsanVcClusterConfigSystem/{moId}/VsanEncryptedClusterRekey_Task"
    ]


async def test_rekey_task_fault_names_the_fault(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    conn.task_faults["VsanEncryptedClusterRekey_Task"] = (
        "VsanFault",
        "The cluster is not encrypted.",
    )
    with pytest.raises(RuntimeError, match="VsanFault: The cluster is not encrypted"):
        await _run(_vsan_cluster.vsan_encryption_rekey_composite, conn, cluster=CLUSTER)


async def test_rekey_poll_timeout_answers_timeout_with_the_task(gate: GateRecorder) -> None:
    conn = VsanFake()
    conn.add_cluster()
    conn.running.add("VsanEncryptedClusterRekey_Task")
    out = await _run(_vsan_cluster.vsan_encryption_rekey_composite, conn, cluster=CLUSTER)
    assert out["status"] == "timeout"
    assert out["task"] == "task-1"
    assert out["status_read"]["op_id"] == "vmware.task.info"
    assert out["issues"][0]["severity"] == "error"


async def test_rekey_unknown_cluster_sends_nothing(gate: GateRecorder) -> None:
    conn = VsanFake()
    out = await _run(_vsan_cluster.vsan_encryption_rekey_composite, conn, cluster=CLUSTER)
    assert out["status"] == "not_found"
    assert conn.vsan_calls == []


# ===========================================================================
# vsan.disk.claim
# ===========================================================================


def _claim_fake(*, soap: bool = False) -> VsanFake:
    conn = VsanFake(soap=soap)
    conn.add_host()
    conn.add_disk(_DISK_A)
    conn.add_disk(_DISK_B, ssd=True)
    conn.add_disk(_DISK_C, ssd=False)
    return conn


@pytest.mark.parametrize("soap", [False, True])
async def test_claim_esa_adds_the_disks_to_the_storage_pool(gate: GateRecorder, soap: bool) -> None:
    conn = _claim_fake(soap=soap)
    out = await _run(
        _vsan_disk_claim.vsan_disk_claim_composite,
        conn,
        host=HOST,
        architecture="esa",
        disks=[_DISK_A, _DISK_B],
    )

    assert out["status"] == "claimed"
    assert [row["name"] for row in out["disks"]] == [_DISK_A, _DISK_B]
    assert {row["vsan_state"] for row in out["disks"]} == {"inUse"}
    assert out["disks"][0]["capacity_bytes"] == 512 * 1000
    [(method, body)] = conn.vsan_calls
    assert method.method == "VsanAddStoragePoolDisk"
    assert body == {
        "specs": [
            {
                "_typeName": "VsanAddStoragePoolDiskSpec",
                "host": moref("HostSystem", HOST),
                "disks": [
                    {"_typeName": "VsanStoragePoolDisk", "diskName": name, "diskType": "singleTier"}
                    for name in (_DISK_A, _DISK_B)
                ],
            }
        ]
    }
    # The eligibility read uses the path of the release.
    query_paths = [p for p, _ in conn.calls_to("QueryDisksForVsan")]
    expected = f"/HostVsanSystem/{VSAN_SYSTEM}/QueryDisksForVsan"
    assert query_paths == [expected if soap else f"/vsan{expected}"] * 2


async def test_claim_osa_sends_the_disk_objects_of_the_eligible_disks(gate: GateRecorder) -> None:
    conn = _claim_fake()
    out = await _run(
        _vsan_disk_claim.vsan_disk_claim_composite,
        conn,
        host=HOST,
        architecture="osa",
        cache_disks=[_DISK_A],
        capacity_disks=[_DISK_C],
        osa_type="hybrid",
    )
    assert out["status"] == "claimed"
    [(method, body)] = conn.vsan_calls
    assert method.method == "InitializeDiskMappings"
    spec = body["spec"]
    assert list(spec) == ["_typeName", "host", "cacheDisks", "capacityDisks", "creationType"]
    assert spec["cacheDisks"][0]["canonicalName"] == _DISK_A
    assert spec["cacheDisks"][0]["_typeName"] == "HostScsiDisk"
    assert spec["capacityDisks"][0]["canonicalName"] == _DISK_C
    assert spec["creationType"] == "hybrid"


async def test_claim_refuses_a_disk_that_is_not_eligible(gate: GateRecorder) -> None:
    conn = _claim_fake()
    conn.disks[_DISK_B]["state"] = "ineligible"
    out = await _run(
        _vsan_disk_claim.vsan_disk_claim_composite,
        conn,
        host=HOST,
        architecture="esa",
        disks=[_DISK_A, _DISK_B],
    )
    assert out["status"] == "precondition_failed"
    assert f"{_DISK_B} (ineligible)" in out["guidance"]
    assert "vsan.disk.wipe" in out["guidance"]
    assert conn.vsan_calls == []
    assert gate.calls == []


async def test_claim_of_disks_vsan_already_uses_is_unchanged(gate: GateRecorder) -> None:
    conn = _claim_fake()
    conn.disks[_DISK_A]["state"] = "inUse"
    out = await _run(
        _vsan_disk_claim.vsan_disk_claim_composite,
        conn,
        host=HOST,
        architecture="esa",
        disks=[_DISK_A],
    )
    assert out["status"] == "unchanged"
    assert conn.vsan_calls == []


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"host": "esxi-a", "architecture": "esa", "disks": [_DISK_A]}, "invalid_request"),
        ({"host": "host-99", "architecture": "esa", "disks": [_DISK_A]}, "invalid_request"),
        ({"host": HOST, "architecture": "esa", "disks": ["naa.unknown"]}, "precondition_failed"),
        (
            {
                "host": HOST,
                "architecture": "osa",
                "cache_disks": [_DISK_A],
                "capacity_disks": [_DISK_A],
            },
            "invalid_request",
        ),
    ],
)
async def test_claim_refuses_before_any_change(
    gate: GateRecorder, params: dict[str, Any], status: str
) -> None:
    conn = _claim_fake()
    out = await _run(_vsan_disk_claim.vsan_disk_claim_composite, conn, **params)
    assert out["status"] == status
    assert conn.vsan_calls == []


async def test_claim_task_fault_names_the_fault(gate: GateRecorder) -> None:
    conn = _claim_fake()
    conn.task_faults["VsanAddStoragePoolDisk"] = ("VsanFault", "Disk is not supported by ESA.")
    with pytest.raises(RuntimeError, match="VsanFault: Disk is not supported by ESA"):
        await _run(
            _vsan_disk_claim.vsan_disk_claim_composite,
            conn,
            host=HOST,
            architecture="esa",
            disks=[_DISK_A],
        )


async def test_claim_poll_timeout_answers_timeout(gate: GateRecorder) -> None:
    conn = _claim_fake()
    conn.running.add("VsanAddStoragePoolDisk")
    out = await _run(
        _vsan_disk_claim.vsan_disk_claim_composite,
        conn,
        host=HOST,
        architecture="esa",
        disks=[_DISK_A],
    )
    assert out["status"] == "timeout"
    assert out["issues"][0]["severity"] == "error"


# ===========================================================================
# vsan.disk.wipe -- plan and preview
# ===========================================================================


def _wipe_fake(*, vmfs: list[str] | None = None) -> VsanFake:
    conn = VsanFake()
    conn.add_host(vmfs_disks=vmfs)
    conn.add_disk(_DISK_A, "ineligible", partitions=2)
    conn.add_disk(_DISK_B, "ineligible", partitions=1)
    conn.add_disk(_DISK_C, "inUse", partitions=1)
    return conn


async def test_wipe_preview_shows_each_disk_and_its_partitions() -> None:
    conn = _wipe_fake()
    preview = await _vsan_disk_wipe.vsan_disk_wipe_preview(
        preview_ctx({"host": HOST, "disks": [_DISK_A, _DISK_B]}, conn)
    )
    assert preview is not None
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"]["host"] == HOST
    assert block["object"]["host_name"] == "esxi-a"
    assert block["object"]["present"] is True
    assert block["irreversibility"] == "permanent"
    assert "cannot be undone" in block["effect"]
    assert [child["name"] for child in block["children"]] == [_DISK_A, _DISK_B]
    assert [p["partition"] for p in block["children"][0]["partitions"]] == [1, 2]
    assert "refusal" not in block
    # The preview only reads.
    assert conn.calls_to("UpdateDiskPartitions") == []
    assert conn.vsan_calls == []


async def test_wipe_preview_refuses_a_host_that_does_not_exist() -> None:
    conn = _wipe_fake()
    preview = await _vsan_disk_wipe.vsan_disk_wipe_preview(
        preview_ctx({"host": "host-99", "disks": [_DISK_A]}, conn)
    )
    assert preview is not None
    block = preview["blast_radius"]
    assert block["object"]["present"] is False
    assert block["refusal"]["status"] == "invalid_request"
    assert "host-99" in block["refusal"]["guidance"]


async def test_wipe_preview_refuses_a_disk_that_does_not_exist() -> None:
    conn = _wipe_fake()
    preview = await _vsan_disk_wipe.vsan_disk_wipe_preview(
        preview_ctx({"host": HOST, "disks": [_DISK_A, "naa.unknown"]}, conn)
    )
    assert preview is not None
    block = preview["blast_radius"]
    assert block["refusal"]["status"] == "precondition_failed"
    assert block["blockers"] == [
        {"disk": "naa.unknown", "reason": "the host has no disk with this name"}
    ]


async def test_wipe_preview_declines_without_a_connector() -> None:
    assert (
        await _vsan_disk_wipe.vsan_disk_wipe_preview(
            preview_ctx({"host": HOST, "disks": [_DISK_A]}, None)
        )
        is None
    )


# ===========================================================================
# vsan.disk.wipe -- the handler
# ===========================================================================


async def test_wipe_erases_each_partition_table_in_turn(gate: GateRecorder) -> None:
    conn = _wipe_fake()
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite, conn, host=HOST, disks=[_DISK_A, _DISK_B]
    )

    assert out["status"] == "wiped"
    assert [s["status"] for s in out["steps"]] == ["done", "done"]
    calls = conn.calls_to("UpdateDiskPartitions")
    assert [path for path, _ in calls] == [
        f"/HostStorageSystem/{STORAGE_SYSTEM}/UpdateDiskPartitions"
    ] * 2
    assert [body for _, body in calls] == [
        {
            "devicePath": f"/vmfs/devices/disks/{name}",
            "spec": {"_typeName": "HostDiskPartitionSpec"},
        }
        for name in (_DISK_A, _DISK_B)
    ]
    assert [c["op_id"] for c in gate.calls] == [
        "POST:/HostStorageSystem/{moId}/UpdateDiskPartitions"
    ] * 2
    assert out["disks_after"] is not None
    assert [row["name"] for row in out["disks_after"]] == [_DISK_A, _DISK_B]


async def test_wipe_failure_at_step_two_lists_what_ran_and_what_did_not(
    gate: GateRecorder,
) -> None:
    conn = _wipe_fake()
    conn.add_disk("naa.0000000000000d", "ineligible", partitions=1)
    conn.partition_faults[f"/vmfs/devices/disks/{_DISK_B}"] = RuntimeError(
        "ResourceInUse: The resource 'disk' is in use."
    )
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite,
        conn,
        host=HOST,
        disks=[_DISK_A, _DISK_B, "naa.0000000000000d"],
    )
    assert out["status"] == "failed_part_way"
    assert [(s["disk"], s["status"]) for s in out["steps"]] == [
        (_DISK_A, "done"),
        (_DISK_B, "failed"),
        ("naa.0000000000000d", "not_run"),
    ]
    assert out["steps"][1]["error"] == "ResourceInUse: The resource 'disk' is in use."
    assert out["issues"][0]["severity"] == "error"
    # Nothing is undone, and the disk after the failure is not touched.
    assert conn.partitions[f"/vmfs/devices/disks/{_DISK_A}"] == []
    assert conn.partitions["/vmfs/devices/disks/naa.0000000000000d"] != []


@pytest.mark.parametrize(
    ("disk", "reason"),
    [
        (_DISK_C, "vSAN uses this disk; remove it from vSAN first"),
        (_DISK_B, f"the disk holds the VMFS datastore 'datastore-on-{_DISK_B}'"),
    ],
)
async def test_wipe_refuses_a_disk_in_use(gate: GateRecorder, disk: str, reason: str) -> None:
    conn = _wipe_fake(vmfs=[_DISK_B])
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite, conn, host=HOST, disks=[_DISK_A, disk]
    )
    assert out["status"] == "precondition_failed"
    assert {"disk": disk, "reason": reason} in out["blockers"]
    assert conn.calls_to("UpdateDiskPartitions") == []
    assert gate.calls == []


async def test_wipe_of_disks_without_partitions_is_unchanged(gate: GateRecorder) -> None:
    conn = _wipe_fake()
    conn.add_disk("naa.0000000000000e", "eligible", partitions=0)
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite, conn, host=HOST, disks=["naa.0000000000000e"]
    )
    assert out["status"] == "unchanged"
    assert conn.calls_to("UpdateDiskPartitions") == []


async def test_wipe_returns_a_parked_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = parked("POST:/HostStorageSystem/{moId}/UpdateDiskPartitions")
    monkeypatch.setattr(_write, "enforce_subop_policy", GateRecorder(waiting))
    conn = _wipe_fake()
    out = await _run(_vsan_disk_wipe.vsan_disk_wipe_composite, conn, host=HOST, disks=[_DISK_A])
    assert out is waiting
    assert conn.calls_to("UpdateDiskPartitions") == []


async def test_secure_wipe_runs_on_the_hosts_vsan_system_ex(gate: GateRecorder) -> None:
    conn = _wipe_fake()
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite,
        conn,
        host=HOST,
        disks=[_DISK_A, _DISK_B],
        method="secure",
    )
    assert out["status"] == "wiped"
    assert out["task"] == "task-1"
    methods = [(m.mo_type, m.moid, m.method) for m, _ in conn.vsan_calls]
    assert methods == [
        ("VsanSystemEx", "vsanSystemEx-12", "VsanHostQueryWipeDisk"),
        ("VsanSystemEx", "vsanSystemEx-12", "VsanHostWipeDisk"),
        ("VsanSystemEx", "vsanSystemEx-12", "VsanHostQueryWipeDisk"),
    ]
    assert conn.vsan_calls[1][1] == {"disks": [_DISK_A, _DISK_B]}
    assert [row["wipe_state"] for row in out["disks_after"]] == ["Success", "Success"]
    assert [c["op_id"] for c in gate.calls] == ["POST:/vsan/VsanSystemEx/{moId}/VsanHostWipeDisk"]
    assert conn.calls_to("UpdateDiskPartitions") == []


async def test_secure_wipe_refuses_a_disk_vsan_cannot_wipe(gate: GateRecorder) -> None:
    conn = _wipe_fake()
    conn.wipe_eligible[_DISK_B] = "No"
    out = await _run(
        _vsan_disk_wipe.vsan_disk_wipe_composite,
        conn,
        host=HOST,
        disks=[_DISK_A, _DISK_B],
        method="secure",
    )
    assert out["status"] == "precondition_failed"
    assert {"disk": _DISK_B, "reason": "vSAN says this disk cannot be securely wiped"} in out[
        "blockers"
    ]
    assert conn.vsan_methods() == ["VsanHostQueryWipeDisk"]


async def test_secure_wipe_task_fault_names_the_fault(gate: GateRecorder) -> None:
    conn = _wipe_fake()
    conn.task_faults["VsanHostWipeDisk"] = ("VsanFault", "Secure wipe is not supported.")
    with pytest.raises(RuntimeError, match="VsanFault: Secure wipe is not supported"):
        await _run(
            _vsan_disk_wipe.vsan_disk_wipe_composite,
            conn,
            host=HOST,
            disks=[_DISK_A],
            method="secure",
        )


# ===========================================================================
# Helpers and schemas
# ===========================================================================


def test_vsan_system_ex_moid_follows_the_host_moid() -> None:
    method = host_vsan_system_ex(VSAN_HOST_WIPE_DISK, "host-1234")
    assert method.moid == "vsanSystemEx-1234"
    assert vsan_op_id(method) == "POST:/vsan/VsanSystemEx/{moId}/VsanHostWipeDisk"
    with pytest.raises(ValueError, match="host-12"):
        host_vsan_system_ex(VSAN_HOST_WIPE_DISK, "esxi-a")


def test_examples_and_either_or_rules_of_the_schemas() -> None:
    from meho_backplane.connectors.vmware_rest.composites import _vsan_schemas as schemas

    configure = schemas.VSAN_CLUSTER_CONFIGURE_PARAMETER_SCHEMA
    assert validate_params(configure, {"cluster": CLUSTER, "vsan_enabled": True}) == []
    assert validate_params(configure, {"cluster": CLUSTER}) != []
    encryption = schemas.VSAN_ENCRYPTION_SET_PARAMETER_SCHEMA
    assert validate_params(encryption, {"cluster": CLUSTER}) != []
    assert (
        validate_params(encryption, {"cluster": CLUSTER, "data_in_transit": {"enabled": False}})
        == []
    )
    claim = schemas.VSAN_DISK_CLAIM_PARAMETER_SCHEMA
    assert validate_params(claim, {"host": HOST, "architecture": "esa", "disks": [_DISK_A]}) == []
    assert validate_params(claim, {"host": HOST, "architecture": "esa"}) != []
    assert (
        validate_params(
            claim,
            {
                "host": HOST,
                "architecture": "osa",
                "cache_disks": [_DISK_A],
                "capacity_disks": [_DISK_B],
            },
        )
        == []
    )
    assert validate_params(claim, {"host": HOST, "architecture": "osa", "disks": [_DISK_A]}) != []
    wipe = schemas.VSAN_DISK_WIPE_PARAMETER_SCHEMA
    assert validate_params(wipe, {"host": HOST, "disks": [_DISK_A], "method": "secure"}) == []
    assert validate_params(wipe, {"host": HOST, "disks": []}) != []
    for schema in (
        configure,
        encryption,
        claim,
        wipe,
        schemas.VSAN_ENCRYPTION_REKEY_PARAMETER_SCHEMA,
    ):
        for example in schema["examples"]:
            assert validate_params(schema, example) == []
            # The ids are placeholders, never values that look real.
            ids = [example.get(k) for k in ("cluster", "host")] + list(example.get("disks", []))
            assert all(v.startswith("<") for v in ids if v is not None), example


def test_no_answer_field_carries_a_secret() -> None:
    """The answers name settings and ids only; no input or output field is a secret."""
    from meho_backplane.connectors.vmware_rest.composites import _vsan_schemas as schemas

    words = ("password", "secret", "token", "private", "licen")
    for name in dir(schemas):
        if name.endswith("_SCHEMA"):
            text = repr(getattr(schemas, name)).lower()
            assert not any(word in text for word in words), name
