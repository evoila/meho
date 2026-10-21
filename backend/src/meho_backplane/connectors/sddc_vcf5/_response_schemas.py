# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas for the ``sddc-vcf5`` typed read core.

Each schema names the main fields of the vendor answer, so the AI knows what a
read returns before it calls it. Broadcom publishes no API file for VCF 5.x;
the field names come from the GET pages of the vendor's "VMware Cloud
Foundation API" reference for 5.0.0 and 5.2.4. A field that only the newer
pages list is marked as such. The schemas are informational: the reducer does
not read them, and every object stays open (``additionalProperties: true``) so
a field a build adds is not hidden.

Split out of :mod:`~meho_backplane.connectors.sddc_vcf5.typed_ops` to keep that
module under the file-size budget.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SDDC_VCF5_RESPONSE_SCHEMAS"]


def _field(type_: str, description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _object(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


def _ref(what: str) -> dict[str, Any]:
    """A vendor reference object: the id plus the name."""
    return _object(
        f"The {what} this belongs to.",
        {
            "id": _field("string", f"The {what} id."),
            "name": _field("string", f"The {what} name."),
        },
    )


#: The vendor ``PageMetadata`` every list read returns next to ``elements``.
_PAGE_METADATA: dict[str, Any] = _object(
    "Paging data for the list.",
    {
        "pageNumber": _field("integer", "The current page number."),
        "pageSize": _field("integer", "The number of elements in this page."),
        "totalElements": _field("integer", "The total number of elements."),
        "totalPages": _field("integer", "The total number of pages."),
    },
)


def _page_of(what: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``PageOf<X>`` answer: ``elements`` plus ``pageMetadata``."""
    return _object(
        f"A page of {what}.",
        {
            "elements": _list_of(f"The {what}.", item_properties),
            "pageMetadata": _PAGE_METADATA,
        },
    )


_DOMAIN_FIELDS: dict[str, Any] = {
    "id": _field("string", "The domain id."),
    "name": _field("string", "The domain name."),
    "type": _field("string", "The domain type, for example MANAGEMENT or VI."),
    "status": _field("string", "The domain state, for example ACTIVE, ACTIVATING or ERROR."),
    "vcenters": _list_of(
        "The vCenters of the domain.",
        {
            "id": _field("string", "The vCenter id."),
            "fqdn": _field("string", "The vCenter FQDN."),
        },
    ),
    "clusters": _list_of(
        "The clusters of the domain.",
        {"id": _field("string", "The cluster id.")},
    ),
    "nsxtCluster": _object(
        "The NSX cluster the domain uses.",
        {
            "id": _field("string", "The NSX cluster id."),
            "vipFqdn": _field("string", "The FQDN of the NSX cluster's virtual IP."),
            "vip": _field("string", "The virtual IP of the NSX cluster."),
        },
    ),
    "ssoId": _field("string", "The SSO domain id."),
    "ssoName": _field("string", "The SSO domain name."),
    "isManagementSsoDomain": _field(
        "boolean", "Whether the domain uses the SSO domain of the management domain."
    ),
    "capacity": _object(
        "The CPU, memory and storage the domain has and uses.",
        {
            "cpu": _field("object", "CPU capacity: total and used."),
            "memory": _field("object", "Memory capacity: total and used."),
            "storage": _field("object", "Storage capacity: total and used."),
        },
    ),
}

_CLUSTER_FIELDS: dict[str, Any] = {
    "id": _field("string", "The cluster id."),
    "name": _field("string", "The cluster name."),
    "primaryDatastoreName": _field("string", "The name of the main datastore."),
    "primaryDatastoreType": _field(
        "string", "The type of the main datastore, for example VSAN, VSAN_ESA, NFS or FC."
    ),
    "isDefault": _field("boolean", "Whether this is the first cluster of its domain."),
    "isStretched": _field("boolean", "Whether the vSAN cluster is stretched."),
    "isImageBased": _field("boolean", "Whether vSphere Lifecycle Manager images manage it."),
    "hosts": _list_of(
        "The ESXi hosts of the cluster.",
        {
            "id": _field("string", "The host id."),
            "fqdn": _field("string", "The host FQDN."),
            "ipAddress": _field("string", "The host management IP."),
            "azName": _field("string", "The availability zone of the host."),
        },
    ),
    "vdsSpecs": _list_of(
        "The distributed switches of the cluster.",
        {
            "name": _field("string", "The switch name."),
            "mtu": _field("integer", "The switch MTU."),
            "isUsedByNsxt": _field("boolean", "Whether NSX uses the switch."),
        },
    ),
    "domain": {
        **_ref("domain"),
        "description": "The domain of the cluster. Only the 5.2 reference lists it.",
    },
}

_HOST_FIELDS: dict[str, Any] = {
    "id": _field("string", "The host id."),
    "fqdn": _field("string", "The host FQDN."),
    "esxiVersion": _field("string", "The ESXi version."),
    "status": _field(
        "string",
        "ASSIGNED (in a domain), UNASSIGNED_USEABLE (free) or UNASSIGNED_UNUSEABLE.",
    ),
    "ipAddresses": _list_of(
        "The IPs of the host.",
        {
            "ipAddress": _field("string", "The IP address."),
            "type": _field("string", "What the IP is used for, for example MANAGEMENT."),
        },
    ),
    "domain": _ref("domain"),
    "cluster": _object(
        "The cluster the host is in.",
        {"id": _field("string", "The cluster id.")},
    ),
    "networkpool": _ref("network pool"),
    "hardwareVendor": _field("string", "The hardware vendor."),
    "hardwareModel": _field("string", "The hardware model."),
    "compatibleStorageType": _field("string", "The storage type the host fits."),
    "cpu": _object(
        "The CPU of the host.",
        {
            "cores": _field("integer", "The number of CPU cores."),
            "frequencyMHz": _field("number", "The total CPU frequency in MHz."),
            "usedFrequencyMHz": _field("number", "The used CPU frequency in MHz."),
        },
    ),
    "memory": _object(
        "The memory of the host.",
        {
            "totalCapacityMB": _field("number", "The total memory in MB."),
            "usedCapacityMB": _field("number", "The used memory in MB."),
        },
    ),
    "storage": _object(
        "The local storage of the host.",
        {
            "totalCapacityMB": _field("number", "The total storage in MB."),
            "usedCapacityMB": _field("number", "The used storage in MB."),
        },
    ),
}

_TASK_FIELDS: dict[str, Any] = {
    "id": _field("string", "The task id."),
    "name": _field("string", "The task name."),
    "type": _field("string", "The task type, for example HOST_COMMISSION."),
    "status": _field(
        "string",
        "The task state, for example PENDING, IN_PROGRESS, SUCCESSFUL, FAILED or CANCELLED.",
    ),
    "creationTimestamp": _field("string", "When the task was created."),
    "completionTimestamp": _field("string", "When the task ended."),
    "isCancellable": _field("boolean", "Whether the task can be cancelled."),
    "resolutionStatus": _field("string", "RESOLVED or UNRESOLVED, for a failed task."),
    "subTasks": _list_of(
        "The steps of the task.",
        {
            "name": _field("string", "The step name."),
            "description": _field("string", "What the step does."),
            "status": _field("string", "The step state."),
        },
    ),
    "errors": _list_of(
        "The errors, when the task failed.",
        {
            "errorCode": _field("string", "The error code."),
            "message": _field("string", "The error message."),
            "remediationMessage": _field("string", "How to fix the error."),
        },
    ),
    "resources": _list_of(
        "The objects the task works on.",
        {
            "resourceId": _field("string", "The object id."),
            "fqdn": _field("string", "The object FQDN."),
            "type": _field("string", "The object type, for example VCENTER."),
            "name": _field("string", "The object name."),
        },
    ),
}


#: Response schema per op id, consumed by
#: :mod:`meho_backplane.connectors.sddc_vcf5.typed_ops`.
SDDC_VCF5_RESPONSE_SCHEMAS: dict[str, dict[str, Any]] = {
    "sddc.vcf5.domain.list": _page_of("domains", _DOMAIN_FIELDS),
    "sddc.vcf5.cluster.list": _page_of("clusters", _CLUSTER_FIELDS),
    "sddc.vcf5.host.list": _page_of("ESXi hosts", _HOST_FIELDS),
    "sddc.vcf5.vcenter.list": _page_of(
        "vCenters",
        {
            "id": _field("string", "The vCenter id."),
            "fqdn": _field("string", "The vCenter FQDN."),
            "ipAddress": _field("string", "The vCenter IP."),
            "version": _field("string", "The vCenter version."),
            "domain": _ref("domain"),
        },
    ),
    "sddc.vcf5.nsxt_cluster.list": _page_of(
        "NSX clusters",
        {
            "id": _field("string", "The NSX cluster id."),
            "vipFqdn": _field("string", "The FQDN of the cluster's virtual IP."),
            "vip": _field("string", "The virtual IP of the cluster."),
            "version": _field("string", "The NSX version."),
            "status": _field("string", "The NSX cluster state, for example STABLE or DEGRADED."),
            "isShared": _field("boolean", "Whether more than one domain uses the cluster."),
            "isShareable": _field("boolean", "Whether a new domain can use the cluster."),
            "domains": _list_of(
                "The domains the cluster serves.",
                {
                    "id": _field("string", "The domain id."),
                    "name": _field("string", "The domain name."),
                },
            ),
            "nodes": _list_of(
                "The NSX manager nodes.",
                {
                    "id": _field("string", "The node id."),
                    "fqdn": _field("string", "The node FQDN."),
                    "ipAddress": _field("string", "The node IP."),
                    "name": _field("string", "The node VM name."),
                },
            ),
        },
    ),
    "sddc.vcf5.manager.list": _page_of(
        "SDDC Manager appliances",
        {
            "id": _field("string", "The SDDC Manager id."),
            "fqdn": _field("string", "The SDDC Manager FQDN."),
            "ipAddress": _field("string", "The SDDC Manager IP."),
            "version": _field(
                "string", "The full VCF version and build, for example 5.2.0.0-<build>."
            ),
            "domain": _ref("domain"),
        },
    ),
    "sddc.vcf5.task.list": _page_of("tasks", _TASK_FIELDS),
}
