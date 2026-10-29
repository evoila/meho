# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure compute reads: VMs, disks, images, scale sets and VM sizes.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.compute.ComputeOps`. The operation
table and the guidance for the AI are in :mod:`.compute`.

The rows are built in :mod:`.compute_rows` from a short list of named
fields, so nothing else from Azure's answer reaches the result.

``azure.vm.list`` makes two paged reads, never one call per VM: the VM list,
and the subscription's VM list with ``statusOnly=true``, which holds each
VM's power state. The two are joined by the VM's resource ID.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import PagedRows, list_all_pages
from meho_backplane.connectors.azure._paths import (
    COMPUTE_API_VERSION,
    COMPUTE_SKUS_API_VERSION,
    COMPUTE_SKUS_PATH,
    DISKS_API_VERSION,
    DISKS_PATH,
    IMAGES_PATH,
    RESOURCE_GROUP_DISKS_PATH,
    RESOURCE_GROUP_IMAGES_PATH,
    RESOURCE_GROUP_SCALE_SETS_PATH,
    RESOURCE_GROUP_VIRTUAL_MACHINES_PATH,
    SCALE_SETS_PATH,
    VIRTUAL_MACHINE_PATH,
    VIRTUAL_MACHINES_PATH,
)
from meho_backplane.connectors.azure.compute_rows import (
    disk_row,
    image_row,
    power_state_read,
    power_states_by_id,
    scale_set_row,
    size_row,
    vm_detail,
    vm_row,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "LOCATION_PATTERN",
    "NAME_PATTERN",
    "disk_list_impl",
    "image_list_impl",
    "vm_list_impl",
    "vm_show_impl",
    "vm_size_list_impl",
    "vmss_list_impl",
]

#: A resource group or VM name. It may not hold a character that changes a
#: URL (``/``, ``\\``, ``?``, ``#``, ``%``) or a space, and it may not be dots
#: only. Azure checks the rest of its own naming rules.
NAME_PATTERN: Final[str] = r"^(?!\.+$)[^/\\?#%\s]+$"

#: A region name as Azure writes it in requests, for example ``westeurope``.
LOCATION_PATTERN: Final[str] = r"^[A-Za-z0-9]{1,64}$"

_NAME: Final[re.Pattern[str]] = re.compile(NAME_PATTERN)
_LOCATION: Final[re.Pattern[str]] = re.compile(LOCATION_PATTERN)


# ----------------------------------------------------------------------
# Parameters and paths
# ----------------------------------------------------------------------


def _name(params: dict[str, Any], key: str) -> str:
    """A name parameter, checked again here; the schema checks it first.

    The error never repeats the value: a value in the wrong field can be a
    secret.
    """
    value = params.get(key)
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(
            f"Parameter {key!r} must be an Azure name without '/', '\\', '?', '#', "
            "'%' or spaces, and not dots only."
        )
    return value


def _location(params: dict[str, Any]) -> str:
    """The ``location`` parameter in lower case, for example ``westeurope``."""
    value = params.get("location")
    if not isinstance(value, str) or not _LOCATION.fullmatch(value):
        raise ValueError(
            "Parameter 'location' must be an Azure region name such as westeurope: "
            "letters and digits only. azure.location.list lists the names."
        )
    return value.lower()


def _path(template: str, subscription_id: str, **names: str) -> str:
    """Fill a path template. Each name is percent-encoded, so it stays one segment.

    *subscription_id* is already checked as a GUID by
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`.
    """
    encoded = {key: quote(value, safe="") for key, value in names.items()}
    return template.format(subscription_id=subscription_id, **encoded)


def _list_path(subscription_id: str, params: dict[str, Any], whole: str, in_group: str) -> str:
    """The subscription's list path, or the resource group's when one is named."""
    if params.get("resource_group") is None:
        return _path(whole, subscription_id)
    return _path(in_group, subscription_id, resource_group=_name(params, "resource_group"))


# ----------------------------------------------------------------------
# Operation bodies
# ----------------------------------------------------------------------


async def vm_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vm.list``: the VMs of the subscription or of one resource group.

    Two paged reads: the VM list, then the subscription's VM list with
    ``statusOnly=true`` for the power states (Azure offers ``statusOnly`` on
    the subscription list only, so it also covers a resource group). The
    second read is skipped when there is no VM. ``power_state_read`` says
    how complete the power states are.
    """
    subscription_id = resolve_target_config(target).subscription_id
    path = _list_path(
        subscription_id, params, VIRTUAL_MACHINES_PATH, RESOURCE_GROUP_VIRTUAL_MACHINES_PATH
    )
    vms = await list_all_pages(
        connector, target, path, operator=operator, api_version=COMPUTE_API_VERSION
    )
    states = PagedRows()
    if vms.rows:
        states = await list_all_pages(
            connector,
            target,
            _path(VIRTUAL_MACHINES_PATH, subscription_id),
            operator=operator,
            api_version=COMPUTE_API_VERSION,
            params={"statusOnly": "true"},
        )
    power_states = power_states_by_id(states.rows)
    rows = (vm_row(item, power_states) for item in vms.rows)
    vms.rows = [row for row in rows if row is not None]
    result = vms.as_dict()
    result["power_state_read"] = power_state_read(states, vms.rows)
    return result


async def vm_show_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vm.show``: one VM with its live state, in one call (``$expand=instanceView``)."""
    path = _path(
        VIRTUAL_MACHINE_PATH,
        resolve_target_config(target).subscription_id,
        resource_group=_name(params, "resource_group"),
        vm_name=_name(params, "name"),
    )
    response = await send(
        connector,
        target,
        "GET",
        path,
        operator=operator,
        params={"api-version": COMPUTE_API_VERSION, "$expand": "instanceView"},
    )
    return vm_detail(json_payload_or_empty(response))


async def disk_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.disk.list``: the managed disks of the subscription or of one resource group."""
    subscription_id = resolve_target_config(target).subscription_id
    path = _list_path(subscription_id, params, DISKS_PATH, RESOURCE_GROUP_DISKS_PATH)
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=DISKS_API_VERSION
    )
    paged.rows = [row for row in map(disk_row, paged.rows) if row is not None]
    return paged.as_dict()


async def image_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.image.list``: the managed images of the subscription or of one resource group."""
    subscription_id = resolve_target_config(target).subscription_id
    path = _list_path(subscription_id, params, IMAGES_PATH, RESOURCE_GROUP_IMAGES_PATH)
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=COMPUTE_API_VERSION
    )
    paged.rows = [row for row in map(image_row, paged.rows) if row is not None]
    return paged.as_dict()


async def vmss_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vmss.list``: the scale sets of the subscription or of one resource group."""
    subscription_id = resolve_target_config(target).subscription_id
    path = _list_path(subscription_id, params, SCALE_SETS_PATH, RESOURCE_GROUP_SCALE_SETS_PATH)
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=COMPUTE_API_VERSION
    )
    paged.rows = [row for row in map(scale_set_row, paged.rows) if row is not None]
    return paged.as_dict()


async def vm_size_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.vm.size.list``: the VM sizes of one region, with the restrictions.

    One paged read of the Resource SKUs list with the filter
    ``location eq '<region>'`` (the only filter Azure supports there). The
    answer holds every kind of compute SKU; only the VM sizes are kept.
    """
    location = _location(params)
    paged = await list_all_pages(
        connector,
        target,
        _path(COMPUTE_SKUS_PATH, resolve_target_config(target).subscription_id),
        operator=operator,
        api_version=COMPUTE_SKUS_API_VERSION,
        params={"$filter": f"location eq '{location}'"},
    )
    rows = (size_row(item, location) for item in paged.rows)
    paged.rows = [row for row in rows if row is not None]
    result = paged.as_dict()
    result["location"] = location
    return result
