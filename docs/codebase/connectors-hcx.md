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
  compute tree, datastores and networks).

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
- `typed_reads.py` -- the bodies of the thirteen reads, `VENDOR_CALLS`
  (every `(method, path)` they send; the reconcile lane checks them),
  `rows_of` (takes the rows out of `items` or `data.items`),
  `flatten_inventory_tree` and `redact_secrets`.

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
| `hcx-service-mesh` | `hcx.service_mesh.list`, `hcx.appliance.list`, `hcx.service_mesh.diagnose`, `hcx.service_mesh.diagnostics.list`, `hcx.service_mesh.diagnostics.get` |
| `hcx-jobs-alerts` | `hcx.job.get`, `hcx.alert.list` |
| `hcx-inventory` | `hcx.inventory.list`, `hcx.inventory.compute.list`, `hcx.inventory.datastore.list`, `hcx.inventory.network.list` |

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
  `percentComplete`, `startTime`, `endTime`, ...).
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
  #4545 (the reads that build on it); #4395 (the same read for NSX);
  #4505 (the same shape for Site Recovery Manager); #4550 (the HCX Manager
  setup).
- `docs/decisions/spec-reconcile-guards-standard.md` -- the waiting lane.
- `docs/codebase/connectors-nsx.md` -- the sibling connector this one
  mirrors.
