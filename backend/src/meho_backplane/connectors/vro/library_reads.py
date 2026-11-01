# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Orchestrator library reads: actions, configuration elements and packages.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.vro.connector.VroConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.vro.library_ops`.

The vendor reference lists no paging query for ``/actions``,
``/configurations`` and ``/packages``: the server sends the whole list.
So these reads send no query, keep at most ``max_results`` rows and set
``more_rows`` when the server sent more. ``vro.action.list`` filters on
``module`` before the cut.

**Secrets.** A configuration element attribute of type ``SecureString``
or ``EncryptedString``, with the ``encrypt-value`` flag, or with a secret
name comes back with its value replaced by
:data:`~meho_backplane.connectors.vro.typed_reads.REDACTED`, the same rule
the run reads use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vro.session import VroTargetLike
from meho_backplane.connectors.vro.typed_reads import (
    DEFAULT_LIST_LIMIT,
    REDACTED,
    flat_rows,
    is_secret_parameter,
    path_segment,
    redact_secrets,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vro.connector import VroConnector

__all__ = [
    "LIBRARY_VENDOR_CALLS",
    "vro_action_list_impl",
    "vro_config_element_list_impl",
    "vro_package_list_impl",
]

_ACTIONS_PATH: Final = "/vco/api/actions"
_CONFIGURATIONS_PATH: Final = "/vco/api/configurations"
_CONFIGURATION_PATH: Final = "/vco/api/configurations/{id}"
_PACKAGES_PATH: Final = "/vco/api/packages"

#: Every vendor call of the library reads, as ``(method, path template)``.
LIBRARY_VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", _ACTIONS_PATH),
        ("GET", _CONFIGURATIONS_PATH),
        ("GET", _CONFIGURATION_PATH),
        ("GET", _PACKAGES_PATH),
    }
)


def _whole_list_answer(
    envelope: Any, params: dict[str, Any], *, keep: dict[str, str] | None = None
) -> dict[str, Any]:
    """``{items, total, count, more_rows}`` from a list the server sends whole.

    *keep* maps a row field to the value it must have; rows that differ are
    dropped before the cut, so ``total`` then counts the kept rows.
    """
    page = envelope if isinstance(envelope, dict) else {}
    rows = flat_rows(page.get("link"))
    total = page.get("total")
    if keep:
        rows = [row for row in rows if all(row.get(k) == v for k, v in keep.items())]
        total = len(rows)
    limit = int(params.get("max_results", DEFAULT_LIST_LIMIT))
    kept = rows[:limit]
    return {
        "items": redact_secrets(kept),
        "total": total,
        "count": len(kept),
        "more_rows": len(rows) > limit,
    }


async def vro_action_list_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.action.list`` -- ``GET /vco/api/actions``, one flat row per action."""
    payload = await connector._vro_get(target, _ACTIONS_PATH, operator=operator)
    module = params.get("module")
    keep = {"module": module} if isinstance(module, str) and module else None
    return _whole_list_answer(payload, params, keep=keep)


async def vro_package_list_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.package.list`` -- ``GET /vco/api/packages``, one flat row per package."""
    payload = await connector._vro_get(target, _PACKAGES_PATH, operator=operator)
    return _whole_list_answer(payload, params)


#: Value wrapper keys that hold a secret, whatever type the attribute declares.
_SECRET_WRAPPERS: Final[frozenset[str]] = frozenset({"secure-string", "encrypted-string"})


def _attribute_row(attribute: dict[str, Any]) -> dict[str, Any]:
    value = attribute.get("value")
    masked = is_secret_parameter(attribute) or (
        isinstance(value, dict) and not _SECRET_WRAPPERS.isdisjoint(value)
    )
    return {
        "name": attribute.get("name"),
        "type": attribute.get("type"),
        "description": attribute.get("description"),
        "masked": masked,
        "value": REDACTED if masked else value,
    }


async def vro_config_element_list_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.config_element.list`` -- the elements, or the attributes of one element.

    Without ``config_element_id``: ``GET /vco/api/configurations``, one flat
    row per element, and ``element`` is ``None``. With it:
    ``GET /vco/api/configurations/{id}``; ``element`` holds the element and
    ``items`` one row per attribute, secret values replaced.
    """
    element_id = params.get("config_element_id")
    if element_id is None:
        payload = await connector._vro_get(target, _CONFIGURATIONS_PATH, operator=operator)
        return {**_whole_list_answer(payload, params), "element": None}
    segment = path_segment("configuration element", element_id)
    element = await connector._vro_get(
        target, _CONFIGURATION_PATH.format(id=segment), operator=operator
    )
    attributes = element.get("attributes")
    rows = [_attribute_row(a) for a in attributes or [] if isinstance(a, dict)]
    limit = int(params.get("max_results", DEFAULT_LIST_LIMIT))
    kept = rows[:limit]
    return {
        "element": {
            "id": element.get("id"),
            "name": element.get("name"),
            "version": element.get("version"),
            "description": element.get("description"),
            "category_id": element.get("category-id"),
        },
        "items": redact_secrets(kept),
        "total": len(rows),
        "count": len(kept),
        "more_rows": len(rows) > limit,
    }
