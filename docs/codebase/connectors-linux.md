# Connector: linux (linux-1.x / `linux-ssh`)

## Overview

`linux-ssh` is a typed, SSH-transport connector for **generic
systemd-Linux hosts** — the governed surface for reading a file, tailing a
log, checking a service, and inspecting firewall / mounts / kernel
parameters on any IP-reachable Linux host through the same
`call_operation` / `preview_operation` dispatch every other connector
rides (auth + policy + audit + broadcast + JSONFlux).

It is **branch (b)** of the guest-operations design fork (#3100): the tier
for hosts VMware Tools cannot reach — Tools-less appliances,
non-VMware-hypervisor guests, bare metal, or any host no vCenter target
fronts. It is complementary to the `vmware.composite.vm.guest.*` family
(branch (a), #3255), not a replacement; the two coexist by target
fingerprint (see `connectors-vmware-rest-guest-ops.md`).

This page documents **T1 (#3360)**, the keystone read floor: the connector
skeleton, transport, probe / fingerprint, and six `safe` read verbs plus
the `linux.about` identity canary. The governed write surface (T2, #3361)
and the day-0 verification recipe (T3, #3362) land separately.

The motivating failure T1 retires: a provisioning run stands up a
services/router VM and returns `configured` on deploy + power-on alone,
while the guest's `set -euo pipefail` first-boot script aborts closed (a
missing NIC, an unresolvable package mirror, a red config-validate). The
script writes a completion sentinel and a first-boot log that **nothing
reads**. Three T1 reads — `file.read` of the sentinel, `log.tail` of the
log, `service.status` of each declared unit — catch that at the moment the
run declares ready, over a governed, audited channel instead of an
operator's unaudited SSH session.

## Identity & registration

- **Registry v2 triple:** `(product="linux", version="1.x",
  impl_id="linux-ssh")`, registered synchronously at import in
  `connectors/linux/__init__.py` via `register_connector_v2`, plus the
  wildcard `(product="linux", version="", impl_id="")` fallback so a fresh
  or unfingerprinted `product="linux"` target (the common distro-agnostic
  case) resolves through the resolver's `versioned_over_wildcard` step
  rather than 501-ing with `no_connector`. The versioned entry always wins
  when both match.
- **Separator-free product token.** `linux` carries no hyphen or
  underscore, so `connector_id="linux-ssh-1.x"` round-trips through
  `parse_connector_id` (product = the first hyphen-segment of `impl_id`)
  and the registry's `_assert_product_impl_id_round_trips` boot guard
  passes — the `windns` / `winsrv` precedent.
- **No v1 `register_connector`.** Like rke2 / windows_dns, this connector
  has no chassis-route history; only the v2 triple advertises the class.
- **Typed-op upserts** run at lifespan startup via the module-level
  `register_linux_typed_operations(*, embedding_service=None)`
  (accept-and-discard the kwarg) queued onto `register_typed_op_registrar`,
  which walks `LINUX_OPS` through `register_typed_operation`. Registration
  fails closed with `ValueError` if a declared `group_key` lacks a curated
  `LINUX_WHEN_TO_USE_BY_GROUP` blurb.

One implementation serves the whole systemd-Linux family using portable
primitives (`systemctl`, `cat`, `nft` / `iptables`, `sysctl`); the
distro / version is surfaced in the fingerprint for the resolver, not
split into per-distro impls. A genuine per-distro command dialect would
land as a *second* impl resolved by fingerprint (the versioned dual-impl
policy), not a rewrite.

## Transport & auth

`LinuxSshConnector` subclasses the shared `SshConnector` adapter and
**inherits** the asyncssh connection pool (keyed on the tenant-unique
`(tenant_id, id)` cache key, 5-minute idle TTL, per-target handshake
lock), the per-command timeout, the one-flight-recorder-span-per-command
seam, and `aclose()`. It overrides only `fingerprint` / `probe` /
`execute` and adds `about` plus the per-op bound-method handler shims. It
does **not** override `_auth_config` and does **not** touch `known_hosts`.

**Credentials are per target — and that is exactly right here.** Because
the Linux host *is* the target (a 1:1 target↔host mapping), "one
credential per target" avoids the guest-ops one-cred-per-vCenter-target
limitation. Resolution reuses the base seam unchanged: `target.secret_ref`
(a Vault KV-v2 path **string**, not an embedded dict) →
`load_vault_secret_data` under the operator's identity → key auth
preferred (`ssh_private_key` → `client_keys`), password fallback
(`password`), default username `root`; every field through
`strip_credential_value`. Operator-less callers (probe / readiness) fall
back to the synthesised system operator, which live Vault rejects —
fail-closed.

## Fingerprint & probe

`fingerprint(target, operator=None)` runs **one fixed round-trip** (no
operator input interpolated) reading hostname + `/etc/os-release` +
`uname -r` + init-system detection, and returns:

| Field | Source |
|---|---|
| `vendor` | the distro ID family — `ID_LIKE`'s first token, else `ID`, else `linux` |
| `product` | always `linux` |
| `version` | `/etc/os-release` `VERSION_ID` (or `null`) |
| `build` | kernel release (`uname -r`) |
| `extras` | `hostname`, `os_pretty` (`PRETTY_NAME`), `kernel`, `init_system`, `distro_id` |

`probe_method` is `"ssh: cat /etc/os-release"`. Any transport or
credential failure (`OSError`, `asyncssh.Error`, `ValueError`,
`VaultClientError`, `CredentialsReadError`) maps to `reachable=False` +
`extras["error"]` — never an unhandled raise (the #986 discipline).
`about` wraps `fingerprint` and calls the shared `_assert_reachable`, so an
unreachable host surfaces as a `ConnectorUnreachableError` the dispatcher
reports as a non-ok result rather than a hollow `status="ok"` envelope of
`None` fields.

`probe(target)` is read-only, runs under the system operator, fails
closed, and surfaces a fixed **five-reason matrix**:

| `reason` | Condition |
|---|---|
| `tcp_unreachable` | the SSH TCP socket cannot connect (host down, firewall, wrong port) |
| `ssh_auth_failed` | credentials rejected, a non-auth handshake failure, or a failed Vault credential read |
| `command_failed` | the handshake succeeded but a post-connect command dropped or timed out |
| `os_release_unreadable` | `cat /etc/os-release` exited non-zero or returned empty |
| `systemd_absent` | a reachable host with **no** systemd — load-bearing, since the service verbs depend on `systemctl` |

## The read surface (T1)

Every T1 op is `safe`, `requires_approval=False`, and read-only. Handlers
return **plain dicts** — a flat dict for scalars, `{rows, total}` for sets
— and never build `OperationResult` or result handles themselves; the
dispatch path's `JsonFluxReducer` wraps set-shaped results.

| op_id | group | Params | Returns |
|---|---|---|---|
| `linux.about` | `system` | — | `{vendor, product, version, kernel, os_pretty, hostname, init_system}` |
| `linux.file.read` | `file` | `path`, `max_bytes?` | `{path, content, truncated, size_bytes, exists}` |
| `linux.log.tail` | `log` | `path`, `lines?` | `{rows, total, path}` |
| `linux.service.status` | `service` | `unit` | `{unit, active, enabled, sub_state}` |
| `linux.sysctl.read` | `system` | `key` | `{key, value}` |
| `linux.firewall.show` | `firewall` | — | `{rows, total, backend}` |
| `linux.mount.list` | `storage` | — | `{rows, total}` (each row `kind` = `mount` / `export`) |

`linux.about` is the identity canary (its handler wraps `fingerprint`).
`linux.mount.list` inspects the mount table (`findmnt` / `mount`) plus NFS
exports (`exportfs -s` / `showmount -e`); each row carries a `kind`
discriminator. Block-topology and capacity reporting (`df` / `lsblk`) is
out of scope — a separate op if a concrete need arises.

### Path confinement

The operator-named paths (`file.read`, `log.tail`) are confined with the
bind9 `ensure_path_under_root` lexical-confinement mold, generalised to an
**allow-list of read roots**: `/etc`, `/var/log`, `/var/lib`, `/run`,
`/proc`, `/sys`. A resolved path must equal or descend one of these roots;
`..` traversal is collapsed with `posixpath.normpath` and rejected if it
escapes, a trailing-slash sentinel stops `/etc-evil` matching `/etc`,
control bytes are refused, and a non-absolute path is rejected. Rejection
raises `PathConfinementError` **before any SSH command is constructed**, so
a rejected path never reaches the host. `file.read` caps content itself
(`head -c`, default 64 KiB, hard cap 1 MiB) with a `truncated` flag,
because a flat scalar dict is never JSONFlux-reduced.

### Injection safety

Every operator-supplied value is `shlex.quote`-wrapped into a **fixed**
command. `unit` and `key` are additionally re-validated against a strict
charset before command construction (the schema `pattern` is advisory, the
handler re-check authoritative — the proxmox method-allow-list mold); the
charset admits no shell metacharacters, so quoting is defence in depth.
Unit tests decode each constructed command and assert both the quoting and
the confinement.

### JSONFlux for set-shaped results

`log.tail`, `firewall.show`, and `mount.list` return `{rows, total}`. The
reducer detects `rows` as the single real list-valued collection (`total`
is preserved as a scalar sibling) and, above the 50-row / 4 KB threshold,
spills the set to a result handle the agent pages with
`result_query(handle_id, offset, limit)`. Below threshold the envelope
passes through unchanged.

### Secret hygiene

No op declares a `password` / `secret` parameter (a schema-pinned test
guards this; the `sysctl.read` `key` param is a kernel-parameter name, not
a secret). The read floor never reads secret material into an operation
parameter, an `OperationResult`, an audit row, a broadcast event, or a log
line; the shared SSH seam logs only command length + exit code, and hands
the command + output to the fail-closed redaction engine.

## The write surface (T2)

The governed day-2 configuration + remediation tier. It turns the
state-changing moves an operator performs today over unaudited bare SSH
into governed operations that flow through the same policy / approval /
audit / broadcast / preview path every other write does. The handlers live
in `ops_write.py`; the root-elevation primitive lives in `_sudo.py`.

| op_id | group | Tier | Params | Returns |
|---|---|---|---|---|
| `linux.file.write` | `file` | `dangerous` + approval | `path`, `content`, `validate_command?`, `backup?` | `{written, path, backup_path, validated, rolled_back}` |
| `linux.service.control` | `service` | `caution` (no approval) | `action`, `unit`, `daemon_reload?` | `{unit, action, daemon_reload, ok}` |
| `linux.script.run` | `exec` | `dangerous` + approval | `script`, `interpreter?`, `arguments?`, `working_directory?`, `env?`, `timeout_seconds?`, `use_sudo?` | `{stdout, total, stderr, exit_code, interpreter, used_sudo}` |
| `linux.sysctl.write` | `system` | `dangerous` + approval | `key`, `value` | `{key, value, applied, dropin_path}` |
| `linux.firewall.load` | `firewall` | `dangerous` + approval | `ruleset`, `backend?` | `{applied, backend, validated}` |

### Tier rationale

`service.control` is `caution`, not `dangerous`: a systemd action
(start / stop / restart / reload / enable / disable) is recoverable, so it
follows the estate `service.*` mold and runs immediately rather than
parking — no approval, but still audited and broadcast. The other four are
`dangerous` + `requires_approval=True`: they write config, run arbitrary
code, change a kernel parameter, or replace a firewall ruleset — a botched
one wedges the host, so a human approves first. **No op is `destructive`**:
none of these deletes an irreplaceable object, so the destructive-tier
blast-radius builder gate (which refuses to park a `destructive` op without
a bound blast-radius preview) does not apply here — `file.write` is
`dangerous`, not `destructive`, because its backup + rollback make it
recoverable.

`script.run` is the **intentional arbitrary-code surface** — a typed verb
governed by approval, not an interactive shell. It exists so the aborted
first-boot script can be re-run (or a multi-step fix applied) *through* the
backplane rather than around it. It returns `stdout` as a list of lines so
a large output spills to a `result_query` handle through the JSONFlux
reducer rather than flooding agent context; `stderr` is capped inline.

### Root elevation

Every write that needs root funnels through `_sudo.py`'s
`run_remote_bash_with_sudo`, a **byte-identical copy** of the rke2 safe-sudo
primitive (only the module docstring and the family-scoped structlog event
name are adapted; the rendered wire shape must not drift between families).
The sudo password is streamed as the last stdin line after the exact script
bytes — `head -c <N>` consumes exactly the script, so `sudo -S` reads only
the password line and it never lands in the remote `argv`, the shell
history, `ApprovalRequest.params`, the audit row, a broadcast event, or a
log line. A `sudo_password` containing a control character (`\n` / `\r` /
`\x00`) is rejected **before** the connection opens. The password resolves
from the target's Vault secret (`sudo_password`, then `password`); the
copied sudo path records no flight-recorder span (it logs lengths + exit
code only).

### Split preview posture

The two credential-bearing writes and the two non-secret writes preview
differently, by design:

- `linux.file.write` (`content`) and `linux.script.run` (`arguments` /
  `env`) can carry secret material in their params, so they are pinned
  `credential_write` in `broadcast/events.py` (`_CREDENTIAL_WRITE_OPS`) —
  the broadcast collapses their params to aggregate-only — and
  `preview_operation` returns `preview_unavailable` for them (the
  credential-class exclusion in `_is_previewable`), because the request-time
  preview's `redacted_body` slot cannot scrub a structured secret. Their
  reviewer preview is instead a **bespoke park-time `proposed_effect`**
  (`register_preview_builder`, fail-soft) that echoes only shape — path +
  content byte size + backup path + validate command for `file.write`;
  interpreter + script byte size + argument byte size + env-var **names** +
  working directory + sudo intent + timeout for `script.run` — and **never**
  the `content` / script body / argument values / env values.
- `linux.sysctl.write` and `linux.firewall.load` carry no secret, so they
  register **no** bespoke builder and stay previewable via
  `preview_operation` on the generic params-echo default. For
  `firewall.load` this is the point: the reviewer sees the full ruleset
  before approving.

### Approval + secret hygiene

No approval logic lives in any handler: the dispatcher parks a `dangerous`
+ `requires_approval` op and the handler body runs only on the
`_approved=True` resume path. Approval is a human-only decision — there is
no MCP decision path for it. No op declares a `password` / `secret`
parameter. Every operator-supplied value is `shlex.quote`d or base64-carried
into a fixed command, and `file.write` confines its `path` under a
**narrower** write-root allow-list (`/etc`, `/var/lib`, `/run`, `/opt`,
`/srv`, `/usr/local`) than the read roots — `/proc` / `/sys` (the sysctl
op's job) and `/var/log` are deliberately not writable here.

**Operator rule (shared-machinery caveat):** do not put a bare secret in
`content` / `arguments` / `env`. `ApprovalRequest.params` stores the params
verbatim so the approved call can be re-dispatched, so a secret placed
there is durable on the approval row (the same rule as the guest
`program.run` / `file.write` verbs). Pass a Vault reference the script
resolves at run time instead.

## Key types

- `LinuxSshConnector` (`connectors/linux/connector.py`) — the connector
  class; `fingerprint` / `probe` / `about` / `execute` /
  `register_operations` plus the per-op bound-method shims, and the
  `parse_os_release` / `parse_fingerprint_output` / `_derive_vendor`
  parse helpers.
- `LinuxOp` (`connectors/linux/ops.py`) — the frozen op-metadata
  dataclass mirroring `Rke2Op`; `LINUX_OPS` is the merged registration
  tuple, `LINUX_WHEN_TO_USE_BY_GROUP` the curated group blurbs,
  `normalise_json_rows` the `{rows, total}` envelope helper, and
  `ensure_path_under_root` / `confine_read_path` / `LINUX_READ_ROOTS` /
  `PathConfinementError` the confinement primitives.
- Per-domain op modules: `ops_file.py` (file.read / log.tail),
  `ops_host.py` (service.status / sysctl.read), `ops_firewall.py`
  (firewall.show), `ops_storage.py` (mount.list) — each ships its handler,
  command builder, output parser, and `LinuxOp` rows.

## Control flow

1. `_eager_import_connectors` imports `connectors/linux/`, which registers
   the v2 triple + wildcard synchronously and queues
   `register_linux_typed_operations`.
2. At lifespan startup, `run_typed_op_registrars` invokes the registrar →
   `LinuxSshConnector.register_operations()` upserts every `LINUX_OPS` row
   into `endpoint_descriptor` (idempotent across restarts), resolving each
   op's `when_to_use` from `LINUX_WHEN_TO_USE_BY_GROUP` (fail-closed).
3. `call_operation(connector_id, op_id, target, params)` resolves the
   connector by the target fingerprint, applies auth + policy + audit +
   broadcast, invokes the bound-method handler (the dispatcher threads
   `operator` when the handler declares it), and runs the returned dict
   through the JSONFlux reducer.
4. The operator-less `execute` shim exists for chassis parity
   (windows_dns / rke2 precedent) and has no policy / audit / broadcast.

## Dependencies

- `connectors/adapters/ssh.py` — the `SshConnector` base (pool, auth,
  timeout, flight-recorder span, `_assert_reachable`).
- `connectors/_shared/vault_creds.py` — `load_vault_secret_data` /
  `strip_credential_value`.
- `operations/typed_register.py` — `register_typed_operation` /
  `register_typed_op_registrar`.
- `operations/jsonflux_reducer.py` — the 50-row / 4 KB reducer.
- `connectors/registry.py` — `register_connector_v2` + the round-trip
  boot guard.

## Known issues / follow-ups

- **T2 (#3361) governed writes** — `file.write`, `service.control`,
  `script.run`, `sysctl.write`, `firewall.load` with tiers, sudo (the
  copied `rke2/_sudo.py` primitive), approvals, the broadcast clamp, and
  the two bespoke park-time previews. Depends on T1.
- **T3 (#3362) verification recipe** — the day-0 verification recipe as an
  ordered sequence of T1 read ops, documented and wired into consumer
  onboarding. Depends on T1.
- **`connectors-vmware-rest-guest-ops.md`** still names tier (b) as
  deferred; that pointer is updated to reference this built connector as
  part of the initiative wrap-up (T3), not T1.
- **Host-key pinning** is inherited from the base as deferred to v0.2.next
  (`known_hosts=None`); this connector sets no host-key policy of its own.
- **`file.read` is confined to read roots but not file-content-denylisted**
  — a root-SSH operator can already read those trees over raw SSH; the
  governed path adds audit, and confinement to config/log/state roots is
  the T1 control. A per-file denylist, if ever wanted, is separate work.

## References

- Initiative #3359 (the `linux-ssh` family); Task #3360 (this keystone).
- Fork: #3100 / #3255 / #3298 (guest-ops branches (a) and (b)).
- Base + shared substrate: #243 / #223 (`SshConnector`), #2155
  (`secret_ref` adapter), #697 (sudo stdio hardening), #1682 (tenant cache
  key), #986 (unreachable-target discipline).
- Molds: `connectors-bind9.md`, `connectors-rke2.md`,
  `connectors-windows-dns.md`.
- v0.1-spec §3 / §4 / §6 / §7.

## Day-0 verification recipe

Readiness is an **observation, not an inference from power-on**. A
provisioning run that deploys a services host, powers it on, and applies a
couple of perimeter reach ops has proven only that the VM is *running* —
not that its in-guest day-0 configuration succeeded. For a Tools-less host
(outside the VMware-Tools guest-ops path — see
[`connectors-vmware-rest-guest-ops.md`](connectors-vmware-rest-guest-ops.md)),
this connector is the only governed channel that can look inside. This
recipe is the ordered sequence of T1 read ops a run (or an operator) issues
**at the moment it would declare readiness**, so a silently-aborted
first-boot is caught by a governed, audited read instead of an unaudited
SSH session.

It ships **no new ops** — every step is an already-shipped `safe` T1 read
verb (§ *The read surface (T1)*), or, for the functional probe, an op on
the existing `net` diagnostics connector
([`connectors-net-diagnostics.md`](connectors-net-diagnostics.md)). The
recipe is a documented contract over those verbs, not code.

Each step runs through the ordinary dispatch path (`call_operation` on the
agent surface, `meho operation call linux-ssh-1.x <op_id> --target <host>
--params '{…}'` on the CLI), so every readiness check is auth-scoped,
policy-gated, audited, and broadcast — the property an out-of-band SSH
session cannot offer.

### The ordered sequence

Cheapest-and-most-decisive first. The caller supplies the environment
specifics (the sentinel path, the first-boot log path, the unit list, the
kernel-parameter keys, the probe targets); the op ids and their decisions
are the fixed part.

| # | op (`connector_id`) | Caller params | The decision it drives |
|---|---|---|---|
| 1 | `linux.file.read` (`linux-ssh-1.x`) | `path` (completion sentinel), `max_bytes?` | Present ⇒ first-boot ran to its last line; **absent ⇒ it failed or is still running**. The cheapest decisive signal. A never-written sentinel returns `exists=false` (a legible signal, not an error). |
| 2 | `linux.log.tail` (`linux-ssh-1.x`) | `path` (first-boot log), `lines?` | The terminal "complete" line, or — when step 1 says *absent* — the `set -euo pipefail` **abort reason**: a missing NIC, an unresolvable package mirror, a red config-validate. |
| 3 | `linux.service.status` (`linux-ssh-1.x`) | `unit` (once per declared unit: DNS, DHCP, NTP, the firewall unit, NFS) | `is-active` / `is-enabled` per unit. **Any inactive ⇒ that subsystem is down even if the sentinel exists.** |
| 4 | `linux.sysctl.read` (`linux-ssh-1.x`) | `key` (e.g. `net.ipv4.ip_forward`) | The **live** value of each kernel parameter the day-0 config set — did first-boot actually enable IP forwarding, or only write a `.conf` that never took effect? |
| 5 | `linux.firewall.show` (`linux-ssh-1.x`) | — | The live ruleset (`nft list ruleset` / `iptables-save`). Confirm the **default-deny base + reach block are actually loaded**, not merely that a rules file validated. |
| 6 | `linux.mount.list` (`linux-ssh-1.x`) | — | The mount table + NFS exports (`findmnt` + `exportfs -s` / `showmount -e`). Confirm the **base NFS export the dependent hosts need is live** and the expected mount is present. |
| 7 | `net.dns_lookup` / `net.ntp_check` (`net-probe-1.x`) | dns: `name`, `type?`, `resolver?`; ntp: `host`, `port?` | **Cross-connector functional probe.** A unit being `active` (step 3) is not proof the service *answers* — resolve a name against the host's resolver, read the NTP peer — closing the gap between "unit active" and "service correct." Served by the existing `net` connector, referenced not duplicated. |

Steps 1–6 are the six T1 read verbs of this connector; step 7 is a
functional probe on the `net` diagnostics connector, **not** a Linux verb —
this connector adds no functional-service probe of its own (Initiative
#3359 non-goal). The `net` probe destinations must be inside
`MEHO_NETDIAG_PROBE_ALLOWLIST` or the probe is refused before any packet is
sent. `net` ships no DHCP-lease probe today; a lease-liveness functional
check would be a separate `net`-connector follow-up.

### Steps 1–3 catch the motivating failure

The failure this recipe retires: a run declared a Tools-less services host
"configured" on deploy + power-on + perimeter reach, while the host's
entire in-guest day-0 config had aborted silently — the first-boot script
wrote a completion sentinel and a log that **nothing ever read**.

- **Step 1** (`file.read` of the sentinel) reports `exists=false` — the
  single decisive signal that first-boot did not finish.
- **Step 2** (`log.tail` of the first-boot log) surfaces *why* it aborted.
- **Step 3** (`service.status` per declared unit) confirms which
  subsystems never came up.

Any one of these three, run as a governed op at the moment the run declared
readiness, would have caught the failure immediately. Steps 4–7 harden the
check beyond "did it start" into "is the configuration correct and does the
service answer." Running the recipe **retires the unaudited-SSH diagnosis
path** for the services-host case: the diagnosis that previously required an
operator to open a hand SSH session is now an ordered sequence of audited
reads any caller — human or automation — can issue.
