# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Content-library **subscribed**-library composites (#3495).

The four ``vmware.composite.content_library.subscribed.*`` composites that
let an operator create and drive a **SUBSCRIBED** content library through the
backplane -- the governed source for a vSphere Supervisor's Tanzu Kubernetes
release (TKr / VKr) images.

Why this exists: on Supervisor enable, ``wcpsvc`` auto-creates a SUBSCRIBED
"Kubernetes Service Content Library" pointed at the fleet offline-depot
content-gateway, which cannot serve the ``VKR`` component on a VCF-Installer
fleet, so activation hangs ``CONFIGURING``. The governed path is to
**pre-create** a SUBSCRIBED library subscribed straight to the upstream VMware
TKr repo (``https://wp-content.vmware.com/v2/latest/lib.json``) and assign it
as the Supervisor's TKr library via the enable spec's
``default_kubernetes_service_content_library`` field (#3281). This module is
that pre-create + sync + status/items read surface.

Delivery shape -- **thin typed composites**, not the generic-ingested row.
The subscribe body carries a ``subscription_info.password`` (BASIC auth) plus
an ``ssl_thumbprint``, so the op needs credential hygiene (broadcast
aggregate-only via the ``_CREDENTIAL_WRITE_OPS`` pin, a park-time preview that
never echoes the secret, ``params_hash``-only audit) that a raw ingested row
cannot carry; and the connector's governed ops must dispatch on a fresh boot
with **zero catalog ingest** (a generic row requires a runtime spec-ingest +
``edit_op`` enable), which composites satisfy and ingested rows do not. The
issue permits a typed composite "if the subscribe body ... needs it" -- it
does. The naming nests under the sibling #3331 LOCAL-library family
(``content_library.create``) via the ``subscribed`` infix, so the two do not
collide.

Each handler rides the **same** direct-session seam the #2909 /
``vm.deploy_from_library`` content-library composite established: the resolve
reads go through :func:`._write._read_sub_op` /
:func:`._write._find_content_library_ids` (un-gated), and the two writes
(create / sync) through :func:`._write._write_sub_op`, which re-applies the
:func:`~meho_backplane.operations.composite.enforce_subop_policy` gate per
governed write -- so the direct path keeps property 3 of #508's four
``dispatch_child`` guarantees. The create / sync composites are registered
``caution`` + ``requires_approval=True`` (the issue's "write" tier: a write
that always needs an approval decision, but not the ``dangerous`` /
destructive intrinsic-risk of a VM/host mutation); the status / items reads
are ``safe`` + ``requires_approval=False``.

Sub-op manifests (``_SUB_OPS_*``) are the canonical path lists the
spec-reconcile lane (``test_connectors_vmware_rest_library_reconcile.py``)
pins against the pinned ``vcenter.yaml`` so no fictional path drifts in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _OP_LIST_DATASTORES,
    _find_content_library_ids,
    _read_sub_op,
    _unwrap_value,
    _write_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

# ---------------------------------------------------------------------------
# Sub-op path manifests (reconcile-lane pinning; #3495).
#
# Every op_id is a well-formed ``METHOD:/path`` the pinned vcenter.yaml
# ingest emits -- the reconcile lane sweeps these and fails on any path the
# real spec does not serve (the "no fictional-path drift" AC). The two write
# composites' manifests are also the grant-discovery source fed into
# ``_GOVERNED_SUBOP_MANIFEST`` (#3349); the read composites need no grant.
# ---------------------------------------------------------------------------

#: ``content_library.subscribed.create`` -- resolve the datastore backing by
#: name, then POST the subscribed-library create.
_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_CREATE: Final[tuple[str, ...]] = (
    _OP_LIST_DATASTORES,
    "POST:/content/subscribed-library",
)

#: ``content_library.subscribed.sync`` -- resolve a ``library_name`` to its
#: id (un-gated find), then force the sync.
_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_SYNC: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    "POST:/content/subscribed-library/{libraryId}?action=sync",
)

#: ``content_library.subscribed.status`` -- resolve a ``library_name``, then
#: read the LibraryModel back.
_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_STATUS: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    "GET:/content/subscribed-library/{libraryId}",
)

#: ``content_library.subscribed.items.list`` -- resolve a ``library_name``,
#: find every item id in the library, then read each item's metadata.
_SUB_OPS_CONTENT_LIBRARY_SUBSCRIBED_ITEMS_LIST: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    "GET:/content/library/item/{libraryItemId}",
)

_OP_CREATE_SUBSCRIBED_LIBRARY = "POST:/content/subscribed-library"
_OP_SYNC_SUBSCRIBED_LIBRARY = "POST:/content/subscribed-library/{libraryId}?action=sync"
_OP_GET_SUBSCRIBED_LIBRARY = "GET:/content/subscribed-library/{libraryId}"
_OP_GET_LIBRARY_ITEM = "GET:/content/library/item/{libraryItemId}"

#: Cap on the item rows surfaced inline before the JSONFlux reducer spills to
#: a handle. The reducer's own 50-row / 4 KB threshold is the real gate; this
#: is a defensive ceiling so a pathologically large library never materialises
#: an unbounded intermediate list in the handler.
_ITEMS_HARD_CAP: Final[int] = 500


def _issue(category: str, severity: str, message: str) -> dict[str, Any]:
    """Build one structured issue entry for a failure envelope."""
    return {"category": category, "severity": severity, "message": message}


async def _resolve_datastore_id(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    name: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a datastore display name to its moid via ``GET:/vcenter/datastore``.

    Returns ``(datastore_id, None)`` on a unique match, or ``(None, envelope)``
    with a terminal ``datastore_not_found`` / ``ambiguous_datastore`` response
    dict. The listing forwards ``filter.names`` (exact match); ambiguity is
    refused so the operator re-dispatches by an unambiguous name.
    """
    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_DATASTORES, {"filter.names": [name]}
    )
    entries = _unwrap_value(listing)
    ids = [
        entry["datastore"]
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("datastore"), str)
    ]
    if not ids:
        return None, {
            "status": "datastore_not_found",
            "library_id": None,
            "issues": [_issue("input", "error", f"datastore {name!r} matched no datastore")],
        }
    if len(ids) > 1:
        return None, {
            "status": "ambiguous_datastore",
            "library_id": None,
            "candidates": ids,
            "issues": [
                _issue("input", "error", f"datastore {name!r} matched {len(ids)} datastores")
            ],
        }
    return ids[0], None


async def _resolve_library_id(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the target library id from ``library_id`` or ``library_name``.

    ``library_id`` (an id) short-circuits; otherwise ``library_name`` is
    resolved via ``Content.Library_find`` (``POST:/content/library?action=find``).
    Returns ``(library_id, None)`` on success or ``(None, envelope)`` with a
    terminal ``invalid_reference`` / ``library_not_found`` /
    ``ambiguous_library`` response dict. Shared by the sync / status / items
    composites.
    """
    passthrough = params.get("library_id")
    if isinstance(passthrough, str) and passthrough:
        return passthrough, None

    library_name = params.get("library_name")
    if not isinstance(library_name, str) or not library_name:
        return None, {
            "status": "invalid_reference",
            "library_id": None,
            "issues": [
                _issue("input", "error", "supply library_id (id) or library_name to identify")
            ],
        }

    library_ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY, spec={"name": library_name}
    )
    if not library_ids:
        return None, {
            "status": "library_not_found",
            "library_id": None,
            "issues": [_issue("input", "error", f"library {library_name!r} matched no library")],
        }
    if len(library_ids) > 1:
        return None, {
            "status": "ambiguous_library",
            "library_id": None,
            "candidates": library_ids,
            "issues": [
                _issue(
                    "input",
                    "error",
                    f"library {library_name!r} matched {len(library_ids)} libraries",
                )
            ],
        }
    return library_ids[0], None


def _build_subscription_info(params: dict[str, Any]) -> dict[str, Any]:
    """Assemble the ``Content.Library.SubscriptionInfo`` body from *params*.

    ``authentication_method`` / ``automatic_sync_enabled`` / ``on_demand`` /
    ``subscription_url`` are the four the create requires; ``user_name`` /
    ``password`` are sent only for ``BASIC`` auth, and ``ssl_thumbprint`` only
    when supplied. The password rides the request body (and the durable
    ``ApprovalRequest.params``) but never a preview / broadcast / audit-hash
    surface -- see the module docstring.
    """
    auth_method = params.get("authentication_method") or "NONE"
    info: dict[str, Any] = {
        "authentication_method": auth_method,
        "automatic_sync_enabled": bool(params.get("automatic_sync_enabled", False)),
        "on_demand": bool(params.get("on_demand", True)),
        "subscription_url": params["subscription_url"],
    }
    if auth_method == "BASIC":
        username = params.get("username")
        if isinstance(username, str):
            info["user_name"] = username
        password = params.get("password")
        if isinstance(password, str) and password:
            info["password"] = password
    ssl_thumbprint = params.get("ssl_thumbprint")
    if isinstance(ssl_thumbprint, str) and ssl_thumbprint:
        info["ssl_thumbprint"] = ssl_thumbprint
    return info


async def content_library_subscribed_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a SUBSCRIBED content library subscribed to a remote publisher.

    Op-id: ``vmware.composite.content_library.subscribed.create``.

    Sub-ops (direct session):

    1. ``GET:/vcenter/datastore`` -- resolve the ``datastore`` name to a moid
       for the mandatory ``storage_backings`` entry (load-bearing: a
       no/ambiguous match returns ``datastore_not_found`` /
       ``ambiguous_datastore`` before any write).
    2. ``POST:/content/subscribed-library`` -- the governed create (through
       :func:`._write._write_sub_op`), body = ``Content.LibraryModel`` with a
       ``DATASTORE`` storage backing and the assembled
       ``Content.Library.SubscriptionInfo``. Returns the new library id (a
       bare string on the modern ``/api`` surface).

    The create is asynchronous on the vCenter side: it returns immediately and
    the Content Library Service synchronises to the remote source in the
    background (metadata only when ``on_demand`` is true). Poll readiness with
    ``content_library.subscribed.status`` (``last_sync_time``) /
    ``content_library.subscribed.items.list``.

    Returns
    -------
    dict[str, Any]
        ``{"status": "created", "library_id": <id>, "name": ...,
        "subscription_url": ..., "datastore_id": ..., "on_demand": ...,
        "automatic_sync_enabled": ...}`` on success; a terminal
        ``datastore_not_found`` / ``ambiguous_datastore`` / ``create_error``
        envelope (``library_id=None``) otherwise. May instead return an
        ``awaiting_approval`` / ``denied`` :class:`OperationResult` from the
        write governance seam.
    """
    datastore_name = params["datastore"]
    datastore_id, ds_err = await _resolve_datastore_id(connector, target, operator, datastore_name)
    if ds_err is not None:
        return ds_err

    body: dict[str, Any] = {
        "name": params["name"],
        "storage_backings": [{"type": "DATASTORE", "datastore_id": datastore_id}],
        "subscription_info": _build_subscription_info(params),
    }
    description = params.get("description")
    if isinstance(description, str) and description:
        body["description"] = description

    try:
        gate, payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_SUBSCRIBED_LIBRARY, body
        )
    except httpx.HTTPError as exc:
        return {
            "status": "create_error",
            "library_id": None,
            "issues": [_issue("connector", "error", f"subscribed-library create failed: {exc}")],
        }
    if gate is not None:
        return gate

    library_id = _unwrap_value(payload)
    if not isinstance(library_id, str) or not library_id:
        return {
            "status": "create_error",
            "library_id": None,
            "issues": [
                _issue(
                    "connector",
                    "error",
                    f"create returned no library id (got {type(library_id).__name__})",
                )
            ],
        }
    subscription_info = body["subscription_info"]
    return {
        "status": "created",
        "library_id": library_id,
        "name": params["name"],
        "subscription_url": subscription_info["subscription_url"],
        "datastore_id": datastore_id,
        "on_demand": subscription_info["on_demand"],
        "automatic_sync_enabled": subscription_info["automatic_sync_enabled"],
    }


async def content_library_subscribed_sync_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Force synchronisation of a SUBSCRIBED content library.

    Op-id: ``vmware.composite.content_library.subscribed.sync``.

    Sub-ops (direct session):

    1. ``POST:/content/library?action=find`` -- resolve a ``library_name`` to
       its id (skipped when ``library_id`` is supplied; un-gated read).
    2. ``POST:/content/subscribed-library/{libraryId}?action=sync`` -- the
       governed sync trigger (through :func:`._write._write_sub_op`).

    The sync respects the library's ``on_demand`` setting and is asynchronous:
    it returns immediately (HTTP 204) and does not wait for the content to
    land. Calling it on a library already synchronising is a no-op vCenter-side.
    Confirm completion with ``content_library.subscribed.status``
    (``last_sync_time``) / ``content_library.subscribed.items.list``.

    Returns
    -------
    dict[str, Any]
        ``{"status": "sync_triggered", "library_id": <id>}`` on success; a
        terminal ``invalid_reference`` / ``library_not_found`` /
        ``ambiguous_library`` / ``sync_error`` envelope otherwise. May instead
        return an ``awaiting_approval`` / ``denied`` :class:`OperationResult`.
    """
    library_id, resolve_err = await _resolve_library_id(connector, target, operator, params)
    if resolve_err is not None:
        return resolve_err

    try:
        gate, _ = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_SYNC_SUBSCRIBED_LIBRARY,
            {"libraryId": library_id},
        )
    except httpx.HTTPError as exc:
        return {
            "status": "sync_error",
            "library_id": library_id,
            "issues": [_issue("connector", "error", f"subscribed-library sync failed: {exc}")],
        }
    if gate is not None:
        return gate

    return {"status": "sync_triggered", "library_id": library_id}


async def content_library_subscribed_status_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read a SUBSCRIBED content library's model + subscription state.

    Op-id: ``vmware.composite.content_library.subscribed.status``.

    Sub-ops (direct session, un-gated reads):

    1. ``POST:/content/library?action=find`` -- resolve a ``library_name``
       (skipped when ``library_id`` is supplied).
    2. ``GET:/content/subscribed-library/{libraryId}`` -- read the
       ``Content.LibraryModel`` back.

    The readiness signal for the enable flow: ``last_sync_time`` is populated
    once the first sync completes. The API contract omits the subscription
    ``password`` from the GET response, so this read carries no secret.

    Returns
    -------
    dict[str, Any]
        ``{"status": "ok", "library_id": ..., "name": ..., "type": ...,
        "subscription_url": ..., "automatic_sync_enabled": ...,
        "on_demand": ..., "last_sync_time": ..., "description": ...,
        "storage_backings": [...]}`` on success; a terminal
        ``invalid_reference`` / ``library_not_found`` / ``ambiguous_library``
        envelope otherwise.
    """
    library_id, resolve_err = await _resolve_library_id(connector, target, operator, params)
    if resolve_err is not None:
        return resolve_err

    detail = await _read_sub_op(
        connector, target, operator, _OP_GET_SUBSCRIBED_LIBRARY, {"libraryId": library_id}
    )
    model = _unwrap_value(detail)
    if not isinstance(model, dict):
        return {
            "status": "read_error",
            "library_id": library_id,
            "issues": [
                _issue(
                    "connector",
                    "error",
                    f"library read returned {type(model).__name__}, expected object",
                )
            ],
        }
    subscription_info = model.get("subscription_info")
    subscription_info = subscription_info if isinstance(subscription_info, dict) else {}
    return {
        "status": "ok",
        "library_id": library_id,
        "name": model.get("name"),
        "type": model.get("type"),
        "subscription_url": subscription_info.get("subscription_url"),
        "authentication_method": subscription_info.get("authentication_method"),
        "automatic_sync_enabled": subscription_info.get("automatic_sync_enabled"),
        "on_demand": subscription_info.get("on_demand"),
        "last_sync_time": model.get("last_sync_time"),
        "description": model.get("description"),
        "storage_backings": model.get("storage_backings"),
    }


async def content_library_subscribed_items_list_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """List the items (synchronised TKr images) in a SUBSCRIBED library.

    Op-id: ``vmware.composite.content_library.subscribed.items.list``.

    Sub-ops (direct session, un-gated reads):

    1. ``POST:/content/library?action=find`` -- resolve a ``library_name``
       (skipped when ``library_id`` is supplied).
    2. ``POST:/content/library/item?action=find`` -- every item id scoped to
       the library.
    3. ``GET:/content/library/item/{libraryItemId}`` -- per-item metadata.

    The set-shaped ``items`` list is JSONFlux-reduced automatically by the
    dispatcher once it crosses the 50-row / 4 KB threshold (a handle the
    caller drills into with ``result_query``); each row carries the TKr-triage
    fields (``name`` = the TKr version, ``cached`` = whether the image content
    is downloaded).

    Returns
    -------
    dict[str, Any]
        ``{"library_id": ..., "item_count": N, "items": [{"id", "name",
        "type", "version", "cached", "size", "last_sync_time"}, ...]}`` on
        success; a terminal ``invalid_reference`` / ``library_not_found`` /
        ``ambiguous_library`` envelope otherwise.
    """
    library_id, resolve_err = await _resolve_library_id(connector, target, operator, params)
    if resolve_err is not None:
        return resolve_err

    item_ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY_ITEM, spec={"library_id": library_id}
    )
    items: list[dict[str, Any]] = []
    for item_id in item_ids[:_ITEMS_HARD_CAP]:
        detail = await _read_sub_op(
            connector, target, operator, _OP_GET_LIBRARY_ITEM, {"libraryItemId": item_id}
        )
        model = _unwrap_value(detail)
        if not isinstance(model, dict):
            continue
        items.append(
            {
                "id": model.get("id", item_id),
                "name": model.get("name"),
                "type": model.get("type"),
                "version": model.get("version"),
                "cached": model.get("cached"),
                "size": model.get("size"),
                "last_sync_time": model.get("last_sync_time"),
            }
        )
    return {"library_id": library_id, "item_count": len(items), "items": items}
