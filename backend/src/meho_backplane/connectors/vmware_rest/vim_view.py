# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read many vCenter objects in one go, through a VIM container view.

The VM list (``vmware.vm.list``), the inventory search
(``vmware.inventory.find``) and the snapshot list of all VMs
(``vmware.snapshot.list`` without ``vm``) all read "every object of some
types below one inventory object". The VIM API does that with a container
view, the same way govc and pyVmomi do:

1. ``ServiceInstance.RetrieveServiceContent`` gives the root folder, the view
   manager and the search index.
2. ``ViewManager.CreateContainerView`` makes a view of the objects of the
   given types below a container (a folder, a datacenter, a cluster, a
   resource pool or a host), recursively.
3. ``PropertyCollector.RetrievePropertiesEx`` reads the asked properties of
   every object in the view; :func:`~.vim_retrieve.retrieve_all_pages`
   follows ``ContinueRetrievePropertiesEx`` until the last page.
4. ``ContainerView.DestroyView`` drops the view again. A failed destroy is
   only logged: vCenter drops the view with the session anyway.

The module also reads properties of a known list of objects
(:func:`read_props`), for the names of hosts, clusters and folders.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_retrieve import retrieve_all_pages

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "OP_CONTINUE_RETRIEVE_PROPERTIES",
    "OP_CREATE_CONTAINER_VIEW",
    "OP_DESTROY_VIEW",
    "OP_RETRIEVE_PROPERTIES",
    "OP_RETRIEVE_SERVICE_CONTENT",
    "VIEW_READ_CALLS",
    "ServiceRefs",
    "VimObject",
    "moref_value",
    "objects_of",
    "read_props",
    "read_service_refs",
    "read_view",
    "view_body",
]

_log = structlog.get_logger(__name__)

#: The VIM calls of a view read, in the ``POST:/<Type>/{moId}/<Method>`` form the
#: vendor-file checks read.
OP_RETRIEVE_SERVICE_CONTENT: Final = "POST:/ServiceInstance/{moId}/RetrieveServiceContent"
OP_CREATE_CONTAINER_VIEW: Final = "POST:/ViewManager/{moId}/CreateContainerView"
OP_DESTROY_VIEW: Final = "POST:/ContainerView/{moId}/DestroyView"
OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
OP_CONTINUE_RETRIEVE_PROPERTIES: Final = (
    "POST:/PropertyCollector/{moId}/ContinueRetrievePropertiesEx"
)
VIEW_READ_CALLS: Final[tuple[str, ...]] = (
    OP_RETRIEVE_SERVICE_CONTENT,
    OP_CREATE_CONTAINER_VIEW,
    OP_RETRIEVE_PROPERTIES,
    OP_CONTINUE_RETRIEVE_PROPERTIES,
    OP_DESTROY_VIEW,
)

#: Objects per page vCenter may send. With the page limit of
#: :mod:`.vim_retrieve` this allows 100,000 objects in one read.
_MAX_OBJECTS_PER_PAGE: Final = 1000

#: Errors of a ``DestroyView`` that do not fail the read.
_DESTROY_ERRORS: Final = (
    httpx.HTTPError,
    RuntimeError,
    ConnectorResourceNotFoundError,
    ConnectorUpstreamError,
)


@dataclass(frozen=True)
class ServiceRefs:
    """The moids of the vCenter singletons a view read needs."""

    root_folder: str
    view_manager: str
    search_index: str


@dataclass(frozen=True)
class VimObject:
    """One object of a property read: its type, moid, properties and unread paths."""

    mo_type: str
    moid: str
    props: dict[str, Any]
    missing: frozenset[str]


def moref_value(ref: Any) -> str | None:
    """The moid of a VI-JSON ``ManagedObjectReference``, else ``None``."""
    if isinstance(ref, dict):
        value = ref.get("value")
        return value if isinstance(value, str) and value else None
    return None


async def read_service_refs(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> ServiceRefs:
    """Read the root folder, the view manager and the search index.

    A ServiceContent without a root folder is an error: nothing can be read
    then. The view manager and the search index fall back to their usual
    vCenter moids.
    """
    raw = await connector._post_vmomi_json(
        target,
        "/ServiceInstance/ServiceInstance/RetrieveServiceContent",
        operator=operator,
        json={},
    )
    content = unwrap_vim_value(raw)
    content = content if isinstance(content, dict) else {}
    root = moref_value(content.get("rootFolder"))
    if root is None:
        raise RuntimeError("vCenter's ServiceContent has no rootFolder")
    return ServiceRefs(
        root_folder=root,
        view_manager=moref_value(content.get("viewManager")) or "ViewManager",
        search_index=moref_value(content.get("searchIndex")) or "SearchIndex",
    )


def view_body(view: str, prop_type: str, path_set: Sequence[str]) -> dict[str, Any]:
    """``RetrievePropertiesEx`` body that reads *path_set* of every object in a view."""
    return {
        "specSet": [
            {
                VIM_TYPE_NAME_KEY: "PropertyFilterSpec",
                "propSet": [
                    {
                        VIM_TYPE_NAME_KEY: "PropertySpec",
                        "type": prop_type,
                        "pathSet": list(path_set),
                    }
                ],
                "objectSet": [
                    {
                        VIM_TYPE_NAME_KEY: "ObjectSpec",
                        "obj": vim_moref("ContainerView", view),
                        "skip": True,
                        "selectSet": [
                            {
                                VIM_TYPE_NAME_KEY: "TraversalSpec",
                                "name": "view",
                                "type": "ContainerView",
                                "path": "view",
                                "skip": False,
                            }
                        ],
                    }
                ],
            }
        ],
        "options": {VIM_TYPE_NAME_KEY: "RetrieveOptions", "maxObjects": _MAX_OBJECTS_PER_PAGE},
    }


def objects_of(result: Any) -> list[VimObject]:
    """The objects of a ``RetrieveResult``, with their properties unboxed."""
    payload = unwrap_vim_value(result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    out: list[VimObject] = []
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        moid = moref_value(ref)
        if moid is None:
            continue
        mo_type = ref.get("type") if isinstance(ref, dict) else None
        props = {
            entry["name"]: entry.get("val")
            for entry in obj.get("propSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        missing = frozenset(
            entry["path"]
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
        out.append(VimObject(str(mo_type or ""), moid, props, missing))
    return out


async def read_view(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    view_manager: str,
    container: tuple[str, str],
    types: Sequence[str],
    prop_type: str,
    path_set: Sequence[str],
) -> list[VimObject]:
    """Read *path_set* of every object of *types* below *container*, recursively.

    *container* is ``(type, moid)``. An empty *types* means every managed
    entity. *prop_type* is the type the property spec names (``ManagedEntity``
    reads the common properties of every type). A container that does not
    exist raises :class:`ConnectorResourceNotFoundError`, which the dispatcher
    turns into a ``not_found`` result. A failed page fails the read.
    """
    created = await connector._post_vmomi_json(
        target,
        f"/ViewManager/{view_manager}/CreateContainerView",
        operator=operator,
        json={"container": vim_moref(*container), "type": list(types), "recursive": True},
        promote_managed_object_not_found=True,
    )
    view = moref_value(unwrap_vim_value(created))
    if view is None:
        raise RuntimeError("CreateContainerView returned no view")
    try:
        result = await retrieve_all_pages(
            connector, target, operator, view_body(view, prop_type, path_set)
        )
        return objects_of(result)
    finally:
        try:
            await connector._post_vmomi_json(
                target, f"/ContainerView/{view}/DestroyView", operator=operator, json={}
            )
        except _DESTROY_ERRORS as exc:
            _log.info("vmware_container_view_not_destroyed", error=type(exc).__name__)


async def read_props(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    mo_type: str,
    moids: Sequence[str],
    path_set: Sequence[str],
) -> dict[str, VimObject]:
    """Read *path_set* of the listed objects of one type, every page: ``{moid: object}``."""
    if not moids:
        return {}
    result = await retrieve_all_pages(
        connector, target, operator, retrieve_properties_body(mo_type, list(moids), path_set)
    )
    return {obj.moid: obj for obj in objects_of(result)}
