# Connector: wsfc (wsfc-2022.x / `wsfc-ssh`)

## Overview

The `wsfc` connector is the typed `Connector` subclass that manages a **Windows
Server Failover Cluster** — cluster / node / group (clustered-role) / resource /
quorum state, cluster validation, and guarded role-move / failover writes — over
SSH → PowerShell (the built-in Windows PowerShell 5.1 `FailoverClusters` module
cmdlets). It is registered under the `(product="wsfc", version="2022.x",
impl_id="wsfc-ssh")` registry triple plus the `("wsfc", "", "")` wildcard
fallback row, and is a child of the shared `SshConnector` adapter — the same
transport base as `WinsrvConnector`, `WindowsDnsConnector`, and
`Rke2SshConnector`.

The `wsfc` connector is a **structured copy of the winsrv estate mold**
(`docs/codebase/connectors-winsrv.md`): identity canary + per-domain op groups
on the `SshConnector` base, built on the PowerShell-over-SSH transport
`_shared/pwsh` (base64 UTF-16LE `-EncodedCommand` + stdlib `json` parse), with
the `rke2` approval-parked-write mold for its destructive ops. It complements
evoila/meho#3256 (the vSphere shared-disk layer *beneath* the guest cluster);
this connector is the cluster layer *inside* it. Its named consumer is the
c1sql1 SQL Server FCI (evoila-bosnia/claude-rdc-hetzner-dc#2794 / #2795), whose
cluster state today is only visible by hand inside the guest.

**The target is any single cluster node.** The `FailoverClusters` cmdlets talk
to the local Cluster Service, which is the cluster-wide database, so they fan
out cluster-wide from whichever node runs them. One node is a sufficient control
point for the whole cluster — there is no per-node target fan-out to arrange.
Pick a stable node (or a node the satellite can reach) as the target.

Registry-triple rationale: `product="wsfc"` is a short, separator-free token
because `parse_connector_id` derives the product from the first hyphen-segment
of `impl_id`, so `connector_id="wsfc-ssh-2022.x"` round-trips (a
hyphen/underscore in the product token breaks the registry's round-trip guard at
boot). `version="2022.x"` marks the cmdlet surface (stable across Windows Server
2019 → 2025). `impl_id="wsfc-ssh"` leaves room for a future `wsfc-winrm` sibling.

Source: `backend/src/meho_backplane/connectors/wsfc/`.

## Op surface (19 ops across five groups)

Safety tier is a reach decision per the Initiative #3259 satellite table: reads
= `safe` (ride satellite runners); recoverable writes = `caution` (satellite-
executable only through the Stage-3 composed write gate, #2901); destructive =
`dangerous` + `requires_approval` (satellite-EXCLUDED by the tier ladder,
central-dial / on-site only).

| op | safety | approval | cmdlet(s) |
| --- | --- | --- | --- |
| `wsfc.about` | safe | no | `Get-Module FailoverClusters` + `Get-Cluster` + `Win32_OperatingSystem` |
| `wsfc.cluster.get` | safe | no | `Get-Cluster` + `Get-ClusterNode` + `Get-ClusterGroup` + `Get-ClusterResource` |
| `wsfc.cluster.quorum` | safe | no | `Get-ClusterQuorum` |
| `wsfc.cluster.validation-report` | safe | no | `Get-ChildItem %SystemRoot%\Cluster\Reports` |
| `wsfc.cluster.test` | caution | no | `Test-Cluster` (**long-running**) |
| `wsfc.nodes.list` | safe | no | `Get-ClusterNode` |
| `wsfc.nodes.state` | safe | no | `Get-ClusterNode -Name` |
| `wsfc.nodes.pause` | caution | no | `Suspend-ClusterNode -Drain` |
| `wsfc.nodes.resume` | caution | no | `Resume-ClusterNode` |
| `wsfc.nodes.evict` | **dangerous** | **yes** | `Remove-ClusterNode -Force` |
| `wsfc.groups.list` | safe | no | `Get-ClusterGroup` |
| `wsfc.groups.state` | safe | no | `Get-ClusterGroup -Name` |
| `wsfc.groups.move` | caution | no | `Move-ClusterGroup` |
| `wsfc.groups.offline` | **dangerous** | **yes** | `Stop-ClusterGroup` |
| `wsfc.groups.online` | **dangerous** | **yes** | `Start-ClusterGroup` |
| `wsfc.resources.list` | safe | no | `Get-ClusterResource` |
| `wsfc.resources.dependency-report` | safe | no | `Get-ClusterResourceDependency` |
| `wsfc.witness.get` | safe | no | `Get-ClusterQuorum` + `Get-ClusterResource` |
| `wsfc.witness.set` | caution | no | `Set-ClusterQuorum` |

The three `dangerous` ops (`nodes.evict`, `groups.offline`, `groups.online`) are
`requires_approval=True` — a dispatch parks at `needs-approval` for a human
decision (the rke2 write mold; no bespoke `proposed_effect` builder is
registered — the generic params-echo default (#1856) surfaces the target /
params redaction-safely to the reviewer). The **agent-principal ceiling**
therefore applies: an agent session cannot self-approve these — approval is a
human decision (v0.1-spec §7), so a dispatch from an agent floors at the approval
queue until an operator approves in the console / CLI. Reads ride satellite
runners; the `dangerous` ops are excluded from satellites by the tier ladder.

**Why `offline`/`online` are dangerous but `move` is caution.** A planned
node-to-node failover (`Move-ClusterGroup`) is a recoverable rebalance — the role
stays online, just on another node — so it is `caution` (the governed-failover
path the acceptance criteria want proven). Taking a production role *offline*
(`Stop-ClusterGroup`) is a service outage, and forcing one *online*
(`Start-ClusterGroup`) — e.g. to the wrong node, or while it should stay down —
can cause split-brain / data issues. Both are `dangerous` + approval per the
#3259 initiative table.

## Key types

- **`WsfcConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="wsfc"`, `version="2022.x"`, `impl_id="wsfc-ssh"`,
  `POWERSHELL_EXECUTABLE="powershell"`. Ships `fingerprint`, `probe`, `about`,
  the 18 bound-method op shims, `register_operations`, and the operator-less
  `execute` dispatcher shim. Inherits the per-target asyncssh connection pool,
  `_auth_config`, `_run_command`, `_assert_reachable`, and `aclose()` from the
  adapter.
- **Shared pwsh transport** (`_shared/pwsh.py`, #3260) — `pwsh_run` (the single
  seam every handler routes its script through), `ps_single_quote` (the
  `shlex.quote` analogue), `encode_pwsh_command`, `strip_clixml`, `PwshRunError`.
  Per-connector wire variation rides three class-level seams:
  `POWERSHELL_EXECUTABLE` (`powershell` here), `POWERSHELL_SCRIPT_PREFIX` (the
  `$ProgressPreference` guard), `POWERSHELL_LOG_EVENT` (`wsfc_pwsh_executed`).
- **Op metadata** (`ops.py`) — `WsfcOp` frozen dataclass (mirrors `WinsrvOp`),
  `WSFC_OPS` (`about` + the five per-domain tuples),
  `WSFC_WHEN_TO_USE_BY_GROUP` (curated per-group blurbs, imported into the
  connector — the rke2 / winsrv precedent), and `normalise_json_rows` (collapses
  `ConvertTo-Json`'s dict / list / null shapes to `list[dict]`).
- **Per-domain op modules** — `ops_cluster.py`, `ops_nodes.py`, `ops_groups.py`,
  `ops_resources.py`, `ops_witness.py`. Each owns its handlers + op tuple,
  imports `WsfcOp` from `ops.py`, and (for parameterized scripts)
  `ps_single_quote` from `_shared/pwsh`.
- **Registration** (`__init__.py`) — two-phase, mirroring winsrv / rke2:
  synchronous `register_connector_v2` at import time (versioned triple + wildcard
  row); async `register_wsfc_typed_operations` queued onto the lifespan registrar
  list. No v1 `register_connector` — no chassis history.

## Transport: PowerShell-over-SSH

Every op runs `powershell -NoProfile -NonInteractive -EncodedCommand <base64>`
over the pooled SSH connection — identical to winsrv (see
`docs/codebase/connectors-winsrv.md` for the full transport rationale). The
wsfc-specific points:

- **`powershell` (Windows PowerShell 5.1), not `pwsh` (PS7).** The connector sets
  `POWERSHELL_EXECUTABLE="powershell"` explicitly. The shared transport's
  fallback is `pwsh` (PS7), which a Windows Server host does NOT ship — the
  single most important footgun when copying the estate mold (see the winsrv doc,
  "building the next estate connector").
- **`$ProgressPreference = 'SilentlyContinue'` guard** — prepended to every
  script so the `FailoverClusters` module's first-use progress stream never
  serialises CLIXML noise onto the streams.
- **Hashtable envelope for list reads** — every list op wraps its result in
  `@{ rows = $x; total = $x.Count }` under `$ErrorActionPreference = 'Stop'`, so a
  zero-object pipeline is still JSON-shaped (never an empty-stdout `PwshRunError`)
  and a cmdlet error is a real failure, not a false-empty read. The `{rows, total}`
  envelope is what the dispatcher's JSONFlux reducer keys on to spill a set-shaped
  result (> 50 rows or > 4 KB) to a `result_query` handle. `nodes.list` /
  `groups.list` / `resources.list` / `resources.dependency-report` /
  `cluster.validation-report` are the list ops.
- **`Test-Cluster` is long-running** — `wsfc.cluster.test` forwards a raised
  15-minute `timeout` to `pwsh_run` (the transport default is 30 s). See
  *`cluster.test` duration semantics* below.

### Enum strings and output property names (grounding note)

The `FailoverClusters` cmdlet reference pages on Microsoft Learn document
parameters and the output *.NET type name* but do **not** tabulate the output
objects' property names or the enum member values. The property names this
connector reads (`State` / `OwnerNode` / `OwnerGroup` / `NodeWeight` /
`DynamicWeight` / `Id`) and the state strings the health rollup compares against
(`Up` / `Down` / `Online` / `Offline` / `Failed`) are the canonical
`Microsoft.FailoverClusters.PowerShell.ClusterNodeState` / `ClusterGroupState` /
`ClusterResourceState` members. To keep the rollup robust against a rendering /
casing difference, the count scripts compare the **stringified** state
(`"$($_.State)" -eq 'Up'` — case-insensitive in PowerShell) rather than the raw
enum, and `wsfc.cluster.get` additionally returns the raw per-state count maps
(`nodes_by_state` / `groups_by_state` / `resources_by_state`), so a state the
hardcoded scalars don't name still surfaces. The live c1sql1 probe (an OPEN
acceptance criterion) is where the exact strings get confirmed against a real
cluster.

### Injection safety

Operator-supplied strings (node / group name, target node, disk-resource name,
file-share path, `Test-Cluster -Include` categories) are interpolated only
inside **single-quoted** PowerShell literals via `ps_single_quote` (embedded `'`
doubled — complete escaping inside a single-quoted PowerShell string). The
`Resume-ClusterNode -Failback` value and the `witness_type` are validated against
bounded enums before they select a fixed cmdlet flag. Every non-parameterized
script (`cluster.get`, `cluster.quorum`, `validation-report`, the two `list`
reads, `dependency-report`, `witness.get`, the fingerprint / probe) is a constant
with no injection surface. A unit test decodes the `-EncodedCommand` base64 back
to the script and asserts the single-quote doubling on every parameterized op.

### No plaintext secret on the wire (deliberate)

The shared transport forbids credential material in the script (it lands on the
remote process argv). One consequence for wsfc: **`witness.set` has no cloud
witness.** `Set-ClusterQuorum -CloudWitness` needs `-AccessKey` (an Azure storage
account key) — a secret that would enter the script. Cloud witness is therefore
out of scope for this cut (`witness_type` is `disk` / `file_share` /
`node_majority` only); a Vault-brokered mint-and-stash flow is a follow-up, the
same pattern by which winsrv's `iscsi.connect` defers CHAP. A unit test
(`test_no_write_op_exposes_a_secret_value_field`) pins this at the schema level:
no op declares a `password` / `secret` / `access_key` parameter.

## `fingerprint(target)` / `probe(target)`

`fingerprint` runs one `-EncodedCommand` round-trip reading the node OS version /
build, the PowerShell version, whether the `FailoverClusters` module is present,
and — when the node is a cluster member — the cluster name and functional level;
returns `vendor="microsoft"`, `product="windows-failover-cluster"`,
`version=<OS version>`, `build=<build number>`, with `extras={hostname,
cluster_name, cluster_functional_level, failover_clusters_module,
powershell_version}`. Whether the node is *clustered* is metadata
(`cluster_name`), not a reachability signal — an unclustered but reachable node
is `reachable=True`. Any transport / credential failure maps to `reachable=False`
+ `extras["error"]` (#986). `about` (`wsfc.about`) wraps `fingerprint` +
`_assert_reachable`.

`probe` surfaces six distinct `ProbeResult.reason` values — the last two are the
cluster-membership reasons the issue asks for:

| reason | meaning |
| --- | --- |
| `tcp_unreachable` | the SSH TCP socket cannot connect |
| `ssh_auth_failed` | credentials rejected / handshake failed / Vault credential unresolvable |
| `powershell_unavailable` | SSH succeeded but the PowerShell reachability script failed |
| `command_failed` | post-connect transport failure (drop / timeout / socket error) after a successful handshake (#986) |
| `failover_module_absent` | PowerShell runs but the `FailoverClusters` module is not installed (not a cluster node) |
| `not_cluster_member` | the module is present but the node is not a member of a running cluster (`Get-Cluster` failed) |

## Auth

Uses the base `SshConnector._auth_config` **unchanged** (see the winsrv doc). The
transport prerequisite is an **OpenSSH server on the cluster node** with
`powershell` reachable, and the SSH principal must have cluster-administrative
rights (the `FailoverClusters` cmdlets require it). `probe()` carries no operator,
so its Vault read runs under the synthesised system operator and fails closed to
`ssh_auth_failed`.

## Control flow

1. **Boot** — `_eager_import_connectors()` imports the `wsfc` subpackage;
   `__init__.py` registers the v2 triple + wildcard synchronously and queues the
   typed-op registrar.
2. **Lifespan startup** — `run_typed_op_registrars()` →
   `register_wsfc_typed_operations` → `WsfcConnector.register_operations`, which
   upserts each `WSFC_OPS` entry into `endpoint_descriptor` (idempotent across
   restarts) with the curated per-group `when_to_use`. The op rows come from
   **runtime registration, not an alembic migration** — there is no schema change.
3. **Dispatch** — `POST /api/v1/operations/call` resolves
   `connector_id="wsfc-ssh-2022.x"` + the target and invokes the bound handler
   through the policy/audit path. `dangerous` + `requires_approval` ops park at
   `needs-approval` first. The CLI reaches the same path via `meho operation call
   wsfc-ssh-2022.x <op_id> --params-json '{...}'` (no dedicated `meho wsfc` verb
   package — the winsrv / rke2 precedent for SSH-typed connectors).

### `cluster.test` duration semantics

`wsfc.cluster.test` RUNS `Test-Cluster`, which executes the full validation suite
and can take **minutes** on a real cluster. The handler forwards a raised
15-minute `timeout` to `pwsh_run` — far above the transport's 30 s default — so
the JSON ack is not truncated into a false failure on a validation that in fact
completed. On a running cluster the disruptive storage tests (which need the
disks offline) are skipped automatically for online disks; scope the run with the
`include` param (a list of `Test-Cluster -Include` categories, e.g. `Inventory` /
`Network` / `System Configuration`) to keep it short. The op is `caution`, not
`safe`, precisely because it is a real, resource-touching validation RUN. It
writes reports under `%SystemRoot%\Cluster\Reports`; enumerate them with the
safe `wsfc.cluster.validation-report`.

## Sensor pin recipes (checks plane)

Cluster state is a first-class **checks/sensors** feed (`docs/codebase/sensor.md`):
node up, role online, witness reachable — deterministic assertions the monitoring
plane can pin. A Sensor stores an `(op + params + assertion + cadence + severity)`
tuple; the runner dispatches the op on a schedule and feeds the reduced op result
into #2504's bounded assertion evaluator (`AssertionSpec` — one dotted-path
select feeding one typed comparator).

Two constraints shape these recipes:

- **The safe-only guard** (`SensorAdminService.create`): a Sensor may pin only a
  `safety_level="safe"` op. All five recipes below use safe reads.
- **Pin *scalar* reads, not list reads.** A list op (`nodes.list`,
  `groups.list`, ...) returns `{rows, total}` that the JSONFlux reducer spills to
  a handle, so the assertion would see the reduced envelope, not the rows. That
  is exactly why `wsfc.cluster.get` exposes flat scalar counts (`nodes_up`,
  `groups_failed`, ...) and `wsfc.groups.state` / `wsfc.witness.get` return flat
  dicts — those are the sensor-shaped ops.

The five recipes (`c1sql1` is the named 2-node SQL FCI consumer; substitute your
own target name). Each shows the op + params + the `assertion` JSON and a runnable
`meho sensor create` invocation. The `--target` names the cluster-node target the
runner dispatches against.

### 1. Cluster node count (all nodes up)

`wsfc.cluster.get` → `$.nodes_up`. For a 2-node cluster, fewer than 2 up is
`degraded`, fewer than 1 (i.e. the last node down / cluster service unreachable)
is `critical`.

```json
{"select": {"path": "$.nodes_up"},
 "compare": {"type": "threshold", "op": "lt", "degraded": 2, "critical": 1}}
```

```bash
meho sensor create --name c1sql1-nodes-up \
  --connector-id wsfc-ssh-2022.x --op-id wsfc.cluster.get \
  --target '{"name": "c1sql1"}' \
  --assertion '{"select":{"path":"$.nodes_up"},"compare":{"type":"threshold","op":"lt","degraded":2,"critical":1}}' \
  --cadence-kind interval --interval-seconds 60 --severity critical
```

### 2. SQL Server FCI role online

`wsfc.groups.state` (params name the clustered role) → `$.state`. Anything other
than `Online` is `critical` (the equals comparator emits `ok` on match,
`critical` otherwise).

```json
{"select": {"path": "$.state"},
 "compare": {"type": "equals", "value": "Online"}}
```

```bash
meho sensor create --name c1sql1-fci-online \
  --connector-id wsfc-ssh-2022.x --op-id wsfc.groups.state \
  --target '{"name": "c1sql1"}' \
  --params '{"name": "SQL Server (MSSQLSERVER)"}' \
  --assertion '{"select":{"path":"$.state"},"compare":{"type":"equals","value":"Online"}}' \
  --cadence-kind interval --interval-seconds 30 --severity critical
```

### 3. Quorum witness online

`wsfc.witness.get` → `$.online`. On a 2-node cluster a downed witness is one node
failure from quorum loss, so a witness that is not `Online` is `critical`. (Pin
this only where a witness is expected — a node-majority cluster reads
`online=false`.)

```json
{"select": {"path": "$.online"},
 "compare": {"type": "bool", "expect": true}}
```

```bash
meho sensor create --name c1sql1-witness-online \
  --connector-id wsfc-ssh-2022.x --op-id wsfc.witness.get \
  --target '{"name": "c1sql1"}' \
  --assertion '{"select":{"path":"$.online"},"compare":{"type":"bool","expect":true}}' \
  --cadence-kind interval --interval-seconds 60 --severity critical
```

### 4. No failed cluster roles

`wsfc.cluster.get` → `$.groups_failed`. Any role in the `Failed` state is
`degraded`; more than one is `critical`.

```json
{"select": {"path": "$.groups_failed"},
 "compare": {"type": "threshold", "op": "gt", "degraded": 0, "critical": 1}}
```

```bash
meho sensor create --name c1sql1-groups-failed \
  --connector-id wsfc-ssh-2022.x --op-id wsfc.cluster.get \
  --target '{"name": "c1sql1"}' \
  --assertion '{"select":{"path":"$.groups_failed"},"compare":{"type":"threshold","op":"gt","degraded":0,"critical":1}}' \
  --cadence-kind interval --interval-seconds 60 --severity degraded
```

### 5. No failed cluster resources

`wsfc.cluster.get` → `$.resources_failed`. Same shape as recipe 4 at the resource
grain (an IP / network-name / disk resource in `Failed`).

```json
{"select": {"path": "$.resources_failed"},
 "compare": {"type": "threshold", "op": "gt", "degraded": 0, "critical": 1}}
```

```bash
meho sensor create --name c1sql1-resources-failed \
  --connector-id wsfc-ssh-2022.x --op-id wsfc.cluster.get \
  --target '{"name": "c1sql1"}' \
  --assertion '{"select":{"path":"$.resources_failed"},"compare":{"type":"threshold","op":"gt","degraded":0,"critical":1}}' \
  --cadence-kind interval --interval-seconds 60 --severity degraded
```

Pinning at least one of these on a live c1sql1 read is the consumer proof for the
checks plane (an OPEN acceptance criterion — the cluster does not exist yet; see
*Known issues / scope*).

## Tests

`backend/tests/test_connectors_wsfc.py` — mirrors the winsrv / windows_dns
harness (`_StubTarget`, `_completed_process`, `patch.object(connector,
"_run_command", AsyncMock(...))`); assertions decode the `-EncodedCommand` base64
back to the script text. Covers the transport (`powershell` +
`$ProgressPreference`), the registry-triple round-trip, the 19-op surface +
safety-tier + group-coverage invariants, every op group's script construction,
the injection-safety escape on every parameterized script (node / group name,
target node, disk resource, file-share path, `Test-Cluster` include —
single-quote doubling), the raised `Test-Cluster` timeout, the witness
`witness_type` validation (incl. cloud-witness rejection), the secret-hygiene
invariant (no secret-value parameter on any op), the `about` fingerprint mapping,
and the probe reason matrix (incl. `failover_module_absent` / `not_cluster_member`).
The connector is also covered by the registry-driven flight-recorder conformance
sweep (`test_flight_recorder_typed_spans.py` — `wsfc-ssh` in
`_EXPECTED_TYPED_IMPLS`). **No spec-reconcile lane** — the cmdlet surface ships no
OpenAPI (the confirmed convention for SSH-typed connectors); drift protection is
this ordinary unit suite.

## CLI

No dedicated `meho wsfc` verb package (the winsrv / rke2 precedent for SSH-typed
connectors). CLI parity is the generic dispatch path — `meho operation call
wsfc-ssh-2022.x <op_id> --params-json '{...}'` — plus the `wsfc` product token
added to the OpenAPI `TargetCreate.product` enum (regenerated from the live
registry, so `meho targets create --product wsfc ...` validates and the generated
Go client knows the token). The CLI OpenAPI snapshot (`cli/api/openapi.json` +
the generated `cli/internal/api/client.gen.go`) is regenerated in this PR.

## Known issues / scope

- **Consumer proof against a live c1sql1 cluster is an OPEN acceptance
  criterion** — the c1sql1 Windows Server 2022 SQL FCI does not exist yet (it is
  built by evoila-bosnia/claude-rdc-hetzner-dc#2794 / #2795). The green probe, the
  governed `groups.move` failover, the approval-parked `evict` / `offline` /
  `online`, and the live Sensor pin are all deferred to that lab program; the exact
  `ClusterNodeState` / `ClusterGroupState` / `QuorumType` strings get confirmed
  against the real cluster there.
- **No cloud witness** — `witness.set` supports `disk` / `file_share` /
  `node_majority` only; the cloud witness's `-AccessKey` is a secret that cannot
  ride the pwsh transport (a Vault-brokered flow is a follow-up).
- **`validation-report` lists reports, it does not parse them** — reading a
  specific validation report's per-test pass/fail is a follow-up (the report is an
  `.mht` on disk; a robust machine-readable parse is out of scope for this cut).
- **`cluster.test` is long-running** — a full `Test-Cluster` can take minutes (the
  handler raises the transport timeout to 15 minutes). Scope it with `include` on
  a busy cluster.
- Host-key checking is disabled (`known_hosts=None`) at the adapter level for
  v0.2, shared across the whole SSH family; pinning is deferred repo-wide.

## References

- Task #3263 (connector); Initiative #3259 (Microsoft estate); mold #3261
  (`winsrv`) + prerequisite #3260 (shared `_shared/pwsh` transport hoist);
  evoila/meho#3256 (the vSphere shared-disk layer beneath the guest cluster).
- Molds: `docs/codebase/connectors-winsrv.md` (the estate mold — structure,
  transport, "building the next estate connector"),
  `docs/codebase/connectors-rke2.md` (approval-parked-write mold),
  `docs/codebase/sensor.md` (the checks-plane conventions the recipes follow).
- Consumer: evoila-bosnia/claude-rdc-hetzner-dc#2794 / #2795 (the c1sql1 SQL FCI).
- Cmdlet references (Windows Server 2022 / PowerShell 5.1):
  <https://learn.microsoft.com/en-us/powershell/module/failoverclusters/>.
