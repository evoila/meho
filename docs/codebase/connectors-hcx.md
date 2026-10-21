# Connector: hcx (VMware HCX 4.11, 9.0 and 9.1)

## Overview

The `hcx` connector is the hand-written `HttpConnector` subclass for VMware
HCX Manager. It is registered under `(product="hcx", version="9.0",
impl_id="hcx-rest")` and as the product wildcard, so a target with no
version yet still resolves to it. The class pin tracks the VCF 9 line; the
`>=4.7,<10.0` range keeps HCX 4.x dispatchable through the same class.

Today the connector ships three things:

- the **login** to an HCX Manager,
- a **version helper** that reads the Manager version, and
- one **read**, `hcx.api_file.get`, which fetches the API documentation
  that a running HCX Manager serves.

Why this read comes first: Broadcom publishes no file and no versioned
reference of the main HCX API (`/hybridity/api/...`). Only a running HCX
Manager serves its documentation. This read is how the vendor API files
get pinned; the other HCX reads (site pairings, service meshes,
appliances, jobs, alerts, inventory) are built on this connector after
the files are pinned and checked.

Package: `backend/src/meho_backplane/connectors/hcx/`.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `hcx.api_file.get` | Read one documentation page or API description file the HCX Manager serves. Returns path, HTTP status, content type, size, SHA-256 and the Manager version; the text is in parts behind a result handle. | `GET {path}` | read (`safe`, no approval) | 4.11, 9.0, 9.1 |

## Key types

- `HcxConnector` (`connector.py`) -- the connector class. Caches one
  session token per target and tenant (`target_cache_key`).
- `HcxTargetLike` (`session.py`) -- the minimum target shape: `id`,
  `tenant_id`, `name`, `host`, `port`, `secret_ref`, `auth_model`.
- `HcxSessionLoader` (`session.py`) -- an async callable
  `(target, operator) -> {"username", "password"}`. The default,
  `load_session_credentials_from_vault`, reads the target's `secret_ref`
  from Vault under the operator's identity through the shared
  `load_basic_credentials` helper. Tests inject a static loader.
- `HcxManagerVersion` (`connector.py`) -- `version`, `build`, `error`.
- `HcxTypedOp`, `HCX_TYPED_OPS`, `register_hcx_typed_operations`
  (`typed_ops.py`) -- the op metadata and the registrar that writes the
  `source_kind="typed"` row at start-up.
- `hcx_api_file_get_impl`, `check_path`, `split_parts` (`api_file.py`) --
  the read's body, the path rule and the part splitter.

## Control flow

### Registration

Importing the package registers the class twice (versioned and wildcard)
and queues `register_hcx_typed_operations` on the typed-op registrar list.
The lifespan runner writes the `hcx.api_file.get` descriptor row before the
first dispatch. No spec ingest is needed.

### Login

1. `POST /hybridity/api/sessions` with a JSON body
   `{"username": ..., "password": ...}`. The account is the vCenter single
   sign-on user registered with the HCX Manager, read from Vault.
2. The response header `x-hm-authorization` carries the session token.
   The connector caches it per target and tenant.
3. Every later call sends `x-hm-authorization: <token>`.
4. A `401` on a later call reaches the dispatcher, which calls
   `invalidate_session(target)` (drops the cached token) and dispatches
   once more. The next call logs in again. A `401` or `403` at login
   itself raises the structured `ConnectorAuthError` naming the target.

Only `auth_model=shared_service_account` (or unset) is accepted; other
modes raise `NotImplementedError` at the boundary.

### Version

`manager_version(target, operator)` sends
`POST /hybridity/api/hcspAppliances` with the body
`{"action": "query", "filter": {"local": "true"}}` and reads the first
appliance row that has a `version`. `build` is the row's `buildNumber`
when present, else the last part of a five-part version string such as
`4.3.0.0.19064502`. The helper never raises: a failure is named in
`error`. `fingerprint()` and `probe()` use it; so does the read below. A
later Task offers the same helper as the operation `hcx.about`.

### The read: `hcx.api_file.get`

Input: one field, `path`. Before any vendor call the path is checked
against this rule:

1. allowed: `/hybridity/docs` and every path under `/hybridity/docs/`;
2. allowed: a Swagger or OpenAPI file at its usual place -- a path with a
   part named `api-docs`, `swagger-resources` or `swagger-config`, or a
   path ending in `swagger.json`, `swagger.yaml`, `openapi.json` or
   `openapi.yaml`;
3. refused: everything else, and any path with `..`, `//`, `\`, an
   encoded dot or slash (`%2e`, `%2f`), a semicolon (`;`), a scheme, a host
   name, a fragment, whitespace or a control character. A query string may hold only
   `group` and `apiVersion`.

A refused path stops with an error and sends no HTTP request.

Then the read sends one `GET` to the target's own address with the
target's own login. It does not follow a redirect: a `3xx` answer is
reported with its status, and `redirect_to` holds the path to read next
(the same rule applies to that path). A relative `Location`, such as
`swagger.json` or `?group=x`, is resolved against the path that was read,
so `redirect_to` is always a full path. A `Location` on another host is
reported as `scheme://host/path`, so the caller sees it cannot be read
through this target. Only a `401` is raised, so the dispatcher can log in
again; every other status (for example `404`) is reported in `status`
with the body as text.

The body is read in a stream and counted after decoding, so a small
compressed answer that decodes to a file over the limit is refused at the
limit and never held whole in memory.

Only UTF-8 text comes back. A content type that is not text (anything
other than `text/*`, JSON, YAML, XML or JavaScript) or a body that is not
valid UTF-8 is refused, and the error names the content type.

The dispatcher runs the shared secret-redaction rules on every answer
before the caller sees it. A documentation page can hold an example that
looks like a secret (for example an `Authorization` header line). Such a
page would come back changed while `sha256` still names the original.
The read runs the same rules on its answer first and stops with an error
that names the rules and the number of places. Redaction stays on; the
read just never claims an exact file it cannot return.

The answer:

| Field | Meaning |
|---|---|
| `path` | The path that was read, with its query string. |
| `status` | The HTTP status. |
| `content_type` | The `Content-Type` header. |
| `size` | Size of the file in bytes. |
| `sha256` | SHA-256 of the file, hex. |
| `version`, `build` | The Manager version and build from the version helper. |
| `version_error` | Why the version read failed; `null` when it worked. The file still comes back. |
| `redirect_to` | Path of a redirect answer; `null` otherwise. |
| `parts` | How many text parts there are. |
| `items` | The parts, `{index, text}`, at most 64 KiB each, in order. |

### Rebuilding a file from the parts

The file is split on UTF-8 character boundaries into parts of at most
64 KiB, so each part decodes on its own. Joining the `text` of every part
in `index` order gives the exact file; the SHA-256 of the joined bytes
equals `sha256`.

A short page (under about 4 KB and 50 parts) comes back inline. A longer
file is set-shaped, so MEHO stores the parts behind a result handle and
keeps the scalar fields (`sha256`, `size`, `version`, ...) inline. Read the
parts with `result_query` in `index` order and join them.

Why parts and not lines: a served file can be one long line. One result
handle keeps at most 10,000 rows (`result_handle_max_spill_rows`) and
16 MiB (`result_handle_max_record_bytes`), and one `result_query` answer
is at most 256 KiB (`result_query_max_output_bytes`). The byte limit
applies to the whole stored record: the parts, the record's own fields
and the answer's scalars. The read measures that record with
`rows_record_bytes` from the result store before it answers. A file whose
record does not fit stops with an error that names the file's size and
the record's size. The read never returns a cut file.

### Shutdown

`aclose()` clears the token cache and closes the HTTP pool. No logout call
is sent.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`): the pooled per-target
  client, TLS trust, SSRF guard, flight-recorder spans.
- `connectors/_shared/vault_creds.py`: the operator-context credential
  read.
- `connectors/_shared/vcf_auth.py`: `session_establish_auth_error`.
- `connectors/result_handle_store.py`: `rows_record_bytes`, the size of
  the stored record the read checks against the limit.
- `redaction/middleware.py`: `apply_connector_boundary_redaction`, the
  rules the read runs on its answer before it returns.
- `operations/typed_register.py`: the typed-op registrar.
- `settings.py`: the result-handle limits the read checks against.

## Known issues

- The HCX API files are not pinned yet, so the reconcile lane
  (`tests/test_connectors_hcx_spec_reconcile.py`) skips its served check
  until the shelf holds `hcx-4.11/`, `hcx-9.0/` and `hcx-9.1/`. The
  manifest-pin test runs always.
- The login, the version call and the documentation path come from the
  vendor's public guides. Their first live use is the pinning of the HCX
  files. If a call differs there, fix the call and the table above.
- The appliance management API on port 9443 serves its own documentation.
  No vendor page names its path, so this read does not cover it yet.
- `build` is derived from the version string when the appliance row has no
  `buildNumber`; the live pin confirms the row's field names.

## References

- Task #4608 (this connector, login, version helper and the read);
  #4545 (the reads that build on it); #4395 (the same read for NSX);
  #4505 (the same shape for Site Recovery Manager).
- `docs/decisions/spec-reconcile-guards-standard.md` -- the waiting lane.
- `docs/codebase/connectors-nsx.md` -- the sibling connector this one
  mirrors.
