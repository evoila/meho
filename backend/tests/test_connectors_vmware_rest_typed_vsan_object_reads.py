# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.resync.status``, ``.object.health`` and ``.maintenance.precheck``.

The real connector runs over a mocked wire (:mod:`tests._vsan_wire`): vCenter
9.1 answers the vSAN methods as VI-JSON, vCenter 8.0.3 as SOAP on
``/vsanHealth``. Property reads and the vCenter task poll use VI-JSON on
both. The answers are hand-made in the shape of the vendor types, with
made-up names and ids. The last part checks the metadata of all six vSAN
reads.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx

from meho_backplane.connectors.vmware_rest import typed_ops_vsan_precheck
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import VSAN_GROUP_KEY
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_objects import health_group
from meho_backplane.operations._validate import validate_params
from tests._vsan_wire import (
    BASE,
    RELEASE_80,
    RELEASE_91,
    FakeVcenter,
    StubTarget,
    close,
    make_operator,
    moref,
    new_connector,
    soap_answer,
    soap_envelope,
    vsan_settings_env,
)

pytestmark = pytest.mark.usefixtures(vsan_settings_env.__name__)

_CLUSTER = "domain-c7"
_CLUSTER_REF = moref("ClusterComputeResource", _CLUSTER)
_OBJ_1 = "7b000000-0000-0000-0000-000000000001"
_OBJ_2 = "7b000000-0000-0000-0000-000000000002"
_OBJ_OK = "7b000000-0000-0000-0000-0000000000aa"
_NODE = "6a000000-0000-0000-0000-000000000001"
_CHECKED_AT = "2026-10-30T10:00:00Z"


async def _run(fake: FakeVcenter, handler: str, params: dict[str, Any]) -> dict[str, Any]:
    connector = new_connector()
    try:
        async with respx.mock(base_url=BASE, assert_all_called=False) as mock:
            fake.install(mock)
            result: dict[str, Any] = await getattr(connector, handler)(
                make_operator(), StubTarget(), params
            )
            return result
    finally:
        await close(connector)


# ---------------------------------------------------------------------------
# vmware.vsan.resync.status
# ---------------------------------------------------------------------------

_SYNC_VIJSON: dict[str, Any] = {
    "_typeName": "VsanHostVsanObjectSyncQueryResult",
    "totalObjectsToSync": 2,
    "totalBytesToSync": 3000,
    "totalRecoveryETA": 60,
    "syncingObjectRecoveryDetails": {
        "_typeName": "VsanSyncingObjectRecoveryDetails",
        "activeObjectsToSync": 1,
        "bytesToSyncForActiveObjects": 1000,
        "activelySyncingObjectRecoveryETA": 20,
        "queuedObjectsToSync": 1,
        "bytesToSyncForQueuedObjects": 2000,
        "queuedForSyncObjectRecoveryETA": 60,
    },
    "objects": [
        {
            "_typeName": "VsanHostVsanObjectSyncState",
            "uuid": _OBJ_1,
            "components": [
                {
                    "_typeName": "VsanHostComponentSyncState",
                    "uuid": "c-1",
                    "diskUuid": "d-1",
                    "hostUuid": _NODE,
                    "bytesToSync": 1000,
                    "recoveryETA": 20,
                    "reasons": ["evacuate", "rebalance"],
                }
            ],
        }
    ],
}


@pytest.mark.asyncio
async def test_resync_on_vcenter_9_reads_totals_and_component_rows() -> None:
    fake = FakeVcenter(RELEASE_91, {"QuerySyncingVsanObjectsSummary": _SYNC_VIJSON})
    result = await _run(
        fake, "vsan_resync_status", {"cluster": _CLUSTER, "reason": "evacuate", "max_objects": 10}
    )

    assert result == {
        "cluster": _CLUSTER,
        "objects_left": 2,
        "bytes_left": 3000,
        "seconds_left": 60,
        "by_status": [
            {"status": "active", "objects": 1, "bytes_left": 1000, "seconds_left": 20},
            {"status": "queued", "objects": 1, "bytes_left": 2000, "seconds_left": 60},
            {"status": "suspended", "objects": None, "bytes_left": None, "seconds_left": None},
        ],
        "components": [
            {
                "object_uuid": _OBJ_1,
                "component_uuid": "c-1",
                "host_uuid": _NODE,
                "disk_uuid": "d-1",
                "bytes_left": 1000,
                "seconds_left": 20,
                "reasons": "evacuate, rebalance",
            }
        ],
    }
    assert fake.calls_of("QuerySyncingVsanObjectsSummary")[0].json() == {
        "cluster": _CLUSTER_REF,
        "syncingObjectFilter": {
            "_typeName": "VsanSyncingObjectFilter",
            "resyncType": "evacuate",
            "numberOfObjects": 10,
        },
    }


@pytest.mark.asyncio
async def test_resync_on_vcenter_8_sends_the_filter_in_soap() -> None:
    answer = soap_answer(
        "QuerySyncingVsanObjectsSummary",
        "<totalObjectsToSync>0</totalObjectsToSync><totalBytesToSync>0</totalBytesToSync>",
    )
    fake = FakeVcenter(RELEASE_80, {"QuerySyncingVsanObjectsSummary": answer})
    result = await _run(fake, "vsan_resync_status", {"cluster": _CLUSTER, "status": "active"})

    assert result["objects_left"] == 0
    assert result["bytes_left"] == 0
    assert result["components"] == []
    body = fake.calls_of("QuerySyncingVsanObjectsSummary")[0].body
    assert '<_this type="VsanObjectSystem">vsan-cluster-object-system</_this>' in body
    assert (
        "<syncingObjectFilter><resyncStatus>active</resyncStatus>"
        "<numberOfObjects>100</numberOfObjects></syncingObjectFilter>"
    ) in body


# ---------------------------------------------------------------------------
# vmware.vsan.object.health
# ---------------------------------------------------------------------------

_OBJECT_HEALTH_VIJSON: dict[str, Any] = {
    "_typeName": "VsanClusterHealthSummary",
    "timestamp": _CHECKED_AT,
    "objectHealth": {
        "_typeName": "VsanObjectOverallHealth",
        "objectHealthDetail": [
            {"_typeName": "VsanObjectHealth", "health": "healthy", "numObjects": 40,
             "objUuids": [_OBJ_OK]},
            {"_typeName": "VsanObjectHealth", "health": "reducedavailabilitywithnorebuild",
             "numObjects": 1, "objUuids": [_OBJ_1]},
            {"_typeName": "VsanObjectHealth", "health": "inaccessible", "numObjects": 1,
             "objUuids": [_OBJ_2]},
        ],
    },
}  # fmt: skip

_IDENTITIES_VIJSON: dict[str, Any] = {
    "_typeName": "VsanObjectIdentityAndHealth",
    "identities": [
        {
            "_typeName": "VsanObjectIdentity",
            "uuid": _OBJ_1,
            "type": "vdisk",
            "vm": moref("VirtualMachine", "vm-42"),
            "vmInstanceUuid": "<vm-instance-uuid>",
            "description": "[vsanDatastore] <vm-name>/<vm-name>.vmdk",
            "spbmProfileName": "<policy-name>",
        }
    ],
}


@pytest.mark.asyncio
async def test_object_health_on_vcenter_9_counts_states_and_names_the_vms() -> None:
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanQueryVcClusterHealthSummary": _OBJECT_HEALTH_VIJSON,
            "VsanQueryObjectIdentities": _IDENTITIES_VIJSON,
        },
    )
    result = await _run(fake, "vsan_object_health", {"cluster": _CLUSTER})

    assert result["summary"] == {
        "healthy": 40,
        "reduced_redundancy": 1,
        "inaccessible": 1,
        "other": 0,
    }
    assert result["by_state"][1] == {
        "health": "reducedavailabilitywithnorebuild",
        "health_group": "reduced_redundancy",
        "objects": 1,
    }
    assert result["unhealthy_objects"] == [
        {
            "object_uuid": _OBJ_1,
            "health": "reducedavailabilitywithnorebuild",
            "health_group": "reduced_redundancy",
            "object_type": "vdisk",
            "vm": "vm-42",
            "vm_instance_uuid": "<vm-instance-uuid>",
            "description": "[vsanDatastore] <vm-name>/<vm-name>.vmdk",
            "storage_policy": "<policy-name>",
        },
        {
            "object_uuid": _OBJ_2,
            "health": "inaccessible",
            "health_group": "inaccessible",
            "object_type": None,
            "vm": None,
            "vm_instance_uuid": None,
            "description": None,
            "storage_policy": None,
        },
    ]
    assert result["checked_at"] == _CHECKED_AT
    assert fake.calls_of("VsanQueryVcClusterHealthSummary")[0].json() == {
        "cluster": _CLUSTER_REF,
        "includeObjUuids": True,
        "fields": ["objectHealth"],
    }
    assert fake.calls_of("VsanQueryObjectIdentities")[0].json() == {
        "cluster": _CLUSTER_REF,
        "objUuids": [_OBJ_1, _OBJ_2],
        "includeHealth": True,
        "includeObjIdentity": True,
    }


@pytest.mark.asyncio
async def test_object_health_on_vcenter_8_all_healthy_sends_no_identity_call() -> None:
    summary = soap_answer(
        "VsanQueryVcClusterHealthSummary",
        "<objectHealth><objectHealthDetail><numObjects>12</numObjects><health>healthy</health>"
        f"<objUuids>{_OBJ_OK}</objUuids></objectHealthDetail></objectHealth>",
    )
    fake = FakeVcenter(RELEASE_80, {"VsanQueryVcClusterHealthSummary": summary})
    result = await _run(fake, "vsan_object_health", {"cluster": _CLUSTER, "from_cache": True})

    assert result["summary"]["healthy"] == 12
    assert result["unhealthy_objects"] == []
    assert result["from_cache"] is True
    assert [call.method for call in fake.vsan_calls] == ["VsanQueryVcClusterHealthSummary"]
    assert (
        "<includeObjUuids>true</includeObjUuids><fields>objectHealth</fields>"
        "<fetchFromCache>true</fetchFromCache>"
    ) in fake.vsan_calls[0].body


@pytest.mark.asyncio
async def test_object_health_on_vcenter_8_sends_the_object_uuids_in_soap() -> None:
    summary = soap_answer(
        "VsanQueryVcClusterHealthSummary",
        "<objectHealth><objectHealthDetail><numObjects>1</numObjects><health>inaccessible"
        f"</health><objUuids>{_OBJ_2}</objUuids></objectHealthDetail></objectHealth>",
    )
    identities = soap_answer(
        "VsanQueryObjectIdentities",
        f"<identities><uuid>{_OBJ_2}</uuid><type>vmswap</type>"
        '<vm type="VirtualMachine">vm-43</vm></identities>',
    )
    fake = FakeVcenter(
        RELEASE_80,
        {"VsanQueryVcClusterHealthSummary": summary, "VsanQueryObjectIdentities": identities},
    )
    result = await _run(fake, "vsan_object_health", {"cluster": _CLUSTER})

    assert result["summary"]["inaccessible"] == 1
    assert result["unhealthy_objects"][0]["vm"] == "vm-43"
    assert result["unhealthy_objects"][0]["object_type"] == "vmswap"
    body = fake.calls_of("VsanQueryObjectIdentities")[0].body
    assert (
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster><objUuids>{_OBJ_2}</objUuids>'
        "<includeHealth>true</includeHealth><includeObjIdentity>true</includeObjIdentity>"
    ) in body


@pytest.mark.asyncio
async def test_object_health_without_object_health_fails_loudly() -> None:
    fake = FakeVcenter(
        RELEASE_91, {"VsanQueryVcClusterHealthSummary": {"_typeName": "VsanClusterHealthSummary"}}
    )
    with pytest.raises(RuntimeError, match="no objectHealth"):
        await _run(fake, "vsan_object_health", {"cluster": _CLUSTER})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_health", "message"),
    [
        ({"_typeName": "VsanObjectOverallHealth"}, "no objectHealthDetail"),
        (
            {"objectHealthDetail": [{"_typeName": "VsanObjectHealth", "health": "healthy"}]},
            "no object count",
        ),
    ],
)
async def test_object_health_with_empty_object_health_fails_loudly(
    object_health: dict[str, Any], message: str
) -> None:
    summary = {"_typeName": "VsanClusterHealthSummary", "objectHealth": object_health}
    fake = FakeVcenter(RELEASE_91, {"VsanQueryVcClusterHealthSummary": summary})
    with pytest.raises(RuntimeError, match=message):
        await _run(fake, "vsan_object_health", {"cluster": _CLUSTER})


@pytest.mark.asyncio
async def test_object_health_reporting_zero_objects_is_a_valid_answer() -> None:
    summary = {
        "_typeName": "VsanClusterHealthSummary",
        "objectHealth": {
            "objectHealthDetail": [
                {"_typeName": "VsanObjectHealth", "health": "healthy", "numObjects": 0}
            ]
        },
    }
    fake = FakeVcenter(RELEASE_91, {"VsanQueryVcClusterHealthSummary": summary})
    result = await _run(fake, "vsan_object_health", {"cluster": _CLUSTER})

    assert result["summary"] == {
        "healthy": 0,
        "reduced_redundancy": 0,
        "inaccessible": 0,
        "other": 0,
    }
    assert result["unhealthy_objects"] == []


@pytest.mark.parametrize(
    ("state", "group"),
    [
        ("healthy", "healthy"),
        ("inaccessible", "inaccessible"),
        ("reducedavailabilitywithactiverebuild", "reduced_redundancy"),
        ("datamove", "other"),
        (None, "other"),
    ],
)
def test_health_group(state: str | None, group: str) -> None:
    assert health_group(state) == group


# ---------------------------------------------------------------------------
# vmware.vsan.maintenance.precheck
# ---------------------------------------------------------------------------

_TASK = "task-77"


def _precheck_props(task_state: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {"_typeName": "TaskInfo", "key": _TASK, "state": task_state}
    if error is not None:
        info["error"] = error
    return {
        "HostSystem": {"host-21": {"parent": moref("ClusterComputeResource", _CLUSTER)}},
        "ClusterComputeResource": {
            _CLUSTER: {
                "configurationEx": {
                    "_typeName": "ClusterConfigInfoEx",
                    "vsanHostConfig": [
                        {
                            "_typeName": "VsanHostConfigInfo",
                            "hostSystem": moref("HostSystem", "host-21"),
                            "clusterInfo": {"nodeUuid": _NODE},
                        }
                    ],
                }
            }
        },
        "Task": {_TASK: {"info": info}},
    }


_STATUS_VIJSON: dict[str, Any] = {
    "_typeName": "VsanResourceCheckStatus",
    "status": "resourceCheckCompleted",
    "result": {
        "_typeName": "VsanResourceCheckResult",
        "timestamp": _CHECKED_AT,
        "status": "red",
        "dataToMove": 5000,
        "inaccessibleObjects": [_OBJ_1],
        "messages": [{"_typeName": "LocalizableMessage", "key": "k", "message": "Not enough room"}],
        "faultDomains": [
            {
                "_typeName": "VsanFaultDomainResourceCheckResult",
                "name": "<site-a>",
                "capacity": 100,
                "hosts": [
                    {
                        "_typeName": "VsanHostResourceCheckResult",
                        "name": "<host-name-1>",
                        "host": moref("HostSystem", "host-21"),
                        "usedCapacity": 40,
                        "postOperationUsedCapacity": 0,
                    }
                ],
            }
        ],
    },
}

_SPEC_JSON: dict[str, Any] = {
    "resourceCheckSpec": {
        "_typeName": "VsanResourceCheckSpec",
        "operation": "EnterMaintenanceMode",
        "entities": [_NODE],
        "maintenanceSpec": {
            "_typeName": "HostMaintenanceSpec",
            "vsanMode": {
                "_typeName": "VsanHostDecommissionMode",
                "objectAction": "evacuateAllData",
            },
        },
    },
    "cluster": _CLUSTER_REF,
}


@pytest.fixture
def _instant_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typed_ops_vsan_precheck, "POLL_INTERVAL_SECONDS", 0.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_poll")
async def test_precheck_on_vcenter_9_runs_the_check_task_and_reads_the_result() -> None:
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerformResourceCheck": moref("Task", _TASK),
            "VsanGetResourceCheckStatus": _STATUS_VIJSON,
        },
        _precheck_props("success"),
    )
    result = await _run(
        fake, "vsan_maintenance_precheck", {"host": "host-21", "vsan_mode": "evacuateAllData"}
    )

    assert result["cluster"] == _CLUSTER
    assert result["check_status"] == "resourceCheckCompleted"
    assert result["result"] == "red"
    assert result["data_to_move_bytes"] == 5000
    assert result["inaccessible_objects"] == 1
    assert result["inaccessible_object_uuids"] == [_OBJ_1]
    assert result["messages"] == ["Not enough room"]
    assert [(row["level"], row["name"]) for row in result["entities"]] == [
        ("fault_domain", "<site-a>"),
        ("host", "<host-name-1>"),
    ]
    assert result["entities"][1]["host"] == "host-21"
    assert result["task"] == _TASK
    assert result["note"] is None
    assert fake.calls_of("VsanPerformResourceCheck")[0].json() == _SPEC_JSON
    assert fake.calls_of("VsanGetResourceCheckStatus")[0].json() == _SPEC_JSON
    read_types = [read["specSet"][0]["propSet"][0]["type"] for read in fake.property_reads]
    assert read_types == ["HostSystem", "ClusterComputeResource", "Task"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_poll")
async def test_precheck_on_vcenter_8_sends_the_spec_in_soap() -> None:
    status = soap_answer(
        "VsanGetResourceCheckStatus",
        "<status>resourceCheckCompleted</status><result><timestamp>"
        f"{_CHECKED_AT}</timestamp><status>green</status><dataToMove>0</dataToMove></result>",
    )
    fake = FakeVcenter(
        RELEASE_80,
        {
            "VsanPerformResourceCheck": soap_envelope(
                '<VsanPerformResourceCheckResponse xmlns="urn:vsan">'
                f'<returnval type="Task">{_TASK}</returnval></VsanPerformResourceCheckResponse>'
            ),
            "VsanGetResourceCheckStatus": status,
        },
        _precheck_props("success"),
    )
    result = await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})

    assert result["result"] == "green"
    assert result["vsan_mode"] == "ensureObjectAccessibility"
    assert result["data_to_move_bytes"] == 0
    body = fake.calls_of("VsanPerformResourceCheck")[0].body
    assert (
        "<resourceCheckSpec><operation>EnterMaintenanceMode</operation>"
        f"<entities>{_NODE}</entities><maintenanceSpec><vsanMode><objectAction>"
        "ensureObjectAccessibility</objectAction></vsanMode></maintenanceSpec>"
        f'</resourceCheckSpec><cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
    ) in body
    assert (
        '<_this type="VsanResourceCheckSystem">vsan-cluster-resource-check-system</_this>' in body
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_poll")
async def test_precheck_task_error_fails_with_the_vendor_text() -> None:
    error = {"_typeName": "LocalizedMethodFault", "localizedMessage": "vSAN check failed"}
    fake = FakeVcenter(
        RELEASE_91,
        {"VsanPerformResourceCheck": moref("Task", _TASK)},
        _precheck_props("error", error),
    )
    with pytest.raises(RuntimeError, match="vSAN check failed"):
        await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})
    assert fake.calls_of("VsanGetResourceCheckStatus") == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_poll")
async def test_precheck_task_still_running_returns_the_status_with_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typed_ops_vsan_precheck, "CHECK_TIMEOUT_SECONDS", 0.0)
    running = {"_typeName": "VsanResourceCheckStatus", "status": "resourceCheckRunning"}
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerformResourceCheck": moref("Task", _TASK),
            "VsanGetResourceCheckStatus": running,
        },
        _precheck_props("running"),
    )
    result = await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})

    assert result["check_status"] == "resourceCheckRunning"
    assert result["result"] is None
    assert result["note"] is not None and _TASK in result["note"]


@pytest.mark.asyncio
async def test_precheck_host_outside_a_cluster_fails() -> None:
    props = {"HostSystem": {"host-30": {"parent": moref("ComputeResource", "domain-s1")}}}
    fake = FakeVcenter(RELEASE_91, {}, props)
    with pytest.raises(RuntimeError, match="not in a cluster"):
        await _run(fake, "vsan_maintenance_precheck", {"host": "host-30"})
    assert fake.vsan_calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_poll")
async def test_precheck_check_that_did_not_complete_fails_with_the_status() -> None:
    failed = {"_typeName": "VsanResourceCheckStatus", "status": "resourceCheckFailed"}
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerformResourceCheck": moref("Task", _TASK),
            "VsanGetResourceCheckStatus": failed,
        },
        _precheck_props("success"),
    )
    with pytest.raises(RuntimeError, match="resourceCheckFailed"):
        await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})


@pytest.mark.asyncio
async def test_precheck_completed_without_result_fails_loudly() -> None:
    completed = {"_typeName": "VsanResourceCheckStatus", "status": "resourceCheckCompleted"}
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerformResourceCheck": moref("Task", _TASK),
            "VsanGetResourceCheckStatus": completed,
        },
        _precheck_props("success"),
    )
    with pytest.raises(RuntimeError, match="sent no result"):
        await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})


@pytest.mark.asyncio
async def test_precheck_completed_with_empty_result_fails_loudly() -> None:
    completed = {
        "_typeName": "VsanResourceCheckStatus",
        "status": "resourceCheckCompleted",
        "result": {},
    }
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerformResourceCheck": moref("Task", _TASK),
            "VsanGetResourceCheckStatus": completed,
        },
        _precheck_props("success"),
    )
    with pytest.raises(RuntimeError, match="result has no status"):
        await _run(fake, "vsan_maintenance_precheck", {"host": "host-21"})


# ---------------------------------------------------------------------------
# Metadata of the six vSAN reads
# ---------------------------------------------------------------------------

_OP_IDS = {
    "vmware.vsan.cluster.info",
    "vmware.vsan.capacity",
    "vmware.vsan.performance",
    "vmware.vsan.resync.status",
    "vmware.vsan.object.health",
    "vmware.vsan.maintenance.precheck",
}


# The vSAN reads, as the typed-op registry collected them from their modules.
VSAN_READ_OPS = tuple(op for op in VMWARE_TYPED_OPS if op.group_key == VSAN_GROUP_KEY)


def test_the_six_reads_are_registered_typed_ops_in_one_group() -> None:
    assert {op.op_id for op in VSAN_READ_OPS} == _OP_IDS
    for op in VSAN_READ_OPS:
        assert op.group_key == VSAN_GROUP_KEY
        assert op.safety_level == "safe"
        assert op.requires_approval is False


def test_group_text_names_each_read_and_claims_no_group_safety() -> None:
    text = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[VSAN_GROUP_KEY]
    for op_id in _OP_IDS:
        assert op_id in text
    assert "Read-only." not in text
    assert "every op" not in text.lower()


@pytest.mark.parametrize("op", VSAN_READ_OPS, ids=lambda op: op.op_id)
def test_examples_validate_and_use_placeholders(op: Any) -> None:
    examples = op.parameter_schema["examples"]
    assert examples
    for example in examples:
        assert validate_params(op.parameter_schema, example) == []
        for key in ("cluster", "host"):
            if key in example:
                assert example[key].startswith("<"), "examples use neutral placeholders"
    assert len(op.summary) <= 120
    assert len(op.description) <= 300
    for key in ("when_to_call", "output_shape", "next_step"):
        assert op.llm_instructions[key]


def test_performance_parameters_reject_an_unknown_entity_type() -> None:
    op = next(op for op in VSAN_READ_OPS if op.op_id == "vmware.vsan.performance")
    assert validate_params(op.parameter_schema, {"cluster": "<cluster-id>", "entity_type": "x"})
