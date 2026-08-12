# Connector: rke2 (rke2-1.x / `rke2-ssh`)

## Overview

The `rke2` connector is the typed `Connector` subclass that dispatches
operator-facing RKE2 **node-OS-lifecycle** operations over plain SSH. It is
registered under the `(product="rke2", version="1.x", impl_id="rke2-ssh")`
registry triple and is a child of the shared `SshConnector` adapter
(`connectors/adapters/ssh.py`), the same transport base as `Bind9Connector`
(G3.4) and `HolodeckConnector` (G3.8/G3.18).

RKE2 cluster nodes expose **no MEHO REST API**; the only operator surface is
SSH to the node OS. The `kubernetes` connector is Kubernetes-API-only (~25
ops) and cannot reach a node at the OS level. Initiative #2172 adds a
governed, typed, path-bounded SSH node-OS-lifecycle op surface so cluster-node
maintenance (RKE2 join-token rotation, service restarts, config edits) runs
through one identity + audit plane instead of an untracked local SSH wrapper
(the field case: `claude-rdc-hetzner-dc#615`).

T1 (#2221) ships the **connector scaffold + the read-only posture tier only**:

- `rke2.about` — the identity canary. `rke2 --version` + `/etc/os-release`
  wrapped in the standard fingerprint/`_assert_reachable` shape.
- `rke2.posture.show` — the read-only posture tier. `stat`s the RKE2
  config-file modes under `/etc/rancher/rke2/` and the on-disk server
  join-token presence, **with the token value never read** (redacted by
  construction). Each path carries a three-state verdict —
  `present` / `absent` / `unknown` (#2698, see
  [Posture tri-state](#posture-tri-state-2698)).

A later read op (#2854) reads config **content** rather than modes:

- `rke2.node.config.get` — the redacted **config-content** read. `cat`s one
  bounded `/etc/rancher/rke2/*.yaml` file (default `config.yaml`) over SSH —
  the same read + `yaml.safe_load` step `rke2.node.config.update` runs — and
  returns the parsed top-level mapping so an operator can verify `tls-san` /
  `datastore-endpoint` / `node-taint` before or after a patch (the
  `claude-rdc-hetzner-dc#615` re-confirm-config step). Secret-bearing keys are
  **redacted, not withheld**: `token` / `agent-token` and the etcd S3
  credentials (`etcd-s3-access-key` / `etcd-s3-secret-key` /
  `etcd-s3-session-token`) become `***redacted***`, and `datastore-endpoint`
  keeps its host/port/db while masking only the `user:pass@` DSN userinfo. The
  masked key **names** are surfaced in `redacted_keys`. The `path` is confined
  by the write op's own `bound_config_path` (traversal rejected before any SSH
  round-trip — no new confinement logic); non-mapping / invalid YAML returns a
  structured `error`. See [Redaction guarantee](#redaction-guarantee).

All three `safety_level="safe"` / `requires_approval=false`. `rke2.about` and
`rke2.posture.show` take no params; `rke2.node.config.get` takes one optional
path-bounded `path`.

T2 (#2429) adds the **first approval-gated write op** (in the
`rke2-token-write` group):

- `rke2.token.rotate` — rotates the RKE2 server join token cluster-wide via
  `rke2 token rotate` over sudo-SSH. `safety_level="dangerous"`,
  `requires_approval=True`. Takes **no parameters and no token value**: the
  new token is minted server-side, the OLD token is read on-disk as root
  inside the rotate script, and the new token is written to Vault — only a
  **pointer** to the Vault location plus non-secret metadata (`rotated` /
  `node` / `exit_status`) is returned. A read-only fingerprint gate refuses a
  non-server node, an inactive `rke2-server`, or a below-floor / known-bad
  (`v1.27.10+rke2r1`) RKE2 version before any mutation.

T3 (#2430) adds two more **approval-gated node-write ops**
(`safety_level="dangerous"` / `requires_approval=true`), both in the shared
`rke2-node-write` group:

- `rke2.node.service.restart` — restarts EXACTLY one allow-listed unit
  (`rke2-server` / `rke2-agent`) via `systemctl restart <UNIT>` and
  health-gates on `systemctl is-active`. The unit is a schema `enum`
  re-checked against a module-level frozenset in the handler (fail-closed,
  the proxmox method-allowlist mold); no other unit and no arbitrary
  `systemctl` action.
- `rke2.node.config.update` — a **backplane-owned key merge** of a bounded
  `/etc/rancher/rke2/*.yaml` file. The handler reads + parses the current
  YAML in-process, applies the operator's key-level `patch`
  (`semantics: merge|replace`), validates it re-parses, and writes it back
  atomically (temp under `/etc/rancher/rke2`, `chmod 0600` + `chown
  root:root`, `mv`). No host-side `sed`/`yq`, no arbitrary-file-write
  primitive. RKE2 config is inert until a restart, so this op does **not**
  restart — it returns `restart_required: true` and changed key **names**
  only (never a value; the config body carries `token:` join credentials).

T4 (#2431) + #2853 add the **safe, non-gated snapshot tier** (both in the
`rke2-etcd-snapshot` group), sharing one embedded-etcd-server precondition
guard:

- `rke2.etcd-snapshot.save` (T4 #2431) — triggers an on-demand managed-etcd
  snapshot on a server node (`rke2 etcd-snapshot save`, embedded-etcd only).
  It is `safety_level="safe"` / `requires_approval=false` because it is
  read-only with respect to *running* cluster state (it copies etcd to a file
  on disk) and returns only a snapshot name + path, never etcd contents. An
  optional `name` param is charset-bounded to `^[A-Za-z0-9._-]+$` at the
  schema boundary AND re-checked in the handler. It is **active** (it writes a
  file), so it is deliberately NOT `read-only`-tagged.
- `rke2.etcd-snapshot.list` (#2853) — enumerates the managed-etcd snapshots
  that already exist on a server node (`rke2 etcd-snapshot list`). Takes no
  operator params and returns `{snapshots: [{name, location, size_bytes,
  created_at}, …]}` — a genuine read (it enumerates, mutating nothing), so it
  DOES carry the `read-only` tag. It is the read counterpart `.save` lacked:
  the way to **confirm a fresh snapshot landed after a token rotation** (a
  snapshot taken before the rotation was made with the retired token — the
  `claude-rdc-hetzner-dc#615` runbook step). The set-shaped result is
  materialised into a JSONFlux result handle by the central reducer above its
  row/byte threshold. See [List output parsing](#list-output-parsing-2853).

Initiative #2833 (read-op coverage wave 2, #2852) adds a **second safe
read-only op** alongside the posture tier, in a new `rke2-service-read` group:

- `rke2.node.service.status` — reports the **live systemd state** of the
  `rke2-server` / `rke2-agent` units without mutating anything. Runs one
  read-only `systemctl show --all -p LoadState,ActiveState,SubState,ExecMainStartTimestamp,NRestarts <unit>`
  round-trip over the fixed unit pair and returns, per unit, `load_state`
  (`loaded` vs `not-found`), `active_state` / `sub_state` (the running
  signal), `since` (the systemd `ExecMainStartTimestamp` string), and
  `restart_count` (systemd `NRestarts`, the crash-loop counter). A node runs
  exactly one of the two units; the other reports `load_state: not-found`
  (every other field null), so the result also reveals the node's role. It
  answers "is `rke2-server` actually up, since when, and is it crash-looping?"
  when the Kubernetes API server itself is down and `kubernetes.*` ops cannot
  help. `safety_level="safe"` / `requires_approval=false`, no params, and the
  probed unit set is the fixed `("rke2-server", "rke2-agent")` pair — there is
  **no** operator-supplied unit, so no arbitrary-unit or shell-injection
  surface. This is the read-only sibling of the approval-gated
  `rke2.node.service.restart`: `systemctl show` never restarts/starts/stops a
  unit. `--all` un-suppresses the `NRestarts=0` / empty-`ExecMainStartTimestamp`
  properties `systemctl show` drops by default, so a healthy unit reports
  `restart_count: 0` rather than a missing field. A node with no `systemctl`
  makes the probe exit non-zero (guarded), which raises
  `Rke2ServiceStatusProbeError` rather than being served as a service-state
  answer — the same infrastructure-failure discipline `rke2.posture.show` uses.

Both snapshot ops run **as root over plain SSH** (`_run_command`, no `sudo`
argv) — like the sibling T3 node-write ops, the connector already
authenticates as root, so no sudo construction is needed and the repo-wide
sudo-guard stays satisfied.

Nine ops total: four safe read-only (T1 `about` / `posture.show`, the #2852
`node.service.status`, and the #2854 redacted `node.config.get` read), three
`dangerous` / `requires_approval=true` write ops (T2 + T3), and two safe
non-gated snapshot ops (`.save` T4 #2431, `.list` #2853). `.save` is safe /
no-approval but active, so it is neither `read-only`-tagged nor in the
dangerous write tier — it belongs to neither sweep set; `.list` is safe /
no-approval and genuinely read-only, so it carries the `read-only` tag
alongside the T1 read tier.

Source: `backend/src/meho_backplane/connectors/rke2/`.

## Key types

- **`Rke2SshConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="rke2"`, `version="1.x"`, `impl_id="rke2-ssh"`.
  Inherits the per-target asyncssh connection pool, `_auth_config`,
  `_run_command`, `_assert_reachable`, and `aclose()` from the adapter.
  Ships `fingerprint`, `probe`, `execute`, `about`, `posture_show`,
  `service_status`, `config_get`, `register_operations`. Two module-level pure
  parsers live here:
  `parse_rke2_version` (release string from `rke2 --version`) and
  `parse_os_pretty_name` (`PRETTY_NAME` from `/etc/os-release`).

- **Posture handler + parsers** (`ops_read.py`) — `rke2_posture_show`
  (the async handler), `build_posture_probe_command` (assembles the
  single-round-trip POSIX-`sh` per-path probe), `parse_posture_probe_output`
  (marker lines → path→verdict map, mode normalised to the 4-digit octal
  form), and `parse_posture` (composes the `{config_files, token}` envelope;
  the token entry always carries `redacted: true`). `POSTURE_CONFIG_PATHS`
  and `RKE2_TOKEN_PATH` are fixed code constants — there is **no** operator
  path parameter, so no path-traversal / shell-injection surface.
  `Rke2PostureProbeError` fires when the probe itself could not run.

- **Service-state handler + parsers** (`ops_read.py`, #2833 / #2852) —
  `rke2_service_status` (the async handler, bound via the `service_status`
  shim), `build_service_status_command` (assembles the single-round-trip
  `systemctl show --all` probe over the fixed `SERVICE_UNITS` pair), and
  `parse_service_status` (`UNIT=` / `KEY=VALUE` marker stream → one entry per
  unit; a `LoadState=not-found` unit nulls its live-state fields, and
  `NRestarts` becomes `restart_count`). `SERVICE_UNITS` and
  `SERVICE_STATUS_PROPERTIES` are fixed code constants — no operator-supplied
  unit, so no arbitrary-unit surface. `Rke2ServiceStatusProbeError` fires when
  the probe cannot run (no `systemctl` on the node).

- **Config-content read + redaction** (`ops_read.py`, #2854) —
  `rke2_config_get` (the async handler: `bound_config_path` confinement →
  `cat` + `yaml.safe_load` → redact), and `redact_config_content` (the pure
  redaction step). `SECRET_CONFIG_KEYS` is the frozenset of fully-masked
  secret keys (`token` / `agent-token` / the three `etcd-s3-*` credentials);
  `REDACTED_SENTINEL` (`***redacted***`) is the replacement value; the
  `datastore-endpoint` DSN userinfo is masked via a scheme-anchored regex that
  leaves host/port/db intact. It reuses the write op's `bound_config_path`
  rather than re-deriving the `/etc/rancher/rke2/*.yaml` filter.

- **Op metadata** (`ops.py`) — `Rke2Op` frozen dataclass (mirrors
  `Bind9Op` / `HolodeckOp`), `SSH_TRANSPORT_NOTE` (the plain-SSH reminder
  copied into every op's `when_to_use`), `_RKE2_ABOUT_OP`, and `RKE2_OPS`
  (`about` + the `READ_OPS` read tuple — posture + `.service.status` +
  `.config.get` — + the `WRITE_OPS` write tuple + the `SNAPSHOT_OPS` tuple,
  the latter now `.save` + `.list`).

- **Write ops** (`ops_write.py`, #2429 + #2430) — the three approval-gated
  write ops share one module:
  - `rke2.token.rotate` (#2429): `rke2_token_rotate` (the async handler, bound
    via the `token_rotate` shim), `rke2_version_rotate_verdict` /
    `parse_rke2_release` (the pure version-gate logic against the per-minor
    CVE-fix floor + the `v1.27.10+rke2r1` deny). The minted new token is
    stashed in Vault under
    `secret/tenants/<tenant_id>/rke2/<node>/server-token`; only a pointer is
    returned. `_sudo.py` carries the family's own safe-`sudo -S` primitive
    (`run_remote_bash_with_sudo`) — the #697-hardened wire shape (script bytes
    first, password last on stdin, never in argv / history / log).
    `ops_write_preview.py` registers a non-secret park-time `proposed_effect`
    preview builder (`{node, service, semantics, new_token_minted}`).
  - `rke2.node.service.restart` / `rke2.node.config.update` (#2430): the
    bounds (`bound_unit` frozenset re-check; `ensure_config_path_under_root` /
    `bound_config_path` lexical `/etc/rancher/rke2/*.yaml` confinement +
    `ConfigPathRejectedError`; `apply_config_patch` / `changed_config_keys`
    backplane-owned merge), the `rke2_service_restart` / `rke2_config_update`
    handlers, and the two node-write approval-park preview builders (registered
    at import via `register_rke2_write_previews`). Privilege model for these
    node ops: the connector operates as `root` over SSH (the posture tier
    already `stat`s `0600 root:root` token files), so they run via
    `_run_command` without a separate sudo-password stream — the sudo primitive
    is reserved for the credential-minting `token.rotate` flow.
  - Shared: `WRITE_OPS` (all three ops) and
    `RKE2_WHEN_TO_USE_WRITE_BY_GROUP` (`rke2-token-write` + `rke2-node-write`,
    merged into the connector's `_WHEN_TO_USE_BY_GROUP`).

- **Snapshot handlers + parsers** (`ops_snapshot.py`, #2431 + #2853) — two
  handlers sharing one guard:
  - `rke2_etcd_snapshot_save` (#2431): guard → save → parse, run as root over
    plain `_run_command` with **no** `sudo` argv; `parse_saved_snapshot_name`
    recovers the name from the RKE2 `Snapshot <name> saved.` log;
    `_validate_name` is the fail-closed charset re-check of the single optional
    `name` (the only operator input), `shlex.quote`'d into the argv.
  - `rke2_etcd_snapshot_list` (#2853): guard → `rke2 etcd-snapshot list` →
    `parse_snapshot_list`, also root-over-plain-SSH, no operator params.
    Returns `{snapshots: [{name, location, size_bytes, created_at}, …]}`.
  - `_run_precondition_guard` — the **shared** embedded-etcd-server guard both
    handlers run first (a genuine reuse of `_GUARD_CMD`, not a per-op
    reimplementation). Its own exit status is checked before its stdout verdict
    is read, so an SSH/transport failure surfaces as a distinct transport error
    (`Rke2SnapshotError`) rather than a mislabeled "not an embedded-etcd server"
    verdict (fail-closed either way); an external-`datastore-endpoint` or
    non-server node raises `Rke2SnapshotPreconditionError`.
  - Structured errors: `Rke2SnapshotNameError` / `Rke2SnapshotPreconditionError`
    / `Rke2SnapshotError`. The `rke2` binary is invoked by absolute path in
    both handlers.

- **Registration** (`__init__.py`) — two-phase, mirroring bind9/holodeck.
  Synchronous `register_connector_v2` at import time (versioned triple +
  the `("rke2", "", "")` wildcard fallback); async
  `register_rke2_typed_operations` queued onto the lifespan registrar list.
  No v1 `register_connector` — RKE2 has no chassis history.

## Control flow

1. **Boot** — `_eager_import_connectors()` imports the `rke2` subpackage;
   `__init__.py` registers the v2 triple + wildcard synchronously and queues
   the typed-op registrar.
2. **Lifespan startup** — `run_typed_op_registrars()` calls
   `register_rke2_typed_operations` → `Rke2SshConnector.register_operations`,
   which upserts each `RKE2_OPS` entry into `endpoint_descriptor` (idempotent
   across restarts) with the curated per-group `when_to_use`.
3. **Dispatch** — `POST /api/v1/operations/call` → `call_operation` resolves
   `connector_id="rke2-ssh-1.x"` + the target, runs the policy gate, and
   invokes the bound handler. `about` reuses `fingerprint` and asserts
   reachability (#986); `posture_show` runs one probe round-trip and returns
   the redacted envelope; `service_status` runs one `systemctl show` probe and
   returns the per-unit systemd state (`{units: [...]}`). Transport/auth
   failures propagate to the dispatcher's `connector_error` branch; a
   merely-absent file surfaces as `present: false`, and an undeterminable one
   as `present: null`.

## Posture tri-state (#2698)

`stat` exits non-zero for **both** a missing file (`ENOENT`) and a parent
directory it cannot traverse (`EACCES`), and writes the distinction only to
stderr. The original handler read stdout alone, so both collapsed to
`present: false`. On a stock hardened server node — `0700 root:root` on
`/var/lib/rancher/rke2/server/`, reached as a non-root SSH user — the op
therefore reported the join token **absent on a node where it exists**,
which is the unsafe direction for an op whose stated job is "confirm the
server token exists before rotating". A rotation runbook pre-checking with
it was told there was nothing to rotate.

The handler now issues one POSIX-`sh` probe that resolves each path
individually and emits a marker line per path:

| Marker | `status` | `present` | Meaning |
| --- | --- | --- | --- |
| `S\|<path>\|<mode>\|<owner>\|<group>` | `present` | `true` | `stat` succeeded; mode/owner/group are real measurements |
| `A\|<path>` | `absent` | `false` | `stat` failed **and** the parent is traversable — genuinely not there (the agent-node "no server token" signal) |
| `U\|<path>` | `unknown` | `null` | the parent cannot be traversed, so existence is undetermined; `detail` names the directory |

Two properties are load-bearing:

- **Traversability is answered by the remote shell's `[ -x ]` test**, not by
  parsing `stat`'s diagnostic text — the kernel is asked the actual
  question, so no verdict depends on coreutils message wording or the node's
  locale.
- **Every real verdict exits 0**, so a non-zero exit is unambiguously an
  infrastructure failure (no `stat` on the node, broken shell) and raises
  `Rke2PostureProbeError` rather than being served as posture. This is the
  same reasoning `ops_snapshot` applies to its precondition guard, now
  extended to the read tier — previously the posture handler ignored
  `exit_status` entirely.

`asyncssh` reports `exit_status` as `None` when the peer closed the channel
without sending one at all (its documented third case, alongside an int and
`-1` for signal death), so "not zero" and "failed" are not the same test.
`None` is resolved from the output rather than assumed either way: the probe
emits exactly one marker line per measured path, so a verdict for every path
is independent evidence the run completed and the read is served. Missing
verdicts with no exit status to vouch for the run raise — neither the run
nor the unaccounted paths can be confirmed. Treating `None` as failure
outright would break reads against SSH implementations that omit
`exit-status` while still delivering full output; treating it as success
outright would re-open the class this fix closes.

A consumer must branch on `status`, not on the falsiness of `present`:
`null` and `false` are both falsy but mean different things. Every entry
carries the same key set (`path` / `present` / `status` / `mode` / `owner` /
`group` / `detail`, plus `redacted` on the token) so there is one shape per
path whatever the verdict.

**Deliberately not done:** elevating the read with `sudo` the way
`rke2.token.rotate` does. It would let the posture tier see inside a
`0700 root:root` directory, but it makes a sudo credential a soft
dependency of a *read* op — so the honest-reporting fix lands first and
alone. Root-authenticated deploys (the common case, per the write tier's
assumption) already report `present` because `stat` simply succeeds.

## Auth

Uses the base `SshConnector._auth_config` **unchanged** — key-preferred,
password-fallback. Credentials resolve via
`_shared/vault_creds.load_vault_secret_data(target, operator)` from the Vault
KV-v2 path string in `target.secret_ref` (the #2155 either/or shape:
`load_vault_secret_data`, **not** `load_basic_credentials`, so a key-only or
password-only secret resolves without demanding every field). The connector
does not touch the pre-#2155 "bind9 anti-shape" (an embedded credential dict).

## Redaction guarantee

The posture tier reads **no secret material**. The join-token entry reports
presence + mode only — the handler `stat`s the path and never `cat`s its
content, so the token value cannot appear in the result envelope, the audit
`raw_payload`, or the logs. Every token entry carries `redacted: true` to
make the guarantee explicit to agents reading the schema. This is the T1
foundation for the load-bearing Initiative #2172 rule: a secret-returning
handler must never return the secret (the audit `raw_payload` stores the raw
result).

`rke2.node.config.get` (#2854) is the same rule applied to a handler that
*does* read the config body. Because `raw_payload` persists the raw handler
result, the redaction must happen **inside the handler, before it returns** —
which it does: `redact_config_content` masks every secret-bearing key
(`token` / `agent-token` / `etcd-s3-access-key` / `etcd-s3-secret-key` /
`etcd-s3-session-token`) to `***redacted***` and masks the
`datastore-endpoint` DSN userinfo, so no secret value ever reaches the result
envelope, the `raw_payload`, or the logs. Redaction is names-not-values: the
masked key names are disclosed in `redacted_keys` (the `changed_config_keys`
precedent) while the values are gone. The secret-key set is grounded in the
RKE2 server-config reference, not just the two join tokens — the etcd S3
credentials are equally secret-bearing and equally masked. Custom, non-standard
keys an operator hand-added are out of scope (this is not a general secret
scanner); the bounded single-file read never enumerates drop-ins.

`rke2.token.rotate` (T2) is the write-side application of the same rule. The
dispatcher persists the **raw** handler result on the audit row and
connector-boundary redaction never scrubs `raw_payload`, so the only reliable
control is that the handler never returns the token — old or new. Both are
handled off the result surface: the OLD token is read on-disk as root inside
the sudo script (a shell `$(cat ...)`, never entering Python), and the NEW
token is minted here, written to Vault, and returned only as a pointer. The
op is additionally pinned in `broadcast/events._CREDENTIAL_MINT_OPS`
(defence-in-depth: `.rotate` would otherwise classify `other` and broadcast
full detail) and its park-time preview carries no token value.

## Dependencies

- `connectors/adapters/ssh.py` — the SSH transport base (pool, auth,
  `_run_command`, `_assert_reachable`).
- `_shared/vault_creds.py` — the operator-context Vault KV-v2 read (#2155).
- `operations/typed_register.py` — the op-registration seam.
- `connectors/registry.py` — the v2 registry + eager-import walk.
- stdlib `re`, `shlex` (path quoting, defensive even though paths are fixed).

## Privilege (etcd-snapshot.save)

The snapshot op runs both remote commands (the precondition guard and the
`rke2 etcd-snapshot save` itself) **as root over plain SSH** via
`_run_command` — no `sudo` argv. The connector authenticates as root (the
same posture the read tier relies on when it `stat`s `0600 root:root` token
files, and the same model the T3 node-write ops use for `systemctl` and
config-file writes), so no privilege elevation is constructed here. This
deliberately avoids hand-rolling a `sudo` argv, which the repo-wide
sudo-guard (`test_sudo_is_only_referenced_via_the_safe_primitive` + its
integration twin `test_remote_bash_with_sudo_is_only_sudo_construction_in_connectors_tree`)
forbids in any `connectors/` file outside the sanctioned safe-sudo
primitives. The only operator input, `name`, is charset-bounded and
`shlex.quote`'d. The credential-minting `token.rotate` flow is the one RKE2
op that needs the sudo-password wire shape (`_sudo.py`); a non-secret snapshot
does not. The precondition guard's own exit status is checked before its
stdout verdict is interpreted, so a transport/SSH failure surfaces as a
distinct error rather than a mislabeled node-role verdict (fail-closed either
way).

## List output parsing (#2853)

`rke2 etcd-snapshot list` prints a `Name / Location / Size / Created` table.
RKE2's own docs (`docs.rke2.io/datastore/backup_restore`, fetched 2026-08-12)
document **only** this table for the `list` subcommand — a `-o json` /
`--output json` flag was requested upstream (`k3s-io/k3s#5130`) but neither its
schema nor its per-version availability is documented. Rather than parse an
unconfirmed JSON shape from memory (the no-guessing-on-APIs rule), the handler
parses the documented table, which is version-universal.

The parse is deliberately drift-resilient, because two columns vary across
RKE2 versions:

- **`Location`** — `local`, a `file://` URL, a bare filesystem path, or an
  `s3://` URL depending on version and store. It is a single whitespace-free
  token in every form, and is **passed through verbatim** (never rewritten
  against `SNAPSHOT_DEFAULT_DIR` — the vendor is the source of truth for where
  a snapshot actually lives, including S3).
- **`Size`** — the documented format is a raw byte count (e.g. `52428800`), but
  some transcripts show a human string (`50 MiB`). `size_bytes` is the integer
  when the column is a bare integer, else `null` (fail-closed — never a guessed
  unit conversion).

Each row is matched by a single regex (`_SNAPSHOT_LIST_ROW_RE`) that anchors the
final column as an ISO-8601 timestamp. That anchor is what lets the same regex
**skip the header row** — its `Created` label is not a timestamp — without
depending on the header's casing (`Name` vs `NAME`), and also skips blank lines
and any "no snapshots" notice. An empty `snapshots` list therefore means the
node genuinely has no snapshots, not a parse failure. This mirrors the
regex-based text parsing `parse_saved_snapshot_name` already does for `.save`'s
log line.

## Broadcast / approval wiring (T3 #2430)

- `rke2.node.service.restart` classifies plain `write` via the `.restart`
  write-suffix added to `broadcast/events.py::_WRITE_SUFFIXES`; its params
  (a single unit) carry no secret.
- `rke2.node.config.update` is pinned in
  `broadcast/events.py::_CREDENTIAL_WRITE_OPS` (its `patch` may carry a
  `token:` value), so the broadcast collapses to aggregate-only.
- Approval-park previews: `_rke2_service_restart_preview` renders
  `{resource: systemd_unit, unit, action, node}`; `_rke2_config_update_preview`
  renders `{resource: config_file, path, semantics, key_names}` — key names
  only, never the file body or values.

## Known issues / follow-ups

- `rke2.token.rotate` (T2 #2429), `rke2.node.service.restart` /
  `rke2.node.config.update` (T3 #2430), and `rke2.etcd-snapshot.save`
  (T4 #2431) are all landed — the Initiative #2172 SSH write/maintenance
  surface is complete. `rke2.etcd-snapshot.list` (#2853) adds the snapshot
  read counterpart on top of that surface.
- The node-write ops (service.restart / config.update) and the snapshot ops
  assume `root` SSH access (consistent with the read posture tier); a future
  non-root + sudo-password path would route the mutating node ops through
  `_sudo.run_remote_bash_with_sudo` (as `token.rotate` already does) if a
  target ever connects as a non-root user. `rke2.etcd-snapshot.save` /
  `rke2.etcd-snapshot.list` would surface a `connector_error` on such a
  target rather than executing.
- `rke2.etcd-snapshot.list` parses the documented `Name/Location/Size/Created`
  table (not `-o json`) because the JSON flag's schema and per-version
  availability are undocumented; if a future RKE2 pins a stable JSON contract,
  the handler could prefer it. See [List output parsing](#list-output-parsing-2853).
- The rotate is a **single-node atomic op**: multi-node token-propagation /
  restart choreography is an operator-composed runbook of T2+T3 ops, not part
  of this op (per the Initiative DoD).
- Host-key checking is disabled (`known_hosts=None`) at the adapter level for
  v0.2, shared across the whole SSH family; pinning is deferred repo-wide.
- The RKE2 version probe is best-effort: `version` is `null` when the `rke2`
  binary is not on the login shell PATH (agent nodes); reachability is not
  affected.

## References

- Parent Initiative #2172 (SSH cluster-node OS-lifecycle write ops); Task
  #2221 (this scaffold). Adapter fix prerequisite #2155.
- Snapshot read tier: Task #2853 (`rke2.etcd-snapshot.list`, parent Initiative
  #2833 connector read-op coverage). Vendor: `rke2 etcd-snapshot list` —
  `docs.rke2.io/datastore/backup_restore` (Name/Location/Size/Created table);
  JSON flag requested at `k3s-io/k3s#5130` (schema/availability undocumented).
- Mold: holodeck-ssh (G3.18 #2145 / `docs/codebase/connectors-holodeck.md`),
  bind9-ssh (`docs/codebase/connectors-bind9.md`).
- Cross-repo coordination: `docs/cross-repo/rke2-infra-coordination.md`.
- Field case: `claude-rdc-hetzner-dc#615` (RKE2 join-token rotation).
