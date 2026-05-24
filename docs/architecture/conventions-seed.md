<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Tenant conventions seed (`rdc-internal`)

> Sister to [docs/codebase/tenant_conventions.md](../codebase/tenant_conventions.md) -- that doc owns the table shape, ORM models, and CRUD control flow; this doc owns the one-shot seed migration that populates the `rdc-internal` tenant with the 8 operational conventions extracted from the consumer's `CLAUDE.md` per [decision #4](../planning/v0.2-decisions.md).
>
> Covers the implementation that landed under [Initiative #229 G7.1](https://github.com/evoila/meho/issues/229), Task [#317 G7.1-T5](https://github.com/evoila/meho/issues/317).

## What the seed does

The seed migration [`backend/alembic/versions/0018_seed_rdc_internal_conventions.py`](../../backend/alembic/versions/0018_seed_rdc_internal_conventions.py) is a **data migration only** -- it adds no schema. The two tables it writes to (`tenant_conventions` and `tenant_convention_history`) were created by [T1 #313](../../backend/alembic/versions/0015_create_tenant_conventions.py).

On `upgrade head` the migration:

1. **Upserts** the `rdc-internal` tenant row (`INSERT ... ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name`). If an operator manually created the row before the migration ran, its `id` is preserved and only the display name refreshes.
2. **Inserts 8 `operational` conventions** for that tenant (`INSERT ... ON CONFLICT (tenant_id, slug) DO NOTHING`). Operator-authored rows under the same slug take precedence over the seed body -- the migration never overwrites a curated convention.
3. **Inserts one CREATE-shape history row per seeded convention** (`body_before=NULL`, `body_after=<seed body>`, `actor_sub='migration:seed-rdc-conventions'`, `audit_id=NULL`). The history row is only written when the convention insert actually landed, so already-curated rows don't get a spurious "seed" entry in their edit trail.

On `downgrade` the migration removes the 8 seeded conventions and their matching history rows, but **leaves the `rdc-internal` tenant intact** -- other v0.2 features key on `tenant_id` and dropping the tenant would orphan that data.

## Why decision #4

[Decision #4](../planning/v0.2-decisions.md#4-g7-server-side-partition--operational-rules-only) draws the partition between the consumer's `CLAUDE.md` content that migrates to server-side conventions (Layer 1, this seed) and the content that stays in the consumer's repo. The principle: **operational rules** bind any session against the tenant regardless of where it runs (Slack agent, MCP client, CLI on a different machine); **repo-internal rules** apply only to repo work and have a filesystem of their own.

Migrated to `rdc-internal` tenant conventions (this seed):

- Vault canonical + 1Password residual
- Naming rule -- no `claude` / AI-tool names in operator-visible identifiers
- Secret-handling discipline (never paste into chat, never commit secrets)
- CLI-wrapper fallback discipline during MEHO transition
- Sensitive-lab-specifics-stay-private (no real hostnames/IPs in public repo)

Not migrated (stay in repo `CLAUDE.md`):

- `/work-ticket` flow + ticket+PR discipline
- Markdown-sidecar convention for OpenAPI specs
- PR cadence rules
- Repo-internal naming for branches / commits

The seed extends decision #4's five-rule list with three additional **operational** rules pulled from cross-team operating practice (destructive-ops probe, audit-trail discipline, approval workflow). These were called out as in-scope by [issue #317](https://github.com/evoila/meho/issues/317)'s context section -- they share the "binds any session, regardless of where it runs" property decision #4 keys on.

## Slug -> source paragraph mapping

The 8 seeded slugs and the source paragraphs they migrate from. Source refers to [`evoila-bosnia/claude-rdc-hetzner-dc/CLAUDE.md`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/CLAUDE.md) except where noted.

| Slug | Title | Source |
|---|---|---|
| `vault-canonical` | Vault is canonical for secrets | `CLAUDE.md` "Secrets" section -- the canonical-vs-residual partition and the pipe-don't-paste discipline. |
| `naming-rule-no-ai-tool-names` | No AI-tool names in operator-visible identifiers | `CLAUDE.md` "Naming rule" section -- the prohibition on AI-tool tokens in operator-visible identifiers + the Holodeck `claude`-legacy carve-out. |
| `secret-handling-discipline` | Secret-handling discipline | `CLAUDE.md` "Secrets" + "How to work with secrets" sections -- the "never enter chat / commit / PR" envelope + the rotation-on-leak path. |
| `cli-wrapper-fallback-discipline` | CLI wrapper fallback during MEHO transition | v0.2 transition rules (cross-team operating practice during the chassis -> MEHO transition window). |
| `destructive-ops-probe-first` | Probe before destructive ops | Cross-team operating practice -- the dependents-check + ticket-document + team-confirm sequence operators apply before destructive ops on shared targets. |
| `audit-trail-discipline` | Document findings as you go | Cross-team operating practice -- the kb-as-investigation-record discipline. Forward-looking on `meho kb write` (G4); falls back to local `kb/` until G4 lands. |
| `sensitive-lab-specifics-stay-private` | Sensitive lab specifics stay private | `CLAUDE.md` "Sensitive lab specifics stay private" section -- the public/private repo partition + the sanitised-examples rule. |
| `approval-workflow-when-it-lands` | Approval workflow expectations | Forward-looking placeholder for v0.2.next G-Policy. Captures the team-norm operate-by-discussion shape until approval-workflow lands. |

## Priority assignment

[T4 #316](https://github.com/evoila/meho/issues/316)'s preamble assembler packs `kind='operational'` conventions **highest-priority-first** into a 600-token budget; over-budget entries are dropped whole (never mid-entry truncation of an operational rule). The seed assigns three priority tiers that reflect operator safety ranking:

| Priority | Tier | Conventions | Rationale |
|---|---|---|---|
| **100** | Unrecoverable breach | `vault-canonical`, `secret-handling-discipline`, `sensitive-lab-specifics-stay-private` | Breach is unrecoverable in the operator-time sense: rotated secret or compromised tenant or leaked specifics on a public repo. Must reach every session even when the budget is tight. |
| **50** | Recoverable but costly | `naming-rule-no-ai-tool-names`, `cli-wrapper-fallback-discipline`, `destructive-ops-probe-first` | Breach is recoverable but costly: incoherent target identifiers; deleted-wrapper-with-no-`meho`-replacement; collision on a destructive op. Important but acceptable to drop when the budget is tight. |
| **10** | Aspirational / forward-looking | `audit-trail-discipline`, `approval-workflow-when-it-lands` | Forward-looking; the enforcement substrate either lands in G4 (`meho kb write`) or is not yet built (G-Policy approval workflow). Drop first when the budget overflows because the rule cannot yet be enforced in v0.2 even if the agent reads it. |

The total estimated token cost of all 8 conventions packed together is approximately **671 tokens** (sum of `ceil(len(body) / 3.3)` per `meho_backplane.conventions.schemas.estimate_tokens`), which slightly exceeds the 600-token default budget. T4's priority-ranked packer will drop one or both of the priority-10 conventions when packing the full seed against an untouched budget -- this is the intended behaviour and the reason the priority tiers exist. The acceptance contract on [#317](https://github.com/evoila/meho/issues/317) explicitly allows "all 8 titles + bodies (or truncated subset if over budget)" in the assembled preamble.

Operators can re-rank individual conventions via the `PATCH /api/v1/conventions/{slug}` route (T2) once the seed has landed -- the seed sets initial priorities, not immutable ones.

## Idempotency

Re-running the migration is a no-op for already-seeded data:

- The tenant `ON CONFLICT (slug) DO UPDATE` rewrites `name` to the same string the previous pass wrote.
- The convention `ON CONFLICT (tenant_id, slug) DO NOTHING` skips every already-present row.
- The history-row insert is gated on the conventions `RETURNING id` result -- only convention inserts that actually landed produce a history row.

This matters for the test suite (the upgrade -> downgrade -> upgrade replay in [`backend/tests/test_alembic_seed_rdc_conventions.py`](../../backend/tests/test_alembic_seed_rdc_conventions.py)) and for the testcontainers PG replay cycle in [`backend/tests/test_migration_rollback.py`](../../backend/tests/test_migration_rollback.py).

## Reversibility

`downgrade()` removes the 8 seeded convention rows and their matching history rows, narrowed on `created_by_sub='migration:seed-rdc-conventions'` and `actor_sub='migration:seed-rdc-conventions'` respectively. The narrowing means:

- A convention an operator authored under the same slug **before** the seed ran (and the seed skipped via `ON CONFLICT DO NOTHING`) survives the downgrade -- its `created_by_sub` carries the operator's JWT `sub`, not the seed marker.
- A history entry the operator wrote against a seeded convention (via the T2 PATCH route) survives the downgrade -- its `actor_sub` carries the operator's JWT `sub`, not the seed marker.

The `rdc-internal` tenant row is intentionally preserved on downgrade. Other v0.2 features (targets, audit rows, broadcast overrides, agent definitions) key on `tenant_id`; deleting the tenant would invisibly orphan that data even though the soft-FK discipline means no DB-level CASCADE would fire.

## How to add new seeded conventions

This migration is a **one-shot** seed -- new conventions land via the T2 API (or T3 CLI when it ships under [#315](https://github.com/evoila/meho/issues/315)), not by amending this file. A follow-up migration that needs to add a seeded convention should:

1. Land its own new revision (e.g. `0019_seed_<convention>.py`) following the same upsert-or-skip discipline this migration established.
2. Read the existing `rdc-internal` tenant id via `SELECT id FROM tenant WHERE slug = 'rdc-internal'` rather than inserting a duplicate tenant.
3. Use the same `_SEED_ACTOR_SUB` synthetic marker (or a fresh one specific to that migration) so the downgrade predicate can be narrowed.
4. Update the table in this doc to reflect the new slug + priority + source mapping.

The reason for one-shot vs amend-and-replay: an operator who edited a seeded body post-seed should keep their edit. Re-running an amended seed migration cannot distinguish "operator hadn't gotten around to it" from "operator deliberately reverted to a different body" -- the `ON CONFLICT DO NOTHING` shape lets the operator's intent win every time.

## References

- Parent Initiative: [#229](https://github.com/evoila/meho/issues/229) (G7.1 tenant conventions + Layer 2 starter).
- This task: [#317](https://github.com/evoila/meho/issues/317) (G7.1-T5 seed migration).
- Companion docs:
  - [`docs/codebase/tenant_conventions.md`](../codebase/tenant_conventions.md) -- schema + API + ORM detail.
  - [`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md) -- decision #4 (the G7 server-side partition).
- Source `CLAUDE.md`: [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/CLAUDE.md) -- the operational paragraphs the seed migrates.
- Prior-art data migration: [`backend/alembic/versions/0011_backfill_operation_group_when_to_use.py`](../../backend/alembic/versions/0011_backfill_operation_group_when_to_use.py) -- the self-contained `sa.table` shim discipline this migration mirrors.
