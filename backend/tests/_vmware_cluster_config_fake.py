# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How vCenter 8.0.x answers a read of a cluster's ``configurationEx``, for test fakes.

vCenter 8.0.x rejects a ``RetrievePropertiesEx`` property path into
``configurationEx`` (for example ``configurationEx.rule``) with HTTP 500 and an
``InvalidProperty`` fault. It accepts the whole ``configurationEx`` and returns
a ``ClusterConfigInfoEx``; an empty ``rule`` or ``group`` array is left out of
that object.

The vim fakes call :func:`reject_config_ex_member_paths` before they answer a
read, so a handler that goes back to the dotted path fails in the unit tests.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = [
    "cluster_config_ex",
    "config_ex_member_path",
    "invalid_property_fault",
    "reject_config_ex_member_paths",
]

_CONFIG_EX_PREFIX = "configurationEx."
_RETRIEVE_URL = (
    "https://vcenter.example.test/sdk/vim25/8.0.3.0"
    "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
)


def cluster_config_ex(
    *,
    rules: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    drs_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``ClusterConfigInfoEx`` as vCenter returns it (empty arrays left out)."""
    value: dict[str, Any] = {
        "_typeName": "ClusterConfigInfoEx",
        "drsConfig": drs_config or {"_typeName": "ClusterDrsConfigInfo", "enabled": False},
    }
    if rules:
        value["rule"] = rules
    if groups:
        value["group"] = groups
    return value


def config_ex_member_path(body: Any) -> str | None:
    """The first ``configurationEx.<member>`` path a read body asks for, else ``None``."""
    for filter_spec in (body or {}).get("specSet") or []:
        for prop_spec in filter_spec.get("propSet") or []:
            for path in prop_spec.get("pathSet") or []:
                if path.startswith(_CONFIG_EX_PREFIX):
                    return str(path)
    return None


def invalid_property_fault(path: str) -> dict[str, str]:
    """The JSON fault body vCenter 8.0.x sends with the HTTP 500."""
    return {"_typeName": "InvalidProperty", "name": path}


def reject_config_ex_member_paths(body: Any) -> None:
    """Raise the HTTP 500 ``InvalidProperty`` error for a ``configurationEx.<member>`` path."""
    path = config_ex_member_path(body)
    if path is None:
        return
    request = httpx.Request("POST", _RETRIEVE_URL)
    response = httpx.Response(500, request=request, json=invalid_property_fault(path))
    raise httpx.HTTPStatusError(
        f"Server error '500 Internal Server Error' for url '{_RETRIEVE_URL}'",
        request=request,
        response=response,
    )
