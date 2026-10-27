# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure inventory operations, and the subscription candidates.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.inventory.InventoryOps`; the operation
table and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.inventory`.

* The lists of the target's own subscription (resource groups, resources, tag
  names, locks) follow ``nextLink`` with
  :func:`~meho_backplane.connectors.azure._paging.list_all_pages`.
* The subscription list is the one list whose next page is not inside the
  target's subscription. :func:`list_visible_subscriptions` checks that link
  itself: the target's Resource Manager host and the path ``/subscriptions``,
  nothing else. Only the link's query values are sent on.
* ``azure.tag.update`` changes the tags of one resource group or resource. The
  resource ID must lie in the target's subscription; it is checked before any
  call.
* :func:`list_subscription_candidates` lists the subscriptions the seed's
  login can see that are not a target yet (``meho targets discover``).

The Resource Graph reads over several subscriptions are in
:mod:`meho_backplane.connectors.azure.inventory_graph`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import httpx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureUrlRefusedError
from meho_backplane.connectors.azure._http import MAX_AZURE_URL_LENGTH, send

# The pager of ``list_all_pages``, reused for the one list whose next page
# ``list_all_pages`` refuses (the subscription list), so both share the limits.
from meho_backplane.connectors.azure._paging import PagedRows, PageLimits, _Pager, list_all_pages
from meho_backplane.connectors.azure._paths import (
    LOCKS_API_VERSION,
    LOCKS_PATH,
    RESOURCE_GROUP_RESOURCES_PATH,
    RESOURCE_GROUPS_PATH,
    RESOURCES_API_VERSION,
    RESOURCES_PATH,
    SUBSCRIPTIONS_API_VERSION,
    SUBSCRIPTIONS_PATH,
    TAG_NAMES_PATH,
    TAGS_AT_SCOPE_PATH,
)
from meho_backplane.connectors.azure.session import (
    AzureTargetConfig,
    AzureTargetLike,
    resolve_target_config,
)
from meho_backplane.connectors.schemas import CandidateHint
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "RESOURCE_GROUP_PATTERN",
    "RESOURCE_TYPE_PATTERN",
    "checked_resource_id",
    "list_subscription_candidates",
    "list_visible_subscriptions",
    "lock_list_impl",
    "resource_group_list_impl",
    "resource_list_impl",
    "subscription_list_impl",
    "tag_list_impl",
    "tag_update_impl",
]

#: A resource type such as ``Microsoft.Compute/virtualMachines``. It goes into
#: a ``$filter`` text, so it may hold no quote and no space.
RESOURCE_TYPE_PATTERN: Final[str] = r"^[A-Za-z][A-Za-z0-9.\-]*(/[A-Za-z0-9][A-Za-z0-9.\-]*)+$"

#: A resource group name, as Azure's API description allows it.
RESOURCE_GROUP_PATTERN: Final[str] = r"^[-\w._()]+$"

#: Longest resource ID MEHO accepts for a tag change.
MAX_RESOURCE_ID_LENGTH: Final[int] = 2048

#: Characters that never belong in a resource ID: a query or fragment start, an
#: encoded character, a backslash, or a control character.
_BAD_ID_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[?#%\\\x00-\x1f\x7f]")

#: The tag path MEHO adds itself; a resource ID must not end with it.
_TAGS_SUFFIX: Final[str] = "/providers/microsoft.resources/tags/default"

_SLUG_RUN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


# ----------------------------------------------------------------------
# Lists of the target's own subscription
# ----------------------------------------------------------------------


async def _own_list(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    path: str,
    *,
    api_version: str,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read every page of *path* (a path in the target's subscription)."""
    paged = await list_all_pages(
        connector, target, path, operator=operator, api_version=api_version, params=query
    )
    return paged.as_dict()


async def resource_group_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.resource_group.list``: the resource groups of the target's subscription."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    path = RESOURCE_GROUPS_PATH.format(subscription_id=config.subscription_id)
    return await _own_list(connector, operator, target, path, api_version=RESOURCES_API_VERSION)


async def resource_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.resource.list``: the resources, optionally of one type or one group.

    With ``resource_group`` the list is read from that group (Resources - List
    By Resource Group), because Azure does not allow a filter on the resource
    type and the group together.
    """
    config = resolve_target_config(target)
    resource_type = _matching(params.get("type"), RESOURCE_TYPE_PATTERN, "type")
    resource_group = _matching(
        params.get("resource_group"), RESOURCE_GROUP_PATTERN, "resource_group"
    )
    if resource_group is None:
        path = RESOURCES_PATH.format(subscription_id=config.subscription_id)
    else:
        path = RESOURCE_GROUP_RESOURCES_PATH.format(
            subscription_id=config.subscription_id, resource_group=resource_group
        )
    query = {"$filter": f"resourceType eq '{resource_type}'"} if resource_type else None
    return await _own_list(
        connector, operator, target, path, api_version=RESOURCES_API_VERSION, query=query
    )


async def tag_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.tag.list``: the tag names of the subscription with their values and counts."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    path = TAG_NAMES_PATH.format(subscription_id=config.subscription_id)
    return await _own_list(connector, operator, target, path, api_version=RESOURCES_API_VERSION)


async def lock_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.lock.list``: the management locks of the subscription and what is in it."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    path = LOCKS_PATH.format(subscription_id=config.subscription_id)
    return await _own_list(connector, operator, target, path, api_version=LOCKS_API_VERSION)


def _matching(value: object, pattern: str, name: str) -> str | None:
    """Return *value* when it is a text that matches *pattern*, ``None`` when unset.

    The parameter schema already checks this; the second check keeps a value
    that goes into a path or a filter safe even when the body is called directly.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"{name} has a form Azure does not accept.")
    return value


# ----------------------------------------------------------------------
# The subscription list
# ----------------------------------------------------------------------


async def subscription_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.subscription.list``: every subscription the target's login can see."""
    del params  # the operation takes no parameters
    paged = await list_visible_subscriptions(connector, operator, target)
    return paged.as_dict()


async def list_visible_subscriptions(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    *,
    limits: PageLimits | None = None,
) -> PagedRows:
    """``GET /subscriptions`` with the login of *target*, every page, with limits.

    The next page of this list is ``/subscriptions`` again, with a page token,
    so the URL check of every other list (which wants the target's own
    subscription in the path) would refuse it. :func:`_next_page_query` checks
    it instead and returns only its query values; the request goes to the
    hand-written path, so the token never goes anywhere else.
    """
    pager = _Pager(limits or PageLimits())
    query: dict[str, str] = {"api-version": SUBSCRIPTIONS_API_VERSION}
    while True:
        response = await send(
            connector, target, "GET", SUBSCRIPTIONS_PATH, operator=operator, params=query
        )
        answer = json_payload_or_empty(response)
        link = answer.get("nextLink")
        if not pager.add(answer.get("value")) or not isinstance(link, str) or not link:
            return pager.result
        query = _next_page_query(target, link)
        if not pager.may_read_more():
            return pager.result


def _next_page_query(target: AzureTargetLike, link: str) -> dict[str, str]:
    """Check the ``nextLink`` of the subscription list and return its query values.

    The link must be an https URL on port 443 of the target's Resource Manager
    host, with no user name or password, at most 4 KB long, and its path must be
    ``/subscriptions``. Raises :class:`AzureUrlRefusedError` otherwise; the
    message never repeats the link.
    """
    config = resolve_target_config(target)
    refused = AzureUrlRefusedError(
        f"Azure target {target.name!r}: the next page of the subscription list does "
        "not point to the subscription list on the target's host. MEHO sent no token to it."
    )
    if len(link) > MAX_AZURE_URL_LENGTH:
        raise refused
    try:
        url = httpx.URL(link)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise refused from exc
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port not in (None, 443)
        or url.host.lower() != config.cloud.management_host
        or url.path.lower() != SUBSCRIPTIONS_PATH
    ):
        raise refused
    query = dict(url.params.items())
    query.setdefault("api-version", SUBSCRIPTIONS_API_VERSION)
    return query


# ----------------------------------------------------------------------
# Tags of one resource
# ----------------------------------------------------------------------


def checked_resource_id(target: AzureTargetLike, resource_id: object) -> str:
    """Return *resource_id* when it is a resource group or a resource of the target.

    Allowed: ``/subscriptions/<the target's subscription>/resourceGroups/<name>``,
    alone or followed by ``/providers/<namespace>/<type>/<name>...``. Refused: any
    other subscription, the subscription itself, an empty, ``.`` or ``..`` part,
    ``?``, ``#``, ``%``, a backslash, a control character, more than
    :data:`MAX_RESOURCE_ID_LENGTH` characters, and the tag path MEHO adds itself.
    Raises :class:`ValueError` before any call to Azure.
    """
    config = resolve_target_config(target)
    text = resource_id if isinstance(resource_id, str) else ""
    parts = text.split("/")
    inside = (
        0 < len(text) <= MAX_RESOURCE_ID_LENGTH
        and _BAD_ID_CHARACTERS.search(text) is None
        and not text.lower().endswith(_TAGS_SUFFIX)
        and len(parts) >= 5
        and parts[0] == ""
        and parts[1].lower() == "subscriptions"
        and parts[2].lower() == config.subscription_id
        and parts[3].lower() == "resourcegroups"
        and (len(parts) == 5 or (len(parts) >= 9 and parts[5].lower() == "providers"))
        and all(part not in ("", ".", "..") for part in parts[1:])
    )
    if not inside:
        raise ValueError(
            f"Azure target {target.name!r}: resource_id must be a resource group or a "
            "resource in the target's own subscription, such as /subscriptions/<the "
            "target's subscription>/resourceGroups/<group>/providers/<namespace>/<type>/"
            "<name>. MEHO sent nothing to Azure."
        )
    return text


async def tag_update_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.tag.update``: merge tags into one resource, or remove named tags.

    ``merge`` sends ``PATCH .../tags/default`` with ``operation: Merge``: new
    names are added and existing names get the new value. ``remove`` first reads
    the resource's tags, then sends ``operation: Delete`` with the current value
    of each named tag that is there, so only those tags go, whatever their value.
    When none of them is there, nothing is sent. Both give the same end state
    when run twice.
    """
    resource_id = checked_resource_id(target, params.get("resource_id"))
    mode = params.get("mode")
    tags = params.get("tags")
    if mode not in ("merge", "remove") or not isinstance(tags, dict) or not tags:
        raise ValueError("mode must be merge or remove, and tags must name at least one tag.")
    path = TAGS_AT_SCOPE_PATH.format(scope=resource_id[1:])
    not_present: list[str] = []
    if mode == "merge":
        after = await _patch_tags(connector, operator, target, path, "Merge", tags)
    else:
        current = await _read_tags(connector, operator, target, path)
        by_name = {name.lower(): name for name in current}
        named = {by_name[name.lower()] for name in tags if name.lower() in by_name}
        not_present = [name for name in tags if name.lower() not in by_name]
        after = current
        if named:
            to_delete = {name: current[name] for name in sorted(named)}
            after = await _patch_tags(connector, operator, target, path, "Delete", to_delete)
    return {"resource_id": resource_id, "mode": mode, "tags": after, "not_present": not_present}


async def _read_tags(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, path: str
) -> dict[str, Any]:
    """``GET .../tags/default``: the tags the resource has now."""
    response = await send(
        connector,
        target,
        "GET",
        path,
        operator=operator,
        params={"api-version": RESOURCES_API_VERSION},
    )
    return _tags_of(json_payload_or_empty(response))


async def _patch_tags(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    path: str,
    operation: str,
    tags: dict[str, Any],
) -> dict[str, Any]:
    """``PATCH .../tags/default`` with *operation*; returns the tags after the change.

    A PATCH is sent again only after a 429 or a 401 (then Azure did not run it),
    never after a timeout or a 5xx.
    """
    response = await send(
        connector,
        target,
        "PATCH",
        path,
        operator=operator,
        params={"api-version": RESOURCES_API_VERSION},
        json={"operation": operation, "properties": {"tags": tags}},
    )
    return _tags_of(json_payload_or_empty(response))


def _tags_of(answer: dict[str, Any]) -> dict[str, Any]:
    """The ``properties.tags`` of a tags answer, or ``{}``."""
    properties = answer.get("properties")
    tags = properties.get("tags") if isinstance(properties, dict) else None
    return dict(tags) if isinstance(tags, dict) else {}


# ----------------------------------------------------------------------
# Candidates for `meho targets discover`
# ----------------------------------------------------------------------


async def list_subscription_candidates(
    connector: AzureConnector,
    seed: AzureTargetLike,
    operator: Operator,
) -> list[CandidateHint]:
    """One candidate per subscription the seed's login can see that is not a target yet.

    Each candidate carries what ``meho targets import`` needs: the seed's
    Resource Manager host and port, its secret reference, and the extras
    ``subscription_id``, ``cloud`` and ``tenant_id`` (the seed's, else the
    tenant of the login's token). A subscription that is not ``Enabled`` gets
    ``confidence="low"``. The display name from Azure is untrusted text: it
    only goes into ``evidence`` and, cut to letters and digits, into the name.
    """
    config = resolve_target_config(seed)
    paged = await list_visible_subscriptions(connector, operator, seed)
    registered = await _registered_subscription_ids(seed.tenant_id, connector.product)
    tenant_id = config.tenant_id or (await connector.login_identity(seed, operator)).tenant_id
    candidates: list[CandidateHint] = []
    for row in paged.rows:
        subscription_id = _subscription_id_of(row)
        if subscription_id is None or subscription_id in registered:
            continue
        registered.add(subscription_id)
        candidates.append(_candidate(row, subscription_id, seed, config, tenant_id))
    return candidates


def _subscription_id_of(row: object) -> str | None:
    """The lower-case subscription ID of one list row, or ``None``."""
    value = row.get("subscriptionId") if isinstance(row, dict) else None
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _candidate(
    row: dict[str, Any],
    subscription_id: str,
    seed: AzureTargetLike,
    config: AzureTargetConfig,
    tenant_id: str | None,
) -> CandidateHint:
    display_name = row.get("displayName")
    state = row.get("state")
    extras: dict[str, Any] = {"subscription_id": subscription_id, "cloud": config.cloud.name}
    if tenant_id:
        extras["tenant_id"] = tenant_id
    return CandidateHint(
        name=_candidate_name(display_name, subscription_id),
        host=config.cloud.management_host,
        port=seed.port,
        secret_ref=seed.secret_ref,
        extras=extras,
        evidence={
            "source": "GET /subscriptions with the login of the seed target",
            "seed_target": seed.name,
            "display_name": display_name if isinstance(display_name, str) else None,
            "state": state if isinstance(state, str) else None,
        },
        confidence="high" if state == "Enabled" else "low",
    )


def _candidate_name(display_name: object, subscription_id: str) -> str:
    """``azure-<display name, letters and digits only>-<start of the ID>``."""
    text = display_name.lower() if isinstance(display_name, str) else ""
    slug = _SLUG_RUN.sub("-", text).strip("-")[:40].strip("-")
    return f"azure-{slug}-{subscription_id[:8]}" if slug else f"azure-{subscription_id}"


async def _registered_subscription_ids(tenant_id: object, product: str) -> set[str]:
    """The subscription IDs of the live targets of *product* in the MEHO tenant."""
    tenant = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(TargetORM.extras).where(
                TargetORM.tenant_id == tenant,
                TargetORM.product == product,
                TargetORM.deleted_at.is_(None),
            )
        )
        ids = (
            extras.get("subscription_id") for extras in result.scalars() if isinstance(extras, dict)
        )
        return {value.strip().lower() for value in ids if isinstance(value, str) and value.strip()}
