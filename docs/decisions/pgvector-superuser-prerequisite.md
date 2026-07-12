# pgvector superuser prerequisite — reject a `migrationSuperuserDsn` chart value (decision)

**Status:** decided — **reject** a dedicated superuser migration DSN; document the pre-create prerequisite instead
**Date:** 2026-07-12
**Goal:** [#221](https://github.com/evoila/meho/issues/221) — closed-loop dogfood hardening
**Initiative:** [#2390](https://github.com/evoila/meho/issues/2390) — Helm chart / first-boot cold-install gaps (fresh-deploy paper cuts on v0.21.0)
**Task:** [#2392](https://github.com/evoila/meho/issues/2392) (this decision + the accompanying docs)

## The matter this records

The chart's pre-install migration Job runs Alembic revision `0003`
([`backend/alembic/versions/0003_create_documents_with_pgvector.py`](../../backend/alembic/versions/0003_create_documents_with_pgvector.py)),
which executes `CREATE EXTENSION IF NOT EXISTS vector`. `CREATE EXTENSION`
requires a **superuser** in stock PostgreSQL. A normally-provisioned
cluster hands MEHO a **least-privilege app role** (the DSN user in
`postgres.credentialsSecret`), so a **cold** install fails at that step
with `permission denied to create extension "vector"`
(`HINT: Must be superuser to create this extension.`). Surfaced by a
GCP-native adopter cold-installing v0.21.0 on CloudNativePG (CNPG).

Issue [#2392](https://github.com/evoila/meho/issues/2392) proposed three
non-exclusive remedies:

1. Document the hard prerequisite (pre-create the extension as a
   superuser, or bootstrap it via CNPG `postInitSQL`).
2. Ship a **second** chart value — `migrationSuperuserDsn` — so the
   `CREATE EXTENSION` step runs under a superuser DSN while normal
   migrations keep running on the app role.
3. Document the CNPG `managed.roles` / `postInitSQL` cluster-init path.

Remedies 1 and 3 are documentation and are being delivered by this task.
The open decision is remedy 2:

> **Should the chart ship a `migrationSuperuserDsn` value that runs the
> extension-creating migration under a separate superuser DSN?**

## Decision

**No.** The chart will **not** ship a `migrationSuperuserDsn` (or any
equivalent second migration DSN). The pgvector superuser requirement is
documented as a one-time, pre-install prerequisite:

- [`deploy/values-examples/README.md`](../../deploy/values-examples/README.md)
  — § *pgvector extension prerequisite* (Option A: pre-create as a
  superuser / CNPG `postInitSQL`; Option B: first migration under a
  superuser role), plus a step in the end-to-end install flow.
- [`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml)
  — the `postgres.credentialsSecret` comment states the hard
  prerequisite and the exact psql / CNPG one-liner.

Recommended posture: **pre-create the extension once as a superuser and
keep the running DSN least-privilege** (Option A).

## Rationale

- **Substrate minimalism.** MEHO's design bias is a dumb substrate and a
  smart operator: no tunables or config surfaces that a one-line runbook
  step covers. `CREATE EXTENSION vector` is a **one-time, per-database**
  bootstrap action, not a per-release operation. Encoding it as a
  standing chart value trades a single documented `psql` line for a
  permanent second DSN, a second Kubernetes Secret to provision and
  rotate, ESO wiring for it, `values.schema.json` conditionals, and
  migration-job template branching to select which DSN runs which
  revision. That is a large, permanent surface for a one-time step.

- **Security posture is worse, not better.** A `migrationSuperuserDsn`
  means a superuser credential is provisioned into the cluster and lives
  in the release's Secret set for the lifetime of the deployment, read by
  the migration Job on **every** `helm upgrade`. Option A confines
  superuser use to a single, out-of-band, pre-install action by whoever
  already administers the database; nothing superuser-capable is
  persisted in MEHO's Secret set. Least privilege favours the documented
  prerequisite.

- **Splitting migrations across two DSNs is fragile.** Alembic applies a
  linear revision chain against one connection. Running one revision
  (0003) under a superuser DSN and the rest under the app-role DSN needs
  the migration runner to know which revisions touch extensions and swap
  connections mid-`upgrade head`. That is real, ongoing complexity in
  `meho_backplane.db.migrate` for a problem that a superuser solves once
  at bootstrap. Future extension-creating migrations would each have to
  be classified into the "superuser" bucket — a standing maintenance tax.

- **The platform already offers the idiomatic path.** CNPG (the adopter's
  operator, and the common managed-Postgres shape) exposes
  `bootstrap.initdb.postInitSQL`, which runs as the bootstrap superuser
  at cluster creation — the natural home for `CREATE EXTENSION vector`.
  Documenting that path is strictly better than reinventing it as a chart
  value.

- **It does not even remove the prerequisite.** A `migrationSuperuserDsn`
  still requires the operator to *have* a superuser credential and wire
  it in — the same superuser access Option A uses directly, only now
  routed through more chart machinery. It moves the requirement; it does
  not eliminate it.

## Consequences

- Cold-install adopters run one documented, idempotent superuser step
  (or declare `postInitSQL`) before `helm install`. The failure mode is
  now anticipated in the chart docs and the `values.yaml` comment rather
  than discovered at first migration.
- Migration `0003` is **unchanged** — it already runs
  `CREATE EXTENSION IF NOT EXISTS vector`
  (`0003_create_documents_with_pgvector.py:167`), which is exactly what
  makes the pre-create workaround succeed (a pre-existing extension turns
  `IF NOT EXISTS` into a NOTICE-and-skip). No migration SQL changes; the
  existing stamp-replay / probe tests
  (`backend/tests/migrations/test_alembic_probe.py`) stay green.
- No new chart value, Secret, schema conditional, or migration-runner
  branching is introduced.

## Revisit criteria

Reopen this decision if a future release adds an extension-creating
migration that must run **unattended on `helm upgrade`** against a role
that cannot be granted the extension out of band — i.e. a genuinely
recurring superuser requirement rather than a one-time bootstrap. At that
point the split-DSN complexity may be justified; today it is not.

## References

- Task: [#2392](https://github.com/evoila/meho/issues/2392)
- Parent Initiative: [#2390](https://github.com/evoila/meho/issues/2390)
- Migration: [`backend/alembic/versions/0003_create_documents_with_pgvector.py`](../../backend/alembic/versions/0003_create_documents_with_pgvector.py)
- Chart docs: [`deploy/values-examples/README.md`](../../deploy/values-examples/README.md) (§ pgvector extension prerequisite)
- Chart values: [`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml) (`postgres.credentialsSecret`)
- pgvector installation (extension setup): <https://github.com/pgvector/pgvector>
- PostgreSQL `CREATE EXTENSION` — superuser required for non-trusted extensions (pgvector's `vector` is not trusted): <https://www.postgresql.org/docs/18/sql-createextension.html>
- CNPG `postInitSQL` bootstrap (`spec.bootstrap.initdb.postInitSQL`, runs as the `postgres` superuser): <https://cloudnative-pg.io/docs/current/bootstrap/>
