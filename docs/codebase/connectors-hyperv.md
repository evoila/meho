# Connector: hyperv (hyperv-2022.x / `hyperv-ssh`)

## Overview

`hyperv` is the **Hyper-V migration-source** connector: the governed,
source-side reach for a Hyper-V→VMware migration. It reads a Hyper-V host's
inventory, VM configuration, and virtual-disk / checkpoint facts — the inputs a
migration plan is built from — plus the guarded `Export-VM` that seeds the
migration, over SSH → PowerShell. The Hyper-V host runs an OpenSSH server whose
shell drives `powershell` (Windows PowerShell 5.1) executing the built-in
`Hyper-V` module cmdlets.

It is a **structured copy of the `winsrv` estate mold** (#3261): the same
`SshConnector` base and the same shared PowerShell-over-SSH transport
(`meho_backplane.connectors._shared.pwsh`), an identity canary + per-domain op
groups, the `{rows, total}` JSONFlux envelope for list reads, and the `rke2`
approval-parked-write mold for its destructive ops. The agent never sees the
difference between this typed connector and a generic one — it goes through the
same `call_operation(connector_id, op_id, target?, params)` meta-tool.

The **target is a single Hyper-V host** (standalone or a cluster node). The
`Hyper-V` module cmdlets act on the local host, so one host is the control point
for its own VMs. **Cluster-wide** views of a Hyper-V *cluster*'s nodes belong to
the [`wsfc`](connectors-wsfc.md) connector (#3263) on the same nodes — this
connector does not duplicate them.

**Migration-source framing — what feeds a VHDX→VMDK plan.** The safe reads
answer the questions a migration assessment asks:

- *Which VMs, and how big?* — `hyperv.vms.list` (inventory: state, generation,
  integration-services version), `hyperv.host.info` (host capacity to size
  against).
- *What firmware does the target need?* — `hyperv.vms.config` surfaces
  `Generation` (1 → BIOS, 2 → EFI) and, for Gen 2, the secure-boot state (→ EFI
  Secure Boot). Wrong firmware is the most common failed-boot after a migration.
- *What do the disks convert to?* — `hyperv.disks.vm.list` maps a VM to its
  virtual-disk files; `hyperv.disks.vhd.get` reports each VHD/VHDX's format,
  virtual size, on-disk file size, and fragmentation; `hyperv.disks.vhd.chain`
  walks a differencing (`.avhdx`) disk's parent chain to the base — a
  differencing chain must be merged before a clean single-file VMDK conversion.
- *Is the source safe to cut over?* — `hyperv.checkpoints.list` flags a live
  checkpoint tree (differencing disks in play); `hyperv.vms.state` confirms the
  guest is quiesced / heartbeating.

The write surface is the seed and the cutover: `hyperv.export.vm` (the migration
seed) and `hyperv.power.start` / `hyperv.power.stop` (source-side cutover
verbs), plus checkpoint create / revert / delete.

## Op surface (18 ops across six groups)

Safety tiers follow the Initiative #3259 satellite table: reads are `safe` (ride
satellite runners into un-dialable customer networks); recoverable writes are
`caution` (satellite-executable only through the Stage-3 composed write gate,
#2901); destructive ops are `dangerous` + `requires_approval` (satellite-EXCLUDED
by the tier ladder — central-dial / on-site only).

| op_id | group | safety | cmdlet(s) |
|---|---|---|---|
| `hyperv.about` | host | safe | `Get-Module Hyper-V` + `Win32_ComputerSystem` (identity canary) |
| `hyperv.host.info` | host | safe | `Get-VMHost` |
| `hyperv.host.numa` | host | safe | `Get-VMHostNumaNode` |
| `hyperv.host.vswitch.list` | host | safe | `Get-VMSwitch` |
| `hyperv.vms.list` | vms | safe | `Get-VM` (the assessment surface) |
| `hyperv.vms.get` | vms | safe | `Get-VM -Name` |
| `hyperv.vms.config` | vms | safe | `Get-VM` (+ `Get-VMFirmware` on Gen 2) |
| `hyperv.vms.state` | vms | safe | `Get-VM -Name` (runtime state) |
| `hyperv.disks.vm.list` | disks | safe | `Get-VMHardDiskDrive -VMName` |
| `hyperv.disks.vhd.get` | disks | safe | `Get-VHD -Path` |
| `hyperv.disks.vhd.chain` | disks | safe | `Get-VHD` (follows `ParentPath` leaf→base) |
| `hyperv.checkpoints.list` | checkpoints | safe | `Get-VMSnapshot -VMName` |
| `hyperv.checkpoints.create` | checkpoints | caution | `Checkpoint-VM` |
| `hyperv.checkpoints.revert` | checkpoints | dangerous + approval | `Restore-VMSnapshot` |
| `hyperv.checkpoints.delete` | checkpoints | dangerous + approval | `Remove-VMSnapshot` |
| `hyperv.export.vm` | export | caution | `Export-VM -Name -Path` (long-running) |
| `hyperv.power.start` | power | caution | `Start-VM` |
| `hyperv.power.stop` | power | caution | `Stop-VM` (`shutdown` / `turnoff` / `save`) |

12 `safe`, 4 `caution`, 2 `dangerous` + `requires_approval`. Every op carries an
`llm_instructions` block; every group carries a `_WHEN_TO_USE_BY_GROUP` entry
(registration fails closed without one).

### Explicit non-goals (this increment)

- **VM create / delete on Hyper-V** — we manage the source, we do not build on
  it.
- **SCVMM** — no consumer.
- **Hyper-V Replica** — out of scope.
- **Cluster-wide views** — the `wsfc` connector owns them on the same nodes.

## Key types

- `HypervConnector` (`connector.py`) — the `SshConnector` subclass. Sets
  `POWERSHELL_EXECUTABLE = "powershell"`, `POWERSHELL_SCRIPT_PREFIX`
  (`$ProgressPreference = 'SilentlyContinue'; `), and `POWERSHELL_LOG_EVENT =
  "hyperv_pwsh_executed"`. Carries `fingerprint`, `probe`, `about`, the 17
  bound-method op shims, `register_operations`, and the operator-less `execute`
  dispatcher shim.
- `HypervOp` (`ops.py`) — a frozen dataclass mirroring the
  `register_typed_operation` kwargs; each op declares its `handler_attr`,
  schemas, `group_key`, `safety_level`, `requires_approval`, and
  `llm_instructions`.
- `HYPERV_OPS` (`ops.py`) — the registration tuple, composed by `_hyperv_ops()`
  from the identity canary + the per-group tuples (`HOST_OPS`, `VMS_OPS`,
  `DISK_OPS`, `CHECKPOINT_OPS`, `EXPORT_OPS`, `POWER_OPS`).
- `hyperv_list_read()` (`ops.py`) — the shared `{rows, total}` JSONFlux
  envelope builder: wraps a `Get-… | Select-Object …` pipeline in `@(…)` under
  `$ErrorActionPreference = 'Stop'` so a single object still counts as one row
  and a cmdlet error is a real failure (not a false-empty read).

## Transport: PowerShell-over-SSH

Cmdlets reach the host as `powershell -NoProfile -NonInteractive -EncodedCommand
<base64-utf16le>` (Windows PowerShell 5.1) via the shared transport
`meho_backplane.connectors._shared.pwsh.pwsh_run`. Output is the cmdlet's
`ConvertTo-Json` piped to stdout, parsed with the stdlib `json`. The transport
strips any CLIXML warning/error block, logs only sizes + exit code (never the
script body or the encoded payload), and raises a single structured
`PwshRunError` on failure.

**Enum / compound stringification.** Windows PowerShell 5.1 `ConvertTo-Json`
renders an enum as its **integer** value and a compound object (`TimeSpan`,
`Version`, `Guid`) as a nested map. The list / config projections therefore
stringify those fields with `Select-Object` calculated properties / `"$(...)"`
(e.g. `@{N='State';E={"$($_.State)"}}`) so the assessment surface is readable and
stable.

### Injection safety

Operator-supplied strings — `vm_name`, `checkpoint_name`, VHD `path`, export
`path` — are interpolated only inside single-quoted PowerShell literals via
`ps_single_quote` (a single quote is doubled; a single-quoted literal does no
`$` expansion). `mode` (power.stop) is validated against a fixed enum in Python
and maps to a constant switch string; `timeout_seconds` (export) is a
Python-validated bounded `int` and never reaches the script text. The identity /
host-read scripts take no operator input (constant scripts). The unit suite
decodes the `-EncodedCommand` base64 back to the script and asserts the escaped
form (e.g. `Get-VHD -Path 'C:\vm\o''db.vhdx'`).

### No secret on the wire (deliberate)

No op accepts a secret-value parameter — the SSH credential (resolved from the
target's Vault secret by the base `SshConnector`) is the only secret and never
enters a script body. A schema-level test asserts no op exposes a
`password` / `secret` / `credential`-shaped field.

## `fingerprint(target)` / `probe(target)`

`fingerprint` runs one round-trip reading hostname + `Win32_OperatingSystem`
(caption / version / build) + PowerShell version + `Hyper-V` module
presence/version + `Win32_ComputerSystem.HypervisorPresent`. It returns
`vendor="microsoft"`, `product="hyper-v"`, and the host / module / hypervisor
facts in `extras`. An unreachable / SSH-failed / cmdlet-failed target returns
`reachable=False` with `extras["error"]` (the #986 discipline — never an
unhandled exception).

`probe` surfaces **six distinct reasons**, two of them Hyper-V specific:

| reason | meaning |
|---|---|
| `tcp_unreachable` | the SSH TCP socket cannot connect |
| `ssh_auth_failed` | credentials rejected / handshake failed / Vault read failed |
| `powershell_unavailable` | SSH ok but the reachability script failed |
| `command_failed` | a post-connect transport drop / timeout |
| `hyperv_module_absent` | PowerShell runs but the `Hyper-V` module is not installed |
| `hypervisor_role_absent` | the module is present but the hypervisor is not running (`HypervisorPresent` false) |

## Auth

Inherits the base `SshConnector._auth_config` unchanged (password-default +
key-fallback; the credential resolves from the target's Vault secret in operator
context). Windows Server / Hyper-V hosts ship an OpenSSH server that must be
enabled and pointed at Windows PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
```

## Control flow

1. `call_operation` resolves `HypervConnector` for the target (versioned
   `hyperv-2022.x` wins; the wildcard row catches an unfingerprinted target).
2. The dispatcher applies auth + policy (a `dangerous` + `requires_approval` op
   parks at `needs-approval`) + JSONFlux + synchronous audit + broadcast.
3. The bound-method shim imports and calls the per-group handler, which composes
   a `-EncodedCommand` script and runs it through `pwsh_run`.
4. List ops return `{rows, total}`; the JSONFlux reducer wraps large sets in a
   result handle the agent drills into via `result_query`.

## Long-running `export.vm` semantics

`Export-VM` is **synchronous** — it blocks the `powershell` process (and this SSH
round-trip) until the copy finishes, which can be many minutes for a
multi-hundred-GB VM. Unlike a reboot it does **not** tear the SSH channel down,
so a blocking read is safe; the only risk is the wall-clock budget. The handler
exposes `timeout_seconds` (default 3600, max 86400) and forwards it to the
transport's per-call timeout, so the caller sizes the budget to the VM's disk
size. A truly asynchronous `-AsJob` submission + out-of-band job polling is a
documented follow-up (it needs a session-surviving job store the stateless
per-call transport does not have); this cut ships the straightforward blocking
export, correct for the lab-scale VMs the consumer proof exercises.

`Stop-VM` in the default `shutdown` mode likewise blocks (the guest gets up to
five minutes), so `power.stop` uses a wider 360s transport timeout.

## Building on the estate mold

This connector sets `POWERSHELL_EXECUTABLE = "powershell"` explicitly — the
shared transport's fallback is `pwsh` (PS7), which a Windows Server / Hyper-V
host does **not** ship by default. See
[`connectors-winsrv.md`](connectors-winsrv.md) — "building the next estate
connector" — for the full checklist the estate connectors share.

## Tests

`backend/tests/test_connectors_hyperv.py` — the ordinary unit suite (there is
**no** spec-reconcile lane: the cmdlet surface ships no OpenAPI, the confirmed
convention for SSH-typed connectors). It uses the winsrv harness (a `_StubTarget`
+ a mocked `_run_command`) and decodes the `-EncodedCommand` base64 back to the
script to assert the cmdlet + escaped arguments the handler built. Coverage:
every op group, the injection-safety escape on every parameterized script, the
`{rows, total}` envelope + single-row collapse, the export timeout validation +
forwarding, the power `mode` switch mapping, the secret-hygiene schema invariant,
`about` fingerprint mapping, and all six probe reasons.

The registry-driven flight-recorder conformance sweep
(`test_flight_recorder_typed_spans.py`) includes `hyperv-ssh` in
`_EXPECTED_TYPED_IMPLS`, so every hyperv op emits a `typed` span through the
shared seam.

## CLI

No dedicated `meho hyperv` verb package (the windows_dns / winsrv / wsfc / msad /
rke2 precedent for SSH-typed connectors). CLI parity is the generic dispatch path
— `meho operation call hyperv-ssh-2022.x <op_id> --params-json '{...}'` — plus the
`hyperv` product token added to the OpenAPI `TargetCreate.product` enum
(regenerated from the live registry, so `meho targets create --product hyperv ...`
validates and the generated Go client knows the token). The CLI OpenAPI snapshot
(`cli/api/openapi.json` + the generated `cli/internal/api/client.gen.go`) is
regenerated in this PR via `cd cli && make snapshot-openapi && make generate`.

## Known issues / scope

- **Live-consumer proof is open.** The dev/test target is the lab's nested
  Hyper-V cluster **c1hv1**
  (`evoila-bosnia/claude-rdc-hetzner-dc#2802`), which is in this lane's own build
  queue. Per the estate precedent (winsrv / msad / mssql all shipped with the
  same honest open AC), the connector ships now with the live-probe / inventory /
  export proof recorded on the issue once c1hv1 is reachable.
- **Blocking export.** `export.vm` blocks for the export duration (see above); a
  background-job path is a documented follow-up.
- **`Get-VHD` on shared storage.** When a VHD is attached to a running VM on
  shared storage it can only be read from the host currently using it; the
  handler runs under `-ErrorAction Stop`, so that surfaces as a real
  `PwshRunError`.

## References

- Initiative #3259 (Microsoft estate connectors) — design rules.
- Task #3265; depends-on #3261 (estate mold); #3263 (cluster views stay in wsfc).
- `evoila-bosnia/claude-rdc-hetzner-dc#2802` — the c1hv1 consumer target.
- Hyper-V PowerShell module reference:
  <https://learn.microsoft.com/en-us/powershell/module/hyper-v/>
  (`Get-VMHost`, `Get-VM`, `Get-VHD`, `Get-VMSnapshot`, `Checkpoint-VM`,
  `Export-VM`, `Stop-VM`).
- [`connectors-winsrv.md`](connectors-winsrv.md) — the estate mold.
- [`connectors-shared-vault-creds.md`](connectors-shared-vault-creds.md) — the
  SSH credential resolution.
