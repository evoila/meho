# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Canonical op table for the governed content-library ISO path (#3086).

Single source of truth for the raw ingested vcenter ops the governed
ISO recipe (``docs/codebase/vmware-rest-governed-iso-path.md``) names:
the content-library **import-from-URL** flow (create item → create
update session → add a ``PULL`` file sourced from an HTTP endpoint →
complete → poll) and the **iso.image mount/unmount** pair. Two test
lanes consume this table so the recipe's op list can never fork:

* ``test_connectors_vmware_rest_governed_iso_dispatch.py`` — always-on
  respx full-dispatch tests seeding these descriptors and asserting the
  literal wire request (flat ``/api`` body, no ``/rest``-style
  ``{"spec": ...}`` envelope — the #2973/#3071 bug class) and the
  result envelope (scalar → ``{"value": ...}``, 204 → ``{}``).
* ``test_connectors_vmware_rest_governed_iso_reconcile.py`` —
  shelf-gated reconcile lane grounding every op_id and request-body
  shape against the pinned ``vcenter-9.0/vcenter.yaml`` (the #2980
  harness contract).

``parameter_schema`` mirrors the shape the G0.7 ingest parser emits for
each op (a single ``body`` container param tagged
``x-meho-param-loc: "body"``, path vars tagged ``"path"``, query params
tagged ``"query"``) — deliberately *minimal* (only the fields the
recipe exercises) so the unit lane stays spec-shelf-free; the reconcile
lane is what grounds the full shapes against the pinned spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["RECIPE_OPS", "RecipeOp"]


@dataclass(frozen=True)
class RecipeOp:
    """One raw ingested op the governed ISO recipe names."""

    op_id: str
    stage: str
    safety_level: str
    #: Seed shape for the unit-lane descriptor row (parser-emitted form).
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    #: Reconcile contract: body property names the pinned spec must serve
    #: at the TOP level of the requestBody (flat ``/api`` shape). ``None``
    #: for bodyless ops; exact-match when ``body_props_exact`` is True,
    #: subset otherwise (the *Model bodies carry server-populated fields
    #: the recipe never sends).
    body_props: frozenset[str] | None = None
    body_props_exact: bool = False
    #: Reconcile contract: body properties the pinned spec marks required.
    body_required: frozenset[str] | None = None


def _schema(
    *,
    body: dict[str, Any] | None = None,
    body_required: list[str] | None = None,
    path_vars: list[str] | None = None,
    query: list[str] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build a parser-shaped parameter_schema for a descriptor seed."""
    props: dict[str, Any] = {}
    if body is not None:
        body_schema: dict[str, Any] = {
            "type": "object",
            "x-meho-param-loc": "body",
            "properties": body,
        }
        if body_required:
            body_schema["required"] = body_required
        props["body"] = body_schema
    for name in path_vars or []:
        props[name] = {"type": "string", "x-meho-param-loc": "path"}
    for name in query or []:
        props[name] = {"type": "string", "x-meho-param-loc": "query"}
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}

#: The recipe's ops, keyed by op_id, in recipe order. op_id strings are
#: byte-for-byte the ``METHOD:/path`` values the ingest parser emits from
#: the pinned ``vcenter.yaml`` (action-discriminated endpoints keep the
#: ``?action=<verb>`` suffix in the path key itself; the required-query
#: marker form ``?library_id`` likewise rides the path key).
RECIPE_OPS: dict[str, RecipeOp] = {
    op.op_id: op
    for op in (
        RecipeOp(
            op_id="GET:/content/library",
            stage="discover: list library ids",
            safety_level="safe",
            parameter_schema=_schema(),
        ),
        RecipeOp(
            op_id="GET:/content/library/item?library_id",
            stage="discover: list item ids in a library",
            safety_level="safe",
            parameter_schema=_schema(query=["library_id"], required=["library_id"]),
        ),
        RecipeOp(
            op_id="POST:/content/library/item",
            stage="import 1: create the library item",
            safety_level="caution",
            parameter_schema=_schema(
                body={"library_id": _STR, "name": _STR, "type": _STR, "description": _STR},
                required=["body"],
            ),
            body_props=frozenset({"library_id", "name", "type", "description"}),
            body_required=frozenset(),
        ),
        RecipeOp(
            op_id="POST:/content/library/item/update-session",
            stage="import 2: create the update session",
            safety_level="caution",
            parameter_schema=_schema(
                body={"library_item_id": _STR},
                required=["body"],
            ),
            body_props=frozenset({"library_item_id"}),
            body_required=frozenset(),
        ),
        RecipeOp(
            op_id="POST:/content/library/item/update-session/{updateSessionId}/file",
            stage="import 3: add the PULL file (HTTP source)",
            safety_level="caution",
            parameter_schema=_schema(
                body={
                    "name": _STR,
                    "source_type": _STR,
                    "source_endpoint": {
                        "type": "object",
                        "properties": {"uri": _STR},
                    },
                },
                body_required=["name", "source_type"],
                path_vars=["updateSessionId"],
                required=["updateSessionId", "body"],
            ),
            body_props=frozenset({"name", "source_type", "source_endpoint"}),
            body_required=frozenset({"name", "source_type"}),
        ),
        RecipeOp(
            op_id="POST:/content/library/item/update-session/{updateSessionId}?action=complete",
            stage="import 4: complete the session",
            safety_level="caution",
            parameter_schema=_schema(path_vars=["updateSessionId"], required=["updateSessionId"]),
        ),
        RecipeOp(
            op_id="GET:/content/library/item/update-session/{updateSessionId}",
            stage="import 5: poll session state to DONE / ERROR",
            safety_level="safe",
            parameter_schema=_schema(path_vars=["updateSessionId"], required=["updateSessionId"]),
        ),
        RecipeOp(
            op_id="POST:/content/library/item/update-session/{updateSessionId}?action=cancel",
            stage="failure path: cancel the session",
            safety_level="caution",
            parameter_schema=_schema(path_vars=["updateSessionId"], required=["updateSessionId"]),
        ),
        RecipeOp(
            op_id="POST:/content/library/item/update-session/{updateSessionId}?action=keep-alive",
            stage="long pull: keep the session alive",
            safety_level="caution",
            parameter_schema=_schema(
                body={"client_progress": {"type": "integer"}},
                path_vars=["updateSessionId"],
                required=["updateSessionId"],
            ),
            body_props=frozenset({"client_progress"}),
            body_required=frozenset(),
        ),
        RecipeOp(
            op_id="POST:/vcenter/iso/image?action=mount",
            stage="mount: attach the ISO item to a VM CD-ROM",
            safety_level="caution",
            parameter_schema=_schema(
                body={"library_item": _STR, "vm": _STR},
                body_required=["library_item", "vm"],
                required=["body"],
            ),
            body_props=frozenset({"library_item", "vm"}),
            body_props_exact=True,
            body_required=frozenset({"library_item", "vm"}),
        ),
        RecipeOp(
            op_id="POST:/vcenter/iso/image?action=unmount",
            stage="unmount: detach the ISO-backed CD-ROM from a VM",
            safety_level="caution",
            parameter_schema=_schema(
                body={"vm": _STR, "cdrom": _STR},
                body_required=["vm", "cdrom"],
                required=["body"],
            ),
            body_props=frozenset({"vm", "cdrom"}),
            body_props_exact=True,
            body_required=frozenset({"vm", "cdrom"}),
        ),
    )
}
