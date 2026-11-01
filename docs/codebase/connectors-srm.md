# Connector: srm (Site Recovery Manager 8.8, Live Site Recovery 9.0, VCF 9.1 Protection and Recovery)

## Overview

The `srm` connector is the hand-written `HttpConnector` subclass for the
Site Recovery Manager REST API v2 (`/api/rest/srm/v2`). It is registered
under `(product="srm", version="9.1", impl_id="srm-rest")` and as the
product wildcard, so a target with no version yet still resolves to it.
The `>=8.8,<10.0` range keeps SRM 8.8 and Live Site Recovery 9.0 on the
same class: all three versions serve the same API v2.

Today the connector ships:

- the **login** to the SRM API,
- **version detection** (MEHO reads the SRM version and build),
- two **server reads**: `srm.about` and `srm.api_file.get`, and
- seven **recovery reads**: pairings, the login to the remote site,
  recovery plans, protection groups, health issues and run history.

Broadcom does not offer the SRM 8.8 and 9.0 API files as downloads; a
running appliance serves them. `srm.api_file.get` is how the lab pins
those files.

**No SOAP client is needed.** The SRM REST API v2 covers the usual work:
pairings, the remote login, recovery plans, protection groups, issues,
history reports, and (in later Tasks) test, recovery, reprotect and the
inventory mappings. Every operation here uses that REST API.

Package: `backend/src/meho_backplane/connectors/srm/`.

The login, the session cache, the version read and both reads are shared
with the vSphere Replication connector (`docs/codebase/connectors-vr.md`).
Both APIs are served by the same REST API gateway, so the shared part
lives in `connectors/vmware_shared/dr_rest.py` (the base class
`DrRestConnector`) and `connectors/vmware_shared/dr_api_file.py` (the read).
`SrmConnector` sets the SRM paths.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `srm.about` | SRM server name, status, version, build and the paired vCenter, plus a short view of the certificates. MEHO uses the same call to detect the version. | `GET /api/rest/srm/v2/server/info` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.api_file.get` | Read one API description file the appliance serves (the SRM API or the appliance setup API). Returns a checked copy (secret values replaced) with its size, SHA-256 and the SRM version; the text is in parts behind a result handle. | `GET /api/rest/srm/v2/open-api.yaml`, `GET /api/rest/configure/v2/open-api.yaml` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.pairing.list` | The site pairings. One row per pairing: the pairing id and, for both sites, the SRM server and the vCenter with id, name, address, status (OK, WARNING, ERROR), version and build. | `GET /pairings` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.remote_login` | Log in to the remote site of a pairing. The account comes from the Vault path in `secret_ref`, which must sit under an allowed folder (see below); the answer names the remote user, never the password. | `POST /pairings/{pairing_id}/remote-session`, then `GET /pairings/{pairing_id}/remote-session` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.plan.list` | The recovery plans. One row per plan: id, name, status, `needs_cleanup`, `is_running`, progress, site names, folder, description, the ids and names of its protection groups, and its newest run (`last_run_*`: report id, type, result, start time, error and warning counts). | `GET /pairings/{pairing_id}/recovery-management/plans`, then per plan `GET .../plans/{plan_id}/protection-groups` and `GET .../plans/{plan_id}/history-reports` with `GET /tasks/{taskId}` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.plan.get` | One plan with its protection groups, its recovery steps in one view mode (test, recovery, cleanup or reprotect), its VMs with priority and start action, and with `vm_id` that VM's full recovery settings. | `GET .../plans/{plan_id}`, `GET .../plans/{plan_id}/protection-groups`, `GET .../plans/{plan_id}/recovery-steps/{view_mode}`, `GET .../plans/{plan_id}/vms`, `GET .../plans/{plan_id}/vms/{vm_id}/recovery-settings` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.group.list` | The protection groups with replication type (HBR is vSphere Replication, ABR is array based), VM count and state; with `group_id` the VMs of that group with their protection state and errors. | `GET /pairings/{pairing_id}/protection-management/groups`, `GET .../groups/{group_id}/vms` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.issue.list` | Health issues (errors and warnings) of the pairing, or of one plan (`plan_id`), one group (`group_id`) or one SRM server (`srm_id`). | `GET /pairings/{pairing_id}/issues`, `GET .../plans/{plan_id}/issues`, `GET .../groups/{group_id}/issues`, `GET /pairings/{pairing_id}/srms/{srm_id}/issues` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.history.list` | The runs of a plan (test, cleanup, recovery, reprotect) in a time range, newest first, with result and error counts; with `report_id` one run with its error and warning texts. | `GET .../plans/{plan_id}/history-reports`, then `GET /tasks/{taskId}`; `GET .../plans/{plan_id}/history-reports/{report_id}` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |

The recovery read paths are relative to `/api/rest/srm/v2`; `...` stands
for `/pairings/{pairing_id}/recovery-management` (plans) or
`/pairings/{pairing_id}/protection-management` (groups).

Every operation exists on every covered version, so none needs a
version check yet. The Tasks that add 9.x-only operations add that check.

## Registering targets

**VCF 9.1 Protection and Recovery.** One appliance serves both the SRM
API and the vSphere Replication API. Register it **twice**, with two
names and the same address:

```bash
meho targets add <srm-target-name> --product srm --host <appliance-address> \
  --secret-ref <vault-path-of-the-srm-login>
meho targets add <vr-target-name> --product vr --host <appliance-address> \
  --secret-ref <vault-path-of-the-vr-login>
```

MEHO picks one connector class per target, by product and version, so
each target gets its own connector and its own login. The `vr` target
uses the vSphere Replication connector (`docs/codebase/connectors-vr.md`).
The two targets may name the same Vault path.

**SRM 8.8 and Live Site Recovery 9.0.** The SRM appliance and the
vSphere Replication appliance are separate machines. Register one target
per appliance: `--product srm` for the SRM appliance.

The login account is the vCenter single sign-on account of the SRM site
(for example a service account with the SRM privileges the operations
need). Only `auth_model=shared_service_account` (or unset) is accepted;
other modes raise `NotImplementedError`.

## Key types

- `SrmConnector` (`connector.py`) -- the connector class, a subclass of
  `DrRestConnector` (`connectors/vmware_shared/dr_rest.py`). It sets the SRM
  paths (`api_base`, `session_path`, `info_path`, `api_files`) and
  defines the entry methods `about` and `api_file_get`, which call the
  shared code. The entry methods live on this class, not on the base, so
  the stored handler of `srm.about` names `SrmConnector`. MEHO binds it
  only to an `SrmConnector`, so an `srm.*` operation cannot run on a `vr`
  target (the call ends with a `connector_error` before any HTTP
  request). The base caches one session id per credential
  (`credential_cache_key`: tenant, secret and login host), so two SRM
  targets that share one credential share one session.
- `SrmTargetLike` (`session.py`) -- the minimum target shape: `id`,
  `tenant_id`, `name`, `host`, `port`, `secret_ref`, `auth_model`.
- `SrmSessionLoader` (`session.py`) -- an async callable
  `(target, operator) -> {"username", "password"}`. The default,
  `load_session_credentials_from_vault`, reads the target's `secret_ref`
  from Vault under the operator's identity. Tests inject a static loader.
- `DrServerVersion`, `parse_server_version`, `summarise_server_info`
  (`connectors/vmware_shared/dr_rest.py`) -- the version read and the
  `srm.about` answer.
- `SrmTypedOp`, `SRM_TYPED_OPS`, `register_srm_typed_operations`
  (`typed_ops.py`) -- the op metadata and the registrar. The response
  schemas and the shared `llm_instructions` texts come from
  `connectors/vmware_shared/dr_rest_schemas.py`.
- `API_FILES` (`api_file.py`) -- the two files the read can fetch.
- `recovery_ops.py` -- the metadata and group texts of the seven
  recovery reads; `recovery_schemas.py` -- their response schemas;
  `recovery_paths.py` -- their vendor paths and `VENDOR_CALLS`, the list
  the reconcile test checks; `recovery_reads.py` -- their bodies. The
  entry methods (`pairing_list`, `remote_login`, `plan_list`, `plan_get`,
  `group_list`, `issue_list`, `history_list`) are on `SrmConnector`, for
  the same reason as `about`.
- `SrmTypedOp` (`_op.py`) -- the metadata record of one operation.
- `dr_api_file_get` (`connectors/vmware_shared/dr_api_file.py`) -- the read's
  body, shared with `vr.api_file.get`.
- `connectors/_shared/api_file_read.py` -- the vendor-neutral steps of an
  API file read (bounded fetch, text check, parts, size check, one
  redaction pass over the whole text). See "The read" below.
- `connectors/_shared/api_file_secrets.py` -- `check_api_file` parses the
  file and replaces secret values (`docs/codebase/connectors-shared-api-file-secrets.md`).

## Control flow

### Registration

Importing the package registers the class twice (versioned and wildcard)
and queues `register_srm_typed_operations` on the typed-op registrar list.
The lifespan runner writes both descriptor rows before the first
dispatch. No spec ingest is needed.

### Login

1. `POST /api/rest/srm/v2/session` with HTTP Basic (the account read from
   Vault).
2. The JSON answer carries `session_id`. The connector caches it per
   target and tenant. Two targets on one address never share a session.
3. Every later call sends `x-dr-session: <session_id>`.
4. A `401` on a later call reaches the dispatcher, which calls
   `invalidate_session(target)`. That drops only this target's session;
   other targets, also on the same address, keep theirs. The dispatcher
   then runs the call once more, and the connector logs in again. A
   `401` or `403` at login itself raises `ConnectorAuthError` naming the
   target.

### Version

`server_version(target, operator)` reads `GET /api/rest/srm/v2/server/info`
and takes `version` and `build_number`. It never raises: a failure is
named in `error`. `fingerprint()`, `probe()` and `srm.api_file.get` use it.
A `401` on this read drops this target's cached session, as the
dispatcher would for an operation. The next call logs in again; other
targets keep their sessions. Without this, a read whose file request
worked but whose version read was rejected would keep sending the
rejected session and come back without a version every time.

### `srm.about`

Returns the scalar fields of `server/info` (`id`, `name`, `url`,
`server_status`, `version`, `build_number`, `vc_guid`, `vc_name`,
`extension_key`). `certificates` and `certificate_warnings` come back as a
short view: subject, issuer, serial number, validity dates and thumbprint.
The PEM text, public key and signature are left out, so the answer stays
small. `_meta` is left out.

### The read: `srm.api_file.get`

Input: one field, `file`:

| `file` | Path on the appliance | What it is |
|---|---|---|
| `srm` | `/api/rest/srm/v2/open-api.yaml` | the Site Recovery API |
| `configure` | `/api/rest/configure/v2/open-api.yaml` | the appliance setup API |

The input is a name, not a path, so no other path can be read. An unknown
name stops before any HTTP request.

Then the read sends one `GET` with the target's own login. It does not
follow a redirect: `redirect_to` holds the path. Only a `401` is raised,
so the dispatcher can log in again; every other status (for example
`404`) is reported in `status` with the body as text.

The vendor-neutral steps live in `connectors/_shared/api_file_read.py`.
They are the same steps the `fleet.api_file.get` read uses
(`docs/codebase/connectors-vcf-fleet.md`):

- the fetch and the file check are one step, `fetch_checked_api_file`;
  the size check of the stored record, the last redaction check and the
  flight recorder call are a second step, `finish_api_file_read`. The
  flight recorder (the stored record of each vendor call) gets the
  checked copy of the file, never the body as it came, and only after
  every check has passed: a file the read refuses, for any reason, is
  recorded with no body. A `401` body goes through the same check before
  the error is raised, or the error is raised with no body, also when
  the body is too big or cannot be decoded; the status and the headers
  stay, so the login retry still runs;
- the body is read in a stream; `gzip` and `deflate` are decoded in
  bounded steps, so a small compressed body that decodes past the limit
  stops at the limit; a cut stream is refused;
- only UTF-8 text comes back;
- the text is cut into parts of at most 64 KiB, on character boundaries;
- a file whose stored record does not fit in one result handle stops
  with an error that names its size;
- the file is parsed and written back by `check_api_file`
  (`connectors/_shared/api_file_secrets.py`): every value under a secret
  name becomes `[REDACTED:param_name]`. A file that does not parse stops
  with the reason;
- the read gives `check_api_file` MEHO's general secret-redaction rules
  as its text rule (`text_redactor` in `connectors/_shared/api_file_read.py`).
  They run on every parsed text value before the file is written back.
  Text they match, such as an `Authorization: Basic ...` example in a
  description, becomes a placeholder like `[REDACTED:authorization_header]`.
  Because the rules see the values and not the written text, the copy
  keeps its format: a placeholder that starts with `[` is written quoted,
  not as a YAML list, and numbers, booleans and numeric keys stay as they
  are;
- `size`, `sha256` and the parts are built from this final copy. If the
  rules would still change the answer, the read stops with an error that
  names the rules and the number of places, never the text. So `sha256`
  always names what the caller gets.

The answer:

| Field | Meaning |
|---|---|
| `file` | The name that was asked for. |
| `path` | The path that was read. |
| `status` | The HTTP status. |
| `content_type` | The `Content-Type` header. |
| `size` | Size of the checked copy in bytes. |
| `sha256` | SHA-256 of the checked copy, hex. |
| `secrets_replaced` | How many values were replaced in the copy (secret-named values, text the redaction rules match, and secret-named query values in `redirect_to`). |
| `version`, `build` | SRM version and build from `server/info`. |
| `version_error` | Why the version read failed; `null` when it worked. The file still comes back. |
| `redirect_to` | Path of a redirect answer; `null` otherwise. |
| `parts` | How many text parts there are. |
| `items` | The parts, `{index, text}`, in order. |

A long file is stored behind a result handle; read the parts with
`result_query` in `index` order and join the `text` fields. The SHA-256
of the joined text equals `sha256`. The copy is not byte for byte the
vendor file: it is the parsed file written back, with secrets replaced.
Pin and compare the copy.

### The recovery reads

All seven need `pairing_id` (from `srm.pairing.list`), except
`srm.pairing.list` itself. Every id the caller gives becomes one path
segment: the schema refuses `/`, `\`, `?`, `#`, `%`, `.` and `..`, and
the code encodes the value before it builds the path.

**Lists.** A list read asks for pages of 100 rows (`limit`, `offset`)
until the last page: a page with fewer than 100 rows, or `_meta.total`
rows in all. There is no page cap. When SRM sends the same page twice,
it ignored the offset; the read then stops with an error instead of
returning part of the list as if it were complete. The answer is a few
scalars (for example `pairing_id`, `total`) and `items`, a flat list of
rows. A long list is stored behind a result handle; read it with
`result_query`. Times come back as ISO 8601 UTC text, not milliseconds.

**`srm.pairing.list`** flattens the four server objects of a pairing
(`local_srm`, `remote_srm`, `local_vc`, `remote_vc`) into columns such
as `remote_srm_status`. Certificates are left out.

**`srm.remote_login`** first checks the Vault path in `secret_ref`. The
path must sit under an allowed folder (prefix):

- by default, the folder of the target's own `secret_ref` (a target with
  `targets/srm-a` allows `targets/<anything>`);
- or the folder an operator sets in the target's `extras` key
  `remote_secret_prefix`, which replaces the default.

Any other path, or a target where no folder can be found, is refused
before Vault is read and before any request. The check is the same one
the VMware guest operations use for `guest_secret_ref`
(`check_guest_secret_ref` in `vmware_rest/composites/_guest_secret_ref.py`).
Errors show only the first part of the path.

The read then gets the secret (KV version 2, mount `secret`, keys
`username` and `password`) under the caller's identity, and sends `POST /pairings/{pairing_id}/remote-session` with
the SRM session header and HTTP Basic of that account. It then reads the
remote user back. The password is never in the answer, a log line or
the stored vendor call (the flight recorder hides the `Authorization`
header). A Vault refusal is reported as a problem with `secret_ref`. A
secret without both keys stops before any request. A `401` may mean
that MEHO's SRM session ended or that the remote site refused the
account: the read drops the SRM session, logs in again and tries once
more. A second `401` is reported as a refused remote login.

The remote login belongs to MEHO's SRM session. When that session ends
and MEHO logs in again, the remote login is gone; a read that needs the
remote site then fails, and the caller runs `srm.remote_login` again.

**`srm.plan.list`** adds `needs_cleanup`: true for `TEST_COMPLETE`,
`TEST_INTERRUPTED`, `CLEANUP_INCOMPLETE` and `CLEANUP_INTERRUPTED`. For
each plan it also reads the plan's protection groups (`protection_group_ids`,
`protection_group_names`) and its newest run: the first page of the
plan's history, asked for newest first (`sort_by=start_time_ms`,
`order_by=desc`), from which it takes the row with the latest start time.
The query sends `start_date=0`, so SRM searches the whole history and not
only its default of the last 7 days: a plan whose newest run is a month
old still shows that run. Four plans are read at a time. The `last_run_*` fields are null for a
plan that never ran. A history task that fails or takes too long does not
stop the list; the row says why in `last_run_error`.

**`srm.plan.get`** makes four reads (plan, groups, steps, VMs) and a
fifth with `vm_id`. `view_mode` defaults to `recovery`. SRM refuses some
view modes in some plan states; then `steps` is empty and `steps_error`
holds the HTTP status and the vendor text. Every other error stops the
read.

**`srm.group.list`** returns `kind: "groups"` rows, or with `group_id`
`kind: "vms"` rows.

**`srm.issue.list`** takes at most one of `plan_id`, `group_id` and
`srm_id` (the schema says so with `oneOf`). Rows carry `severity` (ERROR
or WARNING), `issue_type`, the object, the text and the time; the answer
counts `errors` and `warnings`.

**`srm.history.list`** without `report_id`: SRM answers the list call
with a task (`202`). The read checks `GET /tasks/{taskId}` once a second
for up to 60 seconds. A task that ends with `ERROR` or `CANCELLED`, or
that runs longer, stops the read with its message. The reports come from
the task's `result` (a list, `{"list": [...]}` or one report). The list
call takes `limit` and `offset` like the other lists, so the read asks
for one task per page of 100 reports until the last page, then sorts all
reports newest first. `start_time`
and `end_time` (ISO 8601) become the vendor's `start_date` and
`end_date`; without them SRM returns the last 7 days. `report_id` is the
report's `run_key`. With `report_id` the read gets that report directly,
with its `errors` and `warnings` texts.

### Shutdown

`aclose()` clears the session cache and closes the HTTP pool. No logout
call is sent.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`): the pooled per-target
  client, TLS trust, SSRF guard, flight-recorder spans.
- `connectors/_shared/vault_creds.py`: the operator-context credential
  read.
- `connectors/_shared/vcf_auth.py`: `basic_auth_header`,
  `is_acceptable_auth_model`, `session_establish_auth_error`.
- `connectors/_shared/api_file_read.py`: the API file read steps.
- `operations/typed_register.py`: the typed-op registrar.

## Tests

- `backend/tests/test_connectors_srm_recovery_reads.py` -- the seven
  recovery reads through `dispatch()` with hand-written answers: flat
  rows and paging, the remote login (account sent, never returned; a
  refused login; a secret without a password sends nothing), plan state,
  a refused view mode, groups and group VMs, each issue scope, two scopes
  refused, the history task checked until it ends, a failed task, one
  report, a vendor error, ids that are not one path segment, and
  registration with group texts.

- `backend/tests/test_connectors_srm.py` -- login, both reads through
  `dispatch()`, replaced and counted secrets, a replaced copy that still
  parses with every value type kept, two targets on one address, a `401`
  on an operation or on the version read that ends only one target's
  session, registration shape, fingerprint. One test reads the pinned 9.1 files through
  `srm.api_file.get`; it runs only when the spec shelf is present.
- `backend/tests/test_connectors_shared_api_file_read.py` -- the shared
  read steps, including `text_redactor` and what the flight recorder and
  a raised `401` carry.
- `backend/tests/test_connectors_srm_spec_reconcile.py` -- the login,
  `server/info` and every recovery read path (`VENDOR_CALLS`) against the
  pinned 9.1 file
  (`vcf-protection-and-recovery-9.1/recovery-srm-v2.yaml` on the spec
  shelf). The 8.8 and 9.0 files join this lane once the lab pins them.

## Known issues

- **The copy is not the vendor file byte for byte.** The SRM file has
  a "getting started" text with example `Authorization: Basic ...`
  headers, and both files have `password` schema properties. The read
  returns them replaced (`[REDACTED:authorization_header]`,
  `[REDACTED:param_name]`) and counts them in `secrets_replaced`; the
  pinned 9.1 `recovery-srm-v2.yaml` comes back with 6 values replaced.
  The copy is also the parsed file written back, so its layout differs
  from the vendor's. Pin and compare the copy, not the vendor bytes.
  `hcx.api_file.get` and `fleet.api_file.get` do not run the extra
  redaction pass yet, so a file like this still stops there.
- **The shape of the history task result is not in the vendor file.** The
  file says only "Result is of type HistoryReport" and types `result` as
  a free object. The read accepts a list, `{"list": [...]}` or one report,
  and takes `run_key` as the `report_id`. The live test confirms this.
- **`srm.plan.list` makes two more vendor calls per plan** (groups and
  the history task), four plans at a time. On a pairing with many plans
  the read takes longer. Whether SRM honours `sort_by=start_time_ms` on
  the history task is for the live test; the read picks the newest row of
  the first page itself, so it is only wrong for a plan with more than
  100 runs whose history SRM did not sort.
- The 8.8 and 9.0 calls follow the 9.1 file and the vendor documentation.
  Their first live use is the pinning of the 8.8 and 9.0 files. If a call
  differs there, fix the call and the table above.

## References

- Task #4505 (this connector); Task #4552 (the recovery reads); #4506 (the vSphere Replication connector,
  the second target of a 9.1 appliance, `connectors-vr.md`).
- `docs/codebase/connectors-hcx.md`, `docs/codebase/connectors-vcf-fleet.md`
  -- the sibling API file reads.
- `docs/decisions/spec-reconcile-guards-standard.md` -- the reconcile lane.
