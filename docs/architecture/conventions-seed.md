<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Tenant conventions seed (`default`)

> Sister to [docs/codebase/tenant_conventions.md](../codebase/tenant_conventions.md) -- that doc owns the table shape, ORM models, and CRUD control flow; this doc owns the seed migrations that populate the `default` tenant with the 2 illustrative conventions every fresh `evoila/meho` deploy receives, and explains the consumer-side template for re-applying customer-specific operational discipline that previously shipped baked into the OSS surface.
>
> Covers the implementation that shipped under [Initiative #229 G7.1](https://github.com/evoila/meho/issues/229), Task [#317 G7.1-T5](https://github.com/evoila/meho/issues/317), and the OSS-commercialization-readiness follow-up [#1130 G0.13](https://github.com/evoila/meho/issues/1130), Task [#1137 G0.13-T7](https://github.com/evoila/meho/issues/1137).

## What the seed does (current state)

Two Alembic migrations are involved:

- [`backend/alembic/versions/0018_seed_rdc_internal_conventions.py`](../../backend/alembic/versions/0018_seed_rdc_internal_conventions.py) -- preserved in OSS history. Originally upserted the `rdc-internal` tenant + 8 operational conventions extracted from one consumer's `CLAUDE.md` (decision #4 partition). Still runs as part of the migration chain (Alembic is forward-only), but its rows are immediately superseded by `0028`.
- [`backend/alembic/versions/0028_supersede_rdc_internal_seed.py`](../../backend/alembic/versions/0028_supersede_rdc_internal_seed.py) -- the supersede landed by [#1137](https://github.com/evoila/meho/issues/1137). On `upgrade head`:
  1. **Cleans up the rows `0018` authored** under the `rdc-internal` tenant. Narrows on `created_by_sub` / `actor_sub` = `'migration:seed-rdc-conventions'` so operator-curated content under seeded slugs survives. The `rdc-internal` tenant row itself is intentionally preserved (other v0.2 features key on `tenant_id`; deleting it would orphan that data).
  2. **Upserts the generic `default` tenant** (`INSERT ... ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name`). If an operator already created a tenant with slug `'default'`, the upsert refreshes the display name and returns the existing id.
  3. **Inserts 2 illustrative conventions** for the `default` tenant (`INSERT ... ON CONFLICT (tenant_id, slug) DO NOTHING`). Operator-authored rows under the same slug take precedence over the seed -- the migration never overwrites curated content.
  4. **Inserts one CREATE-shape history row per seeded convention** (`body_before=NULL`, `body_after=<seed body>`, `actor_sub='migration:seed-default-conventions'`, `audit_id=NULL`). The history row is only written when the convention insert actually landed.

On `downgrade 0027`, the migration removes the 2 seeded `default` conventions and their matching history rows but **leaves the `default` tenant intact** -- and crucially does NOT restore the `rdc-internal` seed content. Restoring it would re-leak consumer-specific operational discipline into the public deploy. Operators who need the rdc-internal content back apply the consumer-side template documented [below](#consumer-side-template-for-rdc-internal-content).

## Why a generic default tenant

The `evoila/meho` repo is public. Migration `0018` originally upserted `rdc-internal` + 8 operational conventions extracted from one consumer's `CLAUDE.md` on every deploy. Adopting customer B's MCP clients were receiving customer A's internal operational discipline + repo references as their `initialize.instructions` text at session start -- the spec-optional MCP field T4 ([#316](https://github.com/evoila/meho/issues/316)) populates from the seeded `kind='operational'` conventions.

[#1137](https://github.com/evoila/meho/issues/1137) (G0.13-T7) closes that OSS commercialization-readiness gap by replacing the consumer-specific seed with a generic illustration:

- The `default` tenant + 2 illustrative conventions demonstrate the feature without baking in a specific consumer's identity.
- The 2 conventions read as documentation about the convention surface itself (slug naming convention, the operator-facing nature of the surface) -- they explain what the feature does rather than carrying any specific operator's operational rules.
- Operators replace the illustrative seed with their own content via the T2 API (`PATCH /api/v1/conventions/{slug}`) or T3 CLI (`meho conventions ...`).
- The MCP `initialize.instructions` field still flows from `assemble_preamble(operator.tenant_id)` -- but for operators whose `tenant_id` is not `default` (the common case for any deploy), the field is empty until the operator authors their own conventions.

The two seeded conventions:

| Slug | Title | Priority | What it demonstrates |
|---|---|---|---|
| `slug-naming-kebab-case` | Convention slugs use kebab-case | 50 | The slug naming pattern the feature itself follows; appears in operator-facing URLs + CLI verbs. |
| `conventions-are-operator-facing` | Conventions are operator-facing and tenant-scoped | 10 | Repeats decision #4's partition: operational rules (server-side) vs repo-discipline rules (consumer-side). Points at this doc. |

Both bodies are intentionally short (under ~200 chars each) so the per-entry token cost leaves headroom for operators adding their own conventions on top.

## Decision #4 partition (still load-bearing)

[Decision #4](../planning/v0.2-decisions.md#4-g7-server-side-partition--operational-rules-only) draws the partition between content that migrates to server-side tenant conventions (Layer 1) and content that stays in the consuming application's own repository (Layer 2). The principle: **operational rules** bind any session against the tenant regardless of where it runs (Slack agent, MCP client, CLI on a different machine); **repo-internal rules** apply only to repo work and have a filesystem of their own.

This partition is unchanged by [#1137](https://github.com/evoila/meho/issues/1137). What changed is **what content the public OSS repo ships as its baked-in seed**:

- Before #1137: the OSS shipped one specific consumer's operational rules baked into the OSS surface (the `rdc-internal` seed).
- After #1137: the OSS ships a generic illustration; consumer-specific operational rules live in consumer-side migrations.

## Consumer-side template for rdc-internal content

The 8 `rdc-internal`-specific conventions that previously shipped baked into `0018` move to a consumer-side migration the consumer applies post-deploy in their own infrastructure. The template carries the same content `0018` originally seeded, indexed under a tenant slug the consumer chooses for their deploy (not necessarily `rdc-internal` -- the seed is generic-shape, not slug-pinned).

The consumer-side migration applies on top of an `evoila/meho` deploy that has already run `upgrade head` (so `0028`'s cleanup ran). Recommended shape (consumer adapts to their own Alembic / sqitch / migration tooling):

```python
# Consumer-side migration: re-seed rdc-internal-specific operational
# discipline that previously shipped baked into evoila/meho 0018.
#
# Run this against the consumer's own deploy AFTER `meho` upgrade-head
# completes (so 0028's cleanup has run and the rdc-internal tenant
# row is empty of seeded conventions).
#
# Replace ``YOUR_TENANT_SLUG`` with the slug your deploy uses --
# either ``rdc-internal`` (kept compatible with the legacy slug) or
# a fresh consumer-chosen slug. The seed marker
# ``'migration:seed-consumer-conventions'`` lets your downgrade
# narrow on the consumer-authored rows without touching whatever
# else has accumulated under the same tenant.

_TENANT_SLUG = "YOUR_TENANT_SLUG"
_SEED_ACTOR_SUB = "migration:seed-consumer-conventions"

_CONSUMER_CONVENTIONS = (
    ("vault-canonical", "Vault is canonical for secrets", 100, "..."),
    ("naming-rule-no-ai-tool-names", "...", 50, "..."),
    ("secret-handling-discipline", "...", 100, "..."),
    ("cli-wrapper-fallback-discipline", "...", 50, "..."),
    ("destructive-ops-probe-first", "...", 50, "..."),
    ("audit-trail-discipline", "...", 10, "..."),
    ("sensitive-lab-specifics-stay-private", "...", 100, "..."),
    ("approval-workflow-when-it-lands", "...", 10, "..."),
)

# upgrade(): upsert tenant by slug, insert conventions with
# ON CONFLICT (tenant_id, slug) DO NOTHING + matching CREATE
# history rows. Mirrors the discipline upstream 0018 established.
# downgrade(): narrow deletes on _SEED_ACTOR_SUB; keep the tenant
# row.
```

The full convention body text (the 8 paragraphs originally extracted from the consumer's `CLAUDE.md`) is in OSS history at [`backend/alembic/versions/0018_seed_rdc_internal_conventions.py`](../../backend/alembic/versions/0018_seed_rdc_internal_conventions.py) and can be copied verbatim into the consumer-side migration.

A companion tracking issue is filed alongside [#1137](https://github.com/evoila/meho/issues/1137) in the consumer's own repository so the consumer picks up the consumer-side migration on their next deploy cycle.

## Why a new revision (`0028`) instead of editing `0018`

Alembic migrations are forward-only: an existing deploy that already ran `0018` has the `rdc-internal` rows in its DB, and a no-data edit to `0018`'s body would not remove them on `upgrade head` (Alembic gates on the revision number, not the migration content). The forward-compatible fix is a new revision whose `upgrade()` performs the cleanup + new seed.

`0018`'s file is preserved in git history as a record of what was once shipped. The OSS commit log captures the cleanup migration on top -- searchable, auditable, reversible.

## Audit-trace join semantics (G8) — unchanged

Both seed-authored history rows (`0018`'s rdc-internal seed and `0028`'s default seed) carry `audit_id = NULL` (the migrations run outside any HTTP request). Any G8 audit-replay query that joins `tenant_convention_history` to `audit_log` **must use `LEFT JOIN`** -- an `INNER JOIN` silently drops every seeded history row.

The `actor_sub` discriminator distinguishes the three sources:

- `migration:seed-rdc-conventions` -- the rows `0018` originally authored (cleaned up by `0028` on `upgrade head`; only present on stamped-mid-chain DBs).
- `migration:seed-default-conventions` -- the rows `0028` authors under the `default` tenant.
- JWT-shaped sub (e.g. `user:operator@example.com`) -- operator-authored content surviving across both migrations.

## Idempotency

Both migrations are idempotent on replay:

- `0018`: upsert + `ON CONFLICT DO NOTHING` shape; covered by [`backend/tests/test_alembic_seed_rdc_conventions.py`](../../backend/tests/test_alembic_seed_rdc_conventions.py) (SQLite) and `TestSeedRdcInternalConventionsPgIdempotency` in [`backend/tests/test_migration_rollback.py`](../../backend/tests/test_migration_rollback.py) (PG).
- `0028`: cleanup narrows on the seed marker (matches zero rows on a replay where the cleanup already ran) + upsert + `ON CONFLICT DO NOTHING` shape; covered by [`backend/tests/test_alembic_seed_0028_supersede.py`](../../backend/tests/test_alembic_seed_0028_supersede.py) (SQLite) and `TestSupersedeDefaultConventionsPgIdempotency` in `test_migration_rollback.py` (PG).

## Reversibility

`0018.downgrade()` removes the 8 originally-seeded `rdc-internal` conventions + their matching history rows (narrowed on `created_by_sub` / `actor_sub` = `'migration:seed-rdc-conventions'`).

`0028.downgrade()` removes the 2 illustrative `default` conventions + their matching history rows (narrowed on `'migration:seed-default-conventions'`). The `default` tenant row is preserved. The downgrade does **not** restore the `rdc-internal` seed -- restoring consumer-specific content into the public deploy would defeat #1137's purpose.

Both downgrades leave operator-authored content (under seeded or unrelated slugs) untouched.

## How to add new seeded conventions

This guidance is unchanged from before #1137: amending an existing seed migration is the wrong shape (operator-edited content would survive the amendment while the new seed body would not land). A follow-up migration that needs to add a seeded convention should:

1. Land its own new revision (e.g. `0029_seed_<convention>.py`) following the upsert-or-skip discipline both existing seed migrations establish.
2. Read the existing tenant id via `SELECT id FROM tenant WHERE slug = 'default'` rather than inserting a duplicate tenant.
3. Use a fresh seed marker (e.g. `migration:seed-<short-name>`) so the downgrade predicate can narrow on the new migration's authored rows alone.
4. Update the tables in this doc.

## References

- Parent Initiative: [#229](https://github.com/evoila/meho/issues/229) (G7.1 tenant conventions + Layer 2 starter).
- Original seed task: [#317](https://github.com/evoila/meho/issues/317) (G7.1-T5 seed migration).
- Supersede Initiative: [#1130](https://github.com/evoila/meho/issues/1130) (G0.13 v0.6.0 dogfood hardening).
- Supersede task: [#1137](https://github.com/evoila/meho/issues/1137) (G0.13-T7 generalize seed, move rdc-internal to consumer-side).
- Companion docs:
  - [`docs/codebase/tenant_conventions.md`](../codebase/tenant_conventions.md) -- schema + API + ORM detail.
  - [`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md) -- decision #4 (the G7 server-side partition).
- Prior-art data migration: [`backend/alembic/versions/0011_backfill_operation_group_when_to_use.py`](../../backend/alembic/versions/0011_backfill_operation_group_when_to_use.py) -- the self-contained `sa.table` shim discipline both seed migrations mirror.
