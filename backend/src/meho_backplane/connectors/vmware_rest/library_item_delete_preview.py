# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Blast-radius preview for the ingested content-library item delete (#4058).

The VMware floor (``ingest_safety.py``) puts the ingested
``DELETE /content/library/item/{id}`` in the ``destructive`` risk level: the
item and its files cannot be brought back. A destructive op can only wait for
approval when its preview names what is destroyed. This builder reuses the
read-only planner of the governed ``vmware.composite.content_library.item.delete``,
so the approver sees the same item, library, files, and mounting VMs.

The plain delete does not apply the composite's refusals (an item in a
subscribed library, an item a VM still mounts). When the composite would
refuse, the preview says so as a ``warning`` and names the composite, so the
approver can reject the plain delete and use the composite instead.

The id must be a vCenter content-library id (a UUID). Any other id makes the
builder raise, so the delete is refused before it waits for approval.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._library_checks import id_problem
from meho_backplane.connectors.vmware_rest.composites._library_delete import (
    plan_content_library_item_delete,
)
from meho_backplane.connectors.vmware_rest.composites._teardown import blast_radius
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "LIBRARY_ITEM_DELETE_OP_IDS",
    "library_item_delete_preview",
    "register_library_item_delete_previews",
]

_PARAM_NAMES: Final = ("libraryItemId", "library_item_id")
_MOUNT_PREFIXES: Final = ("", "/api")
_COMPOSITE_OP_ID: Final = "vmware.composite.content_library.item.delete"

#: Every op_id spelling of the ingested item delete: the vendor spec spells
#: the path parameter ``{libraryItemId}``, an appliance-served spec can spell
#: it ``{library_item_id}``, with or without the ``/api`` mount.
LIBRARY_ITEM_DELETE_OP_IDS: Final[frozenset[str]] = frozenset(
    f"DELETE:{prefix}/content/library/item/{{{name}}}"
    for prefix, name in product(_MOUNT_PREFIXES, _PARAM_NAMES)
)


async def library_item_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Blast radius for ``DELETE /content/library/item/{id}``: the item and its files."""
    if ctx.connector_instance is None:
        return None
    item_id = next((ctx.params[name] for name in _PARAM_NAMES if name in ctx.params), None)
    problem = id_problem(item_id, "the library item id")
    if problem is not None:
        raise ValueError(problem)
    plan = await plan_content_library_item_delete(
        ctx.connector_instance,  # type: ignore[arg-type]
        ctx.target,
        ctx.operator,
        {"item_id": item_id},
        enumerate_files=True,
    )
    effect = blast_radius(plan)
    refusal = effect["blast_radius"].pop("refusal", None)
    if refusal is not None:
        effect["blast_radius"]["warning"] = (
            f"{refusal['guidance']}. This plain delete does not check this; "
            f"{_COMPOSITE_OP_ID} would refuse it."
        )
    return effect


def register_library_item_delete_previews() -> None:
    """Register the builder under every op_id spelling. Import-time."""
    for op_id in LIBRARY_ITEM_DELETE_OP_IDS:
        register_preview_builder(op_id, library_item_delete_preview)


register_library_item_delete_previews()
