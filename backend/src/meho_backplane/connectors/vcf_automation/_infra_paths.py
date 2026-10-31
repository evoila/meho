# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request paths of the infrastructure ops (cloud accounts, cloud zones, profiles).

Aria Automation 8.18 and the tenant plane of VCF Automation 9 serve the same
IaaS API, so both connectors send these paths (:mod:`._infra`). Every path is
in the pinned ``vra-iaas.json`` of the ``vra-sdk-go`` v0.6.5 shelf; the
reconcile lanes of both connectors map each constant to its HTTP method(s)
(``tests/_vra_infra_paths.py``) and check them against that file. The path
parameter names match the file, so the lanes compare them as they are.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "CLOUD_ACCOUNTS_NSXT_PATH",
    "CLOUD_ACCOUNTS_PATH",
    "CLOUD_ACCOUNTS_VSPHERE_PATH",
    "CLOUD_ACCOUNT_PATH",
    "FLAVOR_PROFILES_PATH",
    "FLAVOR_PROFILE_PATH",
    "NETWORK_IP_RANGES_PATH",
    "NETWORK_PROFILES_PATH",
    "PROJECTS_PATH",
    "PROJECT_PATH",
    "STORAGE_PROFILES_PATH",
    "ZONES_PATH",
    "ZONE_PATH",
]

#: Every cloud account (``GET`` list and the name check of a create).
CLOUD_ACCOUNTS_PATH: Final[str] = "/iaas/api/cloud-accounts"
#: One cloud account (``GET`` for the delete preview, ``DELETE``).
CLOUD_ACCOUNT_PATH: Final[str] = "/iaas/api/cloud-accounts/{id}"
#: Add a vCenter cloud account (``POST`` → request tracker).
CLOUD_ACCOUNTS_VSPHERE_PATH: Final[str] = "/iaas/api/cloud-accounts-vsphere"
#: Add an NSX cloud account (``POST`` → request tracker).
CLOUD_ACCOUNTS_NSXT_PATH: Final[str] = "/iaas/api/cloud-accounts-nsx-t"
#: Cloud zones (``GET`` list, ``POST`` create).
ZONES_PATH: Final[str] = "/iaas/api/zones"
#: One cloud zone (``GET`` before a change, ``PATCH`` the change).
ZONE_PATH: Final[str] = "/iaas/api/zones/{id}"
#: Flavor profiles (``GET`` list, ``POST`` create).
FLAVOR_PROFILES_PATH: Final[str] = "/iaas/api/flavor-profiles"
#: One flavor profile (``GET`` before a change, ``PATCH`` the change).
FLAVOR_PROFILE_PATH: Final[str] = "/iaas/api/flavor-profiles/{id}"
#: Network profiles (``GET``).
NETWORK_PROFILES_PATH: Final[str] = "/iaas/api/network-profiles"
#: IP ranges of the networks (``GET``).
NETWORK_IP_RANGES_PATH: Final[str] = "/iaas/api/network-ip-ranges"
#: Storage profiles (``GET``).
STORAGE_PROFILES_PATH: Final[str] = "/iaas/api/storage-profiles"
#: Projects (``GET`` name check, ``POST`` create) -- the vRA 8 project create.
PROJECTS_PATH: Final[str] = "/iaas/api/projects"
#: One project (``GET`` before a member change, ``PATCH`` the change).
PROJECT_PATH: Final[str] = "/iaas/api/projects/{id}"
