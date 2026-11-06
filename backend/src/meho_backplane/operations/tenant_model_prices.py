# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prices a tenant sets for agent models that MEHO does not know (#5853).

:data:`~meho_backplane.operations.identity_budget.MODEL_PRICING` holds the
prices of the models MEHO knows. A run on any other model (an open model on
a model server the tenant runs itself, for example) costs 0, so the spend
limits do not count it. A tenant admin can give such a model a price here:
input and output, in USD per million tokens.

The prices are stored on the tenant row, in ``tenant.agent_model_prices``:
``{"<model id>": {"input": "<USD>", "output": "<USD>"}}``, prices as decimal
strings. The REST route ``/api/v1/tenants/model-prices`` writes the column
through :func:`set_model_price`.

Rules:

* A model in ``MODEL_PRICING`` always keeps that price. A tenant price for it
  is never read.
* A run looks up its full model id (``provider:model``) first, then the
  model name alone, so ``openai:qwen3`` and ``qwen3`` both match a run whose
  provider is ``openai`` and whose model is ``qwen3``.
* Cached input tokens cost the tenant's input price: the setting has one.
* No tenant price: the run costs 0 and MEHO logs the same warning as before
  (``identity_budget_pricing_miss``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import Tenant
from meho_backplane.operations.identity_budget import MODEL_PRICING, PerMillionPricing

__all__ = [
    "ModelPrice",
    "parse_model_prices",
    "price_for_model",
    "set_model_price",
]

_log = structlog.get_logger(__name__)

#: (input, output) in USD per million tokens.
ModelPrice = tuple[Decimal, Decimal]


def _parse_entry(entry: object) -> ModelPrice | None:
    """Read one stored ``{"input": ..., "output": ...}`` entry, or ``None``."""
    if not isinstance(entry, Mapping):
        return None
    try:
        price_in = Decimal(str(entry["input"]))
        price_out = Decimal(str(entry["output"]))
    except (KeyError, InvalidOperation):
        return None
    if not (price_in.is_finite() and price_out.is_finite()):
        return None
    if price_in < 0 or price_out < 0:
        return None
    return price_in, price_out


def parse_model_prices(stored: Mapping[str, object] | None) -> dict[str, ModelPrice]:
    """Return the tenant's prices by model id. A broken entry is left out."""
    if not stored:
        return {}
    prices: dict[str, ModelPrice] = {}
    for model, entry in stored.items():
        price = _parse_entry(entry)
        if price is None:
            _log.warning("tenant_model_price_unreadable", model_id=model)
            continue
        prices[model] = price
    return prices


async def price_for_model(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str | None,
    model: str | None,
) -> PerMillionPricing | None:
    """Return the tenant's price for a run's model, or ``None``.

    ``None`` when the model is in ``MODEL_PRICING`` (that price wins), when
    the run has no model, and when the tenant set no price for it.
    """
    if model is None:
        return None
    full_id = f"{provider}:{model}" if provider is not None else model
    if full_id in MODEL_PRICING:
        return None
    stored = (
        await session.execute(select(Tenant.agent_model_prices).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    prices = parse_model_prices(stored)
    price = prices.get(full_id) or prices.get(model)
    if price is None:
        return None
    # The setting has one input price. A cached input token costs the same,
    # so a provider that reports a prompt cache is not charged less.
    return PerMillionPricing(
        input=price[0], output=price[1], cache_read=price[0], cache_write=price[0]
    )


def set_model_price(tenant: Tenant, model: str, price: ModelPrice | None) -> bool:
    """Set or remove *tenant*'s price for *model*; return whether it changed.

    *price* ``None`` removes the model's price. The change is named in this
    request's ``audit_log`` row (model, before, after). Validation (name
    shape, bounds) stays with the caller's input model.
    """
    stored = dict(tenant.agent_model_prices or {})
    old = _parse_entry(stored.get(model))
    if old == price:
        return False
    if price is None:
        stored.pop(model, None)
    else:
        stored[model] = {"input": str(price[0]), "output": str(price[1])}
    # A new dict, so SQLAlchemy sees the JSON column change.
    tenant.agent_model_prices = stored or None
    structlog.contextvars.bind_contextvars(
        audit_agent_model_price_model=model,
        audit_agent_model_price_before=_audit_value(old),
        audit_agent_model_price_after=_audit_value(price),
        audit_agent_model_price_changed=True,
        audit_tenant_id=str(tenant.id),
    )
    _log.info("tenant_model_price_updated", tenant_id=str(tenant.id), model_id=model)
    return True


def _audit_value(price: ModelPrice | None) -> object:
    """Render a price for the audit payload; ``None`` is ``"none"``."""
    if price is None:
        return "none"
    return {"input": str(price[0]), "output": str(price[1])}
