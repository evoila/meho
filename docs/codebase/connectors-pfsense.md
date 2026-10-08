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
idiom. #3232 adds the first two **governed destructive deletes**
(`pfsense.nat.delete`, `pfsense.alias.delete`) — `safety_level="destructive"`
on the governed-delete tier (`docs/decisions/governed-delete-operations.md`).
#3313 adds the three **teardown-inverse** governed deletes
(`pfsense.route.static.delete`, `pfsense.gateway.delete`,
`pfsense.alias.member.remove`) that reverse a governed bring-up — retire a
static route, retire a gateway (fail-closed on any live referrer), and trim
ONE member out of a *shared* alias without deleting it. #3954 adds two safe
reads, `pfsense.user.list` and `pfsense.route.static.list`. They return only a
short list of fields, so nobody needs `pfsense.config.show` (the whole file)
just to check a user or a static route.

Source: `backend/src/meho_backplane/connectors/pfsense/`.

## Key types

- **`PfSenseConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="pfsense"`, `version="2.7"`, `impl_id="pfsense-ssh"`.
  Inherits the per-target asyncssh connection pool and `aclose()` from the
  adapter; overrides `_auth_config` to reject password auth, plus `fingerprint`,
  `probe`, `execute`, `about`, the read-op bound-method shims (the 7 T2 ops
  plus `dhcp_leases`, #2849), the write-op bound-method shims (`gateway_add`,
  `route_static_add`, #3090), the destructive-delete bound-method shims
  (`nat_delete`, `alias_delete`, #3232; `route_static_delete`, `gateway_delete`,
  `alias_member_remove`, #3313), and the allow-listed config-read shims
  (`user_list`, `route_static_list`, #3954).

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
  composition function, and the `PFSENSE_OPS` tuple (19 ops total). T1 shipped
  `pfsense.about`; T2 (#847) adds 7 read ops via the `ops_read` module; #2849
  appends `pfsense.dhcp.leases`; #3090 appends the two write ops via the
  `ops_write` module; #3232 appends the first two destructive deletes and #3313
  the three teardown-inverse deletes, both via the `ops_delete` module;
  meho-internal#252 appends the parameterized management-plane flow classifier
  `pfsense.mgmt_flow.summary` via the `ops_mgmt_flow` module; #3954 appends
  `pfsense.user.list` and `pfsense.route.static.list` via the
  `ops_config_reads` module.

- **Allow-listed config reads** (`ops_config_reads.py`, #3954) — the pure
  parsers `list_local_users` and `list_static_routes`, the strict root parser
  `_parse_config_root` (raises `PfSenseConfigParseError` with a fixed message),
  the one text reader `_text_or_none` (own text only, then the
  `looks_like_secret` shape check), the handlers `pfsense_user_list` / `pfsense_route_static_list`, the row-key
  allow-lists `USER_ROW_FIELDS` / `ROUTE_ROW_FIELDS`, and the `CONFIG_READ_OPS`
  tuple. See "`pfsense.user.list` and `pfsense.route.static.list`" below.

- **Destructive-delete handlers** (`ops_delete.py`, #3232 + #3313) — the
  connector's `safety_level="destructive"` ops. Config parsers
  (`parse_nat_port_forwards_xml` / `parse_aliases_xml`), the fail-closed
  reference scans (`find_alias_references` — nested aliases + filter / NAT
  port-forward / outbound / 1:1 rules; `find_gateway_references` — static
  routes + gateway groups + default-gateway setting), the canonical route
  matcher (`_match_static_routes_canonical`) and member-token split
  (`_alias_members`), the delete/remove-by-identity `pfSsh.php playback`
  fragment builders (`_build_nat_delete_playback` / `_build_alias_delete_playback`
  / `_build_route_delete_playback` / `_build_gateway_delete_playback` /
  `_build_alias_member_remove_playback`, each persisting only on a single
  clean removal), the handlers (`pfsense_nat_delete` / `pfsense_alias_delete` /
  `pfsense_route_static_delete` / `pfsense_gateway_delete` /
  `pfsense_alias_member_remove`), the mandatory blast-radius preview builders
  (one per op, registered at import via
  `register_pfsense_delete_preview_builders`), and the `DELETE_OPS` tuple. All
  fold into the destructive-tier service-grant refusal (safety_level +
  `destructive` tag; `alias.member.remove`, whose op-id does not end in
  `.delete`, is refused via the tier rather than the `*.delete` glob) with no
  new pattern list.

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
    output parsers. `parse_pfctl_states` reads the real `pfctl -ss` field
    order — **interface first, protocol second** (`<if> <proto> <ep1>
    <-|-> <ep2>  <state>`, pfSense 2.7 / pf state format, redmine #2121);
    the `proto`/`iface` capture groups were transposed before #252, which
    mislabelled every row (and, latently, `pfsense.firewall.state`'s
    output). A NAT-translated first endpoint (`addr:port (xlate:port)`) does
    not match and is returned unparsed (`proto=None`) rather than misparsed.
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
- **Config secret removal** (`redaction.py`, with `redaction_names.py` and
  `redaction_shapes.py`) — `redact_config_xml` (returns the cleaned text and
  `redacted_count`), the name rule `is_secret_name` and its lists
  (`redaction_names.py`), the value-shape rule `looks_like_secret`
  (`redaction_shapes.py`), the marker `REDACTED` (`***REDACTED***`), the
  depth limit `MAX_DEPTH` and `ConfigRedactionError`. Used only by
  `pfsense_config_show`; see "`pfsense.config.show` — secret removal" below.

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

## Op surface (19 ops)

| Op ID | Command | Group | Safety |
|---|---|---|---|
| `pfsense.about` | `cat /etc/version` | `identity` | `safe` |
| `pfsense.mgmt_flow.summary` | `pfctl -ss` (classified in-op by source vs `sanctioned_src` / `baseline_src` and dst vs `mgmt_nets` / `mgmt_ports`) | `firewall` | `safe` |
| `pfsense.version` | `cat /etc/version` | `config` | `safe` |
| `pfsense.firewall.rules` | `pfctl -sr` | `firewall` | `safe` |
| `pfsense.firewall.state` | `pfctl -ss` | `firewall` | `safe` |
| `pfsense.nat.rules` | `pfctl -sn` | `nat` | `safe` |
| `pfsense.interface.list` | `ifconfig -a` | `network` | `safe` |
| `pfsense.gateway.list` | `cat /cf/conf/config.xml` (gateways block) + `pfSsh.php playback gatewaystatus` (live dpinger status) | `network` | `safe` |
| `pfsense.config.show` | `cat /cf/conf/config.xml` (full file, known secret fields and shapes replaced) | `config` | `safe` |
| `pfsense.dhcp.leases` | `cat /var/dhcpd/var/db/dhcpd.leases` (ISC dhcpd lease DB) | `dhcp` | `safe` |
| `pfsense.user.list` | `cat /cf/conf/config.xml` (`<system><user>` + `<system><group>`, allow-listed fields only) | `users` | `safe` |
| `pfsense.route.static.list` | `cat /cf/conf/config.xml` (`<staticroutes><route>`, allow-listed fields only) | `routing` | `safe` |
| `pfsense.gateway.add` | `cat /cf/conf/config.xml` guard + `pfSsh.php playback` fragment (append `gateway_item` + `write_config()`) | `routing` | `caution` |
| `pfsense.route.static.add` | `cat /cf/conf/config.xml` guard + `pfSsh.php playback` fragment (append `staticroutes/route` + `write_config()` + `system_routing_configure()`) | `routing` | `caution` |
| `pfsense.nat.delete` | `cat /cf/conf/config.xml` guard (match one `<nat><rule>` by `<tracker>`) + `pfSsh.php playback` fragment (delete-by-tracker + `write_config()` + `filter_configure()`) + read-back verify | `nat` | `destructive` |
| `pfsense.alias.delete` | `cat /cf/conf/config.xml` guard (match one `<aliases><alias>` by name + fail-closed reference scan) + `pfSsh.php playback` fragment (delete-by-name + `write_config()` + `filter_configure()`) + read-back verify | `alias` | `destructive` |
| `pfsense.route.static.delete` | `cat /cf/conf/config.xml` guard (canonical-match one `<staticroutes><route>` by network) + `pfSsh.php playback` fragment (delete-by-network + `write_config()` + `system_routing_configure()`) + read-back verify | `routing` | `destructive` |
| `pfsense.gateway.delete` | `cat /cf/conf/config.xml` guard (match one `<gateways><gateway_item>` by name + fail-closed reference scan) + `pfSsh.php playback` fragment (delete-by-name + `write_config()` + `system_routing_configure()`) + read-back verify | `routing` | `destructive` |
| `pfsense.alias.member.remove` | `cat /cf/conf/config.xml` guard (match one `<aliases><alias>` by name + locate the member token) + `pfSsh.php playback` fragment (read-modify-write `<address>`/`<detail>` + `write_config()` + `filter_configure()`) + read-back verify | `alias` | `destructive` |

The 11 read / identity ops plus the `pfsense.mgmt_flow.summary` classifier are
`safety_level="safe"`; the 2 write ops (#3090)
are `safety_level="caution"` / `requires_approval=False` — the same posture
`bind9.record.add` / `windns.record.add` carry for an additive, recoverable,
idempotent config write. The 5 destructive ops (#3232 nat/alias; #3313 the
three teardown-inverse ops) are `safety_level="destructive"` /
`requires_approval=True` — the governed-delete tier: mandatory human approval
(no agent path, no standing grant, no self-approval even under break-glass, no
satellite mint), a #3197 preview-hash binding, and a mandatory blast-radius
statement naming the exact object. Each acts on exactly one object by stable
identity (`<tracker>` for a NAT rule, exact name for an alias / gateway,
canonical network for a route, exact member token for an alias member — never
by position), refuses fail-closed on a missing (`not_found`) / duplicated
(`ambiguous`) identity or a live reference (`referenced`, naming every
referrer, for `alias.delete` and `gateway.delete`), and read-back-verifies the
result. `pfsense.alias.member.remove` is the odd one out: it is a
**read-modify-write on a shared object**, not a delete — it removes ONE member
from the alias's `<address>` (and its positionally-aligned `<detail>`) while
keeping the alias and every other member, refuses `member_not_found` /
`last_member` (never empties a shared alias into deletion — that is
`alias.delete`'s job), and verifies the member is gone, the alias is retained,
and the residual members survive.

`pfsense.firewall.state` and `pfsense.dhcp.leases` both return `{rows, total}`
and are the JSONFlux reduction candidates: connection-state tables and busy
DHCP pools can each carry many rows. `pfsense.user.list` and
`pfsense.route.static.list` return `{rows, total}` too, so a long user or
route list also comes back as a handle. The reducer (key `pfsense_firewall_state`
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

### `pfsense.config.show` — secret removal

`config.xml` holds the firewall's secrets next to its normal settings: user
password hashes, certificate and CA private keys (`<prv>`), OpenVPN shared and
TLS keys, IPsec pre-shared keys, RADIUS / LDAP / sync passwords, notification
tokens and package secrets. The op is `safe` with no approval, so the handler
replaces **known secret fields and known secret shapes** **before** the result
leaves it (`redaction.redact_config_xml`). The audit row's `raw_payload`, the
flight-recorder trace, a stored result and the broadcast feed therefore only
see the cleaned text.

- **Use the small reads first.** To check local users or static routes, call
  `pfsense.user.list` or `pfsense.route.static.list`. They return only a
  short list of fields and never read secret fields. The op's description and
  `when_to_use` say this too.
- **The limit.** This is a list of known fields and known shapes, not a proof.
  Free-text fields (descriptions, notes, cron or shell commands, custom config
  text, URLs) can still hold secrets that someone typed in. The Notes package
  `notes` field is one of them: it is free text, so only a secret shape or a
  secret word inside it (see rule 5) removes it.
- **What is removed.** A value goes when any of these rules fires:
  1. *Known names* (`SECRET_ELEMENT_NAMES`), checked against the pfSense 2.7.2
     source and the pfSense package sources: pfSense's own "sanitized config"
     list (`$filtered_tags` in `status_output.inc`) minus `authorizedkeys`,
     plus `keydata`, `omapi_key`, `trapstring`, `vouchersyncpass`, `apikey`,
     `userkey`, `api`, `radiuskey*`, `simpin` (SIM card PIN), the `sshdata`
     block (backed-up SSH host keys), the generic `hash` and `pin`, and package
     text fields that hold whole config files: NUT `upsd_users`, Telegraf
     `telegraf_raw_config`, BIND `bind_custom_options`, Squid
     `custom_options*_squid3`, Zabbix `userparams` / `advancedparams`, snmptt
     `snmptt_configfile`, syslog-ng `objectparameters`.
  2. *Context rules* (a name that is secret only below a given element):
     `<username>` below `<pppoes>` (PPPoE users with base64 passwords);
     `filedata` below `<dnsseckeys>` (BIND DNSSEC private key backups) and
     below `<filer>`; `frr`, `frrrunning`, `zebra`, `bgpd`, `ospfd`, `ospf6d`,
     `ripd`, `bfdd` below `<frrglobalraw>` (saved and running FRR configs);
     `advanced`, `advanced_backend` and `content` below `<haproxy>`;
     `custom_options` below `<netsnmp>` / `<netsnmptrapd>`; `custom_config`
     below `<ntopng>`. Below `<acme>`, **every** `dns_*` element (the ACME
     DNS-provider settings): many are API keys and passwords whose names do not
     say so (`dns_ovhovh_as`, `dns_lala_sk`), so the few non-secret ones go
     too.
  3. *Name patterns* (`is_secret_name`): names containing `password`,
     `passwd`, `passwort`, `passphrase`, `passcode`, `pswd`, `secret`, `psk`,
     `bindpw`, `prv`, `shared_key`, `api_key`, `private_key`, `credential`,
     `creds`, `authdata`, `community`, `token` or `bearer`; names ending in
     `pass` (not `bypass`), `pwd`, `pw` or `key`; a `-hash` / `_hash` suffix;
     the ACME `dns_*key|password|secret|token|pwd|pw` fields. Every part of a
     namespaced name (`ns:name`) counts. A bare "contains hash" rule and a
     `pin` ending are deliberately not used: they would hide the IPsec
     `hash-algorithm` choices, the LAGG `lagghash` policy, the `pwhash`
     algorithm name and the FRR `routemap_in` setting.
  4. *Value shapes* (`looks_like_secret`), checked on every text value and
     CDATA section: a PEM private key, an OpenVPN key, a DNSSEC private key file
     (`Private-key-format:` / `PrivateKey:`), crypt password hashes (`$1$`,
     `$2a$`/`$2b$`/`$2y$`, `$5$`, `$6$`, ...), a password in a URL
     (`scheme://user:password@host`) or a query parameter whose name holds
     `token`, `key`, `pass`, `pwd`, `secret` or `auth`, a run of 256 or more
     hex digits (key size), and base64 that decodes (or decodes and inflates)
     to a key or to a DER private key (PKCS#1, PKCS#8, SEC1, encrypted
     PKCS#8). Base64 is checked as a whole value, as runs inside the text,
     with the whitespace removed (any line width) and at all four alignments
     (text glued in front). A compressed value that inflates to more than
     4 KiB cannot be checked in full, so it is removed.
  5. *Base64 text*: when a whole value is base64 that decodes to text, the
     decoded text gets the shape checks above plus a secret-word check: the
     name parts from rule 3, and words ending in `pw`, `pwd` or `pass` (not
     the bare word `pass`, a firewall rule keyword). PEM blocks and long
     base64 runs are dropped before the word check, so the random letters in
     a public certificate cannot spell a word. On a hit, the whole value goes.
  Everything below a secret element is secret too.
- **What stays.** Every other byte, unchanged: tags, whitespace, CDATA
  wrappers, entity references. Public certificates (`<crt>`), CSRs, SSH public
  keys (`authorizedkeys`) and WireGuard public keys stay. Plain free text is
  not checked for words, so a description that says "password" stays.
- **Names that only look secret** (`_NOT_SECRET_NAMES` in
  `redaction_names.py`), checked against the package sources: the FRR
  route-map settings `community_set` / `community_match` / `community_action`
  / `community_additive`, FRR `password_type` and `passwordencrypt`, Suricata
  `snortcommunityrules` and `eve_log_files_hash`, the OpenVPN client export
  `usepass` / `usetoken` / `useproxypass`, WireGuard `hide_secrets`, sudo
  `nopasswd`, pfBlockerNG `pfb_dnsvip_skew`, FreeRADIUS
  `varsettingsmotptokenlength`, and, for the `key` ending, `publickey` /
  `pubkey` / `public_key`, `widgetkey`, `eve_redis_key`, `prefetchkey`, the
  Wi-Fi `wpa_*_rekey` settings, the IPsec `mobilekey` list (each entry's key is
  still checked) and the OpenVPN wizard `dhkey`. A name we are not sure about
  stays redacted. Below a secret element, these names are removed too.
- **What pfSense never writes.** pfSense's own writer (`dump_xml_config` in
  `xmlparse.inc`) writes a leading `<?xml version="1.0"?>`, then elements, each
  value as one text or CDATA piece, and nothing else. So every attribute value
  and every comment is replaced, whatever it holds, and the scan fails closed
  on any other processing instruction, on text outside the root element and
  on a value split into several text / CDATA pieces.
- **No XML parser.** The file is scanned as text with regular expressions, so
  no entity is expanded and the output is the input with only the secret
  values swapped. `defusedxml` (already a dependency, used by
  `parse_gateways_xml`) was not used here because parsing and re-serialising
  would not keep the rest of the file byte for byte (CDATA, attribute quoting,
  whitespace).
- **Fail closed.** The scan raises `ConfigRedactionError` on markup it does
  not recognise (for example a `<!DOCTYPE`, which pfSense never writes), a
  mismatched or unclosed element, elements nested deeper than `MAX_DEPTH`
  (64; a real file is about 10 deep), one of the "never written" cases above,
  or a key / hash shape that survives in the output. The error message is a
  fixed reason plus a character offset (for example
  `mismatched end tag at offset 812`); it never quotes the file. The handler
  turns any exception into `config_xml: null` plus an `error`, and logs only
  that fixed reason; the raw file is never returned.
- **Speed.** The scan runs in a worker thread (`asyncio.to_thread`), so a
  large file never blocks the event loop. Every regular expression stays
  linear, the open element names are kept in a running count (no per-tag
  rebuild), and the depth limit stops a deeply nested file at once. A 600 KB
  config takes about 0.1 s on a laptop.
- **Result shape.** `{config_xml, length, redacted_count}`; `length` is the
  length of the returned (cleaned) text and `redacted_count` is the number of
  values replaced. The output is not a restorable backup.
- **Traces.** The raw `cat` output still passes through the shared SSH
  transport span (`SshConnector._run_command`), but that span hands it to the
  flight-recorder redaction engine as plain text, which drops it
  (`[MEHO-OMITTED:redaction-uncertain]`) — pinned by
  `test_ssh_span_never_keeps_raw_pfsense_config_stdout`.

### `pfsense.user.list` and `pfsense.route.static.list` — safe reads with an allow-list (#3954)

Two `safe` reads, with no approval. Each reads `config.xml` once and returns
`{rows, total}`.

- **`pfsense.user.list`** returns one row per `<system><user>` with these keys
  only: `name`, `descr` (full name), `scope`, `disabled`, `expires`, `uid` and
  `groups`. `groups` lists the names of every `<system><group>` whose
  `<member>` list holds the user's uid, sorted. pfSense keeps membership on
  the group, not on the user. The built-in `all` group can appear.
- **`pfsense.route.static.list`** returns one row per
  `<staticroutes><route>` with these keys only: `network`, `gateway` (the
  gateway name), `descr` and `disabled`.
- **From list to delete.** This works for a route whose destination is a CIDR
  network: `pfsense.route.static.delete` finds the route by `network`, so an
  operator can list the routes and then delete one with a row's `network`
  value. A route stored with host bits set still matches, because the delete
  op compares the canonical network; IPv6 works too. pfSense also allows an
  alias as the destination. The list shows the alias name, but the delete op
  rejects it ("network must be a valid CIDR"). This limit is in the delete
  op, not in the list.
- **Secret fields are never read.** Each row is built from a fixed set of
  child elements. Only each element's own text is read: no other child, no
  attribute, and no text of an element nested inside an allowed one. So
  password hashes, the IPsec pre-shared key, SSH authorized keys, OTP data and
  certificate references are never read, also for secret fields a future
  pfSense version adds.
- **Secret shapes are replaced.** An allowed field can still hold a secret that
  someone put there, most likely the free-text `descr`. So every returned
  string (also `name`, `uid`, group names, `gateway` and `network`) goes
  through `looks_like_secret()`, the same value-shape check
  `pfsense.config.show` uses (`redaction_shapes.py`). A value with a known
  secret shape (a private key, a crypt password hash, a password in a URL, a
  long hex run, or base64 that hides one of these) comes back as
  `***REDACTED***`. Normal names, dates, uids, gateway and alias names and
  IPv4 / IPv6 networks never match.
- **The limit.** A secret typed in plain words (for example a password written
  into `descr`) can still show. This is the same limit as
  `pfsense.config.show`.
- **Flags.** `disabled` is `true` when a `<disabled>` child is there, whatever
  it holds (pfSense checks `isset`). `expires` is the stored date text
  (`MM/DD/YYYY`), or `null` when empty. Other text fields are `null` when the
  element is missing or empty.
- **Only direct children.** Users are read from `<pfsense><system>` and routes
  from `<pfsense><staticroutes>`. A `<user>` or `<staticroutes>` inside a
  package section is not listed.
- **Fail with a clear error.** The file is parsed with `defusedxml`, like
  `parse_gateways_xml`, with `forbid_dtd=True` (pfSense never writes a
  DOCTYPE). Unlike that parser, an empty file, a file that is not
  well-formed, a file with a DTD or entity, or a root other than `<pfsense>`
  raises `PfSenseConfigParseError`. A failed `cat` raises `RuntimeError`. Both
  messages are fixed text: they never quote the file, and the parser's own
  exception is not chained. The call then fails with `connector_error`, so a
  broken read never looks like "no users".
- **CLI.** `meho pfsense user list` and `meho pfsense route list`.

### `pfsense.mgmt_flow.summary` — management-plane flow classifier (meho-internal#252)

The enabling read op for the management-plane lockdown ratchet's **alert**
stage (Goal meho-internal#234, Initiative #249, Task #252). It runs `pfctl -ss`
and classifies every live TCP connection state whose *server* side (resolved
from the `pfctl -ss` direction arrow, then required to carry a management
port) sits in a caller-supplied management network into **sanctioned** vs
**non-sanctioned** by source, and flags **unexpected**
sources — non-sanctioned AND not in a caller-supplied baseline. It returns a
compact per-leg summary (open-state counts + coverage %) plus the distinct
`non_sanctioned_sources` (capped) and `unexpected_sources` (complete) as
`{src, leg, ports, states}` rows and the exact `*_source_count` scalars. The
pure classifier `classify_mgmt_flows` (`ops_mgmt_flow.py`) is tested against
fixture text; the handler is the thin `pfctl -ss` + `parse_pfctl_states` +
classify layer.

Why it exists as a governed op rather than a raw-state Sensor: a Sensor's
bounded assertion (one dotted path + at most one aggregate + one comparator,
`docs/codebase/sensor.md`) cannot itself filter a state table by source-set
membership, and `pfsense.firewall.state` reduces to a JSONFlux handle on a busy
box — neither is assertable. This op collapses the state table to a small,
inline, pre-classified summary a Sensor can pin: assert
`$.unexpected_source_count <= 0`, or aggregate `count` over
`$.unexpected_sources` so the breach evidence (offender sample, #2976) names
each new source, leg, and port class.

Design notes:

- **Lab-agnostic.** No CIDRs or hostnames are baked in. `sanctioned_src` and
  `mgmt_nets` (a list of `{cidr, leg}`) are **required** params; `mgmt_ports`
  defaults to the vendor-generic 443/22/902/5480 class; `baseline_src` is
  optional. The domain-specific values live in the pinning Sensor's `params`.
- **Zero firewall change.** It classifies the live state table (the same
  zero-change source as the stage-1 report's `states` mode), so no counting /
  logging rule is required. The trade-off is snapshot semantics — a flow that
  opens and closes between reads is not captured; a cumulative logged-counter
  source is a separate, firewall-changing decision, out of scope here.
- **Failed read raises.** A non-zero `pfctl -ss` exit with no output raises so
  the dispatch fails and a pinned Sensor evaluates `unknown` rather than a
  false all-clear (the sensor contract: a refusal must fail the dispatch, not
  return a reading). An empty (exit 0) state table classifies to zeros.
- **Unparsed lines are counted, not dropped.** A *successful* read whose lines
  the parser cannot structure (a truncated/unknown form, or a NAT-translated
  first endpoint `addr:port (xlate:port) <dir> ...` — out of scope for this
  port-based classifier) is counted in the result's `unparsed_lines` scalar and
  never classified. A Sensor pins `$.unparsed_lines <= 0` alongside the source
  assertion so an unrecognised state cannot be silently absorbed into a clean
  summary (the second false-all-clear the field-order fix closed for #252).
- **Server side is resolved from the direction arrow (#3471).** The `pfctl -ss`
  arrow points from the connection initiator (client) to the listener
  (server): `->` puts the server on the `dst` endpoint, `<-` on the `src`.
  `_server_client_split` honours it and tests only the arrow-resolved server
  side for a management port, so an **outbound** connection whose local client
  ephemeral source port coincidentally lands in the management-port set is not
  inverted into a phantom inbound management-port hit (a false `unexpected`
  source). The port heuristic (server = whichever endpoint carries a management
  port, `dst` first) survives only as a fallback for the bidirectional `<->` /
  missing-arrow form, which real `pfctl -ss` TCP states do not emit.
- **Same-subnet caveat.** A pfSense only sees flows it routes between two of
  its segments; same-subnet flows are invisible. The op echoes this on every
  result's `caveat` field.

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
- Task #3232: `pfsense.nat.delete` + `pfsense.alias.delete` — the first two
  governed destructive deletes. Decision:
  `docs/decisions/governed-delete-operations.md`.
- Task #3313: `pfsense.route.static.delete` + `pfsense.gateway.delete` +
  `pfsense.alias.member.remove` — the three teardown-inverse governed deletes
  that reverse a bring-up (retire a route / gateway; trim one member out of a
  shared alias without deleting it). `gateway.delete` refuses fail-closed while
  any static route / gateway group / default-gateway setting still uses the
  gateway; `alias.member.remove` refuses `last_member` so a shared alias is
  never emptied into deletion.
- Task #3954: `pfsense.user.list` + `pfsense.route.static.list` — safe reads
  with an allow-list of fields, so nobody needs `pfsense.config.show` to check
  a user or a static route.
- Parent initiative: #370 (G3.7 tier-3 standalone connectors).
- Bind9 connector (canonical typed-SSH reference): `docs/codebase/connectors-bind9.md`.
- `SshConnector` adapter: `backend/src/meho_backplane/connectors/adapters/ssh.py`.
