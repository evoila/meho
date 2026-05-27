# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-identity budget bucketing + per-op cost source (G11.5-T5 / C3-a).

Initiative #806 (G11.5 Portability + cost), Task #1079. The module is the
behaviour pair of the :class:`~meho_backplane.db.models.IdentityBudget`
ORM row added by migration ``0031``:

* **Per-op cost source.** :func:`compute_cost` turns a finished agent
  run's :class:`pydantic_ai.usage.RunUsage` plus a resolved model id
  into a :class:`~decimal.Decimal` USD cost using a pinned per-model
  pricing table.
* **Per-principal budget bucketing.** :func:`apply_consumption` upserts
  one :class:`IdentityBudget` row per active window-kind (daily /
  weekly / monthly) for the principal and increments the consumption
  counters atomically.
* **Remaining-budget query.** :func:`get_remaining` returns the gap
  between limits and consumption for a given (principal, window_kind),
  the read-side primitive both an operator dashboard and the C3-b
  enforcement gate (#1080) call.

Enforcement and degradation policy (the *"refuse at the cap, downgrade
at the threshold"* behaviour from Initiative #806) live in C3-b. This
module is intentionally **observational only** -- it records, it does
not block. A run that would push consumption past a limit is recorded
faithfully here; the gate that prevents that run from starting is
G11.5-T6 (#1080).

Why the pricing table lives in code, not the DB
-----------------------------------------------

Per-1M-token rates are published-by-provider quantities that change
on the provider's calendar, not MEHO's. Pinning them in a
:data:`MODEL_PRICING` constant lets a PR bump rates alongside the
model-id rotation and lands them through code review (the same path
that adds new models to the resolver in G11.5-C4). A DB-row pricing
table would invite drift between published rates and the live row,
plus add a join to every cost computation. Once C4 lands multi-
provider routing the constant grows -- one entry per
``(provider, model_id)`` -- without changing the API surface.

Why window-start truncation lives here, not in the DB
-----------------------------------------------------

The Alembic migration ``0031`` does not stamp ``window_start`` from a
trigger / generated column; the consumption service is the single
writer that truncates ``now`` to the bucket boundary
(:func:`window_start_for`). Doing the truncation in code (a) keeps the
migration portable across SQLite + PG without resorting to dialect-
specific generated columns, and (b) makes the truncation rule
trivially testable in :mod:`tests.test_identity_budget_service`
without spinning up the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import BudgetWindowKind, IdentityBudget

if TYPE_CHECKING:  # pragma: no cover - import only for type-checking
    from pydantic_ai.usage import RunUsage

__all__ = [
    "MODEL_PRICING",
    "BudgetReading",
    "PerMillionPricing",
    "TokenUsage",
    "apply_consumption",
    "compute_cost",
    "get_remaining",
    "set_limits",
    "window_start_for",
]


_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PerMillionPricing:
    """Per-1M-token published rates for one model id.

    Values are USD per 1,000,000 tokens for each token stream the
    provider charges separately on. Fields default to 0 when a stream
    is not billed by the provider (e.g. providers that do not separate
    cache-read pricing simply set ``cache_read=0`` -- those tokens are
    already counted in :attr:`input`).

    Decimal-typed -- pricing arithmetic must not introduce float
    rounding error when summed across the rate-times-tokens products.
    """

    input: Decimal
    output: Decimal
    cache_read: Decimal = Decimal(0)
    cache_write: Decimal = Decimal(0)


#: Per-model published pricing as of 2026-05-27 (Anthropic).
#:
#: The keys are the resolved provider-prefixed model ids the runtime
#: emits via :attr:`AgentRunAuditMeta.model` (e.g. the
#: ``anthropic:claude-sonnet-4-6`` shape ``settings.agent_default_model``
#: defaults to). The runtime calls
#: :func:`compute_cost` with the *resolved* id, not the operator-
#: facing logical tier, so this map keys directly on what the
#: provider was actually billed for.
#:
#: A model id not present in the map yields :attr:`PerMillionPricing.input`
#: =0 / :attr:`output` =0, i.e. cost reported as 0 (a *known-unknown*
#: rather than an error). The runtime logs a single ``warning`` on the
#: first lookup miss per process so an operator notices the gap
#: without breaking the run.
MODEL_PRICING: Final[dict[str, PerMillionPricing]] = {
    # Anthropic Claude Sonnet 4.x family.
    "anthropic:claude-sonnet-4-6": PerMillionPricing(
        input=Decimal("3.00"),
        output=Decimal("15.00"),
        cache_read=Decimal("0.30"),
        cache_write=Decimal("3.75"),
    ),
    "anthropic:claude-sonnet-4-5": PerMillionPricing(
        input=Decimal("3.00"),
        output=Decimal("15.00"),
        cache_read=Decimal("0.30"),
        cache_write=Decimal("3.75"),
    ),
    # Anthropic Claude Opus 4.x family.
    "anthropic:claude-opus-4-7": PerMillionPricing(
        input=Decimal("15.00"),
        output=Decimal("75.00"),
        cache_read=Decimal("1.50"),
        cache_write=Decimal("18.75"),
    ),
    "anthropic:claude-opus-4-6": PerMillionPricing(
        input=Decimal("15.00"),
        output=Decimal("75.00"),
        cache_read=Decimal("1.50"),
        cache_write=Decimal("18.75"),
    ),
    # Anthropic Claude Haiku 4 family.
    "anthropic:claude-haiku-4-5": PerMillionPricing(
        input=Decimal("0.80"),
        output=Decimal("4.00"),
        cache_read=Decimal("0.08"),
        cache_write=Decimal("1.00"),
    ),
}


_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
_ALL_WINDOW_KINDS: Final[tuple[BudgetWindowKind, ...]] = (
    BudgetWindowKind.DAILY,
    BudgetWindowKind.WEEKLY,
    BudgetWindowKind.MONTHLY,
)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The four token-stream totals one finished run produced.

    Lifted from :class:`pydantic_ai.usage.RunUsage` at the runtime
    boundary so the consumption service does not import the framework
    type. ``cache_read_tokens`` and ``cache_write_tokens`` are the
    Anthropic prompt-cache shapes; providers that do not separate
    them simply pass ``0``.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_run_usage(cls, usage: RunUsage) -> TokenUsage:
        """Build a :class:`TokenUsage` from a pydantic_ai ``RunUsage``.

        The framework's :class:`~pydantic_ai.usage.RunUsage` carries
        many fields; only the four token-stream counters factor into
        cost. Audio token streams (which the framework also tracks)
        are out of scope for the text-only agent runtime in v1.
        """
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    @property
    def total_tokens(self) -> int:
        """Sum across all four streams.

        Used as the ``tokens`` consumption increment when the budget's
        token limit is a single number (the common case). Enforcement
        (C3-b, #1080) may evaluate per-stream limits later; the model
        already carries enough columns to express that without a
        migration.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class BudgetReading:
    """One window-bucket's limits + consumption + remaining-headroom view.

    Returned by :func:`get_remaining`. Numeric fields use
    :class:`~decimal.Decimal` for the same precision reason the ORM
    columns do. A ``None`` ``*_remaining`` value means the
    corresponding limit was unset (``NULL`` in the DB), i.e. the
    bucket has no cap on that dimension.
    """

    tenant_id: UUID
    principal_sub: str
    window_kind: BudgetWindowKind
    window_start: datetime
    window_end: datetime
    token_limit: Decimal | None
    tokens_consumed: Decimal
    tokens_remaining: Decimal | None
    cost_limit: Decimal | None
    cost_consumed: Decimal
    cost_remaining: Decimal | None
    request_limit: int | None
    requests_consumed: int
    requests_remaining: int | None


def window_start_for(kind: BudgetWindowKind, when: datetime) -> datetime:
    """Truncate *when* to the inclusive start of its *kind* bucket.

    Returns a UTC-naive-on-display but UTC-aware (``tzinfo=UTC``)
    datetime so PG's ``timestamptz`` storage round-trips without
    timezone surprises.

    * :attr:`BudgetWindowKind.DAILY` — 00:00 UTC of the same calendar
      day.
    * :attr:`BudgetWindowKind.WEEKLY` — 00:00 UTC of the Monday of the
      same ISO week. ``isoweekday() == 1`` is Monday.
    * :attr:`BudgetWindowKind.MONTHLY` — 00:00 UTC of the 1st of the
      same calendar month.

    Raises:
        ValueError: if *when* is naive (no tzinfo).
    """
    if when.tzinfo is None:
        raise ValueError("window_start_for requires an aware datetime")
    when_utc = when.astimezone(UTC)
    if kind is BudgetWindowKind.DAILY:
        return when_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind is BudgetWindowKind.WEEKLY:
        midnight = when_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        # isoweekday(): Monday=1 ... Sunday=7. Subtract (weekday - 1)
        # days to land on Monday.
        return midnight - timedelta(days=midnight.isoweekday() - 1)
    # MONTHLY
    return when_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _window_end_for(kind: BudgetWindowKind, start: datetime) -> datetime:
    """Compute the exclusive upper bound for a *kind* window that begins at *start*.

    *start* must already be on the bucket boundary (the value
    :func:`window_start_for` returns); the function performs no
    validation.
    """
    if kind is BudgetWindowKind.DAILY:
        return start + timedelta(days=1)
    if kind is BudgetWindowKind.WEEKLY:
        return start + timedelta(days=7)
    # MONTHLY: next 1st of the month at 00:00 UTC. Adding 32 days
    # always lands somewhere in the next calendar month; replacing
    # ``day=1`` collapses to the next month's first.
    return (start + timedelta(days=32)).replace(day=1)


_PRICING_MISS_LOGGED: set[str] = set()


def compute_cost(usage: TokenUsage, model_id: str | None) -> Decimal:
    """Convert a run's :class:`TokenUsage` into a USD cost.

    Looks *model_id* up in :data:`MODEL_PRICING`; on miss the function
    returns ``Decimal(0)`` (cost reported as 0) and emits a single
    ``warning`` per (process, model_id) so an operator notices the
    pricing gap without breaking the run. The gate that prevents a
    run from *starting* against an un-priced model is out of scope
    here — that is part of the multi-provider resolver landing in
    G11.5-C4.

    Args:
        usage: Token totals lifted from the run's ``RunUsage``.
        model_id: The resolved provider-prefixed id the runtime
            stamped on the agent run row's ``model`` column. ``None``
            falls through to the unknown-model path.

    Returns:
        The total USD cost as a :class:`~decimal.Decimal`,
        quantized to six fractional places by the caller's
        ``Numeric(14, 6)`` storage; arithmetic precision is
        preserved here.
    """
    if model_id is None:
        return Decimal(0)
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        if model_id not in _PRICING_MISS_LOGGED:
            _PRICING_MISS_LOGGED.add(model_id)
            _log.warning(
                "identity_budget_pricing_miss",
                model_id=model_id,
                reason="no entry in MODEL_PRICING; cost recorded as 0",
            )
        return Decimal(0)
    return (
        pricing.input * Decimal(usage.input_tokens)
        + pricing.output * Decimal(usage.output_tokens)
        + pricing.cache_read * Decimal(usage.cache_read_tokens)
        + pricing.cache_write * Decimal(usage.cache_write_tokens)
    ) / _PER_MILLION


async def _get_or_create_bucket(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_sub: str,
    window_kind: BudgetWindowKind,
    now: datetime,
) -> IdentityBudget:
    """Find the active bucket for (tenant, principal, kind) — create on miss.

    Uses a key-tuple SELECT, then a Python-side INSERT on miss. The
    composite UNIQUE constraint (``uq_identity_budget_window``)
    serves as the upsert race guard: a concurrent INSERT from a
    second writer surfaces as :class:`IntegrityError` on flush, and
    the caller's retry path (the runtime catches and re-issues this
    helper) lands on the now-existing row.

    The bucket is created with zero consumption + NULL limits. A
    seeding helper (:func:`set_limits`) is the only path that
    populates ``token_limit`` / ``cost_limit`` / ``request_limit``
    — limits are an out-of-band operator decision, not a runtime
    side effect.
    """
    window_start = window_start_for(window_kind, now)
    window_end = _window_end_for(window_kind, window_start)
    stmt = select(IdentityBudget).where(
        IdentityBudget.tenant_id == tenant_id,
        IdentityBudget.principal_sub == principal_sub,
        IdentityBudget.window_kind == window_kind.value,
        IdentityBudget.window_start == window_start,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    row = IdentityBudget(
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        window_kind=window_kind.value,
        window_start=window_start,
        window_end=window_end,
    )
    session.add(row)
    await session.flush()
    return row


async def apply_consumption(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_sub: str,
    tokens: int,
    cost: Decimal,
    requests: int = 1,
    when: datetime | None = None,
) -> tuple[IdentityBudget, ...]:
    """Increment the three active budget buckets for *principal_sub* by one run.

    Touches one row per :class:`BudgetWindowKind` (daily / weekly /
    monthly). All three are incremented atomically inside the
    caller's open transaction; the caller commits.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        tenant_id: The tenant whose budget bucket is being charged.
        principal_sub: The JWT ``sub`` of the principal whose
            consumption is being recorded.
        tokens: Sum across input + output + cache-read + cache-write
            streams from the run's :class:`TokenUsage`. Stored as the
            ``tokens_consumed`` increment.
        cost: USD cost of the run, as returned by :func:`compute_cost`.
            Stored as the ``cost_consumed`` increment.
        requests: Run count (almost always 1; the parameter exists for
            future batch / replay paths that fold N synthetic runs
            into one consumption row).
        when: Override the bucket-resolution clock (testing /
            backfill). Defaults to ``datetime.now(UTC)``.

    Returns:
        Tuple of the three updated rows in
        ``(daily, weekly, monthly)`` order. The runtime ignores the
        return value; the dashboard / enforcement read of the
        post-increment state uses :func:`get_remaining`.
    """
    now = when if when is not None else datetime.now(UTC)
    updated: list[IdentityBudget] = []
    cost_decimal = cost if isinstance(cost, Decimal) else Decimal(str(cost))
    tokens_decimal = Decimal(tokens)
    for kind in _ALL_WINDOW_KINDS:
        bucket = await _get_or_create_bucket(
            session,
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            window_kind=kind,
            now=now,
        )
        bucket.tokens_consumed = bucket.tokens_consumed + tokens_decimal
        bucket.cost_consumed = bucket.cost_consumed + cost_decimal
        bucket.requests_consumed = bucket.requests_consumed + requests
        bucket.updated_at = now
        updated.append(bucket)
    await session.flush()
    return tuple(updated)


async def set_limits(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_sub: str,
    window_kind: BudgetWindowKind,
    token_limit: int | Decimal | None = None,
    cost_limit: Decimal | None = None,
    request_limit: int | None = None,
    when: datetime | None = None,
) -> IdentityBudget:
    """Set / replace the budget limits on a (principal, window_kind) bucket.

    Creates the bucket if it does not exist. ``None`` for a given
    limit field means "do not change it"; passing
    :attr:`Decimal(0)` / ``0`` for ``token_limit`` / ``request_limit``
    or ``Decimal("0")`` for ``cost_limit`` *does* set a zero cap (a
    valid configuration that means *"this principal cannot use this
    dimension at all"*).

    This helper is an operator / seed surface; the runtime never
    calls it. The runtime's only mutation is :func:`apply_consumption`.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        tenant_id: The tenant the bucket belongs to.
        principal_sub: The principal sub the bucket is for.
        window_kind: The window-kind the bucket belongs to.
        token_limit: New token cap, or ``None`` to keep existing.
        cost_limit: New USD cost cap, or ``None`` to keep existing.
        request_limit: New request-count cap, or ``None`` to keep
            existing.
        when: Override the bucket-resolution clock.

    Returns:
        The mutated, flushed bucket row.
    """
    now = when if when is not None else datetime.now(UTC)
    bucket = await _get_or_create_bucket(
        session,
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        window_kind=window_kind,
        now=now,
    )
    if token_limit is not None:
        bucket.token_limit = (
            token_limit if isinstance(token_limit, Decimal) else Decimal(token_limit)
        )
    if cost_limit is not None:
        bucket.cost_limit = cost_limit
    if request_limit is not None:
        bucket.request_limit = request_limit
    bucket.updated_at = now
    await session.flush()
    return bucket


async def get_remaining(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    principal_sub: str,
    window_kind: BudgetWindowKind,
    when: datetime | None = None,
) -> BudgetReading:
    """Return the limits / consumption / remaining-headroom view for one bucket.

    Returns a synthetic empty :class:`BudgetReading` (zero
    consumption, no limits) when no row exists for the
    (principal, window_kind, period) — *"no budget configured"* is a
    distinct read from *"budget configured but unspent"*, but both
    are honest answers and the enforcement gate (C3-b) is the only
    consumer that needs to distinguish them.

    Args:
        session: Open :class:`AsyncSession`.
        tenant_id: The tenant the bucket belongs to.
        principal_sub: The principal sub the bucket is for.
        window_kind: The window-kind to read.
        when: Override the bucket-resolution clock.

    Returns:
        A :class:`BudgetReading` whose ``*_remaining`` fields are
        ``None`` when the corresponding limit is ``None``.
    """
    now = when if when is not None else datetime.now(UTC)
    window_start = window_start_for(window_kind, now)
    window_end = _window_end_for(window_kind, window_start)
    stmt = select(IdentityBudget).where(
        IdentityBudget.tenant_id == tenant_id,
        IdentityBudget.principal_sub == principal_sub,
        IdentityBudget.window_kind == window_kind.value,
        IdentityBudget.window_start == window_start,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return BudgetReading(
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            window_kind=window_kind,
            window_start=window_start,
            window_end=window_end,
            token_limit=None,
            tokens_consumed=Decimal(0),
            tokens_remaining=None,
            cost_limit=None,
            cost_consumed=Decimal(0),
            cost_remaining=None,
            request_limit=None,
            requests_consumed=0,
            requests_remaining=None,
        )
    tokens_remaining: Decimal | None = (
        row.token_limit - row.tokens_consumed if row.token_limit is not None else None
    )
    cost_remaining: Decimal | None = (
        row.cost_limit - row.cost_consumed if row.cost_limit is not None else None
    )
    requests_remaining: int | None = (
        row.request_limit - row.requests_consumed if row.request_limit is not None else None
    )
    return BudgetReading(
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        window_kind=window_kind,
        window_start=row.window_start,
        window_end=row.window_end,
        token_limit=row.token_limit,
        tokens_consumed=row.tokens_consumed,
        tokens_remaining=tokens_remaining,
        cost_limit=row.cost_limit,
        cost_consumed=row.cost_consumed,
        cost_remaining=cost_remaining,
        request_limit=row.request_limit,
        requests_consumed=row.requests_consumed,
        requests_remaining=requests_remaining,
    )
