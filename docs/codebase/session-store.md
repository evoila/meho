# Agent session store (#4901)

## Overview

MEHO keeps a copy of agent sessions that people upload. A session is the
record of one agent conversation: for Claude Code, the transcript file
`<session-id>.jsonl` and the folder `<session-id>/` next to it (subagent
transcripts and large tool outputs).

What is true with this feature:

- A person uploads one session as one compressed archive. MEHO checks every
  file for secrets, stores the result in S3-compatible object storage, and
  writes one index row per session.
- A session that is still open can be uploaded again and again. MEHO adds
  only the new bytes. The index row says whether the session is open,
  ended or unfinished.
- Admins list, read and delete sessions with `meho sessions list`, `show`
  and `delete`.
- A cleanup job removes sessions older than the tenant's retention period,
  with their files.
- Every upload, read and delete writes an audit row.
- A hosted agent run (a run that MEHO starts itself) gets one session too.
  It saves each of its events while it runs. See "Hosted agent runs"
  below.

S3-compatible object storage is a file store with the Amazon S3 interface,
for example MinIO. The customer brings its own.

Not in this part: the laptop uploader (`meho sessions push`), the link
from audit rows to a session, and a review page.

The code is in `backend/src/meho_backplane/session_store/` and
`backend/src/meho_backplane/api/v1/sessions.py`. The CLI is in
`cli/internal/cmd/sessions/`.

## Setup

The operator guide page `docs-site/guides/agent-sessions.md` shows the
setup. An operator sets the Helm values under `config.sessionStore`
(`endpoint`, `bucket`, `credentialsVaultPath`, `region`,
`retentionDaysDefault`, `maxUploadBytes`, `maxUnpackedBytes`, `maxFiles`,
`cleanupEnabled`, `cleanupIntervalSeconds`). The chart renders each set
value into its variable below (`templates/configmap.yaml`); an empty
value renders nothing, so the default here applies. The chart test is
`backend/tests/test_chart_session_store_values.py`.

The store is off until three environment variables are set. Until then
an upload, a download and a delete answer `503
session_store_not_configured`.

| Variable | Meaning |
|---|---|
| `SESSION_STORE_ENDPOINT` | URL of the object storage, for example `https://objects.example.com` |
| `SESSION_STORE_BUCKET` | The bucket (the top folder) for the files |
| `SESSION_STORE_CREDENTIALS_VAULT_PATH` | Vault KV-v2 API path (`secret/data/...`) with the fields `access_key_id` and `secret_access_key` |
| `SESSION_STORE_REGION` | Region name sent to the store (default `us-east-1`) |
| `SESSION_STORE_RETENTION_DAYS_DEFAULT` | Retention period of a tenant that set none (default 90) |
| `SESSION_STORE_MAX_UPLOAD_BYTES` | Largest compressed archive (default 25 MiB) |
| `SESSION_STORE_MAX_UNPACKED_BYTES` | Largest unpacked archive (default 64 MiB) |
| `SESSION_STORE_MAX_FILES` | Most entries (files and folders) in one archive (default 2000) |
| `SESSION_STORE_CLEANUP_ENABLED` | Start the cleanup job (default `true`) |
| `SESSION_STORE_CLEANUP_INTERVAL_SECONDS` | Time between cleanup runs (default 3600) |

The access keys are never a plain setting. MEHO reads them from Vault with
its scheduler token (`VAULT_SCHEDULER_TOKEN`), for every call to the
store, so a rotated key works at once. The token's policy must allow
`read` on that path. The built-in OpenBao's `meho-scheduler` policy
allows `read` on `secret/data/services/session-store`, so a built-in
install uses that path.

The variables live in `session_store/config.py`, not in `settings.py`.

## Rights

Each right is separate (`session_store/rights.py`). Each maps to the
lowest tenant role that has it:

| Right | Lowest role | Routes |
|---|---|---|
| upload | `read_only` | `POST /api/v1/sessions` (own sessions only) |
| list | `operator` | `GET /api/v1/sessions` |
| read | `operator` | `GET /api/v1/sessions/{session_id}`, `GET .../uploads/{seq}` |
| delete | `tenant_admin` | `DELETE /api/v1/sessions/{session_id}` |
| read quarantine | `tenant_admin` | `GET .../uploads/{seq}` of a quarantined upload |
| set retention | `tenant_admin` | `GET` / `PUT /api/v1/sessions/retention` |

Only a person may use these routes. An agent, service or runner login gets
`403`. Every query is limited to the caller's tenant: another tenant's
session is `404 session_not_found`, and the same session id in two tenants
is two separate sessions.

## Key types

| Type | Module | Role |
|---|---|---|
| `AgentSessionRecord` | `models.py` | Index row: tenant, session id, who, project, app, start, end, state, outcome, last upload time, file hashes |
| `AgentSessionUpload` | `models.py` | One stored upload: object key, archive hash, quarantine flag, where each file's tail starts |
| `AgentSessionRetention` | `models.py` | The retention period a tenant set |
| `ObjectStore` / `S3ObjectStore` | `object_store.py` | Put, get and delete whole objects; boto3 in a worker thread |
| `UploadPlan` | `plan.py` | The new tail of each file, or the conflicts |
| `CheckedPart` | `secret_check.py` | The checked bytes of one file part, and whether the check was sure |
| `PreparedUpload` | `prepare.py` | What the worker process returns: the new file states, the conflicts, and the archive of the checked tails |
| `AgentSessionEvent` | `models.py` | One saved event of a hosted agent run: kind, checked data, quarantine flag, `seq` |
| `RunEventLog` | `hosted_runs.py` | Saves the events of one hosted run, in order, when they happen |

The tables come from migrations `0152` and `0161` (`agent_session_event`).

## Control flow

### Upload

`POST /api/v1/sessions` takes `multipart/form-data`: the `archive` file (a
gzip tar archive) and the fields `session_id`, `project`, `app`,
`app_version`, `started_at`, `ended_at`, `state` (`open`, `ended` or
`unfinished`) and `outcome`. Times need a time zone.

MEHO takes one upload per person at a time, and at most four in all. More
get `429 session_upload_already_running` or `429 session_uploads_busy`
with `Retry-After: 60` (`upload_slots.py`). A retry of the same bytes is
safe, so the uploader tries again later.

MEHO first loads and locks the stored session. A session that another
person started is `409 session_owned_by_another_person`. Then steps 1, 2
and the secret check of step 4 run in one call in a worker process
(`prepare.py`, `worker.py`), not on the server's event loop. So a large
upload never holds up other requests. Each upload gets its own new
worker process, which stops when the upload is done. At most two run at
a time; a third upload waits for a free place. A worker thread would not
be enough: the regular expressions of the secret check hold Python's
interpreter lock for a whole search. When a worker process dies (for
example, it runs out of memory), only that upload fails with `500`.
Other uploads that run or wait at the same time go on as normal.

1. **Unpack** (`archive.py`). Nothing goes to disk. MEHO refuses a link or
   device, a path with `..`, a leading `/`, a backslash or a control
   character, a path that comes twice, too many entries (files and
   folders) or bytes, a cut-off archive and an empty one (`422 archive_*`).
   The gzip stream is unpacked with a byte limit before tar reads any
   header, so tar metadata (such as a large extended header) counts too.
2. **Compare** (`plan.py`). The laptop sends the whole current files every
   time. For each file MEHO compares the size and SHA-256 hash with what it
   stored before (the hashes are of the bytes before the secret check):
   - a new file: all of it is new;
   - the same size and hash, or the same as an earlier upload (a late
     retry): nothing new;
   - longer, and the first stored bytes have the stored hash: the bytes
     after them are the new tail;
   - anything else: the stored part changed. `409 session_conflict` with
     the paths. Nothing is overwritten.

   The session of a hosted agent run is never changed by an upload: `409
   session_is_a_hosted_run`.

   MEHO stores whole lines only. While the session is `open`, a tail ends
   at the last newline of the file: the last line may still be in
   writing, so it waits for a later upload. With `state` ended or
   unfinished, the last line is taken as it is. So the secret check
   always sees each line in one piece.
3. **No new bytes.** An upload with `state` ended or unfinished ends an open
   session (`state_changed`). Otherwise nothing changes (`unchanged`): a
   retry is safe. A late upload that says `open` never reopens an ended
   session.
4. **New bytes.** Each tail passes the secret check. MEHO writes one archive
   of the checked tails to
   `tenants/<tenant>/sessions/<record>/<seq>-<random>.tar.gz`, or under
   `quarantine/` when the check was not sure. Then it commits the index
   row, the upload row and the audit row in one transaction. When the
   commit fails, it deletes the archive again. So each upload is stored
   complete or not at all. Two uploads of one stored session at the same
   time run one after the other: the second waits for the first to
   commit, then compares against what the first stored. Two first uploads
   of a new session at the same time: one wins, the other gets `409
   session_upload_in_progress` and its archive is deleted again.

New bytes take the state the uploader sent. So a session uploaded three
nights in a row while it stays open has one index row, three uploads and
three audit rows, and its state is `open`.

### Secret check

`secret_check.py` checks each file part line by line. A JSON line (a JSON
Lines transcript) is parsed, and every string in it is checked, so a
replacement never breaks the JSON. Other lines are checked as text.

- **Known secrets are replaced** with `[REDACTED:<pattern>]`. The patterns
  come from the redaction engine (`docs/codebase/redaction.md`): private
  keys, kubeconfigs, `Authorization` headers, bearer tokens, JWTs, URL
  secrets, password hashes, Ceph keys, BIND keys and `password=...` style
  pairs. Host names, IP addresses and UUIDs stay: they are not secrets.
- **A private key over many lines is removed whole.** In plain text, the
  `BEGIN` line of a private key becomes one marker, and every line up to
  the `END` line goes. A key can start in one upload and end in the next.
  The upload always holds the whole file, so MEHO reads the stored part
  before the new tail and knows the tail starts inside a key.
- **Unclear content goes to quarantine.** The check is not sure when a file
  is not UTF-8 text, when a secret-like name still has a value, for
  example `"password": "..."` in JSON, or when a private key starts or
  ends in another upload. Variables (`${X}`), placeholders (`<x>`), short
  values and markers do not count.

A quarantined upload is stored (with the known secrets replaced) but only a
tenant admin can download it. An operator gets `403
session_upload_quarantined`, and the refusal is audited. `show` marks the
upload as in quarantine.

### Read and delete

- `GET /api/v1/sessions` lists the tenant's sessions, newest upload first,
  with filters `project` and `state`. The answer is
  `{items, next_cursor}`; follow `next_cursor` to read every page.
  `meho sessions list --all` does that.
- `GET /api/v1/sessions/{session_id}` returns the index row, the file
  hashes and every upload.
- `GET /api/v1/sessions/{session_id}/uploads/{seq}` returns the stored
  archive of one upload. Put the tails of all uploads together in `seq`
  order to get the whole (checked) files; each upload row says where its
  tail starts.
- `DELETE /api/v1/sessions/{session_id}` locks the session row (an upload
  locks it too), deletes the files, then the rows. So an upload at the same
  time ends before the files are listed, or waits until the rows are gone.
  When the store fails (`502 session_store_unavailable`) nothing in the
  index changes, and a retry is safe.

### Audit

Each call writes one `audit_log` row in the caller's name, with
`payload["op_id"]` set to `sessions.upload`, `sessions.list`,
`sessions.read`, `sessions.download`, `sessions.delete` or
`sessions.retention.set`. A change writes its row in the same transaction
as the change. A read commits its row before the answer goes out. Refused
uploads (`409`) and refused quarantine downloads (`403`) are audited too.
The payload holds the session id, counts and hashes, never file content.

### Retention

The cleanup job (`retention.py`) runs every
`SESSION_STORE_CLEANUP_INTERVAL_SECONDS`. A session is old when its last
upload is older than the tenant's period (`PUT /api/v1/sessions/retention`,
1 to 3650 days, or `null` for the default). For each old session it
locks the session row and checks the age again, so a session that got an
upload after the search stays. Then it deletes the files, then the rows with an `INTERNAL` audit row
(`sessions.cleanup`, `system:session-store-cleanup`) in the same commit.
When the store fails, the rows stay and the next run tries again. One run
removes at most 500 sessions. The job does nothing while the store is not
configured.

## Hosted agent runs

A hosted run is an agent run that MEHO starts itself: from the run console,
`meho agent run`, the REST and MCP run calls, a schedule or an event. The
code is in `session_store/hosted_runs.py` (write) and `agent/run_events.py`
(read).

- **One session per run.** The transaction that creates the run also adds
  its `agent_session_record` row. The row's `id` and `session_id` are the
  run id, `project` is the agent name, `app` is `meho-agent-run`, and
  `uploaded_by_sub` is the person (or, for a scheduled run, the agent) the
  run acts for. So the run shows in `meho sessions list`. A child run (one
  agent starts another) gets no session of its own; its answer is in the
  parent's tool result.
- **Each event is saved when it happens.** The run saves each `turn`,
  `tool_call`, `tool_result` and its `final` answer (or `error`) as one
  `agent_session_event` row, in order (`seq` 1, 2, 3, ...). Each row
  commits on its own, so a reader sees it at once and a crash keeps every
  event saved before it. The background run gets the events from the
  agent loop (`PydanticAgentRun.start(on_event=...)`); the inline stream
  saves each event before it sends it.
- **The same secret check as an upload.** The data of an event is written
  as one JSON line and passes `secret_check.check_part`. Known secrets are
  replaced with `[REDACTED:<pattern>]`. When the check is not sure, the
  event is marked `quarantined`: it is saved with the known secrets
  replaced, and only a tenant admin can read its data.
- **The end.** A saved `final` or `error` event sets the state to `ended`.
  When the run reaches its end status (`succeeded`, `failed` or
  `cancelled`), `end_run_session` writes the outcome and the end time in
  the same transaction (`operations/agent_run.py`, also for the reaper).
  A session that is still `open` then becomes `unfinished`: the run
  stopped without a final event (a crash, a restart, a lost lease or a
  cancel). MEHO never adds an ending that the run did not make.
- **A save and the end do not overlap.** Each save first updates the
  session row, and only while its state is `open`. The end of the run
  updates the same row. So the database runs them one after the other: a
  save that started before a cancel commits before the `cancelled` status
  does, and a save that comes after the end saves nothing. A reader that
  sees the end status therefore sees every saved event.
- **A tool result is saved before the next model call.** The agent loop
  sends a `tool_result` as soon as the tool is done, before it asks the
  model again. So the result is kept also when that model call fails or
  the run hits its turn limit.
- **A failed save.** When one save fails, MEHO logs
  `hosted_run_event_not_saved` and saves no more events for that run. So
  the saved events never have a gap. The run goes on, and its session
  ends `unfinished` unless a final event was saved.

The events are in the database, not in the object storage. So a run saves
its events also when no object storage is set up, and the run console can
follow a live run in order.

**Reading.** `agent/run_events.read_run_events` reads the events after a
`seq`. It reads the run status before the events, so a view that says the
run is done holds every event. Rules:

- Only the caller's tenant. Another tenant's run is "not found".
- The data of an event follows the rule for the run's output
  (`run_output_visible_to`): only the person who started a direct run
  reads its event data. Others see the kind and order (`withheld`).
- A quarantined event shows its data only to a tenant admin.
- A read with an audit path writes one `sessions.run_events.read` audit
  row and commits it before the answer goes out.

The run detail page (`/ui/agents/runs/{handle}`) lists the events. The run
console follows them live through `GET /ui/agents/runs/{handle}/events`
(`docs/codebase/ui-agent-run-console.md`).

## Dependencies

- `boto3` / `botocore` for the object storage. Path-style addressing and
  checksums only where the S3 API needs them
  (`request_checksum_calculation="when_required"`), because many
  S3-compatible stores refuse the checksum headers newer boto3 releases
  send by default.
- `scheduler/vault_credentials.read_service_secret` for the access keys.
- The redaction pattern library (`redaction/patterns.py`).

## Known issues

- The quarantine check is careful on purpose: a transcript that talks about
  passwords in a `name: value` form goes to quarantine. An admin can still
  open it.
- A changed JSON line is stored as compact JSON, so its bytes differ from
  the upload. The hashes in the index are of the uploaded bytes.
- The cleanup job and `DELETE /api/v1/sessions/{session_id}` need the
  object storage to be set up. Without it, the sessions of hosted runs and
  their events stay.

## References

- Code: `backend/src/meho_backplane/session_store/`,
  `backend/src/meho_backplane/api/v1/sessions.py`,
  `cli/internal/cmd/sessions/`.
- Tests: `backend/tests/test_session_store_api.py`,
  `backend/tests/test_hosted_run_events.py`,
  `backend/tests/test_session_store_units.py`,
  `backend/tests/test_session_store_worker.py`,
  `backend/tests/migrations/test_migration_0152_create_agent_session_store.py`.
- boto3 S3 client: <https://docs.aws.amazon.com/boto3/latest/reference/services/s3.html>
- botocore `Config`: <https://docs.aws.amazon.com/botocore/latest/reference/config.html>
