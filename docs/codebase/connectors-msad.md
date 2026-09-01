# Connector: msad (msad-2022.x / `msad-ssh`)

## Overview

The `msad` connector is the typed `Connector` subclass that manages **Active
Directory** — domain / forest / DC topology facts, and day-2 reads plus guarded
writes over users, groups, computers, and organizational units — through the
`ActiveDirectory` PowerShell module (Windows PowerShell 5.1) run on a **domain
controller** over SSH. It is registered under the `(product="msad",
version="2022.x", impl_id="msad-ssh")` registry triple plus the `("msad", "",
"")` wildcard fallback row, and is a child of the shared `SshConnector` adapter
(`connectors/adapters/ssh.py`) — the same transport base as `WindowsDnsConnector`
and `WinsrvConnector`.

AD exposes **no unified REST API** for these operations; before this connector
the c1sql1 program (evoila-bosnia/claude-rdc-hetzner-dc#2789) stood up the
`c1sql.lab` domain and drove day-2 AD tasks through ungoverned `govc guest.run` +
hand-run PowerShell. The connector is a structural copy of the `winsrv` **estate
mold** (#3261): identity canary + per-domain op groups on the `SshConnector` base,
the PowerShell-over-SSH transport `_shared/pwsh` (base64 UTF-16LE
`-EncodedCommand` + stdlib `json` parse), the `{rows, total}` JSONFlux envelope
for list reads, and the `rke2` approval-parked-write mold for destructive ops. It
complements — not duplicates — `windows_dns` (DNS records) on the same DC.

Registry-triple rationale: `product="msad"` is a short, **separator-free** token
because `parse_connector_id` derives the product from the first hyphen-segment of
`impl_id`, so `connector_id="msad-ssh-2022.x"` round-trips (a hyphen/underscore in
the product token breaks the registry's round-trip guard at boot).
`version="2022.x"` marks the cmdlet surface (stable across Windows Server 2019 →
2025 DCs). `impl_id="msad-ssh"` leaves room for a future `msad-winrm` sibling.

Source: `backend/src/meho_backplane/connectors/msad/`.

## In-task decisions (on the record)

### DC-targeting: the SSH target host IS the domain controller

Every AD cmdlet runs on the SSH target **without** an explicit `-Server` — the
`ActiveDirectory` module contacts the local DC by default. The default and **only
supported topology is "the SSH target host IS a domain controller."** A jump-host
pattern — where the SSH host is not the DC and the cmdlets carry `-Server
<other-dc>` plus a second `-Credential` secret to reach the DC — is **explicitly
deferred**: it needs a second credential brokered from Vault (the connector's
single Vault secret is the SSH credential), and no consumer needs it yet. When a
consumer does, the follow-up adds an optional `-Server` + Vault-brokered
`-Credential` seam; until then, point `msad` targets directly at a DC.

### Password provisioning / reset is deferred (secret-hygiene contract)

The shared pwsh transport's safety contract **forbids credential material in the
`-EncodedCommand` script** — the encoded payload lands on the remote process argv
and is visible to a privileged remote observer (see `_shared/pwsh`). A plaintext
AD password therefore cannot ride the transport. Consequences, mirroring winsrv's
passwordless-create:

- **`user.create` creates a disabled, passwordless account.** Per `New-ADUser`'s
  documented behaviour an account created without `-AccountPassword` is disabled
  and "cannot be enabled until a password is set." Set the password out of band,
  then `user.enable`.
- **`user.set` never touches the password**, and there is **no `password-reset`
  op.** Password provisioning / reset is a Vault-brokered follow-up (the `rke2`
  `token.rotate` mint-and-stash mold, so no secret enters the script).

A unit test (`test_no_op_exposes_a_secret_value_field` +
`test_no_op_id_is_password_reset`) pins this at the schema level: no op declares a
`password` / `secret` / `credential` parameter, and no op id contains
"password". This is the "password-bearing params provably redacted" acceptance
criterion, satisfied the winsrv way — there are none, proven at the schema level.

## Op surface (27 ops across five groups)

Safety tier is a reach decision per the Initiative #3259 satellite table: reads =
`safe` (ride satellite runners); recoverable writes = `caution` (satellite-
executable only through the Stage-3 composed write gate, #2901); destructive =
`dangerous` + `requires_approval` (satellite-EXCLUDED by the tier ladder,
central-dial / on-site only).

| op | safety | approval | cmdlet(s) |
| --- | --- | --- | --- |
| `msad.about` | safe | no | `Get-ADDomain` (minimal) + AD module version |
| `msad.domain.info` | safe | no | `Get-ADDomain` (FSMO PDC/RID/Infra, mode) |
| `msad.domain.forest` | safe | no | `Get-ADForest` (FSMO schema/naming, mode) |
| `msad.domain.controllers` | safe | no | `Get-ADDomainController -Filter *` |
| `msad.domain.replication` | safe | no | `Get-ADReplicationPartnerMetadata -Scope Domain` |
| `msad.user.list` | safe | no | `Get-ADUser -Filter *` (capped) |
| `msad.user.get` | safe | no | `Get-ADUser -Identity` |
| `msad.user.search` | safe | no | `Get-ADUser -Filter {name/sam -like}` (capped) |
| `msad.user.create` | caution | no | `New-ADUser` (disabled, no password) |
| `msad.user.set` | caution | no | `Set-ADUser` (never a password) |
| `msad.user.enable` | caution | no | `Enable-ADAccount` |
| `msad.user.disable` | caution | no | `Disable-ADAccount` |
| `msad.user.delete` | **dangerous** | **yes** | `Remove-ADUser -Confirm:$false` |
| `msad.group.list` | safe | no | `Get-ADGroup -Filter *` (capped) |
| `msad.group.get` | safe | no | `Get-ADGroup -Identity` |
| `msad.group.members` | safe | no | `Get-ADGroupMember -Identity` |
| `msad.group.add-member` | caution | no | `Add-ADGroupMember -Confirm:$false` |
| `msad.group.remove-member` | caution | no | `Remove-ADGroupMember -Confirm:$false` |
| `msad.group.delete` | **dangerous** | **yes** | `Remove-ADGroup -Confirm:$false` |
| `msad.computer.list` | safe | no | `Get-ADComputer -Filter *` (capped) |
| `msad.computer.get` | safe | no | `Get-ADComputer -Identity` |
| `msad.computer.join-prestage` | caution | no | `New-ADComputer` |
| `msad.computer.unjoin` | caution | no | `Disable-ADAccount` (recoverable) |
| `msad.computer.delete` | **dangerous** | **yes** | `Remove-ADComputer -Confirm:$false` |
| `msad.ou.list` | safe | no | `Get-ADOrganizationalUnit -Filter *` (capped) |
| `msad.ou.create` | caution | no | `New-ADOrganizationalUnit` |
| `msad.ou.move` | caution | no | `Move-ADObject` |

The three `dangerous` ops (`user.delete`, `group.delete`, `computer.delete`) are
`requires_approval=True` — a dispatch parks at `needs-approval` for a human
decision (the rke2 write mold; the generic params-echo default (#1856) surfaces
target / params redaction-safely to the reviewer). The **agent-principal ceiling**
therefore applies: an agent session cannot self-approve these — approval is a
human decision (v0.1-spec §7), so an agent dispatch floors at the approval queue
until an operator approves in the console / CLI. Reads ride satellite runners; the
`dangerous` ops are excluded from satellites by the tier ladder.

`-Confirm:$false` on the delete / remove-member ops suppresses the AD cmdlets'
own interactive confirmation prompt (they carry a High `ConfirmImpact` and would
otherwise hang over the non-interactive `-EncodedCommand` transport) — the
governance gate is MEHO's `requires_approval`, not AD's `-Confirm`.

### "unjoin" is a disable, not a delete

`computer.unjoin` runs `Disable-ADAccount` — the recoverable inverse of a live
domain join from the directory side: the machine can no longer authenticate to
the domain, but its account object is retained (re-enable restores trust). A full
object removal is the separate `dangerous` `computer.delete`. This keeps the
`caution` (recoverable) / `dangerous` (destructive) split honest.

## Key types

- **`MsadConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="msad"`, `version="2022.x"`, `impl_id="msad-ssh"`,
  `POWERSHELL_EXECUTABLE="powershell"`, `POWERSHELL_SCRIPT_PREFIX` (the
  `$ProgressPreference` guard), `POWERSHELL_LOG_EVENT="msad_pwsh_executed"`. Ships
  `fingerprint`, `probe`, `about`, the 27 bound-method op shims,
  `register_operations`, and the operator-less `execute` dispatcher shim.
- **Shared pwsh transport** (`_shared/pwsh.py`, #3260) — `pwsh_run`,
  `ps_single_quote` (the `shlex.quote` analogue), `encode_pwsh_command`,
  `strip_clixml`, `PwshRunError`.
- **Op metadata** (`ops.py`) — `MsadOp` frozen dataclass (mirrors `WinsrvOp`),
  `MSAD_OPS` (`about` + the five per-domain tuples), `MSAD_WHEN_TO_USE_BY_GROUP`
  (curated per-group blurbs, registration fails closed without them),
  `normalise_json_rows`, `validate_limit`, and `ad_list_read` (the shared
  read-under-`'Stop'` helper that builds the `{rows, total}` envelope).
- **Per-domain op modules** — `ops_domain.py`, `ops_users.py`, `ops_groups.py`,
  `ops_computers.py`, `ops_ou.py`.
- **Registration** (`__init__.py`) — two-phase, mirroring winsrv: synchronous
  `register_connector_v2` at import time (versioned triple + wildcard row); async
  `register_msad_typed_operations` queued onto the lifespan registrar list. No v1
  `register_connector` — no chassis history.

## Transport: PowerShell-over-SSH

Every op runs `powershell -NoProfile -NonInteractive -EncodedCommand <base64>`
over the pooled SSH connection:

- **`powershell` (Windows PowerShell 5.1), not `pwsh` (PS7).** A Windows DC ships
  5.1; PS7 is absent by default. The connector sets
  `POWERSHELL_EXECUTABLE="powershell"` explicitly — **the load-bearing estate
  trap:** the shared transport's fallback is `pwsh`, so a connector that forgets
  this attribute silently emits `pwsh -EncodedCommand` and fails confusingly on
  every 5.1-only host.
- **`-EncodedCommand` wire shape** — the script is UTF-16LE-encoded and base64'd
  (no BOM). The shell never parses the script text; only the PowerShell parser
  does.
- **`$ProgressPreference = 'SilentlyContinue'` guard** — prepended so the
  ActiveDirectory module's first-use progress / import warning never CLIXML-
  serialises onto the streams.
- **JSON out** — reads pipe through `ConvertTo-Json` (explicit `-Depth`) and
  `pwsh_run` parses stdout with stdlib `json`. Rich AD objects are projected to a
  bounded scalar subset via `Select-Object` (or an explicit hashtable projection
  for the FSMO-bearing domain / forest reads) so the JSON stays stable and small.
- **`{rows, total}` envelope for list reads** — `ConvertTo-Json` emits *nothing*
  on a zero-object pipeline (which trips `pwsh_run`'s empty-stdout guard), so
  every list read wraps its result in `@{ rows = $x; total = $x.Count }` under
  `$ErrorActionPreference = 'Stop'` (a cmdlet error must surface as a real
  `PwshRunError`, not read as a false-empty inventory). The envelope is what the
  dispatcher's JSONFlux reducer keys on to spill a set-shaped result to a
  `result_query` handle. `-Filter *` reads carry a `-ResultSetSize` cap (default
  500, operator-overridable via `limit`) so a large domain can't blow the payload.

### Injection safety

Operator-supplied identities, attribute values, member identities, and DNs are
interpolated only inside **single-quoted** PowerShell literals via
`ps_single_quote` (embedded `'` doubled — complete escaping inside a single-quoted
PowerShell string). The `limit` cap is Python-validated to a positive `int` before
interpolation; the `protected` toggle renders as a fixed `$true` / `$false`
literal.

The **`user.search`** query is the one non-`-Identity` operator input on a
filter. It is bound the documented injection-safe way: the query is wrapped in
`*…*`, assigned to `$q` as a single-quoted literal, and passed through a
**script-block** `-Filter {(Name -like $q) -or (SamAccountName -like $q)}`. Per
the `about_ActiveDirectory_Filter` guidance, the AD filter engine binds the
session variable's *value* as a data operand rather than re-parsing an
interpolated string — so a value containing a quote can't break out of the filter.

## `fingerprint(target)` / `probe(target)`

`fingerprint` runs one `-EncodedCommand` round-trip reading `Get-ADDomain` (DNS
root / NetBIOS / forest / domain mode / PDC emulator) + the ActiveDirectory module
version; returns `vendor="microsoft"`, `product="active-directory"`,
`version=<domain functional level>`, with `extras={dns_root, netbios_name, forest,
pdc_emulator, ad_module_version}`. Any transport / credential / non-DC failure
maps to `reachable=False` + `extras["error"]` — never an unhandled exception
(#986). `about` (`msad.about`) wraps `fingerprint` + `_assert_reachable`.

`probe` surfaces six distinct `ProbeResult.reason` values:

| reason | meaning |
| --- | --- |
| `tcp_unreachable` | the SSH TCP socket cannot connect |
| `ssh_auth_failed` | credentials rejected / handshake failed / Vault credential unresolvable |
| `powershell_unavailable` | SSH succeeded but the PowerShell reachability script failed |
| `command_failed` | post-connect transport failure (drop / timeout / socket error) after a successful handshake (the #986 guard) |
| `ad_module_unavailable` | PowerShell runs but the `ActiveDirectory` module is not installed (target is not a DC / lacks RSAT-AD) |
| `domain_unreachable` | the module is present but `Get-ADDomain` fails (directory services down / not a DC) |

The probe script checks module presence + domain readability in one round-trip and
always emits JSON (the `Get-ADDomain` failure is caught inside the script), so a
`PwshRunError` unambiguously means `powershell_unavailable`.

## Auth

Uses the base `SshConnector._auth_config` **unchanged** — a Vault secret with
`ssh_private_key` prefers key auth, otherwise password auth runs. Credentials
resolve via `_shared/vault_creds` from the Vault KV-v2 path in
`target.secret_ref`. The transport prerequisite is an **OpenSSH server on the
domain controller** with `powershell` reachable, plus the `RSAT-AD-PowerShell` /
AD DS role installed (present by default on a DC). `probe()` carries no operator,
so its Vault read runs under the synthesised system operator and fails closed to
`ssh_auth_failed`.

## Control flow

1. **Boot** — `_eager_import_connectors()` imports the `msad` subpackage;
   `__init__.py` registers the v2 triple + wildcard synchronously and queues the
   typed-op registrar.
2. **Lifespan startup** — `run_typed_op_registrars()` →
   `register_msad_typed_operations` → `MsadConnector.register_operations`, which
   upserts each `MSAD_OPS` entry into `endpoint_descriptor` (idempotent across
   restarts) with the curated per-group `when_to_use`. The op rows come from
   **runtime registration, not an alembic migration** — there is no schema change.
3. **Dispatch** — `POST /api/v1/operations/call` resolves
   `connector_id="msad-ssh-2022.x"` + the target and invokes the bound handler
   through the policy/audit path. `dangerous` + `requires_approval` ops park at
   `needs-approval` first. The CLI reaches the same path via `meho operation call
   msad-ssh-2022.x <op_id> --params-json '{...}'`.

## Building on the estate mold

`msad` copies the `winsrv` package (see `docs/codebase/connectors-winsrv.md` →
"Building the next estate connector"). The single most important footgun is the
`POWERSHELL_EXECUTABLE = "powershell"` override (documented above). Other copy
points honoured here: the `$ProgressPreference` prefix; a connector-scoped
`POWERSHELL_LOG_EVENT`; the `{rows, total}` envelope under `$ErrorActionPreference
= 'Stop'` for list reads; `ps_single_quote` on every operator string; no
credential material in any script; and destructive ops on the `dangerous` +
`requires_approval` tier.

## Tests

`backend/tests/test_connectors_msad.py` — mirrors the winsrv harness
(`_StubTarget`, `_completed_process`, `patch.object(connector, "_run_command",
AsyncMock(...))`); assertions decode the `-EncodedCommand` base64 back to the
script text. Covers the transport (`powershell` + `$ProgressPreference`), the
registry-triple round-trip, the 27-op surface + safety-tier + group-coverage
invariants, every op group's script construction, the injection-safety escape on
every parameterized script (identity / member / search query / OU DN —
single-quote doubling), the `user.search` script-block filter form, the
`limit`/`-ResultSetSize` validation, the secret-hygiene invariant (no
secret-value parameter on any op; no `password-reset` op; passwordless create),
the `about` fingerprint mapping, and the full six-reason probe matrix. The global
`test_flight_recorder_typed_spans.py` conformance sweep lists `msad-ssh` in
`_EXPECTED_TYPED_IMPLS`. **No spec-reconcile lane** — the cmdlet surface ships no
OpenAPI (the confirmed convention for SSH-typed connectors); drift protection is
this ordinary unit suite.

## CLI

No dedicated `meho msad` verb package (the windows_dns / winsrv / rke2 precedent
for SSH-typed connectors). CLI parity is the generic dispatch path — `meho
operation call msad-ssh-2022.x <op_id> --params-json '{...}'` — plus the `msad`
product token added to the OpenAPI `TargetCreate.product` enum (regenerated from
the live registry, so `meho targets create --product msad ...` validates and the
generated Go client knows the token).

## Known issues / scope

- **Password provisioning / reset** is not shipped (create is passwordless →
  disabled; there is no `password-reset` op) — a Vault-minted-password flow
  (rke2 `token.rotate` mold) is a follow-up. See the in-task decision above.
- **Jump-host / `-Server` targeting** is deferred — the SSH target must itself be
  a domain controller. See the in-task decision above.
- **Group creation** and **OU deletion** are out of scope for this cut (the issue
  enumerates group add/remove-member + delete, and ou list/create/move); they are
  natural follow-ups when a consumer needs them.
- **Consumer proof against the live `c1sql-dc1` DC is an OPEN acceptance
  criterion** — the c1sql1 domain-controller VM/probe path is not reachable from
  this build; the probe + governed guarded-write (e.g. group-member add) proof is
  deferred to the lab program (evoila-bosnia/claude-rdc-hetzner-dc#2789 / #2792).
- Host-key checking is disabled (`known_hosts=None`) at the adapter level for
  v0.2, shared across the whole SSH family; pinning is deferred repo-wide.

## References

- Task #3262 (connector); Initiative #3259 (Microsoft estate); prerequisites
  #3260 (shared `_shared/pwsh` transport hoist) and #3261 (`winsrv` estate mold).
- Molds: `docs/codebase/connectors-winsrv.md` (structure + probe #986 discipline +
  PowerShell-over-SSH transport + the `POWERSHELL_EXECUTABLE` trap),
  `docs/codebase/connectors-windows-dns.md`, `docs/codebase/connectors-rke2.md`
  (approval-parked-write mold).
- Cmdlet references (ActiveDirectory module, Windows Server 2022 / PowerShell
  5.1): <https://learn.microsoft.com/en-us/powershell/module/activedirectory/>
  (`Get-ADDomain` / `Get-ADForest` / `Get-ADUser` / `New-ADUser` / `Set-ADUser` /
  `*-ADAccount` / `Get-ADGroup` / `Add-ADGroupMember` / `Get-ADComputer` /
  `New-ADComputer` / `Get-ADOrganizationalUnit` / `Move-ADObject` /
  `Get-ADReplicationPartnerMetadata`), and `about_ActiveDirectory_Filter` for the
  safe script-block filter form.
