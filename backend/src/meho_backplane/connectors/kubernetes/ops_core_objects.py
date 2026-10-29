# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Core-group object reads for the generic ``k8s.cr.list`` / ``k8s.cr.info`` ops (#3840).

``CustomObjectsApi`` builds ``/apis/{group}/{version}/...`` paths. The
core API group has an empty name and lives under a different root,
``/api/{version}/...`` (for example ``/api/v1/namespaces``). So a call
with ``group=""`` through ``CustomObjectsApi`` asks for ``/apis//v1/...``
and the API server answers 404. These helpers send the same GET to the
core root instead, through the same :class:`ApiClient` (same auth, same
TLS, same wire span), and return the plain dict the API server sends.

:func:`list_items` also fixes one gap in list answers. The API server
does not repeat ``kind`` / ``apiVersion`` on each item of a list of
built-in objects (only the list itself carries ``kind: SecretList``).
The row projection keys its Secret redaction on ``kind: Secret``, so
every item gets the kind and apiVersion of its list before projection.
"""

from __future__ import annotations

from typing import Any

from kubernetes_asyncio import client

__all__ = ["get_core_object", "list_core_objects", "list_items"]

#: Same accept and auth settings the generated ``CustomObjectsApi`` uses.
_HEADERS = {"Accept": "application/json"}
_AUTH_SETTINGS = ["BearerToken"]
_RESPONSE_TYPES = {200: "object", 401: None}


async def _core_get(
    api_client: client.ApiClient, resource_path: str, path_params: dict[str, str]
) -> dict[str, Any]:
    """GET *resource_path* under the core root and return the JSON body as a dict.

    ``call_api`` quotes every path parameter with no safe characters, so a
    ``/`` in ``plural`` or ``name`` cannot reach a sub-resource path.
    """
    result: dict[str, Any] = await api_client.call_api(
        resource_path,
        "GET",
        path_params=path_params,
        query_params=[],
        header_params=dict(_HEADERS),
        response_types_map=_RESPONSE_TYPES,
        auth_settings=_AUTH_SETTINGS,
        _return_http_data_only=True,
    )
    return result


async def list_core_objects(
    api_client: client.ApiClient,
    *,
    version: str,
    plural: str,
    namespace: str | None,
) -> dict[str, Any]:
    """List core-group objects: one namespace, or every namespace / a cluster-scoped kind."""
    if namespace:
        return await _core_get(
            api_client,
            "/api/{version}/namespaces/{namespace}/{plural}",
            {"version": version, "namespace": namespace, "plural": plural},
        )
    return await _core_get(
        api_client, "/api/{version}/{plural}", {"version": version, "plural": plural}
    )


async def get_core_object(
    api_client: client.ApiClient,
    *,
    version: str,
    plural: str,
    name: str,
    namespace: str | None,
) -> dict[str, Any]:
    """Read one named core-group object, namespaced or cluster-scoped."""
    if namespace:
        return await _core_get(
            api_client,
            "/api/{version}/namespaces/{namespace}/{plural}/{name}",
            {"version": version, "namespace": namespace, "plural": plural, "name": name},
        )
    return await _core_get(
        api_client,
        "/api/{version}/{plural}/{name}",
        {"version": version, "plural": plural, "name": name},
    )


def list_items(resp: dict[str, Any], *, group: str, plural: str) -> list[dict[str, Any]]:
    """Return the items of a list answer, each with its own ``kind`` / ``apiVersion``.

    An item that already names its kind keeps it (custom resources do).
    Otherwise the kind comes from the list kind without its ``List``
    suffix. If the list kind is missing too, a core ``secrets`` list
    still counts as ``Secret``, so redaction can never be skipped for it.
    """
    list_kind = resp.get("kind")
    item_kind: str | None = None
    if isinstance(list_kind, str) and list_kind.endswith("List") and len(list_kind) > 4:
        item_kind = list_kind[: -len("List")]
    elif group == "" and plural == "secrets":
        item_kind = "Secret"
    api_version = resp.get("apiVersion")
    items: list[dict[str, Any]] = []
    for item in resp.get("items") or []:
        if not item.get("kind") and item_kind is not None:
            item = {**item, "kind": item_kind}
            if not item.get("apiVersion") and api_version:
                item["apiVersion"] = api_version
        items.append(item)
    return items
