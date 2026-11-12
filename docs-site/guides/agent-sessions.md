# Agent sessions

MEHO can keep a copy of the agent sessions that people upload. A session
is the record of one agent conversation. For Claude Code, that is the
transcript file `<session-id>.jsonl` and the folder `<session-id>/` next
to it.

MEHO stores the files in S3-compatible object storage that you bring. That
is a file store with the Amazon S3 interface, for example MinIO. MEHO
writes one index row per session in its own database.

This page shows how to turn the store on with Helm values, how a person
uploads sessions from a laptop, who may do what, and how long MEHO keeps
a session.

!!! note "Prerequisites"

    - A MEHO install from the Helm chart ([Install](../install/index.md)).
    - An S3-compatible object storage, a bucket in it, and an access key
      that may read, write and delete objects in that bucket.
    - A Vault (or the built-in OpenBao) that MEHO reads with its scheduler
      token ([Scheduler](scheduler.md)).

## What MEHO keeps, and what it never keeps

MEHO keeps:

- the files of each upload, after a secret check, in your bucket;
- one index row per session: tenant, session id, who uploaded it,
  project, app, start and end time, state (`open`, `ended` or
  `unfinished`) and the file hashes;
- one audit row for each upload, read and delete.

MEHO never keeps:

- **Secrets it finds.** Every file passes a secret check before it is
  stored. Known secrets, such as private keys, bearer tokens, JWTs and
  `password=...` pairs, are replaced with `[REDACTED:<pattern>]`.
- **File content in audit rows.** An audit row holds the session id,
  counts and hashes only.
- **A session after its retention period.** A cleanup job deletes it,
  with its files.
- **Sessions from a machine login.** Only a person can upload. An agent,
  service or runner login gets `403`.
- **The access key as a setting.** MEHO reads the key from Vault for every
  call to the store. A rotated key works at once.

When the secret check is not sure, the upload goes to quarantine. See
[Quarantine](#quarantine) below.

## Turn it on

The store is off until three values are set. Until then an upload, a
download and a delete answer `503 session_store_not_configured`.

### 1. Store the access key in Vault

Write the access key of the object storage to a Vault KV-v2 secret with
two fields, `access_key_id` and `secret_access_key`.

With the **built-in OpenBao**, use the path `secret/services/session-store`.
The scheduler token may read it. Write it with an admin login:

```bash
bao kv put secret/services/session-store \
  access_key_id=<access-key-id> \
  secret_access_key=<secret-access-key>
```

With **your own Vault**, pick any path, and give the scheduler token's
policy `read` on it. The setting takes the API path, with `data/` in it:

```hcl
# Add to the policy of the scheduler token (VAULT_SCHEDULER_TOKEN).
path "secret/data/services/session-store" {
  capabilities = ["read"]
}
```

!!! warning "Do not use a path under `secret/meho/`"

    Every MEHO user may read `secret/meho/*`. Keep the access key on a path
    that only the scheduler token reads.

### 2. Set the Helm values

```yaml
config:
  sessionStore:
    endpoint: "https://objects.example.com"
    bucket: "meho-sessions"
    credentialsVaultPath: "secret/data/services/session-store"
```

Then run `helm upgrade`. MEHO reads the values when a pod starts.

All values are strings. Write numbers and `true` / `false` in quotes. An
empty value keeps MEHO's default:

| Value | Default | Meaning |
|---|---|---|
| `endpoint` | — | URL of the object storage. Needed. |
| `bucket` | — | The bucket for the files. Needed. |
| `credentialsVaultPath` | — | Vault KV-v2 API path (`secret/data/...`) of the access key. Needed. |
| `region` | `us-east-1` | Region name sent to the store. |
| `retentionDaysDefault` | `90` | Days a session is kept when its tenant set no period. |
| `maxUploadBytes` | `26214400` (25 MiB) | Largest compressed upload. |
| `maxUnpackedBytes` | `268435456` (256 MiB) | Largest unpacked upload. |
| `maxFiles` | `2000` | Most files and folders in one upload. |
| `cleanupEnabled` | `"true"` | Run the cleanup job. |
| `cleanupIntervalSeconds` | `3600` | Seconds between cleanup runs (at least 60). |

A wrong value, such as `maxFiles: "many"`, stops `helm install` and
`helm upgrade` with a schema error.

### 3. Check it

The config map of the release now holds the three `SESSION_STORE_*`
variables:

```bash
kubectl get configmap <release>-config -o yaml | grep SESSION_STORE_
```

The first upload shows whether the store works:

- `503 session_store_not_configured`: a needed value is missing.
- `502 session_store_unavailable`: MEHO cannot reach the store, or cannot
  read the access key from Vault.

`meho sessions list` reads only MEHO's own index, so it works even
before the store is set up.

## Upload sessions from a laptop

A person sends the agent sessions of a laptop to MEHO with one command:

```bash
meho sessions push
```

### What is sent, and where it goes

- `push` reads a local folder, the **outbox**. A hook of the AI app
  copies each session into it (see [Fill the outbox](#fill-the-outbox)).
- It sends only sessions that were started in a **project folder on your
  allowlist**, or that you marked by hand. Every other session stays on
  the laptop and is never sent. An empty allowlist sends nothing.
- Before a session leaves the laptop, a **secret check** replaces known
  secrets with `[REDACTED:<pattern>]`: private keys, bearer tokens,
  `Authorization` headers, JWTs, secrets in links (the user part before
  `@`, and query values such as `token=` or `sig=`, also when the name is
  written with `%` codes), BIND key secrets (`secret "...";`),
  `password=...` style pairs, GitHub tokens and AWS access keys. MEHO
  checks every upload again.
- When the check is not sure, the session **stays in the outbox**, and
  `push` says why. This happens when a file is not UTF-8 text, when a
  name that looks like a secret still has a value (for example
  `"password": "..."`), when a private key has no `END` line, or when a
  BIND secret in quotes goes on to the next line.
- The upload goes to the MEHO of your `meho login`, with your normal
  login. It needs no extra key. MEHO stores it in your tenant.
- `push` sends the transcript `<session-id>.jsonl` and the files of the
  folder `<session-id>/`. It sends the project folder's last name as the
  project (for example `ops-runbooks`), the app version and the start
  and end time from the transcript.

### See what was sent

`push` prints one line per session: the session id, the project, the
state, the size of the upload, the result and MEHO's answer. The result
is one of `sent`, `already sent`, `not on allowlist`, `held back`,
`nothing to send yet` or `failed`. `--json` prints the same as JSON.

`meho sessions push --dry-run` shows the same list and sends nothing.

After each upload, `push` writes a receipt next to the session:
`<outbox>/<session-id>/meho-receipt.json`. It holds the session id, the
time, the MEHO address and tenant it was sent to, MEHO's answer, and for
each file how many bytes were sent and their SHA-256 hash.

An admin sees the uploaded session in `meho sessions list`.

### Run it again at any time

`push` is safe to run twice:

- A session with a receipt that matches its files is not sent again.
  The receipt counts only for the MEHO address and tenant it names: with
  another `--backplane`, another login or another tenant, `push` sends
  the session again.
- A failed upload writes no receipt. The next run sends the session
  again. When a session fails (it cannot be read or packed, or the
  upload fails), `push` ends with an error. MEHO stores an upload whole or not at all, and the same bytes
  sent twice change nothing.
- A session that is **still open** grows at the end. While a session is
  open, `push` sends only complete lines. When the files have grown since
  the last receipt, `push` sends the session again with the same id.
  MEHO stores only the new part, and the session stays `open` until the
  hook marks it as ended.

### Settings

The settings are in `sessions.json` in the meho config folder
(`$XDG_CONFIG_HOME/meho/` or `~/.config/meho/`):

```json
{
  "outbox": "~/.config/meho/session-outbox",
  "projects": [
    "/home/<user>/work/ops-runbooks"
  ]
}
```

- `outbox`: the outbox folder. The default is `session-outbox` in the
  meho config folder. `push --outbox <folder>` uses another folder once.
- `projects`: the allowlist. Each entry is a folder that a session was
  started in. A sub-folder is a different folder. Put only **operations
  projects** on the list: projects where people work on running
  systems. Never put a coding project on it. A session started in a
  coding project's folder is not sent, even when it did some operations
  work.

To send one session whose project is not on the list, mark it by hand:

```bash
touch <outbox>/<session-id>/meho-send
```

### Fill the outbox

Claude Code deletes its session files after 30 days by default. The
outbox keeps a copy until it is uploaded. Each session gets a folder:

| Path in the outbox | What it is |
|---|---|
| `<session-id>/<session-id>.jsonl` | The transcript. Needed. |
| `<session-id>/<session-id>/` | The session's folder (subagent transcripts, large tool outputs). |
| `<session-id>/meho-ended` | An empty file: the session ended. Without it, the session is still open. |
| `<session-id>/meho-send` | An empty file: you marked the session by hand. |
| `<session-id>/meho-receipt.json` | Written by `push`. |

A Claude Code hook can fill it. A hook is a small command that Claude
Code runs at a fixed moment. This script copies one session. It needs
`jq`:

```bash
#!/bin/sh
# Copy a Claude Code session into the MEHO outbox.
# Usage: copy-session.sh [ended]
input="$(cat)"
id="$(printf '%s' "$input" | jq -r .session_id)"
src="$(printf '%s' "$input" | jq -r .transcript_path)"
out="$HOME/.config/meho/session-outbox/$id"
mkdir -p "$out"
cp "$src" "$out/$id.jsonl"
if [ -d "${src%.jsonl}" ]; then
  mkdir -p "$out/$id"
  cp -R "${src%.jsonl}/." "$out/$id/"
fi
if [ "$1" = "ended" ]; then
  touch "$out/meho-ended"
fi
```

Run it at the end of each session (`SessionEnd`), and after each answer
(`Stop`) to keep a copy of sessions that stay open. In
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "~/bin/copy-session.sh ended", "timeout": 10}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "~/bin/copy-session.sh", "timeout": 10}]}
    ]
  }
}
```

A `SessionEnd` hook has 1.5 seconds by default; `timeout` gives it more.

## Who may do what

Each right maps to the lowest tenant role that has it. Only a person may
use the session routes.

| Right | Lowest role | How |
|---|---|---|
| Upload own sessions | `read_only` | `meho sessions push`, `POST /api/v1/sessions` |
| List sessions | `operator` | `meho sessions list`, `GET /api/v1/sessions` |
| Read a session | `operator` | `meho sessions show`, `GET /api/v1/sessions/{session_id}` |
| Delete a session | `tenant_admin` | `meho sessions delete`, `DELETE /api/v1/sessions/{session_id}` |
| Open an upload in quarantine | `tenant_admin` | `GET /api/v1/sessions/{session_id}/uploads/{seq}` |
| Read or set the retention period | `tenant_admin` | `GET` / `PUT /api/v1/sessions/retention` |

Every call sees only the caller's tenant. A session of another tenant
answers `404 session_not_found`.

## Retention per tenant

A session is old when its last upload is older than the tenant's
retention period. The cleanup job deletes old sessions with their files,
and writes an audit row for each.

A tenant admin sets the period for the tenant, from 1 to 3650 days:

```bash
curl -X PUT "https://<meho-host>/api/v1/sessions/retention" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"retention_days": 30}'
```

`{"retention_days": null}` goes back to the default
(`retentionDaysDefault`). `GET /api/v1/sessions/retention` shows the
period and whether it is the tenant's own (`"source": "tenant"`) or the
default (`"source": "default"`).

## Quarantine

The secret check is careful on purpose. It sends an upload to quarantine
when it is not sure, for example when:

- a file is not UTF-8 text;
- a name that looks like a secret still has a value, such as
  `"password": "..."` in JSON;
- a private key starts or ends in another upload.

A quarantined upload is stored, with the known secrets replaced. Only a
tenant admin can download it. An operator gets
`403 session_upload_quarantined`, and MEHO writes an audit row for the
refused download. `meho sessions show` marks the upload as in quarantine.

## Related

- The developer page with the full upload flow:
  `docs/codebase/session-store.md` in the source repo.
