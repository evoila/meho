-- SPDX-License-Identifier: Apache-2.0
-- Copyright (c) 2026 evoila Group
--
-- synthetic-n-plus-1.sql — sample non-trivial additive migration the
-- helm-rollback acceptance exercise (Task #57, Goal #11 DoD bullet 3)
-- uses for the N→N+1 step.
--
-- INTENTIONALLY NOT under backend/alembic/versions/. The CI guard
-- shipped by Task #29 (scripts/ci/check_migration_compat.py) scans
-- that directory and would reject destructive patterns; the rollback
-- exercise uses the sample migration only as a forward-compat
-- *fixture*, not as a real schema bump that should land on main.
-- Keeping it out-of-band from production migrations preserves the
-- additive-only invariant on the real migration sequence and matches
-- the unit-level test fixture at
-- backend/tests/fixtures/synthetic_n_plus_1.py (the unit-level
-- forward-compat proof — Task #30).
--
-- What "non-trivial additive migration" means for Goal #11 DoD bullet 3:
--
--   * Adds at least one new NULLABLE column with a server-side default
--     to an existing table. Non-trivial because it's not a no-op
--     (table exists; new field is observable to anyone with `\d`).
--   * Additive-only because v0.1's deploy contract forbids destructive
--     migrations (Task #29 CI guard). The rollback exercise leaves
--     the schema at N+1 — there is no down-migration in v0.1; helm
--     rollback reverts the image, not the schema.
--   * Touches the audit_log table because that's the only persistent
--     surface v0.1 writes to. Future v0.2 migrations will follow the
--     same shape (add columns rather than rename / drop).
--
-- Two columns, mirroring the unit-level fixture
-- (backend/tests/fixtures/synthetic_n_plus_1.py) so the cluster-level
-- and unit-level forward-compat exercises stay aligned on the same
-- shape of additive change. Both columns are NULLABLE with PG-side
-- defaults; PostgreSQL applies the default to existing rows lazily
-- (>= PG 11), so this is O(1) regardless of audit_log row count.
--
-- The rollback verifier's default --expected-schema-columns is
-- `payload_summary` — the text column below. Operators applying this
-- migration directly should also pass `payload_summary` (or whatever
-- column-set their real N+1 migration ships) to
-- rollback-verify.sh's --expected-schema-columns flag.
--
-- Apply with psql (the consumer-side rollback exercise runs this
-- against the namespace-scoped Postgres):
--
--   psql "$DATABASE_URL" -f scripts/acceptance/synthetic-n-plus-1.sql
--
-- The transactional wrapper ensures BOTH columns land or neither
-- does — a half-applied migration would leave the schema ambiguous
-- and the rollback exercise's assertions undefined.

BEGIN;

-- Primary additive column the rollback-verify.sh default expects.
-- `text NULL DEFAULT 'reserved_for_v0.2'` mirrors the unit-level
-- fixture's `future_field`; the default value is asserted by the
-- unit test as the proof that revision-N code does NOT write this
-- column.
ALTER TABLE audit_log
    ADD COLUMN payload_summary text NULL
    DEFAULT 'reserved_for_v0.2';

-- Companion JSONB column. JSONB is the realistic shape future v0.2
-- migrations will use (the existing `payload` column on audit_log is
-- already JSONB), so a sibling JSONB column is a representative
-- additive change. NULLABLE + server-side default keeps the migration
-- O(1) on existing rows.
ALTER TABLE audit_log
    ADD COLUMN payload_summary_jsonb jsonb NULL
    DEFAULT '{}'::jsonb;

COMMIT;
