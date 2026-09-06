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
