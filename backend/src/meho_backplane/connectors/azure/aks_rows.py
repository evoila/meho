# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers of the Azure Kubernetes Service (AKS) reads.

Two kinds of functions. None of them calls Azure:

* **Rows.** Each ``*_row`` function turns one item of an Azure answer into
  one row of a MEHO answer. Every field is read with a type check, so an odd
  answer gives ``None`` in a field, never an error. An item that is not an
  object is left out by :func:`object_rows`.
* **Parameter checks.** :func:`resource_group_name`, :func:`cluster_name`
  and :func:`location_name` check a caller's value before it goes into a
  path. A value that is not usable raises :class:`AksParameterError` before
  any request is made.

Only the fields named in the rows are copied. The rest of Azure's answer
(for example the SSH public keys, the add-on settings, the node labels and
the service principal profile) is left out, so nothing secret-shaped reaches
a result. Text from Azure is copied as data and never used to build a
request.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from meho_backplane.connectors.azure._paging import PagedRows

__all__ = [
    "CLUSTER_NAME_PATTERN",
    "LOCATION_PATTERN",
    "RESOURCE_GROUP_MAX",
    "RESOURCE_GROUP_PATTERN",
    "AksParameterError",
    "cluster_detail",
    "cluster_name",
    "cluster_row",
    "default_version",
    "list_answer",
    "location_name",
    "node_pool_row",
    "object_rows",
    "resource_group_name",
    "upgrade_options",
    "version_key",
    "version_rows",
]

#: A resource group name: letters and digits (any script), ``_``, ``-``,
#: ``.``, ``(`` and ``)``, and not only dots. No slash, ``?``, ``#``, ``%``,
#: quote or space can pass, so a name never changes the path.
RESOURCE_GROUP_PATTERN: Final[str] = r"^(?!\.+$)[-\w.()]+$"

#: The longest resource group name Azure allows.
RESOURCE_GROUP_MAX: Final[int] = 90

#: A cluster name as Azure's API description writes it: 1 to 63 letters,
#: digits, ``-`` and ``_``, starting and ending with a letter or a digit.
CLUSTER_NAME_PATTERN: Final[str] = r"^[a-zA-Z0-9]$|^[a-zA-Z0-9][-_a-zA-Z0-9]{0,61}[a-zA-Z0-9]$"

#: A region name as Azure uses it in requests: letters and digits only,
#: for example ``westeurope``.
LOCATION_PATTERN: Final[str] = r"^[A-Za-z0-9]{1,64}$"

_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_PATTERN)
_CLUSTER_NAME: Final[re.Pattern[str]] = re.compile(CLUSTER_NAME_PATTERN)
_LOCATION: Final[re.Pattern[str]] = re.compile(LOCATION_PATTERN)

#: What a managed identity cluster has in ``servicePrincipalProfile.clientId``
#: instead of the application ID of a service principal.
_MANAGED_IDENTITY_CLIENT_ID: Final[str] = "msi"

#: The identity types that mean a managed identity.
_MANAGED_IDENTITY_TYPES: Final[frozenset[str]] = frozenset({"systemassigned", "userassigned"})

#: Add-ons and managed features that Azure keeps outside ``addonProfiles``:
#: MEHO's name, the keys that lead to the value, and the value that means "on".
_PROFILE_ADDONS: Final[tuple[tuple[str, tuple[str, ...], object], ...]] = (
    ("app_routing", ("ingressProfile", "webAppRouting", "enabled"), True),
    ("container_insights", ("azureMonitorProfile", "containerInsights", "enabled"), True),
    ("istio", ("serviceMeshProfile", "mode"), "Istio"),
    ("keda", ("workloadAutoScalerProfile", "keda", "enabled"), True),
    ("managed_prometheus", ("azureMonitorProfile", "metrics", "enabled"), True),
    ("pod_identity", ("podIdentityProfile", "enabled"), True),
    ("workload_identity", ("securityProfile", "workloadIdentity", "enabled"), True),
)


class AksParameterError(ValueError):
    """A parameter of an AKS read is not usable. Raised before any request.

    The message names the target and the parameter, never the value. The
    dispatcher returns it as ``connector_error`` with ``remediation``.
    """

    def __init__(self, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


# ----------------------------------------------------------------------
# Reading Azure's answers
# ----------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _texts(value: object) -> list[str]:
    """The non-empty texts in *value* when it is a list, else an empty list."""
    return [item for item in _items(value) if isinstance(item, str) and item]


def _dig(data: Mapping[str, Any], keys: tuple[str, ...]) -> object:
    """The value under *keys* (one per level), or ``None`` when a level is missing."""
    value: object = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _resource_group(resource_id: str | None) -> str | None:
    """The resource group named in a resource ID, as Azure wrote it."""
    if resource_id is None:
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[index + 1] or None
    return None


def version_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key of a Kubernetes version, so ``1.29.10`` comes after ``1.29.9``.

    Each dotted part that is a number compares as a number. Any other part
    compares as text, after the numbers.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in version.split(".")
    )


def _sorted_versions(versions: list[str]) -> list[str]:
    return sorted(set(versions), key=version_key)


def object_rows(
    items: list[Any], to_row: Callable[[dict[str, Any]], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Turn every object in *items* into a row with *to_row*; leave out the rest."""
    return [to_row(item) for item in items if isinstance(item, dict)]


def list_answer(paged: PagedRows, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """The answer of a list read.

    ``value`` holds *rows* and ``count`` their number. Then come the paging
    fields of :meth:`PagedRows.as_dict` and *extra*. They are all plain
    values, so they stay next to the stored-result link when the rows go
    behind one.
    """
    paging = {key: value for key, value in paged.as_dict().items() if key != "value"}
    return {"value": rows, "count": len(rows), **paging, **extra}


# ----------------------------------------------------------------------
# Clusters
# ----------------------------------------------------------------------


def _identity_type(item: Mapping[str, Any], properties: Mapping[str, Any]) -> str | None:
    """How the cluster logs in to Azure.

    ``SystemAssigned`` or ``UserAssigned`` for a managed identity, as Azure
    reports it in ``identity.type``. ``ServicePrincipal`` when the cluster
    has no managed identity and its ``servicePrincipalProfile`` names an
    application: then it logs in with a service principal and a secret.
    Otherwise Azure's ``identity.type`` as it is (``None`` or missing).
    """
    kind = _text(_object(item.get("identity")).get("type"))
    if kind is not None and kind.lower() in _MANAGED_IDENTITY_TYPES:
        return kind
    client_id = _text(_object(properties.get("servicePrincipalProfile")).get("clientId"))
    if client_id is not None and client_id.lower() != _MANAGED_IDENTITY_CLIENT_ID:
        return "ServicePrincipal"
    return kind


def _addons(properties: Mapping[str, Any]) -> list[str]:
    """The add-ons and managed features that are on, sorted.

    Azure's own name of every ``addonProfiles`` entry with ``enabled: true``
    (for example ``omsagent`` or ``azurepolicy``), plus MEHO's name of each
    add-on Azure keeps in another setting (:data:`_PROFILE_ADDONS`). An
    add-on's settings are never copied.
    """
    names = {
        name
        for name, profile in _object(properties.get("addonProfiles")).items()
        if isinstance(name, str) and name and _object(profile).get("enabled") is True
    }
    for name, keys, on in _PROFILE_ADDONS:
        value = _dig(properties, keys)
        # The type check keeps a 1 from counting as true.
        if type(value) is type(on) and value == on:
            names.add(name)
    return sorted(names, key=str.lower)


def _node_count(profiles: list[Any]) -> int | None:
    """The sum of the node counts of the pools; ``None`` when no pool has one."""
    counts = [_int(_object(profile).get("count")) for profile in profiles]
    known = [count for count in counts if count is not None]
    return sum(known) if known else None


def cluster_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.aks.cluster.list`` from one ``ManagedCluster``."""
    properties = _object(item.get("properties"))
    network = _object(properties.get("networkProfile"))
    auto_upgrade = _object(properties.get("autoUpgradeProfile"))
    api_server = _object(properties.get("apiServerAccessProfile"))
    pools = _items(properties.get("agentPoolProfiles"))
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "id": resource_id,
        "resource_group": _resource_group(resource_id),
        "location": _text(item.get("location")),
        "kubernetes_version": _text(properties.get("kubernetesVersion")),
        "current_kubernetes_version": _text(properties.get("currentKubernetesVersion")),
        "provisioning_state": _text(properties.get("provisioningState")),
        "power_state": _text(_object(properties.get("powerState")).get("code")),
        "sku_tier": _text(_object(item.get("sku")).get("tier")),
        "support_plan": _text(properties.get("supportPlan")),
        "node_resource_group": _text(properties.get("nodeResourceGroup")),
        "network_plugin": _text(network.get("networkPlugin")),
        "network_plugin_mode": _text(network.get("networkPluginMode")),
        "network_policy": _text(network.get("networkPolicy")),
        "network_dataplane": _text(network.get("networkDataplane")),
        "outbound_type": _text(network.get("outboundType")),
        "private_cluster": api_server.get("enablePrivateCluster") is True,
        "identity_type": _identity_type(item, properties),
        "auto_upgrade_channel": _text(auto_upgrade.get("upgradeChannel")),
        "node_os_upgrade_channel": _text(auto_upgrade.get("nodeOSUpgradeChannel")),
        "node_pools": [
            name for name in (_text(_object(pool).get("name")) for pool in pools) if name
        ],
        "node_count": _node_count(pools),
        "addons": _addons(properties),
    }


def _cidrs(network: Mapping[str, Any], many: str, one: str) -> list[str]:
    """The address ranges in ``podCidrs`` (or ``serviceCidrs``), else the single field."""
    ranges = _texts(network.get(many))
    if ranges:
        return ranges
    single = _text(network.get(one))
    return [single] if single else []


def _pool_upgrades(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The versions in one ``ManagedClusterPoolUpgradeProfile``, preview ones apart."""
    released: list[str] = []
    preview: list[str] = []
    for upgrade in _items(profile.get("upgrades")):
        data = _object(upgrade)
        version = _text(data.get("kubernetesVersion"))
        if version is not None:
            (preview if data.get("isPreview") is True else released).append(version)
    return {
        "kubernetes_version": _text(profile.get("kubernetesVersion")),
        "upgrades": _sorted_versions(released),
        "preview_upgrades": _sorted_versions(preview),
    }


def upgrade_options(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The ``upgrades`` block of ``azure.aks.cluster.show`` from the upgrade profile."""
    properties = _object(answer.get("properties"))
    pools = [
        {
            "name": _text(profile.get("name")),
            "os_type": _text(profile.get("osType")),
            **_pool_upgrades(profile),
        }
        for profile in _items(properties.get("agentPoolProfiles"))
        if isinstance(profile, dict)
    ]
    return {
        "control_plane": _pool_upgrades(_object(properties.get("controlPlaneProfile"))),
        "node_pools": pools,
    }


def cluster_detail(cluster: dict[str, Any], upgrade_profile: Mapping[str, Any]) -> dict[str, Any]:
    """The answer of ``azure.aks.cluster.show``: the cluster row, a few details, the upgrades."""
    properties = _object(cluster.get("properties"))
    network = _object(properties.get("networkProfile"))
    return {
        **cluster_row(cluster),
        "fqdn": _text(properties.get("fqdn")),
        "private_fqdn": _text(properties.get("privateFQDN")),
        "pod_cidrs": _cidrs(network, "podCidrs", "podCidr"),
        "service_cidrs": _cidrs(network, "serviceCidrs", "serviceCidr"),
        "upgrades": upgrade_options(upgrade_profile),
    }


# ----------------------------------------------------------------------
# Node pools and versions
# ----------------------------------------------------------------------


def node_pool_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.aks.nodepool.list`` from one ``AgentPool``."""
    properties = _object(item.get("properties"))
    return {
        "name": _text(item.get("name")),
        "mode": _text(properties.get("mode")),
        "vm_size": _text(properties.get("vmSize")),
        "count": _int(properties.get("count")),
        "autoscaling": properties.get("enableAutoScaling") is True,
        "min_count": _int(properties.get("minCount")),
        "max_count": _int(properties.get("maxCount")),
        "os_type": _text(properties.get("osType")),
        "os_sku": _text(properties.get("osSKU")),
        "orchestrator_version": _text(properties.get("orchestratorVersion")),
        "current_orchestrator_version": _text(properties.get("currentOrchestratorVersion")),
        "node_image_version": _text(properties.get("nodeImageVersion")),
        "power_state": _text(_object(properties.get("powerState")).get("code")),
        "provisioning_state": _text(properties.get("provisioningState")),
        "availability_zones": _texts(properties.get("availabilityZones")),
        "max_pods": _int(properties.get("maxPods")),
        "priority": _text(properties.get("scaleSetPriority")),
        "pool_type": _text(properties.get("type")),
    }


def _version_row(item: dict[str, Any]) -> dict[str, Any]:
    """One row of ``azure.aks.version.list`` from one ``KubernetesVersion``."""
    patches = _object(item.get("patchVersions"))
    return {
        "version": _text(item.get("version")),
        "is_default": item.get("isDefault") is True,
        "is_preview": item.get("isPreview") is True,
        "support_plans": _texts(_object(item.get("capabilities")).get("supportPlan")),
        "patch_versions": _sorted_versions(
            [patch for patch in patches if isinstance(patch, str) and patch]
        ),
    }


def version_rows(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The rows of ``azure.aks.version.list``, oldest version first.

    Azure answers with ``values`` (not ``value``) and no next page.
    """
    rows = object_rows(_items(answer.get("values")), _version_row)
    return sorted(rows, key=lambda row: version_key(row["version"] or ""))


def default_version(rows: list[dict[str, Any]]) -> str | None:
    """The version AKS uses when a new cluster names none, or ``None``."""
    return next((row["version"] for row in rows if row["is_default"]), None)


# ----------------------------------------------------------------------
# Parameter checks
# ----------------------------------------------------------------------


def resource_group_name(value: object, *, target_name: str) -> str:
    """A checked resource group name for a path. Raises :class:`AksParameterError`.

    Checked with ``fullmatch``, so a value with a line break at the end
    fails too (a ``$`` in a JSON Schema pattern lets one through).
    """
    if (
        isinstance(value, str)
        and len(value) <= RESOURCE_GROUP_MAX
        and _RESOURCE_GROUP.fullmatch(value)
    ):
        return value
    remediation = (
        "Pass the resource group of the cluster as azure.aks.cluster.list shows it: "
        "letters, digits, '_', '-', '.', '(' and ')', at most 90 characters."
    )
    raise AksParameterError(
        f"Azure target {target_name!r}: 'resource_group' is not a resource group name. "
        f"{remediation}",
        remediation=remediation,
    )


def cluster_name(value: object, param: str, *, target_name: str) -> str:
    """A checked AKS cluster name for a path. Raises :class:`AksParameterError`."""
    if isinstance(value, str) and _CLUSTER_NAME.fullmatch(value):
        return value
    remediation = (
        "Pass the cluster name as azure.aks.cluster.list shows it: 1 to 63 letters, "
        "digits, '-' and '_', starting and ending with a letter or a digit."
    )
    raise AksParameterError(
        f"Azure target {target_name!r}: {param!r} is not an AKS cluster name. {remediation}",
        remediation=remediation,
    )


def location_name(value: object, *, target_name: str) -> str:
    """A checked region name for a path. Raises :class:`AksParameterError`."""
    if isinstance(value, str) and _LOCATION.fullmatch(value):
        return value
    remediation = (
        "Pass the region name Azure uses in requests, for example westeurope (not the "
        "display name West Europe). azure.location.list lists the names."
    )
    raise AksParameterError(
        f"Azure target {target_name!r}: 'location' is not a region name. {remediation}",
        remediation=remediation,
    )
