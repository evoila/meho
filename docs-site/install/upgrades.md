# Upgrades

MEHO upgrades are a standard `helm upgrade` with two things worth
understanding before your first one: migrations run *before* the new
version rolls, and Helm 4's server-side apply can surface
field-ownership conflicts on clusters where someone once hand-patched
the Deployment.

## What `helm upgrade` does

```bash
helm upgrade meho oci://ghcr.io/evoila/meho-chart \
  --version <chart-version> -n meho -f values.yaml
```

1. The **pre-upgrade migration Job** runs every pending database
   migration first. If a migration fails, the release fails and the
   old Pods keep serving — an unmigrated schema never meets a new
   application version. Failed Jobs are retained for ten minutes for
   `kubectl logs`.
2. Only after migrations succeed does the new Deployment roll.
3. The old Pod stops before the new Pod starts (update strategy
   `Recreate`). Old and new code never share the database. The old Pod
   keeps serving for 5 seconds, so no request runs into a closed Pod.
   Then it takes no new calls and lets running operations finish, for up
   to 60 seconds. An operation that still runs then is written to the
   audit log as `unfinished`: check its target before you run it again.
   Then the new Pod starts.

**What an update costs:** about one to two minutes without MEHO: the
drain of the old Pod plus the start of the new one. This is the price of
never running two versions against one database. Plan updates for a quiet
time, and tell the people who use MEHO. The values `shutdown.drainSeconds`
and `shutdown.preStopSleepSeconds` set the two limits.

An upgrade ends the approval requests that still wait. A person approves
one exact release of MEHO, so when the new version starts, every request
that waits from the older version expires with the reason "MEHO was
updated; ask again". An approved request that has not run yet is refused
and does not run. The requester asks again under the new version. Tell
the people who wait for an approval before you upgrade.

Keep the image tag pinned and immutable (`v<x.y.z>` or
`sha-<git-sha>`) — `:latest` is never published, and `:main` is a
moving dev alias, not a deploy target.

## Helm 4 field-ownership conflicts

Helm 4 applies upgrades via Kubernetes **server-side apply**, which
tracks who owns every field of every object. If an operator once
hand-patched the live Deployment (`kubectl patch` / `kubectl edit`)
with a field a newer chart now sets, the upgrade fails with:

```text
Apply failed with 1 conflict: conflict with "kubectl-patch"
```

Pre-flight check — look for foreign field managers before upgrading:

```bash
kubectl get deploy meho -n meho -o yaml --show-managed-fields \
  | grep 'manager:'
```

Any manager other than Helm's own, owning a field the chart renders,
will conflict. The remedy is to hand those fields to the chart:

```bash
helm upgrade meho oci://ghcr.io/evoila/meho-chart \
  --version <chart-version> -n meho -f values.yaml --force-conflicts
```

`--force-conflicts` overwrites the conflicting fields and transfers
ownership to Helm. (Observed in the field: `--take-ownership` is *not*
the right flag here — it adopts whole un-owned resources, not
individual fields, and does not clear a per-field conflict.)

## Rollback

`helm rollback meho` is supported, with one asymmetry to know:
**migrations are forward-only**. A rollback reverts the application
version but not the schema — there is no automatic downgrade, so the
older application runs against the newer schema and relies on the
backend tolerating it. What "verified rollback" means precisely, and
the schema-check fallback when it cannot be, is specified in the
[rollback acceptance contract](https://github.com/evoila/meho/blob/main/docs/acceptance/rollback.md).

Every start writes the settings of the built-in operations into rows that
all pods share. A start that fails changes none of them. A pod that started
does change them, so during an update the old pod serves the new settings
until it stops. `GET /api/v1/health` shows which release wrote the rows
(`operation_catalog`). After a rollback, if it names another release,
restart the pods you keep. The rollback contract above has the steps.

**When the new version does not start**, MEHO is down, because the old
Pod has already stopped. A start that fails changes no operation row, so
roll back to the last good revision and wait for it:

```bash
helm history meho -n meho
helm rollback meho <revision> -n meho --wait --timeout 10m
```

Then check that `/healthz` answers and `/version` shows the previous
release. The rollback contract has the full checklist.

## Version-specific notes

Read these when your upgrade crosses the version in question:

| Crossing | What changes | Action |
|---|---|---|
| **v0.15.0** | Vault secret references became tenant-scoped by default: schemeless refs resolve under `secret/tenants/<tenant-id>/…`. | Migrate existing target `secret_ref`s to the tenant-scoped layout, or temporarily hold the old behaviour with the `VAULT_KV_TENANT_SCOPE_PREFIX=""` env override (via `extraEnv`) until migrated. Guide: [`docs/codebase/connectors-vault-tenant-scope.md`](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault-tenant-scope.md). Google Secret Manager deployments are unaffected. |
| **v0.22.0** | First release to ship the backplane startup probe in the chart. | If you ever hand-patched a `startupProbe` onto the Deployment, this is the textbook field-ownership conflict above — pre-flight and use `--force-conflicts`. |
| **v0.31.0** | The MCP tool surface became claim-scoped: a default session lists and dispatches only the 25-tool working surface; the operator-plane tools require the `mcp:admin` OAuth scope, and the approval-decision verbs have no MCP path at all. | Agent sessions that used operator-plane tools break until they request elevation: add `mcp:admin` as an **optional** (request-only) client scope on the MCP client in your realm, then opt sessions in with `MEHO_MCP_SCOPES=mcp:admin` (Claude Code plugin) or the `.mcpb` `scopes` user config. Approval decisions move to the operator console or CLI. The migrations are additive-only; no schema action. |
| **v0.32.0** | A new, most-restrictive `destructive` safety tier lands (`safe < caution < dangerous < destructive`), excluded by default at every gate and reachable only through the governed-delete approval flow (a preview-hash-bound call plus a mandatory blast-radius statement). Several permanent-delete operations adopt it. | Automation, agents, or service principals that previously called those operations directly are now refused — a human must approve each governed delete on the console or CLI. No config change is needed to adopt the safer default; the migrations are additive-only, so no manual schema step. |
| **v0.39.0**, **v0.40.0** | One person can be in several tenants. From v0.39.0 the Keycloak setup adds the `meho_tenant` attribute to the role groups and the `meho-tenants` mapper, so the login token lists the person's tenants (`meho_tenants`). From v0.40.0 MEHO reads this list and works in one current tenant per request. | **A site with one tenant needs to do nothing.** Without the list, MEHO keeps the old path. To add the list, run the setup again: `meho admin keycloak bootstrap-clients` with the same `--tenant-id` as before (your own Keycloak), or `helm upgrade` (the built-in Keycloak). The token then lists one tenant, and MEHO answers exactly as before, with no `Meho-Tenant` header. To add a second tenant, follow [One person in several tenants](../guides/several-tenants.md). |

The authoritative, always-current version of this table lives in
[`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md#version-specific-upgrade-notes);
release notes for each version are on the
[releases page](https://github.com/evoila/meho/releases) with the full
narrative in the
[CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md).

## Day-2 operations beyond upgrades

Backup/restore and observability guides land on this site with MEHO's
disaster-recovery workstream. Until then: PostgreSQL is MEHO's durable
*application* store and the thing to back up first (see the
[reference architecture](../architecture.md#state-postgresql-pgvector));
the Valkey activity feed is ephemeral by design.

A PostgreSQL restore alone does not rebuild a working deployment. The
target credentials themselves live in Vault or Google Secret Manager,
realm and client configuration lives in Keycloak, and your TLS Secrets
and Helm values live in the cluster and in whatever you keep them under
version control in. Each needs its own backup or reprovisioning path
until the full guide lands.
