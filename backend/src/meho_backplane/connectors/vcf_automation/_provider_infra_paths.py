# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request paths of the VCFA provider ops for regions and organization quotas.

The ops connect vCenters and NSX managers, build regions from them and give
an organization a quota (a "virtual datacenter" in the API) in a region.

Most calls live under ``/cloudapi/vcf/`` on VCF Automation 9.0. The 9.1 and
9.1.1 API references list the same calls under ``/cloudapi/v1/`` and show no
``/cloudapi/vcf/`` path at all, the same move as the region list (see
:func:`~.typed_ops.provider_regions_path`). The constants below hold the 9.0
form; :func:`provider_vcf_path` gives the form for a target's version.

Two calls are legacy ``/api`` calls that the 9.x references do not list:
the vCenter refresh and the task read. The vendor's own SDK
(``go-vcloud-director`` v3, ``govcd/vsphere_vcenter.go`` "uses legacy
endpoint as there is no OpenAPI endpoint for this operation", and the task
links that every ``202`` answer carries) uses them on VCF Automation 9.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from meho_backplane.connectors.vcf_automation import typed_ops as _typed_ops
from meho_backplane.connectors.vcf_automation._lookups import quote_segment

if TYPE_CHECKING:
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "INFRA_NSX_MANAGERS_PATH",
    "INFRA_REGIONS_PATH",
    "INFRA_REGION_PATH",
    "INFRA_REGION_STORAGE_POLICIES_PATH",
    "INFRA_SUPERVISORS_PATH",
    "INFRA_TASK_PATH",
    "INFRA_TEST_CONNECTION_PATH",
    "INFRA_TRUSTED_CERTIFICATES_PATH",
    "INFRA_VCENTERS_PATH",
    "INFRA_VCENTER_PATH",
    "INFRA_VCENTER_REFRESH_PATH",
    "INFRA_VDCS_PATH",
    "INFRA_VDC_PATH",
    "INFRA_VDC_STORAGE_POLICIES_PATH",
    "INFRA_VDC_STORAGE_POLICY_PATH",
    "INFRA_VDC_VM_CLASSES_PATH",
    "INFRA_VM_CLASSES_PATH",
    "INFRA_ZONES_PATH",
    "LEGACY_API_PATHS",
    "provider_vcf_path",
    "safe_segment",
]

#: The vCenters VCF Automation knows (``GET`` list, ``POST`` add one; the
#: answer is ``202`` with a task link).
INFRA_VCENTERS_PATH: Final[str] = "/cloudapi/1.0.0/virtualCenters"
#: One vCenter (``PUT`` the whole object, for example to turn it on).
INFRA_VCENTER_PATH: Final[str] = "/cloudapi/1.0.0/virtualCenters/{vcUrn}"
#: Ask VCF Automation to connect to a host and report its certificate (``POST``).
INFRA_TEST_CONNECTION_PATH: Final[str] = "/cloudapi/1.0.0/testConnection"
#: Trust one certificate (``POST`` ``{alias, certificate}``).
INFRA_TRUSTED_CERTIFICATES_PATH: Final[str] = "/cloudapi/1.0.0/ssl/trustedCertificates"
#: Refresh one vCenter so its Supervisors appear (``POST``; legacy ``/api``).
#: ``{id}`` is the uuid part of the vCenter URN.
INFRA_VCENTER_REFRESH_PATH: Final[str] = "/api/admin/extension/vimServer/{id}/action/refresh"
#: One vendor task (``GET``; legacy ``/api``): the job a ``202`` answer started.
INFRA_TASK_PATH: Final[str] = "/api/task/{id}"
#: NSX managers (``GET`` list, ``POST`` add one).
INFRA_NSX_MANAGERS_PATH: Final[str] = "/cloudapi/vcf/nsxManagers"
#: Supervisors VCF Automation found in its vCenters (``GET``).
INFRA_SUPERVISORS_PATH: Final[str] = "/cloudapi/vcf/supervisors"
#: Regions (``POST`` create one; the list read is ``vcfa.provider.region.list``).
INFRA_REGIONS_PATH: Final[str] = "/cloudapi/vcf/regions"
#: One region (``GET`` read, ``PUT`` change).
INFRA_REGION_PATH: Final[str] = "/cloudapi/vcf/regions/{regionUrn}"
#: Zones (``GET``, filtered by region).
INFRA_ZONES_PATH: Final[str] = "/cloudapi/vcf/zones"
#: Storage policies of the regions (``GET``, filtered by region).
INFRA_REGION_STORAGE_POLICIES_PATH: Final[str] = "/cloudapi/vcf/regionStoragePolicies"
#: VM classes of the regions (``GET``, filtered by region).
INFRA_VM_CLASSES_PATH: Final[str] = "/cloudapi/vcf/virtualMachineClasses"
#: Organization quotas: one per org and region (``GET`` list, ``POST`` create).
INFRA_VDCS_PATH: Final[str] = "/cloudapi/vcf/virtualDatacenters"
#: One organization quota (``PUT`` change).
INFRA_VDC_PATH: Final[str] = "/cloudapi/vcf/virtualDatacenters/{vdcUrn}"
#: Storage limits of the quotas (``GET`` list, ``POST`` add with a ``values`` list).
INFRA_VDC_STORAGE_POLICIES_PATH: Final[str] = "/cloudapi/vcf/virtualDatacenterStoragePolicies"
#: One storage limit of a quota (``PUT`` change).
INFRA_VDC_STORAGE_POLICY_PATH: Final[str] = (
    "/cloudapi/vcf/virtualDatacenterStoragePolicies/{vdcStoragePolicyUrn}"
)
#: The VM classes a quota offers (``GET`` read, ``PUT`` replace with a ``values`` list).
INFRA_VDC_VM_CLASSES_PATH: Final[str] = (
    "/cloudapi/vcf/virtualDatacenters/{vdcUrn}/virtualMachineClasses"
)

#: The legacy ``/api`` paths: no 9.x API reference lists them (see module text).
LEGACY_API_PATHS: Final[frozenset[str]] = frozenset({INFRA_VCENTER_REFRESH_PATH, INFRA_TASK_PATH})

_VCF_PREFIX: Final = "/cloudapi/vcf/"
_V1_PREFIX: Final = "/cloudapi/v1/"
#: Path segment values that would point at another API path.
_UNSAFE_SEGMENTS: Final = frozenset({"", ".", ".."})


def provider_vcf_path(target: VcfAutomationTargetLike, path: str) -> str:
    """Return *path* in the form the API reference of *target*'s version shows.

    A ``/cloudapi/vcf/`` path becomes ``/cloudapi/v1/`` on 9.1 and newer.
    The version rule is the one of the region list, so the two never
    disagree. Other paths come back unchanged.
    """
    if not path.startswith(_VCF_PREFIX):
        return path
    # Imported as a module: a *_PATH name here would join the reconcile sweep.
    if _typed_ops.provider_regions_path(target) == _typed_ops.PROVIDER_REGIONS_V1_PATH:
        return _V1_PREFIX + path.removeprefix(_VCF_PREFIX)
    return path


def safe_segment(value: object, what: str) -> str:
    """Check one caller or vendor value for a URL path, then percent-encode it.

    Refuses an empty value, ``.`` and ``..`` (they would point at another
    path) with :exc:`ValueError`, and encodes every other character that is
    not safe in one path segment (``/``, ``?``, ``#`` and the rest).
    """
    text = str(value).strip() if value is not None else ""
    if text in _UNSAFE_SEGMENTS:
        raise ValueError(f"{what} {value!r} is not a valid id for a URL path")
    return quote_segment(text)
