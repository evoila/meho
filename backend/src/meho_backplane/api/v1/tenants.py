# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator mutation surface for the per-tenant flight-recorder policy (#3272).

The flight-recorder decision (``docs/decisions/dispatch-flight-recorder.md``,
F1) models capture enablement as an operator action -- "a lab-class tenant is
one an operator flips ON". The policy columns shipped (#3212/#3216) and the
resolver (:mod:`meho_backplane.flight_recorder.config`) reads them per dispatch,
but there was **no writable path**: no ``/api/v1/tenants`` CRUD, so capture
could not be enabled on a deployment without direct DB writes (which the
governance model forbids). This route closes that gap for the three per-tenant
policy fields; the per-target tri-state override
(``targets.flight_recorder_capture``) rides the existing
``PATCH /api/v1/targets/{name}`` route (see :mod:`meho_backplane.api.v1.targets`).

Surface posture (postulate 5): this is an **operator-plane** route -- REST +
CLI (``meho tenants flight-recorder-policy set``) only. It is deliberately
**not** on the 25-tool agent working surface and has **no MCP tool** at all; a
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

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder.config import invalidate_tenant_policy_cache

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])

#: Module-level :class:`Depends` closure -- built once at import time to
#: satisfy ruff's B008 rule. Capture policy is governance config, gated at the
#: admin tier (same as the target PATCH and tenant-convention writes).
_require_tenant_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Audit-payload sentinel for the tri-state fields' ``inherit`` (NULL) state.
#: :func:`~meho_backplane.audit._resolve_audit_payload` drops ``None``
#: contextvars, so a ``True``/``False`` -> ``NULL`` transition would silently
#: lose its "after" value; the sentinel keeps the transition legible in the
#: ``audit_log`` payload. Mirrors the ``""`` unpinned marker the target CA-pin
#: audit uses for the same reason.
_INHERIT_SENTINEL = "inherit"


def _audit_value(value: object) -> object:
    """Render a policy field value for the audit payload (``None`` -> sentinel).

    Booleans and ints pass through as-is (queryable); ``None`` -- the tri-state
    ``inherit`` / retention-default state -- becomes :data:`_INHERIT_SENTINEL`
    so the change does not vanish from the ``audit_log`` payload (which drops
    ``None`` contextvars).
    """
    return _INHERIT_SENTINEL if value is None else value


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

    updates = body.model_dump(exclude_unset=True)
    changed_fields: list[str] = []
    for field, new_value in updates.items():
        old_value = getattr(tenant, field)
        if old_value == new_value:
            continue
        setattr(tenant, field, new_value)
        # Fold field / old / new into the audit payload (never-silent
        # governance mutation). ``None`` -> sentinel so a clear-to-inherit
        # transition is not dropped by the payload's None-stripping.
        # The field name already carries the ``flight_recorder_`` prefix, so
        # the payload keys read ``flight_recorder_enabled_before`` etc. after
        # the middleware strips the ``audit_`` prefix.
        structlog.contextvars.bind_contextvars(
            **{
                f"audit_{field}_before": _audit_value(old_value),
                f"audit_{field}_after": _audit_value(new_value),
            }
        )
        changed_fields.append(field)

    if changed_fields:
        structlog.contextvars.bind_contextvars(
            audit_flight_recorder_policy_changed=True,
            audit_tenant_id=str(operator.tenant_id),
        )
        # Evict the resolver's per-tenant cache so the flip takes effect on the
        # next dispatch. The dependency commits after this handler returns; the
        # next resolver read (a fresh session, post-request) misses the cache
        # and reads the committed value -- proven by the without-restart test.
        invalidate_tenant_policy_cache(operator.tenant_id)
        _log.info(
            "tenant_flight_recorder_policy_updated",
            tenant_id=str(operator.tenant_id),
            fields=changed_fields,
        )

    return TenantFlightRecorderPolicy(
        tenant_id=tenant.id,
        flight_recorder_enabled=tenant.flight_recorder_enabled,
        flight_recorder_agent_readable=tenant.flight_recorder_agent_readable,
        flight_recorder_retention_days=tenant.flight_recorder_retention_days,
    )
