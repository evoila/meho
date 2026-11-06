# The settings file (`meho-settings.yaml`)

MEHO keeps some of its settings in its own database: per tenant, the
policies, the conventions and the targets. The settings file hands these out
as one file and takes such a file back in. Before it applies a file, MEHO
checks it, explains every mistake in one plain sentence, and shows exactly
what will change. Every applied import is kept as a revision: who, when, and
what changed.

The design is the internal note `core.md` §5. The code is in
`backend/src/meho_backplane/instance_settings/` (`schema.py`, `diff.py`,
`service.py`) and `backend/src/meho_backplane/api/v1/instance_settings.py`.
The command-line verbs and the console page are a separate Task (#4048).

Do not confuse this with `backend/src/meho_backplane/settings.py`: those are
the environment variables, read once at start. They are never in the file.

## Where the file is used

- A rebuild, a restore (#3960), a move to another install form, a lab copy
  of a site, and a file prepared before install day all start from it.
- A full restore also needs the OpenBao backup, the generated Kubernetes
  Secrets and the PostgreSQL dump. The file alone is not a backup.
- The first day (#3972) reads the systems to add from it.

## The file

```yaml
apiVersion: meho.evoila.com/v1
kind: Settings
metadata:                      # written by export, ignored by import
  exportedAt: 2026-10-14T12:00:00Z
  exportedFrom: { mehoVersion: v0.35.22, migrationHead: "0136" }
spec:
  tenants:
    - id: 4f0c…                 # the tenant's Keycloak id (UUID)
      policies:
        announce_gate_enabled: false
        flight_recorder: { enabled: true, retention_days: 14, agent_readable: null }
        mail_recipient_allowlist: "@example.com"
      conventions:
        - slug: change-window
          title: Change window
          body: Writes only between 20:00 and 06:00.
          kind: operational
          priority: 5
      targets:
        - name: vc-01
          product: vsphere
          version: "8.0"
          host: 10.1.0.1
          port: null
          fqdn: null
          aliases: []
          auth_model: shared_service_account
          verify_tls: true
          tls_ca_pin: null
          tls_server_name: null
          vpn_required: false
          preferred_impl_id: null
          flight_recorder_capture: null
          notes: null
          extras: {}
```

### The frame

- `apiVersion` is the file format. This release reads and writes
  `meho.evoila.com/v1`; any other value is refused with one sentence.
- `kind` is always `Settings`.
- `metadata` is written by export and ignored by import, with one exception:
  when `metadata.exportedFrom` names a newer MEHO version (from `GET
  /version`) or a newer migration than this install, the preview gives one
  warning sentence. The import still runs.
- `spec.tenants[]` holds one block per tenant. Export always writes `id`. In
  a file with one tenant, imported by that tenant's `tenant_admin`, `id` may
  be left out.

### Tenant block

- `policies` — the per-tenant policy columns on `tenant`:
  `announce_gate_enabled`, `flight_recorder.{enabled, retention_days,
  agent_readable}`, `mail_recipient_allowlist`. `null` means "use the
  instance default" (`false` for the two switches, inherit for the others).
  `policies: null` resets all of them.
- `conventions[]` — keyed by `slug`. `title` and `kind` are needed when the
  convention is new. `kind` cannot change on import, the same as on
  `PATCH /api/v1/conventions/{slug}`; delete the convention and import it
  again. The preamble budget check of the conventions routes applies.
- `targets[]` — keyed by `name`; the keys of `TargetCreate` (the same entry
  shape as `meho targets export`, #4048). `name`, `product` and `host` are
  required. `extras` is a mapping with named keys. `secret_ref` is a path,
  never a password: export leaves it out when MEHO can work it out from the
  name (`tenants/<tenant_id>/<name>`), and writes `secret_ref: null` for a
  target with no credential. An explicit `secret_ref` must sit under the
  tenant's own path (the same check as `POST /api/v1/targets`).
  `secret_field_map` (#3711) maps the field names a connector reads to the
  field names in the secret; names only, never values. The file holds the
  whole map: an import removes stored entries the file leaves out, and an
  empty map clears it. The product, `preferred_impl_id`, SSRF and TLS checks
  of the target routes all apply.

### What an import does with each key

- A key in the file sets the value.
- A key **not** in the file leaves the stored value unchanged. A section not
  in the file (`policies`, `conventions`, `targets`) leaves the whole section
  unchanged. For a new item, a key not in the file gets the same default the
  REST route gives it.
- An item in MEHO but not in the file is reported as "not in file" and left
  alone. **Nothing is ever deleted.** (A later `--prune` is its own Task.)

## Never in the file

The schema refuses unknown keys, so none of these can be added by hand; the
secret check and the CI check (below) catch secret-shaped values anywhere.

- The 15 settings that carry secrets: `DATABASE_URL`,
  `BROADCAST_REDIS_PASSWORD`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `KEYCLOAK_ADMIN_CLIENT_SECRET`, `UI_KEYCLOAK_CLIENT_SECRET`,
  `UI_SESSION_ENCRYPTION_KEY`, `APPROVAL_HANDOFF_ENCRYPTION_KEY`,
  `CHECK_RUNNER_CLIENT_SECRET`, `VAULT_SCHEDULER_TOKEN`,
  `SATELLITE_WRITE_SIGNING_KEY`, `VCF_PAIF_OIDC_CLIENT_SECRET`,
  `MAIL_SMTP_PASSWORD`, `METRICS_AUTH_TOKEN`, `CORPUS_SERVICE_TOKEN`.
- Every generated `<release>-core-*` Kubernetes Secret (#4041, #4043, #4044).
- The OpenBao unseal key and root token.
- The TLS private key and the certificate chain. The certificate is a
  setting of the machine or cluster.
- Target passwords. Only `secret_ref` paths.
- The machine name, IP address, DNS and NTP servers; ingress class, storage
  class, host names, network ranges. These live in the install form.
- Who is in which Keycloak group, and the realm setup. These move only with
  the stretch Task #4049. Adding someone to an approving group is a
  privilege grant (#3325), so v1 carries no approver list.
- Fields the server manages: `id` (of a target or convention), `tenant_id`,
  `fingerprint`, `created_at`, `updated_at`, `deleted_at`.
- Settings that exist only as environment variables (the per-tenant maps,
  `APPROVAL_ALLOW_SELF_APPROVAL`).

## Checks on the file

Every problem is one sentence that names the path, for example
``"`spec.tenants[0].targets[3].host` is required."``. A preview lists all of
them at once.

- The file must be UTF-8 YAML (JSON is valid YAML), at most 4 MiB.
- Every unknown key is a mistake, at every level. A key repeated in the same
  block is a mistake (plain YAML loading would silently keep the last one).
  Two tenants with one `id`, or two conventions / targets with one key in a
  tenant, are mistakes.
- `tenants[].id` must be a UUID. Booleans must be `true` / `false`, numbers
  must be whole numbers (no `"yes"`, no `"14"`).
- Every string, and every key under `extras`, is checked for a full
  `PRIVATE KEY` PEM block, a JWT, the same after one base64 decode, and
  base64 that decodes to a raw or DER key. A `CERTIFICATE` block passes, so
  `tls_ca_pin` works.
- Under `extras`, a key named like a credential (`password`, `api_key`,
  `clientSecret`, `token`, ...) is refused whatever its value: an ordinary
  password has no shape to find. A key that only names something about a
  token or secret (`token_url`, `secret_path`) passes. The name rule is
  `is_secret_param_name` in `broadcast/events.py`, the same one the broadcast
  scrub and the approval echo use.
- A mask such as `*`, `********` or `<keep>` is refused. It never means
  "keep the stored value".
- Once a secret is found, nothing else is checked: no later check, and not
  the import, ever handles the value. A key name that looks like a secret is
  shown as `<redacted>` in every sentence.
- A value YAML cannot convert, such as the date `2026-99-99`, is a mistake
  sentence with its line, never a server error.
- A YAML syntax error, or an unknown tag, gives only its line: PyYAML's own
  message can quote the input, and nothing has been checked for secrets yet.
- YAML anchors (`&name`) and aliases (`*name`), merge keys with an alias
  included, are refused with their line: "The file is not valid YAML on
  line N: anchors (`&`) and aliases (`*`) are not accepted; write each value
  out." The file is refused before any node is built, so no later check runs.
  The anchor name is never shown back.
- The file is parsed and checked in a worker thread, not on the event loop,
  so a large file does not hold up other requests.
- `!!binary`, `!!pairs`, `!!omap` and `!!set` values are refused and never
  built: each would hide its contents from the secret check and reach JSON
  as readable text. Nothing else is checked, as with a secret.
- Stored data is checked too. When a tenant the file names already has a
  secret-shaped value stored (a target's `extras.password`, a JWT in
  `notes`, ...), the preview and the import stop with that path
  (`stored.<tenant>.targets.<name>.extras.password`, never the value): a
  diff shows "before" values and a revision keeps them for good. Fix the
  target or convention through its own route, then import.
- A refused value is never shown back: the models set
  `hide_input_in_errors=True`, and sentences are built from the path and the
  error type only.

## Who may call

All four endpoints need a login; without a token they answer 401. There is
no page before login: the first admin from #4043 signs in and uses the
settings page (#4048) or `meho settings import`.

| Caller | Export | Preview / import | History |
|---|---|---|---|
| `tenant_admin` | own tenant | a file with only their own tenant | revisions that touched only their tenant |
| `platform_admin` | every tenant, or `?tenant_id=` | any file | every revision |
| any other role | 403 | 403 | 403 |

- The preview has the same rules as the apply.
- The import is a human-only verb: a machine principal (agent, service,
  runner) gets 403 `human_principal_required`. So a revision's `actor_sub`
  is always a real person.
- Open, decided by 2026-10-24 (#3325 rule 1): whether an import from the
  command line writes at once, as the target routes do today, or waits for
  someone to approve it in the web console. Today it writes at once.

## Endpoints

### `GET /api/v1/settings/export`

Builds the file from the live state (`application/yaml`, sent as
`meho-settings.yaml`). Before the file is written, the same secret check runs
on it: stored data that would be refused on import (a JWT in a target's
`notes`, `extras.password`, a private key in a convention body) stops the
export with 409 `settings_export_refused` and a `mistakes` list naming each
path, never the value. Move the secret into OpenBao and export again. Tenants are sorted by id, conventions by slug, targets
by name, and keys come in a fixed order, so the same state always gives the
same `spec`. The export writes each value out each time it appears
and never writes a YAML anchor or alias, so an exported file always imports
again.

### `POST /api/v1/settings/import?preview=true`

Body: the file as sent. Reads the database and nothing else: it writes
nothing and calls no outside system. Always 200 (403 for a file the caller
may not import):

```json
{
  "preview": true,
  "ok": false,
  "mistakes": ["`spec.tenants[0].targets[3].host` is required."],
  "warnings": [],
  "diff": null,
  "applied": false,
  "revision_id": null,
  "failed": []
}
```

When there are no mistakes, `diff` holds one entry per section — `tenants`,
`policies`, `conventions`, `targets` — each with three lists:

- `added`: `{tenant, key, value}` (for `tenants`: `{tenant}`);
- `changed`: `{tenant, key, before, after}` — for conventions and targets,
  `before` and `after` hold only the keys that change; for policies `key` is
  the dotted name (`flight_recorder.retention_days`);
- `not_in_file`: `{tenant, key}` — information only; nothing is deleted.
  `tenants.not_in_file` is filled only for a `platform_admin`.

This is the answer `meho settings import --preview --json` (#4048) prints.

### `POST /api/v1/settings/import`

Runs the same checks. With any mistake it answers 422 with the preview's
body and writes nothing. Otherwise it adds or updates, never deletes:

- One database transaction, in this order: tenants (`ensure_tenant`),
  policies, conventions, targets.
- A savepoint per item, so one failed item does not spoil the whole import.
  Items that could not be written are listed in `failed` (one sentence
  each), and `ok` is then `false`; everything else is written.
- The writes go through the same code as the REST routes:
  `tenancy/policies.py` for the tenant columns (`announce_gate_enabled` has
  no route of its own and is written there, audited), `ConventionsService`
  for conventions, and `create_target` / `update_target` for targets.
- One `audit_log` row covers the whole import (`op_id=settings.import`). Its
  `op_class` is `write` only when something is written; a refused or
  unchanged file leaves a `read` row. The revision row keeps its id. Every
  `tenant_convention_history` row the import writes keeps the import's
  `request_id` (convention history no longer points at `audit_log`, #3747).
- `diff` in the answer is what actually changed; `revision_id` is the new
  revision.
- An unchanged file gives an empty diff and writes no revision.

### `GET /api/v1/settings/history?limit=50`

The revisions the caller may see, newest first: `id`, `ts`, `actor_sub`,
`audit_id`, `file_sha256`, `api_version`, `exported_from`, `summary` and
`diff` (worked out from `spec_before` and `spec_after` when read, without
`not_in_file`).

## The table: `instance_settings_revision`

Migration `0136`. Append-only: on PostgreSQL a `BEFORE UPDATE OR DELETE`
trigger refuses any change (a copy of the `audit_log` trigger from `0100`);
on SQLite it is append-only by convention.

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | the revision |
| `ts` | timestamptz | when |
| `actor_sub` | text, not null | who; always the real person |
| `audit_id` | uuid | the import's `audit_log` row |
| `file_sha256` | text | digest of the file bytes |
| `api_version` | text | the file's `apiVersion` |
| `exported_from` | jsonb, null | the file's `metadata.exportedFrom` |
| `spec_before` | jsonb | `{"tenants": [...]}` for the touched tenants, before |
| `spec_after` | jsonb | the same, after |
| `summary` | jsonb | `{section: {added, changed}}` |

## CI check

`scripts/ci/check-settings-export-secrets.sh <file>` greps an export for JWT,
password-key and PEM private-key patterns and prints only line numbers.
`backend/tests/test_api_v1_settings.py` runs it on a live export of seeded
targets on every CI run. Run it by hand on any export:

```bash
scripts/ci/check-settings-export-secrets.sh meho-settings.yaml
```
