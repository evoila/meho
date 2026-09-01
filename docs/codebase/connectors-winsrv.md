# Connector: winsrv (winsrv-2022.x / `winsrv-ssh`)

## Overview

The `winsrv` connector is the typed `Connector` subclass that manages **Windows
Server core** — system facts, service and feature management, reboot/shutdown,
local users, and disk / iSCSI-initiator storage — over SSH → PowerShell (the
built-in Windows PowerShell 5.1 cmdlets). It is registered under the
`(product="winsrv", version="2022.x", impl_id="winsrv-ssh")` registry triple
plus the `("winsrv", "", "")` wildcard fallback row, and is a child of the
shared `SshConnector` adapter (`connectors/adapters/ssh.py`) — the same
transport base as `WindowsDnsConnector`, `HolodeckConnector`, and
`Rke2SshConnector`.

Windows Server core management exposes **no unified REST API**; before this
connector the c1sql1 program (evoila-bosnia/claude-rdc-hetzner-dc#2789) drove
feature installs and reboots through raw `govc guest.run` — ungoverned. The
connector is a structural sibling of `windows_dns` (identity canary + per-domain
op groups on the `SshConnector` base) built on the PowerShell-over-SSH transport
`_shared/pwsh` (base64 UTF-16LE `-EncodedCommand` + stdlib `json` parse). It
adopts the `rke2` approval-parked-write mold for its destructive ops.

`winsrv` is also the **estate mold**: `msad` (#3262), `wsfc` (#3263), and
`hyperv` (#3265) are structured copies of this package with different cmdlet
modules — see "Building the next estate connector" below.

Registry-triple rationale: `product="winsrv"` is a short, separator-free token
because `parse_connector_id` derives the product from the first hyphen-segment
of `impl_id`, so `connector_id="winsrv-ssh-2022.x"` round-trips (a
hyphen/underscore in the product token breaks the registry's round-trip guard at
boot — the reason `vcf_logs` retired its `vcf-logs` token). `version="2022.x"`
marks the cmdlet surface (stable across Windows Server 2019 → 2025).
`impl_id="winsrv-ssh"` leaves room for a future `winsrv-winrm` sibling.

Source: `backend/src/meho_backplane/connectors/winsrv/`.

## Op surface (23 ops across six groups)

Safety tier is a reach decision per the Initiative #3259 satellite table: reads
= `safe` (ride satellite runners); recoverable writes = `caution` (satellite-
executable only through the Stage-3 composed write gate, #2901); destructive =
`dangerous` + `requires_approval` (satellite-EXCLUDED by the tier ladder,
central-dial / on-site only).

| op | safety | approval | cmdlet(s) |
| --- | --- | --- | --- |
| `winsrv.about` | safe | no | `Get-CimInstance Win32_OperatingSystem` + `$PSVersionTable` |
| `winsrv.system.os-info` | safe | no | `Get-CimInstance Win32_OperatingSystem` |
| `winsrv.system.uptime` | safe | no | `Win32_OperatingSystem.LastBootUpTime` |
| `winsrv.system.pending-reboot` | safe | no | CBS / WU / Session-Manager marker registry keys |
| `winsrv.service.list` | safe | no | `Get-Service` |
| `winsrv.service.get` | safe | no | `Get-Service -Name` |
| `winsrv.service.start` | caution | no | `Start-Service` |
| `winsrv.service.stop` | caution | no | `Stop-Service` |
| `winsrv.service.restart` | caution | no | `Restart-Service` |
| `winsrv.feature.list` | safe | no | `Get-WindowsFeature` |
| `winsrv.feature.install` | caution | no | `Install-WindowsFeature` |
| `winsrv.feature.remove` | caution | no | `Uninstall-WindowsFeature` |
| `winsrv.power.reboot` | **dangerous** | **yes** | `shutdown.exe /r /t <delay>` |
| `winsrv.power.shutdown` | **dangerous** | **yes** | `shutdown.exe /s /t <delay>` |
| `winsrv.localuser.list` | safe | no | `Get-LocalUser` |
| `winsrv.localuser.create` | caution | no | `New-LocalUser -NoPassword` |
| `winsrv.localuser.set` | caution | no | `Set-LocalUser` + `Enable-`/`Disable-LocalUser` |
| `winsrv.localuser.delete` | **dangerous** | **yes** | `Remove-LocalUser` |
| `winsrv.storage.disk.list` | safe | no | `Get-Disk` |
| `winsrv.storage.volume.list` | safe | no | `Get-Volume` |
| `winsrv.storage.iscsi.list` | safe | no | `Get-IscsiTarget` |
| `winsrv.storage.iscsi.connect` | caution | no | `Connect-IscsiTarget` |
| `winsrv.storage.disk.format` | caution | no | `Initialize-Disk` + `New-Partition` + `Format-Volume` |

The three `dangerous` ops (`power.reboot`, `power.shutdown`, `localuser.delete`)
are `requires_approval=True` — a dispatch parks at `needs-approval` for a human
decision (the rke2 write mold; no bespoke `proposed_effect` builder is
registered — the generic params-echo default (#1856) surfaces the target /
params redaction-safely to the reviewer). The **agent-principal ceiling**
therefore applies: an agent session cannot self-approve these — approval is a
human decision (v0.1-spec §7), so a `winsrv.power.*` / `localuser.delete`
dispatch from an agent floors at the approval queue until an operator approves in
the console / CLI. Reads ride satellite runners; the `dangerous` ops are excluded
from satellites by the tier ladder.

## Key types

- **`WinsrvConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="winsrv"`, `version="2022.x"`, `impl_id="winsrv-ssh"`,
  `POWERSHELL_EXECUTABLE="powershell"`. Ships `fingerprint`, `probe`, `about`,
  the 22 bound-method op shims, `register_operations`, and the operator-less
  `execute` dispatcher shim. Inherits the per-target asyncssh connection pool,
  `_auth_config`, `_run_command`, `_assert_reachable`, and `aclose()` from the
  adapter.
- **Shared pwsh transport** (`_shared/pwsh.py`, #3260) — `pwsh_run` (the single
  seam every handler routes its script through), `ps_single_quote` (the
  `shlex.quote` analogue), `encode_pwsh_command`, `strip_clixml`, `PwshRunError`.
  Per-connector wire variation rides three class-level seams:
  `POWERSHELL_EXECUTABLE` (`powershell` here), `POWERSHELL_SCRIPT_PREFIX` (the
  `$ProgressPreference` guard), `POWERSHELL_LOG_EVENT` (`winsrv_pwsh_executed`).
- **Op metadata** (`ops.py`) — `WinsrvOp` frozen dataclass (mirrors `WindowsDnsOp`
  / `Rke2Op`), `WINSRV_OPS` (`about` + the six per-domain tuples),
  `WINSRV_WHEN_TO_USE_BY_GROUP` (curated per-group blurbs, imported into the
  connector — the rke2 `RKE2_WHEN_TO_USE_WRITE_BY_GROUP` precedent), and
  `normalise_json_rows` (collapses `ConvertTo-Json`'s dict / list / null shapes
  to `list[dict]`).
- **Per-domain op modules** — `ops_system.py`, `ops_services.py`,
  `ops_features.py`, `ops_power.py`, `ops_localusers.py`, `ops_storage.py`. Each
  owns its handlers + op tuple, imports `WinsrvOp` from `ops.py`, and (for
  parameterized scripts) `ps_single_quote` from `_shared/pwsh`.
- **Registration** (`__init__.py`) — two-phase, mirroring windows_dns / rke2:
  synchronous `register_connector_v2` at import time (versioned triple + wildcard
  row); async `register_winsrv_typed_operations` queued onto the lifespan
  registrar list. No v1 `register_connector` — no chassis history.

## Transport: PowerShell-over-SSH

Every op runs `powershell -NoProfile -NonInteractive -EncodedCommand <base64>`
over the pooled SSH connection:

- **`powershell` (Windows PowerShell 5.1), not `pwsh` (PS7).** A Windows Server
  host ships 5.1; PS7 is a separate optional install absent by default. The
  connector sets `POWERSHELL_EXECUTABLE="powershell"` explicitly.
- **`-EncodedCommand` wire shape** — the script is UTF-16LE-encoded and base64'd
  (no BOM). The shell never parses the script text, so there is no `sh -c`
  quoting layer; only the PowerShell parser sees it.
- **`$ProgressPreference = 'SilentlyContinue'` guard** — prepended to every
  script so the ServerManager / CIM modules' first-use progress stream never
  serialises CLIXML noise onto the streams.
- **JSON out** — handlers pipe through `ConvertTo-Json` (explicit `-Depth`) and
  `pwsh_run` parses stdout with stdlib `json`.
- **Hashtable envelope for list reads** — `ConvertTo-Json` emits *nothing* on a
  zero-object pipeline (which trips `pwsh_run`'s empty-stdout guard), so every
  list read wraps its result in `@{ rows = $x; total = $x.Count }` and runs under
  `$ErrorActionPreference = 'Stop'` (a cmdlet error must surface as a real
  `PwshRunError`, not read as a false-empty inventory). The `{rows, total}`
  envelope is what the dispatcher's JSONFlux reducer keys on to spill a
  set-shaped result to a `result_query` handle.
- **`shutdown.exe` for power ops** — a bare `Restart-Computer -Force` tears down
  the SSH channel before the JSON ack flushes (`pwsh_run` would raise on the
  truncated read, reporting failure on a reboot that succeeded). The power ops
  instead *schedule* the reboot/shutdown with `shutdown.exe /r|/s /t <delay>`
  (default 15s), so the ack flushes cleanly and the host goes down after the
  delay. `$LASTEXITCODE` is checked so a rejected schedule surfaces as a real
  `PwshRunError`.
- **Secret hygiene** — logging emits `script_len` / `encoded_len` /
  `exit_status` only; `PwshRunError` never retains the script or the encoded
  payload. The SSH credential is the only secret and it never reaches the script
  text.

### Injection safety

Operator-supplied strings (service / feature / user names, iSCSI IQN, volume
label, reboot message) are interpolated only inside **single-quoted** PowerShell
literals via `ps_single_quote` (embedded `'` doubled — complete escaping inside a
single-quoted PowerShell string). Integers (delay, disk number, iSCSI port) are
validated to `int` before interpolation; `filesystem` and `drive_letter` are
validated against a bounded enum / `^[A-Za-z]$`; booleans render as fixed
`$true` / `$false` literals, never as interpolated operator text.

### No plaintext secret on the wire (deliberate)

The `-EncodedCommand` payload lands on the remote process argv and the script
body is visible to a privileged remote observer, so the shared transport's safety
contract **forbids credential material in the script**. Two consequences:

- **`localuser.create` is passwordless** (`New-LocalUser -NoPassword`) and
  `localuser.set` never touches the password. Provisioning a password is a
  deferred follow-up (the rke2 `token.rotate` mint-and-stash-in-Vault mold, so no
  secret enters the script) — until then set it out of band or via the domain
  (msad, #3262).
- **`iscsi.connect` has no CHAP** — `Connect-IscsiTarget` supports
  `-ChapUsername` / `-ChapSecret`, but the secret would land in the script. CHAP
  is out of scope for this cut (connect against a trusted fabric); a
  Vault-brokered CHAP flow is a follow-up.

A unit test (`test_no_write_op_exposes_a_secret_value_field`) pins this at the
schema level: no op declares a `password` / `secret` / CHAP parameter.

## `fingerprint(target)` / `probe(target)`

`fingerprint` runs one `-EncodedCommand` round-trip reading the hostname +
`Win32_OperatingSystem` (caption / version / build) + PowerShell version; returns
`vendor="microsoft"`, `product="windows-server"`, `version=<OS version>`,
`build=<build number>`, with `extras={hostname, os_caption, powershell_version}`.
Any transport / credential failure (`OSError`, `asyncssh.Error`, `ValueError`,
`VaultClientError`, `CredentialsReadError`, `PwshRunError`) maps to
`reachable=False` + `extras["error"]` — never an unhandled exception (#986).
`about` (`winsrv.about`) wraps `fingerprint` + `_assert_reachable`.

`probe` surfaces five distinct `ProbeResult.reason` values:

| reason | meaning |
| --- | --- |
| `tcp_unreachable` | the SSH TCP socket cannot connect |
| `ssh_auth_failed` | credentials rejected / handshake failed / Vault credential unresolvable |
| `powershell_unavailable` | SSH succeeded but the PowerShell reachability script failed |
| `command_failed` | post-connect transport failure (connection drop / timeout / socket error) after a successful handshake (the #986 post-connect guard) |
| `os_query_failed` | PowerShell runs but `Win32_OperatingSystem` is not readable (not a healthy Windows host, or WMI/CIM broken) |

## Auth

Uses the base `SshConnector._auth_config` **unchanged** — a Vault secret with
`ssh_private_key` prefers key auth, otherwise password auth runs. Credentials
resolve via `_shared/vault_creds` from the Vault KV-v2 path string in
`target.secret_ref`. The transport prerequisite is an **OpenSSH server on the
Windows target** (same as windows_dns), with `powershell` reachable as the login
shell or via the default shell. Server-side setup:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
# Optionally make PowerShell the default SSH shell:
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
  -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force
```

`probe()` carries no operator, so its Vault read runs under the synthesised
system operator and fails closed to `ssh_auth_failed`.

## Control flow

1. **Boot** — `_eager_import_connectors()` imports the `winsrv` subpackage;
   `__init__.py` registers the v2 triple + wildcard synchronously and queues the
   typed-op registrar.
2. **Lifespan startup** — `run_typed_op_registrars()` →
   `register_winsrv_typed_operations` → `WinsrvConnector.register_operations`,
   which upserts each `WINSRV_OPS` entry into `endpoint_descriptor` (idempotent
   across restarts) with the curated per-group `when_to_use`. The op rows come
   from **runtime registration, not an alembic migration** — there is no schema
   change.
3. **Dispatch** — `POST /api/v1/operations/call` resolves
   `connector_id="winsrv-ssh-2022.x"` + the target and invokes the bound handler
   through the policy/audit path. `dangerous` + `requires_approval` ops park at
   `needs-approval` first. The CLI reaches the same path via `meho operation call
   winsrv-ssh-2022.x <op_id> --params-json '{...}'` (no dedicated `meho winsrv`
   verb package — the windows_dns / rke2 precedent for SSH-typed connectors).

## Building the next estate connector

`winsrv` is the mold for `msad` / `wsfc` / `hyperv`. When copying this package,
**the single most important footgun**:

> The shared transport's `POWERSHELL_EXECUTABLE` fallback is **`pwsh` (PS7)**,
> which a Windows Server host does NOT ship. A connector that forgets to set the
> class attribute gets a silent `pwsh -EncodedCommand`, which fails confusingly
> ("`pwsh` is not recognized") on every 5.1-only Windows host. **Every
> Windows-estate connector MUST set `POWERSHELL_EXECUTABLE = "powershell"`
> explicitly** (as `winsrv` and `windows_dns` do). The transport keeps the `pwsh`
> default deliberately, so a bare mock connector resolves to the documented
> default in the shared-transport unit suite (`test_connectors_holodeck_pwsh.py`);
> the estate connectors override it, so the default is never hit in production.

Other copy points: set `POWERSHELL_SCRIPT_PREFIX` to the `$ProgressPreference`
guard if the connector's cmdlet module emits first-use progress; give
`POWERSHELL_LOG_EVENT` a connector-scoped name; keep the `{rows, total}` envelope
under `$ErrorActionPreference = 'Stop'` for list reads; interpolate every
operator string through `ps_single_quote`; never embed credential material in a
script; and put destructive ops on the `dangerous` + `requires_approval` tier
(the rke2 mold).

## Tests

`backend/tests/test_connectors_winsrv.py` — mirrors the windows_dns harness
(`_StubTarget`, `_completed_process`, `patch.object(connector, "_run_command",
AsyncMock(...))`); assertions decode the `-EncodedCommand` base64 back to the
script text. Covers the transport (`powershell` + `$ProgressPreference`), the
registry-triple round-trip, the 23-op surface + safety-tier + group-coverage
invariants, every op group's script construction, the injection-safety escape on
every parameterized script (service / feature / user name, iSCSI IQN, volume
label, reboot message — single-quote doubling), the secret-hygiene invariant (no
secret-value parameter on any op; passwordless create/set), the `disk.format`
RAW-disk guard + `force` override + input validation, the `about` fingerprint
mapping, and the probe reason matrix. **No spec-reconcile lane** — the cmdlet
surface ships no OpenAPI (the confirmed convention for SSH-typed connectors);
drift protection is this ordinary unit suite.

## CLI

No dedicated `meho winsrv` verb package (the windows_dns / rke2 precedent for
SSH-typed connectors). CLI parity is the generic dispatch path — `meho operation
call winsrv-ssh-2022.x <op_id> --params-json '{...}'` — plus the `winsrv` product
token added to the OpenAPI `TargetCreate.product` enum (regenerated from the live
registry, so `meho targets create --product winsrv ...` validates and the
generated Go client knows the token).

## Known issues / scope

- **Local-user passwords** are not provisioned (create is `-NoPassword`); a
  Vault-minted-password flow (rke2 `token.rotate` mold) is a follow-up.
- **iSCSI CHAP** is not supported (`iscsi.connect` connects against a trusted
  fabric); a Vault-brokered CHAP flow is a follow-up.
- **`disk.format`** is non-destructive by construction: it `Initialize-Disk`s
  only a `RAW` disk and `New-Partition -UseMaximumSize` only allocates
  **unallocated** space, so it never removes an existing partition or its data.
  It refuses a non-`RAW` disk unless `force=true`; `force=true` then only
  provisions the disk's free space (a full disk fails at `New-Partition`) — it
  does **not** wipe. A true data-destroying wipe (`Clear-Disk -RemoveData`) is
  deliberately not offered on this `caution` op: per the #3259 tier doctrine a
  wipe belongs to a separate `dangerous` op with its own ticket.
- **Consumer proof against a live c1sql1 lab node is an OPEN acceptance
  criterion** — the c1sql1 Windows Server 2022 VMs do not exist yet; the probe +
  feature-install + service-ops proof is deferred to the lab program
  (evoila-bosnia/claude-rdc-hetzner-dc#2789).
- Host-key checking is disabled (`known_hosts=None`) at the adapter level for
  v0.2, shared across the whole SSH family; pinning is deferred repo-wide.

## References

- Task #3261 (connector); Initiative #3259 (Microsoft estate); prerequisite
  #3260 / PR #3268 (shared `_shared/pwsh` transport hoist).
- Molds: `docs/codebase/connectors-windows-dns.md` (structure + probe #986
  discipline + PowerShell-over-SSH transport), `docs/codebase/connectors-rke2.md`
  (approval-parked-write mold).
- Cmdlet references (Windows Server 2022 / PowerShell 5.1):
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/>,
  <https://learn.microsoft.com/en-us/powershell/module/servermanager/>,
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.localaccounts/>,
  <https://learn.microsoft.com/en-us/powershell/module/storage/>,
  <https://learn.microsoft.com/en-us/powershell/module/iscsi/>.
