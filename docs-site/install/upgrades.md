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

## Version-specific notes

Read these when your upgrade crosses the version in question:

| Crossing | What changes | Action |
|---|---|---|
| **v0.15.0** | Vault secret references became tenant-scoped by default: schemeless refs resolve under `secret/tenants/<tenant-id>/…`. | Migrate existing target `secret_ref`s to the tenant-scoped layout, or temporarily hold the old behaviour with the `VAULT_KV_TENANT_SCOPE_PREFIX=""` env override (via `extraEnv`) until migrated. Guide: [`docs/codebase/connectors-vault-tenant-scope.md`](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault-tenant-scope.md). Google Secret Manager deployments are unaffected. |
| **v0.22.0** | First release to ship the backplane startup probe in the chart. | If you ever hand-patched a `startupProbe` onto the Deployment, this is the textbook field-ownership conflict above — pre-flight and use `--force-conflicts`. |

The authoritative, always-current version of this table lives in
[`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md#version-specific-upgrade-notes);
release notes for each version are on the
[releases page](https://github.com/evoila/meho/releases) with the full
narrative in the
[CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md).

## Day-2 operations beyond upgrades

Backup/restore and observability guides land on this site with MEHO's
disaster-recovery workstream. Until then: the state that needs backing
up is the PostgreSQL database (all durable state lives there — see the
[reference architecture](../architecture.md#state-postgresql-pgvector));
the Valkey activity feed is ephemeral by design.
