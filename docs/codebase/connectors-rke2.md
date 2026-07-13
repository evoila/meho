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

T4 (#2431) adds the lone **safe, non-gated write-ish op**:

- `rke2.etcd-snapshot.save` — triggers an on-demand managed-etcd snapshot on
  a server node (`rke2 etcd-snapshot save`, embedded-etcd only). It is
  `safety_level="safe"` / `requires_approval=false` because it is read-only
  with respect to *running* cluster state (it copies etcd to a file on disk)
  and returns only a snapshot name + path, never etcd contents. An optional
  `name` param is charset-bounded to `^[A-Za-z0-9._-]+$` at the schema
  boundary AND re-checked in the handler; a fail-closed precondition guard
  refuses a non-server / external-`datastore-endpoint` node.

Three ops total, all `safety_level="safe"` / `requires_approval=false`. The
approval-gated write ops (`rke2.token.rotate`, `rke2.node.service.restart`,
`rke2.node.config.update`) ship in sibling Tasks #2429/#2430 — out of scope
here.

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
  (`about` + the `READ_OPS` posture tuple + the `SNAPSHOT_OPS` tuple).

- **Snapshot handler + parser** (`ops_snapshot.py`) —
  `rke2_etcd_snapshot_save` (the async handler: guard → `sudo -n` save →
  parse), `parse_saved_snapshot_name` (recovers the name from the RKE2
  `Snapshot <name> saved.` log), `_validate_name` (fail-closed charset
  re-check), and the `Rke2SnapshotNameError` / `Rke2SnapshotPreconditionError`
  / `Rke2SnapshotError` structured errors. The `rke2` binary is invoked by
  absolute path; the single optional `name` is the only operator input and
  is `shlex.quote`'d into the argv after the charset re-check.

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

## Dependencies

- `connectors/adapters/ssh.py` — the SSH transport base (pool, auth,
  `_run_command`, `_assert_reachable`).
- `_shared/vault_creds.py` — the operator-context Vault KV-v2 read (#2155).
- `operations/typed_register.py` — the op-registration seam.
- `connectors/registry.py` — the v2 registry + eager-import walk.
- stdlib `re`, `shlex` (path quoting, defensive even though paths are fixed).

## Privilege (etcd-snapshot.save)

The snapshot op runs every remote command under `sudo -n` (non-interactive):
the `rke2` binary and the config / etcd paths are root-owned. `sudo -n` needs
no password on a root or NOPASSWD-sudo operator account (the expected RKE2
node access model) and fails **closed** — exiting non-zero rather than
hanging — when a password would be required. The op interpolates no secret
into its argv (the only operator input, `name`, is charset-bounded and
`shlex.quote`'d), so it does **not** need the password-hiding
`_remote_bash_with_sudo` mold the approval-gated write ops (#2429/#2430)
add; keeping it self-contained also lets it land independently of those
concurrent tasks.

## Known issues / follow-ups

- The approval-gated write ops (token rotate / service restart / config
  update) are deferred to #2429/#2430 under Initiative #2172.
- If a real deployment requires a sudo *password* (no root / NOPASSWD),
  `rke2.etcd-snapshot.save` surfaces a `connector_error` (`sudo: a password
  is required`) rather than executing; adopting the `_remote_bash_with_sudo`
  primitive that #2429/#2430 add is the follow-up for that case.
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
