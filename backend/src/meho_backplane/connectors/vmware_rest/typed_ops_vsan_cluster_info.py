# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vsan.cluster.info`` typed op -- the vSAN settings of one cluster.

The read sends up to four calls on the connector's own session, so it works
on a fresh install with no catalog ingest:

1. vSAN ``VsanVcClusterConfigSystem.VsanClusterGetConfig`` (object
   ``vsan-cluster-config-system``): on or off, OSA or ESA, the cluster's vSAN
   UUID, deduplication and compression, encryption at rest and in transit.
2. When vSAN is on: vSAN ``VimClusterVsanVcStretchedClusterSystem.
   VSANVcGetWitnessHosts`` (object ``vsan-stretched-cluster-system``). A
   cluster with a witness host is a stretched or a two-node cluster.
3. When vSAN is on: the cluster's ``configurationEx`` through
   ``PropertyCollector.RetrievePropertiesEx``. Its ``vsanHostConfig`` holds
   the vSAN settings of each host (the same ``VsanHostConfigInfo`` as
   ``HostVsanSystem.config``): the fault domain and the vSAN node UUID.
   vCenter 8.0 refuses a path inside ``configurationEx``, so it is read whole.
4. The ``name`` of those hosts, in one more property read.

Calls 1 and 2 are vSAN methods (VI-JSON on vCenter 9.x, SOAP on 8.0, see
:meth:`VmwareRestConnector._post_vsan`); 3 and 4 are core vSphere and use
VI-JSON on both. Object ids and types: the vendor VI/JSON file of vCenter 9.0
and the vSAN Management SDK 8.0 (``vsanmgmtObjects.py``).

The key ids of the encryption settings (``kekId``, ``hostKeyId``, the wrapped
data keys) are never returned; only whether encryption is on and the key
provider's name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vsan_common import (
    CLUSTER_MO_TYPE,
    CLUSTER_PARAM_SCHEMA,
    HOST_MO_TYPE,
    VSAN_GROUP_KEY,
    as_bool,
    as_text,
    cluster_ref,
    dict_items,
    read_properties,
    vsan_call,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "VMWARE_VSAN_CLUSTER_INFO_OP",
    "VSAN_CLUSTER_GET_CONFIG",
    "VSAN_GET_WITNESS_HOSTS",
    "vsan_cluster_info_impl",
    "vsan_hosts",
]

VSAN_CLUSTER_GET_CONFIG: Final = VsanMethod(
    mo_type="VsanVcClusterConfigSystem",
    moid="vsan-cluster-config-system",
    method="VsanClusterGetConfig",
)
VSAN_GET_WITNESS_HOSTS: Final = VsanMethod(
    mo_type="VimClusterVsanVcStretchedClusterSystem",
    moid="vsan-stretched-cluster-system",
    method="VSANVcGetWitnessHosts",
)

_PROP_CONFIGURATION_EX: Final = "configurationEx"
_PROP_NAME: Final = "name"

ARCHITECTURE_ESA: Final = "ESA"
ARCHITECTURE_OSA: Final = "OSA"


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    """The cluster-wide fields of a ``VsanConfigInfoEx``."""
    enabled = as_bool(config.get("enabled"))
    esa = as_bool(config.get("vsanEsaEnabled"))
    default = config.get("defaultConfig")
    efficiency = config.get("dataEfficiencyConfig")
    encryption = config.get("dataEncryptionConfig")
    in_transit = config.get("dataInTransitEncryptionConfig")
    kms = encryption.get("kmsProviderId") if isinstance(encryption, dict) else None
    architecture = None
    if enabled:
        architecture = ARCHITECTURE_ESA if esa else ARCHITECTURE_OSA
    return {
        "vsan_enabled": enabled,
        "architecture": architecture,
        "vsan_cluster_uuid": as_text(default.get("uuid")) if isinstance(default, dict) else None,
        "dedup_enabled": (
            as_bool(efficiency.get("dedupEnabled")) if isinstance(efficiency, dict) else None
        ),
        "compression_enabled": (
            as_bool(efficiency.get("compressionEnabled")) if isinstance(efficiency, dict) else None
        ),
        "encryption_enabled": (
            as_bool(encryption.get("encryptionEnabled")) if isinstance(encryption, dict) else None
        ),
        "key_provider": as_text(kms.get("id")) if isinstance(kms, dict) else None,
        "in_transit_encryption_enabled": (
            as_bool(in_transit.get("enabled")) if isinstance(in_transit, dict) else None
        ),
    }


def _witness_row(info: dict[str, Any]) -> dict[str, Any]:
    host = info.get("host")
    return {
        "host": as_text(host.get("value")) if isinstance(host, dict) else None,
        "node_uuid": as_text(info.get("nodeUuid")),
        "preferred_fault_domain": as_text(info.get("preferredFdName")),
    }


def _host_rows(configuration_ex: Any) -> list[dict[str, Any]]:
    """One row per host of ``configurationEx.vsanHostConfig``, names still unset."""
    if not isinstance(configuration_ex, dict):
        raise RuntimeError("vSAN cluster info: vCenter sent no cluster configuration")
    rows: list[dict[str, Any]] = []
    for host_config in dict_items(configuration_ex.get("vsanHostConfig")):
        host = host_config.get("hostSystem")
        fault_domain = host_config.get("faultDomainInfo")
        cluster_info = host_config.get("clusterInfo")
        rows.append(
            {
                "host": as_text(host.get("value")) if isinstance(host, dict) else None,
                "name": None,
                "vsan_enabled": as_bool(host_config.get("enabled")),
                "fault_domain": (
                    as_text(fault_domain.get("name")) if isinstance(fault_domain, dict) else None
                ),
                "node_uuid": (
                    as_text(cluster_info.get("nodeUuid"))
                    if isinstance(cluster_info, dict)
                    else None
                ),
            }
        )
    return rows


async def vsan_hosts(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    cluster_moid: str,
    *,
    with_names: bool = True,
) -> list[dict[str, Any]]:
    """One row per host of the cluster with its vSAN settings, and the host names.

    Reads the cluster's ``configurationEx`` (its ``vsanHostConfig``) and, with
    *with_names*, the ``name`` of those hosts.
    """
    cluster_props = await read_properties(
        connector, operator, target, CLUSTER_MO_TYPE, [cluster_moid], (_PROP_CONFIGURATION_EX,)
    )
    rows = _host_rows(cluster_props.get(cluster_moid, {}).get(_PROP_CONFIGURATION_EX))
    host_ids = [row["host"] for row in rows if row["host"]]
    if with_names and host_ids:
        names = await read_properties(
            connector, operator, target, HOST_MO_TYPE, host_ids, (_PROP_NAME,)
        )
        for row in rows:
            row["name"] = as_text(names.get(row["host"] or "", {}).get(_PROP_NAME))
    return rows


async def vsan_cluster_info_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vsan.cluster.info``."""
    cluster_moid = params["cluster"]
    body = {"cluster": cluster_ref(cluster_moid)}
    config = await vsan_call(
        connector, operator, target, VSAN_CLUSTER_GET_CONFIG, body, what="cluster settings"
    )
    if not isinstance(config, dict):
        raise RuntimeError("vSAN cluster settings: vCenter sent no VsanConfigInfoEx")
    answer: dict[str, Any] = {"cluster": cluster_moid, **_settings(config)}
    witnesses: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    if answer["vsan_enabled"]:
        witness_answer = await vsan_call(
            connector,
            operator,
            target,
            VSAN_GET_WITNESS_HOSTS,
            body,
            what="witness hosts",
            empty_ok=True,
        )
        witnesses = [_witness_row(info) for info in dict_items(witness_answer)]
        hosts = await vsan_hosts(connector, operator, target, cluster_moid)
    answer["stretched_or_two_node"] = bool(witnesses)
    answer["witness_hosts"] = witnesses
    answer["fault_domains"] = sorted({row["fault_domain"] for row in hosts if row["fault_domain"]})
    answer["hosts"] = hosts
    _log().info(
        "vmware_vsan_cluster_info_read",
        target=target.name,
        cluster=cluster_moid,
        vsan_enabled=answer["vsan_enabled"],
        host_count=len(hosts),
    )
    return answer


# ---------------------------------------------------------------------------
# Op metadata + schemas
# ---------------------------------------------------------------------------


def _nullable(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string", "description": "Managed object id of the cluster."},
        "vsan_enabled": _nullable("boolean", "True when vSAN is on for the cluster."),
        "architecture": {
            "type": ["string", "null"],
            "enum": [ARCHITECTURE_ESA, ARCHITECTURE_OSA, None],
            "description": (
                "ESA (Express Storage Architecture) or OSA (Original Storage Architecture, "
                "disk groups). null when vSAN is off."
            ),
        },
        "vsan_cluster_uuid": _nullable(
            "string", "The vSAN UUID of the cluster. vmware.vsan.performance takes it."
        ),
        "dedup_enabled": _nullable("boolean", "True when deduplication is on (OSA only)."),
        "compression_enabled": _nullable("boolean", "True when compression is on."),
        "encryption_enabled": _nullable("boolean", "True when data-at-rest encryption is on."),
        "key_provider": _nullable(
            "string", "Name of the key provider used for encryption. Never a key."
        ),
        "in_transit_encryption_enabled": _nullable(
            "boolean", "True when vSAN encrypts the data it sends between hosts."
        ),
        "stretched_or_two_node": {
            "type": "boolean",
            "description": (
                "True when the cluster has a witness host: a stretched or two-node cluster."
            ),
        },
        "witness_hosts": {
            "type": "array",
            "description": "The witness hosts of the cluster. Empty for a normal cluster.",
            "items": {
                "type": "object",
                "properties": {
                    "host": _nullable("string", "Managed object id of the witness host."),
                    "node_uuid": _nullable("string", "vSAN node UUID of the witness host."),
                    "preferred_fault_domain": _nullable(
                        "string", "The preferred fault domain (site) of the stretched cluster."
                    ),
                },
            },
        },
        "fault_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of the fault domains the hosts are in. Empty when none is set.",
        },
        "hosts": {
            "type": "array",
            "description": "One row per host of the cluster with its vSAN settings.",
            "items": {
                "type": "object",
                "properties": {
                    "host": _nullable("string", "Managed object id of the host."),
                    "name": _nullable("string", "Name of the host."),
                    "vsan_enabled": _nullable("boolean", "True when vSAN is on for this host."),
                    "fault_domain": _nullable(
                        "string", "The host's fault domain; null for the default one."
                    ),
                    "node_uuid": _nullable(
                        "string", "vSAN node UUID of the host. vmware.vsan.performance takes it."
                    ),
                },
            },
        },
    },
    "required": ["cluster", "vsan_enabled", "witness_hosts", "fault_domains", "hosts"],
}

VMWARE_VSAN_CLUSTER_INFO_OP: Final[VmwareTypedOp] = VmwareTypedOp(
    op_id="vmware.vsan.cluster.info",
    handler_attr="vsan_cluster_info",
    summary=(
        "vSAN settings of a cluster: OSA or ESA, space saving, encryption, fault domains, witness."
    ),
    description=(
        "Returns the vSAN settings of one cluster: on or off, OSA or ESA, deduplication and "
        "compression, encryption, whether it is stretched or two-node with its witness host, "
        "and the fault domain and vSAN UUID of each host."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"cluster": CLUSTER_PARAM_SCHEMA},
        "required": ["cluster"],
        "additionalProperties": False,
        "examples": [{"cluster": "<cluster-id>"}],
    },
    response_schema=_RESPONSE_SCHEMA,
    group_key=VSAN_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vsan", "cluster", "settings"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to learn how vSAN is set up on a cluster: whether it is on, OSA or ESA, "
            "space saving, encryption, stretched or two-node, and the fault domains. Call it "
            "first when you need the vSAN UUID of the cluster or of a host for "
            "vmware.vsan.performance."
        ),
        "parameter_hints": {"cluster": "Cluster moid from vmware.cluster.list."},
        "output_shape": (
            "{cluster, vsan_enabled, architecture, vsan_cluster_uuid, dedup_enabled, "
            "compression_enabled, encryption_enabled, key_provider, "
            "in_transit_encryption_enabled, stretched_or_two_node, witness_hosts: [{host, "
            "node_uuid, preferred_fault_domain}], fault_domains: [name], hosts: [{host, name, "
            "vsan_enabled, fault_domain, node_uuid}]}. When vSAN is off, the lists are empty. "
            "A failed vSAN call returns an error with vCenter's fault; a wrong cluster id "
            "gives not_found."
        ),
        "next_step": (
            "For space use, call vmware.vsan.capacity. For the health tests, call "
            "vmware.host.vsan_health."
        ),
    },
)
