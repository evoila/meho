# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.cluster.info``, ``vmware.vsan.capacity`` and ``vmware.vsan.performance``.

The real connector runs over a mocked wire (:mod:`tests._vsan_wire`): vCenter
9.1 answers the vSAN methods as VI-JSON on ``/sdk/vim25/9.1.0.0/vsan/...``,
vCenter 8.0.3 as SOAP on ``/vsanHealth``. The answers are hand-made in the
shape of the vendor types of the vCenter 9.0 VI/JSON file and the vSAN
Management SDK 8.0, with made-up names and ids.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_performance import NO_SAMPLES_NOTE
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
    soap_fault,
    vsan_settings_env,
)

pytestmark = pytest.mark.usefixtures(vsan_settings_env.__name__)

_CLUSTER = "domain-c7"
_CLUSTER_REF = moref("ClusterComputeResource", _CLUSTER)
_CLUSTER_UUID = "52aa0000-0000-0000-0000-0000000000c1"
_NODE_1 = "6a000000-0000-0000-0000-000000000001"
_NODE_2 = "6a000000-0000-0000-0000-000000000002"
_WITNESS_NODE = "6a000000-0000-0000-0000-0000000000ff"
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
# vmware.vsan.cluster.info
# ---------------------------------------------------------------------------

_CONFIG_EX_VIJSON: dict[str, Any] = {
    "_typeName": "VsanConfigInfoEx",
    "enabled": True,
    "vsanEsaEnabled": True,
    "defaultConfig": {"_typeName": "VsanClusterConfigInfoHostDefaultInfo", "uuid": _CLUSTER_UUID},
    "dataEfficiencyConfig": {
        "_typeName": "VsanDataEfficiencyConfig",
        "dedupEnabled": False,
        "compressionEnabled": True,
    },
    "dataEncryptionConfig": {
        "_typeName": "VsanDataEncryptionConfig",
        "encryptionEnabled": True,
        "kmsProviderId": {"_typeName": "KeyProviderId", "id": "<key-provider>"},
        "kekId": "<kek-id-must-not-leak>",
        "hostKeyId": "<host-key-must-not-leak>",
    },
    "dataInTransitEncryptionConfig": {
        "_typeName": "VsanDataInTransitEncryptionConfig",
        "enabled": False,
    },
}

_CONFIG_EX_SOAP = soap_answer(
    "VsanClusterGetConfig",
    "<enabled>true</enabled>"
    f"<defaultConfig><uuid>{_CLUSTER_UUID}</uuid></defaultConfig>"
    "<vsanEsaEnabled>false</vsanEsaEnabled>"
    "<dataEfficiencyConfig><dedupEnabled>true</dedupEnabled>"
    "<compressionEnabled>true</compressionEnabled></dataEfficiencyConfig>"
    "<dataEncryptionConfig><encryptionEnabled>false</encryptionEnabled></dataEncryptionConfig>",
)


def _host_config(host: str, node: str, fault_domain: str) -> dict[str, Any]:
    return {
        "_typeName": "VsanHostConfigInfo",
        "hostSystem": moref("HostSystem", host),
        "enabled": True,
        "clusterInfo": {"_typeName": "VsanHostConfigInfoClusterInfo", "nodeUuid": node},
        "faultDomainInfo": {"_typeName": "VsanHostFaultDomainInfo", "name": fault_domain},
    }


_CLUSTER_PROPS: dict[str, dict[str, dict[str, Any]]] = {
    "ClusterComputeResource": {
        _CLUSTER: {
            "configurationEx": {
                "_typeName": "ClusterConfigInfoEx",
                "vsanHostConfig": [
                    _host_config("host-21", _NODE_1, "<site-a>"),
                    _host_config("host-22", _NODE_2, "<site-b>"),
                ],
            }
        }
    },
    "HostSystem": {"host-21": {"name": "<host-name-1>"}, "host-22": {"name": "<host-name-2>"}},
}

_EXPECTED_HOSTS = [
    {
        "host": "host-21",
        "name": "<host-name-1>",
        "vsan_enabled": True,
        "fault_domain": "<site-a>",
        "node_uuid": _NODE_1,
    },
    {
        "host": "host-22",
        "name": "<host-name-2>",
        "vsan_enabled": True,
        "fault_domain": "<site-b>",
        "node_uuid": _NODE_2,
    },
]


@pytest.mark.asyncio
async def test_cluster_info_on_vcenter_9_reads_settings_witness_and_fault_domains() -> None:
    witness = [
        {
            "_typeName": "VimClusterVSANWitnessHostInfo",
            "nodeUuid": _WITNESS_NODE,
            "host": moref("HostSystem", "host-99"),
            "preferredFdName": "<site-a>",
        }
    ]
    fake = FakeVcenter(
        RELEASE_91,
        {"VsanClusterGetConfig": _CONFIG_EX_VIJSON, "VSANVcGetWitnessHosts": witness},
        _CLUSTER_PROPS,
    )
    result = await _run(fake, "vsan_cluster_info", {"cluster": _CLUSTER})

    assert result == {
        "cluster": _CLUSTER,
        "vsan_enabled": True,
        "architecture": "ESA",
        "vsan_cluster_uuid": _CLUSTER_UUID,
        "dedup_enabled": False,
        "compression_enabled": True,
        "encryption_enabled": True,
        "key_provider": "<key-provider>",
        "in_transit_encryption_enabled": False,
        "stretched_or_two_node": True,
        "witness_hosts": [
            {"host": "host-99", "node_uuid": _WITNESS_NODE, "preferred_fault_domain": "<site-a>"}
        ],
        "fault_domains": ["<site-a>", "<site-b>"],
        "hosts": _EXPECTED_HOSTS,
    }
    assert "must-not-leak" not in json.dumps(result)
    assert [call.method for call in fake.vsan_calls] == [
        "VsanClusterGetConfig",
        "VSANVcGetWitnessHosts",
    ]
    assert all(call.json() == {"cluster": _CLUSTER_REF} for call in fake.vsan_calls)
    assert [read["specSet"][0]["propSet"][0]["pathSet"] for read in fake.property_reads] == [
        ["configurationEx"],
        ["name"],
    ]


@pytest.mark.asyncio
async def test_cluster_info_on_vcenter_8_uses_vsan_soap() -> None:
    fake = FakeVcenter(
        RELEASE_80,
        {
            "VsanClusterGetConfig": _CONFIG_EX_SOAP,
            # A normal cluster: the method sends no returnval.
            "VSANVcGetWitnessHosts": soap_answer("VSANVcGetWitnessHosts"),
        },
        _CLUSTER_PROPS,
    )
    result = await _run(fake, "vsan_cluster_info", {"cluster": _CLUSTER})

    assert result["architecture"] == "OSA"
    assert result["dedup_enabled"] is True
    assert result["encryption_enabled"] is False
    assert result["key_provider"] is None
    assert result["stretched_or_two_node"] is False
    assert result["witness_hosts"] == []
    assert result["hosts"] == _EXPECTED_HOSTS
    body = fake.calls_of("VsanClusterGetConfig")[0].body
    assert '<_this type="VsanVcClusterConfigSystem">vsan-cluster-config-system</_this>' in body
    assert f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>' in body
    witness_body = fake.calls_of("VSANVcGetWitnessHosts")[0].body
    assert (
        '<_this type="VimClusterVsanVcStretchedClusterSystem">vsan-stretched-cluster-system'
        in witness_body
    )


@pytest.mark.asyncio
async def test_cluster_info_with_vsan_off_reads_nothing_more() -> None:
    fake = FakeVcenter(
        RELEASE_91, {"VsanClusterGetConfig": {"_typeName": "VsanConfigInfoEx", "enabled": False}}
    )
    result = await _run(fake, "vsan_cluster_info", {"cluster": _CLUSTER})

    assert result["vsan_enabled"] is False
    assert result["architecture"] is None
    assert result["hosts"] == []
    assert [call.method for call in fake.vsan_calls] == ["VsanClusterGetConfig"]
    assert fake.property_reads == []


@pytest.mark.asyncio
async def test_cluster_info_empty_answer_fails_loudly() -> None:
    fake = FakeVcenter(RELEASE_91, {"VsanClusterGetConfig": httpx.Response(200)})
    with pytest.raises(RuntimeError, match="no answer to VsanClusterGetConfig"):
        await _run(fake, "vsan_cluster_info", {"cluster": _CLUSTER})


@pytest.mark.asyncio
async def test_cluster_info_missing_cluster_is_not_found() -> None:
    not_found = {
        "_typeName": "ManagedObjectNotFound",
        "faultstring": "The object has already been deleted",
        "obj": _CLUSTER_REF,
    }
    fake = FakeVcenter(RELEASE_91, {"VsanClusterGetConfig": httpx.Response(500, json=not_found)})
    with pytest.raises(ConnectorResourceNotFoundError):
        await _run(fake, "vsan_cluster_info", {"cluster": _CLUSTER})


# ---------------------------------------------------------------------------
# vmware.vsan.capacity
# ---------------------------------------------------------------------------

_TIB = 1024**4
_SPACE_VIJSON: dict[str, Any] = {
    "_typeName": "VsanSpaceUsage",
    "totalCapacityB": 10 * _TIB,
    "freeCapacityB": 4 * _TIB,
    "uncommittedB": 1 * _TIB,
    "spaceOverview": {
        "_typeName": "VsanObjectSpaceSummary",
        "usedB": 6 * _TIB,
        "physicalUsedB": 5 * _TIB,
        "primaryCapacityB": 3 * _TIB,
        "overheadB": 2 * _TIB,
        "reservedCapacityB": 1 * _TIB,
    },
    "spaceDetail": {
        "_typeName": "VsanSpaceUsageDetailResult",
        "spaceUsageByObjectType": [
            {"_typeName": "VsanObjectSpaceSummary", "objType": "vdisk", "usedB": 5 * _TIB},
            {"_typeName": "VsanObjectSpaceSummary", "objType": "vmswap", "usedB": _TIB},
        ],
    },
    "efficientCapacity": {
        "_typeName": "VimVsanDataEfficiencyCapacityState",
        "logicalCapacityUsed": 8 * _TIB,
        "physicalCapacityUsed": 5 * _TIB,
        "dedupMetadataSize": 1024,
    },
    "spaceEfficiencyRatio": {"_typeName": "VsanSpaceEfficiencyRatio", "overallRatio": 1.6},
    "capacityHealthThreshold": {
        "_typeName": "VsanHealthThreshold",
        "yellowValue": 7 * _TIB,
        "redValue": 9 * _TIB,
    },
}


def _whatif(failures: int, total: int, used: int) -> dict[str, Any]:
    return {
        "_typeName": "VsanClusterWhatifHostFailuresResult",
        "numFailures": failures,
        "totalCapacityB": total,
        "totalUsedCapacityB": used,
        "totalRcReservationB": 0,
        "totalRcSizeB": 0,
        "usedComponents": 120,
        "totalComponents": 27000,
        "diskFreeSpaceHealth": "green" if failures == 0 else "yellow",
        "componentLimitHealth": "green",
    }


_LIMIT_VIJSON: dict[str, Any] = {
    "_typeName": "VsanClusterHealthSummary",
    "timestamp": _CHECKED_AT,
    "limitHealth": {
        "_typeName": "VsanClusterLimitHealthResult",
        "issueFound": False,
        "componentLimitHealth": "green",
        "diskFreeSpaceHealth": "green",
        "rcFreeReservationHealth": "green",
        "whatifHostFailures": [_whatif(0, 10 * _TIB, 6 * _TIB), _whatif(1, 7 * _TIB, 6 * _TIB)],
    },
}


@pytest.mark.asyncio
async def test_capacity_on_vcenter_9_reads_space_and_host_failures() -> None:
    fake = FakeVcenter(
        RELEASE_91,
        {"VsanQuerySpaceUsage": _SPACE_VIJSON, "VsanQueryVcClusterHealthSummary": _LIMIT_VIJSON},
    )
    result = await _run(fake, "vsan_capacity", {"cluster": _CLUSTER})

    assert result["total_bytes"] == 10 * _TIB
    assert result["free_bytes"] == 4 * _TIB
    assert result["used_bytes"] == 6 * _TIB
    assert result["overview"]["reserved_bytes"] == _TIB
    assert result["by_object_type"][0] == {
        "object_type": "vdisk",
        "used_bytes": 5 * _TIB,
        "physical_used_bytes": None,
        "primary_bytes": None,
        "overhead_bytes": None,
        "temporary_overhead_bytes": None,
        "reserved_bytes": None,
        "over_reserved_bytes": None,
        "provisioned_bytes": None,
        "snapshot_used_bytes": None,
    }
    assert result["space_saving"]["saved_bytes"] == 3 * _TIB
    assert result["space_saving"]["overall_ratio"] == 1.6
    assert result["capacity_threshold"] == {"yellow_bytes": 7 * _TIB, "red_bytes": 9 * _TIB}
    assert [row["host_failures"] for row in result["host_failures"]] == [0, 1]
    assert result["host_failures"][1]["disk_space_health"] == "yellow"
    assert result["checked_at"] == _CHECKED_AT
    assert fake.calls_of("VsanQuerySpaceUsage")[0].json() == {"cluster": _CLUSTER_REF}
    assert fake.calls_of("VsanQueryVcClusterHealthSummary")[0].json() == {
        "cluster": _CLUSTER_REF,
        "includeObjUuids": False,
        "fields": ["limitHealth"],
    }


@pytest.mark.asyncio
async def test_capacity_on_vcenter_8_reads_soap_numbers_and_one_entry_lists() -> None:
    space = soap_answer(
        "VsanQuerySpaceUsage",
        f"<totalCapacityB>{10 * _TIB}</totalCapacityB><freeCapacityB>{4 * _TIB}</freeCapacityB>"
        "<spaceDetail><spaceUsageByObjectType><objType>vdisk</objType>"
        f"<usedB>{5 * _TIB}</usedB></spaceUsageByObjectType></spaceDetail>",
    )
    limit = soap_answer(
        "VsanQueryVcClusterHealthSummary",
        f"<timestamp>{_CHECKED_AT}</timestamp><limitHealth><issueFound>false</issueFound>"
        "<whatifHostFailures><numFailures>1</numFailures>"
        f"<totalCapacityB>{7 * _TIB}</totalCapacityB>"
        f"<totalUsedCapacityB>{6 * _TIB}</totalUsedCapacityB>"
        "<diskFreeSpaceHealth>red</diskFreeSpaceHealth></whatifHostFailures></limitHealth>",
    )
    fake = FakeVcenter(
        RELEASE_80, {"VsanQuerySpaceUsage": space, "VsanQueryVcClusterHealthSummary": limit}
    )
    result = await _run(fake, "vsan_capacity", {"cluster": _CLUSTER, "from_cache": True})

    assert result["total_bytes"] == 10 * _TIB
    assert result["used_bytes"] == 6 * _TIB
    assert result["space_saving"] is None
    assert [row["object_type"] for row in result["by_object_type"]] == ["vdisk"]
    assert result["host_failures"] == [
        {
            "host_failures": 1,
            "total_bytes": 7 * _TIB,
            "used_bytes": 6 * _TIB,
            "used_components": None,
            "total_components": None,
            "disk_space_health": "red",
            "component_limit_health": None,
            "space_needed_for_operations_bytes": None,
        }
    ]
    assert result["from_cache"] is True
    body = fake.calls_of("VsanQueryVcClusterHealthSummary")[0].body
    assert (
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
        "<includeObjUuids>false</includeObjUuids><fields>limitHealth</fields>"
        "<fetchFromCache>true</fetchFromCache>"
    ) in body
    assert '<_this type="VsanSpaceReportSystem">vsan-cluster-space-report-system</_this>' in (
        fake.calls_of("VsanQuerySpaceUsage")[0].body
    )


@pytest.mark.asyncio
async def test_capacity_without_limit_health_fails_loudly() -> None:
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanQuerySpaceUsage": _SPACE_VIJSON,
            "VsanQueryVcClusterHealthSummary": {"_typeName": "VsanClusterHealthSummary"},
        },
    )
    with pytest.raises(RuntimeError, match="no limitHealth"):
        await _run(fake, "vsan_capacity", {"cluster": _CLUSTER})


@pytest.mark.asyncio
async def test_capacity_vsan_fault_on_vcenter_8_reaches_the_caller() -> None:
    fake = FakeVcenter(
        RELEASE_80, {"VsanQuerySpaceUsage": soap_fault("VsanFault", "vSAN is not enabled")}
    )
    with pytest.raises(Exception, match="vSAN is not enabled"):
        await _run(fake, "vsan_capacity", {"cluster": _CLUSTER})


# ---------------------------------------------------------------------------
# vmware.vsan.performance
# ---------------------------------------------------------------------------

_TIMES = "2026-10-30 09:55:00,2026-10-30 10:00:00"


def _perf_entity(entity: str, series: dict[str, str]) -> dict[str, Any]:
    return {
        "_typeName": "VsanPerfEntityMetricCSV",
        "entityRefId": entity,
        "sampleInfo": _TIMES,
        "value": [
            {
                "_typeName": "VsanPerfMetricSeriesCSV",
                "metricId": {"_typeName": "VsanPerfMetricId", "label": label},
                "values": values,
            }
            for label, values in series.items()
        ],
    }


@pytest.mark.asyncio
async def test_performance_on_vcenter_9_sends_one_spec_and_returns_flat_rows() -> None:
    entity = f"cluster-domclient:{_CLUSTER_UUID}"
    fake = FakeVcenter(
        RELEASE_91,
        {
            "VsanPerfQueryPerf": [
                _perf_entity(entity, {"iopsRead": "100,120", "latencyAvgRead": "850,None"})
            ]
        },
    )
    result = await _run(
        fake,
        "vsan_performance",
        {
            "cluster": _CLUSTER,
            "entity_id": _CLUSTER_UUID,
            "minutes": 30,
            "end_time": "2026-10-30T10:00:00Z",
            "metrics": ["iopsRead", "latencyAvgRead"],
        },
    )

    assert result["entity"] == entity
    assert result["start_time"] == "2026-10-30T09:30:00Z"
    assert result["end_time"] == "2026-10-30T10:00:00Z"
    assert result["note"] is None
    assert result["rows"] == [
        {"entity": entity, "metric": "iopsRead", "time": "2026-10-30 09:55:00", "value": 100.0},
        {"entity": entity, "metric": "iopsRead", "time": "2026-10-30 10:00:00", "value": 120.0},
        {
            "entity": entity,
            "metric": "latencyAvgRead",
            "time": "2026-10-30 09:55:00",
            "value": 850.0,
        },
        {
            "entity": entity,
            "metric": "latencyAvgRead",
            "time": "2026-10-30 10:00:00",
            "value": None,
        },
    ]
    assert fake.calls_of("VsanPerfQueryPerf")[0].json() == {
        "querySpecs": [
            {
                "_typeName": "VsanPerfQuerySpec",
                "entityRefId": entity,
                "startTime": "2026-10-30T09:30:00Z",
                "endTime": "2026-10-30T10:00:00Z",
                "labels": ["iopsRead", "latencyAvgRead"],
            }
        ],
        "cluster": _CLUSTER_REF,
    }


@pytest.mark.asyncio
async def test_performance_on_vcenter_8_reads_each_returnval() -> None:
    def entity_xml(node: str, values: str) -> str:
        return (
            f"<entityRefId>host-domclient:{node}</entityRefId><sampleInfo>{_TIMES}</sampleInfo>"
            f"<value><metricId><label>iopsWrite</label></metricId><values>{values}</values></value>"
        )

    fake = FakeVcenter(
        RELEASE_80,
        {
            "VsanPerfQueryPerf": soap_answer(
                "VsanPerfQueryPerf", entity_xml(_NODE_1, "5,6"), entity_xml(_NODE_2, "7,8")
            )
        },
    )
    result = await _run(
        fake,
        "vsan_performance",
        {"cluster": _CLUSTER, "entity_type": "host-domclient", "end_time": "2026-10-30T10:00:00Z"},
    )

    assert [(row["entity"], row["value"]) for row in result["rows"]] == [
        (f"host-domclient:{_NODE_1}", 5.0),
        (f"host-domclient:{_NODE_1}", 6.0),
        (f"host-domclient:{_NODE_2}", 7.0),
        (f"host-domclient:{_NODE_2}", 8.0),
    ]
    body = fake.calls_of("VsanPerfQueryPerf")[0].body
    assert (
        "<querySpecs><entityRefId>host-domclient:*</entityRefId>"
        "<startTime>2026-10-30T09:00:00Z</startTime><endTime>2026-10-30T10:00:00Z</endTime>"
        f'</querySpecs><cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
    ) in body
    assert "_typeName" not in body


@pytest.mark.asyncio
async def test_performance_without_samples_says_why() -> None:
    fake = FakeVcenter(RELEASE_91, {"VsanPerfQueryPerf": httpx.Response(200, json=[])})
    result = await _run(fake, "vsan_performance", {"cluster": _CLUSTER})

    assert result["rows"] == []
    assert result["note"] == NO_SAMPLES_NOTE
    assert result["entity"] == "cluster-domclient:*"
