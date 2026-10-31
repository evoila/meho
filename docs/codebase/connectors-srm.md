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
- two **reads**: `srm.about` and `srm.api_file.get`, and
- the **appliance setup** operations: register the appliance with vCenter
  and SSO, set its network, time and certificates, and install storage
  replication adapters. They use the appliance's second API, the setup
  API (`/api/rest/configure/v2`). See "Appliance setup" below.

Why these come first: Broadcom does not offer the SRM 8.8 and 9.0 API
files as downloads; a running appliance serves them. `srm.api_file.get`
is how the lab pins those files. The other SRM operations (pairings,
protection groups, recovery plans, test recovery and more) are built on
this connector after the files are pinned.

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

The appliance setup operations (all on the setup API, all paths below
start with `/api/rest/configure/v2`):

| Operation | What it does | What it returns | Vendor call | Risk | Versions |
|---|---|---|---|---|---|
| `srm.appliance.show` | The appliance's setup. | Version and build; registration with vCenter (state, site name, SSO address, services); DNS and interfaces; time zone and NTP; server certificate (subject, issuer, valid to). | `GET /appliance`, `GET /appliance/configuration-manager/configuration`, `GET /appliance/settings/network`, `GET /appliance/settings/time`, `GET /appliance/certificates/server` | read (`safe`) | 8.8, 9.0, 9.1 |
| `srm.appliance.task.get` | One setup task: the status read of `register` and `certificate.set`. | State (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `not_reachable`), progress, the vendor's error text, the time of the read. | `GET /tasks/{task_id}` | read (`safe`) | 8.8, 9.0, 9.1 |
| `srm.appliance.register` | Register the appliance with its vCenter and SSO. | `accepted` with the task id and the status read, or `already_running`, `refused` or `failed`, with the steps. | `POST .../probe-ssl-connection`, `POST .../validate-connection`, `POST .../check-existing-registrations`, up to 9.0 `POST .../list-vc-services`, then `PUT /appliance/configuration-manager/configuration` | change (`dangerous`, waits for approval) | 8.8, 9.0, 9.1 |
| `srm.appliance.network.set` | Set DNS and host name, or the IPv4 setup of one interface. | The steps; `session_ended` and `target_must_change` when MEHO's address changed. | `PUT /appliance/settings/network/dns`, `POST /appliance/settings/network/interfaces/{interface_name}` | change (`dangerous`, waits for approval) | 8.8, 9.0, 9.1 |
| `srm.appliance.time.set` | Set the time zone and the time sync (NTP servers, the host, or none). | The time settings read back. | `PUT /appliance/settings/time` | change (`dangerous`, waits for approval) | 8.8, 9.0, 9.1 |
| `srm.appliance.csr.create` | Make a new key and a certificate signing request. The key stays on the appliance. | The request (PEM), its subject, names and a hash of its public key. | `POST /appliance/certificates/actions/generate-csr` | change (`caution`, waits for approval) | 8.8, 9.0, 9.1 |
| `srm.appliance.certificate.set` | Install the signed certificate for the request, with its CA chain; or add or remove trusted CA certificates. | Server certificate: `accepted` with the task, or `already_running`, `refused`, `failed`. CA change: `done`. | `POST /appliance/certificates/server/actions/update`, `POST /appliance/certificates/ca`, `POST /appliance/certificates/ca/actions/remove` | change (`dangerous`, waits for approval) | 8.8, 9.0, 9.1 |
| `srm.storage_adapter.install` | Fetch a storage replication adapter archive, check its SHA-256, upload it. | The new adapter, the archive (name, source, size, SHA-256), the adapter lists before and after, the steps, the clean-up. | `GET /storage-replication-adapters`, `POST /storage-replication-adapters` | change (`destructive`: only a person, after a preview) | 8.8, 9.0, 9.1 |

All operations exist on every covered version, so none needs a version
check. `srm.appliance.register` reads the appliance version to choose the
registration form (see below).

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
- `SetupApi`, `SetupApiError`, `SetupApiUnreachableError`, `Steps`,
  `task_view`, `find_running_task`, `wait_for_task`, `SETUP_API_CALLS`
  (`setup_api.py`) -- the setup API client and the shared parts of the
  setup operations.
- `appliance.py` (show, task read, network, time), `appliance_register.py`,
  `appliance_certificate.py`, `storage_adapter.py` -- the bodies of the
  setup operations. The entry methods are on `SrmConnector`.
- `appliance_ops.py`, `appliance_ops_more.py` -- the metadata of the setup
  operations and the two group texts (`srm-appliance`,
  `srm-storage-adapters`). `_op.py` holds the `SrmTypedOp` record.
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

### Shutdown

`aclose()` clears both session caches (the SRM API and the setup API) and closes the HTTP pool. No logout
call is sent.

## Appliance setup

### Login to the setup API

The setup API has its own login: `POST /api/rest/configure/v2/session`
with HTTP Basic and the appliance's **own admin user**, not the vCenter
SSO account of the target. Every setup operation takes it as a Vault
reference, `appliance_secret_ref` (a KV v2 path with `username` and
`password`). MEHO reads it under the operator's identity
(`load_param_basic_credentials` in `setup_api.py`) and never returns it.
A wrong password gives an error that names `appliance_secret_ref` and
HTTP 401, never the value.

The session id goes in `x-dr-session`. It is cached per appliance address
and Vault reference (`SrmConnector.setup_sessions`). A `401` on a later
call drops the session, logs in again and sends the call once more
(`SetupApi.send`). So a login that expired between a start and a status
read does not fail the read. This retry is inside the setup client; the
dispatcher's own `401` retry (which would run a whole multi-step change
again) never sees a setup `401`.

`srm.appliance.register` and `srm.appliance.certificate.set` also need
the vCenter SSO admin user: `sso_secret_ref`, read the same way. It goes
to the appliance in the request body. The flight recorder records no
request body of a setup API call, only the method, path, status and
answer.

### Vendor jobs: `register` and the server certificate

The vendor answers both with a task. MEHO's rules:

- The start call returns once the vendor took the job: `outcome:
  accepted`, `task_id`, and `status_read` (`srm.appliance.task.get` with
  the task id). It never says the work is done.
- `srm.appliance.task.get` gives the vendor's state, the progress and the
  error text. Call it until the state is `succeeded`, `failed` or
  `cancelled`.
- Before a start, MEHO reads `GET /tasks` and looks for a queued or
  running task of the same kind (its type or description holds
  `configur` or `regist` for a registration, `certificat` for a
  certificate). If one runs, the answer is `already_running` with its id,
  and nothing starts. This is a help, not a lock: two callers can both
  check before either starts, and a task that just ended is no longer
  running.
- After a lost reply, do not start again blindly. Read
  `srm.appliance.show` (`registration.state`, `certificate`) or the task,
  and decide from that.
- Start with MEHO's async mode (`async: true`,
  `docs/codebase/async-governed-dispatch.md`): MEHO stores the answer
  (with the task id) under a run handle, so a lost reply does not lose
  the task id.
- When the appliance does not answer (no connection, or HTTP 502, 503 or
  504 from a service that is down, for example during an upgrade),
  `srm.appliance.task.get` says `state: not_reachable` with the task id and the time of the read. It
  never says `failed` for that. MEHO keeps no job state between calls;
  the caller keeps the task id.
- A multi-step change that fails part-way returns `steps`: each step is
  `done`, `failed` (with the vendor's error) or `not_run`. Nothing that
  ran is undone.

### `srm.appliance.register`

Steps (`appliance_register.py`):

1. `running_task` -- read the version (`GET /appliance`) and look for a
   running registration task.
2. `probe_certificate` -- `probe-ssl-connection` on `https://<vcenter>:443`.
   MEHO computes the SHA-256 fingerprint of the certificate the appliance
   saw and compares it with `vcenter_fingerprint`. A different
   fingerprint stops the call (`refused`, with `seen_fingerprint`). The
   fingerprint goes out as the vendor writes it: SHA-256, in capitals,
   with colons. The input may be in any case, with or without colons.
3. `validate_connection` -- the appliance checks the SSO login and the
   address. A wrong SSO password fails here with the vendor's text.
4. `check_existing_registrations` -- when the appliance or the extension
   key (`com.vmware.vcDr` by default) is already registered, the call
   stops (`refused`, with the registrations it found).
5. `list_vc_services` -- only up to 9.0 (version below 9.1): the vCenter
   id (`service_id`) of the vCenter at that address. The registration
   then also sends `vc_instance_id` and `vc_thumbprint`. 9.1 finds the
   vCenter from the SSO address itself and does not need them.
6. `register` -- `PUT /appliance/configuration-manager/configuration` with
   the site name, admin e-mail, extension key, connection and SSO user.

The vCenter address always goes out as `https://<name>:443`. Without the
scheme and the port the vendor uses `http` and port 80, and the 9.1 setup
API answers `500` on the connection test. The input may be the name,
`https://<name>`, `<name>:443` or `https://<name>:443`; another scheme or
port is refused.

### `srm.appliance.network.set`

DNS first (`set_dns`), then the interface (`set_interface`). MEHO reads
the network first and refuses an interface the appliance does not have.
The interface MEHO uses is every interface unless the target's host is
the address of another interface. A new address there (a new static
address, or a change to DHCP) needs `allow_address_change: true`;
without it nothing changes. The vendor ends the connection when the
address changes, so a lost connection on that call counts as sent. The
answer then has `session_ended: true` and `target_must_change`: change
the target's host to the new address. IPv4 only.

### Certificates

`srm.appliance.csr.create` returns the request (PEM). Each call makes a
new key, so only the newest request fits. `srm.appliance.certificate.set`
takes exactly one of:

- `server_certificate` (`certificate_pem`, `csr_pem`, `ca_chain_pem`):
  MEHO checks that the certificate holds the public key of the request,
  names the appliance's host name (from `GET /appliance`; a wildcard
  name counts) and has not expired. Else it is refused before any change.
  Then it is a vendor job (see above).
- `add_ca_certificates` / `remove_ca_certificates`: done when the call
  returns.

Answers name certificates by subject, issuer, end date and SHA-256
fingerprint; no PEM text comes back except the request.

### `srm.storage_adapter.install`

A storage replication adapter is the storage vendor's plug-in for
array-based replication. It runs as a container on the appliance. MEHO
does all the work inside its own call (`storage_adapter.py`):

1. `list_before` -- `GET /storage-replication-adapters`. The vendor
   answers with a task; MEHO reads it until it ends.
2. `download` -- MEHO fetches `archive_url` into a temporary folder
   (`meho-srm-sra-*` under the system temp folder, the file named
   `<name>.meho-partial` until the check passed). Size limit 4 GiB.
3. `check_sha256` -- the SHA-256 must equal `sha256`; else nothing is
   uploaded.
4. `upload` -- `POST /storage-replication-adapters`, the archive as
   `multipart/form-data` in the field `file`.
5. `list_after` -- the new adapter is the one not in the first list.

Rules:

- **Use async mode.** The download and upload take minutes. With
  `async: true` MEHO answers `202` with a run handle at once; the stored
  result has the final answer. The answer is only written after the last
  step and the clean-up.
- **Keep-alive.** While the call runs, MEHO reads `GET /session` once a
  minute, so the vendor session does not expire during a long download.
- **Clean-up.** The temporary folder is removed in a `finally` block:
  after success, an error or a cancel. The answer says so in `cleanup`.
- **When MEHO dies.** The clean-up cannot run. What is left: the
  temporary folder (the next run removes `meho-srm-sra-*` folders older
  than six hours, listed in `cleanup.leftovers_removed`) and the vendor
  session (it ends by its own timeout). MEHO's async mode does not resume
  work: the run ends as failed. Then read the adapter list on the
  appliance and run again; a partial upload is not in the vendor's list.
- **A broken upload.** MEHO reads the list again and sets `installed`:
  `false` (nothing installed), `true`, or `null` when the list could not
  be read.
- **Credentials and URLs.** MEHO sends no credential to the archive
  server and follows no redirect, so nothing crosses to another host. The
  archive address passes the same address screen as a target address.
  Answers, errors and logs show the URL without its query part (a signed
  URL keeps its token there); a connection error is named by its class
  only.
- **Preview.** The operation is `destructive`: agents cannot run it, and
  a person calls `preview_operation` first. The preview builder reads the
  appliance and its adapters and sends `HEAD` to the archive URL. Its
  `blast_radius` names the appliance and the archive (`object`), the
  adapters already there (`children`) and what cannot be undone. It
  refuses when the setup API cannot be read or the archive URL answers
  404 or 410. `call_operation` without the matching `preview_hash` is
  refused.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`): the pooled per-target
  client, TLS trust, SSRF guard, flight-recorder spans.
- `connectors/_shared/vault_creds.py`: the operator-context credential
  read.
- `connectors/_shared/vcf_auth.py`: `basic_auth_header`,
  `is_acceptable_auth_model`, `session_establish_auth_error`.
- `connectors/_shared/api_file_read.py`: the API file read steps.
- `operations/typed_register.py`: the typed-op registrar.
- `operations/_preview.py`: the preview builder of `srm.storage_adapter.install`.
- `connectors/_shared/pinned_transport.py` and `targets/ssrf_guard.py`:
  the address screen of the archive download.
- `cryptography`: the certificate and request checks.

## Tests

- `backend/tests/test_connectors_srm.py` -- login, both reads through
  `dispatch()`, replaced and counted secrets, a replaced copy that still
  parses with every value type kept, two targets on one address, a `401`
  on an operation or on the version read that ends only one target's
  session, registration shape, fingerprint. One test reads the pinned 9.1 files through
  `srm.api_file.get`; it runs only when the spec shelf is present.
- `backend/tests/test_connectors_shared_api_file_read.py` -- the shared
  read steps, including `text_redactor` and what the flight recorder and
  a raised `401` carry.
- `backend/tests/test_connectors_srm_spec_reconcile.py` -- the login and
  `server/info` paths against the pinned 9.1 file
  (`vcf-protection-and-recovery-9.1/recovery-srm-v2.yaml` on the spec
  shelf), and every setup API call (`SETUP_API_CALLS` in `setup_api.py`)
  against the pinned 9.1 setup API file
  (`vcf-protection-and-recovery-9.1/appliance-configure-v2.yaml`). The 8.8
  and 9.0 files join these lanes once the lab pins them. CI requires both
  files, so a missing file fails the job instead of skipping the lane.
- `backend/tests/test_connectors_srm_appliance.py` -- risk and approval of
  every setup operation, the setup login, `show`, `task.get` (failed,
  expired login, not reachable), `register` (address form, fingerprint,
  existing registration, running task, both registration forms, wrong SSO
  password), `network.set` and `time.set`.
- `backend/tests/test_connectors_srm_appliance_certs_sra.py` -- the
  request, the certificate checks, CA changes, and the adapter install
  (good run, unreachable archive, wrong SHA-256, redirect, broken upload,
  keep-alive, clean-up, preview, refused without a preview hash).

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
- The 8.8 and 9.0 calls follow the 9.1 file and the vendor documentation.
  Their first live use is the pinning of the 8.8 and 9.0 files. If a call
  differs there, fix the call and the table above.
- **The adapter upload format is not in the vendor file.** The 9.1 setup
  API file lists `POST /storage-replication-adapters` with no request
  body, and the vendor's API pages show none. MEHO sends
  `multipart/form-data` with the archive in the field `file`. The live
  proof checks it; if the field differs, change `_upload` in
  `storage_adapter.py`.
- **Task kinds are matched by words.** The vendor does not list its task
  types, so the "is one running already" check matches words in a task's
  type and description (`REGISTER_TASK_WORDS`, `CERTIFICATE_TASK_WORDS`).
  The live proof records the real type names.
- **The 8.8 and 9.0 setup API** follows the 9.1 file. `vc_instance_id` and
  `vc_thumbprint` are deprecated from 9.0.3 on; MEHO sends them only below
  9.1.

## References

- Task #4505 (this connector); #4506 (the vSphere Replication connector,
  the second target of a 9.1 appliance, `connectors-vr.md`).
- Task #4559 (the appliance setup operations).
- `docs/codebase/async-governed-dispatch.md` -- MEHO's async mode.
- `docs/codebase/connectors-hcx.md`, `docs/codebase/connectors-vcf-fleet.md`
  -- the sibling API file reads.
- `docs/decisions/spec-reconcile-guards-standard.md` -- the reconcile lane.
