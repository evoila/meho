# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request paths of the provider organization-settings and networking ops.

The paths follow the VCF Automation reference lists of the "Provider
Infrastructure APIs" (made from the vendor's public reference pages, one list
per version: 9.0, 9.1 and 9.1.1). Each path parameter has the name the
reference page gives it.

The networking objects (IP spaces, provider gateways, IP space links,
regional networking, edge clusters) sit under ``/cloudapi/vcf/`` in the 9.0
reference. The 9.1 and 9.1.1 references list the same calls only under
``/cloudapi/v1/``. :func:`provider_path` picks the prefix for the target's
version, the same rule ``vcfa.provider.region.list`` uses. The constants
below hold the 9.0 form; the spec-reconcile lane checks both forms against
the reference list of each version.

The organization calls (``/cloudapi/1.0.0/orgs/...``) and the Tier-0 list
(``/cloudapi/1.0.0/nsxTResources/importableTier0Routers``) have the same
path in every 9.x version.

One call is a legacy ``/api`` call that the 9.x references do not list: the
task read. The vendor's own SDK (``go-vcloud-director`` v3) follows the task
link that every ``202`` answer carries through it on VCF Automation 9.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "INFRA_TASK_PATH",
    "PROVIDER_EDGE_CLUSTERS_PATH",
    "PROVIDER_GATEWAYS_PATH",
    "PROVIDER_GATEWAY_PATH",
    "PROVIDER_IP_SPACES_PATH",
    "PROVIDER_IP_SPACE_ASSOCIATIONS_PATH",
    "PROVIDER_IP_SPACE_ASSOCIATION_PATH",
    "PROVIDER_IP_SPACE_PATH",
    "PROVIDER_ORG_NETWORKING_PATH",
    "PROVIDER_ORG_PATH",
    "PROVIDER_REGIONAL_NETWORKING_PATH",
    "PROVIDER_REGIONAL_NETWORKING_SETTING_PATH",
    "PROVIDER_TIER0_ROUTERS_PATH",
    "V1_MIN_VERSION",
    "bad_id_reason",
    "is_v1_target",
    "provider_path",
    "target_version_label",
]

#: One organization (``GET`` read, ``PUT`` change).
PROVIDER_ORG_PATH: Final[str] = "/cloudapi/1.0.0/orgs/{orgUrn}"
#: The networking settings of one organization (``GET``, ``PUT``).
PROVIDER_ORG_NETWORKING_PATH: Final[str] = "/cloudapi/1.0.0/orgs/{orgUrn}/networkingSettings"
#: The NSX Tier-0 gateways that no provider gateway uses yet (``GET``). The
#: list is empty unless the ``filter`` names a region or an NSX manager.
PROVIDER_TIER0_ROUTERS_PATH: Final[str] = "/cloudapi/1.0.0/nsxTResources/importableTier0Routers"
#: IP spaces (``GET`` list, ``POST`` create).
PROVIDER_IP_SPACES_PATH: Final[str] = "/cloudapi/vcf/ipSpaces"
#: One IP space (``GET`` read, ``PUT`` change, ``DELETE``).
PROVIDER_IP_SPACE_PATH: Final[str] = "/cloudapi/vcf/ipSpaces/{ipSpaceId}"
#: Provider gateways (``GET`` list, ``POST`` create).
PROVIDER_GATEWAYS_PATH: Final[str] = "/cloudapi/vcf/providerGateways"
#: One provider gateway (``GET`` read, ``DELETE``).
PROVIDER_GATEWAY_PATH: Final[str] = "/cloudapi/vcf/providerGateways/{gatewayId}"
#: Links between an IP space and a provider gateway (``GET`` list, ``POST``).
PROVIDER_IP_SPACE_ASSOCIATIONS_PATH: Final[str] = "/cloudapi/vcf/ipSpaceAssociations"
#: One IP space link (``GET`` read, ``DELETE``).
PROVIDER_IP_SPACE_ASSOCIATION_PATH: Final[str] = (
    "/cloudapi/vcf/ipSpaceAssociations/{ipSpaceAssociationId}"
)
#: Regional networking of organizations (``GET`` list, ``POST`` create).
PROVIDER_REGIONAL_NETWORKING_PATH: Final[str] = "/cloudapi/vcf/regionalNetworkingSettings"
#: One regional networking setting (``GET`` read, ``DELETE``).
PROVIDER_REGIONAL_NETWORKING_SETTING_PATH: Final[str] = (
    "/cloudapi/vcf/regionalNetworkingSettings/{settingId}"
)
#: NSX edge clusters (``GET``).
PROVIDER_EDGE_CLUSTERS_PATH: Final[str] = "/cloudapi/vcf/edgeClusters"
#: One vendor task (``GET``; legacy ``/api``): the job a ``202`` answer started.
INFRA_TASK_PATH: Final[str] = "/api/task/{id}"

_VCF_PREFIX: Final = "/cloudapi/vcf/"
_V1_PREFIX: Final = "/cloudapi/v1/"
#: First VCF Automation release whose reference lists the ``/cloudapi/v1/`` form.
V1_MIN_VERSION: Final = Version("9.1")
#: A date-shaped label that happens to parse (``2021.07``) is not a release.
_PRODUCT_VERSION_CEILING: Final = Version("100")


def target_version_label(target: VcfAutomationTargetLike) -> str | None:
    """The product version recorded for *target*, or ``None``."""
    return resolve_target_version(target)


def is_v1_target(target: VcfAutomationTargetLike) -> bool:
    """True when *target* runs VCF Automation 9.1 or newer.

    No version, or a label that is not a product version, counts as 9.0:
    the 9.0 form is the one 9.0 documents, and a 9.1 system still answers it.
    """
    raw = resolve_target_version(target)
    if raw is None:
        return False
    try:
        version = Version(raw)
    except InvalidVersion:
        return False
    return V1_MIN_VERSION <= version < _PRODUCT_VERSION_CEILING


def provider_path(target: VcfAutomationTargetLike, path: str, **ids: str) -> str:
    """*path* for *target*'s version, with each ``{name}`` set to a percent-encoded id.

    A ``/cloudapi/vcf/`` path becomes ``/cloudapi/v1/`` on 9.1 and newer.
    Check each id with :func:`bad_id_reason` first.
    """
    if path.startswith(_VCF_PREFIX) and is_v1_target(target):
        path = _V1_PREFIX + path.removeprefix(_VCF_PREFIX)
    return path.format(**{name: quote(value, safe="") for name, value in ids.items()})


def bad_id_reason(value: object, what: str) -> str | None:
    """Why *value* cannot go into a URL path as the *what* id, or ``None`` when it can.

    Refuses an empty value, ``.`` and ``..`` (they would point at another
    path), and a value with a slash, a backslash or a control character.
    """
    if not isinstance(value, str) or not value.strip():
        return f"the {what} id is empty"
    if value in {".", ".."}:
        return f"the {what} id {value!r} is not an id"
    if any(char in value for char in "/\\") or any(ord(char) < 32 for char in value):
        return f"the {what} id {value!r} holds a character an id cannot have"
    return None
