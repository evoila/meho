# Connector: hcx (VMware HCX 4.11, 9.0 and 9.1)

## Overview

The `hcx` connector is the hand-written `HttpConnector` subclass for VMware
HCX Manager. It is registered under `(product="hcx", version="9.0",
impl_id="hcx-rest")` and as the product wildcard, so a target with no
version yet still resolves to it. The class pin tracks the VCF 9 line; the
`>=4.7,<10.0` range keeps HCX 4.x dispatchable through the same class.

Today the connector ships:

- the **login** to an HCX Manager,
- a **version helper** that reads the Manager version,
- the **read** `hcx.api_file.get`, which fetches the API documentation
  that a running HCX Manager serves, and
- the **HCX Manager setup** (`hcx.manager.*`): connect vCenter, SSO and
  NSX, set the location and role mapping, activate, and restart the
  application service, through the appliance management API on port 9443,
  and
- **thirteen reads** of the usual work: the Manager version and role, site
  pairings, service meshes, appliances and their tunnels, the service
  mesh diagnostics (start a run, list the runs, read the findings), jobs,
  alerts and the inventory for a migration (resource containers, the
  compute tree, datastores and networks), and
- **four service mesh changes** (create, change, resync, delete) with the
  status read `hcx.task.get`. Each change waits for a person's approval, and
- **network extension**: one read of the extended networks and three
  changes that each wait for a person's approval: extend a network,
  unextend it, and set Mobility Optimized Networking (see "Network
  extension" below), and
- **seven migration operations**: validate, start, list and cancel
  migrations, list and set mobility groups, and set the switchover window
  (see "The migration operations").

Why `hcx.api_file.get` came first: Broadcom publishes no file and no
versioned reference of the main HCX API (`/hybridity/api/...`). Only a
running HCX Manager serves its documentation. This read is how the vendor
API files get pinned.

Where the paths of the reads come from: no HCX API file is pinned yet.
The paths come from Broadcom's public reference "HCX Workload Migration
APIs", which names no HCX version, and for the calls under
`/hybridity/api/service/inventory/` other than `resourcecontainer/list`
(`cloud/list`, `vc/list`, `vc/datastores/query`, `networks`) from VMware's
own Terraform provider for HCX. The reconcile lane checks them once the
files are pinned (see Known issues).

Package: `backend/src/meho_backplane/connectors/hcx/`.

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `hcx.api_file.get` | Read one documentation page or API description file the HCX Manager serves. Returns path, HTTP status, content type, size, SHA-256 and the Manager version; the text is in parts behind a result handle. | `GET {path}` | read (`safe`, no approval) | 4.11, 9.0, 9.1 |
| `hcx.about` | The Manager version, build, system type and role (connector or cloud), and its local site. | `POST /hybridity/api/hcspAppliances`, `POST /hybridity/api/service/inventory/cloud/list` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.site_pairing.list` | The site pairings: one row per paired site with its id, cloud type, address and user. | `GET /hybridity/api/cloudConfigs` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.service_mesh.list` | The service meshes with their state, services, status and last task. | `GET /hybridity/api/interconnect/serviceMesh` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.appliance.list` | The interconnect, network extension and other appliances: state, overall, transport and tunnel status, tunnel counts, version and build. | `GET /hybridity/api/interconnect/appliances` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.service_mesh.diagnose` | Start the diagnostics of one service mesh and return the started task. It changes no configuration. | `POST /hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnose` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.service_mesh.diagnostics.list` | The diagnostics runs of one service mesh: run id, status, message, times, who started it, the appliances tested. Optional status filter. | `POST /hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnostics/query` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.service_mesh.diagnostics.get` | The findings of one diagnostics run (`details`), with its status, message, times and the Manager's warnings and errors. | `GET /hybridity/api/interconnect/serviceMesh/{serviceMeshId}/diagnostics/{serviceMeshDiagId}` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.job.get` | One job: done, failed, cancelled, progress and times. | `GET /hybridity/api/jobs/{jobid}` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.alert.list` | The alerts, with an optional filter by severity, state, module, category or object. | `POST /hybridity/api/alerts?action=query` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.inventory.list` | The resource containers (vCenter instances) of the local and the paired sites, for planning a migration. | `POST /hybridity/api/service/inventory/resourcecontainer/list` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.inventory.compute.list` | The vCenter instances HCX knows with their datacenters and clusters, as a flat list with parent ids. | `POST /hybridity/api/service/inventory/vc/list` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.inventory.datastore.list` | The datastores of one or more clusters of a vCenter instance. | `POST /hybridity/api/service/inventory/vc/datastores/query` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.inventory.network.list` | The networks (port groups, segments) of one site. | `POST /hybridity/api/service/inventory/networks` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.service_mesh.create` | Start the creation of a service mesh from two compute profiles (local, then paired site) and a list of services. Returns `accepted` with the interconnect task id. | `POST /hybridity/api/interconnect/serviceMesh` | change (`dangerous`) | 4.11, 9.x |
| `hcx.service_mesh.update` | Change a service mesh: its services, compute profiles, uplinks, switch pairs, bandwidth or traffic settings. Reads the mesh, merges the change, sends it back. Returns `accepted` with the task id. | `GET` then `PUT /hybridity/api/interconnect/serviceMesh/{serviceMeshId}` | change (`dangerous`) | 4.11, 9.x |
| `hcx.service_mesh.resync` | Resync a service mesh with its compute profiles: HCX computes the difference and applies it, and can redeploy appliances. Returns `accepted` with the task id. | `POST /hybridity/api/interconnect/serviceMesh/{serviceMeshId}/resync` | change (`dangerous`) | 4.11, 9.x |
| `hcx.service_mesh.delete` | Start the deletion of a service mesh; HCX removes its appliances. Needs a preview first. Returns `accepted` with the task id. | `DELETE /hybridity/api/interconnect/serviceMesh/{serviceMeshId}?force=` | change (`destructive`) | 4.11, 9.x |
| `hcx.task.get` | One interconnect task: state (QUEUED, RUNNING, SUCCESS, FAILED), progress, message, the vendor error, the steps. `not_reachable` when the Manager does not answer. | `GET /hybridity/api/interconnect/tasks/{interconnectTaskId}` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.network_extension.list` | The extended networks: stretch id, state, job id, source network, router and site at the other side, appliance, and the MON and egress flags. | `GET /hybridity/api/l2Extensions` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.network_extension.extend` | Extend a network to the other site. Starts a vendor job; returns `accepted` and the job id. | `POST /hybridity/api/l2Extensions` | change (`dangerous`, approval) | 4.11, 9.x |
| `hcx.network_extension.unextend` | Unextend a network, and if asked connect the cloud network to its local router. Starts a vendor job; returns `accepted` and the job id. | `DELETE /hybridity/api/l2Extensions/{extensionId}?connectToLocalRouter=` | change (`dangerous`, approval) | 4.11, 9.x |
| `hcx.network_extension.mon.set` | Turn Mobility Optimized Networking on or off, and set the policy routes. Two steps; lists what ran, what failed and what did not run. | `PUT /hybridity/api/networkExtensions/{extensionId}`, `PUT /hybridity/api/l2Extensions/policyRoutes` | change (`dangerous`, approval) | 4.11, 9.x |
| `hcx.migration.validate` | Check a planned migration or a mobility group before it starts. Returns `valid` and HCX's errors and warnings for the group and each VM. Changes nothing. | `POST /hybridity/api/mobility/groups/validate` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.migration.start` | Start all migrations of a mobility group, or make a group for VMs (bulk, vMotion, replication-assisted vMotion, cold) and start it. Returns `accepted` with the ids and the status read; `failed` or `partial` when HCX rejects the start, also with HTTP 200. | `POST /hybridity/api/mobility/groups/start`, `POST /hybridity/api/mobility/groups`, `POST /hybridity/api/migrations?action=query` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.migration.list` | The migrations with vendor state, outcome, progress, messages and times; filter by state, ids, VMs or group. | `POST /hybridity/api/migrations?action=query` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.migration.cancel` | Cancel migrations, or force-cancel a stuck one. Returns `accepted`; migrations that already ended are not sent. | `POST /hybridity/api/mobility/migrations/cancel`, `POST /hybridity/api/mobility/migrations/forceCancel`, `POST /hybridity/api/migrations?action=query` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.mobility_group.list` | The mobility groups (migration waves) with state and configuration status, or the summary of one group. | `POST /hybridity/api/v2/mobility-groups/query`, `GET /hybridity/api/v2/mobility-groups/{mobilityGroupId}/summary` | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.mobility_group.set` | Make a mobility group, or replace the settings of one. Starts nothing. | `POST /hybridity/api/mobility/groups`, `PUT /hybridity/api/mobility/groups` | change (`caution`, waits for approval) | 4.11, 9.x |
| `hcx.migration.schedule` | Set or clear the switchover window (and optionally the copy window) of a mobility group. | `POST /hybridity/api/mobility/groups/{mobilityGroupId}/schedule` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.manager.config.show` | How the HCX Manager is set up: vCenter, SSO lookup service, NSX, location, role mapping and activation state. Never a password or key. | port 9443: `GET /api/admin/global/config/vcenter`, `.../lookupservice`, `.../nsx`, `.../location`, `.../roleMappings`, then `GET /api/admin/global/config/hcx` (4.x) or `GET /license/hcx/activationStatus` (9.x) | read (`safe`, no approval) | 4.11, 9.x |
| `hcx.manager.vcenter.set` | Connect the HCX Manager to its vCenter (user and password from a Vault reference) and to the SSO lookup service. A restart makes it take effect. | port 9443: `POST /api/admin/global/config/vcenter`, then `POST /api/admin/global/config/lookupservice` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.manager.nsx.set` | Connect the HCX Manager to NSX (user and password from a Vault reference). | port 9443: `POST /api/admin/global/config/nsx` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.manager.location.set` | Set the site location: the one city of HCX's own list that matches. Refuses when none or several match. | port 9443: `GET /api/admin/global/config/searchCities?searchString={city}`, then `PUT /api/admin/global/config/location` | change (`caution`, waits for approval) | 4.11, 9.x |
| `hcx.manager.role_mapping.set` | Set which SSO groups get the HCX roles System Administrator and Enterprise Administrator (Migration Admin and Migration User on 9.1). Other roles stay. | port 9443: `GET`, then `PUT /api/admin/global/config/roleMappings` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.manager.activate` | 4.x: activate HCX with a key from a Vault reference. An active HCX (for example with VCF licensing from vCenter) answers `already_active`. 9.x has no key call and only reports the state. | port 9443: `GET` and `POST /api/admin/global/config/hcx` (4.x), `GET /license/hcx/activationStatus` (9.x) | change (`dangerous`, waits for approval) | 4.11 (with a key), 9.x (state only) |
| `hcx.manager.restart` | Restart the HCX application service so the setup takes effect. HCX is not usable for several minutes. Refuses while migrations run. | port 9443: `GET /components/appengine/status`, `POST /components/appengine?action=stop`, then `?action=start`; port 443: `POST /hybridity/api/migrations?action=query` | change (`dangerous`, waits for approval) | 4.11, 9.x |
| `hcx.manager.service.status` | The state of the HCX application service (RUNNING, STOPPED, STARTING), or `not_reachable` with the time. The status read of a restart. | port 9443: `GET /components/appengine/status` | read (`safe`, no approval) | 4.11, 9.x |

Every operation in the table exists on every version the connector
covers, so none stops early with a version message. `hcx.manager.activate`
sends a key only on 4.x; on 9.x it reports the state.

## Key types

- `HcxConnector` (`connector.py`) -- the connector class. Caches one
  session token per credential (`credential_cache_key`: tenant, secret
  and login host).
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
- `hcx_api_file_get_impl`, `check_path` (`api_file.py`) -- the read's body
  and the path rule; the fetch, the file check, the part splitter and the
  flight recorder call are the shared steps in `_shared/api_file_read.py`.
- `HCX_READ_OPS`, `HCX_READ_WHEN_TO_USE_BY_GROUP` (`read_ops.py`) -- the
  metadata of the first eight reads and the group texts of all of them.
  `HCX_DIAGNOSTICS_OPS` (`diagnostics_ops.py`) and `HCX_INVENTORY_OPS`
  (`inventory_ops.py`) hold the other five. `HcxTypedOp`, the group keys
  and the schema helpers live in `op_types.py`, so every op module shares
  them.
- `HCX_SERVICE_MESH_OPS` (`service_mesh_ops.py`) and
  `service_mesh_writes.py` -- the service mesh changes, `hcx.task.get`,
  their `VENDOR_CALLS` and the delete preview.
- `typed_reads.py` -- the bodies of the thirteen reads, `VENDOR_CALLS`
  (every `(method, path)` they send; the reconcile lane checks them),
  `rows_of` (takes the rows out of `items` or `data.items`),
  `flatten_inventory_tree`, `redact_secrets` and `job_outcome` (the plain
  state of a job, from its flags).
- `HCX_NETWORK_EXTENSION_OPS`, `HCX_NETWORK_EXTENSION_WHEN_TO_USE_BY_GROUP`
  (`network_extension_ops.py`) -- the metadata and group text of the four
  network extension operations; their bodies, `VENDOR_CALLS` and the busy
  check `is_busy` are in `network_extension.py`.
- `migration_ops.py`, `migration_schemas.py`, `migration_body.py`,
  `migrations.py` -- the metadata, the shared schemas, the vendor body
  builders (`group_item`, `migration_outcome`) and the bodies of the seven
  migration operations, with `MIGRATION_VENDOR_CALLS` (the reconcile lane
  checks them).

- `HcxManagerSetupOps` (`manager_setup_handlers.py`) -- a mixin of
  `HcxConnector` with the handler methods of the `hcx.manager.*`
  operations, so `connector.py` stays small. The bodies are in
  `manager_setup.py` and `manager_restart.py`, the metadata in
  `manager_setup_ops.py` and `manager_setup_change_ops.py`.
- `appliance_session`, `ApplianceApi`, `HcxApplianceError`,
  `HcxManagerSetupError` (`appliance_api.py`) -- the access to the
  appliance management API (port 9443) and its two error types.

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
`error`. `fingerprint()` and `probe()` use it; so does the read below.
`hcx.about` reads the same call, but raises a vendor error instead.

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

The fetch and the file check are one shared step,
`fetch_checked_api_file` in `connectors/_shared/api_file_read.py`. The
size check of the stored record, the last redaction check and the flight
recorder call are a second shared step, `finish_api_file_read`. The
flight recorder (the stored record of each vendor call) gets the checked
copy of the file, never the body as it came, and only after every check
has passed: a file the read refuses, for any reason, is recorded with no
body. A `401` body goes through the same check before the error is
raised, or the error is raised with no body, also when the body is too
big or cannot be decoded; the status and the headers stay, so the login
retry still runs. So neither the stored call record nor the error result
can carry a password the Manager put in a file.

The body is read in a stream. The read asks for `gzip` or `deflate` only
and decodes the wire chunks itself with `zlib`, in steps that never
produce more than the room left under the limit. So a small compressed
answer that decodes to a file over the limit is refused at the limit, and
the decoded bytes never grow past it, not even for one step. The read
returns the whole file or fails: a body whose compressed stream ends
early is refused, and so are bytes after a `deflate` stream. A `gzip`
body made of several members (which the gzip format allows) is decoded
whole, under the same limit. An answer with any other content encoding
is refused with the encoding named.

Only UTF-8 text comes back. A content type that is not text (anything
other than `text/*`, JSON, YAML, XML or JavaScript) or a body that is not
valid UTF-8 is refused, and the error names the content type.

Before the text is cut into parts, the read checks the whole file with
the shared API file secret check
(`connectors/_shared/api_file_secrets.py`, see
`connectors-shared-api-file-secrets.md`). The file is parsed (JSON, YAML,
XML or HTML; the content type decides, or the file ending when the type
is `text/plain` or missing), every value under a secret name is replaced
with `[REDACTED:param_name]`, and the file is written back from the
parsed copy. A file that does not parse, or whose format is unknown
(JavaScript, CSS, plain text), is refused with the reason. The answer
carries the count in `secrets_replaced`; `size` and `sha256` describe the
checked copy. A redirect path gets the same check on its query string.

The dispatcher runs the shared secret-redaction rules on every answer
before the caller sees it. A documentation page can still hold free text
that looks like a secret (for example an `Authorization` header line in
a text block). Such a page would come back changed while `sha256` names
the copy as checked. The read runs the same rules first, on the whole
text and then on its answer, and stops with an error that names the
rules and the number of places. The whole text is checked because a
secret whose label ends one part and whose value starts the next is not
seen in either part alone. The rules skip the placeholder, so a replaced
value does not set them off. Redaction stays on; the read just never
claims a file it cannot return.

The answer:

| Field | Meaning |
|---|---|
| `path` | The path that was read, with its query string. |
| `status` | The HTTP status. |
| `content_type` | The `Content-Type` header. |
| `size` | Size of the checked copy in bytes. |
| `sha256` | SHA-256 of the checked copy, hex. |
| `secrets_replaced` | How many values under a secret name were replaced in the copy. |
| `version`, `build` | The Manager version and build from the version helper. |
| `version_error` | Why the version read failed; `null` when it worked. The file still comes back. |
| `redirect_to` | Path of a redirect answer; `null` otherwise. |
| `parts` | How many text parts there are. |
| `items` | The parts, `{index, text}`, at most 64 KiB each, in order. |

### Rebuilding a file from the parts

The checked copy is split on UTF-8 character boundaries into parts of at
most 64 KiB, so each part decodes on its own. Joining the `text` of every
part in `index` order gives the copy; the SHA-256 of the joined bytes
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

### The thirteen reads

The op metadata lives in three modules: `read_ops.py` (the first eight
reads), `diagnostics_ops.py` (the two diagnostics reads) and
`inventory_ops.py` (the three inventory reads below the containers).
`op_types.py` holds the shared record and schema helpers. The bodies of
all thirteen are in `typed_reads.py`.

Groups (the text the agent reads in `list_operation_groups`):

| Group | Reads |
|---|---|
| `hcx-manager` | `hcx.about`, `hcx.site_pairing.list` |
| `hcx-service-mesh` | `hcx.service_mesh.list`, `hcx.appliance.list`, `hcx.service_mesh.diagnose`, `hcx.service_mesh.diagnostics.list`, `hcx.service_mesh.diagnostics.get`, and the changes `hcx.service_mesh.create`, `hcx.service_mesh.update`, `hcx.service_mesh.resync`, `hcx.service_mesh.delete` |
| `hcx-jobs-alerts` | `hcx.job.get`, `hcx.task.get`, `hcx.alert.list` |
| `hcx-inventory` | `hcx.inventory.list`, `hcx.inventory.compute.list`, `hcx.inventory.datastore.list`, `hcx.inventory.network.list` |
| `hcx-network-extension` | `hcx.network_extension.list`, `hcx.network_extension.extend`, `hcx.network_extension.unextend`, `hcx.network_extension.mon.set` |

What each read sends and returns:

- `hcx.about` sends the version call (above) and
  `POST /hybridity/api/service/inventory/cloud/list` with
  `{"filter": {"local": true, "remote": false}}`. It returns `version`,
  `build`, `system_type`, `system_name`, `role` and the local site's
  `local_endpoint_id`, `local_endpoint_name`, `local_endpoint_type`,
  `local_url`. `role` is `cloud` when `systemType` names "cloud",
  `connector` when it names "connector", else `unknown`. Unlike the
  version helper, `hcx.about` raises a vendor error, so a `401` makes a
  new login.
- The list reads (`hcx.site_pairing.list`, `hcx.service_mesh.list`,
  `hcx.appliance.list`, `hcx.service_mesh.diagnostics.list`,
  `hcx.alert.list` and the four inventory reads) return
  `{"items": [...]}`: one row per vendor object, taken out of the vendor
  envelope (`items` or `data.items`). The vendor fields stay. MEHO stores
  a long list behind a result handle; the agent reads it with
  `result_query`.
- `hcx.service_mesh.list` adds `last_task_id`, `last_task_type` and
  `last_task_status` (from `lastTaskDetails`) to each row.
- `hcx.appliance.list` adds `state`, `overall_status`, `transport_status`,
  `tunnel_status`, `tunnels_active`, `tunnels_total`, `version`
  (`major.minor.patch`), `build` and `update_available` to each row.
- `hcx.service_mesh.diagnose` takes `service_mesh_id` and sends an empty
  JSON body. The Manager answers `202` with the task id. The read returns
  `service_mesh_id`, `interconnect_task_id`, `warnings` and `errors`.
- `hcx.service_mesh.diagnostics.list` takes `service_mesh_id` and an
  optional `status` list. It sends `{"filter": {}}`, or
  `{"filter": {"status": [...]}}`, and returns one row per run: `diagId`,
  `serviceMeshId`, `status`, `message`, `reason`, `triggeredBy`,
  `applianceIds`, `startTime`, `endTime`. The flow after a diagnose call:
  list the runs, take the newest, and when its status says it is done,
  read it.
- `hcx.service_mesh.diagnostics.get` takes `service_mesh_id` and `diag_id`
  and returns the run's fields flat, plus `details` (the findings, as the
  Manager returns them), `warnings` and `errors`.
- `hcx.job.get` takes `job_id` and returns the vendor job object
  (`jobId`, `isDone`, `didFail`, `isCancelled`, `isRolledBack`,
  `percentComplete`, `startTime`, `endTime`, ...) plus three fields:
  `outcome` (from the flags: `failed`, `cancelled`, `rolled_back`,
  `succeeded`, `queued`, else `running`; a failure wins), `error` (the
  job's `errorMessage`, `error` or `errors`, when it has one) and
  `checked_at`. When the Manager does not answer (a connection or time-out
  error after the retries, for example during its own upgrade), it
  returns `{jobId, outcome: "not_reachable", error: null, checked_at}`:
  not `failed`. A certificate MEHO does not trust, and an address MEHO
  refuses, are raised as errors instead. A `401` makes a new login and
  the read runs again, so a login that expired since the start does not
  lose the job. MEHO keeps no job state between calls.
- `hcx.alert.list` sends `?action=query`. Optional `severity` (a list),
  `state`, `module`, `category` and `entity_id` go into the body's
  `filter` (`entity_id` as `entityId`); with none set the body is `{}`.
- `hcx.inventory.list` takes an optional `site`: `local` sends
  `{"filter": {"cloud": {"local": true, "remote": false}}}`, `remote` the
  opposite, and no `site` sends `{}`.
- `hcx.inventory.compute.list` sends `{}` and gets a tree: one vCenter
  instance per item, its datacenters as `children`, their clusters as
  `children` again. The read flattens it (`flatten_inventory_tree`): one
  row per node, parents before children, without `children`, with
  `parent_id` (the parent's `entity_id`) and `depth` (0, 1, 2). The vendor
  fields are `entity_id`, `name`, `entityType`, `vcenter_instanceId`.
- `hcx.inventory.datastore.list` takes `vcenter_id` and `cluster_ids` and
  sends `{"filter": {"vcenter_instanceId": ..., "computeType":
  "ClusterComputeResource", "computeIds": [...]}}`. Rows: `id`, `name`,
  `entity_type`.
- `hcx.inventory.network.list` takes `endpoint_id` and sends
  `{"filter": {"cloud": {"endpointId": ...}}}`. Rows: `entity_id`, `name`,
  `entityType`.

Secrets: every read replaces a field whose name says password, secret,
token, authorization, private key or licence key (any case, with or
without `_` or `-`) with `***REDACTED***`, at any depth. A name that ends
with one of these words (for example `vcPassword`) counts too. The key stays,
so the shape does not change. The dispatcher's own redaction runs after
that.

The same rule protects the flight-recorder trace. The shared trace
redaction knows secrets by shape (a bearer token, a key) and by op family
(a login); a stored pairing password in a `cloudConfigs` row is neither.
So `HcxConnector.trace_response_body` parses every vendor answer and masks
the secret-named fields before `record_vendor_call` stores the span (the
hook is `HttpConnector.trace_response_body`, see
`docs/codebase/redaction.md`). A test dispatches `hcx.site_pairing.list`
with capture on and checks the stored span.

### The service mesh changes

The metadata lives in `service_mesh_ops.py`, the bodies and the delete
preview in `service_mesh_writes.py` (`VENDOR_CALLS` lists every call).

Risk and approval:

- `hcx.service_mesh.create`, `update` and `resync` are `dangerous` and
  have `requires_approval: true`. A person's call waits until a second
  person approves it. An agent's call is refused unless the agent holds a
  grant; with a grant it waits for approval.
- `hcx.service_mesh.delete` is `destructive`. A person runs it: first
  `preview_operation` with the same inputs, then `call_operation` with the
  `preview_hash`. It then waits for a second person. An agent is always
  refused.

What each change sends:

- `hcx.service_mesh.create` takes `name`, `compute_profiles` (two
  entries: the local site's, then the paired site's: `endpoint_id`,
  `compute_profile_id`, optional names and `uplink_networks`) and
  `services` (`INTERCONNECT`, `VMOTION`, `BULK_MIGRATION`, `RAV`,
  `NETWORK_EXTENSION`). Optional: `switch_pairs` (for network extension),
  `uplink_max_bandwidth`, `app_path_resiliency`, `tcp_flow_conditioning`.
  It sends them in the vendor field names (`computeProfiles`,
  `switchPairCount`, `wanoptConfig`, `trafficEnggCfg`). Disaster Recovery
  and WAN optimisation are left out: HCX 9.0 removed them.
- `hcx.service_mesh.update` takes `service_mesh_id` and at least one
  change: `services`, `compute_profiles`, `uplinks` (new uplink networks
  of one compute profile the mesh uses) or one of the optional settings.
  The vendor `PUT` needs the whole mesh, so the change reads it
  (`GET .../serviceMesh/{serviceMeshId}`), replaces the fields that were
  set, and sends back only the vendor's mesh fields (`serviceMeshId`,
  `name`, `isLocalMesh`, `orchestratorInfo`, `switchPairCount`,
  `wanoptConfig`, `services`, `computeProfiles`, `trafficEnggCfg`). An
  uplink change for a profile the mesh does not use is refused before the
  `PUT`.
- `hcx.service_mesh.resync` takes `service_mesh_id` and sends
  `POST .../serviceMesh/{serviceMeshId}/resync` with no body.
- `hcx.service_mesh.delete` takes `service_mesh_id` and `force` (default
  false; set it when the paired site does not answer) and sends
  `DELETE .../serviceMesh/{serviceMeshId}?force=true|false`.

These are vendor jobs: the Manager does the work after the call returns.

1. **Start.** The Manager answers `202` with
   `data.interconnectTaskId`. The change returns at once with
   `{status: "accepted", service_mesh_id, interconnect_task_id,
   status_read: "hcx.task.get", warnings, errors}`. `accepted` never means
   the work is done. An answer without a task id is an error that carries
   the Manager's error text.
2. **Running check.** Before the start, the change asks
   `POST /hybridity/api/interconnect/tasks/query` for `QUEUED` and
   `RUNNING` tasks (with `entityIds` = the mesh id). If a task runs on the
   same mesh (for create: on a mesh with the same name), the change
   returns `status: "already_running"` with that task's id, type and state
   and starts nothing. The query is paged: the body carries
   `paging: {offset, pageSize}` (100 rows a page, `offset` counts rows)
   and the answer `paging.totalSize`. The check reads page after page
   until a task matches or the rows end, at most 50 pages. When the rows
   do not end within 50 pages, the check is incomplete: the change fails
   with an error and starts nothing. This is a help, not a lock: two
   callers can both check before either starts.
3. **Status.** `hcx.task.get` reads
   `GET /hybridity/api/interconnect/tasks/{interconnectTaskId}` and returns
   `status` (the vendor state), `done`, `progress`, `message`, `error`
   (the task message and every step error, when the state is `FAILED`),
   the object it works on, the times, the steps and `checked_at`. The
   caller reads it until `done` is true. A `401` makes a new login, so an
   expired login between the start and the read does not matter. When the
   Manager does not answer (for example during its own upgrade), the read
   returns `status: "not_reachable"` with the task id and `checked_at`; it
   never says `failed` for that. An untrusted certificate or an address
   MEHO refuses is still reported as an error.
4. **Lost reply.** After a lost reply the caller does not start again.
   It reads `hcx.service_mesh.list` (the mesh's `last_task_id` and
   `last_task_status`) or `hcx.task.get`, and decides from what it finds.
5. **Async mode.** Callers should use MEHO's async mode (`async: true`,
   see `async-governed-dispatch.md`) for a start. MEHO then stores the
   answer, with the task id, under a run handle, so a lost reply does not
   lose the task id. MEHO keeps no task state between calls.

None of the changes is a multi-step change on the vendor side: each
sends one change call. The read before the update `PUT` changes nothing.

The delete preview (`register_preview_builder`) reads the mesh and
`GET /hybridity/api/interconnect/appliances`, and fills `blast_radius`:
`object` (the mesh id, name, services and `force`), `children` (the
appliances of the mesh) and `irreversibility` (the appliances are
removed, migrations and network extensions through the mesh stop, a new
mesh needs a new create and a new deployment). When the mesh does not
exist (`404`), the preview fails, so the delete is refused before it
waits for approval.
### Network extension

The metadata is in `network_extension_ops.py` (group
`hcx-network-extension`), the bodies in `network_extension.py`.
`network_extension.VENDOR_CALLS` lists the five vendor calls for the
reconcile lane.

Risk: the list is `safe`. The three changes are `dangerous` with
`requires_approval: true`. A person's call waits until a second person
approves it. An agent needs a grant for `dangerous`, and its call then
waits for approval too.

- `hcx.network_extension.list` sends `GET /hybridity/api/l2Extensions`
  and returns one row per extension. The vendor fields stay, and these
  are added flat: `stretch_id`, `state` (`operationStatus.state`),
  `job_id` (`operationStatus.jobId`, when the vendor gives it),
  `source_network_id`, `source_network_name`, `source_network_type`,
  `destination_network_name`, `destination_gateway_id`,
  `destination_endpoint_id`, `source_appliance_id`,
  `mobility_optimized_networking`, `egress_optimization`. A long list is
  behind a result handle.
- `hcx.network_extension.extend` takes the source network (`network_id`,
  `network_type`, optional `network_name`), the other site (`endpoint_id`,
  `resource_id`, optional names and types), `destination_gateway_id` (the
  router at the other site), `source_appliance_id`, `gateway`, `netmask`,
  and optional `dns`, `vc_guid`, `egress_optimization` and
  `mobility_optimized_networking`. It sends the body VMware's own
  Terraform provider sends (`sourceNetwork`, `destination`,
  `destinationNetwork.gatewayId`, `sourceAppliance`, `features`, ...).
- `hcx.network_extension.unextend` takes `stretch_id` and
  `connect_to_local_router` (default false). The flag goes out as the
  query parameter `connectToLocalRouter=true|false`: with true, the
  network at the other site is connected to its local router, so it stays
  routed there.
- `hcx.network_extension.mon.set` takes `stretch_id` and at least one of
  `mobility_optimized_networking` (true or false) and `policy_routes`
  (`allow` and `deny` subnet lists, optional `endpoint_id`; default: the
  other site of the extension). It reads the extension first; its row is
  the body of the MON change, with `features.mobilityOptimizedNetworking`
  set and `operationStatus` left out. Then step 1 sends
  `PUT /hybridity/api/networkExtensions/{extensionId}` and step 2
  `PUT /hybridity/api/l2Extensions/policyRoutes` with
  `{endpointId, allow, deny}`. The policy routes replace the routes of the
  site. A step fails on an HTTP error, on an answer whose `errors` list is
  not empty (both `outcome: rejected`, the step did not apply), or on a
  lost connection (`outcome: unknown`: the Manager may have taken the
  step; the list read shows it). The run stops there. The answer is
  `{status: accepted|partial|failed, stretch_id, steps_ran, step_failed:
  {step, error, outcome}|null, steps_not_run, warnings}`. Nothing is
  undone, and a step that ran is never lost from the answer. A `401` at a
  step makes a new login and one more send of that step only; the steps
  before it are not repeated. When the new login fails (the Manager
  rejects it, answers it with a fault, or does not answer), or the step
  gets a second `401`, the error is raised when no step ran yet, so the
  dispatcher reports the login failure; after a step ran, it is the
  failed step of the answer (`outcome: rejected`, the Manager did nothing
  with the step), with the login error as its text. A
  lost connection at the first step is raised when the request never
  left MEHO (a blocked destination, an untrusted certificate). Every
  vendor text in the answer (an error body, an `errors` or `warnings`
  message) goes through the secret filter: a secret-named field of a JSON
  error body is replaced before the body becomes text, and the text goes
  through the default redaction policy.

Vendor jobs (extend and unextend):

1. The start returns as soon as HCX took the job. The answer is
   `{status: "accepted", job_id, status_read: "hcx.job.get", ...}`. It
   never says the work is done.
2. Before the start, the operation reads the extension list. Extend
   starts nothing when the source network already has an extension, in
   any state (`already_extended`, with its `stretch_id`, `state` and
   `job_id`). Unextend starts nothing when the extension is gone
   (`not_found`) or when its state holds a word that says work still runs
   (`PROGRESS`, `RUNNING`, `PENDING`, `QUEUED`, `EXTENDING`, `DELETING`,
   `TEARING`, `UNEXTEND`, `UNSTRETCH`: `already_running`). The vendor
   publishes no fixed list of states and no list of running jobs. This
   check helps; it is not a lock. Two callers can both check before
   either starts.
3. After a lost reply, do not start again blindly. Read
   `hcx.network_extension.list` first. For extend, a row with the same
   source network means the job was started. For unextend, no row, or a
   row in a running state, means it was started.
4. Start the change in MEHO's async mode (`async: true`, see
   `async-governed-dispatch.md`). MEHO then keeps the answer with the job
   id under a run handle, so a lost reply does not lose the job id.
5. Follow the job with `hcx.job.get` until `outcome` is `succeeded`,
   `failed`, `cancelled` or `rolled_back`.

Where the calls come from: Broadcom's "HCX Workload Migration APIs"
reference, and VMware's Terraform provider and PowerCLI module for HCX,
which use the same `l2Extensions` calls for list, create and delete. The
reference marks the `l2Extensions` create and delete as deprecated. They
are still served, and they return the job id (`{"id": ...}`); the newer
`networkExtensions` delete answers with no job id. MON is set with
`PUT /hybridity/api/networkExtensions/{extensionId}`, the call the
reference names for "enable or disable MON". The Task first named
`PUT /hybridity/api/l2Extensions/{stretchId}`; the reference has no such
call.
### The migration operations

A migration in HCX belongs to a **mobility group** (a migration wave): a
named set of VM migrations with shared settings. The operations use the
mobility group calls of the vendor reference. The older calls
`POST /hybridity/api/migrations?action=validate|start|cancel` are marked
deprecated there, so MEHO does not use them; the migration list uses
`POST /hybridity/api/migrations?action=query`, which is not deprecated.

All of them sit in the group `hcx-migration`.

The `group` input (start, validate, `hcx.mobility_group.set`):

| Field | Vendor field |
|---|---|
| `name` | `name` |
| `migration_type`: `bulk`, `vmotion`, `rav`, `cold` | `transferParams.transferType` and `switchoverParams.switchoverType` (also each migration's `migrationType`): bulk `vSphereReplication`/`vSphereReplication`, vmotion `NO_OP`/`vMotion`, rav `vSphereReplication`/`RAV`, cold `NO_OP`/`ColdMigration` |
| `service_mesh_id` | `servicemeshId` |
| `source`, `destination` (`endpoint_id`, `resource_id`, names and types) | `source`, `destination` (`endpointType` and `resourceType` are `VC` when not given) |
| `placement` (`id`, `name`, `type`) | `placement` |
| `storage` (`datastore_id`, `datastore_name`, `disk_provision_type`) | `storage.defaultStorage` (`type: datastore`) |
| `network_mappings` | `networkParams.defaultMappings` |
| `switchover_options` | `switchoverParams.options` (`retainMac`, `forcePowerOffVm`, `upgradeHardware`, `upgradeVMTools`, `removeSnapshots`, `removeISOs`) |
| `vms` (`vm_id`, `vm_name`) | `migrations[].entity` (`entityType: VirtualMachine`) |

What each operation sends and returns:

- `hcx.migration.validate` takes `mobility_group_id` or `group` (one of
  them) and sends `{"items": [{"migrationGroupId": ...}]}` or
  `{"items": [<group>]}`. HCX answers a failed check with HTTP 400 and the
  same body; that is a result: `valid: false` with the errors.
- `hcx.migration.start` takes the same input. With `mobility_group_id` it
  sends `POST .../mobility/groups/start`. With `group` it has two steps:
  `create_group` (`POST .../mobility/groups`) and `start_group`. The answer:
  `status`, `mobility_group_id`, `migrations` (`migration_id`, `vm_id`,
  errors, warnings), `running`, `errors`, `warnings`, `steps`,
  `status_read` (`hcx.migration.list`) and `status_read_params`.
  `status_read_params` holds only known values, never null: the
  `mobility_group_id` when there is one, the `migration_ids` of the
  running migrations for `already_running`, or the `vm_ids` when no
  group was made. Passed as they are, `hcx.migration.list` accepts them.
- `hcx.migration.list` sends `{"filter": {...}}` with `skipDrafts: true`
  (unless `include_drafts`), and `state`, `migrationId`, `entityId`,
  `migrationGroupId` from the inputs. Each row keeps the vendor fields and
  adds `migration_id`, `mobility_group_id`, `vm_id`, `vm_name`, `outcome`,
  `status_message`, `error_message`, `start_time`, `end_time`. `outcome`
  comes from the vendor `progress` flags (`isCancelled`, `didFail`,
  `isDone`), else from the state (`MIGRATED`, `MIGRATE_FAILED`,
  `MIGRATE_CANCELED`), else `unknown`. Next to the rows: `reachable`,
  `checked_at`, `migration_ids`, `mobility_group_id`.
- `hcx.migration.cancel` takes `migrations` (`migration_id`, optional
  `mobility_group_id`) and `force`. It reads the migrations first; those
  that ended are listed under `already_ended` and not sent. It sends
  `{"items": [{"migrationId", "migrationGroupId"}]}` to `cancel`, or to
  `forceCancel` with `force: true`. `status` is `accepted`, or
  `already_ended` when nothing was left to send.
- `hcx.mobility_group.list` sends
  `{"filter": {"mobilityGroupName", "mobilityGroupState"}}` (and
  `pageParameters` when `page_size` or `page_number` is set). With
  `mobility_group_id` it reads that group's summary instead. Rows:
  `mobility_group_id`, `name`, `state`, `configuration_status`,
  `is_archived`, the wave, pairing and service mesh, times and user;
  `page` holds HCX's page fields.
- `hcx.mobility_group.set` sends `POST .../mobility/groups` without
  `mobility_group_id`, or `PUT .../mobility/groups` with the id in the
  body. A change replaces the group's settings with the group given.
- `hcx.migration.schedule` takes `mobility_group_id`, `switchover` and an
  optional `transfer` window. A window is `start_time` and `end_time`
  (ISO 8601 with a time zone; the end after the start), or `clear: true`.
  It sends `{"mobilityGroupDefaults": {"switchover": {"startTime",
  "expiryTime"}}}` (epoch milliseconds) or `{"clearSchedule": true}`.
  The answer has the window sent, the counts of changed schedules and
  HCX's errors as `[{scope, id, code, message}]`.

#### Vendor jobs: start and cancel

HCX does the migration after the start call returns. So:

- A start or cancel returns as soon as HCX accepted it: `status:
  accepted`, the ids, and `status_read: hcx.migration.list` with
  `status_read_params`. It never says the work is done.
- `hcx.migration.list` is the status read. It gives the vendor state, the
  `outcome` and the vendor's error text. The caller reads it until every
  outcome is `succeeded`, `failed` or `cancelled`.
- Before a start, the operation lists the migrations of the same group or
  VMs. If one runs, it returns `already_running` with those migrations
  and starts nothing. Before a cancel, it lists the migrations and skips
  those that ended. This is a help, not a lock: two callers can both
  check before either starts.
- After a lost reply, do not start again blindly. Read
  `hcx.migration.list` with the `mobility_group_id` (or the VM ids) and
  decide from what it shows.
- Call the start and the cancel with MEHO's async mode (`async: true`,
  `docs/codebase/async-governed-dispatch.md`). MEHO then keeps the answer
  with the ids under a run handle, so a lost reply does not lose them.
- When the login expired between the start and the status read, the read
  gets a `401`; the dispatcher logs in again and runs the read once more.
- When HCX does not answer (a connection error or time-out, for example
  during its own upgrade), the status read returns `reachable: false`,
  the ids it was given and `checked_at`, and no rows. It does not say
  `failed`. A certificate MEHO does not trust, and an address MEHO refused,
  are raised on, so the dispatcher names the real reason. MEHO keeps no
  job state between calls.
- A start of VMs that fails at `start_group` returns `status: failed`
  (or `no_answer` when HCX did not reply), with `steps`: `create_group`
  done, `start_group` failed with the vendor's error. The new group stays;
  nothing is undone. Read `hcx.migration.list` with its
  `mobility_group_id`, then start it by id. When `create_group` fails, the
  vendor error comes back as the call's error and nothing ran.
- HCX can answer a start with HTTP 200 and still reject it: the `errors`
  of the group, or of each migration, are not empty. The operation reads
  them before it sets the status. Errors on the group, or on every
  migration, mean nothing started: `status: failed`, `start_group` failed
  with the messages. Errors on some migrations mean HCX started the
  others: `status: partial`, `start_group` partial, and the rejected
  migrations are those with errors in `migrations`. A start of VMs keeps
  its `create_group` done step in both cases; the group stays. An error
  counts by its presence, not by its text: one with only a `code` is a
  rejection too, and the code (else `rejected`) becomes the step's text.
- The error text of a step has its secrets removed before it leaves the
  connector: the value of each secret-named field of the vendor body
  (`"password": "..."`) is replaced, then the default redaction policy
  runs. The vendor's messages are kept; a body without messages becomes
  text only after that scrub. The same scrub runs on every text in the
  `errors` and `warnings` lists of an answer (the group's, and each
  migration's), so the copies next to the step error hold no secret
  either.
### The HCX Manager setup

The setup operations use the HCX Manager **appliance management API** on
port 9443 of the target's host, not the main API on port 443. Group:
`hcx-manager-setup`.

| Group | Reads | Changes |
|---|---|---|
| `hcx-manager-setup` | `hcx.manager.config.show`, `hcx.manager.service.status` | `hcx.manager.vcenter.set`, `hcx.manager.nsx.set`, `hcx.manager.location.set`, `hcx.manager.role_mapping.set`, `hcx.manager.activate`, `hcx.manager.restart` |

Login (`appliance_api.py`): HTTP basic with the HCX appliance account (the
`admin` user set at deployment) on every call. There is no session, so a
login cannot expire between a start and a status read: each operation
reads the account again. The account comes from the target:

- `extras.appliance_secret_ref`: a Vault reference to a secret with
  `username` and `password`, when the target sets it;
- else the target's own secret (`secret_ref`). On a new HCX Manager the
  appliance account is the only account, so this is the usual case.

Each operation opens one short-lived HTTP client to `https://<host>:9443`
with the target's TLS settings and the same address check as the main
client, and closes it at the end.

Secrets: a change never takes a password or key as input. It takes a
Vault reference (`vcenter_secret_ref`, `nsx_secret_ref`: fields `username`
and `password`; `activation_key_secret_ref`: field `activation_key`). MEHO
reads it under the caller's identity just before the call. HCX takes the
vCenter and NSX passwords as base64 text. Before a vendor error reaches
the answer, MEHO cuts every secret it sent (the value and its base64 form)
out of the vendor text. The flight recorder gets the request body with the
password and the key replaced. No answer holds a password or key.

What each change does:

- `hcx.manager.vcenter.set` -- two steps: register vCenter, then the SSO
  lookup service (`{"lookupServiceUrl", "providerType": "PSC"}`). A failure
  at step one raises: nothing changed. A failure at step two answers
  `status: failed` with `steps` (the step that ran, the step that failed
  with the vendor's error); vCenter stays registered, nothing is undone.
- `hcx.manager.nsx.set` -- one call.
- `hcx.manager.location.set` -- searches HCX's city list for `city`, keeps
  the rows whose `city` or `cityAscii` match (and `country`, `province`
  when given) and sends the one match with `PUT`. No match, or more than
  one, is refused with the names the search found.
- `hcx.manager.role_mapping.set` -- the vendor `PUT` replaces the whole
  mapping. So the change reads the mapping, replaces the groups of the
  roles it was given (`system_admin_groups`, `enterprise_admin_groups`) and
  sends all roles back. It keeps the role names the Manager uses: HCX 9.1
  renamed them to Migration Admin and Migration User. A group SSO does not
  know: HCX answers `isSuccess: false`, and that becomes an error.
- `hcx.manager.activate` -- reads the state first: `GET
  /api/admin/global/config/hcx` (4.x; active when it holds a key), else
  `GET /license/hcx/activationStatus` (9.x; `activationDone`). Active:
  `already_active`, no key is read. 4.x and not active: reads the key and
  sends `POST /api/admin/global/config/hcx` with the activation server
  (default `https://connect.hcx.vmware.com`). 9.x and not active: refused,
  because HCX 9 takes its licence from vCenter (VCF licensing).

The restart (`manager_restart.py`) is a vendor job with no job id:

1. `GET /components/appengine/status`. `STARTING` or `STOPPING` means a
   restart already runs: the answer is `already_running` and nothing
   starts. This check is a help, not a lock.
2. The migration check: skipped when no vCenter is registered (then no
   migration can run) or when `skip_migration_check` is true; else
   `POST /hybridity/api/migrations?action=query` with
   `{"filter": {"skipDrafts": true}}` on the main API. A row with
   `progress.isDone: false` (and not failed or cancelled) refuses the
   restart. If the check cannot run (for example because the SSO login does
   not work yet on a new Manager), the restart is refused and names
   `skip_migration_check`.
3. `POST /components/appengine?action=stop` (skipped when the service is
   `STOPPED`), then `?action=start`. Each call waits until HCX is done, up
   to 300 seconds.

The answer: `status` (`accepted`, `already_running`, `failed`, `no_answer`),
`job_id: null`, `service_state`, `migration_check`, `steps` and
`status_read: hcx.manager.service.status`. `accepted` never means HCX is
ready: follow `hcx.manager.service.status` until `running` is true, then
`hcx.about` until it answers. A failure after the stop leaves the service
stopped; a new `hcx.manager.restart` then only starts it.

After a lost reply, do not start again blindly: read
`hcx.manager.service.status` (and `hcx.manager.config.show` for the other
changes) and decide from what it shows. Run the restart in MEHO's async
mode (`async: true`, `docs/codebase/async-governed-dispatch.md`), so MEHO
stores the answer under a run handle and a lost reply does not lose it.

`hcx.manager.service.status` answers `state: not_reachable`,
`reachable: false` and `checked_at` when the Manager does not answer; it
never says the service failed.

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
- Broadcom's "HCX Workload Migration APIs" reference
  (https://developer.broadcom.com/xapis/hcx-workload-migration-apis/latest/):
  the paths and fields of the reads.
- VMware's Terraform provider for HCX (`hcx/utils.go` in
  https://github.com/vmware/terraform-provider-hcx): the inventory calls
  `cloud/list`, `vc/list`, `vc/datastores/query` and `networks`.
- `settings.py`: the result-handle limits the read checks against.
- VMware's PowerShell module for HCX (`VMware.HCX` on the PowerShell
  Gallery): the migration state names the status read knows.
- Broadcom's "HCX Manager Appliance Management APIs" reference for HCX
  9.1 (https://developer.broadcom.com/xapis/hcx-manager-appliance-management-apis/9.1/):
  the paths and fields of the setup calls.
- VMware's Terraform provider for HCX (`hcx/activation.go`, `vcenter.go`,
  `sso.go`, `location.go`, `role_mapping.go`, `jobs.go`) and VMware's
  PowerCLI example module for HCX (`VMware.HCX.psm1`): the 4.x key
  activation, the base64 passwords and the basic login on port 9443.

## Known issues

- The HCX API files are not pinned yet, so the reconcile lane
  (`tests/test_connectors_hcx_spec_reconcile.py`) skips its served check
  until the shelf holds `hcx-4.11/`, `hcx-9.0/` and `hcx-9.1/`. The
  manifest-pin test runs always and lists every call of the reads.
  When the files are pinned, add their folders to the spec-shelf sparse
  checkout and the required-file list in `.github/workflows/ci.yml`.
- There is no read of single virtual machines yet. Neither Broadcom's
  public reference nor VMware's Terraform provider for HCX names a call
  that lists the VMs of a site (the reference's `vms` calls list only the
  VMs on an extended network). The VM read waits for the pinned HCX API
  file, which a running Manager serves. Folders are not in the compute
  tree the Manager returns (vCenter, datacenters, clusters).
- The inventory calls other than `resourcecontainer/list` and the
  diagnostics calls come from the Terraform provider and the vendor
  reference, with no HCX version named. The datastore query always sends
  `computeType: ClusterComputeResource`, the only shape the provider uses.
- The service mesh changes and `hcx.task.get` come from the vendor
  reference ("Service Mesh" and "Tasks") and VMware's Terraform provider
  for HCX. Two details differ from the first plan of this work: the mesh
  changes answer with an interconnect task, which `hcx.task.get` reads
  (not `hcx.job.get`), and resync has its own call
  (`POST .../serviceMesh/{serviceMeshId}/resync`), so it is not a
  `generateDiff` call followed by a `PUT` (`generateDiff` only starts a
  task and returns no difference). The create call takes no `vcGuid`
  query; the reference names none. The vendor reference documents the
  paging of the task query (`paging: {offset, pageSize}` in the body,
  `paging: {offset, pageSize, totalSize}` in the answer) but not whether
  `offset` counts rows or pages; the check counts rows. The live proof
  will confirm them.
- The running check matches a task to the mesh by `entityDetails.id` (by
  `entityDetails.name` for create). Every running task on the mesh counts,
  also a diagnostics run, because the vendor task types are not
  documented.
- The uplink fields (`networks`, `overriddenUplink` in a compute profile
  entry) come from the vendor schema, which does not describe them; the
  live proof will confirm them.
- The network extension state names are not published. The busy check
  of unextend matches words in the state (see "Network extension"); the
  live proof confirms the real names. The job object's error field name
  (`errorMessage`) is not in the reference either; the read also looks
  at `error` and `errors`.
- The `l2Extensions` create and delete are marked deprecated in the
  vendor reference. If a later HCX version drops them, move to
  `POST /hybridity/api/l2NetworkExtensions` and
  `DELETE /hybridity/api/networkExtensions/{extensionId}` and read the job
  id from their answer.
- The vendor reference writes the job path as `/hybridity/api/jobs/{jobid}`
  (lower-case `id`); the reconcile lane uses that spelling.
- `role` in `hcx.about` is derived from `systemType`. The vendor upgrade
  article shows `HCX Cloud`; the value on a connector is confirmed by the
  live proof.
- The login, the version call and the documentation path come from the
  vendor's public guides. Their first live use is the pinning of the HCX
  files. If a call differs there, fix the call and the table above.
- The appliance management API on port 9443 serves its own documentation.
  No vendor page names its path, so this read does not cover it yet.
- `build` is derived from the version string when the appliance row has no
  `buildNumber`; the live pin confirms the row's field names.
- The migration operations follow the vendor reference "HCX Workload
  Migration APIs", which names no HCX version. Three calls differ from the
  first plan of the Task: the start, validate and cancel of single
  migrations use the mobility group calls, because the vendor marks
  `/hybridity/api/migrations?action=validate|start|cancel` as deprecated;
  and the reference changes a mobility group with
  `PUT /hybridity/api/mobility/groups` (the id in the body), not with the
  id in the path.
- The reference lists no values for `migrationType`. MEHO sends the
  switchover type (`RAV`, `vMotion`, `vSphereReplication`,
  `ColdMigration`), the value public HCX examples use. The live proof
  confirms it, and the epoch-millisecond unit of the schedule times.
- The check for a running migration of the same VMs (before a start of
  VMs) and the `vm_ids` filter of `hcx.migration.list` send an
  `entityId` filter on the migration query. The reference does not show
  that filter; the live proof must confirm that HCX accepts it.
- The reference gives no list of migration states. `outcome` reads the
  vendor progress flags first; the state names `MIGRATED`,
  `MIGRATE_FAILED` and `MIGRATE_CANCELED` come from VMware's PowerShell
  module for HCX.
- The setup calls are checked against Broadcom's 9.1 reference only; no
  appliance management API file is pinned, so their reconcile lane
  (`appliance_management_api.openapi3.json`) skips. The 4.x key activation
  call is not in the 9.1 reference; it comes from the Terraform provider
  and the PowerCLI module. The 9.1 reference marks the location calls as
  deprecated but names no new call.
- The 9.1 reference also has a role mapping for VCF SSO groups
  (`/api/admin/global/config/roleMappings/idp/groups`). The setup maps
  vSphere SSO groups only.

## References

- Task #4608 (this connector, login, version helper and the read);
  #4545 (the reads that build on it); #4548 (the service mesh changes);
  #4547 (network extension); #4546 (the migration operations); #4550 (the HCX Manager
  setup); #4395 (the same read for NSX); #4505 (the same shape for Site Recovery Manager).
- `docs/decisions/spec-reconcile-guards-standard.md` -- the waiting lane.
- `docs/codebase/connectors-nsx.md` -- the sibling connector this one
  mirrors.
