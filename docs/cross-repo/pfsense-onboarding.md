<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# pfSense op surface onboarding — operator recipe

> Operator-facing recipe for the G3.7 `pfsense-ssh-2.7` op surface —
> the `meho pfsense …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/pfsense.sh` wrapper. The op
> handlers live in
> [`backend/src/meho_backplane/connectors/pfsense/`](../../backend/src/meho_backplane/connectors/pfsense/);
> the engineering-facing companion is
> [`docs/codebase/connectors-pfsense.md`](../codebase/connectors-pfsense.md)
> (if present). This doc is the cookbook every RDC operator reads when
> retiring the bash wrapper in favour of `meho pfsense …`.

## What this surface is

The `pfsense-ssh-2.7` connector is a **typed** connector: hand-coded
handlers over `asyncssh`, registered into the G0.6
`endpoint_descriptor` table at backplane startup. It dispatches under
the `(product="pfsense", version="2.7", impl_id="pfsense-ssh")` registry
triple — the connector id `pfsense-ssh-2.7`.

The `-ssh` discriminator in the impl_id leaves room for a future
`pfsense-rest-2.7` or `pfsense-api-v3` sibling without breaking the
resolver's tie-break ladder; v0.1 ships only the SSH transport.

The v0.1 op surface (Initiative
[#370](https://github.com/evoila/meho/issues/370)) is the read-only
working set operators use for daily firewall and network diagnostics:

| Group | Ops | Class |
| --- | --- | --- |
| `identity` | `pfsense.about` | read-only fingerprint |
| `config` | `pfsense.version` | read-only version summary |
| `firewall` | `pfsense.firewall.rules`, `pfsense.firewall.state` | read-only |
| `nat` | `pfsense.nat.rules` | read-only |
| `network` | `pfsense.interface.list`, `pfsense.gateway.list` | read-only |
| `config` | `pfsense.config.show` | read-only XML dump |

Eight ops total. Every op dispatches through the same
`POST /api/v1/operations/call` route the agent surface uses — auth,
policy, audit, broadcast, and JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6. The CLI verb tree is operator ergonomics
over that one route; it is **not** a separate data path and is **not**
mirrored on the MCP surface (CLAUDE.md postulate 5 — the agent reaches
every pfSense op via the narrow-waist meta-tools, see the
[agent meta-tool path](#the-agent-meta-tool-path) section).

## Prerequisites

- **An SSH-reachable pfSense 2.7 host.** The connector talks pfSense
  over SSH using `asyncssh`. The target must run pfSense 2.7 and expose
  an SSH port with key-based auth for a user that has **shell access**.
- **Shell access — not the console menu.** pfSense's `admin` user by
  default drops into an interactive menu (`pfSense`) rather than a
  shell. The connector's `probe` method asserts shell access by running
  `cat /etc/version` — if the session lands in the console menu the
  probe returns `no_shell_access` and every op will fail. To fix this:
  - Create a dedicated `meho-pfsense` user whose shell is `/bin/sh`
    (System → User Manager → add user, set shell to `/bin/sh`).
  - Or configure the `admin` user's shell to `/bin/sh` in the pfSense
    user manager if your policy allows it.
  - The `meho-pfsense` user needs sudo permissions if any write ops
    are added in a future initiative; for the current read-only surface
    no sudo is required.
- **pfctl + ifconfig + cat accessible.** The connector runs `pfctl -sr`,
  `pfctl -ss`, `pfctl -sn`, `ifconfig -a`, and `cat /cf/conf/config.xml`
  via SSH. These commands are available in pfSense's base shell. The
  SSH user must be able to run `pfctl` read operations — on pfSense 2.7,
  the default `admin` user can; non-admin users may need a `doas` rule.
- **A registered pfSense target.** The CLI verbs take `--target <slug>`.
  The slug resolves server-side to a row in the `targets` table. The
  target carries `product="pfsense"`, `host`, `port` (optional, defaults
  to 22), `secret_ref` (Vault path), and `auth_model`.
- **`auth_model = shared_service_account`** — the only auth model v0.1
  ships. The connector reads the SSH credential out of `secret_ref`
  (resolved from Vault); password auth is not supported — only SSH
  private key auth works (see [Known gotchas](#known-gotchas)).
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb.

## Target + auth model — Vault credential setup

The shipped connector's auth model is `shared_service_account` over
SSH key auth. The SSH credential is stored in Vault at a per-host path
under the operator tenant's KV-v2 mount, then materialized onto the
target row's `secret_ref` column.

**Only SSH key auth is supported.** The pfSense SSH server will accept
a password for the `admin` user, but the connector deliberately refuses
password credentials — the `_auth_config` method raises `ValueError` for
any secret missing `ssh_private_key`. This is intentional: password auth
exposes the credential in the SSH handshake log and in `auth.log`.

### Creating the dedicated SSH user in pfSense

1. In the pfSense web UI: System → User Manager → Add.
2. Set username to `meho-pfsense`.
3. Under "Effective Privileges", add "System: Shell account access".
4. Under the user, click "Add SSH Key" and paste the public half of the
   key you'll store in Vault (see below).
5. Set the user shell to `/bin/sh` (or `/usr/local/bin/bash` if
   installed) — the connector asserts shell access.
6. Save and apply.

### Generating the SSH key pair

```console
$ ssh-keygen -t ed25519 -f ~/.ssh/meho-pfsense-hetzner-dc -C "meho-pfsense@rdc-hetzner-dc" -N ""
```

The public key (`~/.ssh/meho-pfsense-hetzner-dc.pub`) goes into
pfSense's user manager (step 4 above). The private key is stored in
Vault.

### Storing the SSH credential in Vault

Use the tenant's KV-v2 path convention:
`secret/<tenant>/pfsense/<host>`. Example via `meho vault kv put`:

```console
$ meho vault kv put --target rdc-vault secret \
    rdc-hetzner-dc/pfsense/pfsense-01 \
    --data @pfsense_secret_ref.json
```

`pfsense_secret_ref.json` shape:

```json
{
  "username": "meho-pfsense",
  "ssh_private_key": "<PEM-encoded OpenSSH private key>"
}
```

The `ssh_private_key` value is the contents of the private-key file
(e.g. `~/.ssh/meho-pfsense-hetzner-dc`) as a single JSON string with
literal `\n` newlines, `BEGIN` / `END` headers, and the trailing
newline. `asyncssh`'s `import_private_key` parses Ed25519, ECDSA, RSA,
and OpenSSH-format PEM keys.

### Registering the target

```yaml
# targets.yaml
targets:
  - name: pfsense-hetzner-dc
    product: pfsense
    host: 10.5.1.1
    port: 22
    secret_ref: secret/rdc-hetzner-dc/pfsense/pfsense-01
    auth_model: shared_service_account
```

```console
$ meho targets import targets.yaml
```

Verify the target is reachable:

```console
$ meho targets probe pfsense-hetzner-dc
ok — pfsense 2.7.2-RELEASE reachable; shell access confirmed

$ meho pfsense about --target pfsense-hetzner-dc
pfsense-ssh-2.7 pfsense.about — status=ok (83ms)
  vendor:     netgate
  product:    pfsense
  version:    2.7.2-RELEASE
  build:      pfSense-CE-2.7.2-RELEASE-amd64
  kernel:     FreeBSD 14.1-RELEASE-p5
```

`probe` exercises the full ladder: TCP reachable → SSH handshake →
key auth → `cat /etc/version` (non-empty stdout asserts shell access).
Failure modes each carry a distinct `reason` field
(`tcp_unreachable`, `ssh_handshake_failed`, `auth_failed`,
`no_shell_access`); read the failing field, fix the host, re-run probe.

## Known gotchas

### admin user lands in the pfSense console menu

The `admin` user's default shell on pfSense is the interactive console
menu (`/etc/rc.initial`). If you SSH in as `admin` and see the pfSense
menu rather than a shell prompt, the connector will probe `no_shell_access`
and every `call_operation` dispatch will fail at the SSH exec step.

**Fix:** Create a dedicated `meho-pfsense` user with shell `/bin/sh` as
described in [Creating the dedicated SSH user](#creating-the-dedicated-ssh-user-in-pfsense).
Alternatively, change the `admin` user's shell in System → User Manager
if your policy allows.

### Password auth is not accepted

The connector raises `ValueError` for any credential missing
`ssh_private_key` — even if the pfSense host would accept a password.
This is intentional and non-overrideable in v0.1. Always generate an
SSH key pair for the dedicated user; do not store `password` in the
Vault secret.

### pfctl requires no special privileges on pfSense 2.7

pfSense 2.7's `pfctl -sr` / `pfctl -ss` / `pfctl -sn` return output
for any user with a shell session, not just root. You do not need `sudo`
for the read ops. If you see a `pfctl: /dev/pf: Permission denied` error,
the SSH user is missing the "System: Shell account access" privilege or
the `pf` subsystem is stopped (probe returns `ok` but `pfctl` calls fail).

### firewall.state can produce large output

On busy pfSense firewalls the connection-state table (`pfctl -ss`) can
contain tens of thousands of entries. The connector streams all rows to
the dispatcher; the CLI's human render caps at 20 rows. Use `--json` and
pipe through `jq` for filtering:

```console
$ meho pfsense firewall state --target pfsense-hetzner-dc --json \
    | jq '.result.rows[] | select(.proto=="tcp") | .dst' | sort -u
```

When the JSONFlux reducer is configured with a row-count threshold, the
dispatcher will produce a `ResultHandle` for large state tables (the
agent can then page via `result_describe` / `result_query`).

### config.xml contains sensitive data

`pfsense.config.show` returns the full `/cf/conf/config.xml` as a string
field in the OperationResult. The config includes VPN keys, user password
hashes, and pre-shared secrets. The CLI's human render caps at 40 lines;
`--json` returns the full content. Treat the JSON output as sensitive and
avoid persisting it in plain text.

## The CLI verb surface

Every verb pre-bakes `connector_id="pfsense-ssh-2.7"` so operators never
type the connector id. All verbs accept `--target <slug>` (required),
`--json` (emit the full `OperationResult` envelope for `jq`), and
`--backplane <url>` (override the URL from the last `meho login`).
Exit codes mirror `meho operation call` (0=ok, 1=error/denied,
2=auth_expired, 3=unreachable, 4=unexpected).

### Identity — `meho pfsense about`

```console
$ meho pfsense about --target pfsense-hetzner-dc
$ meho pfsense about --target pfsense-hetzner-dc --json | jq .result
```

Returns vendor / product / version / build / kernel from `/etc/version`.
Use before issuing higher-level ops to confirm reachability and version.

### Version — `meho pfsense version`

```console
$ meho pfsense version --target pfsense-hetzner-dc
$ meho pfsense version --target pfsense-hetzner-dc --json | jq .result.version
```

Returns version / build / kernel without the full FingerprintResult
envelope. Prefer `about` when vendor + product confirmation is needed.

### Firewall — `meho pfsense firewall rules` / `state`

```console
# List filter rules (pfctl -sr)
$ meho pfsense firewall rules --target pfsense-hetzner-dc

# List connection state table (pfctl -ss; cap at 20 rows in human mode)
$ meho pfsense firewall state --target pfsense-hetzner-dc

# Pipe JSON through jq for filtering
$ meho pfsense firewall rules --target pfsense-hetzner-dc --json \
    | jq '.result.rows[] | select(.action=="block")'
```

`rules` parses `pfctl -sr` into `{action, direction, rule}` rows.
`state` parses `pfctl -ss` into `{proto, iface, src, direction, dst, state}` rows.

### NAT — `meho pfsense nat rules`

```console
$ meho pfsense nat rules --target pfsense-hetzner-dc
$ meho pfsense nat rules --target pfsense-hetzner-dc --json | jq '.result.rows[]'
```

Parses `pfctl -sn` into `{action, direction, rule}` rows. Actions:
`nat`, `rdr`, `binat`, `no_nat`, `no_rdr`.

### Network — `meho pfsense network interface` / `gateway`

```console
# List interfaces (ifconfig -a)
$ meho pfsense network interface --target pfsense-hetzner-dc

# List gateways (from config.xml)
$ meho pfsense network gateway --target pfsense-hetzner-dc
$ meho pfsense network gateway --target pfsense-hetzner-dc --json \
    | jq '.result.rows[] | select(.defaultgw)'
```

`interface` parses `ifconfig -a` into `{name, mtu, inet, inet6, ether, status, media}` rows.
`gateway` parses `<gateways>` from `config.xml` into `{name, interface, gateway, monitor, descr, defaultgw}` rows.

### Config — `meho pfsense config show`

```console
# Print first 40 lines of config.xml with length summary
$ meho pfsense config show --target pfsense-hetzner-dc

# Extract full XML to a file
$ meho pfsense config show --target pfsense-hetzner-dc --json \
    | jq -r .result.config_xml > pfsense-backup-$(date +%Y%m%d).xml
```

Returns the raw `/cf/conf/config.xml` content and its character length.
For structured gateway data, prefer `meho pfsense network gateway`.

## The agent meta-tool path

Per CLAUDE.md postulate 5, these CLI verbs are **operator-only
ergonomics** — they are not mirrored on the MCP surface. Agents reach
pfSense ops via the same narrow-waist `search_operations` + `call_operation`
meta-tools used for every other connector:

```
search_operations(connector_id="pfsense-ssh-2.7", query="firewall rules")
→ [{"op_id": "pfsense.firewall.rules", "summary": "List the active pfSense firewall filter rules from pfctl.", ...}]

call_operation(op_id="pfsense.firewall.rules", target={"name": "pfsense-hetzner-dc"}, params={})
→ OperationResult{status="ok", result={"rows": [...], "total": 2}}
```

The `llm_instructions` on each op provide agent-readable guidance:

| Op | `when_to_use` summary |
| --- | --- |
| `pfsense.about` | Identify pfSense version and confirm SSH reachability |
| `pfsense.version` | Get version without the full FingerprintResult envelope |
| `pfsense.firewall.rules` | Inspect active filter rules from `pfctl -sr` |
| `pfsense.firewall.state` | List active connection-state table from `pfctl -ss` |
| `pfsense.nat.rules` | Inspect active NAT ruleset from `pfctl -sn` |
| `pfsense.interface.list` | Get IP/MAC/MTU/status for each interface |
| `pfsense.gateway.list` | Get routing gateway config from `config.xml` |
| `pfsense.config.show` | Export or inspect the full `config.xml` |

## Wrapper-flip recipe — retiring `scripts/pfsense.sh`

The consumer's `scripts/pfsense.sh` wrapper calls `pfctl` and friends
over SSH with hard-coded credentials. Replace each invocation with the
`meho pfsense` verb:

| Old `pfsense.sh` invocation | New `meho pfsense` equivalent |
| --- | --- |
| `./scripts/pfsense.sh --about` | `meho pfsense about --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --version` | `meho pfsense version --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --firewall-rules` | `meho pfsense firewall rules --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --firewall-state` | `meho pfsense firewall state --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --nat-rules` | `meho pfsense nat rules --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --interfaces` | `meho pfsense network interface --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --gateways` | `meho pfsense network gateway --target pfsense-hetzner-dc` |
| `./scripts/pfsense.sh --config-show` | `meho pfsense config show --target pfsense-hetzner-dc` |

Once every calling site in `evoila-bosnia/claude-rdc-hetzner-dc` is
migrated:

1. Add the pfSense target with `meho targets import` (see above).
2. Store the SSH key in Vault with `meho vault kv put`.
3. Run `meho targets probe pfsense-hetzner-dc` to confirm end-to-end
   connectivity.
4. Remove `scripts/pfsense.sh` from the consumer repo and update any
   CI or runbook references.

## Goal #214 G3.7 pfSense checklist

- [x] G3.7-T1 (#844) — pfSense connector skeleton + `pfsense.about` canary op
- [x] G3.7-T2 (#847) — pfSense 7 read ops (`pfctl`/config.xml parsed) via `register_typed_operation` + JSONFlux state handle
- [x] G3.7-T3 (#850) — pfSense CLI verbs + MCP review + recorded-fixture/fake-shell E2E + this onboarding doc
