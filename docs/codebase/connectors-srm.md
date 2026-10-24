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
- **version detection** (MEHO reads the SRM version and build), and
- two **reads**: `srm.about` and `srm.api_file.get`.

Why these come first: Broadcom does not offer the SRM 8.8 and 9.0 API
files as downloads; a running appliance serves them. `srm.api_file.get`
is how the lab pins those files. The other SRM operations (pairings,
protection groups, recovery plans, test recovery and more) are built on
this connector after the files are pinned.

Package: `backend/src/meho_backplane/connectors/srm/`.

The login, the session cache, the version read and both reads are shared
with the vSphere Replication connector (`docs/codebase/connectors-vr.md`).
Both APIs are served by the same REST API gateway, so the shared part
lives in `connectors/_shared/dr_rest.py` (the base class
`DrRestConnector`) and `connectors/_shared/dr_api_file.py` (the read).
`SrmConnector` sets the SRM paths.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `srm.about` | SRM server name, status, version, build and the paired vCenter, plus a short view of the certificates. MEHO uses the same call to detect the version. | `GET /api/rest/srm/v2/server/info` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `srm.api_file.get` | Read one API description file the appliance serves (the SRM API or the appliance setup API). Returns a checked copy (secret values replaced) with its size, SHA-256 and the SRM version; the text is in parts behind a result handle. | `GET /api/rest/srm/v2/open-api.yaml`, `GET /api/rest/configure/v2/open-api.yaml` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |

Both operations exist on every covered version, so neither needs a
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
  `DrRestConnector` (`connectors/_shared/dr_rest.py`). It sets the SRM
  paths (`api_base`, `session_path`, `info_path`, `api_files`). The base
  caches one session id per target and tenant (`target_cache_key`).
- `SrmTargetLike` (`session.py`) -- the minimum target shape: `id`,
  `tenant_id`, `name`, `host`, `port`, `secret_ref`, `auth_model`.
- `SrmSessionLoader` (`session.py`) -- an async callable
  `(target, operator) -> {"username", "password"}`. The default,
  `load_session_credentials_from_vault`, reads the target's `secret_ref`
  from Vault under the operator's identity. Tests inject a static loader.
- `DrServerVersion`, `parse_server_version`, `summarise_server_info`
  (`connectors/_shared/dr_rest.py`) -- the version read and the
  `srm.about` answer.
- `SrmTypedOp`, `SRM_TYPED_OPS`, `register_srm_typed_operations`
  (`typed_ops.py`) -- the op metadata and the registrar. The response
  schemas and the shared `llm_instructions` texts come from
  `connectors/_shared/dr_rest_schemas.py`.
- `API_FILES` (`api_file.py`) -- the two files the read can fetch.
- `dr_api_file_get` (`connectors/_shared/dr_api_file.py`) -- the read's
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

- `backend/tests/test_connectors_srm.py` -- login, both reads through
  `dispatch()`, replaced and counted secrets, a replaced copy that still
  parses with every value type kept, two targets on one address, a `401`
  on an operation or on the version read that ends only one target's
  session, registration shape, fingerprint. One test reads the pinned 9.1 files through
  `srm.api_file.get`; it runs only when the spec shelf is present.
- `backend/tests/test_connectors_shared_api_file_read.py` -- the shared
  read steps, including `text_redactor`.
- `backend/tests/test_connectors_srm_spec_reconcile.py` -- the login and
  `server/info` paths against the pinned 9.1 file
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
- The 8.8 and 9.0 calls follow the 9.1 file and the vendor documentation.
  Their first live use is the pinning of the 8.8 and 9.0 files. If a call
  differs there, fix the call and the table above.

## References

- Task #4505 (this connector); #4506 (the vSphere Replication connector,
  the second target of a 9.1 appliance, `connectors-vr.md`).
- `docs/codebase/connectors-hcx.md`, `docs/codebase/connectors-vcf-fleet.md`
  -- the sibling API file reads.
- `docs/decisions/spec-reconcile-guards-standard.md` -- the reconcile lane.
