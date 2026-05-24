# Connector: bind9 (bind9-9.x / `bind9-ssh`)

## Overview

The `bind9` connector is the typed `Connector` subclass that dispatches
operator-facing ISC bind9 operations over SSH. It is registered under
the `(product="bind9", version="9.x", impl_id="bind9-ssh")` registry
triple and is the first tier-1 child of the `SshConnector` adapter
(G0.2-T4 #243). The `9.x` version range covers ISC bind9 9.18
(current Extended Support Version) and 9.20 (current Stable Release);
both expose `named -v` and `named-checkconf -p` with the same flags
and the same banner format.

The connector replaces the operator's `scripts/bind9-dns.sh` wrapper
(~700 LoC, the heaviest SSH wrapper in the inventory). The G3.4-T1
(#587) skeleton ships only the `bind9.about` canary op, the safe-sudo
primitive, the fingerprint, and the probe; T2 (#588) adds the read
op group, T3 (#589) adds the atomic-apply primitive plus
`bind9.record.add` / `bind9.record.remove`, T4 (#590) adds the
config-write group, and T5 (#591) ships the CLI verbs + E2E
acceptance suite + onboarding doc.

Source: `backend/src/meho_backplane/connectors/bind9/`.

## Key types

- **`Bind9Connector`** (`connector.py`) -- `SshConnector` subclass.
  Class attributes: `product="bind9"`, `version="9.x"`,
  `impl_id="bind9-ssh"`. Inherits the per-target asyncssh connection
  pool, the key-or-password auth selection, and `aclose()` from the
  adapter; overrides `fingerprint`, `probe`, `execute`, and adds the
  `about` op handler.
- **`_remote_bash_with_sudo()`** -- the load-bearing safety
  primitive. The only sudo-shell-construction path in the connector
  layer. The constructed remote argv is the fixed string
  `"sudo -S -p '' bash -s"`; the sudo password is streamed via
  `input=` (stdin) as the first line, and the bash script body is
  streamed as the remainder. The caller passes `script` and
  `sudo_password` as separate arguments and cannot express a
  mis-ordered payload because the helper builds the stdin string
  itself. The password never appears in the remote argv, the remote
  shell-history file, or any local structured-log event.
- **Op metadata** (`ops.py`) -- the `Bind9Op` dataclass plus the
  `BIND9_OPS` tuple the connector's `register_operations` walks at
  startup. T1 shipped a single-element tuple with `bind9.about`;
  T2 added the read op group via a `_bind9_ops()` composition
  function (mirrors K8s `_kubernetes_ops()`) that splats per-area
  tuples from `ops_zone.py`, `ops_record.py`, and `ops_config.py`.
  T4 will follow the same shape.
- **`ops_zone.py`** -- T2 zone-read ops module. Pure parsers
  (`parse_named_checkconf_zones`, `parse_zonefile`), bound-method
  handler functions (`bind9_zone_list`, `bind9_zone_read`), and the
  `ZONE_OPS` registration table.
- **`ops_record.py`** -- T2 record-read + T3 record-write op module.
  Pure parser (`parse_dig_answer`), read handler (`bind9_record_get`),
  T3 (#589) write handlers (`bind9_record_add`, `bind9_record_remove`)
  with longest-suffix zone resolution + pure dnspython zonefile
  transforms, and the `RECORD_OPS` registration table.
- **`_atomic.py`** -- T3 (#589) atomic-apply primitive. One async
  `atomic_apply()` helper that runs the seven-step bash pipeline
  (snapshot, capture state_before, stage, validate, rndc reload,
  caller-supplied verify predicate, on-failure snapshot rollback)
  in a single `_remote_bash_with_sudo` invocation. T4 (#590)
  generalised the validate step from hardcoded `named-checkzone` to
  a caller-supplied `BIND9_VALIDATE_CMD` env var (default still
  `named-checkzone "$ZONE_NAME" "$AUDIT_SLICE_PATH"` for record
  writes) and added a multi-file tar staging shape
  (`staged_tar_bytes`) so `config.apply_views` can deposit a whole
  views subtree atomically. Returns `AtomicApplyResult(state_before,
  state_after, audit_slice_path)`; raises `AtomicApplyError(step,
  detail)` on any failure -- by the time the helper returns, the
  remote `/etc/bind/` tree is either fully-staged-and-verified or
  byte-identical to the pre-op snapshot.
- **`ops_config.py`** -- T2 config-read + T4 config-write op module.
  Pure path-safety filter (`ensure_path_under_root`), read handler
  (`bind9_config_show`), and the four T4 write handlers:
  `bind9_config_apply_file` (single-fragment write, atomic-apply
  single-file mode), `bind9_config_apply_views` (multi-file tree
  write, atomic-apply tar mode -- `primary_path` is validated to
  reference one of the staged files so the primitive's success-path
  audit-slice capture cannot hit a missing file after a successful
  reload, which would otherwise force a double-apply on retry),
  `bind9_config_backup` (`tar -czf` of `/etc/bind/` to
  `/var/backups/meho-bind9/<timestamp>[-<tag>]-<hex>.tar.gz` with a
  JSON listing of existing backups; the 24-bit hex suffix breaks
  same-second + same-tag collisions), and `bind9_config_reload`
  (`rndc reload` with structured success/failure envelope). The
  pure `pack_views_tar` helper builds the multi-file archive
  client-side. The `CONFIG_OPS` registration table carries all five
  ops.
- **`parse_named_version()`** / **`parse_os_release()`**
  (`connector.py`) -- pure helpers. `parse_named_version` recovers
  the `<X.Y.Z>` version triple from a `BIND <X.Y.Z>-<distro-suffix>`
  banner; `parse_os_release` reads the `ID` and `VERSION_ID` keys
  from `/etc/os-release`'s key=value content and returns
  `"<id> <version_id>"` (e.g. `"debian 12"`). Pure-function shapes
  keep the unit suite assertions tight without booting any IO.

## Safe-sudo primitive (`_remote_bash_with_sudo`)

The primitive is **the only way to invoke sudo** in the connector
layer. The wrapper this connector replaces leaked the sudo password
into the remote shell-history file twice in seven days (2026-05-04
and 2026-05-05) because its `remote_bash()` shape let callers
construct the heredoc themselves and put the password line in the
wrong position. The replacement encodes safety by construction:

| Property | How the API enforces it |
| -------- | ----------------------- |
| Password not in remote argv | Constructed argv is a fixed constant (`sudo -S -p '' bash -s`); the password streams via `input=` |
| Password not in shell history | `bash -s` reads from stdin; commands fed on stdin are not recorded |
| Password not in local logs | The structured log event binds `cmd_len` / `script_len` / `exit_code` only |
| Caller cannot mis-order the payload | `script` and `sudo_password` are separate arguments; the helper assembles the stdin string |
| `sudo_password` cannot be passed positionally | The parameter is keyword-only (signature uses `*,` separator) |

The primitive is mandatory for every sudo-requiring op. T3 (#589)'s
atomic-apply primitive (`_atomic.py`) layers on top of it -- the
whole seven-step pipeline (snapshot, stage, validate, reload,
verify, rollback) runs as one bash body fed to
`_remote_bash_with_sudo`, so the snapshot and the rollback always
share an interpreter on the target. T4 (#590)'s
`bind9.config.apply_file` / `bind9.config.apply_views` route through
the same atomic-apply primitive; `bind9.config.backup` and
`bind9.config.reload` bypass the atomic shape (additive / single
command) but still route their sudo through this helper.

## `fingerprint(target)`

Two `_run_command` calls in sequence (the SSH adapter's pool ensures
both share one connection):

1. `named -v` -- the BIND version banner, e.g.
   `BIND 9.18.24-1+deb12u2-Debian (Extended Support Version) <id:>`.
   The `<X.Y.Z>` triple parsed via `parse_named_version` lands in
   `FingerprintResult.version`; the full banner lands in
   `FingerprintResult.build`.
2. `cat /etc/os-release` -- key=value file parsed via
   `parse_os_release` and surfaced under `extras["os"]`. Falls back
   to `cat /etc/debian_version` when `/etc/os-release` is missing
   (older Debian releases); the bare version string from
   `/etc/debian_version` is prefixed with `"debian "` (note the
   trailing space) for consistency with the os-release shape.

`probe_method` is the fixed string `"ssh: named -v"`.
`extras["named_conf_path"]` carries the Debian-family default
(`/etc/bind/named.conf`); RHEL-family detection lands in a follow-up
once T2 ships `bind9.config.show`.

The `_run_command` calls are wrapped in a `(OSError, asyncssh.Error)`
guard so a connection drop, an `asyncssh.Error`, or a timeout
mid-fingerprint returns `reachable=False` + `extras["error"]` rather than
propagating (mirrors the pfsense sibling). This is what lets the shared
`SshConnector._assert_reachable` guard surface the failure consistently
from `about` (#986).

## `probe(target)`

Five distinct failure-reason values:

| Reason | Trigger |
| ------ | ------- |
| `tcp_unreachable` | `OSError` raised by `asyncssh.connect()` (host down, firewall, wrong port) |
| `ssh_handshake_failed` | `asyncssh.DisconnectError` (host-key mismatch under a future pinning regime, protocol mismatch) |
| `auth_failed` | `asyncssh.PermissionDenied` (credentials rejected) |
| `named_not_running` | `pgrep -x named` exited non-zero |
| `named_config_invalid` | `named-checkconf -p > /dev/null` exited non-zero |
| `command_failed` | a post-connect command (`pgrep` / `named-checkconf`) raised after a successful connect (drop / `asyncssh.Error` / timeout) |

The post-connect commands are wrapped in a `(OSError, asyncssh.Error)`
guard so a mid-probe failure maps to `command_failed` rather than escaping
`probe()` as an unhandled exception (#986). `TimeoutError` is an `OSError`
subclass, so the command-timeout case is covered by the same tuple.

`about` reuses `fingerprint` and calls the shared
`SshConnector._assert_reachable(result)` guard, which raises
`ConnectorUnreachableError` when the fingerprint is not reachable; the
dispatcher maps it to a `connector_error` `OperationResult`
(`status="error"`) rather than reporting empty identity fields as a
successful op (#986).

The probe is read-only and does not require a writable filesystem.
`named-checkconf -p` parses the active config and emits its
canonicalised form on stdout (which we discard); the exit code is
the parse-success signal. Ordering of the exception clauses puts
the most-specific class first (`PermissionDenied` is a subclass of
`DisconnectError` in asyncssh) so the dispatch maps to the right
reason.

## Shipped op surface (T1 + T2 + T3 + T4 = 11 ops)

| Op id | Handler | Safety | Description |
| ----- | ------- | ------ | ----------- |
| `bind9.about` | `Bind9Connector.about` | `safe` | Operator-facing wrapper around fingerprint; returns vendor / product / version / build / os / named_conf_path |
| `bind9.zone.list` | `Bind9Connector.bind9_zone_list` | `safe` | Parse `named-checkconf -p` into zone rows: `{name, file, type}` per declared zone |
| `bind9.zone.read` | `Bind9Connector.bind9_zone_read` | `safe` | Resolve zonefile via `named-checkconf -p`, read + parse via dnspython; row per rrset member `{name, ttl, class, type, rdata}` |
| `bind9.record.get` | `Bind9Connector.bind9_record_get` | `safe` | `dig @localhost <fqdn> <type>` parsed into structured rows; defaults to A; supports A / AAAA / CNAME / MX / TXT |
| `bind9.record.add` | `Bind9Connector.bind9_record_add` | `caution` | Atomic A/AAAA record write with snapshot rollback; resolves owning zone via longest-suffix match when `zone` omitted; verify predicate = `dig` returns the new IP |
| `bind9.record.remove` | `Bind9Connector.bind9_record_remove` | `caution` | Atomic remove of A + AAAA at the given FQDN with snapshot rollback; verify predicate = `dig` no longer resolves the FQDN |
| `bind9.config.show` | `Bind9Connector.bind9_config_show` | `safe` | Read named.conf or an included fragment under the bind config root; path-safety filter refuses traversal with no content leaked |
| `bind9.config.apply_file` | `Bind9Connector.bind9_config_apply_file` | `dangerous` | Atomic single-fragment write via T3's primitive; validate = `named-checkconf -p`; verify = config still parses after live reload |
| `bind9.config.apply_views` | `Bind9Connector.bind9_config_apply_views` | `dangerous` | Atomic multi-file tree write (tar mode of T3's primitive); validate = `named-checkconf -p`; verify = caller-supplied `dig` or fallback parse check |
| `bind9.config.backup` | `Bind9Connector.bind9_config_backup` | `caution` | `tar -czf` of `/etc/bind/` under `/var/backups/meho-bind9/`; returns backup ID + listing of existing backups |
| `bind9.config.reload` | `Bind9Connector.bind9_config_reload` | `caution` | `rndc reload` with structured success/failure envelope; captures `rndc status` before/after for audit |

The `dangerous` safety tier is reserved for T4's apply ops -- a bad
views file can dark the whole resolver. The production-path gate
(G7/G10) keys on `safety_level`, so the apply ops carry an additional
warning in their `description` + `llm_instructions` that any agent
proposing the write sees.

## Atomic-apply primitive (T3 #589)

`_atomic.py` exposes one async `atomic_apply()` helper used by every
mutating op (record-writes today, config-writes when T4 lands). The
discipline is the load-bearing safety contract Initiative #367 calls
WI5 -- DNS is global; a half-applied zone change wedges every
consumer of this nameserver. The primitive runs a fixed seven-step
pipeline inside one `_remote_bash_with_sudo` invocation:

1. **Snapshot.** `tar -czf` the bind config root to a per-call
   `/tmp/meho-bind9-snapshot-<token>.tar.gz`. Captures every file
   under the bind root, not just the affected zonefile -- callbacks
   may transitively edit `named.conf.local` or fragment files.
2. **Capture state_before.** `cat` the caller-supplied audit-slice
   path with sentinel framing so the Python side can split the
   slice content out of the script's progress output.
3. **Stage.** Write the caller's `staged_bytes` to the audit-slice
   path. Staged bytes arrive base64-encoded via env var so arbitrary
   bytes (control characters, embedded NULs) round-trip unchanged.
4. **Validate.** `named-checkzone <zone> <file>` against the staged
   file. Non-zero exit -> rollback + raise `AtomicApplyError("checkconf",
   detail)`. (Per-zone, not the parent-config-tree `named-checkconf -p`,
   so the validation gate matches the blast-radius of the operator's
   change.)
5. **Reload.** `rndc reload`. Non-zero exit -> rollback + raise
   `AtomicApplyError("reload", detail)`.
6. **Verify.** Run the caller-supplied verify command (e.g.
   `dig @localhost <fqdn> +short A | grep -qxF <expected-ip>`).
   Non-zero exit -> rollback + raise `AtomicApplyError("verify",
   detail)`. The rollback restores the pre-op tree and reloads named
   back to it, so the operator-visible state post-rollback is
   identical to pre-op.
7. **Success.** Capture state_after; delete the snapshot tar.

The integration suite asserts byte-identical-tree rollback via
`find /etc/bind -type f | xargs sha256sum | sha256sum` before / after
each failure scenario (checkzone-fail + verify-fail both covered).

### Why one bash script vs N round-trips

A 7-call pipeline would pay sudo's TTY-less-cache cost seven times,
write seven structured-log rows, and could leave the staged file in
place if the network blipped between stage and validate. Bundling
into one remote bash keeps the pipeline atomic from the target's
POV; the bash body is generated entirely by `_atomic.py` (no
caller-supplied substring lands in shell), so the safe-sudo
helper's invariants extend uninjured.

### Zone resolution when `--zone` is omitted

`bind9.record.add` / `bind9.record.remove` accept an optional
`zone` parameter. When omitted, the handler resolves the owning
zone from `named-checkconf -p` (T2's zone parser) by longest-suffix
match against the FQDN:

* The FQDN's label sequence must end with the zone's label sequence
  -- label boundaries are atomic, so `api.evba.lab` matches
  `evba.lab` but not `ba.lab`.
* The root zone (`.`) is excluded from candidates.
* On a tie at the longest suffix, raises `ZoneResolutionError(reason="ambiguous", candidates=[...])`.
* On no match, raises `ZoneResolutionError(reason="unresolvable", fqdn=...)`.

Both errors raise **before** any staging, so the dispatcher's
`invalid_params` envelope reports the rejection with zero side
effects on the remote tree.

### Audit integration

Both write handlers emit `op_class="write"` plus
`result_state_before` / `result_state_after` (the full pre/post-op
zonefile text captured by `atomic_apply`). These power the G8.2
audit-replay path; the broadcast classifier's `_WRITE_SUFFIXES`
tuple is extended to include `.add` / `.remove` so a future
broadcast leak via `classify_op` falling through to `other` cannot
surface the rdata.

## Read op group (T2 #588)

### Pure parsers vs handler thin layer

Each read handler is a thin SSH-call + parse + shape layer over a
pure parser that takes captured stdout / file text. The unit suite
pins the parsers directly without booting an event loop:

| Parser | Source module | Input | Output |
| ------ | ------------- | ----- | ------ |
| `parse_named_checkconf_zones` | `ops_zone.py` | `named-checkconf -p` stdout | list of `{name, file, type}` rows |
| `parse_zonefile` | `ops_zone.py` | zonefile text + origin | list of `{name, ttl, class, type, rdata}` rows via `dns.zone.from_text` |
| `parse_dig_answer` | `ops_record.py` | `dig` stdout (`+noall +answer` or default) | list of `{name, ttl, class, type, rdata}` rows |
| `ensure_path_under_root` | `ops_config.py` | requested path + allowed root | canonical absolute path under root, or `ConfigPathRejectedError` |

### JSONFlux handle pattern -- deferred to the reducer

Issue #588's acceptance language ("JSONFlux handle when the parsed
record list exceeds ~20 rows / 4 KB") was patterned on Issue #322's
identical clause for the K8s connector. The K8s landing
(`ops_core.py`) deliberately did **not** implement per-handler
threshold logic; the bind9 read group adopts the same posture and the
rationale is documented in
[`ops_zone.py`'s module docstring](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py):

* `OperationResult.handle` is populated by the dispatcher's reducer
  slot, not by individual connectors.
* The G3.1-T4 (#304) `HandleStore` Task was closed as superseded; no
  shared substrate exists today that per-handler emission could
  delegate to.
* Coupling every connector to the reducer's threshold calibration
  doubles the spill-path implementation and locks the threshold at
  the connector boundary.

The handlers ship raw row lists plus a `total` count so a future
JSONFlux reducer has the inlined-sample-size + total-count signals to
drive its threshold check. The `bind9.zone.read` `llm_instructions`
mention the future handle-wrapping behaviour so the agent already
expects a handle when the reducer ships.

### Path-safety filter (`bind9.config.show`)

`ensure_path_under_root` is the load-bearing safety primitive for
`bind9.config.show`. It encodes the scoping the consumer wrapper's
hand-coded paths only achieved by convention:

* The handler reads the bind config root from
  `fingerprint().extras["named_conf_path"]` (the *directory* of the
  fingerprint's named.conf path -- Debian default `/etc/bind/`).
* The filter accepts absolute paths lexically under the root, and
  relative paths that resolve under the root after
  `posixpath.normpath` collapses `..`/`.` segments.
* Rejections raise `ConfigPathRejectedError` (a `ValueError`
  subclass) before any wire IO, so the dispatcher's
  `connector_error` envelope carries no file content. The
  integration test asserts this explicitly against a real container.
* Control characters (`\n`, `\r`, `\x00`) in the requested path are
  rejected outright -- the path goes through `shlex.quote` before
  the SSH command line, but a NUL/newline would survive quoting in
  some shells and is easier to refuse at the API boundary.

The check is lexical, not realpath -- a `realpath` resolution would
double the wire cost (a second SSH round-trip), and the threat model
is operator-typed paths in agent prompts (not a hostile operator
placing a symlink inside `/etc/bind/`). The lexical check rejects
`../` ladders and absolute paths outside the root, which is the
right granularity.

## Registration

Two phases, mirroring the `connectors/vault/__init__.py` and
`connectors/kubernetes/__init__.py` patterns:

1. **Synchronous (import time)** -- `register_connector_v2(product="bind9",
   version="9.x", impl_id="bind9-ssh", cls=Bind9Connector)` runs at
   `connectors/bind9/__init__.py` import time.
2. **Asynchronous (lifespan startup)** --
   `register_typed_op_registrar(register_bind9_typed_operations)`
   queues the typed-op upsert onto the lifespan-driven registrar
   list; `run_typed_op_registrars` invokes it after
   `_eager_import_connectors` has walked every connector subpackage.

The connector does **not** call the v1 `register_connector` because
bind9 has no v1 chassis history -- the deprecated chassis route was
removed by G0.6-T11 (#412) and bind9 has never shipped behind it.

## Tests

- `backend/tests/test_connectors_bind9.py` -- T1 unit suite. Covers the
  registry-v2 class attrs, the package-import registration shape, the
  parse helpers, the safe-sudo primitive's argv / stdin / log-shape
  contracts, the fingerprint version-and-OS parsing, the five probe
  reason values, the register_operations upsert + idempotency loop,
  and the execute() dispatcher shim's unknown / valid / invalid
  branches.
- `backend/tests/test_connectors_bind9_reads.py` -- T2 unit suite.
  Pure-parser tests for `parse_named_checkconf_zones`,
  `parse_zonefile`, `parse_dig_answer` (both shapes), and
  `ensure_path_under_root` (accepts + rejects matrix); handler-shim
  tests against mocked `_run_command` covering quoted-path
  invocations, missing-zone errors, NXDOMAIN-as-empty-rows, and the
  path-rejection-leaks-no-content invariant through the dispatcher
  seam.
- `backend/tests/test_connectors_bind9_atomic.py` -- T3 unit suite.
  Covers (1) the longest-suffix `resolve_zone_for_fqdn` resolver
  including label-boundary, root-zone-excluded, ambiguous, and
  unresolvable cases; (2) the pure dnspython zonefile transforms
  (`_add_record_to_zonefile` / `_remove_record_from_zonefile`) with
  SOA-serial bump assertions; (3) the `_build_pipeline_script`
  shlex-quote contract and `_parse_pipeline_output` sentinel
  framing; (4) `atomic_apply` success + every rollback branch
  (`checkconf`, `reload`, `verify`, plus the unparseable-output
  fallback that narrows to `snapshot`); (5) the `bind9_record_add` /
  `bind9_record_remove` handlers' happy path, zone-omitted
  longest-suffix resolution, invalid-IP / type-family-mismatch /
  unsupported-type / missing-sudo-password rejection branches; (6)
  the registration metadata (`safety_level=caution`, write-warning
  in description + `llm_instructions`, `additionalProperties=False`).
- `backend/tests/test_connectors_bind9_config.py` -- T4 unit suite.
  Covers (1) `pack_views_tar` pure builder (absolute member names,
  traversal rejection, mode-bit pinning, UTF-8 round-trip); (2)
  `_parse_reload_output` sentinel parser; (3) `bind9_config_apply_file`
  and `bind9_config_apply_views` route through `atomic_apply` with
  the expected validate command + staging shape (asserted by mocking
  the primitive and inspecting the call kwargs -- the load-bearing
  T4 constraint that no rollback logic is duplicated in
  `ops_config.py`); (4) `bind9_config_apply_file` rejects traversal
  paths pre-stage with no atomic-apply invocation; (5)
  `bind9_config_backup` shapes the create + list script correctly,
  parses the JSON listing into rows, emits `op_class=write` +
  `state_after` only (no `state_before`); (6) `bind9_config_reload`
  structured envelope distinguishing success from rndc failure
  without raising on non-zero rndc exit; (7) registration metadata
  for the four T4 ops (safety levels, global-atomic warnings,
  `additionalProperties=False`, handler-attr resolution).
- `backend/tests/integration/test_connectors_bind9_container.py` --
  containerised smoke test against a Debian-bookworm image with
  `bind9 bind9-host bind9utils dnsutils openssh-server` installed.
  Builds the image inline from a Dockerfile fixture (T2 seeds an
  `evba.lab` zone with each supported record type so the read-op
  tests assert against a real container). T3 extended with end-to-end
  add / remove tests, longest-suffix zone resolution against a real
  `named-checkconf -p`, and the two byte-identical-tree rollback
  assertions (checkzone failure + verify failure) gated on a
  pre/post-op `sha256sum` of the bind config root. T4 extends with
  end-to-end `config.reload` / `config.backup` / `config.apply_file`
  / `config.apply_views` tests plus two byte-identical-tree rollback
  assertions for the apply ops (invalid fragment / invalid views
  tree both refuse via `named-checkconf -p` and roll back cleanly).
  Skip-on-no-Docker matches the rest of `tests/integration/`.

## References

- Parent Initiative: [#367 G3.4 bind9-9.x typed-SSH connector](https://github.com/evoila/meho/issues/367)
- Skeleton task: [#587 G3.4-T1 Bind9Connector skeleton](https://github.com/evoila/meho/issues/587)
- Read op group: [#588 G3.4-T2 bind9 read op group](https://github.com/evoila/meho/issues/588)
- Atomic-apply + record-writes: [#589 G3.4-T3 bind9 atomic-apply primitive + record.add / record.remove](https://github.com/evoila/meho/issues/589)
- Config-write op group: [#590 G3.4-T4 bind9 config-write op group](https://github.com/evoila/meho/issues/590)
- Adapter inherited: [#243 G0.2-T4 SshConnector adapter](https://github.com/evoila/meho/issues/243)
- Registration substrate: [#395 G0.6-T4 register_typed_operation()](https://github.com/evoila/meho/issues/395)
- Sibling skeleton precedent: [#321 G3.2-T1 KubernetesConnector skeleton](https://github.com/evoila/meho/issues/321) + `backend/src/meho_backplane/connectors/kubernetes/connector.py`
- Two-phase registration precedent: `backend/src/meho_backplane/connectors/vault/__init__.py`
- Consumer wrapper replaced: [scripts/bind9-dns.sh](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/bind9-dns.sh)
- ISC bind9 9.18 docs: <https://bind9.readthedocs.io/en/v9.18/>
- asyncssh: <https://asyncssh.readthedocs.io/>
