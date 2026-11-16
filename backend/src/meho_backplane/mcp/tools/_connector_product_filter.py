# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``product`` filter of ``meho_connector_list`` with other names (#6560).

A small model that looks for "the vCenter connector" filters the list by
``product="vcenter"``. MEHO's product name is ``vmware``, so the filter
found nothing and the model gave up.

The switch ``CONNECTOR_LIST_PRODUCT_ALIASES``
(:attr:`~meho_backplane.settings.Settings.connector_list_product_aliases`,
off by default) changes the filter:

* Case is ignored.
* A connector class may declare other names for its product
  (:attr:`~meho_backplane.connectors.base.Connector.product_aliases`). The
  filter matches them too. The core holds no vendor name: it only reads
  what the registered classes declare.
* The answer has a ``product_filter`` block. It names the name that
  matched for each product. When nothing matched, it lists the products
  that exist (after the ``status`` filter) and a hint to ask again.
* A list in the block that is too long for the answer is stored behind a
  handle, like a long ``connectors`` list (:func:`reduce_product_filter`).

With the switch off, :func:`filter_by_product` is not called and the answer
is the same as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from meho_backplane.connectors.registry import connectors_for_tenant
from meho_backplane.operations.ingest import ConnectorListItem
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

__all__ = [
    "LISTED_PRODUCTS_KEY",
    "PRODUCT_FILTER_KEY",
    "connector_list_product_aliases",
    "filter_by_product",
    "keep_allowed_product_filter",
    "listed_products",
    "reduce_product_filter",
]

#: The key of the block next to ``connectors`` in the answer.
PRODUCT_FILTER_KEY: Final[str] = "product_filter"

#: The key of ``{connector_id: product}`` for every listed row, before the
#: product filter. Only the rows not yet reduced carry it: the hosted agent
#: reads it to name the products it may use, and the reducer drops it.
LISTED_PRODUCTS_KEY: Final[str] = "_listed_products"

#: The lists of the block that may be long.
_BLOCK_LISTS: Final[tuple[str, ...]] = ("matches", "known_products")

_NO_MATCH_HINT: Final[str] = (
    "No connector has this product name or another name for it. Ask again "
    "with one of known_products, or leave product out to list every connector."
)

_NO_ALLOWED_MATCH_HINT: Final[str] = (
    "No connector this agent may use has this product name or another name "
    "for it. Ask again with one of known_products, or leave product out to "
    "list the connectors this agent may use."
)


def connector_list_product_aliases() -> bool:
    """Return the state of the ``CONNECTOR_LIST_PRODUCT_ALIASES`` switch."""
    return get_settings().connector_list_product_aliases


def _aliases_by_product(tenant_id: UUID | None) -> dict[str, dict[str, str]]:
    """Return ``{product: {folded name: declared name}}`` from the registry.

    The names of every class registered for a product count for that
    product, so one class that declares them covers all connectors of the
    product. A name that is not a non-empty string is skipped.
    """
    names: dict[str, dict[str, str]] = {}
    for (product, _version, _impl_id), cls in connectors_for_tenant(tenant_id).items():
        for alias in getattr(cls, "product_aliases", ()) or ():
            if isinstance(alias, str) and alias.strip():
                names.setdefault(product, {})[alias.strip().casefold()] = alias.strip()
    return names


def _matched_name(product: str, query: str, aliases: dict[str, str]) -> str | None:
    """Return the name of *product* that *query* matches, or ``None``."""
    if product.casefold() == query:
        return product
    return aliases.get(query)


def filter_by_product(
    items: Sequence[ConnectorListItem],
    product: str,
    tenant_id: UUID | None,
) -> tuple[list[ConnectorListItem], dict[str, Any]]:
    """Return the *items* whose product matches *product*, and the block.

    *product* is not empty. A row matches when its product is *product*
    (case ignored) or one of the product's other names is. The block holds
    ``query`` and ``matches`` (one ``{product, matched_name}`` per matched
    product, sorted). With no match it also holds ``known_products`` (the
    products of *items*, sorted) and a ``hint``.
    """
    query = product.casefold()
    aliases = _aliases_by_product(tenant_id)
    matched: dict[str, str] = {}
    kept: list[ConnectorListItem] = []
    for item in items:
        name = _matched_name(item.product, query, aliases.get(item.product, {}))
        if name is None:
            continue
        matched[item.product] = name
        kept.append(item)
    block: dict[str, Any] = {
        "query": product,
        "matches": [{"product": key, "matched_name": matched[key]} for key in sorted(matched)],
    }
    if not kept:
        block["known_products"] = sorted({item.product for item in items})
        block["hint"] = _NO_MATCH_HINT
    return kept, block


def listed_products(items: Sequence[ConnectorListItem]) -> dict[str, str]:
    """Return ``{connector_id: product}`` for *items* (:data:`LISTED_PRODUCTS_KEY`)."""
    return {item.connector_id: item.product for item in items}


def keep_allowed_product_filter(
    block: dict[str, Any],
    kept_rows: Sequence[dict[str, Any]],
    listed: Mapping[str, str],
    allowed_connectors: frozenset[str],
) -> dict[str, Any]:
    """Narrow a ``product_filter`` *block* to the rows a hosted agent may use.

    ``matches`` keeps only the products of *kept_rows*. When none is left,
    ``known_products`` names only the products of the allowed connectors in
    *listed* (``{connector_id: product}`` before the product filter), so a
    product the agent may not use is never named.
    """
    products = {row.get("product") for row in kept_rows}
    matches = [match for match in block.get("matches", []) if match.get("product") in products]
    if matches:
        return {**block, "matches": matches}
    allowed = {product for cid, product in listed.items() if cid in allowed_connectors}
    return {
        "query": block.get("query"),
        "matches": [],
        "known_products": sorted(allowed),
        "hint": _NO_ALLOWED_MATCH_HINT,
    }


async def reduce_product_filter(
    reducer: JsonFluxReducer,
    block: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Return *block* with each long list stored behind a handle.

    ``matches`` and ``known_products`` each go through *reducer* with the
    same limits as the rows (more than 50 entries or 4 KB). A short list
    stays as it is. A long one becomes the reducer's summary with a
    ``handle``; ``result_query`` reads its rows. ``known_products`` goes to
    the reducer as ``{"product": name}`` rows.
    """
    reduced = dict(block)
    for key in _BLOCK_LISTS:
        values = block.get(key)
        if not values:
            continue
        rows = [{"product": v} if isinstance(v, str) else v for v in values]
        summary, handle = await reducer.reduce({key: rows}, None, context)
        if handle is not None:
            reduced[key] = {**summary, "handle": handle.model_dump(mode="json")}
    return reduced
