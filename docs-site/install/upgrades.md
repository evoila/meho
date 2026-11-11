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

## Upgrade fails with `spec.strategy.rollingUpdate: Forbidden`

From v0.46.0 the chart uses the `Recreate` update strategy: the old pod
stops before the new one starts. An install made with an older chart has
the strategy `RollingUpdate` on its live Deployment, with a
`rollingUpdate` block that Kubernetes filled in. Helm 4 with server-side
apply does not remove that block, and Kubernetes refuses it together with
`Recreate`:

```text
Deployment.apps "meho" is invalid: spec.strategy.rollingUpdate: Forbidden: may not be specified when strategy `type` is 'Recreate'
```

**The chart fixes this itself.** On such an upgrade it runs a short Job
before the upgrade (`<release>-…-update-strategy`). The Job removes the
block and sets `Recreate` on the live Deployment. No pod restarts for this.
Helm removes the Job once it has succeeded. Helm 3 needs no Job: it removes
the block on its own.

**If a revision already failed** with this message (an upgrade to a chart
built between v0.45.0 and this fix), the old pod still runs the old
version. Check with `helm history meho -n meho`. Then:

1. Run `helm upgrade` again with a chart that has the fix. The Job fixes
   the Deployment first, and the release ends `deployed`.
2. If you must stay on the chart that failed, fix the Deployment by hand
   and run the same `helm upgrade` again:

   ```bash
   kubectl patch deployment meho -n meho --type=merge \
     -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
   ```

3. Check the result. The release is `deployed`, and the Deployment has
   `Recreate` and no `rollingUpdate` block:

   ```bash
   helm status meho -n meho
   kubectl get deploy meho -n meho -o jsonpath='{.spec.strategy}'
   ```

**Argo CD** renders the chart with `helm template`, so it never runs this
Job. With server-side apply on, run the `kubectl patch` above once, then
sync.

**Later, back to `RollingUpdate`:** the change above belongs to the field
manager `meho-update-strategy` (or `kubectl-patch`). If you later set
`updateStrategy.type: RollingUpdate`, Helm 4 reports a conflict with that
manager. Use `--force-conflicts`, as in the section above.

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

## The bootstrap Job needs `manage-realm`

This applies to the built-in Keycloak (`coreKeycloak.enabled`).

After every install and upgrade, the bootstrap Job keeps the approval
sign-in up to date: it makes the login flow `meho approval sign-in level`
with the steps of the realm file, names the approval level in the realm,
and binds the client `meho-cli-approve` to the flow. For this, its account `meho-admin` needs the realm-management role
`manage-realm`. A new install gets the role from the realm file.

An install whose first start was older, and had the company login off,
may lack the role. Then the Job stops, and its log names the role and
this section:

```text
core_bootstrap_failed: login flow meho approval sign-in level: meho-admin may not read or change login flows, ...
```

With `helm upgrade --atomic`, Helm rolls the release back when the Job
stops, so the old version keeps running until you add the role.

Give `meho-admin` the role once, then run the upgrade again:

1. Log in to the Keycloak admin console as the console admin
   ([The console admin](keycloak-realm.md#the-console-admin)) and pick
   realm `meho`. Lost every admin login? See
   [Keycloak break-glass](keycloak-break-glass.md).
2. **Clients → meho-admin → Service account roles → Assign role →
   Filter by clients**, search for `realm-management`, select
   `manage-realm`, **Assign**.
3. Run `helm upgrade` again. The Job's log says `login flow meho approval
   sign-in: kept` (or `created`), and `client meho-cli-approve: kept`.

Then check it: `GET /api/v1/health` shows `approvals.sign_in.state: ok`,
and `meho doctor` shows `approval sign-in` as OK.

With this role the backplane, which also uses `meho-admin`, can change
the login settings of realm `meho`. It could already change people,
groups and clients.

## Version-specific notes

Read these when your upgrade crosses the version in question:

| Crossing | What changes | Action |
|---|---|---|
| **v0.15.0** | Vault secret references became tenant-scoped by default: schemeless refs resolve under `secret/tenants/<tenant-id>/…`. | Migrate existing target `secret_ref`s to the tenant-scoped layout, or temporarily hold the old behaviour with the `VAULT_KV_TENANT_SCOPE_PREFIX=""` env override (via `extraEnv`) until migrated. Guide: [`docs/codebase/connectors-vault-tenant-scope.md`](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-vault-tenant-scope.md). Google Secret Manager deployments are unaffected. |
| **v0.22.0** | First release to ship the backplane startup probe in the chart. | If you ever hand-patched a `startupProbe` onto the Deployment, this is the textbook field-ownership conflict above — pre-flight and use `--force-conflicts`. |
| **v0.31.0** | The MCP tool surface became claim-scoped: a default session lists and dispatches only the 25-tool working surface; the operator-plane tools require the `mcp:admin` OAuth scope, and the approval-decision verbs have no MCP path at all. | Agent sessions that used operator-plane tools break until they request elevation: add `mcp:admin` as an **optional** (request-only) client scope on the MCP client in your realm, then opt sessions in with `MEHO_MCP_SCOPES=mcp:admin` (Claude Code plugin) or the `.mcpb` `scopes` user config. Approval decisions move to the operator console or CLI. The migrations are additive-only; no schema action. |
| **v0.32.0** | A new, most-restrictive `destructive` safety tier lands (`safe < caution < dangerous < destructive`), excluded by default at every gate and reachable only through the governed-delete approval flow (a preview-hash-bound call plus a mandatory blast-radius statement). Several permanent-delete operations adopt it. | Automation, agents, or service principals that previously called those operations directly are now refused — a human must approve each governed delete on the console or CLI. No config change is needed to adopt the safer default; the migrations are additive-only, so no manual schema step. |
| **v0.39.0**, **v0.40.0** | One person can be in several tenants. From v0.39.0 the Keycloak setup adds the `meho_tenant` attribute to the role groups and the `meho-tenants` mapper, so the login token lists the person's tenants (`meho_tenants`). From v0.40.0 MEHO reads this list and works in one current tenant per request. | **A site with one tenant needs to do nothing.** Without the list, MEHO keeps the old path. To add the list, run the setup again: `meho admin keycloak bootstrap-clients` with the same `--tenant-id` as before (your own Keycloak), or `helm upgrade` (the built-in Keycloak). The token then lists one tenant, and MEHO answers exactly as before, with no `Meho-Tenant` header. To add a second tenant, follow [One person in several tenants](../guides/several-tenants.md). |
| **v0.42.0** | On the command line and REST, only a token of the approval client `meho-cli-approve` approves a request (chart `config.keycloakCliApproveClientId`). The `meho-web` client needs the `basic` and `acr` default scopes. | **Built-in Keycloak:** the upgrade adds the client and the scopes. Create the login flow `meho approval sign-in` by hand, unless the company login is on. **Your own Keycloak:** create `meho-cli-approve` by hand, give `meho-web` the `basic` and `acr` default scopes, and give `meho-cli` the optional `offline_access` scope ([The approval client](keycloak-realm.md#the-approval-client-meho-cli-approve)). Do not run `meho admin keycloak bootstrap-clients` for this on an existing realm. Install the v0.42.0 CLI. |
| **v0.44.0** | The public image has no VMware connectors. They come as a separate, signed connector package that runs on top of the public image. The chart turns on request limits per person, counted in the chart's Valkey store (`broadcast.enabled`, on by default). `/metrics` and the `features` block of `/ready` need the metrics token from the Secret `<release>-metrics-auth`. | **VMware users:** run an overlay image built on this release that adds the VMware connector package. Keep `broadcast.enabled` on, or set `config.dispatchRateLimitPerMinute`, `config.dispatchMaxConcurrentOps` and `config.mcpSessionStartLimitPerMinute` to `"0"`. Give any scraper other than the chart's ServiceMonitor the token from `<release>-metrics-auth`. |
| **v0.45.0** | The chart installs the built-in PostgreSQL, Keycloak and OpenBao by default: `corePostgres.enabled`, `coreKeycloak.enabled` and `coreVault.enabled` are `true`. The chart connects MEHO to them by itself: the database login, the OpenBao address and login role, and the key for credential writes that wait for approval. With the built-in OpenBao, each tenant reads and writes only `secret/tenants/<tenant id>/`. | **An install that brings its own PostgreSQL, Keycloak or Vault:** before the upgrade, set the matching key to `false` for each part you bring yourself, for example `--set corePostgres.enabled=false --set coreKeycloak.enabled=false --set coreVault.enabled=false`. Without these keys, Helm stops with an error that names the key. **An install that uses the built-in parts:** remove `vault.address` and `config.vaultAddr` from your values, because the chart sets the OpenBao address itself. Leave `postgres.credentialsSecret` empty, or set it to `<release>-core-postgres-meho`. |
| **v0.45.0** | Every call made with a token now runs only `safe` operations at once: MCP, the command line and REST, also with a person's own token, and every agent (#3980). `caution`, `dangerous` and `destructive` changes sent this way wait for an approval click. In the web interface, `caution` still runs at once, and `dangerous` changes now wait for an approval click too. An agent's `dangerous` or `destructive` request now waits for a person's approval instead of being refused. Service principals are unchanged: their standing grants keep working. Who may approve follows the login groups (since v0.44.0): only a MEHO-SA-Operator or a MEHO-Admin approves another person's `dangerous` or `destructive` change, and only a MEHO-SA-Operator approves their own. | **Before the upgrade**, put at least one person in `MEHO-SA-Operators` ([Keycloak realm setup](keycloak-realm.md#who-may-approve-their-own-changes)). Otherwise nobody can approve their own `dangerous` or `destructive` change, and on a site with a single admin these changes wait with nobody to approve them. Tell command-line, MCP and REST users that they will see more waiting requests. They approve them in the web interface, or with `meho approvals approve` after a fresh sign-in (with an authenticator code when your realm asks for one). A person, for example a MEHO-Operator, may approve their own `caution` change that waited only because it came with a token, with a click in the web interface. Scripts that run `caution` changes with a token now get a waiting request (`awaiting_approval`) instead of a result; give fixed automation a service principal with a standing grant. See [Approvals and break-glass](../guides/approvals-and-break-glass.md). |
| **v0.45.0** | When a connector states the target versions its operations work on, MEHO runs an operation only on a target inside that range. MEHO checks this each time the operation runs. Outside the range, the call is refused with `unqualified_target_version`, and the error names the range and the target's version. For an operation from an API file, MEHO first looks for the same operation on the connector version that matches the target, and runs it there when it exists and is turned on. MEHO checks the range for operations from an API file and for multi-step operations. From v0.45.0 the VMware connectors state the target versions their multi-step operations work on, so MEHO now applies the check to them. A Sensor on a connector whose range does not include the target's version can read `unknown`. A scheduled job or another saved call on such a connector is refused. | **Before the upgrade**, list your Sensors with `meho sensor list --json` and check that each one uses the connector that matches its target's version. For example, use `vmware-rest-8.0` for a vCenter 8.0.x target and `vmware-rest-9.0` for a vCenter 9.0.x target. A Sensor cannot be changed, so delete a wrong one and create it again on the right connector. Check scheduled jobs and other saved calls the same way. See [Pick the connector version that matches the target](../guides/sensors-quickstart.md#pick-the-connector-version-that-matches-the-target). |
| **v0.46.0** | The key of the hashes and fingerprints in audit rows and approval requests is a secret of its own (`paramsHashKey` in the chart). With a built-in part, the install makes it in the Secret `<release>-core-params-hash` and keeps it on every upgrade. A new database password then keeps every hash. New audit hashes do not match the ones made with the key from the database URL. | **Built-in core:** nothing to do. **No built-in part:** store at least 32 random characters in a Kubernetes Secret and set `paramsHashKey.secretName`. A request that waits for approval while you set the key still matches its own params. Without it, MEHO makes the key from the database URL, and a new database password changes it. To rotate the key, follow "Rotating `PARAMS_HASH_KEY`" in [`docs/codebase/approvals.md`](https://github.com/evoila/meho/blob/main/docs/codebase/approvals.md). |
| **v0.46.0** | The chart uses the `Recreate` update strategy: the old pod stops before the new one starts. An install made with an older chart has `RollingUpdate` and a `rollingUpdate` block on its live Deployment. | **Nothing to do.** Before a Helm 4 upgrade from such an install, the chart runs a short Job that removes the block. If an upgrade already failed with `spec.strategy.rollingUpdate: Forbidden`, see [Upgrade fails with `spec.strategy.rollingUpdate: Forbidden`](#upgrade-fails-with-specstrategyrollingupdate-forbidden). |
| **v0.46.0** | Fifteen database migrations (`0138` to `0152`). They only add, and change no existing row. The upgrade ends every approval request that still waits. With the built-in Keycloak, four chart values must be empty or equal the chart's value: `config.uiKeycloakClientId`, `checkRunner.clientId`, `keycloakAdmin.url` and `keycloakAdmin.clientId`. The retrieval quality check and the retire checklist are removed. Machine logins no longer write targets or topology over REST. An operation from an API file runs only on a connector class that accepts such operations (`accepts_ingested_operations`); otherwise MEHO answers `ingested_ops_not_accepted`. | Back up the database, and decide waiting approval requests first. If you use connector packages, update them before this version or together with it, to a version that sets `accepts_ingested_operations = True` where it runs operations from an API file. With the built-in Keycloak, remove the four values or set the value the error message names. Machine logins use the operations `targets.register`, `targets.update`, `targets.remove` and `topology.*` through `call_operation`. On OpenShift, set the UBI OpenBao image in `coreVault.image`. Details: [CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md). |
| **v0.47.0** | With the built-in Keycloak, the bootstrap Job keeps the approval sign-in flow and its binding to `meho-cli-approve` up to date on every upgrade. It needs the role `manage-realm` on `meho-admin`, and it stops when the role is missing. `/api/v1/health` shows the state of the approval sign-in (`approvals.sign_in`), and `meho doctor` shows it in one line. | **Built-in Keycloak, first started before v0.47.0 with the company login off:** give `meho-admin` the role `manage-realm` before the upgrade, as [The bootstrap Job needs `manage-realm`](#the-bootstrap-job-needs-manage-realm) says. Otherwise the upgrade stops at the Job. **Your own Keycloak:** give the backplane's Keycloak admin client the realm-management role `view-realm`, so MEHO can check the approval sign-in, and make sure the flow is set up ([The approval client](keycloak-realm.md#the-approval-client-meho-cli-approve)). |
| **v0.47.0** | An approval sign-in carries its own level. The approval client `meho-cli-approve` signs in through the new login flow `meho approval sign-in level`, which gives the level `meho-approve` (level 2 in the realm attribute `acr.loa.map`). The new setting `APPROVAL_SIGN_IN_LEVEL` (chart `config.approvalSignInLevel`: `off`, `observe` or `required`, default `observe`) says whether an approve on the command line or REST needs this level. `/api/v1/health` shows it in `approvals.sign_in_level`. In the next release the default becomes `required`. | **Built-in Keycloak:** the bootstrap Job makes the flow, names the level and binds the client. **Your own Keycloak:** add the level and the flow by hand or with `keycloak.authflow.ensure_approval_flow` ([The approval level](keycloak-realm.md#the-approval-level)). Check that `approvals.sign_in.state` is `ok` on every install before the next release. **With a company login in front of Keycloak:** set `config.approvalSignInLevel: "required"` now. A person with only a company account then approves in the web interface. |

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
the Valkey activity feed is ephemeral by design. For the knowledge store,
see [Back up and restore the knowledge store](#back-up-and-restore-the-knowledge-store).

A PostgreSQL restore alone does not rebuild a working deployment. The
target credentials themselves live in Vault or Google Secret Manager,
realm and client configuration lives in Keycloak, and your TLS Secrets
and Helm values live in the cluster and in whatever you keep them under
version control in. Each needs its own backup or reprovisioning path
until the full guide lands.

## Back up and restore the knowledge store

MEHO keeps its knowledge in the backplane database (PostgreSQL). One
database backup holds all of it. There is no other copy.

### What the database holds

| What | Where | What it is |
|---|---|---|
| Entries | table `documents`, rows with `source = 'kb'` | One row per entry. It holds the latest text and the metadata. |
| Revisions | table `kb_revision` | One row per stored text of an entry. Older texts stay readable here. |
| Passages and embeddings | table `documents`, rows with `source = 'kb-passage'` | The pieces of text that search ranks, with their embeddings. An embedding is a list of numbers for search by meaning. It is stored with the `pgvector` extension. |

The tenant settings file is **not** a backup of the knowledge store. It
holds policies, conventions and targets, and no knowledge. See
[Backup and the settings](ways-to-install.md#backup-and-the-settings).

### Take a backup

Run `pg_dump` against the backplane database, with the custom format.
Run it from a machine or pod that has the PostgreSQL client tools. Use
the same major version as the server, or a newer one:

```bash
pg_dump --format=custom --file=meho-backplane.dump \
  "postgresql://<user>@<host>:5432/<database>"
```

Store the file somewhere that survives the loss of the server. Take the
backup on a schedule, and take one before each upgrade.

### Restore into an empty database

1. Create an empty database on a PostgreSQL server where the `vector`
   extension is available (the database image of the MEHO chart has it).
2. Restore the dump into it:

   ```bash
   createdb -h <host> -U <user> <new-database>
   pg_restore --no-owner --exit-on-error \
     --dbname="postgresql://<user>@<host>:5432/<new-database>" \
     meho-backplane.dump
   ```

   The dump contains the line that creates the `vector` extension, so
   the restore creates it. If your database user may not create
   extensions, ask a database administrator to run
   `CREATE EXTENSION vector;` in the new database first, and leave out
   `--exit-on-error`. `pg_restore` then reports one "already exists"
   error for that line. It is safe to ignore.
3. Point MEHO at the new database (the `DATABASE_URL` setting) and start it.

A restore of the database alone does not bring back credentials,
Keycloak or the Helm values. See the notes above.

### Check after a restore

Pick one entry that you know:

1. **The entry:** `meho kb show <slug>` returns the latest text.
2. **Its revisions:** in the new database, this lists every stored text:

   ```sql
   SELECT r.revision_number, left(r.body, 60)
   FROM kb_revision r JOIN documents d ON d.id = r.entry_id
   WHERE d.source = 'kb' AND d.source_id = '<slug>'
   ORDER BY r.revision_number;
   ```

   An agent reads one older text through the MCP resource
   `meho://kb/id/<entry>/revision/<revision>`.
3. **A search hit:** `meho kb search "<words from that entry>"` lists
   the entry.

A test in the MEHO code (`test_kb_backup_restore_pg.py`) takes a dump
with `pg_dump`, restores it with `pg_restore`, and checks the same three
things.

### If the restored MEHO uses another embedding model

The dump keeps the embeddings that the old model made. Search compares
the question, which the new model embeds, with these stored numbers.
Numbers from two different models do not match well, so search results
get worse.

To fix it, write each entry again with the same text. MEHO sees that
the passages came from another model and builds them again. The
easiest way is `meho kb ingest` over the source files of your entries.
An entry that has no source file can be written again with
`meho kb add <slug> --body @<file>`.

A new model must give vectors of the same size (384 numbers). The
database column has that size fixed. A model with another size needs a
database migration first.
