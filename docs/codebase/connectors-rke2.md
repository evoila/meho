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

T3 (#2430) adds the first two **approval-gated node-write ops**
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

The remaining write ops (`rke2.token.rotate` T2 #2429,
`rke2.etcd-snapshot.save` T4 #2431) append to `RKE2_OPS` from their own
sibling modules the same way.

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
  (`about` + the `READ_OPS` posture tuple + the `WRITE_OPS` node-write tuple).

- **Write ops** (`ops_write.py`, T3 #2430) — the bounds
  (`bound_unit` frozenset re-check; `ensure_config_path_under_root` /
  `bound_config_path` lexical `/etc/rancher/rke2/*.yaml` confinement +
  `ConfigPathRejectedError`; `apply_config_patch` / `changed_config_keys`
  backplane-owned merge), the `rke2_service_restart` / `rke2_config_update`
  handlers, the two approval-park preview builders (registered at import via
  `register_rke2_write_previews`), `WRITE_OPS`, and
  `RKE2_WHEN_TO_USE_WRITE_BY_GROUP` (merged into the connector's
  `_WHEN_TO_USE_BY_GROUP`). Privilege model: the connector operates as `root`
  over SSH (the posture tier already `stat`s `0600 root:root` token files),
  so writes run via `_run_command` without a separate sudo-password stream.

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

- Remaining write ops (`token.rotate` #2429, `etcd-snapshot.save` #2431) are
  deferred to their sibling Tasks under Initiative #2172.
- The write ops assume `root` SSH access (consistent with the read posture
  tier); a future non-root + sudo-password path would mirror bind9's
  `_remote_bash_with_sudo` if a target ever connects as a non-root user.
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
