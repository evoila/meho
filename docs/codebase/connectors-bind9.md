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
  startup. T1 ships a single-element tuple with `bind9.about`;
  T2-T4 splat their op groups onto the tuple from their own
  modules.
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
atomic-apply primitive layers on top of it; T4 (#590)'s
`bind9.config.apply_views` / `bind9.config.apply_file` /
`bind9.config.backup` / `bind9.config.reload` all route their
elevated-privilege calls through it.

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

## `probe(target)`

Five distinct failure-reason values:

| Reason | Trigger |
| ------ | ------- |
| `tcp_unreachable` | `OSError` raised by `asyncssh.connect()` (host down, firewall, wrong port) |
| `ssh_handshake_failed` | `asyncssh.DisconnectError` (host-key mismatch under a future pinning regime, protocol mismatch) |
| `auth_failed` | `asyncssh.PermissionDenied` (credentials rejected) |
| `named_not_running` | `pgrep -x named` exited non-zero |
| `named_config_invalid` | `named-checkconf -p > /dev/null` exited non-zero |

The probe is read-only and does not require a writable filesystem.
`named-checkconf -p` parses the active config and emits its
canonicalised form on stdout (which we discard); the exit code is
the parse-success signal. Ordering of the exception clauses puts
the most-specific class first (`PermissionDenied` is a subclass of
`DisconnectError` in asyncssh) so the dispatch maps to the right
reason.

## Shipped op surface (T1)

| Op id | Handler | Safety | Description |
| ----- | ------- | ------ | ----------- |
| `bind9.about` | `Bind9Connector.about` | `safe` | Operator-facing wrapper around fingerprint; returns vendor / product / version / build / os / named_conf_path |

The T2-T4 op surface lands in follow-on PRs against this same
`BIND9_OPS`-splat registration shape.

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

- `backend/tests/test_connectors_bind9.py` -- unit suite. Covers the
  registry-v2 class attrs, the package-import registration shape, the
  parse helpers, the safe-sudo primitive's argv / stdin / log-shape
  contracts, the fingerprint version-and-OS parsing, the five probe
  reason values, the register_operations upsert + idempotency loop,
  and the execute() dispatcher shim's unknown / valid / invalid
  branches.
- `backend/tests/integration/test_connectors_bind9_container.py` --
  containerised smoke test against a Debian-bookworm image with
  `bind9 bind9-host bind9utils openssh-server` installed. Builds the
  image inline from a Dockerfile fixture so the test does not depend
  on an externally-curated bind9+SSH image. Skip-on-no-Docker
  matches the rest of `tests/integration/`.

## References

- Parent Initiative: [#367 G3.4 bind9-9.x typed-SSH connector](https://github.com/evoila/meho/issues/367)
- Skeleton task: [#587 G3.4-T1 Bind9Connector skeleton](https://github.com/evoila/meho/issues/587)
- Adapter inherited: [#243 G0.2-T4 SshConnector adapter](https://github.com/evoila/meho/issues/243)
- Registration substrate: [#395 G0.6-T4 register_typed_operation()](https://github.com/evoila/meho/issues/395)
- Sibling skeleton precedent: [#321 G3.2-T1 KubernetesConnector skeleton](https://github.com/evoila/meho/issues/321) + `backend/src/meho_backplane/connectors/kubernetes/connector.py`
- Two-phase registration precedent: `backend/src/meho_backplane/connectors/vault/__init__.py`
- Consumer wrapper replaced: [scripts/bind9-dns.sh](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/bind9-dns.sh)
- ISC bind9 9.18 docs: <https://bind9.readthedocs.io/en/v9.18/>
- asyncssh: <https://asyncssh.readthedocs.io/>
