# Connector: pfsense (pfsense-2.7 / `pfsense-ssh`)

## Overview

The `pfsense` connector is the typed `Connector` subclass that dispatches
operator-facing pfSense operations over SSH. It is registered under the
`(product="pfsense", version="2.7", impl_id="pfsense-ssh")` registry triple
and is the second typed-SSH tier child of the `SshConnector` adapter (G0.2-T4
#243), after `Bind9Connector`. The `2.7` version targets the pfSense CE 2.7.x
release series (FreeBSD 14.1 base, as of 2.7.2).

The connector replaces the operator's `scripts/pfsense.sh` wrapper in the
`claude-rdc-hetzner-dc` consumer repository. The G3.7-T1 (#844) skeleton ships
only the `pfsense.about` canary op, the key-only auth enforcement, the
fingerprint, and the probe. G3.7-T2 (#847) adds the 7 read ops
(`pfctl`/config.xml parsed); G3.7-T3 (#850) ships the CLI verbs + E2E
acceptance suite + onboarding doc.

Source: `backend/src/meho_backplane/connectors/pfsense/`.

## Key types

- **`PfSenseConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="pfsense"`, `version="2.7"`, `impl_id="pfsense-ssh"`.
  Inherits the per-target asyncssh connection pool and `aclose()` from the
  adapter; overrides `_auth_config` to reject password auth, plus `fingerprint`,
  `probe`, `execute`, and adds the `about` op handler.

- **`_auth_config()` override** — the load-bearing auth constraint. Requires
  `ssh_private_key` in `target.secret_ref`; raises `ValueError` with a message
  naming the WebGUI break-glass credential when the key is absent. The
  `password` field in the Vault secret is the pfSense WebGUI break-glass
  credential and must never be used for SSH auth — pfSense's `admin` account
  connected via SSH with a password opens the console menu (an interactive PHP
  REPL) instead of a POSIX shell, causing any subsequent command to hang.

- **Op metadata** (`ops.py`) — the `PfSenseOp` dataclass plus the `PFSENSE_OPS`
  tuple the connector's `register_operations` walks at startup. T1 ships a
  single-element tuple with `pfsense.about`; T2 will extend via a
  `_pfsense_ops()` composition function.

## Control flow

### Auth

`_auth_config` is called by the `SshConnector._connect` method before opening
any TCP connection. It inspects `target.secret_ref`:

1. `ssh_private_key` present → parse via `asyncssh.import_private_key`, return
   `{username, client_keys=[key]}`.
2. No `ssh_private_key` (even if `password` is present) → `ValueError` naming
   the WebGUI break-glass credential. No password auth is attempted.

### Fingerprint (`cat /etc/version`)

`fingerprint()` runs a single `_run_command("cat /etc/version")` call. The
`/etc/version` file ships on every pfSense release and contains:

- Line 1: pfSense release string, e.g. `2.7.2-RELEASE (amd64)`.
- Line 2: build timestamp, e.g. `built on Fri Jan 12 18:00:00 UTC 2024`.
- Line 3: FreeBSD kernel, e.g. `FreeBSD 14.1-RELEASE-p5 #1 releng/14.1`.

`parse_pfsense_version()` extracts `version` (e.g. `"2.7.2-RELEASE"`), `build`
(the full first line), and `kernel` (the first `FreeBSD <token>` fragment).
Unreachable targets (OSError or asyncssh.Error from `_run_command`) return
`reachable=False` with `extras["error"]` holding the exception message.

### Probe (shell-access assertion)

`probe()` attempts the SSH connection via `_connect`, then runs
`cat /etc/version` and checks that stdout is non-empty. Failure modes:

| Condition | `ok` | `reason` |
|---|---|---|
| TCP socket refused / unreachable | `False` | `tcp_unreachable` |
| SSH handshake failed (protocol error) | `False` | `ssh_handshake_failed` |
| SSH auth rejected | `False` | `auth_failed` |
| `ValueError` from `_auth_config` (missing key) | `False` | `auth_failed` |
| `cat /etc/version` stdout empty or non-zero exit | `False` | `no_shell_access` |
| SSH connects + `/etc/version` returns content | `True` | `None` |

The `no_shell_access` reason targets the console-menu trap: pfSense's
default `admin` SSH session may land in the pfSense console menu (a PHP REPL)
rather than a POSIX shell if the account is not configured with a forced
command or if SSH key auth is not properly wired. In that scenario, `cat`
returns no output; the probe correctly reports the shell is inaccessible.

### Dispatcher shim (`execute`)

`execute()` is identical in shape to `Bind9Connector.execute`: it reads the
`endpoint_descriptor` table for the `(pfsense, 2.7, pfsense-ssh, op_id)` row,
validates params against the descriptor's JSON Schema, resolves the
`handler_ref` dotted path, and dispatches. Unknown ops return the `unknown_op`
envelope; invalid params return `invalid_params`; handler exceptions return
`connector_error`.

## Registration

Two-phase registration, identical to the bind9 pattern:

1. **Import-time (synchronous)**: `connectors/pfsense/__init__.py` calls
   `register_connector_v2(product="pfsense", version="2.7",
   impl_id="pfsense-ssh", cls=PfSenseConnector)`.
2. **Lifespan-time (asynchronous)**: `register_pfsense_typed_operations` is
   queued via `register_typed_op_registrar` and called by
   `run_typed_op_registrars` after `_eager_import_connectors`. It delegates to
   `PfSenseConnector.register_operations()`, which walks `PFSENSE_OPS` and
   calls `register_typed_operation()` per op. Idempotent.

## Dependencies

- **asyncssh ≥ 2.18, < 3.0** — SSH transport (pinned in `pyproject.toml`).
  No `py.typed` marker; mypy uses `ignore_missing_imports` per the existing
  project-wide mypy config.
- **`SshConnector`** (`connectors/adapters/ssh.py`) — parent class providing
  the per-target connection pool, `_connect`, `_run_command`, and `aclose`.
- **`register_connector_v2`** / **`register_typed_op_registrar`**
  (`connectors/registry.py`, `operations/typed_register.py`) — registration
  infrastructure.

## Known issues

- `known_hosts=None` in the SSH adapter disables host-key verification for
  v0.2. Key pinning is deferred to v0.2.next once a Vault-managed key store
  is in place.
- The T1 skeleton ships only `pfsense.about`. The 7 pfSense read ops
  (`pfctl -sr`, `pfctl -ss`, `pfctl -sn`, `ifconfig -a`, config.xml reads)
  land in T2 (#847).

## References

- Task #844 (this skeleton): G3.7-T1 PfSenseConnector skeleton.
- Task #847 (next): G3.7-T2 pfSense 7 read ops.
- Task #850 (final): G3.7-T3 pfSense CLI verbs + E2E + onboarding doc.
- Parent initiative: #370 (G3.7 tier-3 standalone connectors).
- Bind9 connector (canonical typed-SSH reference): `docs/codebase/connectors-bind9.md`.
- `SshConnector` adapter: `backend/src/meho_backplane/connectors/adapters/ssh.py`.
