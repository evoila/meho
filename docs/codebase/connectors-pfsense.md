# Connector: pfsense (pfsense-2.7 / `pfsense-ssh`)

## Overview

The `pfsense` connector is the typed `Connector` subclass that dispatches
operator-facing pfSense operations over SSH. It is registered under the
`(product="pfsense", version="2.7", impl_id="pfsense-ssh")` registry triple
and is the second typed-SSH tier child of the `SshConnector` adapter (G0.2-T4
#243), after `Bind9Connector`. The `2.7` version targets the pfSense CE 2.7.x
release series (FreeBSD 14.1 base, as of 2.7.2).

The connector replaces the operator's `scripts/pfsense.sh` wrapper in the
consumer repository. The G3.7-T1 (#844) skeleton ships
only the `pfsense.about` canary op, the key-only auth enforcement, the
fingerprint, and the probe. G3.7-T2 (#847) adds the 7 read ops
(`pfctl`/config.xml parsed); G3.7-T3 (#850) ships the CLI verbs + E2E
acceptance suite + onboarding doc. #2849 adds `pfsense.dhcp.leases`. #3090
adds the first two write ops (`pfsense.gateway.add`,
`pfsense.route.static.add`) via the `pfSsh.php playback` config-mutation
idiom.

Source: `backend/src/meho_backplane/connectors/pfsense/`.

## Key types

- **`PfSenseConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="pfsense"`, `version="2.7"`, `impl_id="pfsense-ssh"`.
  Inherits the per-target asyncssh connection pool and `aclose()` from the
  adapter; overrides `_auth_config` to reject password auth, plus `fingerprint`,
  `probe`, `execute`, `about`, the read-op bound-method shims (the 7 T2 ops
  plus `dhcp_leases`, #2849), and the write-op bound-method shims (`gateway_add`,
  `route_static_add`, #3090).

- **`_auth_config()` override** — the load-bearing auth constraint. Requires
  `ssh_private_key` in the target's **Vault secret** (`target.secret_ref` is a
  KV-v2 path string resolved via the base adapter's `_resolve_secret`, #2155);
  raises `ValueError` with a message
  naming the WebGUI break-glass credential when the key is absent. The
  `password` field in the Vault secret is the pfSense WebGUI break-glass
  credential and must never be used for SSH auth — pfSense's `admin` account
  connected via SSH with a password opens the console menu (an interactive PHP
  REPL) instead of a POSIX shell, causing any subsequent command to hang.

- **Op metadata** (`ops.py`) — the `PfSenseOp` dataclass, the `_pfsense_ops()`
  composition function, and the `PFSENSE_OPS` tuple (11 ops total). T1 shipped
  `pfsense.about`; T2 (#847) adds 7 read ops via the `ops_read` module; #2849
  appends `pfsense.dhcp.leases`; #3090 appends the two write ops via the
  `ops_write` module.

- **Write op handlers** (`ops_write.py`, #3090) — input validators
  (`_validate_gateway_name` / `_validate_interface` / `_validate_gateway_ip` /
  `_validate_network_cidr`), the `parse_static_routes_xml` config parser, the
  `pfSsh.php playback` fragment builders (`_build_gateway_playback` /
  `_build_route_playback`), the stage/playback/cleanup helper (`_apply_playback`),
  and the handler functions `pfsense_gateway_add` / `pfsense_route_static_add`
  plus the `WRITE_OPS` tuple.

- **Read op parsers** (`ops_read.py`) — pure parsers for pfctl, config.xml, and
  the ISC dhcpd lease DB, plus the handler functions and the `READ_OPS` tuple:
  - `parse_pfctl_rules` / `parse_pfctl_states` / `parse_pfctl_nat` — pfctl
    output parsers.
  - `parse_ifconfig` / `_netmask_to_cidr` — ifconfig output parser.
  - `parse_gateways_xml` — XML parser for the `<gateways>` block.
  - `parse_dhcp_leases` (#2849) — parses `/var/dhcpd/var/db/dhcpd.leases`
    (log-structured ISC dhcpd lease DB) into de-duplicated lease rows; an
    `active` lease whose `ends` is past reads as `expired`.
  - `parse_gateway_status` / `_parse_metric` — parser for the live
    `pfSsh.php playback gatewaystatus` (dpinger) table, keyed by gateway
    name.
  - Handler functions: `pfsense_version`, `pfsense_firewall_rules`,
    `pfsense_firewall_state`, `pfsense_nat_rules`, `pfsense_interface_list`,
    `pfsense_gateway_list`, `pfsense_config_show`, `pfsense_dhcp_leases`.

## Control flow

### Auth

`_auth_config(target, operator)` is called by the `SshConnector._connect`
method before opening any TCP connection. It resolves `target.secret_ref`
(a Vault KV-v2 path string) to the secret's data dict via the base
adapter's `_resolve_secret` (operator-context Vault read, #2155), then:

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
| `cat /etc/version` raises after a successful connect (drop / `asyncssh.Error` / timeout) | `False` | `command_failed` |
| `cat /etc/version` stdout empty or non-zero exit | `False` | `no_shell_access` |
| SSH connects + `/etc/version` returns content | `True` | `None` |

The post-connect `cat /etc/version` is wrapped in a `(OSError,
asyncssh.Error)` guard so a connection drop, an `asyncssh.Error`, or a
timeout after the handshake maps to `command_failed` rather than escaping
`probe()` as an unhandled exception (#986). `TimeoutError` is an `OSError`
subclass, so the command-timeout case is covered by the same tuple.

The `no_shell_access` reason targets the console-menu trap: pfSense's
default `admin` SSH session may land in the pfSense console menu (a PHP REPL)
rather than a POSIX shell if the account is not configured with a forced
command or if SSH key auth is not properly wired. In that scenario, `cat`
returns no output; the probe correctly reports the shell is inaccessible.

### About (`pfsense.about`) — unreachability surfacing

`about()` reuses `fingerprint()`, which returns `reachable=False` (rather
than raising) on a connection failure. `about()` calls the shared
`SshConnector._assert_reachable(result)` guard immediately after, which
raises `ConnectorUnreachableError` when the fingerprint is not reachable.
Without this check `about()` would return a dict of empty/None identity
fields that the dispatcher reports as a successful (`status="ok"`) op,
masking the failure (#986). The raised error is caught by the dispatcher
shim and mapped to a `connector_error` `OperationResult` (`status="error"`).

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

## Op surface (11 ops)

| Op ID | Command | Group | Safety |
|---|---|---|---|
| `pfsense.about` | `cat /etc/version` | `identity` | `safe` |
| `pfsense.version` | `cat /etc/version` | `config` | `safe` |
| `pfsense.firewall.rules` | `pfctl -sr` | `firewall` | `safe` |
| `pfsense.firewall.state` | `pfctl -ss` | `firewall` | `safe` |
| `pfsense.nat.rules` | `pfctl -sn` | `nat` | `safe` |
| `pfsense.interface.list` | `ifconfig -a` | `network` | `safe` |
| `pfsense.gateway.list` | `cat /cf/conf/config.xml` (gateways block) + `pfSsh.php playback gatewaystatus` (live dpinger status) | `network` | `safe` |
| `pfsense.config.show` | `cat /cf/conf/config.xml` (full) | `config` | `safe` |
| `pfsense.dhcp.leases` | `cat /var/dhcpd/var/db/dhcpd.leases` (ISC dhcpd lease DB) | `dhcp` | `safe` |
| `pfsense.gateway.add` | `cat /cf/conf/config.xml` guard + `pfSsh.php playback` fragment (append `gateway_item` + `write_config()`) | `routing` | `caution` |
| `pfsense.route.static.add` | `cat /cf/conf/config.xml` guard + `pfSsh.php playback` fragment (append `staticroutes/route` + `write_config()` + `system_routing_configure()`) | `routing` | `caution` |

The 9 read / identity ops are `safety_level="safe"`; the 2 write ops (#3090)
are `safety_level="caution"`. All 11 are `requires_approval=False` — the same
posture `bind9.record.add` / `windns.record.add` carry for an additive,
recoverable, idempotent config write.

`pfsense.firewall.state` and `pfsense.dhcp.leases` both return `{rows, total}`
and are the JSONFlux reduction candidates: connection-state tables and busy
DHCP pools can each carry many rows. The reducer (key `pfsense_firewall_state`
/ `pfsense_dhcp_leases`) wraps the payload in a `ResultHandle` when `total`
exceeds its threshold; smaller payloads pass through inline. Handle-vs-inline
is the reducer's job, not the connector's — every handler returns rows inline.

`pfsense.dhcp.leases` reads the live ISC dhcpd lease database (pfSense chroots
`dhcpd` under `/var/dhcpd`). The file is log-structured, so the parser
de-duplicates to the last block per IP; timestamps are UTC; an `active` lease
whose `ends` has passed is reported as `expired`. DHCPv6 (`dhcpd6.leases`) and
pool-exhaustion percentage math (correlating against the `config.xml` `<range>`)
are out of scope — follow-ups.

`pfsense.gateway.list` runs **two** SSH commands and merges them (mirroring
`bind9.zone.read`): `cat /cf/conf/config.xml` for the static `<gateways>`
block, then `pfSsh.php playback gatewaystatus` for pfSense's live `dpinger`
view. The live state is keyed by gateway name and overlaid onto each config
row as `status` (`online`/`down`), `delay_ms`, `stddev_ms`, `loss_pct`, and
`substatus`. `config.xml` alone only answers "what gateways are configured";
the second command answers "is this gateway degraded right now". A gateway
present in `config.xml` but absent from the live view (e.g. on a down
interface `dpinger` is not monitoring) keeps its row with all five health
fields `null`; a failure of the status command degrades the whole set to
`null` health rather than failing the op.

## Write ops (#3090)

`pfsense.gateway.add` and `pfsense.route.static.add` are the connector's first
mutating ops. pfSense CE 2.7 has no REST surface, so the mutation runs through
the **`pfSsh.php playback` idiom** — the same mechanism the `pfsense.gateway.list`
read op already uses (`pfSsh.php playback gatewaystatus`). Because
`pfSsh.php playback <name>` resolves its argument through `basename()` against
`/etc/phpshellsessions/` (and piped stdin does not drive the interactive `exec`
loop reliably), each write:

1. reads `/cf/conf/config.xml` and parses the relevant block (the guard);
2. if the entry is absent, stages a raw-PHP fragment into
   `/etc/phpshellsessions/<script>` via a **quoted-delimiter heredoc** (no shell
   expansion), plays it back with `pfSsh.php playback <script>`, and removes the
   file in a `finally`;
3. reads `config.xml` back and confirms the entry landed.

The fragment carries no `<?php` tag and no trailing `exec`, and opens with
`global $config;` — `playback_text` prepends `require_once` of the pfSense
config libraries and `eval`s the text inside a function scope, so the config
libraries populate the `$config` global and the fragment must pull it into
scope (mirroring the shipped `gatewaystatus` script's `global $argv;`).

**Guarded + idempotent.** A gateway whose `name` already exists, or a route
whose canonical `network` already exists, is reported as
`{existed_before: true, applied: false, existing: <row>}` and stages **no**
playback — never a duplicate. `pfsense.route.static.add` additionally requires
the referenced gateway to already exist (pre-stage it with
`pfsense.gateway.add`, whose `monitor_disable` flag suits a gateway whose
upstream device is not up yet). A non-zero playback exit, or a silent
`write_config` failure that leaves the entry absent on read-back, raises rather
than reporting success.

**Injection safety.** Every operator value is validated and re-serialised before
it reaches the fragment: names / interfaces against a strict character-class
allowlist (`^[A-Za-z0-9_-]{1,64}$` / `^[A-Za-z0-9_]{1,32}$`), IPs parsed and
re-emitted through `ipaddress`, CIDRs canonicalised. Anything outside the
allowlist is rejected before a single SSH round-trip; `_php_squote` escapes
defensively on top.

**Surgical contract.** The handlers touch only `gateways/gateway_item` and
`staticroutes/route` — never interface config. The perimeter pfSense frequently
carries the operator's own access path, so an interface re-enumeration would
sever the very session issuing the change. `pfsense.route.static.add` calls
`system_routing_configure()` (route-table apply only); `pfsense.gateway.add`
applies nothing — a pre-staged gateway is inert config until referenced.

## Known issues

- `known_hosts=None` in the SSH adapter disables host-key verification for
  v0.2. Key pinning is deferred to v0.2.next once a Vault-managed key store
  is in place.

## References

- Task #844 (this skeleton): G3.7-T1 PfSenseConnector skeleton.
- Task #847 (this): G3.7-T2 pfSense 7 read ops (landed).
- Task #850 (final): G3.7-T3 pfSense CLI verbs + E2E + onboarding doc.
- Task #2849: `pfsense.dhcp.leases` — ISC dhcpd lease-DB read op (connector
  read-op coverage wave 2, initiative #2833). Grammar per `dhcpd.leases(5)`;
  chroot path confirmed against pfSense's `status_dhcp_leases.php`.
- Task #2850: project live `dpinger` status (up/RTT/loss) onto
  `pfsense.gateway.list` via `pfSsh.php playback gatewaystatus`.
- Task #3090: `pfsense.gateway.add` + `pfsense.route.static.add` — the first
  write ops, via the `pfSsh.php playback` config-mutation idiom. Classification
  mirrors `bind9.record.add` (`caution` / no-approval). pfSense PHP shell:
  https://docs.netgate.com/pfsense/en/latest/development/php-shell.html.
- Parent initiative: #370 (G3.7 tier-3 standalone connectors).
- Bind9 connector (canonical typed-SSH reference): `docs/codebase/connectors-bind9.md`.
- `SshConnector` adapter: `backend/src/meho_backplane/connectors/adapters/ssh.py`.
