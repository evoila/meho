# Connector: vr (vSphere Replication 8.8, 9.0, VCF 9.1 Protection and Recovery)

## Overview

The `vr` connector is the hand-written `HttpConnector` subclass for the
vSphere Replication REST API v2 (`/api/rest/vr/v2`). It is registered
under `(product="vr", version="9.1", impl_id="vr-rest")` and as the
product wildcard, so a target with no version yet still resolves to it.
The `>=8.8,<10.0` range keeps vSphere Replication 8.8 and 9.0 on the
same class: all three versions serve the same API v2.

Today the connector ships:

- the **login** to the vSphere Replication API,
- **version detection** (MEHO reads the version and build),
- two **server reads**: `vr.about` and `vr.api_file.get`, and
- five **replication reads**: pairings and replication servers,
  replications with their RPO state, replication issues, a VM check with
  the target datastores, and one task.

Broadcom does not offer the vSphere Replication 8.8 and 9.0 API files as
downloads; a running appliance serves them. `vr.api_file.get` is how the
lab pins those files.

Package: `backend/src/meho_backplane/connectors/vr/`.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `vr.about` | vSphere Replication server name, status, version and build, the local vCenter, plus a short view of the certificates. MEHO uses the same call to detect the version. | `GET /api/rest/vr/v2/info` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.api_file.get` | Read one API description file the appliance serves (the vSphere Replication API or the appliance setup API). Returns a checked copy (secret values replaced) with its size, SHA-256 and the version; the text is in parts of at most 64 KiB behind a result handle. | `GET /api/rest/vr/v2/open-api.yaml`, `GET /api/rest/configure/v2/open-api.yaml` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.pairing.list` | Pairings and replication servers. One flat row per pairing (local and remote vCenter and vSphere Replication server: id, name, URL, status, version, build); `replication_servers` holds one row per replication server (connected, retired, number of replications). | `GET /api/rest/vr/v2/pairings`, `GET /api/rest/vr/v2/pairings/{pairing_id}/vrs`, `GET /api/rest/vr/v2/pairings/{pairing_id}/vrs/{vr_id}/replication-servers` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.replication.list` | Replications: VM, RPO, status, last sync, RPO met or not, errors. With `include_disks`, also the disks of each replication (`disks`). | `GET /api/rest/vr/v2/pairings/{pairing_id}/replications?extended_info=true`, `GET /api/rest/vr/v2/pairings/{pairing_id}/replications/{replication_id}/disks` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.replication.issues` | Replication issues (for example RPO not met): object, text, WARNING or ERROR, where, when. | `GET /api/rest/vr/v2/pairings/{pairing_id}/replications/issues` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.vm.check` | Check a VM before replication (validation status, reasons, minimum RPO, supported features), and list the target datastores. | `GET /api/rest/vr/v2/pairings/{pairing_id}/vcenters/{vcenter_id}/vms/{vm_id}/capability`, `GET /api/rest/vr/v2/pairings/{pairing_id}/vcenters/{vcenter_id}/datastores` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |
| `vr.task.get` | State of a replication task: status, progress, times, error, result. | `GET /api/rest/vr/v2/tasks/{task_id}` | read (`safe`, no approval) | 8.8, 9.0, 9.1 |

Every operation exists on every covered version, so none needs a
version check yet. The 8.x API has no recovery, test recovery or
point-in-time list; the Tasks that add those operations add a version
check (9.0 and newer).

## Shared with the SRM connector

Site Recovery Manager and vSphere Replication are served by the same
REST API gateway. On VCF 9.1 one appliance serves both. They share the
login, the `x-dr-session` header, the server summary and the API file
read. This shared part lives in:

- `connectors/vmware_shared/dr_rest.py` -- the base class `DrRestConnector`
  (login, session cache, `invalidate_session`, version read,
  fingerprint, probe, `server_about`), `DrTargetLike`,
  `DrSessionLoader`, `DrServerVersion`, `parse_server_version`,
  `summarise_server_info`;
- `connectors/vmware_shared/dr_api_file.py` -- `dr_api_file_get`, the body of
  both `api_file.get` reads;
- `connectors/vmware_shared/dr_rest_schemas.py` -- the response schemas and the
  `llm_instructions` texts both connectors use.

`VrConnector` and `SrmConnector` set the product, the version range and
the paths (`api_base`, `session_path`, `info_path`, `api_files`). Each
class also defines its own operation entry methods, `about` and
`api_file_get`, which call the shared code. The entry methods are not on
the base class on purpose. MEHO stores, for each operation, the name of
the class that defines its method, and binds that method only to a
connector of that class. So `srm.about` cannot run on a `vr` target, and
`vr.about` cannot run on an `srm` target: the call ends with a
`connector_error` before any HTTP request, and the policy check and the
audit record never name one product's operation for the other product's
read. `docs/codebase/connectors-srm.md` describes the shared steps in
detail.

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
each target gets its own connector and its own login. The two targets
may name the same Vault path.

**vSphere Replication 8.8 and 9.0.** The SRM appliance and the vSphere
Replication appliance are separate machines. Register one target per
appliance: `--product vr` for the vSphere Replication appliance.

The login account is the vCenter single sign-on account of the site.
Only `auth_model=shared_service_account` (or unset) is accepted; other
modes raise `NotImplementedError`.

## Key types

- `VrConnector` (`connector.py`) -- the connector class, a subclass of
  `DrRestConnector`.
- `API_FILES` (`connector.py`) -- the two files `vr.api_file.get` can
  read.
- `VrTypedOp` and the group keys (`op_types.py`) -- the op metadata
  type.
- `VR_TYPED_OPS`, `register_vr_typed_operations` (`typed_ops.py`) -- all
  ops and the registrar.
- `VR_READ_OPS` (`read_ops.py`) -- the metadata of the five replication
  reads.
- `reads.py` -- the bodies of the five replication reads, the page
  reader and `VENDOR_CALLS` (every path these reads call).

## Control flow

### Registration

Importing the package registers the class twice (versioned and wildcard)
and queues `register_vr_typed_operations` on the typed-op registrar list.
The lifespan runner writes both descriptor rows before the first
dispatch. No spec ingest is needed.

### Login

1. `POST /api/rest/vr/v2/session` with HTTP Basic (the account read from
   Vault through `load_basic_credentials`).
2. The JSON answer carries `session_id`. The connector caches it per
   credential (`credential_cache_key`: tenant, secret and login host), so
   two `vr` targets that share one credential share one session. The `srm`
   and `vr` connectors keep separate caches: on a 9.1 appliance the `srm`
   target and the `vr` target each log in to their own API.
3. Every later call sends `x-dr-session: <session_id>`.
4. A `401` on a later call reaches the dispatcher, which calls
   `invalidate_session(target)`. That drops the session of this target's
   credential; the `srm` target on the same address keeps its own. The dispatcher
   then runs the call once more, and the connector logs in again. A
   `401` or `403` at login itself raises `ConnectorAuthError` naming the
   target.

### Version

`server_version(target, operator)` reads `GET /api/rest/vr/v2/info` and
takes `version` and `build_number`. It never raises: a failure is named
in `error`. `fingerprint()`, `probe()` and `vr.api_file.get` use it. A
`401` on this read drops this target's cached session. The flight
recorder (the stored record of each vendor call) gets the checked copy
of the file, never the body as it came, and only after every check has
passed; a `401` body is checked before the error is raised (see the SRM
page for the shared steps).

### `vr.about`

Returns the scalar fields of `info` (`id`, `name`, `url`,
`server_status`, `version`, `build_number`, `vc_guid`, `vc_name`,
`extension_key`). `certificates` and `certificate_warnings` come back as a
short view: subject, issuer, serial number, validity dates and thumbprint.
The PEM text, public key and signature are left out. `_meta` is left out.

### `vr.api_file.get`

Input: one field, `file`:

| `file` | Path on the appliance | What it is |
|---|---|---|
| `vr` | `/api/rest/vr/v2/open-api.yaml` | the vSphere Replication API |
| `configure` | `/api/rest/configure/v2/open-api.yaml` | the appliance setup API |

The input is a name, not a path, so no other path can be read. An unknown
name stops before any HTTP request. The answer has the same fields as
`srm.api_file.get` (`file`, `path`, `status`, `content_type`, `size`,
`sha256`, `secrets_replaced`, `version`, `build`, `version_error`,
`redirect_to`, `parts`, `items`). `items` holds the checked copy in
parts of at most 64 KiB, in order; joined, they are the copy whose
SHA-256 is `sha256`. A long file is stored behind a result handle; read
the parts with `result_query` in `index` order.

### The replication reads

`vr.pairing.list`, `vr.replication.list`, `vr.replication.issues`,
`vr.vm.check` and `vr.task.get` are thin methods on `VrConnector`; the
bodies are in `reads.py`. Groups: `vr-pairings`, `vr-replications`
(replications, issues, VM check) and `vr-tasks`.

- **Flat rows.** A list read returns `{items, count, more_rows, errors}`.
  A nested vendor object becomes a few named fields; for example
  `status.rpo_violation` becomes `rpo_violation`, and `rpo_met` is its
  opposite. MEHO stores a long list behind a result handle. Extra lists
  (`replication_servers`, `disks`) ride next to `items` and are stored
  with it.
- **Pages.** The API pages with `limit` and `offset`. A read asks for 100
  rows at a time until the last page, at most 50 pages; `more_rows` says
  when rows were left.
- **Partial data.** Without a login to the remote site, the server can
  answer with part of the data and the reasons in `_meta.errors`. The
  read returns them in `errors`. In `vr.pairing.list`, the replication
  servers are read once per vSphere Replication server of a pairing; a
  read that fails there (often the remote one) becomes an entry in
  `errors`, and the read goes on.
- **`include_disks`** costs one call per replication, so it runs for at
  most 50 replications. With more, the read stops before any disk read
  and asks for `name` (one replication, the VM name).
- **`vr.vm.check`** reads the capability of the VM on `vcenter_id` and
  the datastores on `target_vcenter_id` (default: `vcenter_id`).
  `replication_possible` is true when the status is `OK` and no reason
  blocks replication.
- **Ids** go into the path as one encoded segment; an id of `..` stops
  before any HTTP request.
- **Dates** stay as the API gives them: milliseconds since 1970.
- **Secrets.** No answer carries a password. The free-form `result` of a
  task comes back with any secret-like field name replaced by
  `[REDACTED]`.

### Shutdown

`aclose()` clears the session cache and closes the HTTP pool. No logout
call is sent.

## Dependencies

- `connectors/vmware_shared/dr_rest.py`, `dr_api_file.py`, `dr_rest_schemas.py`
  (shared with SRM, see above).
- `connectors/_shared/api_file_read.py`, `api_file_secrets.py`: the API
  file read steps and the secret check.
- `connectors/_shared/vault_creds.py`, `vcf_auth.py`: the credential read
  and the Basic header.
- `operations/typed_register.py`: the typed-op registrar.

## Tests

- `backend/tests/test_connectors_vr.py` -- login, both reads through
  `dispatch()`, an `srm` and a `vr` target on one address that resolve to
  their own class and log in on their own, a `401` on the `vr` target
  that logs in again for it only, registration shape, fingerprint. One
  test reads the pinned 9.1 files through `vr.api_file.get`; it runs
  only when the spec shelf is present.
- `backend/tests/test_connectors_vr_replication_reads.py` -- the five
  replication reads: paths and query, pages, flat rows, RPO fields,
  partial data, the disk limit, target datastores, task result secrets,
  a vendor error, a `..` id, a `401` that logs in again, metadata.
- `backend/tests/test_connectors_vr_spec_reconcile.py` -- the login,
  `info` and every replication read path against the pinned 9.1 file
  (`vcf-protection-and-recovery-9.1/replication-vr-v2.yaml` on the spec
  shelf). The 8.8 and 9.0 files join this lane once the lab pins them.
- The edge cases of the shared read (redirects, 404, files that do not
  parse, files too big, secret values) are tested once, in
  `backend/tests/test_connectors_srm.py`.

## Known issues

- **The copy is not the vendor file byte for byte.** The vSphere
  Replication file has a "getting started" text with example
  `Authorization: Basic ...` headers. The read returns them replaced
  (`[REDACTED:authorization_header]`) and counts them in
  `secrets_replaced`; the pinned 9.1 `replication-vr-v2.yaml` comes back
  with 7 values replaced. Pin and compare the copy, not the vendor bytes.
- **The remote site needs its own login.** Reads that touch the remote
  site (its replication servers, the datastores of the remote vCenter)
  can come back partial or with an error until the remote site is logged
  in. MEHO has no remote-site login for this connector yet; `errors` and
  the vendor error say so.
- The 8.8 and 9.0 calls follow the 9.1 file and the vendor documentation.
  Their first live use is the pinning of the 8.8 and 9.0 files. If a call
  differs there, fix the call and the table above.

## References

- Task #4506 (this connector); #4560 (the replication reads); #4505
  (the SRM connector, `connectors-srm.md`).
- `docs/codebase/connectors-shared-api-file-secrets.md` -- the secret
  check.
- `docs/decisions/spec-reconcile-guards-standard.md` -- the reconcile lane.
