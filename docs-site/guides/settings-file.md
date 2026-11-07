# The settings file: export, import and history

MEHO keeps some settings of each tenant in its own database: the
policies, the conventions and the targets. The settings file
(`meho-settings.yaml`) puts these settings into one file. You can:

- **export** the file, for example to keep a copy or to move the
  settings to another MEHO,
- **import** a file, and first see a **preview** of every change,
- read the **history**: who changed what, and when.

The file never holds a secret. Passwords and keys stay in your secret
store.

!!! note "REST only"

    The command line and the web console do not have it yet. Today you
    use the settings file through the REST API, as shown on this page.

## Who may use it

Every call needs a login. Without a token MEHO answers `401`.

| Caller | Export | Preview and import | History |
|---|---|---|---|
| `tenant_admin` | their own tenant | a file with only their own tenant | the changes that touched only their own tenant |
| [platform admin](several-tenants.md#the-platform-admin-role) | every tenant, or one tenant with `?tenant_id=` | any file | every change |
| any other role | `403` | `403` | `403` |

Only a person can preview or import a file. An agent, a service or a
runner gets `403` with `human_principal_required`. So every change in
the history names a real person.

To call the API from a shell, get a token with the CLI. The value is a
live login: do not log it or share it.

```bash
TOKEN=$(meho login --print-token https://meho.example.com)
```

## What is in the file

```yaml
apiVersion: meho.evoila.com/v1
kind: Settings
metadata:                      # written by export, ignored by import
  exportedAt: 2026-10-14T12:00:00Z
  exportedFrom: { mehoVersion: v0.45.0, migrationHead: "0138" }
spec:
  tenants:
    - id: <tenant-uuid>
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
        - name: <target-name>
          product: vsphere
          version: "8.0"
          host: <host>
          verify_tls: true
          secret_ref: null
          extras: {}
```

- `spec.tenants` holds one block per tenant. Each block has three
  parts:
    - `policies`: the tenant's switches, such as the flight recorder
      and the mail allow list. `null` means "use the default".
    - `conventions`: the tenant's conventions, one per `slug`.
    - `targets`: the tenant's targets, one per `name`. A target has the
      same fields as when you register it (see
      [Register targets and secrets](targets-and-secrets.md)).
- A target carries no password. `secret_ref` is only the **path** to
  the secret in your secret store, never its value. Export leaves
  `secret_ref` out when MEHO can work out the path from the target
  name.
- `metadata` is written by export and ignored by import. One
  exception: when the file comes from a newer MEHO than yours, the
  preview shows a warning. The import still runs.
- When a `tenant_admin` imports a file with only their own tenant, the
  tenant `id` may be left out.

### What is never in the file

- Passwords, tokens and keys of any kind.
- The settings MEHO reads from environment variables when it starts.
- Who is in which Keycloak group, and the Keycloak realm setup.
- The settings of the machine or cluster: host names, IP addresses,
  certificates, storage and network setup.
- Fields that MEHO manages itself, such as ids and timestamps.

The file alone is not a full backup. To restore a whole MEHO you also
need the database backup, the backup of your secret store and the
Kubernetes Secrets the install made.

## Export

```bash
curl -H "Authorization: Bearer $TOKEN" \
  -o meho-settings.yaml \
  https://meho.example.com/api/v1/settings/export
```

- A `tenant_admin` gets their own tenant.
- A platform admin gets every tenant. Add `?tenant_id=<tenant-uuid>`
  to get one tenant.
- The same settings always give the same `spec`: tenants, conventions
  and targets come in a fixed order.

Before MEHO writes the file, it checks the stored settings for
anything that looks like a secret (for example a password in a
target's `extras`, or a key in a convention text). When it finds one,
export answers `409` with `settings_export_refused` and a `mistakes`
list. Each entry names the place, never the value. Move the secret
into your secret store, then export again.

## Import with a preview

Always run the preview first. It checks the file and shows every
change. It saves nothing.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/yaml" \
  --data-binary @meho-settings.yaml \
  "https://meho.example.com/api/v1/settings/import?preview=true"
```

The answer is JSON:

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

- `mistakes` lists every problem in the file at once. Each problem is
  one sentence that names the place in the file.
- When there are no mistakes, `ok` is `true` and `diff` shows the
  changes. `diff` has one part for each of `tenants`, `policies`,
  `conventions` and `targets`. Each part has three lists:
    - `added`: new items.
    - `changed`: the value before and after, only for the fields that
      change.
    - `not_in_file`: items that MEHO has but the file does not. This is
      for your information only. MEHO leaves them as they are.

When the preview looks right, send the same file without
`?preview=true`:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/yaml" \
  --data-binary @meho-settings.yaml \
  https://meho.example.com/api/v1/settings/import
```

What the import does:

- **It adds and updates. It never deletes.** An item that is in MEHO
  but not in the file stays as it is.
- A field in the file sets the value. A field not in the file keeps
  the stored value. A part not in the file (for example no `targets`)
  keeps that whole part as it is.
- It runs the same checks as the preview. With any mistake it answers
  `422` with the preview's answer and saves nothing.
- It saves each item through the same rules as the normal API for
  targets and conventions. When one item fails, MEHO saves the others.
  The failed items are in `failed`, and `ok` is then `false`.
- The answer's `diff` shows what really changed, and `revision_id` is
  the id of the new entry in the history.
- A file that changes nothing gives an empty `diff` and no new history
  entry.
- One audit row covers the whole import.

### What the checks refuse

- A file larger than 4 MiB (`413`). The file must be YAML in UTF-8.
  JSON works too, because JSON is valid YAML.
- Any field MEHO does not know, at every level, and a field that
  appears twice in the same block.
- Anything that looks like a secret: a private key, a token, or a
  field in a target's `extras` named like a password or a key, whatever
  its value.
- A mask such as `********` or `<keep>`. A mask never means "keep the
  stored value": leave the field out instead.
- An `apiVersion` other than `meho.evoila.com/v1`.

MEHO never shows a refused value back. The sentence names only the
place in the file.

When a tenant in the file already has a secret-like value stored in
MEHO, the preview and the import stop and name that place. Fix the
target or convention through its normal API first, then import again.

## History

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://meho.example.com/api/v1/settings/history?limit=50"
```

The answer lists the imports you may see, newest first. `limit` is
between 1 and 500; the default is 50. Each entry has:

- `ts`: when the import ran.
- `actor_sub`: who ran it. This is always a person.
- `diff`: what changed, with the values before and after.
- `audit_id`: the audit row of the import (see
  [Audit forensics](audit-forensics.md)).
- `file_sha256`, `api_version` and `exported_from`: which file was
  imported.

The history is append-only: MEHO never changes or deletes an entry.
