# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator policy surface for per-tenant configuration (#3272, #3447, #3499).

Three policy families live here: the flight-recorder capture policy (#3272,
#3447), the mail-recipient allowlist (#3499,
:func:`update_mail_recipient_policy`) and the prices of agent models MEHO does
not know (#5853, :func:`update_model_price`). All share the surface posture
and scope rules documented next.

Flight-recorder policy (#3272, #3447).

The flight-recorder decision (``docs/decisions/dispatch-flight-recorder.md``,
F1) models capture enablement as an operator action -- "a lab-class tenant is
one an operator flips ON". The policy columns shipped (#3212/#3216) and the
resolver (:mod:`meho_backplane.flight_recorder.config`) reads them per dispatch,
but there was **no writable path**: no ``/api/v1/tenants`` CRUD, so capture
could not be enabled on a deployment without direct DB writes (which the
governance model forbids). The PATCH route closes that gap for the three
per-tenant policy fields, while the GET route exposes both their raw stored
values and the resolver's effective policy. The per-target tri-state override
(``targets.flight_recorder_capture``) rides the existing
``PATCH /api/v1/targets/{name}`` route (see :mod:`meho_backplane.api.v1.targets`).

Surface posture (postulate 5): this is an **operator-plane** route -- REST +
CLI (``meho tenants flight-recorder-policy set``) only. It is deliberately
**not** on the agent working surface and has **no MCP tool** at all; a
governance-config mutation is an operator action, not an agent one.

Scope + authorization:

* **Tenant-scoped, self-only.** The route operates on ``operator.tenant_id``
  read from the JWT -- it accepts *no* tenant id in the path or body, so it
  structurally cannot write another tenant's policy (no cross-tenant write).
* **``tenant_admin`` claim.** Capture policy is governance config, so the
  mutation is gated at the admin tier via
  :func:`~meho_backplane.auth.rbac.require_role` -- the same gate the target
  PATCH and the tenant-convention writes use. ``operator`` / ``read_only``
  callers get 403 ``insufficient_role``.

Audit + cache: every applied change binds ``audit_*`` contextvars naming
field / old / new, which :class:`~meho_backplane.audit.AuditMiddleware` folds
into this request's ``audit_log`` row (governance-relevant mutation, never
silent). On a change the route also evicts the resolver's per-tenant cache via
:func:`~meho_backplane.flight_recorder.config.invalidate_tenant_policy_cache`
so the flip takes effect on the next dispatch rather than waiting out the 60s
TTL or a restart.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors.mail.allowlist import parse_recipient_allowlist
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder.config import (
    resolve_retention_days,
    should_capture,
    should_expose_to_agent,
)
from meho_backplane.operations.tenant_model_prices import parse_model_prices, set_model_price
from meho_backplane.operations.unpriced_model import is_model_priced
from meho_backplane.tenancy.policies import (
    set_flight_recorder_policy,
    set_mail_recipient_allowlist,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])

#: Module-level :class:`Depends` closure -- built once at import time to
#: satisfy ruff's B008 rule. Capture policy is governance config, gated at the
#: admin tier (same as the target PATCH and tenant-convention writes).
_require_tenant_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


class TenantFlightRecorderPolicy(BaseModel):
    """Resolved per-tenant flight-recorder policy -- the PATCH read-back shape.

    Frozen; maps 1:1 to the three ``tenant`` policy columns plus the tenant id.
    ``flight_recorder_agent_readable`` and ``flight_recorder_retention_days``
    are nullable: ``None`` means "inherit" (agent-read follows the capture
    default) / "use the global default" (retention) respectively.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    flight_recorder_enabled: bool
    flight_recorder_agent_readable: bool | None
    flight_recorder_retention_days: int | None


class EffectiveTenantFlightRecorderPolicy(BaseModel):
    """Flight-recorder policy after global and tenant defaults are resolved."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    flight_recorder_enabled: bool
    flight_recorder_agent_readable: bool
    flight_recorder_retention_days: int


class TenantFlightRecorderPolicyRead(BaseModel):
    """Tenant policy read response with resolved and stored values (#3447)."""

    model_config = ConfigDict(frozen=True)

    effective: EffectiveTenantFlightRecorderPolicy
    raw: TenantFlightRecorderPolicy


@router.get("/flight-recorder-policy", response_model=TenantFlightRecorderPolicyRead)
async def get_flight_recorder_policy(
    operator: Operator = _require_tenant_admin,
    session: AsyncSession = Depends(get_session),
) -> TenantFlightRecorderPolicyRead:
    """Read the caller's effective and raw flight-recorder policy (#3447).

    The raw section is the tenant row's stored tri-state values. The effective
    section deliberately calls the dispatch resolver so global defaults and the
    global capture kill switch use exactly the same precedence as the hot path.
    No target is supplied: this is the tenant default before a per-target
    override is considered.
    """
    tenant = await session.get(Tenant, operator.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    raw = TenantFlightRecorderPolicy(
        tenant_id=tenant.id,
        flight_recorder_enabled=tenant.flight_recorder_enabled,
        flight_recorder_agent_readable=tenant.flight_recorder_agent_readable,
        flight_recorder_retention_days=tenant.flight_recorder_retention_days,
    )
    effective = EffectiveTenantFlightRecorderPolicy(
        tenant_id=tenant.id,
        flight_recorder_enabled=await should_capture(tenant_id=tenant.id),
        flight_recorder_agent_readable=await should_expose_to_agent(tenant_id=tenant.id),
        flight_recorder_retention_days=await resolve_retention_days(tenant.id),
    )
    return TenantFlightRecorderPolicyRead(effective=effective, raw=raw)


class TenantFlightRecorderPolicyUpdate(BaseModel):
    """``PATCH /api/v1/tenants/flight-recorder-policy`` body.

    All three fields are optional-partial: only fields the client actually
    sends are applied (``model_dump(exclude_unset=True)`` in the handler keys
    off ``model_fields_set``, so a JSON ``null`` is distinguished from an
    absent key). ``extra='forbid'`` rejects unknown keys with a 422.

    * ``flight_recorder_enabled`` (F1) -- Boolean, ``NOT NULL`` at the DB
      layer. Absent = leave unchanged; ``true`` / ``false`` flips the
      per-tenant capture default. An explicit ``null`` is rejected (the column
      has no inherit state) -- the validator fires only when the key is
      present, so an absent field never trips it.
    * ``flight_recorder_agent_readable`` (F5) -- **tri-state**. Absent = leave
      unchanged; ``true`` / ``false`` force the agent-read override on / off;
      ``null`` clears it back to inherit (follow the capture default). This is
      the JSON-``null``-vs-absent distinction the tri-state needs.
    * ``flight_recorder_retention_days`` (F4) -- nullable, bounded ``1..365``
      (the reaper window; matches ``Settings.flight_recorder_retention_days_default``'s
      ``ge=1, le=365``). Absent = leave unchanged; an int sets the per-tenant
      window; ``null`` clears it back to the global default. The bound applies
      only to the int arm -- a ``null`` clear is always legal.
    """

    model_config = ConfigDict(extra="forbid")

    flight_recorder_enabled: bool | None = None
    flight_recorder_agent_readable: bool | None = None
    flight_recorder_retention_days: Annotated[int, Field(ge=1, le=365)] | None = None

    @field_validator("flight_recorder_enabled")
    @classmethod
    def _reject_null_enabled(cls, value: bool | None) -> bool | None:
        """Reject an explicit ``{"flight_recorder_enabled": null}``.

        The column is ``NOT NULL`` (no inherit state); ``null`` is a caller
        error, not a clear-to-default. Pydantic runs a ``field_validator`` only
        for a *provided* field, so an absent ``flight_recorder_enabled`` (the
        leave-unchanged case) never reaches this check.
        """
        if value is None:
            raise ValueError(
                "flight_recorder_enabled cannot be null (the column is NOT NULL); "
                "omit it to leave it unchanged, or send true / false"
            )
        return value


@router.patch("/flight-recorder-policy", response_model=TenantFlightRecorderPolicy)
async def update_flight_recorder_policy(
    body: TenantFlightRecorderPolicyUpdate,
    operator: Operator = _require_tenant_admin,
    session: AsyncSession = Depends(get_session),
) -> TenantFlightRecorderPolicy:
    """Partially update the caller's tenant flight-recorder policy (#3272).

    Tenant-scoped to ``operator.tenant_id`` (no cross-tenant write is
    expressible). Applies only the fields present in the body; each field whose
    value actually changes is folded into this request's ``audit_log`` row
    (field / old / new) and, once any change lands, the resolver's per-tenant
    cache is evicted so the next dispatch sees the new policy without a restart.
    """
    tenant = await session.get(Tenant, operator.tenant_id)
    if tenant is None:
        # Defensive depth: every authenticated request runs the
        # ``ensure_tenant`` middleware get-or-create first, so the row exists by
        # the time we read it. This guard only fires if that invariant is ever
        # bypassed -- surface it structured rather than 500-ing on the setattr.
        raise HTTPException(status_code=404, detail="tenant_not_found")

    # Field / old / new audit binding and the resolver-cache eviction live in
    # the shared service function (the settings import writes the same
    # columns). The dependency commits after this handler returns; the next
    # resolver read misses the cache and reads the committed value -- proven
    # by the without-restart test.
    set_flight_recorder_policy(tenant, body.model_dump(exclude_unset=True))

    return TenantFlightRecorderPolicy(
        tenant_id=tenant.id,
        flight_recorder_enabled=tenant.flight_recorder_enabled,
        flight_recorder_agent_readable=tenant.flight_recorder_agent_readable,
        flight_recorder_retention_days=tenant.flight_recorder_retention_days,
    )


class TenantMailRecipientPolicy(BaseModel):
    """Resolved per-tenant mail-recipient policy -- the PATCH read-back shape (#3499).

    Frozen; maps 1:1 to the tenant's ``mail_recipient_allowlist`` column plus the
    tenant id. ``mail_recipient_allowlist`` is nullable: ``None`` means
    **inherit** (no per-tenant narrowing; the deployment-level
    ``MAIL_RECIPIENT_ALLOWLIST`` instance floor alone governs the send), ``""``
    means **deny** (an empty tenant allowlist refuses every dispatched
    ``mail.send``), and a comma-separated string is the tenant's own recipient
    space (still intersected with the instance floor at the transport).
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    mail_recipient_allowlist: str | None


class TenantMailRecipientPolicyUpdate(BaseModel):
    """``PATCH /api/v1/tenants/mail-recipient-policy`` body (#3499).

    The single field is optional-partial (``model_dump(exclude_unset=True)`` in
    the handler keys off ``model_fields_set``, so a JSON ``null`` is
    distinguished from an absent key). ``extra='forbid'`` rejects unknown keys
    with a 422.

    * absent = leave the column unchanged.
    * ``null`` = clear back to **inherit** (the instance floor alone governs the
      tenant's ``mail.send``).
    * ``""`` = **deny** (an empty tenant allowlist refuses every dispatched
      send) -- the shared-instance containment lever.
    * a comma-separated address/domain string = the tenant's own recipient
      space. Validated with the same grammar as the instance floor
      (:func:`~meho_backplane.connectors.mail.allowlist.parse_recipient_allowlist`),
      so a malformed entry (whitespace, ``foo@``, bare ``@``, ...) is a 422 at
      write time rather than a silent inert allowlist at dispatch time.
    """

    model_config = ConfigDict(extra="forbid")

    mail_recipient_allowlist: str | None = None

    @field_validator("mail_recipient_allowlist")
    @classmethod
    def _validate_allowlist_grammar(cls, value: str | None) -> str | None:
        """Reject a malformed non-null allowlist string at write time.

        ``None`` (clear-to-inherit) and ``""`` (deny) are always legal. A
        non-empty string must parse under the recipient-allowlist grammar; the
        parser raises :class:`ValueError` naming the offending token, which
        Pydantic surfaces as a 422 -- so an operator learns the entry is
        unusable now instead of discovering later that the tenant silently
        cannot send. The validator fires only for a *provided* field, so an
        absent key (leave-unchanged) never reaches it.
        """
        if value is None or value == "":
            return value
        parse_recipient_allowlist(value)  # raises ValueError -> 422 on a bad entry
        return value


@router.patch("/mail-recipient-policy", response_model=TenantMailRecipientPolicy)
async def update_mail_recipient_policy(
    body: TenantMailRecipientPolicyUpdate,
    operator: Operator = _require_tenant_admin,
    session: AsyncSession = Depends(get_session),
) -> TenantMailRecipientPolicy:
    """Set or clear the caller's per-tenant mail-recipient allowlist (#3499).

    Tenant-scoped to ``operator.tenant_id`` (no cross-tenant write is
    expressible -- no tenant id is accepted in the path or body).
    ``tenant_admin`` only. Applies the field only when present in the body; an
    applied change is folded into this request's ``audit_log`` row (field / old
    / new, ``None`` -> ``inherit`` sentinel). The resolver reads the committed
    column on every send, so the new value governs the next dispatched
    ``mail.send`` in every process as soon as this request commits.
    """
    tenant = await session.get(Tenant, operator.tenant_id)
    if tenant is None:
        # Defensive depth, mirroring the flight-recorder route: the
        # ``ensure_tenant`` middleware get-or-creates the row on every
        # authenticated request, so this only fires if that invariant is ever
        # bypassed -- surface it structured rather than 500-ing on the setattr.
        raise HTTPException(status_code=404, detail="tenant_not_found")

    updates = body.model_dump(exclude_unset=True)
    if "mail_recipient_allowlist" in updates:
        set_mail_recipient_allowlist(tenant, updates["mail_recipient_allowlist"])

    return TenantMailRecipientPolicy(
        tenant_id=tenant.id,
        mail_recipient_allowlist=tenant.mail_recipient_allowlist,
    )


#: Most models one tenant can price. A tenant runs a handful of models; the
#: cap keeps the stored map small, since every run on an unknown model reads it.
MAX_MODEL_PRICES = 200

#: USD per million tokens. Far above any published price; refuses a typo.
_MAX_PRICE = Decimal(100_000)

_PriceValue = Annotated[Decimal, Field(ge=0, le=_MAX_PRICE, decimal_places=6, allow_inf_nan=False)]
_ModelId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[\w.:/@+-]+$")]


class ModelPrice(BaseModel):
    """Price of one model, in USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_million: _PriceValue
    output_per_million: _PriceValue


class ModelPriceEntry(ModelPrice):
    """One row of the tenant's model prices."""

    model: str


class TenantModelPrices(BaseModel):
    """The tenant's prices for agent models MEHO does not know (#5853)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    prices: list[ModelPriceEntry]


class TenantModelPriceUpdate(BaseModel):
    """``PATCH /api/v1/tenants/model-prices`` body: set or remove one price.

    ``model`` is the model id as an agent run shows it: ``provider:model``
    (``openai:qwen3``) or the model name alone (``qwen3``). ``price`` sets the
    price; ``null`` removes it. Both keys are required.
    """

    model_config = ConfigDict(extra="forbid")

    model: _ModelId
    price: ModelPrice | None


def _prices_response(tenant: Tenant) -> TenantModelPrices:
    prices = parse_model_prices(tenant.agent_model_prices)
    return TenantModelPrices(
        tenant_id=tenant.id,
        prices=[
            ModelPriceEntry(model=model, input_per_million=p_in, output_per_million=p_out)
            for model, (p_in, p_out) in sorted(prices.items())
        ],
    )


@router.get("/model-prices", response_model=TenantModelPrices)
async def get_model_prices(
    operator: Operator = _require_tenant_admin,
    session: AsyncSession = Depends(get_session),
) -> TenantModelPrices:
    """Read the caller's tenant prices for agent models MEHO does not know (#5853)."""
    tenant = await session.get(Tenant, operator.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    return _prices_response(tenant)


@router.patch("/model-prices", response_model=TenantModelPrices)
async def update_model_price(
    body: TenantModelPriceUpdate,
    operator: Operator = _require_tenant_admin,
    session: AsyncSession = Depends(get_session),
) -> TenantModelPrices:
    """Set or remove the caller's tenant price for one agent model (#5853).

    A run on a model that is not in MEHO's own price table uses this price
    for its cost, and the cost counts against the spend limits like any
    other run. A model MEHO already has a price for is refused (422): MEHO's
    price always wins. Tenant-scoped and ``tenant_admin`` only, like the other
    policies here. The tenant row is locked for the change, so two changes at
    once do not lose one of them.
    """
    if body.price is not None and is_model_priced(body.model):
        raise HTTPException(
            status_code=422,
            detail="MEHO already has a price for this model; it cannot be changed here.",
        )
    tenant = await session.get(
        Tenant, operator.tenant_id, with_for_update=True, populate_existing=True
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    stored = parse_model_prices(tenant.agent_model_prices)
    if body.price is not None and body.model not in stored and len(stored) >= MAX_MODEL_PRICES:
        raise HTTPException(
            status_code=422,
            detail=f"A tenant can price at most {MAX_MODEL_PRICES} models; remove one first.",
        )
    price = (
        None
        if body.price is None
        else (body.price.input_per_million, body.price.output_per_million)
    )
    set_model_price(tenant, body.model, price)
    return _prices_response(tenant)
