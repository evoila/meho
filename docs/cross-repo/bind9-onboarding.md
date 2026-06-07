<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Bind9 op surface onboarding — operator recipe

> Operator-facing recipe for the G3.4 `bind9-ssh-9.x` op surface — the
> `meho bind9 …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/bind9-dns.sh` wrapper. The op
> handlers live in
> [`backend/src/meho_backplane/connectors/bind9/`](../../backend/src/meho_backplane/connectors/bind9/);
> the engineering-facing companion is
> [`docs/codebase/connectors-bind9.md`](../codebase/connectors-bind9.md).
> This doc is the cookbook every RDC operator reads when retiring the
> bash wrapper in favour of `meho bind9 …`.

## What this surface is

The `bind9-ssh-9.x` connector is a **typed** connector: hand-coded
handlers over `asyncssh`, registered into the G0.6
`endpoint_descriptor` table at backplane startup. It dispatches under
the `(product="bind9", version="9.x", impl_id="bind9-ssh")` registry
triple — the connector id `bind9-ssh-9.x`.

The `-ssh` discriminator in the impl_id leaves room for a future
`bind9-rndc-9.x` or `bind9-rest-9.x` sibling without breaking the
resolver's tie-break ladder; v0.2 ships only the SSH transport (the
shape every consumer bind9 host already exposes through the same
SSH-bastion path operators reach for diagnostics).

The v0.2 op surface (Initiative
[#367](https://github.com/evoila/meho/issues/367)) is the working set
the consumer's `bind9-dns.sh` wrapper exercises daily:

| Group | Ops | Class |
| --- | --- | --- |
| `identity` | `bind9.about` | read-only fingerprint |
| `zone` | `bind9.zone.list`, `bind9.zone.read` | read-only |
| `record` | `bind9.record.get`, `bind9.record.add`, `bind9.record.remove` | 1 read + 2 atomic writes |
| `config` | `bind9.config.show`, `bind9.config.apply_file`, `bind9.config.apply_views`, `bind9.config.backup`, `bind9.config.reload` | 1 read + 4 writes |

Eleven ops total. Every op dispatches through the same
`POST /api/v1/operations/call` route the agent surface uses — auth,
policy, audit, broadcast, and JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6. The CLI verb tree is operator
ergonomics over that one route; it is **not** a separate data path and
is **not** mirrored on the MCP surface (CLAUDE.md postulate 5 — the
agent reaches every bind9 op via the narrow-waist meta-tools, see the
[agent meta-tool path](#the-agent-meta-tool-path) section).

## Prerequisites

- **An SSH-reachable bind9 host.** The connector talks bind9 over SSH
  using `asyncssh`. The target host must run `bind9` 9.18 or 9.20 with
  the standard Debian-family layout (`/etc/bind/named.conf`,
  `named-checkconf`, `rndc`); other distros work as long as those
  binaries are on `$PATH` for the SSH user.
- **`sudo` for the bind user.** Every write op (`record.add`,
  `record.remove`, `config.apply_*`, `config.backup`,
  `config.reload`) routes through the connector's
  `_remote_bash_with_sudo()` primitive (see the
  [Credential-leak postmortem](#credential-leak-postmortem) section —
  this is load-bearing). The SSH user must have `sudo` rights to
  manage `/etc/bind/` and run `rndc`; the operator-facing recipe is a
  `NOPASSWD: ALL` rule for the dedicated `meho-bind9` user, kept
  separate from the operator's personal account.
- **`python3` on the target.** The atomic-apply primitive's snapshot
  / rollback step stages files through a Python one-liner that walks
  `/etc/bind/` and computes a SOA-normalised checksum. Standard-library
  Python 3.x suffices — no third-party packages.
- **A registered bind9 target.** The CLI verbs take `--target <slug>`
  (e.g. `--target vcf-router-bind9`); the slug resolves server-side to
  a row in the `targets` table. The target carries `product="bind9"`,
  `host`, `port` (optional, defaults to 22), `secret_ref`
  (SSH credential dict), and `auth_model="shared_service_account"`.
- **`auth_model = shared_service_account`** — the only auth model
  v0.2 ships. The connector reads the SSH credential out of
  `secret_ref` directly (no Vault-fetch yet); the federation chain
  setup that lets a Vault-managed `secret_ref` resolve through
  [`vault-onboarding.md`](./vault-onboarding.md) lands in a future
  Initiative.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses across every verb. `meho bind9 …`
  needs `operator` role minimum (same gate as every dispatch verb);
  `read_only` callers get HTTP 403 on the write ops.

## Target + auth model

The shipped connector's auth model is `shared_service_account` over
SSH. The SSH credential is stored in **Vault** at a per-host path
under the operator tenant's KV-v2 mount, then materialised onto the
target row's `secret_ref` column. Two SSH credential shapes are
supported (in order of preference):

| Shape | `secret_ref` fields | When to use |
| --- | --- | --- |
| **Key auth (preferred)** | `username`, `ssh_private_key` (PEM-encoded text) | Production. Key auth means no sudo password is exposed to the operator's account. |
| **Password auth (fallback)** | `username`, `password` | Bootstrap / lab targets only; the password lives in Vault but every successful auth surfaces in the host's `auth.log`. |

The connector's
[`SshConnector._auth_config`](../../backend/src/meho_backplane/connectors/adapters/ssh.py)
reads `secret_ref` in that order; missing both fields raises
`ValueError` at dispatch time (fail-fast — never an opaque asyncssh
auth error).

### Storing the SSH credential in Vault

Use the tenant's KV-v2 path convention:
`secret/<tenant>/bind9/<host>`. The credential blob is the
`secret_ref` dict the target row will carry. Example via the v0.2
`meho vault kv put` (replaces `vault.sh PUT`):

```console
$ meho vault kv put --target rdc-vault secret \
    rdc-hetzner-dc/bind9/vcf-router-01 \
    --data @secret_ref.json
```

`secret_ref.json` shape (key auth):

```json
{
  "username": "meho-bind9",
  "ssh_private_key": "<PEM-encoded OpenSSH private key, one literal newline-escaped string from the host headers through the footers>"
}
```

The `ssh_private_key` value is the contents of the private-key file
(typically `~/.ssh/id_ed25519` for the dedicated `meho-bind9` user)
emitted as a single JSON string: keep the literal `\n` newlines, the
BEGIN / END headers, and the trailing newline intact. `asyncssh`'s
`import_private_key` parses any standard PEM-encoded key (Ed25519,
ECDSA, RSA, OpenSSH-format) the same way `ssh` does.

Or, for password fallback:

```json
{
  "username": "meho-bind9",
  "password": "<one-line-password>"
}
```

The `password` field is single-line by contract — `_remote_bash_with_sudo`
refuses a password containing `\n`, `\r`, or `\x00` (the failure mode
the 2026-05-04 leak chain exposed, see
[Credential-leak postmortem](#credential-leak-postmortem)).

### Registering the target

```yaml
# targets.yaml
targets:
  - name: vcf-router-bind9
    product: bind9
    host: bind9.lab.evba
    secret_ref: secret/rdc-hetzner-dc/bind9/vcf-router-01
    auth_model: shared_service_account
```

```console
$ meho targets import targets.yaml
```

Verify the target round-trips:

```console
$ meho targets probe vcf-router-bind9
ok — bind9 9.18.24 reachable; named-checkconf -p clean
$ meho bind9 about --target vcf-router-bind9
bind9-ssh-9.x bind9.about — status=ok (47ms)
  vendor:           isc
  product:          bind9
  version:          9.18.24
  build:            BIND 9.18.24-1+deb12u2-Debian
  os:               debian 12
  named_conf_path:  /etc/bind/named.conf
```

`probe` exercises the full ladder: TCP reachable → SSH handshake →
auth → `pgrep -x named` → `named-checkconf -p`. Failure modes each
carry a distinct `reason` field (`tcp_unreachable`,
`ssh_handshake_failed`, `auth_failed`, `named_not_running`,
`named_config_invalid`); read the failing field, fix the host, re-run
probe.

## The CLI verb surface

Every verb pre-bakes `connector_id="bind9-ssh-9.x"` so operators never
type the connector id. All verbs accept `--target <slug>` (required
for ops that resolve a bind9 target), `--json` (emit the full
`OperationResult` envelope for `jq`), and `--backplane <url>`
(override the URL from the last `meho login`). Exit codes mirror
`meho operation call`.

### Identity — `meho bind9 about`

```console
$ meho bind9 about --target vcf-router-bind9
$ meho bind9 about --target vcf-router-bind9 --json | jq .result
```

`about` returns vendor / product / version (parsed BIND triple) /
build (full `named -v` banner) / os / named_conf_path. Use it to
confirm the target before any higher-level op and to pick a
version-flavoured doc page.

### Zone browse — `meho bind9 zone …`

```console
$ meho bind9 zone list --target vcf-router-bind9
$ meho bind9 zone read evba.lab --target vcf-router-bind9
$ meho bind9 zone read evba.lab. --target vcf-router-bind9 --json
```

| Verb | op_id | Result |
| --- | --- | --- |
| `zone list` | `bind9.zone.list` | One row per zone declared in `named-checkconf -p` output; `{name, type, file}`. |
| `zone read <zone>` | `bind9.zone.read` | One row per rrset member; `{name, ttl, class, type, rdata}`. Resolves the zonefile path server-side — operators pass the zone name, not the filename. |

`zone read` parses the zonefile via `dns.zone.from_text` (dnspython)
so every record type is fully decoded (A, AAAA, CNAME, MX, TXT, NS,
SOA, SRV, …); the column-shaped output is consumed by `jq` for ad-hoc
diffing.

### Record ops — `meho bind9 record …`

```console
$ meho bind9 record get www.evba.lab --target vcf-router-bind9
$ meho bind9 record get mail.evba.lab --type AAAA --target vcf-router-bind9
$ meho bind9 record add esx-dc6.evba.lab 10.5.50.25 \
    --zone evba.lab --target vcf-router-bind9
$ meho bind9 record remove esx-dc6.evba.lab \
    --zone evba.lab --target vcf-router-bind9
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `record get <fqdn>` | `bind9.record.get` | `--type` defaults to `A`; supported: `A` / `AAAA` / `CNAME` / `MX` / `TXT`. Resolution is via `dig @localhost`, so views + cache hits behave as the rest of the world sees them. |
| `record add <fqdn> <ip>` | `bind9.record.add` | `safety_level=caution`, `op_class=write`. Routes through the atomic-apply primitive. `--zone` is optional; the handler resolves the owning zone via longest-suffix match. `--type` accepts `A` or `AAAA` only — CNAME / MX / TXT writes are out of scope for v0.2. |
| `record remove <fqdn>` | `bind9.record.remove` | `safety_level=caution`, `op_class=write`. Removes the A + AAAA rdatasets at `<fqdn>`. |

`record add` is the **1:1 replacement** for the consumer's
`bind9-dns.sh --add-a-record …` invocation. The wrapper composed a
zonefile edit + `rndc reload` + a dig-verify by hand; the MEHO op
routes the same flow through the atomic-apply primitive (see
[Atomic-apply contract](#the-atomic-apply-contract) below).

### Config browse + write — `meho bind9 config …`

```console
$ meho bind9 config show named.conf --target vcf-router-bind9
$ meho bind9 config show /etc/bind/named.conf.local --target vcf-router-bind9

$ meho bind9 config apply-file named.conf.options \
    ./local/named.conf.options --target vcf-router-bind9

$ meho bind9 config apply-views ./views/external.conf ./zones \
    --target vcf-router-bind9 \
    --verify-fqdn www.evba.lab \
    --primary-path named.conf.local

$ meho bind9 config backup --tag pre-migration --target vcf-router-bind9
$ meho bind9 config reload --target vcf-router-bind9
```

| Verb | op_id | Notes |
| --- | --- | --- |
| `config show <file>` | `bind9.config.show` | `<file>` may be absolute (must be lexically under the bind config root) or relative (resolved against it). Traversal / outside-root rejected pre-stage with no content leak. |
| `config apply-file <name> <local-src>` | `bind9.config.apply_file` | `safety_level=dangerous`, `op_class=write`. CLI reads `<local-src>` locally and stages it as the replacement for `<name>` on the target. |
| `config apply-views <local-views.conf> <zones-dir>` | `bind9.config.apply_views` | `safety_level=dangerous`, `op_class=write`. CLI reads `<local-views.conf>` (lands at `named.conf.local`) and every regular file under `<zones-dir>` (each lands at its relative path under `/etc/bind/`). `--verify-fqdn` adds a dig-verify post-reload; `--primary-path` selects which staged key drives the audit row's pre/post-op capture. |
| `config backup [--tag T]` | `bind9.config.backup` | `safety_level=safe`. Creates a UTC-timestamped tar.gz under `/var/backups/meho-bind9/`. `--tag` (optional) embeds a `[A-Za-z0-9._-]{1,64}` friendly tag in the filename. |
| `config reload` | `bind9.config.reload` | `safety_level=caution`, `op_class=write`. `rndc reload`. Use only when you needed to bypass the apply ops' automatic reload (rare). |

## The atomic-apply contract

Every write op except `config.backup` routes through the connector's
[`_atomic.py`](../../backend/src/meho_backplane/connectors/bind9/_atomic.py)
primitive. The contract:

1. **Snapshot.** Compute a SOA-normalising SHA256 checksum of
   `/etc/bind/`.
2. **Stage.** Drop the new file(s) into a working tree under a tmp
   directory on the target.
3. **Validate.** Run `named-checkconf -p` (and `named-checkzone` for
   record / zone writes) against the staged tree. Failure → rollback;
   `/etc/bind/` is byte-identical to the snapshot.
4. **Apply.** Atomically overlay the staged tree onto `/etc/bind/`
   (`find … -delete; tar -x` for `apply_views`; explicit `cp` +
   `chmod` + `chown` for `apply_file`).
5. **Reload.** `rndc reload`. Non-zero exit → rollback.
6. **Verify.** Either `named-checkconf -p` against the live config (no
   `--verify-fqdn`) or `dig @localhost <fqdn> +short` returning
   non-empty (with `--verify-fqdn`). Failure → rollback.

A failed validate / reload / verify produces a `connector_error`
envelope with `op_class=write` + the failing step name and an
`AtomicApplyError.step` field on the raised exception. The result
envelope carries `result_state_before` (a truncated preview of the
pre-op file content) and `result_state_after` so operators can diff
successful writes; the full pre/post-op bytes ride through the audit
row's `state_before` / `state_after` payload (read via
`meho audit show <id> --json`).

This is the structural protection against the 2026-05-04 / 2026-05-05
credential-leak surface — see
[Credential-leak postmortem](#credential-leak-postmortem) — and the
load-bearing reason `bind9-dns.sh` retires in favour of this connector.

## The agent meta-tool path

Agents never see `meho bind9 …` — those are operator-only CLI
ergonomics. Per [CLAUDE.md](../../CLAUDE.md) postulate 5, an agent
reaches every bind9 op through the narrow-waist meta-tools:

```text
search_connectors(query="bind9 dns")            → finds bind9-ssh-9.x
list_operation_groups(connector_id="bind9-ssh-9.x")
                                                → identity / zone / record / config
search_operations(
    connector_id="bind9-ssh-9.x",
    query="add a dns record",
    group="record",
)                                                → top hit: bind9.record.add
call_operation(
    connector_id="bind9-ssh-9.x",
    operation_id="bind9.record.add",
    target={"name": "vcf-router-bind9"},
    params={"fqdn": "esx-dc6.evba.lab",
            "ip": "10.5.50.25",
            "type": "A",
            "zone": "evba.lab"},
)
```

The agent's flow is always: pick connector → list operation groups →
search operations (optionally scoped to a group) → `call_operation`.
The CLI verb table and the `call_operation` params are 1:1 —
`meho bind9 record add …` and the `call_operation` call above
dispatch the identical route, audit row, and broadcast event. Each
op's `llm_instructions` payload (registered at
`register_typed_operation()` time) is what `search_operations`
surfaces to rank and guide the agent; it is reviewable in
[`backend/src/meho_backplane/connectors/bind9/ops_zone.py`](../../backend/src/meho_backplane/connectors/bind9/ops_zone.py)
/ `ops_record.py` / `ops_config.py`.

**Why no per-op MCP tools?** Eleven ops × per-tool registration adds
eleven entries to the agent's tool surface, fans out into more
duplicated parameter schemas, and re-implements the
search-then-dispatch flow the meta-tools already cover. The agent
surface stays the same five meta-tools across every connector —
CLAUDE.md postulate 5's narrow-waist invariant.

## Migrating off `bind9-dns.sh`

The consumer's
[`scripts/bind9-dns.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/bind9-dns.sh)
retires in favour of the verb tree above:

| Wrapper invocation | `meho bind9 …` replacement | Notes |
| --- | --- | --- |
| `bind9-dns.sh --target <h> --add-a-record <fqdn> <ip> --zone <zone>` | `meho bind9 record add <fqdn> <ip> --zone <zone> --target <slug>` | The wrapper's `<host>` becomes a registered target slug. Atomic-apply rollback is now built-in (the wrapper bolted on its own check + reload step). |
| `bind9-dns.sh --target <h> --remove-a-record <fqdn> --zone <zone>` | `meho bind9 record remove <fqdn> --zone <zone> --target <slug>` | |
| `bind9-dns.sh --target <h> --get-record <fqdn>` | `meho bind9 record get <fqdn> --target <slug>` | |
| `bind9-dns.sh --target <h> --list-zones` | `meho bind9 zone list --target <slug>` | |
| `bind9-dns.sh --target <h> --read-zone <zone>` | `meho bind9 zone read <zone> --target <slug>` | |
| `bind9-dns.sh --target <h> --show-config <file>` | `meho bind9 config show <file> --target <slug>` | |
| `bind9-dns.sh --target <h> --reload` | `meho bind9 config reload --target <slug>` | Only needed for ad-hoc reloads; `apply_*` reload as part of their flow. |
| (no wrapper for the apply / backup flows) | `meho bind9 config apply-file …` / `apply-views …` / `backup …` | New surface; the wrapper hand-built zonefile edits with `sed`. The MEHO ops produce a structured audit row + atomic-apply rollback. |

Migration discipline: run the `meho bind9 …` form alongside the
wrapper for an overlap window, diff the outputs, retire the wrapper
call site. The MEHO path adds the full audit row + broadcast event
the bash pattern never had — that audit coverage is the point.

What the wrappers did that `meho bind9` deliberately does **not** do
(out of scope for v0.2 — keep the wrapper for these until a future
Initiative lands them):

- DNSSEC key management — out of scope for v0.2.
- TSIG / `rndc` admin beyond `reload` — out of scope; the wrapper's
  `--rndc-status` shape is covered by `meho bind9 config reload`'s
  `result_state_after` payload (the live rndc-status snapshot).
- Host-key pinning — deferred. The SSH adapter sets `known_hosts=None`
  for v0.2 (matches every other typed-SSH connector); a future
  Vault-managed known-hosts store is the right shape.

## Credential-leak postmortem

The `_remote_bash_with_sudo()` primitive on
`Bind9Connector` is **structural protection** against a documented
credential leak. Read this section before reviewing any future
PR that touches the connector's transport code.

**The incidents** (Initiative
[#367](https://github.com/evoila/meho/issues/367) WI1):

- **2026-05-04** — the consumer's `bind9-dns.sh` invoked
  `ssh <host> "echo '<password>' | sudo -S bash -c '<remote-script>'"`.
  The `<password>` value landed in the remote argv. A concurrent
  `ps aux` from any unprivileged remote process captured the
  password.
- **2026-05-05** — a follow-up attempt swapped to
  `ssh <host> "sudo -S bash -c '<script>'" <<< '<password>'`. The
  password moved off `argv` but the operator's bash history kept the
  `<<< '<password>'` here-string verbatim in `~/.bash_history` for
  every operator invocation.
- **External coordination**:
  [evoila-bosnia/claude-rdc-hetzner-dc#86](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/86)
  tracks the rotation chain + the consumer-side `bind9-dns.sh`
  retirement.

**The encoded fix.** `_remote_bash_with_sudo()`:

1. Fixes the remote argv as a **constant string** — `sudo -S -p ''
   bash -s` — with no caller interpolation. A grep for `sudo` in the
   `connectors/` tree finds exactly one match (the helper itself).
2. Builds the stdin payload as `f"{sudo_password}\\n{script}\\n"`.
   The password is the first stdin line; `sudo -S` consumes it; `bash
   -s` reads the rest. The password never appears in `argv`, never
   appears in any local shell history, never appears in the
   structured log event (the helper logs `cmd_len` + `exit_status`
   only).
3. Refuses a multi-line password (`\n` / `\r` / `\x00` in the value)
   pre-call — the single-line invariant is what makes the stdin
   framing unambiguous.
4. **Codebase-wide invariant.** The E2E acceptance harness in
   [`tests/integration/test_g3_4_bind9_e2e.py`](../../backend/tests/integration/test_g3_4_bind9_e2e.py)
   walks `backend/src/meho_backplane/connectors/` looking for any
   `sudo` literal outside the bind9 package; a regression on a
   sibling connector that hand-rolls a `sudo -S` invocation fails
   the test with a pointer at the offending file. The mis-ordered-
   payload shape is **unrepresentable** in the codebase.

Every future bind9 op + every sibling typed-SSH connector that needs
`sudo` must route through `_remote_bash_with_sudo()`. There is no
escape hatch.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no backplane URL configured` (exit 2) | Never logged in / no `--backplane`. | `meho login <url>` or pass `--backplane <url>`. |
| `auth_expired` / stored token rejected | Keycloak token expired; refresh failed. | `meho login <url>` again. |
| `status=error … unknown_op` | Connector_id drift — typed it as `bind9-9.x` instead of `bind9-ssh-9.x`. | Use `bind9-ssh-9.x`; the `-ssh` impl_id discriminator is part of the connector_id grammar. The CLI bakes the right id in; the agent meta-tool path needs the full string. |
| `status=error … operation not found` | The typed-op registrar didn't run (lifespan crash) or op_id drift. | Check the backplane started cleanly; verify `meho connector list` shows `bind9-ssh-9.x` with eleven enabled ops. |
| `status=denied` on a write op | `read_only` role, or a policy gate denied. | Use an `operator`-role token; the policy gate on `safety_level=dangerous` ops can require approval. |
| `record.add` returns `connector_error … step=verify` | The write landed in the zonefile + reload succeeded but `dig @localhost <fqdn>` returned empty post-reload. | Confirm the zone view actually serves the new fqdn (split-horizon configs sometimes need the verify FQDN scoped via `--verify-fqdn`); the atomic-apply primitive rolled back, so `/etc/bind/` is byte-identical to before the call. |
| `apply_views` returns `connector_error … step=checkconf` | Staged tree failed `named-checkconf -p`. | The error text carries the line + reason; fix the local file, re-run. The primitive's rollback already restored `/etc/bind/`. |
| `config.backup` returns `path … invalid_params` | `--tag` contained a character outside `[A-Za-z0-9._-]`. | Tags are restricted to those characters so they can't inject shell metacharacters or path separators in the on-disk filename. |
| `bind9 about` fails with `tcp_unreachable` | SSH port not open from the backplane. | Diagnose via `meho targets probe` — the failure mode tree narrows it to TCP / SSH / auth / named / checkconf. |

## References

- Initiative: [#367 G3.4 bind9-9.x typed-SSH connector](https://github.com/evoila/meho/issues/367); Goal [#214](https://github.com/evoila/meho/issues/214) (G3 connector parity).
- Tasks that shipped this surface: [#587](https://github.com/evoila/meho/issues/587) (connector skeleton + safe-sudo primitive), [#588](https://github.com/evoila/meho/issues/588) (read ops), [#589](https://github.com/evoila/meho/issues/589) (atomic-apply primitive + record writes), [#590](https://github.com/evoila/meho/issues/590) (config-write ops), [#591](https://github.com/evoila/meho/issues/591) (CLI + E2E + this doc).
- Engineering companion: [`docs/codebase/connectors-bind9.md`](../codebase/connectors-bind9.md).
- Op handlers: [`backend/src/meho_backplane/connectors/bind9/`](../../backend/src/meho_backplane/connectors/bind9/) (`connector.py` skeleton + safe-sudo, `ops_zone.py`, `ops_record.py`, `ops_config.py`, `_atomic.py`). CLI verbs: [`cli/internal/cmd/bind9/`](../../cli/internal/cmd/bind9/).
- E2E acceptance harness: [`backend/tests/integration/test_g3_4_bind9_e2e.py`](../../backend/tests/integration/test_g3_4_bind9_e2e.py); containerised fixture: [`backend/tests/integration/test_connectors_bind9_container.py`](../../backend/tests/integration/test_connectors_bind9_container.py).
- Consumer wrapper retired: [`scripts/bind9-dns.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/bind9-dns.sh).
- Credential-leak postmortems: [evoila-bosnia/claude-rdc-hetzner-dc#86](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/86) tracks the 2026-05-04 / 2026-05-05 chain + the rotation history that motivated the `_remote_bash_with_sudo()` safe-by-construction design.
- BIND 9 docs: <https://bind9.readthedocs.io/en/v9.18/> (9.18 — Debian bookworm default), <https://bind9.readthedocs.io/en/v9.20/> (9.20 — current Stable Release).
- Agent meta-tool surface: G0.5 MCP server [#226](https://github.com/evoila/meho/issues/226); `search_operations` / `call_operation`. Onboarding-doc precedents: [`vault-onboarding.md`](./vault-onboarding.md), [`kubernetes-onboarding.md`](./kubernetes-onboarding.md), [`docs/cross-repo/README.md`](./README.md).
