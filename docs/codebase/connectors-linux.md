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
fingerprint (see `connectors-vmware-rest-guest-ops.md`,
in the private connectors repository).

This page documents the read floor (T1): the connector skeleton,
transport, probe / fingerprint, the `safe` read verbs and the
`linux.about` identity canary. It also documents the governed write
surface (T2, § *The write surface*) and the day-0 verification recipe
(T3, § *Day-0 verification recipe*).

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

The class sets no `supported_version_range`. It sets `version_range_reason`
instead: any systemd host fits, so a rule for the distribution applies, not a
version range.

## Transport & auth

`LinuxSshConnector` subclasses the shared `SshConnector` adapter and
**inherits** the asyncssh connection pool (keyed on the tenant-unique
`(tenant_id, id)` cache key plus the caller's kind and `sub`, so each
caller logs in once after their own Vault read, and neither a Sensor run
by the check runner under a person's `sub` nor a runbook verification
without a token under that `sub` gets that person's login;
5-minute idle TTL; one handshake lock per key), the per-command timeout,
the one-flight-recorder-span-per-command
seam, the 16 MiB cap on the output of one command (`run_capped`), the
address check at connect time (it dials only an address it checked,
`connect_checked`), and `aclose()`. It overrides only `fingerprint` / `probe` /
`execute` and adds `about` plus the per-op bound-method handler shims. It
does **not** override `_auth_config`; it therefore inherits the base
adapter's fail-closed host-key contract (#270) unchanged — a target
whose Vault secret carries no `known_hosts` pin and no
`known_hosts_insecure` opt-out is refused before the SSH connection
opens (see `connectors-shared-vault-creds.md § SSH host-key trust`).

**Credentials are per target — and that is exactly right here.** Because
the Linux host *is* the target (a 1:1 target↔host mapping), "one
credential per target" avoids the guest-ops one-cred-per-vCenter-target
limitation. Resolution reuses the base seam unchanged: `target.secret_ref`
(a Vault KV-v2 path **string**, not an embedded dict) →
`load_vault_secret_data` under the operator's identity → key auth
preferred (`ssh_private_key`, or `private_key` when it is absent →
`client_keys`), password fallback
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
| `linux.about` | `system` | — | `{vendor, product, version, kernel, os_pretty, hostname, init_system, uptime_seconds, load_1m, load_5m, load_15m}` |
| `linux.file.read` | `file` | `path`, `max_bytes?` | `{path, content, truncated, size_bytes, exists, zero_length}` |
| `linux.log.tail` | `log` | `path`, `lines?` | `{rows, total, path}` |
| `linux.service.status` | `service` | `unit` | `{unit, active, enabled, sub_state}` |
| `linux.sysctl.read` | `system` | `key` | `{key, value}` |
| `linux.dns.lookup` | `network` | `name` | `{name, found, addresses, exit_code}` |
| `linux.firewall.show` | `firewall` | — | `{rows, total, backend}` |
| `linux.mount.list` | `storage` | — | `{rows, total}` (each row `kind` = `mount` / `export`) |
| `linux.journal.tail` | `log` | `unit?`, `since?`, `until?`, `priority?`, `lines?` | `{rows, total, truncated}` (+ `warning?`); row = `{ts, unit, priority, message, message_truncated}` |
| `linux.fs.usage` | `storage` | `path` | `{path, filesystem, fstype, size_bytes, used_bytes, avail_bytes, inodes_total, inodes_used}` |
| `linux.dir.list` | `file` | `path`, `depth?`, `include_hidden?` | `{rows, total, path, exists, truncated, incomplete}`; row = `{name, type, size_bytes, mtime, mode, owner}` |
| `linux.tool.which` | `system` | `names` (1 to 20 program names) | `{tools: [{name, found, path}]}` |
| `linux.identity.show` | `system` | — | `{user, uid, gid, groups, is_root, sudo_installed, sudo_passwordless, sudo_exit_code}` |
| `linux.http.head` | `network` | `url` | `{url, reachable, status_code, status_line, headers, curl_exit_code, error}` — `caution` + approval (see below) |

`linux.about` is the identity canary (its handler wraps `fingerprint`).
`linux.firewall.show` reads the rules as root. When the login is not root,
it runs the read under sudo through `_sudo.py`, with the `sudo_password`
(or `password`) of the target's secret. When the secret holds no password,
it sends an empty one: a login that may use sudo without a password still
reads, and any other login gets `permission_denied`. It checks the exit status and keeps the error text. A
read that fails is an error that starts with `permission_denied` or
`read_failed`, never `rows: []`. `backend: none` means only that the host
has neither `nft` nor `iptables-save`.
`linux.mount.list` inspects the mount table (`findmnt` / `mount`) plus NFS
exports (`exportfs -s` / `showmount -e`); each row carries a `kind`
discriminator. Block topology (`lsblk`) is out of scope.

`linux.about` also returns `uptime_seconds` and the 1, 5 and 15 minute load
(`load_1m`, `load_5m`, `load_15m`). They come from `/proc/uptime` and
`/proc/loadavg`, read in the same fixed round-trip as the identity; a value
the host does not answer is `null`.

`linux.file.read` sets `zero_length: true` when a `/proc` or `/sys` file read
back with no content. Those files always report size 0, so empty content is
the kernel's empty answer, not an empty file. An empty regular file has
`size_bytes: 0` and `zero_length: false`.

### Journal, filesystem usage and directory listing (`ops_inspect.py`)

Three reads for storage and NFS diagnosis, in `ops_inspect.py` (handlers
and checks) and `ops_inspect_ops.py` (op rows).

- `linux.journal.tail` runs `journalctl -o json --all --no-pager -n <lines>`
  (default 200, same cap as `log.tail`). `--all` keeps fields over 4096
  bytes; without it `journalctl` prints them as `null`. `unit` uses the
  unit-name check of `service.status`; `since` / `until` allow letters,
  digits, space and `: + . _ -` (max 64 characters) and are passed as
  `--since=<quoted>`; `priority` is `0`-`7` or a syslog level name.
  The command prints the exit code of `journalctl` after its output,
  because a pipe returns only the exit code of its last command. The
  output is cut at 8 MiB (`head` keeps one byte more, so a cut right
  after a line break is still seen), and then `awk` (in the C locale, so
  it counts bytes) keeps only the lines that fit the cap whole, so a cut
  never splits a UTF-8 character. The rows are oldest first, so a cut
  drops the newest rows. A cut ends with a cut marker and sets
  `truncated`. A
  message over 4096 characters is cut and its row has
  `message_truncated: true`. No `journalctl` (exit 127), a `journalctl`
  exit code other than 0, an output with neither the exit code nor the
  cut marker, or no rows with text on stderr (for example no permission
  to read the journal) is an error, never an empty list. Rows with
  stderr text carry a `warning`.
- `linux.fs.usage` runs `df -B1 --output=source,fstype,size,used,avail,itotal,iused`
  for one path. `/` itself is allowed here only. A `-` inode count is
  `null`. A df failure or an answer that cannot be read is an error.
- `linux.dir.list` runs one command: it changes into the directory with
  `cd -P`, prints the real path, then `find -mindepth 1 -maxdepth <depth>`
  with NUL-ended records. `depth` is 1 or 2. Hidden names are left out
  unless `include_hidden`. At most 5000 rows; more sets `truncated`.
  `incomplete` is true when `find` exited non-zero (part of the tree could
  not be read). A missing directory is `exists: false`. The real path the
  host reports goes through the same allow-list check as the typed path.

The paths of `fs.usage` and `dir.list` must lie under the **directory
allow-list**: `LINUX_READ_ROOTS` plus `/srv`, `/mnt`, `/media`, `/data`,
`/export`, `/exports`, `/opt`, plus any absolute roots in the
`LINUX_DIR_LIST_ROOTS` environment variable of the backplane (separated by
`:`; `/` is refused and a wrong entry raises an error). The key and
password deny list of `file.read` applies too. Overlap with the host
readiness reads: `linux.fs.usage` is the one `df` read.

### DNS lookup on the host (`linux.dns.lookup`)

`linux.dns.lookup` answers one question: can this host resolve a name
through its **own** resolver? It runs `getent ahosts '<name>'` on the host,
so the lookup goes through the host's name-service switch (on Ubuntu that
is `systemd-resolved`) and the answer is what programs on that host see.
This is different from `net.dns_lookup`, which resolves from MEHO's side
and cannot show that a host's resolver is broken.

- **Input.** `name` is checked against `^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$`
  (letters, digits, `-`, `.`; must start with a letter or digit; at most
  253 characters) **before** any command is built. A name with a space,
  `;`, `$`, `` ` ``, `|`, `>`, a quote, or a leading `-` is refused with a
  `ValueError` and never reaches the host. Because the charset admits no
  quote character, the builder wraps the name in single quotes itself and
  the command is always literally `getent ahosts '<name>'`.
- **Output.** `{name, found, addresses, exit_code}`. The parser collects
  the first token of every `getent` line (one line per socket type, so
  each address appears three times) and de-duplicates in first-seen
  order. `getent` exit `2` ("key not found") is a **lookup verdict**:
  `found=false`, `addresses=[]`, no error. Any other non-zero exit
  (`127` when `getent` is missing, `1` for a bad invocation) raises
  `LinuxDnsLookupProbeError`, so a probe failure is never reported as
  "not found".
- **Timeout.** The SSH command runs with a 15-second cap
  (`DNS_LOOKUP_TIMEOUT_S`). The host's resolver applies its own per-query
  timeouts underneath; the cap stops a hung resolver from holding the
  command open. A timeout surfaces as a connector error, not as
  `found=false`.
- **Sensors.** The op is `safe` with no approval, so a sensor can watch a
  host's DNS on a schedule — for example `select $.found`, compare
  `{"type": "bool", "expect": true}` — and alert when the host's resolver
  stops answering. `resolvectl query` (which DNS server answered) is not
  part of this op.

### Host-readiness checks (`ops_diagnose.py`)

Use these before `linux.script.run` for a routine readiness check. A script
waits for a person once per run, and the person must read a shell script.
These ops are fixed commands with checked inputs, so there is nothing to
read but the few values.

| Question | Op |
|---|---|
| Is a program installed? | `linux.tool.which` |
| Which user is the login, and does `sudo` work without a password? | `linux.identity.show` |
| Is there free space on the filesystem of a path? | `linux.fs.usage` |
| Can the host reach a URL? | `linux.http.head` |
| Can the host resolve a name? | `linux.dns.lookup` |
| Is the expected export mounted? | `linux.mount.list` |

- **`linux.tool.which`.** `names` is a list of 1 to 20 distinct program
  names, each checked against `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$` (no path,
  no option, no shell character) before any command is built. The command
  is a `command -v` loop. A name with no answer in the output raises an
  error, so a loop that stopped early is never read as "not installed".
  `path` is `null` for a shell builtin.
- **`linux.identity.show`.** No parameters. It runs `id` and one constant
  `sudo -n true`. `sudo_passwordless` is true only when that exits 0.
  `sudo_installed` is false when the host has no `sudo`. An unreadable
  user or uid raises an error.
- **`linux.http.head`.** `url` must be a plain `http` or `https` URL (host
  name or bracketed IPv6, optional port, path and query). No user name or
  password, no `&`, no `#`, no quote, no space. `curl` sends one HEAD
  request (`curl -sS -I -g --proto '=http,https'`), follows no redirect and
  stops after 15 seconds. Any HTTP status gives `reachable: true`. A network
  failure (no DNS answer, refused, timeout, TLS) gives `reachable: false`
  with `curl_exit_code` and `error`. A missing `curl` or a request `curl`
  cannot make (exit 1 to 4) raises an error. `Set-Cookie` headers are
  removed, at most 64 headers are returned, and each value is cut at 512
  characters.
- **Approval for `linux.http.head`.** The URL is an address the host
  connects to. MEHO's address floor
  (`operations/caller_address.py`) therefore makes the op `caution` with
  approval, so a person approves each probe. The other two ops are `safe`.
  Disk usage is the existing `linux.fs.usage`.

### Path confinement

The operator-named paths (`file.read`, `log.tail`) are confined with the
bind9 `ensure_path_under_root` mold, generalised to an **allow-list of read
roots** (`LINUX_READ_ROOTS`): `/etc`, `/var/log`, `/var/lib`, `/run`. A path
must equal or descend one of these roots, or be one of a few **named single
files** (`LINUX_READ_FILES`), mostly under `/proc` and `/sys`:
`/proc/meminfo`, `/proc/partitions`, `/proc/cpuinfo`, `/proc/cmdline`,
`/proc/loadavg`, `/proc/uptime`, `/proc/version`,
`/sys/devices/system/cpu/online`, `/sys/firmware/efi`, the DMI identity
files (`product_uuid`, `sys_vendor`, `product_name`, under both
`/sys/class/dmi/id` and its real path `/sys/devices/virtual/dmi/id`), and
`/usr/lib/os-release` (the real path of `/etc/os-release` on most systemd
hosts). The rest of `/proc` and `/sys` is not readable; kernel parameters
have `linux.sysctl.read`.

`..` traversal is collapsed with `posixpath.normpath` and rejected if it
escapes, a trailing-slash sentinel stops `/etc-evil` matching `/etc`,
control bytes are refused, and a non-absolute path is rejected.

**Key and password files are refused** even under an allowed root
(`LINUX_READ_DENY`, `fnmatch` patterns on the full path): `/etc/shadow`,
`/etc/gshadow` and their `-` backups, `/etc/security/opasswd`, SSH host
private keys, `/etc/ssl/private`,
`/etc/pki/tls/private`, `/etc/letsencrypt`, kubeconfigs and PKI under
`/etc/kubernetes`, `/etc/rancher`, the RKE2 / K3s server tokens,
credentials, TLS and database under `/var/lib/rancher/*/server`, the
agent kubeconfigs under `/var/lib/rancher/*/agent`, `/var/lib/kubelet/pki`,
the kubelet kubeconfig, the pod volumes under
`/var/lib/kubelet/pods` (mounted Kubernetes Secrets and service account
tokens), the etcd data under `/var/lib/etcd`, the Docker container config
files (`/var/lib/docker/containers/*/config.v2.json`), NetworkManager
connection files, `/etc/wireguard`, `/etc/ipsec.secrets`,
`/etc/ipsec.d/private`, `/etc/sssd/sssd.conf`, `/var/lib/sss/secrets`,
`/run/secrets`, any `*.key`, `*.keytab`, `*.p12`, `*.pfx`, `id_rsa`, `id_dsa`,
`id_ecdsa`, `id_ed25519` or `.pgpass` file, every Ceph keyring (`*.keyring`
and the bare `keyring` file of a monitor or OSD directory), DNSSEC
`*.private` keys, the Postfix `sasl_passwd` file and its map, the cloud-init
user and vendor data (`*user-data*`, `*vendor-data*`, the combined
`cloud-config.txt`, `obj.pkl` under `/var/lib/cloud`, and
`instance-data-sensitive.json`), `wpa_supplicant` files, `htpasswd` files,
`.netrc`, every backup of
`shadow` and `gshadow` (`shadow-`, `shadow.bak`, `shadow~`), and every path
that contains `secret` or `credential`. The match ignores upper and lower
case. Public files next to them (`/etc/passwd`, `*.pub`, `/etc/ssl/certs`,
`/etc/ceph/ceph.conf`, `/var/log/secure`, RKE2 agent logs, pod logs under
`/var/log/pods`, Docker container logs) stay readable. A Ceph key inside a
readable file (`/etc/ceph/ceph.conf`, a log) is hidden by the `ceph_key`
redaction rule of the default policy (`redaction.md`).

The check runs twice:

1. On the typed path, **before any SSH command is constructed**. A refused
   path never reaches the host.
2. On the **path of the file the host opened**. The read is one command
   (`build_file_read_command` / `build_log_tail_command`): the shell opens
   the file once (`3<<path>`), prints the path the kernel reports for that
   open file (`readlink /proc/self/fd/3`, every symlink followed) as one
   line of hex digits (`od -An -v -tx1`) and its size, then reads from
   the same open file (`head -c` or `tail -n` on `<&3`). The hex line
   keeps a path with line breaks or marker text in it on one line, so
   such a path cannot pose as the size line, the marker or the content;
   the handler decodes it whole and refuses a path with a control
   character. The handler runs `confine_read_path` on the reported path and
   returns the content only when it passes. Path and content come from
   one open file, so a change of the path or of a parent directory after
   the open cannot swap in another file, and a file swapped in before the
   open is reported under its own path. So a symlink under `/var/lib`
   that points at `/root/...`, at a `/proc/<pid>` file or at
   `/etc/shadow` is refused. A file deleted while open (the kernel marks
   its path `(deleted)`), an error exit and an unclear answer (no
   reported path, more than one line, a path that is not clean hex) are
   refused too. A missing file
   gives `exists=false` (`file.read`) or no rows (`log.tail`); a file
   that exists but cannot be opened gives `exists=true` with no content.

Every refusal raises `PathConfinementError`. A root that is itself a
symlink on a host (for example `/var/log` pointing elsewhere) is refused
too, because the real path is outside the list. `file.read` caps content
itself (`head -c`, default 64 KiB, hard cap 1 MiB) with a `truncated`
flag, because a flat scalar dict is never JSONFlux-reduced. The default
redaction policy also hides private key blocks and password hashes in
any result (`docs/codebase/redaction.md`).

### Injection safety

Every operator-supplied value is quoted into a **fixed** command
(`shlex.quote` for paths, units and keys; explicit single quotes for the
DNS `name`, whose charset excludes the quote character). `unit`, `key` and
`name` are additionally re-validated against a strict charset before
command construction (the schema `pattern` is advisory, the handler
re-check authoritative — the proxmox method-allow-list mold); the charset
admits no shell metacharacters, so quoting is defence in depth.
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

The multi-step read `linux.migration.inventory` (group `migration`,
`safe`) returns the host as one server record in the common migration
inventory shape. It only calls `linux.about` and `linux.file.read`. It lives
in `connectors/inventory/source_hosts.py`; see `connectors-inventory.md`.

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
| `linux.script.run` | `exec` | `dangerous` + approval | `script`, `interpreter?`, `arguments?`, `working_directory?`, `env?`, `secret_env?`, `timeout_seconds?`, `use_sudo?`, `script_ref?`, `expected_sha256?` | `{stdout, total, stderr, exit_code, interpreter, used_sudo}` |
| `linux.sysctl.write` | `system` | `dangerous` + approval | `key`, `value` | `{key, value, applied, dropin_path}` |
| `linux.firewall.load` | `firewall` | `dangerous` + approval | `ruleset`, `backend?`, `rollback_after_seconds?` | `{applied, backend, validated}`, plus `{rollback_id, rollback_due_at}` with the timer |
| `linux.firewall.confirm` | `firewall` | `caution` + approval | `rollback_id`, `install_path?` | `{confirmed, rollback_id, installed, install_path}` |

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

### Firewall changes with a timed way back

A rule with good syntax can still cut the SSH path to the host. Then only
a console login can fix it. So `linux.firewall.load` takes an optional
`rollback_after_seconds` (60 to 1800). Without it, the load works as
before. With it, one root script (`ops_firewall_rollback.py`) runs these
steps in this order:

1. Take the host lock (`flock` on `/var/lib/meho/firewall-rollback/.lock`).
   Refuse when another timed change still waits for its confirm or its
   timer. Only one timed change can wait at a time. To tell, the script
   asks systemd whether the other timer still waits or its restore runs,
   and it also checks the saved end time. So a change of the host clock
   does not let a second change start.
2. Save the live rules to `/var/lib/meho/firewall-rollback/<rollback_id>/`
   (a root-only folder) and check the syntax of the saved file. The nft
   copy starts with `flush ruleset`, so loading it replaces the rules. For
   iptables, each table that the new rules use but the live rules do not
   goes into the saved copy as an empty table. So loading the saved copy
   also clears a table that the new rules added.
3. Check the syntax of the new rules.
4. Start a timer with `systemd-run --on-active=<seconds>`. When it ends, it
   takes the lock, loads the saved rules and writes a `restored` mark.
   It waits for the lock as long as a load or a confirm holds it, and it
   loads nothing without the lock.
5. Load the new rules.

If the save or the timer fails, the script stops before any change and the
call is an error. A host without systemd or `flock` is refused. If the load
itself fails (iptables can stop after some tables are in), the script loads
the saved rules at once. Only when that works does it stop the timer. If it
does not work, the timer stays and tries again when it ends. The answer
adds `rollback_id` and `rollback_due_at` (UTC, from the host clock).

`linux.firewall.confirm` keeps the new rules:

- It opens a **new** SSH connection, not one from the pool. A pooled
  connection stays open under the new rules, so it proves nothing.
- It holds the same lock as the load and the timer, so it never runs at
  the same time as the restore.
- It refuses an unknown `rollback_id`, a timer that already ran, and a
  timer that ends in less than 20 seconds. It checks the time left again
  right before it stops the timer. The error says whether the old rules
  are back or coming back.
- It writes a `keep` mark, then stops the timer. The restore does nothing
  when it finds `keep`, so a restore that started just before the stop
  cannot undo the confirm. With `install_path` it first checks the new
  rules, then writes them to that file, so they survive a reboot. The path
  must be inside the write roots of `linux.file.write`. An existing file
  keeps its mode.

`linux.firewall.confirm` is `caution` with `requires_approval=true`. The
operation contract check asks every operation that is not `safe` to wait
for approval, and a call made with a token waits for a person's approval
anyway. So a person approves the confirm too. Pick a timer long enough for
the checks and for that approval. If the approval comes too late, the
confirm refuses and the old rules come back by themselves.

### File mode and owner (`file.write`)

`file.write` writes the new content to a temp file in the same folder,
then renames it over the old file. The temp file starts as `0600`. When the
file already exists, the script copies the old file's mode and owner onto
the temp file before the rename. So a `0644 root:root` file stays
`0644 root:root`, and a daemon that reads it as a non-root user can still
read it. If the copy fails, the script stops before the rename: the old
file stays as it was and the op returns `written: false`. A new file is
created `0600` and owned by root. If a non-root service must read a new
file, change its mode afterwards.

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
  `env`) can carry secret material in their params, so they are declared
  `credential_write` in `linux/op_classes.py` —
  the broadcast collapses their params to aggregate-only — and
  `preview_operation` returns `preview_unavailable` for them (the
  credential-class exclusion in `_is_previewable`), because the request-time
  preview's `redacted_body` slot cannot scrub a structured secret. Their
  reviewer preview is instead a **bespoke park-time `proposed_effect`**
  (`register_preview_builder`, fail-soft). For `file.write` it echoes only
  shape — path + content byte size + backup path + validate command, never
  the `content`. For `script.run` it shows interpreter + argument byte size +
  env-var **names** + the `secret_env` **reference mapping**
  (`ENV → <vault-path>#<field>`, the non-secret Vault pointers) + working
  directory + sudo intent + timeout, and **the script itself** (see "What the
  approver of `script.run` reads" below) — never the argument values / env
  values / resolved `secret_env` values.
- `linux.sysctl.write` and `linux.firewall.load` carry no secret, so they
  register **no** bespoke builder and stay previewable via
  `preview_operation` on the generic params-echo default. For
  `firewall.load` this is the point: the reviewer sees the full ruleset
  before approving.

### What the approver of `script.run` reads

For `script.run` the script body is the change that waits for approval, so
the approver reads it. `linux/script_preview.py` builds these preview fields:

| Field | What it holds |
|---|---|
| `script` | The script text. Values that look like secrets are replaced by a marker, in two passes. First the shell-aware command-line hider that the `k8s.job.create` preview uses (`redaction/command_line.py`, `hide_shell_script`): the value of a `NAME=VALUE` whose name holds a secret word (`PGPASSWORD=...`, `export AWS_SECRET_ACCESS_KEY=...`), the value after a secret flag (`--password x`), the password in `curl -u user:password` and in a URL, tokens with a known vendor prefix. In a script, `-p` is a secret flag only for `sshpass`, `mysql*`, `mariadb*`, `mongo*` (`mongosh`, `mongodump`) and a command with the word `login` (`docker login`, `podman login`, `helm registry login`), also with the value glued (`mysql -pVALUE`), so `mkdir -p`, `cp -p`, `ssh -p 22` and `docker run -p 8080:80` stay as they are. This list is narrower than the `k8s.job.create` rule, where `-p` is a secret flag after any program. A password glued to `-u` (`curl -uuser:password`) is hidden in a script too. A new line ends a command, but a line continuation (a `\` at the end of a line) does not: the value of a flag may be on the next line. Then the packaged default redaction policy (`docs/codebase/redaction.md`): a bearer token, a JWT, a private key block, a quoted `"password": "..."` pair. Any change counts as hidden. Cut at 64 KiB. |
| `script_bytes` | The size of the full script in UTF-8 bytes. |
| `script_sha256` | The SHA-256 of the full script as it runs, to compare with a reviewed copy. Left out when values were hidden: with the rest of the text in view, the hash would let a reader test guesses of the hidden value. |
| `script_values_hidden`, `script_truncated` | `true` when the shown text differs from the script that runs. |
| `script_unavailable` | Set, with no text and no hash, when the redaction pass fails. |
| `script_ref` | The optional `script_ref` param: where the reviewed copy lives. |
| `script_check` | `matches` or `does not match`, when the caller sent the optional `expected_sha256` param. |

When `expected_sha256` is set and the script does not match it, the handler
refuses to run the script. The preview lives only in the approval row's
`proposed_effect`; the audit row keeps only the params hash, and the
broadcast stays aggregate-only for this `credential_write` op.

The console approval modal shows the text as code in a "Script to run"
block (`ui/templates/approvals/_script.html`), with the hash, the reviewed
copy and the check. `meho approvals show` and the `meho approvals approve`
prompt print the same block, one line per script line
(`cli/internal/cmd/approvals/script.go`).

### Approval + secret hygiene

No approval logic lives in any handler: the dispatcher parks a `dangerous`
+ `requires_approval` op and the handler body runs only on the
`_approved=True` resume path. Approval is a human-only decision — there is
no MCP decision path for it. No op declares a `password` / `secret`
parameter. Every operator-supplied value is `shlex.quote`d or base64-carried
into a fixed command, and `file.write` confines its `path` under its
own write-root allow-list (`/etc`, `/var/lib`, `/run`, `/opt`, `/srv`,
`/usr/local`) — `/proc` / `/sys` (the sysctl op's job) and `/var/log` are
deliberately not writable here.

**Operator rule (shared-machinery caveat):** do not put a bare secret in
`content` / `arguments` / `env`. `ApprovalRequest.params` stores the params
verbatim so the approved call can be re-dispatched, so a secret placed
there is durable on the approval row (the same rule as the guest
`program.run` / `file.write` verbs). For `script.run`, pass the credential by
reference via `secret_env` (below) — the backplane resolves it server-side at
run time, so the value never touches the params.

### Server-side secret injection (`secret_env`)

`script.run` is `safety_level=dangerous`, `requires_approval=True`, and pinned
`credential_write`: its `script` / `arguments` / `env` are base64-carried on
the wire but stored **verbatim** in `ApprovalRequest.params` (needed to
re-dispatch the exact call on approval) and in the audit params. That is the
right design for re-dispatch, but it means a script cannot safely *receive* a
credential through `arguments` / `env` — any secret placed there is durable on
the approval row and in audit. The only server-side secret resolution the
connector otherwise performs is the sudo password (`_resolve_sudo_password`,
read from the target's own Vault secret) and the SSH auth material.

The anti-pattern this replaces: passing a non-secret Vault *path* as an
argument and resolving it **on the host** — which requires a Vault CLI and a
Vault token to be present on the target (a live credential parked on a third
host), exactly the un-governed footgun this connector exists to retire.

`secret_env` closes that gap. It is an optional object mapping
`ENV_NAME → "<vault-path>#<field>"`:

- **Reference shape.** `<vault-path>` is a logical KV-v2 path under the
  `secret` mount — the same convention `load_vault_secret_data` accepts for the
  connector (**not** an API-path-shaped `secret/data/…`). The path must sit
  under the caller's tenant path (`tenants/<tenant_id>/` with the default
  `VAULT_KV_TENANT_SCOPE_PREFIX`); `check_param_secret_ref` refuses any
  other path before the Vault read, and the call answers
  `vault_credentials_read_error`. `#<field>` names the field within that
  secret. Each
  `ENV_NAME` must be a valid POSIX identifier, must not start with a digit,
  must not be a reserved name (see "Reserved env names" below) or a name
  already declared in `env`, and the mapping is bounded (≤ 32 entries).
- **Checked before the call waits for approval.** The `secret_env` schema
  carries `propertyNames` (the POSIX name pattern and a `not` rule for the
  reserved names) and `maxProperties: 32`.
  The dispatcher checks the schema before the policy gate, so a bad or
  reserved name or too many entries fail at once with `invalid_params`, and
  no approval is asked for. The pattern ends with `(?![\s\S])`, not `$`, so a
  name with a trailing newline also fails here. The handler
  (`_validate_secret_env`) still checks every rule at run time. The clash
  with `env` and the reference shape are checked only there.
- **Resolution at execution time only.** The value is resolved when the handler
  runs — the `_approved=True` resume path (or a non-parking dispatch) — never at
  park time. Each field is read through the connector's single Vault seam
  (`_resolve_secret` → `load_vault_secret_data`, reused, not forked) under the
  **dispatching operator's** identity / tenant scope — the same scoping the
  sudo-password resolution uses — then injected into the remote wrapper as an
  env var exactly like `env` (base64-carried, `umask 077` temp handling).
  Reads are de-duplicated per path.
- **Never persisted.** `ApprovalRequest.params`, the audit params, and the
  broadcast payload carry only the `ENV → <vault-path>#<field>` mapping; the
  resolved value never enters params, the `OperationResult`, the park-time
  preview, or a log line. The park-time bespoke preview echoes the reference
  mapping (a non-secret pointer) so the reviewer can see *which* credential each
  env var will receive, but no value exists at park to leak.
- **Fail closed.** A missing field, a forbidden / API-path-shaped path, or any
  Vault resolution error raises a structured error **before** any remote
  command is built or run (no partial execution). The error names the `ENV`
  and the `<vault-path>#<field>` reference — never a resolved value.

`secret_env` is `script.run`-scoped; the connector has no `command.run` verb
to extend it to.

### Reserved env names (`env` and `secret_env`)

The approver of `script.run` sees the script and the env **names**, not the
env values. So an env value must not change which code runs. `env` and
`secret_env` refuse the same reserved names:

- the wrapper's own shell variables: `tmp`, `args`, `f`;
- names that control how a shell starts or splits words: `ENV`,
  `SHELLOPTS`, `BASHOPTS`, `PS4`, `IFS`, `GLOBIGNORE`, `CDPATH`,
  `PROMPT_COMMAND`, `ZDOTDIR`, `HOME`;
- bash variables whose assigned value bash evaluates as arithmetic:
  `HISTCMD`, `OPTIND`, `RANDOM`, `SRANDOM`. An arithmetic value can run a
  command, so the wrapper's own `export` line would run it;
- names that control where commands and libraries are found: `PATH`,
  `GCONV_PATH`;
- Java start options: `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS`,
  `_JAVA_OPTIONS`;
- every name that starts with `BASH_` (for example `BASH_ENV`), `LD_` (the
  dynamic loader, for example `LD_PRELOAD`), `PYTHON`, `PERL`, `RUBY` or
  `NODE_`.

Names are case-sensitive, so `TMP` or `bash_env` stay allowed. Both schemas
carry the list under `propertyNames` → `not`, so the dispatcher refuses a
reserved name with `invalid_params` before the call waits for approval. The
handler (`_check_env_name`, used by `_validate_env` and
`_validate_secret_env`) checks the same list again at run time. The list
lives in `_RESERVED_ENV_NAMES` and `_RESERVED_ENV_PREFIXES` in
`connectors/linux/ops_write.py`. A script that needs one of these names sets
it in its own body, where the approver reads it.

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
  `LINUX_READ_FILES` / `LINUX_READ_DENY` / `PathConfinementError` the
  confinement primitives.
- Per-domain op modules: `ops_file.py` (file.read / log.tail),
  `ops_host.py` (service.status / sysctl.read / dns.lookup),
  `ops_diagnose.py` (tool.which / identity.show / http.head),
  `ops_firewall.py` (firewall.show), `ops_storage.py` (mount.list) — each
  ships its handler, command builder, output parser, and `LinuxOp` rows.
- `ops_firewall_rollback.py` — the timed load of `firewall.load` and the
  `firewall.confirm` op (`FIREWALL_ROLLBACK_OPS`).
- `ops_write.py` — the five write verbs (`WRITE_OPS`), `LINUX_WRITE_ROOTS`
  / `confine_write_path`, the `secret_env` checks and the two park-time
  preview builders (`register_linux_write_previews`).
- `_sudo.py` — `run_remote_bash_with_sudo`, the root-elevation primitive.
- `op_classes.py` — declares `linux.file.write` and `linux.script.run` as
  `credential_write` for the broadcast classifier.

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

- **`connectors-vmware-rest-guest-ops.md`** (in the private connectors repository) still names tier (b) as
  deferred; that pointer is updated to reference this built connector as
  part of the initiative wrap-up (T3), not T1.
- **Host-key verification** is inherited from the base adapter, which is
  now fail-closed (#270): a Linux target's Vault secret must carry a
  `known_hosts` pin (or the audited `known_hosts_insecure` opt-out) or
  dispatch is refused before connecting. This connector sets no host-key
  policy of its own beyond that inherited default.
- **The deny list of `file.read` and `log.tail` is a list of names.** A
  secret file with a name that is on no pattern (no `secret`, `credential`,
  `key` or `keyring` in it) under an allowed root is still readable. An
  allow list with approval for everything else would be the stronger
  design; it is not built.

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
`connectors-vmware-rest-guest-ops.md`, in the private connectors repository),
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

Steps 1–6 use six T1 read verbs of this connector; step 7 is a
functional probe on the `net` diagnostics connector, **not** a Linux verb.
To check name lookup from the host's own side, `linux.dns.lookup` asks
the host's resolver (§ *DNS lookup on the host*). The `net` probe
destinations must be inside
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
