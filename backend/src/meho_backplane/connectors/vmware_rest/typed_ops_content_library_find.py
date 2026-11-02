# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed reads that find content libraries and library items by name.

* ``vmware.content_library.find`` -- the ids of the libraries that match a
  name and/or a library type (``POST /content/library?action=find``).
* ``vmware.content_library.item.find`` -- the ids of the library items that
  match a name, a library, a type, a source id and/or the cached flag
  (``POST /content/library/item?action=find``).

Both are bound methods on :class:`VmwareRestConnector` in the
:mod:`~meho_backplane.connectors.vmware_rest.typed_ops` mould. They call the
vCenter directly on the connector session, so they work on a fresh install
with no spec ingest. The HTTP call itself is the composites' helper
``_find_content_library_ids``: one place sends the find, and the composites
that resolve a library or an item by name keep calling it directly. Those
composites are already checked as a whole, so their find step needs no
second policy check.

The request body is the ``FindSpec`` at the top level of the JSON body. The
older ``{"spec": {...}}`` wrapper gets ``400 INVALID_ARGUMENT`` on the
``/api`` surface. vCenter also answers ``400`` when the ``FindSpec`` is
empty, so the parameter schemas need at least one filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._content_library_schemas import (
    CONTENT_LIBRARY_GROUP_KEY,
    CONTENT_LIBRARY_WHEN_TO_USE,
    FIND_LIBRARY_CALL,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "CONTENT_LIBRARY_FIND_REST_CALLS",
    "CONTENT_LIBRARY_GROUP_KEY",
    "CONTENT_LIBRARY_WHEN_TO_USE",
    "TYPED_OPS",
    "VMWARE_CONTENT_LIBRARY_FIND_OP",
    "VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP",
    "WHEN_TO_USE_BY_GROUP",
    "content_library_find_impl",
    "content_library_item_find_impl",
]

#: The two vCenter REST calls (``Content.Library_find`` /
#: ``Content.Library.Item_find``), in the ``METHOD:/path`` form the ingest
#: parser emits. The library find is the shared :data:`FIND_LIBRARY_CALL`.
#: The spec reconcile lane checks both against the pinned ``vcenter.yaml``.
FIND_LIBRARY_ITEM_CALL: Final = "POST:/content/library/item?action=find"
CONTENT_LIBRARY_FIND_REST_CALLS: Final = (FIND_LIBRARY_CALL, FIND_LIBRARY_ITEM_CALL)


def _log() -> Any:
    """Get the logger per call, so ``structlog.testing.capture_logs`` sees every event."""
    return structlog.get_logger(__name__)


def _find_spec(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The ``FindSpec`` body: the filters the caller set, nothing else."""
    return {key: params[key] for key in keys if params.get(key) is not None}


async def _find(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    *,
    call: str,
    spec: dict[str, Any],
) -> list[str]:
    # Lazy import: the composites package imports the typed ops while it
    # loads, so a top-level import of ``_write`` here would be circular.
    from meho_backplane.connectors.vmware_rest.composites._write import (
        _find_content_library_ids,
    )

    return await _find_content_library_ids(connector, target, operator, op_id=call, spec=spec)


_LIBRARY_SPEC_KEYS = ("name", "type")
_ITEM_SPEC_KEYS = ("name", "library_id", "type", "source_id", "cached")


async def content_library_find_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.find``. Returns ``{"library_ids": [...]}``."""
    ids = await _find(
        connector,
        operator,
        target,
        call=FIND_LIBRARY_CALL,
        spec=_find_spec(params, _LIBRARY_SPEC_KEYS),
    )
    _log().info("vmware_content_library_find_read", target=target.name, match_count=len(ids))
    return {"library_ids": ids}


async def content_library_item_find_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.content_library.item.find``. Returns ``{"item_ids": [...]}``."""
    ids = await _find(
        connector,
        operator,
        target,
        call=FIND_LIBRARY_ITEM_CALL,
        spec=_find_spec(params, _ITEM_SPEC_KEYS),
    )
    _log().info("vmware_content_library_item_find_read", target=target.name, match_count=len(ids))
    return {"item_ids": ids}


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_TAGS: tuple[str, ...] = ("read-only", "vmware", "vcenter", "content-library")


def _ids_response_schema(key: str, description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            key: {
                "type": "array",
                "description": description,
                "items": {"type": "string"},
            }
        },
        "required": [key],
        "additionalProperties": False,
    }


VMWARE_CONTENT_LIBRARY_FIND_OP = VmwareTypedOp(
    op_id="vmware.content_library.find",
    handler_attr="content_library_find",
    summary="Find content libraries by name or type and return their ids.",
    description=(
        "Calls POST /content/library?action=find (Content.Library_find). Returns the "
        "ids of the libraries you can see that match every filter you set. The name "
        "match ignores upper and lower case. Set at least one filter. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "The library name. The match ignores upper and lower case.",
            },
            "type": {
                "type": "string",
                "enum": ["LOCAL", "SUBSCRIBED"],
                "description": "Only libraries of this type.",
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
        "examples": [{"name": "templates"}, {"type": "SUBSCRIBED"}],
    },
    response_schema=_ids_response_schema(
        "library_ids", "The ids of the matching libraries. Empty when nothing matches."
    ),
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to get the id of a content library when you know its name, or to "
            "list the ids of all LOCAL or all SUBSCRIBED libraries."
        ),
        "parameter_hints": {
            "name": "The library name, for example 'templates'.",
            "type": "LOCAL or SUBSCRIBED.",
        },
        "output_shape": (
            "{library_ids: [string]}. Empty when no library matches. More than one id "
            "means more than one library has that name."
        ),
        "next_step": (
            "Pass a library id as library_id to vmware.content_library.item.find to "
            "find an item in that library."
        ),
    },
)

VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP = VmwareTypedOp(
    op_id="vmware.content_library.item.find",
    handler_attr="content_library_item_find",
    summary="Find content library items by name, library or type and return their ids.",
    description=(
        "Calls POST /content/library/item?action=find (Content.Library.Item_find). "
        "Returns the ids of the library items you can see that match every filter "
        "you set. The name and type match ignores upper and lower case. Set at least "
        "one filter. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "The item name. The match ignores upper and lower case.",
            },
            "library_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Only items in this library. Get the id from "
                    "vmware.content_library.find. Omit it to search every library."
                ),
            },
            "type": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Only items of this type, for example 'ovf', 'iso' or 'vm-template'."
                ),
            },
            "source_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Only the item with this id at the publisher (for an item of a "
                    "subscribed library)."
                ),
            },
            "cached": {
                "type": "boolean",
                "description": "Only items whose content is (true) or is not (false) stored.",
            },
        },
        "minProperties": 1,
        "additionalProperties": False,
        "examples": [
            {"name": "ubuntu-24.04", "type": "ovf"},
            {"library_id": "<library-id>"},
        ],
    },
    response_schema=_ids_response_schema(
        "item_ids", "The ids of the matching library items. Empty when nothing matches."
    ),
    group_key=CONTENT_LIBRARY_GROUP_KEY,
    tags=_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to get the id of a content library item (an OVF template, an ISO, a "
            "VM template, a file) when you know its name, or to list the item ids of "
            "one library."
        ),
        "parameter_hints": {
            "name": "The item name, for example 'ubuntu-24.04'.",
            "library_id": "A library id from vmware.content_library.find.",
            "type": "For example 'ovf', 'iso' or 'vm-template'.",
        },
        "output_shape": (
            "{item_ids: [string]}. Empty when no item matches. More than one id means "
            "more than one item matches; add library_id or type to narrow it."
        ),
        "next_step": (
            "Pass an OVF item id as library_item to vmware.composite.vm.deploy_from_library."
        ),
    },
)

TYPED_OPS: tuple[VmwareTypedOp, ...] = (
    VMWARE_CONTENT_LIBRARY_FIND_OP,
    VMWARE_CONTENT_LIBRARY_ITEM_FIND_OP,
)
WHEN_TO_USE_BY_GROUP: dict[str, str] = {CONTENT_LIBRARY_GROUP_KEY: CONTENT_LIBRARY_WHEN_TO_USE}
