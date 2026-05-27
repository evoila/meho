# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Seed daily per-identity budgets for the R1 tiered-triage agents.

Initiative #807 R1 (Task #1084). The closed-loop pattern caps every
LLM-spending principal under a per-window budget so a misconfigured
schedule (every-minute cron with a noisy event feed) cannot run away
on the operator's dollar before the next operator look-in. This
script is the operator's seed surface; it writes one daily
``identity_budget`` row per principal via
:func:`meho_backplane.operations.identity_budget.set_limits`.

Run shape (substitute the real tenant id + principal subs)::

    cd backend
    uv run python ../examples/r1-tiered-triage/identity_budget_seed.py \\
        --tenant-id 11111111-1111-1111-1111-111111111111 \\
        --cheap-sub agent:r1-cheap-tier-classifier \\
        --deep-sub agent:r1-deep-tier-investigator

After seeding, the runtime's
:func:`meho_backplane.operations.budget_enforcement.evaluate_pre_run_budget`
pre-execution gate reads these rows on every invocation. When
``cost_consumed`` crosses ``AGENT_BUDGET_DEGRADE_THRESHOLD`` (default
``0.8``) of ``cost_limit``, the gate downgrades the requested tier to
``fast`` (the cheap-tier remains ``fast`` so it is a no-op for the
cheap tier; the deep tier degrades to fast, which preserves operability
under cost pressure). When ``cost_consumed >= cost_limit`` the gate
raises :class:`meho_backplane.agent.run.BudgetExceededError` and the
run is refused before any model call.

The seed values below are deliberately small (``$2 / day`` for the
cheap tier, ``$10 / day`` for the deep tier) so a real-world consumer
copying this example out has to think about the numbers rather than
land on a multi-thousand-dollar cap by accident. Tune for your
tenant's expected event throughput + your provider's published rates
(``meho_backplane.operations.identity_budget.MODEL_PRICING``).

Why a script, not a CLI verb
============================

The seed path lives in
:mod:`meho_backplane.operations.identity_budget` because the runtime
calls :func:`apply_consumption` from the same module. A future
``meho identity-budget set ...`` verb would wrap exactly this script;
in v0.2 the operator runs the script under their existing backend
toolchain (it imports the package the same way an alembic upgrade
does) so the example does not depend on an as-yet-unwritten CLI
surface.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from uuid import UUID

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import BudgetWindowKind
from meho_backplane.operations.identity_budget import set_limits


async def _seed(
    *,
    tenant_id: UUID,
    cheap_sub: str,
    deep_sub: str,
) -> None:
    """Write the daily budget row for each agent principal.

    Uses one :class:`AsyncSession` for both writes so a connection
    hiccup mid-seed either commits both rows or commits neither --
    the consumption / enforcement path is happy with one row missing
    (no row means "no cap on this dimension"), but a half-seed
    surprises an operator reading the dashboard and wondering why
    only one agent has a daily cap.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub=cheap_sub,
            window_kind=BudgetWindowKind.DAILY,
            # Two USD per day for the cheap tier. At cheap-tier model
            # rates (Claude Haiku-class, ~$0.25/$1.25 per M tokens
            # in/out), this comfortably absorbs a per-15-minute firing
            # against a moderate event feed without crossing the
            # 80%-degrade threshold mid-day. Tune for your tenant.
            cost_limit=Decimal("2.00"),
            # The cheap tier's request budget. Each scheduled firing
            # is one logical run; 200 runs/day is well above the
            # 96 runs at */15 + headroom for manual reruns.
            request_limit=200,
        )
        await set_limits(
            session,
            tenant_id=tenant_id,
            principal_sub=deep_sub,
            window_kind=BudgetWindowKind.DAILY,
            # Ten USD per day for the deep tier. At deep-tier rates
            # (Claude Sonnet-class, ~$3/$15 per M tokens in/out), this
            # comfortably absorbs ~20-40 investigations per day; the
            # degrade-threshold downgrade (deep -> fast) kicks in at
            # 80% consumed, which is the operator's signal to either
            # raise the cap or tighten the cheap tier's escalation
            # rules so fewer events reach the deep tier.
            cost_limit=Decimal("10.00"),
            request_limit=100,
        )
        await session.commit()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments. Kept in its own function so the test
    can construct an args namespace directly without round-tripping
    through ``sys.argv``."""
    parser = argparse.ArgumentParser(
        description="Seed daily identity_budget rows for the R1 tiered-triage agents.",
    )
    parser.add_argument("--tenant-id", required=True, type=UUID, help="Tenant UUID.")
    parser.add_argument(
        "--cheap-sub",
        required=True,
        help='OIDC sub for the cheap-tier principal (e.g. "agent:r1-cheap-tier-classifier").',
    )
    parser.add_argument(
        "--deep-sub",
        required=True,
        help='OIDC sub for the deep-tier principal (e.g. "agent:r1-deep-tier-investigator").',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Script entrypoint. Returns a Unix exit code."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    asyncio.run(
        _seed(
            tenant_id=args.tenant_id,
            cheap_sub=args.cheap_sub,
            deep_sub=args.deep_sub,
        ),
    )
    print(
        f"seeded daily budgets: cheap={args.cheap_sub} (cost_limit=$2.00, requests=200), "
        f"deep={args.deep_sub} (cost_limit=$10.00, requests=100)",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover -- script entrypoint
    raise SystemExit(main())
