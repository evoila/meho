# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``<ns>.integration.delete``: remove an integration of an Automation organization.

MEHO first lists what uses the integration: the external IP ranges an IPAM
integration brought in, the network profiles whose networks carry those
ranges, and the content sources that read through it. When something uses it,
MEHO stops and removes nothing, unless the call sets ``confirm_in_use``. Then
it sends ``DELETE /iaas/api/integrations/{id}`` and reads the vendor's request
tracker until it ends.

The remove is ``destructive``: this module registers its preview builder for
both ids (``vcfa.tenant.integration.delete`` and
``vcfa.vra8.integration.delete``). The preview reads the integration and its
users and fills ``blast_radius``; it refuses an id that does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    DEFAULT_WAIT_SECONDS,
    EXTERNAL_IP_RANGES_PATH,
    IAAS_API_VERSION,
    INTEGRATION_PATH,
    LIST_TOP,
    NETWORK_PROFILES_PATH,
    Calls,
    Steps,
    content_rows,
    follow_tracker,
    path_with,
    vendor_text,
)
from meho_backplane.connectors.vmware_shared.vcfa_integrations import (
    git_source_rows,
    integration_row,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "DELETE_OP_IDS",
    "integration_delete",
    "integration_delete_preview",
    "integration_users",
]

#: The delete op on each connector; both get the preview builder.
DELETE_OP_IDS: Final[tuple[str, ...]] = (
    "vcfa.tenant.integration.delete",
    "vcfa.vra8.integration.delete",
)


def _link_ids(row: Mapping[str, Any], name: str) -> set[str]:
    links = row.get("_links")
    link = links.get(name) if isinstance(links, Mapping) else None
    hrefs: list[Any] = []
    if isinstance(link, Mapping):
        hrefs = list(link.get("hrefs") or []) + ([link["href"]] if link.get("href") else [])
    return {str(href).rstrip("/").rsplit("/", 1)[-1] for href in hrefs if href}


async def integration_users(calls: Calls, integration_id: str) -> list[dict[str, Any]]:
    """What uses the integration: its external IP ranges, their network profiles, sources."""
    users: list[dict[str, Any]] = []
    ranges = [
        row
        for row in content_rows(await calls.iaas("GET", EXTERNAL_IP_RANGES_PATH))
        if row.get("integrationId") == integration_id
    ]
    networks: set[str] = set()
    for row in ranges:
        users.append({"kind": "ip_range", "id": row.get("id"), "name": row.get("name")})
        networks |= _link_ids(row, "fabric-networks")
    if networks:
        profiles = content_rows(
            await calls.iaas("GET", NETWORK_PROFILES_PATH, **{"$top": LIST_TOP})
        )
        users.extend(
            {"kind": "network_profile", "id": row.get("id"), "name": row.get("name")}
            for row in profiles
            if _link_ids(row, "fabric-networks") & networks
        )
    users.extend(
        {"kind": "content_source", "id": row["id"], "name": row["name"]}
        for row in await git_source_rows(calls, integration_id=integration_id)
    )
    return users


async def _read_integration(calls: Calls, integration_id: Any) -> dict[str, Any] | None:
    try:
        row = await calls.iaas("GET", path_with(INTEGRATION_PATH, integration_id, "id"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return row if isinstance(row, dict) else {}


async def integration_delete(calls: Calls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<ns>.integration.delete``: list the users, stop or remove, follow the tracker."""
    integration_id = str(params.get("id") or "")
    path = path_with(INTEGRATION_PATH, integration_id, "id")
    steps = Steps("delete")
    row = await _read_integration(calls, integration_id)
    if row is None:
        return {
            "status": "unchanged",
            "integration": None,
            "users": [],
            "steps": steps.rows(),
            "guidance": "No integration has this id. Nothing was removed.",
        }
    users = await integration_users(calls, integration_id)
    base = {"integration": integration_row(row), "users": users}
    if users and not params.get("confirm_in_use"):
        return {
            "status": "in_use",
            **base,
            "steps": steps.rows(),
            "guidance": (
                "Other objects use this integration (see users). Nothing was removed. Ask the "
                "person; to remove it anyway, call again with confirm_in_use true."
            ),
        }
    try:
        resp = await calls.send("DELETE", path, params={"apiVersion": IAAS_API_VERSION})
    except httpx.HTTPStatusError as exc:
        steps.failed("delete", vendor_text(exc))
        return {
            "status": "failed",
            **base,
            "steps": steps.rows(),
            "guidance": "The vendor refused.",
        }
    tracker = None
    if resp.status_code == httpx.codes.NO_CONTENT or not resp.content:
        steps.done("delete")
    else:
        wait = float(params.get("wait_seconds") or DEFAULT_WAIT_SECONDS)
        tracker = await follow_tracker(calls, steps, "delete", resp.json(), wait, ())
    result = steps.rows()[0]["result"]
    status = {"done": "removed", "failed": "failed"}.get(result, "unfinished")
    return {
        "status": status,
        **base,
        "tracker": tracker,
        "steps": steps.rows(),
        "guidance": {
            "removed": "The integration is removed.",
            "failed": "The vendor's remove failed; see the step error.",
        }.get(
            status,
            f"The remove did not end in time. Read it with {calls.op('integration.list')} "
            "request_tracker_id.",
        ),
    }


async def integration_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The ``blast_radius`` of an integration remove: the integration and its users.

    Returns ``None`` without a connector (the ``preview_operation`` call does
    not reach the vendor). Raises :exc:`LookupError` when the integration does
    not exist: MEHO then refuses to queue the remove.
    """
    connector: Any = ctx.connector_instance
    integration_id = ctx.params.get("id")
    if connector is None or ctx.target is None or not isinstance(integration_id, str):
        return None
    ns = ctx.descriptor.op_id.rsplit(".integration.delete", 1)[0]
    calls = Calls(connector=connector, operator=ctx.operator, target=ctx.target, ns=ns)
    row = await _read_integration(calls, integration_id)
    if row is None:
        raise LookupError(f"no integration has the id {integration_id!r}")
    users = await integration_users(calls, integration_id)
    return {
        "blast_radius": {
            "object": {
                "kind": "integration",
                "id": integration_id,
                "name": row.get("name"),
                "integration_type": row.get("integrationType"),
            },
            "children": users,
            "irreversibility": "irreversible",
            "note": (
                f"{len(users)} object(s) use this integration. Removing it stops IP "
                "allocation, workflow runs or content syncs through it; the stored login is "
                "gone and must be given again to add it back"
            ),
            "match_count": 1,
        }
    }


for _op_id in DELETE_OP_IDS:
    register_preview_builder(_op_id, integration_delete_preview)
