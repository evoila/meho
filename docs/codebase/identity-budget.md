# identity-budget — per-identity token / cost / request budgets, per-window

## Overview

`identity_budget` is the data substrate for per-principal LLM consumption
caps. Initiative #806 (G11.5 Portability + cost) attaches budgets to
**any** MEHO principal — human user, service account, or agent — keyed
on a `(tenant, principal, window-kind, window-start)` tuple. Task #1079
(G11.5-T5 / C3-a) ships:

- The **table** (`identity_budget`, migration `0031`) and its ORM model
  (`IdentityBudget`) with optional limits + NOT-NULL consumption.
- A **per-op cost source**: a pricing table keyed on resolved
  `provider:model_id` (`MODEL_PRICING`) and a `compute_cost(usage,
  model_id) -> Decimal` function over a finished run's `RunUsage`.
- A **consumption service** (`meho_backplane.operations.identity_budget`)
  exposing `apply_consumption`, `set_limits`, `get_remaining`, plus the
  window-truncation helper `window_start_for`.
- **Runtime wiring**: `_finalize_run` in `agent/invocation.py` computes
  cost on every successful run, stamps it on `agent_run.cost`, and
  increments the three active budget buckets (daily + weekly + monthly)
  atomically in the same transaction as the `succeed_run` transition.

Enforcement (the *"refuse at the cap, downgrade at the threshold"*
half) is Task #1080 (G11.5-T6 / C3-b) — see "Enforcement" below.
This module is intentionally **observational only** — it records, it
does not block; `budget_enforcement.py` is the decider companion that
reads the same rows.

## Key types

| Type | Where | Role |
|---|---|---|
| `IdentityBudget` (ORM) | `db/models.py` | The durable bucket row |
| `BudgetWindowKind` | `db/models.py` | Closed enum (`daily` / `weekly` / `monthly`) backed by a DB-level CHECK |
| `PerMillionPricing` | `operations/identity_budget.py` | Per-1M-token published rates for one model |
| `MODEL_PRICING` | `operations/identity_budget.py` | The pinned `provider:model -> PerMillionPricing` map |
| `TokenUsage` | `operations/identity_budget.py` | Framework-free token-stream record (input / output / cache-read / cache-write) |
| `BudgetReading` | `operations/identity_budget.py` | Read-side view: limits + consumption + remaining-headroom |

## Control flow

```
                 ┌─────────────────────────────┐
agent_run loop ─►│ AgentRun.start / .result    │
                 │  (PydanticAgentRun)         │
                 └──────────────┬──────────────┘
                                │ RunUsage(input, output,
                                │   cache_read, cache_write,
                                │   requests, tool_calls)
                                ▼
                  ┌──────────────────────────────┐
                  │ AgentRunResult (extended)    │
                  │  • request_count             │
                  │  • tool_call_count           │
                  │  • input_tokens              │
                  │  • output_tokens             │
                  │  • cache_read_tokens         │
                  │  • cache_write_tokens        │
                  └──────────────┬───────────────┘
                                 │ TokenUsage
                                 ▼
            ┌─────────────────────────────────────────┐
            │ AgentInvoker._finalize_run              │
            │  (success path)                         │
            │                                         │
            │  1. provider:model = _full_model_id(.)  │
            │  2. cost = compute_cost(usage, full_id) │
            │  3. succeed_run(session, row, cost=...) │
            │  4. apply_consumption(session, ...)     │
            │  5. commit                              │
            └─────────────────────────────────────────┘
                                 │
                                 ▼
               ┌─────────────────────────────────────┐
               │ identity_budget rows (per window)    │
               │  • daily  (00:00 UTC of today)       │
               │  • weekly (Monday 00:00 UTC of week) │
               │  • monthly (1st 00:00 UTC of month)  │
               └─────────────────────────────────────┘
```

A **failed** run skips steps 1-4 entirely; `agent_run.cost` stays NULL
and no budget rows materialise. Future enforcement may opt to charge
best-effort partial usage on failure, but the v0.2 contract is
"failed runs cost nothing".

## The pricing table

`MODEL_PRICING` is a `Final[dict[str, PerMillionPricing]]` pinned at
import time. The keys are the **resolved** provider-prefixed ids the
runtime emits via `AgentRunAuditMeta.model` (e.g.
`anthropic:claude-sonnet-4-6`). The runtime calls `compute_cost(usage,
model_id)` with the *resolved* id, not the operator-facing logical
tier, so the table keys directly on what the provider was actually
billed for.

Why in code and not the DB:

- Per-1M-token rates are published-by-provider quantities. The cadence
  is the provider's calendar, not MEHO's.
- A code-resident table lands rate bumps through code review, the same
  path that adds new models to the resolver in G11.5-C4.
- A DB-row pricing table invites drift between published rates and
  the live row, plus adds a join to every cost computation.

An unknown model id (lookup miss) yields `Decimal(0)` and logs a
single `warning` per process — the "known unknown" contract. The gate
that prevents a run from *starting* against an un-priced model lives
in the multi-provider resolver (G11.5-C4).

## Window truncation

`window_start_for(kind, when)` truncates a UTC-aware datetime to the
inclusive lower bound of its bucket:

- **Daily** — 00:00 UTC of the same calendar day.
- **Weekly** — 00:00 UTC of the Monday of the same ISO week
  (`isoweekday() == 1`).
- **Monthly** — 00:00 UTC of the 1st of the same calendar month.

The window end is computed by `_window_end_for` (`+1 day` / `+7 days`
/ next-month-1st) and persisted on the row so audit reads do not have
to re-derive the boundary.

Truncation lives **in code, not in the DB**, so:

- The Alembic migration stays portable across SQLite + PG without
  dialect-specific generated columns.
- The truncation rule is unit-testable without spinning up the DB
  (`test_identity_budget_service.py::test_window_start_*`).

## Upserts and the unique-key contract

Each bucket is uniquely identified by
`(tenant_id, principal_sub, window_kind, window_start)` — enforced
both at the DB layer (`uq_identity_budget_window` UNIQUE constraint)
and by the consumption service's
`_get_or_create_bucket`. The composite UNIQUE serves as the upsert
race guard: a concurrent INSERT from a second writer surfaces as
`IntegrityError` on flush, and the caller's retry path lands on the
now-existing row.

## Schema column rationale

| Column | Type | Nullable | Why |
|---|---|---|---|
| `id` | UUID | NO | Stable handle (no cross-table refs in v0.2) |
| `tenant_id` | UUID FK → `tenant(id)` | NO | Real FK, brand-new clean-slate table |
| `principal_sub` | Text | NO | JWT `sub`; soft-FK (could be human / service / agent) |
| `window_kind` | Text + CHECK | NO | Closed `BudgetWindowKind` vocabulary |
| `window_start` | timestamptz | NO | Inclusive lower bound; truncated to boundary |
| `window_end` | timestamptz | NO | Exclusive upper bound (computed) |
| `token_limit` | Numeric(20,0) | YES | NULL = no token cap |
| `cost_limit` | Numeric(14,6) | YES | NULL = no cost cap; money-shape precision |
| `request_limit` | Integer | YES | NULL = no request cap |
| `tokens_consumed` | Numeric(20,0) | NO | Default 0; wider than Integer for hot-cache cases |
| `cost_consumed` | Numeric(14,6) | NO | Default 0; money-shape precision |
| `requests_consumed` | Integer | NO | Default 0 |
| `created_at` | timestamptz | NO | PG `now()`; ORM fallback for SQLite |
| `updated_at` | timestamptz | NO | Bumped by service on every consumption / set_limits call |

## Indexes

- `uq_identity_budget_window` (UNIQUE) — drives upsert + serves as race guard.
- `ck_identity_budget_window_kind` (CHECK) — closes the window-kind vocabulary at the DB layer.
- `identity_budget_tenant_principal_idx` (b-tree) — drives the dominant query: *"find the active buckets for this principal in this tenant"*.

The `(window_kind)` filter on top of the b-tree is cheap in-memory because the row count per principal is bounded by **three buckets per period** (one each for daily / weekly / monthly).

## Dependencies

- **Upstream:** `agent_run` table (#813), `agent_principal` table (#815),
  `tenant` table (G0.1). All present on `main`.
- **Downstream consumer (sibling):** G11.5-T6 / C3-b (#1080) — landed
  on `main`. See "Enforcement" below.

## Enforcement (G11.5-T6 #1080)

`budget_enforcement.py` is the **decider** companion to this
**recorder** module. Per Initiative #806's *"at 80% drop to a cheaper
tier; at 100% refuse"* DoD, it adds a single
`evaluate_pre_run_budget` call at the agent-run dispatch seam
(`AgentInvoker.run` / `AgentInvoker.run_scheduled` /
`AgentInvoker.stream_events`, plus `ensure_runnable` for the SSE
pre-check) that produces a `BudgetDecision`:

- **REFUSE** — the runtime raises `BudgetExceededError` (subclass of
  `AgentRunError`); no `agent_run` row is created.
- **ALLOW (no change)** — the run proceeds against the requested tier.
- **ALLOW (downgraded)** — the runtime resolves the cheaper tier
  one rung down `TIER_DOWNGRADE_LADDER`
  (INVESTIGATE → SUMMARIZE → TRIAGE) before building the model.

### Knobs

| Setting | Default | Role |
|---|---|---|
| `AGENT_BUDGET_DEGRADE_THRESHOLD` | `0.8` | Ratio at which a window flips to "downgrade tier" |
| `AGENT_RUNS_DISABLED_GLOBAL` | `false` | Hard global kill switch (refuse every run) |
| `AGENT_RUNS_DISABLED_TENANTS` | `""` | Comma-separated tenant UUIDs refused |

The **per-identity kill switch** is the existing
`IdentityBudget.request_limit = 0` row (set via the consumption
service's `set_limits`); the enforcement gate's cap-breach branch
picks it up alongside cap-by-use, and the reason string distinguishes
the two for the audit row.

### Boundary mapping

| Surface | On `BudgetExceededError` |
|---|---|
| REST `POST /api/v1/agents/{name}/run` | `HTTP 429` with body `{"detail": {"error": "budget_exceeded", "reason": "..."}}` |
| REST `POST /api/v1/agents/{name}/run/events` | Same `429` *before* the SSE stream opens (via `ensure_runnable`) |
| MCP `meho.agents.run` | JSON-RPC `-32602` (invalid params) with message `budget_exceeded: <reason>` |
| Scheduler fire | Logged at `WARN` and the trigger is *not* retried this tick (the cap is the contract) |

### Why all three windows are checked

The gate inspects the daily + weekly + monthly buckets and refuses on
the worst-case across the three — a run can be under-budget for today
but over-budget for the month. The conservative-read is the right
answer for the Initiative DoD *"total cost stays under the configured
budget"*.

### What does **not** trigger degradation

The threshold branch fires on the **tokens** and **cost** dimensions
only — not requests. A cheaper tier doesn't help with a "you used 9
of 10 daily request slots" cap (each run is still one request), so
degradation on requests would be semantically wrong; the request cap
fires only at the hard refusal.

### What is **deferred**

- **M1 persistence wiring + enum unification.** The persisted
  `AgentModelTier` (`standard` / `fast` / `deep`) and the
  resolver's `AgentTier` (`triage` / `investigate` / `summarize`)
  remain orthogonal. `_to_agent_definition` keeps its TODO; until
  the unification lands, the enforcement gate runs against
  `definition.tier` (still `None` for persisted definitions, set
  by tests + the resolver path). The threshold-degradation path
  exercises directly via programmatic `AgentDefinition`
  construction in tests.

## Known issues

- **Single token-stream limit.** The DB has one `token_limit` field;
  enforcement that wants to cap *output tokens specifically* (vs.
  total tokens) cannot do that with `IdentityBudget` alone. The
  consumption service tracks all four streams on the row's
  `tokens_consumed` aggregate, so the enforcement gate can read the
  per-stream amounts off `agent_run.cost` × pricing if needed in
  v1.next; a per-stream limit column would be additive.
- **No retention policy.** Old buckets accumulate indefinitely; a
  cleanup job (G11.5-T-followup or G11.3 scheduler trigger) is the
  natural home but is not scoped in v1.
- **Child runs do not separately charge consumption.** Pydantic AI
  shares the parent run's `RunUsage` across `invoke_agent` cascades
  (`usage=usage` in `run_child`), so the parent's terminal usage
  reflects the whole cascade. Applying consumption again on each
  child's `_finalize_child_run` would double-charge. The
  `_finalize_child_run` path calls `_finalize_run` without
  `usage=`, which the `if usage is not None` guard turns into a
  no-op for consumption. This is the right answer in the shared-
  usage world; a hypothetical per-child cost attribution would need
  RFC 8693 act-claim chain attribution (out of scope for this
  initiative).

## References

- Migration: `backend/alembic/versions/0031_create_identity_budget.py`
- ORM: `IdentityBudget` + `BudgetWindowKind` in
  `backend/src/meho_backplane/db/models.py`
- Service: `backend/src/meho_backplane/operations/identity_budget.py`
- Runtime wiring: `_finalize_run` + `_full_model_id` in
  `backend/src/meho_backplane/agent/invocation.py`
- Result-shape extension: `AgentRunResult` token fields in
  `backend/src/meho_backplane/agent/run.py`
- Tests:
  - `backend/tests/test_migration_0031_identity_budget.py`
  - `backend/tests/test_db_identity_budget.py`
  - `backend/tests/test_identity_budget_service.py`
  - `backend/tests/test_agent_run_consumption.py`
- Initiative #806 §C3; Task #1079; sibling Task #1080 (C3-b
  enforcement gate).
- Anthropic published pricing (as of 2026-05-27).
