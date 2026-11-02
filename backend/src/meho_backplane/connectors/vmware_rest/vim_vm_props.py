# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read some VIM properties of one VM (``PropertyCollector.RetrievePropertiesEx``).

The boot read and the advanced-settings read and change need VM properties
that the vCenter REST API does not have (``config.bootOptions``,
``config.extraConfig``). This helper sends one un-gated
``RetrievePropertiesEx`` on the VI-JSON path, the same read
``vmware.snapshot.list`` uses, and returns the properties with their VI-JSON
boxes removed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.typed_ops_snapshot_list import _vm_object
from meho_backplane.connectors.vmware_rest.vim_body import retrieve_properties_body

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = ["RETRIEVE_PROPERTIES_CALL", "read_vm_vim_props"]

#: The VI-JSON method of the read, as the ``METHOD:/path`` key of ``vi-json.yaml``.
RETRIEVE_PROPERTIES_CALL: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_VIRTUAL_MACHINE_MO_TYPE: Final = "VirtualMachine"


async def read_vm_vim_props(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    props: Sequence[str],
    *,
    op_id: str,
) -> tuple[dict[str, Any], set[str]]:
    """Return ``(properties, unreadable property paths)`` of VM *vm*.

    Raises :exc:`ConnectorResourceNotFoundError` when the VM does not exist,
    so the dispatcher answers ``not_found``. *op_id* names the operation in
    that message.
    """
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(_VIRTUAL_MACHINE_MO_TYPE, [vm], props),
        promote_managed_object_not_found=True,
    )
    found = _vm_object(result, vm)
    if found is None:
        raise ConnectorResourceNotFoundError([vm], f"{op_id}: no VM {vm!r}")
    return found
