# Connector: windows_dns (windns-2016.x / `windns-ssh`)

## Overview

The `windows_dns` connector is the typed `Connector` subclass that manages
**Windows AD-DNS records** (the Windows `DnsServer` PowerShell module) over
SSH → PowerShell. It is registered under the
`(product="windns", version="2016.x", impl_id="windns-ssh")` registry triple
plus the `("windns", "", "")` wildcard fallback row, and is a child of the
shared `SshConnector` adapter (`connectors/adapters/ssh.py`) — the same
transport base as `Bind9Connector`, `HolodeckConnector`, `PfSenseConnector`,
and `Rke2SshConnector`.

Windows AD-DNS exposes **no record-management REST API**; before this
connector the only path was manual `ssh <dc> powershell
Add-DnsServerResourceRecord…`, which bypasses MEHO's audit/approval plane
entirely. The connector is a structural sibling of bind9 (it mirrors bind9's
identity + zone + record op surface and safety levels) built on the
PowerShell-over-SSH transport the Holodeck connector established (base64
UTF-16LE `-EncodedCommand` + stdlib `json` parse). Zone creation/deletion is
out of scope, matching bind9 — the write surface is record-scoped.

Registry-triple rationale: `product="windns"` is a short, separator-free
token because `parse_connector_id` derives the product from the first
hyphen-segment of `impl_id`, so `connector_id="windns-ssh-2016.x"`
round-trips (a hyphen/underscore in the product token breaks the registry's
round-trip guard at boot — the reason `vcf_logs` retired its `vcf-logs`
token). `version="2016.x"` marks the DnsServer cmdlet surface, stable across
Windows Server 2016 → 2025. `impl_id="windns-ssh"` leaves room for a future
`windns-winrm` sibling.

Source: `backend/src/meho_backplane/connectors/windows_dns/`.

## Op surface (5 ops)

| op | safety | approval | cmdlet(s) |
| --- | --- | --- | --- |
| `windns.about` | safe | no | `Get-Module -ListAvailable DnsServer` + `[System.Net.Dns]::GetHostName()` |
| `windns.zone.list` | safe | no | `Get-DnsServerZone` |
| `windns.record.get` | safe | no | `Get-DnsServerResourceRecord -ZoneName [-Name] [-RRType]` |
| `windns.record.add` | caution | no | `Add-DnsServerResourceRecordA -IPv4Address` (A) / `Add-DnsServerResourceRecordCName -HostNameAlias` (CNAME), optional `-TimeToLive (New-TimeSpan -Seconds <ttl>)` |
| `windns.record.remove` | caution | no | `Remove-DnsServerResourceRecord -ZoneName -Name -RRType -Force` |

The two writes are `safety_level="caution"` (a DNS change is global — no
per-caller scoping), mirroring bind9's record writes; the production-path
gate is G7/G10 policy territory keyed on that value, so
`requires_approval=false` today. `record.get` supports
A/AAAA/CNAME/MX/TXT/PTR/NS/SRV/SOA as `-RRType` filters; `record.remove`
the same set minus SOA; `record.add` is A + CNAME only (the bind9
write-surface mirror). `record.remove` deletes **all** records matching
`(zone, name, RRType)` — the cmdlet default when `-RecordData` is omitted;
a precision-delete `record_data` parameter is a known follow-up.

## Key types

- **`WindowsDnsConnector`** (`connector.py`) — `SshConnector` subclass.
  Class attributes: `product="windns"`, `version="2016.x"`,
  `impl_id="windns-ssh"`, `POWERSHELL_EXECUTABLE="powershell"`. Ships
  `fingerprint`, `probe`, `about`, the bound-method op shims
  (`windows_dns_zone_list`, `windows_dns_record_get` / `_add` / `_remove`),
  `register_operations`, and the operator-less `execute` dispatcher shim
  (same shape as bind9 / Holodeck). Inherits the per-target asyncssh
  connection pool, `_auth_config`, `_run_command`, `_assert_reachable`,
  and `aclose()` from the adapter.
- **Shared pwsh transport** (`_shared/pwsh.py`) — `pwsh_run` (the single
  seam every handler routes its script through), `encode_pwsh_command`
  (the base64/UTF-16LE encoder), `ps_single_quote` (the `shlex.quote`
  analogue), `strip_clixml` (the CLIXML-warning net), `PwshRunError`
  (structured failure carrying `exit_status` + truncated `stderr`, never
  the script body or auth material), `PWSH_DEFAULT_DEPTH` (=4). Hoisted
  out of the former package-local `windows_dns/_pwsh` + `holodeck/_pwsh`
  copies (#3260, executing the #2759 note) once the Microsoft-estate
  program (#3259) pushed the consumer count past the ≥3 sharing
  threshold. Per-connector wire variation is three class-level seams on
  the connector: `POWERSHELL_EXECUTABLE` (`powershell` here),
  `POWERSHELL_SCRIPT_PREFIX` (the `$ProgressPreference` guard here),
  `POWERSHELL_LOG_EVENT` (`windows_dns_pwsh_executed`).
- **Op metadata** (`ops.py`) — `WindowsDnsOp` frozen dataclass (mirrors
  `Bind9Op` / `HolodeckOp`) and `WINDOWS_DNS_OPS` (`about` + `ZONE_OPS` +
  `RECORD_OPS`).
- **Zone read** (`ops_zone.py`) — `windows_dns_zone_list` and
  `normalise_json_rows` (collapses `ConvertTo-Json`'s dict / list / null
  shapes to `list[dict]`).
- **Record ops** (`ops_record.py`) — `windows_dns_record_get` / `_add` /
  `_remove` and the supported-`RRType` frozensets. `ps_single_quote` (the
  PowerShell analogue of `shlex.quote`) is imported from `_shared/pwsh`.
- **Registration** (`__init__.py`) — two-phase, mirroring bind9/holodeck:
  synchronous `register_connector_v2` at import time (versioned triple +
  wildcard row); async `register_windows_dns_typed_operations` queued onto
  the lifespan registrar list. No v1 `register_connector` — no chassis
  history.

## Transport: PowerShell-over-SSH

Every op runs `powershell -NoProfile -NonInteractive -EncodedCommand
<base64>` over the pooled SSH connection:

- **`powershell` (Windows PowerShell 5.1), not `pwsh` (PS7).** A Windows
  Server AD-DNS host ships 5.1; PS7 is a separate optional install that is
  absent by default on a domain controller (verified 2026-08-03 against a
  live WS2022 DC: `pwsh` → "not recognized"; `powershell` → DnsServer
  module 2.0.0.0). A future PS7 target overrides the
  `POWERSHELL_EXECUTABLE` class attr.
- **`-EncodedCommand` wire shape** — the script is UTF-16LE-encoded and
  base64'd (no BOM). The shell never parses the script text, so there is no
  `sh -c` quoting layer; only the PowerShell parser sees it.
- **`$ProgressPreference = 'SilentlyContinue'` guard** — prepended to every
  script so the DnsServer module's first-use progress stream never
  serialises CLIXML noise onto stderr.
- **JSON out** — handlers pipe through `ConvertTo-Json` (explicit
  `-Depth 4`; the PowerShell default of 2 silently truncates nested
  `RecordData`) and `pwsh_run` parses stdout with stdlib `json`.
- **Hashtable envelope for reads** — `ConvertTo-Json` emits *nothing* on a
  zero-object pipeline, which would trip `pwsh_run`'s empty-stdout guard,
  so both reads wrap their result:
  `ConvertTo-Json -Depth 4 -InputObject @{ rows = $x; total = $x.Count }`.
  `record.get` runs under `$ErrorActionPreference = 'SilentlyContinue'`
  (a missing zone / no-match read is a legitimate empty result);
  `zone.list` runs under `'Stop'` (it takes no narrowing parameters, so a
  cmdlet error — DNS service stopped, insufficient rights — must exit
  non-zero and surface the real stderr via `PwshRunError`, not read as a
  false empty inventory). Writes also run under `'Stop'` with a
  `@{ ok = $true }` JSON tail.
- **Secret hygiene** — logging emits `script_len` / `encoded_len` /
  `exit_status` only; `PwshRunError` never retains the script or the
  encoded payload. The SSH credential is the only secret and it never
  reaches the script text.

### Injection safety

Operator-supplied strings (zone / name / IP / CNAME target) are
interpolated only inside **single-quoted** PowerShell literals via
`ps_single_quote` (embedded `'` doubled — inside a single-quoted PowerShell
string the only metacharacter is `'` itself, so doubling is complete
escaping). TTL is validated as a non-negative `int` before interpolation;
A-record IPs must parse as `ipaddress.IPv4Address` before reaching the wire.

## `fingerprint(target)` / `probe(target)`

`fingerprint` runs one `-EncodedCommand` round-trip reading the hostname +
DnsServer module presence/version; returns `vendor="microsoft"`,
`product="windows-dns"`, `version=<module version>`, with
`extras={hostname, dnsserver_module_present}`. Any transport / credential
failure (`OSError`, `asyncssh.Error`, `ValueError`, `VaultClientError`,
`CredentialsReadError`, `PwshRunError`) maps to `reachable=False` +
`extras["error"]` — never an unhandled exception (#986). `about`
(`windns.about`) wraps `fingerprint` + `_assert_reachable`.

`probe` surfaces five distinct `ProbeResult.reason` values:

| reason | meaning |
| --- | --- |
| `tcp_unreachable` | the SSH TCP socket cannot connect |
| `ssh_auth_failed` | credentials rejected / handshake failed / Vault credential unresolvable |
| `pwsh_unavailable` | SSH succeeded but the PowerShell reachability script failed |
| `command_failed` | post-connect transport failure — connection drop (`asyncssh.Error`) or command timeout / socket error (`OSError`) after a successful handshake (the #986 post-connect guard bind9 documents) |
| `dnsserver_module_missing` | PowerShell runs but the `DnsServer` module is not installed |

## Auth

Uses the base `SshConnector._auth_config` **unchanged** — a Vault secret
with `ssh_private_key` prefers key auth, otherwise password auth runs (the
Holodeck password-default + key-fallback shape). Credentials resolve via
`_shared/vault_creds` from the Vault KV-v2 path string in
`target.secret_ref` (the #2155 either/or shape). `probe()` carries no
operator, so its Vault read runs under the synthesised system operator and
fails closed to `ssh_auth_failed`.

## Control flow

1. **Boot** — `_eager_import_connectors()` imports the `windows_dns`
   subpackage; `__init__.py` registers the v2 triple + wildcard
   synchronously and queues the typed-op registrar.
2. **Lifespan startup** — `run_typed_op_registrars()` →
   `register_windows_dns_typed_operations` →
   `WindowsDnsConnector.register_operations`, which upserts each
   `WINDOWS_DNS_OPS` entry into `endpoint_descriptor` (idempotent across
   restarts) with the curated per-group `when_to_use`
   (`identity` / `zone` / `record` groups).
3. **Dispatch** — `POST /api/v1/operations/call` resolves
   `connector_id="windns-ssh-2016.x"` + the target and invokes the bound
   handler through the policy/audit path. Transport/auth failures propagate
   to the dispatcher's `connector_error` branch.

## Tests

`backend/tests/test_connectors_windows_dns.py` — mirrors the bind9-reads +
Holodeck-pwsh harness (`_StubTarget`, `_completed_process`,
`patch.object(connector, "_run_command", AsyncMock(...))`); assertions
decode the `-EncodedCommand` base64 back to the script text. Covers the
encode round-trip, `ps_single_quote`, the zone.list envelope (incl. the
zone-less server case), record.get filters / empty-match /
single-quote-injection, record.add A + CNAME + TTL + validation rejects,
record.remove `-Force`, `about` mapping, the probe post-connect
`command_failed` guard (parametrized over `OSError` /
`asyncssh.ConnectionLost` / `TimeoutError`), and the registry-triple
round-trip + safety-level invariants.

## Known issues / scope

- `windns.record.remove` deletes **all** records matching
  `(zone, name, RRType)`; an optional `record_data` precision-delete
  parameter (cmdlet `-RecordData`) is a candidate follow-up.
- `record.add` supports A + CNAME only (the deliberate bind9
  write-surface mirror); zone create/delete is out of scope.
- Writes were live-validated against a WS2022 AD-DNS DC via `-WhatIf`
  (2026-08-03); cmdlet surfaces are cross-checked against Microsoft Learn.
- Host-key checking is disabled (`known_hosts=None`) at the adapter level
  for v0.2, shared across the whole SSH family; pinning is deferred
  repo-wide.
- The transport helper was hoisted from the package-local `_pwsh.py` to
  the shared `_shared/pwsh.py` (#3260, executing the #2759 note) — one
  copy now serves windows_dns, holodeck, and the incoming Microsoft-estate
  connectors (#3259). Per-connector wire differences ride three
  class-level seams (`POWERSHELL_EXECUTABLE` / `POWERSHELL_SCRIPT_PREFIX`
  / `POWERSHELL_LOG_EVENT`).

## References

- Task #2759 (connector); PR #2760.
- Molds: `docs/codebase/connectors-bind9.md` (op-surface mirror + #986
  probe discipline), `docs/codebase/connectors-holodeck.md`
  (PowerShell-over-SSH transport), `docs/codebase/connectors-rke2.md`
  (SSH-family doc shape).
- DnsServer module cmdlet reference:
  <https://learn.microsoft.com/en-us/powershell/module/dnsserver/>
- `about_powershell_exe` (`-EncodedCommand`, PowerShell 5.1):
  <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1>
