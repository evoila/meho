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
  construction).

Both `safety_level="safe"` / `requires_approval=false`.

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

T4 (#2431) adds the lone **safe, non-gated snapshot op** (in the
`rke2-etcd-snapshot` group):

- `rke2.etcd-snapshot.save` — triggers an on-demand managed-etcd snapshot on
  a server node (`rke2 etcd-snapshot save`, embedded-etcd only). It is
  `safety_level="safe"` / `requires_approval=false` because it is read-only
  with respect to *running* cluster state (it copies etcd to a file on disk)
  and returns only a snapshot name + path, never etcd contents. An optional
  `name` param is charset-bounded to `^[A-Za-z0-9._-]+$` at the schema
  boundary AND re-checked in the handler; a fail-closed precondition guard
  refuses a non-server / external-`datastore-endpoint` node. Like the sibling
  T3 node-write ops it runs **as root over plain SSH** (`_run_command`, no
  `sudo` argv) — the connector already authenticates as root, so no sudo
  construction is needed and the repo-wide sudo-guard stays satisfied.

Six ops total: two safe read-only (T1), three `dangerous` /
`requires_approval=true` write ops (T2 + T3), and one safe non-gated snapshot
op (T4). The snapshot op is safe / no-approval like the read ops but is
neither read-only-tagged nor in the dangerous write tier — it belongs to
neither sweep set.

Source: `backend/src/meho_backplane/connectors/rke2/`.

## Key types

- **`Rke2SshConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="rke2"`, `version="1.x"`, `impl_id="rke2-ssh"`.
  Inherits the per-target asyncssh connection pool, `_auth_config`,
  `_run_command`, `_assert_reachable`, and `aclose()` from the adapter.
  Ships `fingerprint`, `probe`, `execute`, `about`, `posture_show`,
  `register_operations`. Two module-level pure parsers live here:
  `parse_rke2_version` (release string from `rke2 --version`) and
  `parse_os_pretty_name` (`PRETTY_NAME` from `/etc/os-release`).

- **Posture handler + parsers** (`ops_read.py`) — `rke2_posture_show`
  (the async handler), `parse_stat_output` (`stat -c '%n|%a|%U|%G'` stdout
  → path→attrs map, mode normalised to the 4-digit octal form), and
  `parse_posture` (composes the `{config_files, token}` envelope; the token
  entry always carries `redacted: true`). `POSTURE_CONFIG_PATHS` and
  `RKE2_TOKEN_PATH` are fixed code constants — there is **no** operator path
  parameter, so no path-traversal / shell-injection surface.

- **Op metadata** (`ops.py`) — `Rke2Op` frozen dataclass (mirrors
  `Bind9Op` / `HolodeckOp`), `SSH_TRANSPORT_NOTE` (the plain-SSH reminder
  copied into every op's `when_to_use`), `_RKE2_ABOUT_OP`, and `RKE2_OPS`
  (`about` + the `READ_OPS` posture tuple + the `WRITE_OPS` write tuple +
  the `SNAPSHOT_OPS` tuple).

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

- **Snapshot handler + parser** (`ops_snapshot.py`, #2431) —
  `rke2_etcd_snapshot_save` (the async handler: guard → save → parse, both run
  as root over plain `_run_command` with **no** `sudo` argv),
  `parse_saved_snapshot_name` (recovers the name from the RKE2
  `Snapshot <name> saved.` log), `_validate_name` (fail-closed charset
  re-check), and the `Rke2SnapshotNameError` / `Rke2SnapshotPreconditionError`
  / `Rke2SnapshotError` structured errors. The `rke2` binary is invoked by
  absolute path; the single optional `name` is the only operator input and
  is `shlex.quote`'d into the argv after the charset re-check. The precondition
  guard's own exit status is checked before its stdout verdict is read, so an
  SSH/transport failure surfaces as a distinct transport error rather than a
  mislabeled "not an embedded-etcd server" verdict (fail-closed either way).

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
   reachability (#986); `posture_show` runs one `stat` round-trip and returns
   the redacted envelope. Transport/auth failures propagate to the
   dispatcher's `connector_error` branch; a merely-absent file surfaces as
   `present: false`.

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
  surface is complete.
- The node-write ops (service.restart / config.update) and the snapshot op
  assume `root` SSH access (consistent with the read posture tier); a future
  non-root + sudo-password path would route the mutating node ops through
  `_sudo.run_remote_bash_with_sudo` (as `token.rotate` already does) if a
  target ever connects as a non-root user. `rke2.etcd-snapshot.save` would
  surface a `connector_error` on such a target rather than executing.
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
- Mold: holodeck-ssh (G3.18 #2145 / `docs/codebase/connectors-holodeck.md`),
  bind9-ssh (`docs/codebase/connectors-bind9.md`).
- Cross-repo coordination: `docs/cross-repo/rke2-infra-coordination.md`.
- Field case: `claude-rdc-hetzner-dc#615` (RKE2 join-token rotation).
