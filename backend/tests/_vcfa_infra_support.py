# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written vendor answers for the VCFA region and quota op tests.

Shared by ``test_connectors_vcf_automation_infra_reads.py`` and
``test_connectors_vcf_automation_infra_changes.py``. Every name, id, host and
certificate here is made up. The shapes follow the VCF Automation provider
API reference (``VCenterServer``, ``NsxManager``, ``Supervisor``, ``Region``,
``Zone``, ``Vdc``, ``VirtualDatacenterStoragePolicy``) and the vendor SDK.
"""

from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from typing import Any

from meho_backplane.operations._preview import approved_preview_var

VC_ID = "urn:vcloud:vimserver:aaaaaaaa-1111-2222-3333-444444444444"
VC_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
VC_URL = "https://vc-example.test.invalid"
NSX_ID = "urn:vcloud:nsxtmanager:bbbbbbbb-1111-2222-3333-444444444444"
NSX_URL = "https://nsx-example.test.invalid"
SUP_ID = "urn:vcloud:supervisor:cccccccc-1111-2222-3333-444444444444"
REGION_ID = "urn:vcloud:region:dddddddd-1111-2222-3333-444444444444"
ZONE_ID = "urn:vcloud:zone:eeeeeeee-1111-2222-3333-444444444444"
RSP_ID = "urn:vcloud:regionStoragePolicy:ffffffff-1111-2222-3333-444444444444"
RSP2_ID = "urn:vcloud:regionStoragePolicy:99999999-1111-2222-3333-444444444444"
VMC_ID = "urn:vcloud:regionVirtualMachineClass:12121212-1111-2222-3333-444444444444"
VDC_ID = "urn:vcloud:vdc:34343434-1111-2222-3333-444444444444"
VDC_SP_ID = "urn:vcloud:virtualDatacenterStoragePolicy:56565656-1111-2222-3333-444444444444"
TASK_ID = "78787878-1111-2222-3333-444444444444"
TASK_LOCATION = f"https://vcfa-prov.test.invalid/api/task/{TASK_ID}"

_DER = b"made-up certificate bytes for the fingerprint test"
CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    + base64.b64encode(_DER).decode()
    + "\n-----END CERTIFICATE-----\n"
)
_HEX = hashlib.sha256(_DER).hexdigest().upper()
CERT_FINGERPRINT = ":".join(_HEX[i : i + 2] for i in range(0, len(_HEX), 2))

#: Paths in the 9.0 form (the seeded target is VCF Automation 9.0).
VCENTERS = "/cloudapi/1.0.0/virtualCenters"
TEST_CONNECTION = "/cloudapi/1.0.0/testConnection"
TRUSTED_CERTS = "/cloudapi/1.0.0/ssl/trustedCertificates"
NSX_MANAGERS = "/cloudapi/vcf/nsxManagers"
SUPERVISORS = "/cloudapi/vcf/supervisors"
REGIONS = "/cloudapi/vcf/regions"
ZONES = "/cloudapi/vcf/zones"
REGION_POLICIES = "/cloudapi/vcf/regionStoragePolicies"
VM_CLASSES = "/cloudapi/vcf/virtualMachineClasses"
VDCS = "/cloudapi/vcf/virtualDatacenters"
VDC_POLICIES = "/cloudapi/vcf/virtualDatacenterStoragePolicies"
TASK = f"/api/task/{TASK_ID}"


def quoted(urn: str) -> str:
    """A URN as one percent-encoded path segment."""
    return urn.replace(":", "%3A")


def vcenter(*, state: str = "CONNECTED", enabled: bool = True) -> dict[str, Any]:
    return {
        "vcId": VC_ID,
        "name": "vc-example",
        "url": VC_URL,
        "username": "svc-example",
        "password": "vendor-echo-sentinel",
        "isEnabled": enabled,
        "isConnected": state == "CONNECTED",
        "listenerState": state,
        "vcVersion": "9.0.0",
    }


def nsx_manager(*, status: str = "REALIZED") -> dict[str, Any]:
    return {"id": NSX_ID, "name": "nsx-example", "url": NSX_URL, "status": status, "active": True}


def supervisor(*, region: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "supervisorId": SUP_ID,
        "name": "sup-example",
        "region": region,
        "virtualCenter": {"name": "vc-example", "id": VC_ID},
    }


def region(*, status: str = "READY", policies: tuple[str, ...] = ("gold",)) -> dict[str, Any]:
    return {
        "id": REGION_ID,
        "name": "region-a",
        "description": "",
        "status": status,
        "nsxManager": {"name": "nsx-example", "id": NSX_ID},
        "supervisors": [{"name": "sup-example", "id": SUP_ID}],
        "storagePolicies": list(policies),
        "cpuCapacityMHz": 100000,
        "memoryCapacityMiB": 262144,
    }


REGION_REF = {"name": "region-a", "id": REGION_ID}


def zone() -> dict[str, Any]:
    return {
        "id": ZONE_ID,
        "name": "zone-a",
        "region": REGION_REF,
        "cpuLimitMHz": 50000,
        "memoryLimitMiB": 131072,
    }


def region_policy(policy_id: str = RSP_ID, name: str = "gold") -> dict[str, Any]:
    return {"id": policy_id, "name": name, "region": REGION_REF, "status": "READY"}


def vm_class() -> dict[str, Any]:
    return {"id": VMC_ID, "name": "best-effort-small", "region": REGION_REF, "cpuCount": 2}


def vdc(*, status: str = "READY", cpu_limit: int = 10000) -> dict[str, Any]:
    return {
        "id": VDC_ID,
        "name": "quota-a",
        "org": {"name": "example-org", "id": "urn:vcloud:org:11111111-2222-3333-4444-555555555555"},
        "region": REGION_REF,
        "status": status,
        "supervisors": [{"name": "sup-example", "id": SUP_ID}],
        "zoneResourceAllocation": [
            {
                "zone": {"name": "zone-a", "id": ZONE_ID},
                "resourceAllocation": {
                    "cpuLimitMHz": cpu_limit,
                    "cpuReservationMHz": 0,
                    "memoryLimitMiB": 32768,
                    "memoryReservationMiB": 0,
                },
            }
        ],
    }


def vdc_policy(limit: int = 102400) -> dict[str, Any]:
    return {
        "id": VDC_SP_ID,
        "name": "gold",
        "virtualDatacenter": {"name": "quota-a", "id": VDC_ID},
        "regionStoragePolicy": {"name": "gold", "id": RSP_ID},
        "storageLimitMiB": limit,
        "storageUsedMiB": 0,
    }


def probe(ssl_result: str = "ERROR_UNTRUSTED_CERTIFICATE") -> dict[str, Any]:
    """A ``testConnection`` answer."""
    return {
        "targetProbe": {
            "connectionResult": "SUCCESS",
            "sslResult": ssl_result,
            "certificateChain": CERT_PEM,
            "canConnect": True,
        }
    }


@contextmanager
def approved_with(op_id: str, preview: dict[str, Any]):
    """Run as the approved re-run of *op_id* whose park stored *preview*."""
    token = approved_preview_var.set((op_id, preview))
    try:
        yield
    finally:
        approved_preview_var.reset(token)
