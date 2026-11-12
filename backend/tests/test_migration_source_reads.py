# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reads added for the migration inventory.

* ``hyperv.vms.guest.list`` -- BIOS GUID and guest OS per Hyper-V VM.
* ``winsrv.system.hardware`` -- processors, memory, product UUID, firmware.
* ``k8s.workload.list`` -- Deployments, StatefulSets, DaemonSets with size.
* ``k8s.node.list`` rows now carry the node's BIOS UUID and capacity.

The SSH reads run against a mocked ``_run_command``; the Kubernetes read
against a mocked ``AppsV1Api``. Names are made up.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
from kubernetes_asyncio.client import (
    V1Container,
    V1DaemonSet,
    V1DaemonSetSpec,
    V1DaemonSetStatus,
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1ListMeta,
    V1Node,
    V1NodeStatus,
    V1NodeSystemInfo,
    V1ObjectMeta,
    V1PersistentVolumeClaim,
    V1PersistentVolumeClaimSpec,
    V1PersistentVolumeClaimVolumeSource,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
    V1StatefulSet,
    V1StatefulSetList,
    V1StatefulSetSpec,
    V1Volume,
    V1VolumeResourceRequirements,
)

from meho_backplane.connectors.hyperv import HYPERV_OPS, HypervConnector
from meho_backplane.connectors.hyperv.ops_guest import hyperv_vms_guest_list
from meho_backplane.connectors.inventory.source_k8s import build_workload_record
from meho_backplane.connectors.kubernetes import ops_workload_set
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.kubernetes.ops_core import node_row
from meho_backplane.connectors.kubernetes.ops_workload_set import (
    k8s_workload_list,
    workload_set_row,
)
from meho_backplane.connectors.winsrv import WINSRV_OPS, WinsrvConnector
from meho_backplane.connectors.winsrv.ops_hardware import winsrv_hardware
from meho_backplane.operations._validate import validate_params


class _Target:
    name = "host-test"
    host = "host.test.invalid"
    port = 22
    secret_ref = "meho/testing/host-test"


def _run(stdout: str) -> AsyncMock:
    proc = MagicMock(stdout=stdout, stderr="", exit_status=0)
    return AsyncMock(return_value=proc)


def _script(run_mock: AsyncMock) -> str:
    cmd: str = run_mock.await_args.args[1]
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")


async def test_hyperv_guest_list_reads_bios_guid_and_kvp_items() -> None:
    connector = HypervConnector()
    run_mock = _run(
        '{"rows":[{"Id":"6f9c2d1e-0a4b-4c3d-9e8f-1a2b3c4d5e6f","Name":"app-win-01",'
        '"BIOSGUID":"{1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9}","OSName":"Windows Server 2022",'
        '"OSVersion":"10.0.20348","FullyQualifiedDomainName":null}],"total":1}'
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await hyperv_vms_guest_list(connector, _Target(), {})
    script = _script(run_mock)
    assert "Msvm_VirtualSystemSettingData" in script
    assert "Microsoft:Hyper-V:System:Realized" in script
    assert "GuestIntrinsicExchangeItems" in script
    assert result["total"] == 1 and result["rows"][0]["OSName"] == "Windows Server 2022"


def test_hyperv_guest_list_is_a_registered_safe_read() -> None:
    op = next(op for op in HYPERV_OPS if op.op_id == "hyperv.vms.guest.list")
    assert (op.safety_level, op.requires_approval, op.group_key) == ("safe", False, "vms")
    assert hasattr(HypervConnector, op.handler_attr)


async def test_winsrv_hardware_reads_the_machine_facts() -> None:
    connector = WinsrvConnector()
    run_mock = _run(
        '{"Hostname":"SQL-01","LogicalProcessors":8,"TotalPhysicalMemory":34359738368,'
        '"ProductUuid":"0F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9","FirmwareType":"Uefi",'
        '"SecureBoot":null}'
    )
    with patch.object(connector, "_run_command", run_mock):
        result = await winsrv_hardware(connector, _Target(), {})
    script = _script(run_mock)
    assert "Win32_ComputerSystemProduct" in script
    assert "BiosFirmwareType" in script
    assert "Confirm-SecureBootUEFI" in script
    assert result["LogicalProcessors"] == 8 and result["SecureBoot"] is None
    op = next(op for op in WINSRV_OPS if op.op_id == "winsrv.system.hardware")
    assert (op.safety_level, op.requires_approval) == ("safe", False)
    assert hasattr(WinsrvConnector, op.handler_attr)


def _statefulset() -> V1StatefulSet:
    template = V1PodTemplateSpec(
        spec=V1PodSpec(
            containers=[
                V1Container(
                    name="pg",
                    image="registry.example/pg:16",
                    resources=V1ResourceRequirements(
                        requests={"cpu": "500m", "memory": "1Gi"}, limits={"memory": "2Gi"}
                    ),
                )
            ],
            volumes=[
                V1Volume(
                    name="backup",
                    persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                        claim_name="pg-backup"
                    ),
                ),
            ],
        )
    )
    claim = V1PersistentVolumeClaim(
        metadata=V1ObjectMeta(name="pgdata"),
        spec=V1PersistentVolumeClaimSpec(
            storage_class_name="fast",
            resources=V1VolumeResourceRequirements(requests={"storage": "20Gi"}),
        ),
    )
    return V1StatefulSet(
        metadata=V1ObjectMeta(name="db", namespace="shop"),
        spec=V1StatefulSetSpec(
            replicas=3,
            selector=V1LabelSelector(match_labels={"app": "db"}),
            service_name="db",
            template=template,
            volume_claim_templates=[claim],
        ),
    )


def test_workload_set_row_for_a_statefulset() -> None:
    assert workload_set_row("StatefulSet", _statefulset()) == {
        "kind": "StatefulSet",
        "name": "db",
        "namespace": "shop",
        "replicas": 3,
        "containers": [
            {
                "name": "pg",
                "image": "registry.example/pg:16",
                "requests": {"cpu": "500m", "memory": "1Gi"},
                "limits": {"memory": "2Gi"},
            }
        ],
        "volumes": [{"name": "backup", "type": "persistentVolumeClaim", "claim_name": "pg-backup"}],
        "volume_claim_templates": [
            {"name": "pgdata", "storage_request": "20Gi", "storage_class": "fast"}
        ],
        "pod_selector": "app=db",
    }


def test_workload_set_row_for_a_daemonset_uses_the_scheduled_count() -> None:
    daemonset = V1DaemonSet(
        metadata=V1ObjectMeta(name="agent", namespace="ops"),
        spec=V1DaemonSetSpec(
            selector=V1LabelSelector(match_labels={"app": "agent"}),
            template=V1PodTemplateSpec(spec=V1PodSpec(containers=[V1Container(name="a")])),
        ),
        status=V1DaemonSetStatus(
            desired_number_scheduled=5,
            current_number_scheduled=5,
            number_misscheduled=0,
            number_ready=5,
        ),
    )
    row = workload_set_row("DaemonSet", daemonset)
    assert (row["replicas"], row["pod_selector"]) == (5, "app=agent")
    assert row["containers"] == [{"name": "a", "image": None, "requests": {}, "limits": {}}]


def test_workload_set_row_keeps_sidecars_and_drops_plain_init_containers() -> None:
    def container(name: str, cpu: str, restart_policy: str | None = None) -> V1Container:
        return V1Container(
            name=name,
            image=f"registry.example/{name}:1",
            restart_policy=restart_policy,
            resources=V1ResourceRequirements(requests={"cpu": cpu}),
        )

    deployment = V1Deployment(
        metadata=V1ObjectMeta(name="web", namespace="shop"),
        spec=V1DeploymentSpec(
            replicas=2,
            selector=V1LabelSelector(match_labels={"app": "web"}),
            template=V1PodTemplateSpec(
                spec=V1PodSpec(
                    init_containers=[
                        container("migrate-db", "1", restart_policy=None),
                        container("log-shipper", "500m", restart_policy="Always"),
                    ],
                    containers=[container("app", "250m")],
                )
            ),
        ),
    )
    row = workload_set_row("Deployment", deployment)
    assert [c["name"] for c in row["containers"]] == ["app", "log-shipper"]
    record = build_workload_record(
        row, cluster="c1", claims={}, nodes=[], collected_at=datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert record.cpu_request_millicores == 750.0
    assert record.images == ["registry.example/app:1", "registry.example/log-shipper:1"]


async def test_k8s_workload_list_pages_across_all_namespaces() -> None:
    api = MagicMock()
    api.list_stateful_set_for_all_namespaces = AsyncMock(
        return_value=V1StatefulSetList(
            items=[_statefulset()], metadata=V1ListMeta(_continue="next-page")
        )
    )
    connector = MagicMock()
    connector._get_api_client = AsyncMock(return_value=object())
    with patch.object(ops_workload_set.client, "AppsV1Api", return_value=api):
        result = await k8s_workload_list(
            connector,
            _Target(),
            MagicMock(),
            {"kind": "StatefulSet", "all_namespaces": True, "limit": 10},
        )
    api.list_stateful_set_for_all_namespaces.assert_awaited_once_with(limit=10)
    assert (result["kind"], result["total"], result["next_continue"]) == (
        "StatefulSet",
        1,
        "next-page",
    )


def test_k8s_workload_list_schema() -> None:
    op = next(op for op in KUBERNETES_OPS if op.op_id == "k8s.workload.list")
    assert (op.safety_level, op.requires_approval) == ("safe", False)
    for example in op.parameter_schema["examples"]:
        validate_params(op.parameter_schema, example)


def test_node_row_carries_identity_and_capacity() -> None:
    node = V1Node(
        metadata=V1ObjectMeta(name="worker-1"),
        status=V1NodeStatus(
            capacity={"cpu": "4", "memory": "16393216Ki"},
            node_info=V1NodeSystemInfo(
                architecture="amd64",
                boot_id="b",
                container_runtime_version="containerd://1.7",
                kernel_version="6.8.0",
                kube_proxy_version="v1.30.0",
                kubelet_version="v1.30.0",
                machine_id="m",
                operating_system="linux",
                os_image="Ubuntu 22.04.4 LTS",
                system_uuid="1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9",
            ),
        ),
    )
    row = node_row(node)
    assert row["system_uuid"] == "1B2C3D4E-5F60-4718-8293-A4B5C6D7E8F9"
    assert (row["cpu_capacity"], row["memory_capacity"]) == ("4", "16393216Ki")
    assert (row["os_image"], row["architecture"]) == ("Ubuntu 22.04.4 LTS", "amd64")
    ops = {op.op_id: op for op in KUBERNETES_OPS}
    # A real node row must pass the node-list schema it is returned under.
    jsonschema.validate({"rows": [row], "total": 1}, ops["k8s.node.list"].response_schema)
    namespace_row = ops["k8s.namespace.list"].response_schema["properties"]["rows"]["items"]
    assert "system_uuid" not in namespace_row["properties"]
