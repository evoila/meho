# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/agents/budgets`` — set, show and clear the spend caps of an agent login.

MEHO checks the caps before every hosted agent run
(:mod:`meho_backplane.operations.budget_enforcement`). This module is the
way to set them. A cap has up to three limits (tokens, cost in USD,
requests) for one window (daily, weekly or monthly). A limit that is not
set means no cap on that value.

Routes
------

* ``GET /api/v1/agents/budgets?principal_sub=<sub>`` — the caps of the
  principal and what is used so far in the current day, week and month.
* ``PUT /api/v1/agents/budgets`` — set one or more limits of one window.
  A limit left out stays as it is.
* ``DELETE /api/v1/agents/budgets?principal_sub=<sub>&window=<window>`` —
  remove all limits of one window.

Only ``tenant_admin`` may call them, the same as for agent grants. Set and
clear are also for people only: an agent login cannot change its own cap.
Every route is tenant-scoped to the caller's token, and every call writes
an audit row (the audit middleware, from ``audit_op_id``).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_human_principal, require_role
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BudgetWindowKind
from meho_backplane.operations.identity_budget import (
    BudgetReading,
    clear_limits,
    get_remaining,
    set_limits,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/agents/budgets", tags=["agent-budgets"])

_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))
_require_human = Depends(require_human_principal)

_BUDGET_OP_IDS: Final[dict[str, str]] = {
    "show": "agent.budget.show",
    "set": "agent.budget.set",
    "clear": "agent.budget.clear",
}


class AgentBudgetRead(BaseModel):
    """The caps of one window and what is used so far in it.

    A ``null`` limit means no cap on that value. ``*_remaining`` is
    ``null`` when the limit is ``null``, and can be negative when a run
    went over the cap. Costs are in USD.
    """

    model_config = ConfigDict(extra="forbid")

    principal_sub: str
    window: BudgetWindowKind
    window_start: datetime
    window_end: datetime
    token_limit: int | None
    tokens_consumed: int
    tokens_remaining: int | None
    cost_limit: float | None
    cost_consumed: float
    cost_remaining: float | None
    request_limit: int | None
    requests_consumed: int
    requests_remaining: int | None


class AgentBudgetListResponse(BaseModel):
    """The caps of one principal, one entry for each window."""

    model_config = ConfigDict(extra="forbid")

    budgets: list[AgentBudgetRead]


class AgentBudgetSet(BaseModel):
    """Body of ``PUT /api/v1/agents/budgets``."""

    model_config = ConfigDict(extra="forbid")

    principal_sub: str = Field(
        min_length=1, description="Login of the agent, e.g. agent:pod-reader"
    )
    window: BudgetWindowKind
    token_limit: int | None = Field(default=None, ge=0)
    cost_limit: float | None = Field(default=None, ge=0, description="Cost cap in USD")
    request_limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one_limit(self) -> AgentBudgetSet:
        if self.token_limit is None and self.cost_limit is None and self.request_limit is None:
            raise ValueError("set at least one of token_limit, cost_limit, request_limit")
        return self


def _opt_int(value: Decimal | None) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _to_read(reading: BudgetReading) -> AgentBudgetRead:
    return AgentBudgetRead(
        principal_sub=reading.principal_sub,
        window=reading.window_kind,
        window_start=reading.window_start,
        window_end=reading.window_end,
        token_limit=_opt_int(reading.token_limit),
        tokens_consumed=int(reading.tokens_consumed),
        tokens_remaining=_opt_int(reading.tokens_remaining),
        cost_limit=_opt_float(reading.cost_limit),
        cost_consumed=float(reading.cost_consumed),
        cost_remaining=_opt_float(reading.cost_remaining),
        request_limit=reading.request_limit,
        requests_consumed=reading.requests_consumed,
        requests_remaining=reading.requests_remaining,
    )


async def _read_window(
    operator: Operator, principal_sub: str, window: BudgetWindowKind
) -> AgentBudgetRead:
    async with get_sessionmaker()() as session:
        reading = await get_remaining(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=principal_sub,
            window_kind=window,
        )
    return _to_read(reading)


@router.get("", response_model=AgentBudgetListResponse)
async def show_budgets(
    principal_sub: str = Query(min_length=1),
    window: BudgetWindowKind | None = Query(default=None),
    operator: Operator = _require_admin,
) -> AgentBudgetListResponse:
    """Show the caps and the use so far of one principal (all windows, or one)."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_BUDGET_OP_IDS["show"],
        audit_op_class="read",
        audit_agent_name=principal_sub,
    )
    windows = [window] if window is not None else list(BudgetWindowKind)
    return AgentBudgetListResponse(
        budgets=[await _read_window(operator, principal_sub, w) for w in windows]
    )


@router.put("", response_model=AgentBudgetRead, dependencies=[_require_human])
async def set_budget(
    payload: AgentBudgetSet,
    operator: Operator = _require_admin,
) -> AgentBudgetRead:
    """Set the limits of one window. A limit that is left out stays as it is."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_BUDGET_OP_IDS["set"],
        audit_op_class="write",
        audit_agent_name=payload.principal_sub,
    )
    async with get_sessionmaker()() as session, session.begin():
        await set_limits(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=payload.principal_sub,
            window_kind=payload.window,
            token_limit=payload.token_limit,
            cost_limit=None if payload.cost_limit is None else Decimal(str(payload.cost_limit)),
            request_limit=payload.request_limit,
        )
    return await _read_window(operator, payload.principal_sub, payload.window)


@router.delete("", response_model=AgentBudgetRead, dependencies=[_require_human])
async def clear_budget(
    principal_sub: str = Query(min_length=1),
    window: BudgetWindowKind = Query(),
    operator: Operator = _require_admin,
) -> AgentBudgetRead:
    """Remove all limits of one window. What is used so far is kept."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_BUDGET_OP_IDS["clear"],
        audit_op_class="write",
        audit_agent_name=principal_sub,
    )
    async with get_sessionmaker()() as session, session.begin():
        await clear_limits(
            session,
            tenant_id=operator.tenant_id,
            principal_sub=principal_sub,
            window_kind=window,
        )
    return await _read_window(operator, principal_sub, window)
