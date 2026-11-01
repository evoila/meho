# Connector: holodeck (holodeck-9.0 / `holodeck-ssh`)

## Overview

The `holodeck` connector is the typed `Connector` subclass that dispatches
operator-facing VMware Holodeck operations over SSH. It is registered under
the `(product="holodeck", version="9.0", impl_id="holodeck-ssh")` registry
triple and is the **third** typed-SSH tier child of the `SshConnector`
adapter (G0.2-T4 #243), after `Bind9Connector` (G3.4) and `PfSenseConnector`
(G3.7).

HoloRouter exposes **no REST API** — the appliance's only operator surface
is SSH-to-root driving `pwsh -EncodedCommand` for Holodeck PowerShell cmdlets
(or `kubectl` for the in-appliance K8s). This makes Holodeck the canonical
SSH-only typed connector in the inventory and the tier-4 closer for G3.

The connector replaces the operator's `scripts/holodeck.sh` wrapper in the
`claude-rdc-hetzner-dc` consumer repository. The G3.8-T1 (#853) skeleton
ships the `holodeck.about` canary op, the password-default + key-fallback
auth via the inherited `SshConnector._auth_config`, the fingerprint, the
probe, and the PowerShell-over-SSH helper (hoisted from the former
package-local `_pwsh.py` to the shared `_shared/pwsh.py` in #3260).
G3.8-T2 (#854) adds
the 7 read ops (`config.show`, `pod.list`, `pod.info`, `service.list`,
`k8s.exec`, `logs.tail`, `networking.show`) for a total of 8 ops
registered under `connector_id="holodeck-ssh-9.0"`. G3.8-T3 (#855) ships
the CLI verbs + E2E acceptance suite + onboarding doc. G3.18-T1 (#2153)
appends `holodeck.disk.usage` — root-fs `df` + `du` on a fixed growth-dir
set for pre-eviction disk diagnosis — bringing the total to 9 ops. #2847
appends `holodeck.backups.list` — a safe per-file inventory of
`/var/backups` reusing `holodeck.backups.prune`'s own `find` listing —
taking the read surface to **10 ops** (13 total, including the 3
approval-gated write ops). #2908 (Initiative #2907) appends the 4
deploy-lifecycle ops (`config.apply`, `instance.start`, `instance.status`,
`router.patch`). #4066 (Initiative #2907) appends the 3 DNS name ops
(`dns.host.list`, `dns.host.add`, `dns.host.remove`), and
`holodeck.instance.remove` deletes an instance again
(see "Delete an instance" below) — **21 ops total**.

Source: `backend/src/meho_backplane/connectors/holodeck/`.

## Key types

- **`HolodeckConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="holodeck"`, `version="9.0"`, `impl_id="holodeck-ssh"`.
  Inherits the per-target asyncssh connection pool, `_auth_config`,
  `_run_command`, and `aclose()` from the adapter. T1 shipped `fingerprint`,
  `probe`, `execute`, `about`. T2 (#854) adds the 7 read-op bound-method
  shims: `config_show`, `pod_list`, `pod_info`, `service_list`, `k8s_exec`,
  `logs_tail`, `networking_show`. G3.18-T1 (#2153) adds `disk_usage`.

- **Read-op handlers + parsers** (`ops_read.py`) — `holodeck_config_show`,
  `holodeck_pod_list`, `holodeck_pod_info`, `holodeck_service_list`,
  `holodeck_k8s_exec`, `holodeck_logs_tail`, `holodeck_networking_show`,
  `holodeck_disk_usage`, `holodeck_backups_list`. Pure parsers:
  `parse_kubectl_command` (verb-safelist + flag-allowlist enforcement),
  `parse_logs_tail_output` (GNU `tail` `==> path <==` header split),
  `parse_networking_payload` (four-section composer),
  `parse_disk_usage_output` (root-fs `df -B1` + per-dir `du -sb` composer
  with per-section `ok`), `parse_backup_listing_output` (`find -printf
  '%T@ %s %p'` → `{path, mtime, size_bytes}` rows, newest-first order
  preserved). `GROWTH_DIRS` is the fixed
  code constant (`/var/backups`, `/holodeck-runtime`) the disk-usage op
  measures — no operator path parameter. `KubectlSafetyError` is the
  `ValueError` subclass the dispatcher's error envelope picks up when a
  mutating verb slips through.

- **`_shared/pwsh.py` — the shared transport (formerly the package-local
  `_pwsh.py`; hoisted in #3260, executing the #2759 note, once the
  Microsoft-estate program #3259 pushed the consumer count past the ≥3
  sharing threshold).** Houses `encode_pwsh_command(script)`
  (UTF-16LE-base64 per the `-EncodedCommand` convention), `pwsh_run(connector,
  target, script)` (runs `<exe> -NoProfile -NonInteractive -EncodedCommand
  <encoded>` over the pooled SSH connection and parses `ConvertTo-Json`
  stdout via stdlib `json`), `ps_single_quote` (the `shlex.quote` analogue,
  replacing the former local `_pwsh_single_quote`), `strip_clixml`,
  `PwshRunError` (structured failure with the truncated stderr fragment but
  no script body or auth material), and `PWSH_DEFAULT_DEPTH = 4` (the
  recommended `ConvertTo-Json -Depth` per the #371 body). Per-connector wire
  variation rides three class-level seams read off the connector:
  `POWERSHELL_EXECUTABLE` (`pwsh` here), `POWERSHELL_SCRIPT_PREFIX` (unset
  here — Holodeck silences warnings per-op instead), `POWERSHELL_LOG_EVENT`
  (`holodeck_pwsh_executed`).

  **Design correction (2026-05-21).** The Initiative body originally
  specified CliXml output via `-OutputFormat Xml` + `pyclixml` parsing. T1
  supersedes that with `ConvertTo-Json` + stdlib `json`: the #371 body's
  fingerprint/probe/op examples all already use `ConvertTo-Json`, and Json
  drops the undecided `pyclixml` dependency. The wire encoding (UTF-16LE-
  base64) and the `-EncodedCommand` argv shape are unchanged from the
  original design.

  **CLIXML warning-stream hygiene (#3081).** `pwsh -EncodedCommand` can
  serialise a warning / error record — notably the `HoloDeck` module's
  "unapproved verbs" auto-load warning — as a `#< CLIXML … </Objs>` block
  written to **stdout** ahead of the real `ConvertTo-Json` payload, which
  makes `json.loads` fail on otherwise-valid output. `-OutputFormat Text`
  does not reliably suppress it (PowerShell #5912). `strip_clixml(stdout)`
  removes every such block before the empty-check and parse; a cmdlet that
  emits *only* a warning strips to empty and fails closed via the existing
  empty-stdout guard (the honest failure `config.show` already reports).
  This is the **single shared seam**, so every pwsh-backed op benefits at
  once. Ops that must never report false success also silence the warning
  at the source (`$WarningPreference = 'SilentlyContinue'`).

- **Op metadata** (`ops.py`) — the `HolodeckOp` dataclass and the
  `HOLODECK_OPS` tuple. T1 shipped the single `holodeck.about` canary; T2
  (#854) extends the tuple via `_holodeck_ops()` which splats `READ_OPS`
  (the 7 read ops defined in `ops_read.py`) onto the canary, and later tasks
  append `WRITE_OPS` (`ops_write.py`, #2154), `DEPLOY_OPS` (`ops_deploy.py`,
  #2908), `DNS_OPS` (`ops_dns.py`, #4066) and `REMOVE_OPS` (`ops_remove.py`) — **21 ops total**. The pattern mirrors bind9's `_bind9_ops()` and
  pfSense's `_pfsense_ops()`.

- **Response schemas** (`_response_schemas.py`) — `HOLODECK_RESPONSE_SCHEMAS`,
  one schema per op id, split out so the op tables stay readable.

**Operation contracts.** The 21 ops meet the VMware operation-contract check
(`backend/tests/test_operation_contracts.py`); none is on its gap list.

- Each `response_schema` names the fields the handler returns, with a
  description per field. Holodeck publishes no API file. Where a field is a
  cmdlet answer passed through as is (`Get-HoloDeckConfig` → `config`,
  `Get-HoloDeckPod` → `rows[]` / `pod`), the object stays open and the text
  names only the keys MEHO reads (`Version`, `PodId`).
- Each `parameter_schema` describes every field, nested ones too, and carries
  one example call under `examples`. The parameter rules themselves (types,
  enums, patterns, the 5.2.x depot refusal) are unchanged.
- `llm_instructions` carries `when_to_call` (with the SSH-only transport
  note), `output_shape` and `next_step`; the eight changes (`k8s.exec`,
  `k8s.pods.gc`, `backups.prune`, `images.import`, `config.apply`,
  `instance.start`, `router.patch`, `instance.remove`) also carry
  `preconditions`.
- The texts say what MEHO really sends. `holodeck.config.apply` sends only
  `description` and `external_ip` to `New-HoloDeckConfig`; `master_cidr`,
  `vlan_range`, `placement`, `depot` and `instance_id` are checked and echoed
  back, not sent. `holodeck.instance.start` sends `-ManagementOnly` only for
  `variant=management_only`; for `vvf` and `full` it sends no profile flag.

- **Deploy-lifecycle op handlers + helpers** (`ops_deploy.py`, #2908) —
  `holodeck_config_apply`, `holodeck_instance_start`, `holodeck_instance_status`,
  `holodeck_router_patch`, plus the pure helpers `validate_config_apply_params`
  (the depot-vs-5.2.x refusal, also unit-tested directly),
  `map_instance_status_envelope` (the `Get-HoloDeckInstance` → flat envelope
  mapper), `target_hr_version` (reads `Target.version` then `fingerprint.version`),
  and `ROUTER_PATCHES` (the three `RouterPatchSpec` records: applied/unpatched
  grep markers + the `sed` expression per patch).

- **DNS name op handlers** (`ops_dns.py`, #4066) — `holodeck_dns_host_list`,
  `holodeck_dns_host_add`, `holodeck_dns_host_remove`, and the small `_Router`
  helper that runs one op call's commands. The pure helpers are in
  `dns_hosts.py`: `bound_dns_entry` (input check), `parse_hosts`, `plan_add`,
  `plan_remove`, `dnsmasq_pods`, `dig_status`, `dig_answers`, and
  `HolodeckDnsError` (carries the failed `step`).

- **`parse_photon_version`** (`connector.py`) — pure parser for
  `/etc/photon-release` output; recovers the `<major>.<minor>(.<patch>)?`
  Photon version token from the appliance's release file.

## Control flow

### Auth (inherited from `SshConnector._auth_config`)

`_auth_config(target, operator)` is called by the base
`SshConnector._connect` method before opening any TCP connection.
`target.secret_ref` is a Vault KV-v2 **path string** (never an embedded
credential dict — the "bind9 anti-shape"); `_auth_config` first resolves
it to the secret's data dict via the adapter's `_resolve_secret`
(`_shared/vault_creds.load_vault_secret_data`, an operator-context read
under the calling operator's Vault identity — #2155), then selects the
auth flavour from the resolved dict:

1. `ssh_private_key` present → parse via `asyncssh.import_private_key`,
   return `{username, client_keys=[key]}`. This is the key-preferred path
   (mirrors the wrapper's `PreferredAuthentications=publickey,password`
   header).
2. No `ssh_private_key`, `password` present → return
   `{username, password}`. This is the **default** path for the HoloRouter
   OVA, which ships with root password auth enabled.
3. Neither set → `ValueError` (the base adapter's standard message). The
   probe folds this into `ssh_auth_failed`, together with the Vault
   two-phase errors (`VaultClientError` / `VaultCredentialsReadError`).

The operator is threaded from the dispatcher into every op handler
(`dispatch_typed` passes it by signature-name introspection) down to
`_run_command` / `pwsh_run`. Operator-less paths (`probe()`, readiness)
fall back to the synthesised system operator, whose placeholder JWT
Vault rejects — system-initiated calls cannot read per-target vendor
credentials.

The Holodeck connector intentionally does **not** override `_auth_config`.
The inversion vs pfSense (key-only) is encoded in pfSense's override; the
Holodeck connector's reliance on the base behaviour is the design choice.

### `fingerprint(target)`

Runs two SSH command paths in sequence over the pooled connection:

1. `cat /etc/photon-release` → parsed via `parse_photon_version` to extract
   the Photon OS version token. The first line of the file lands in `build`;
   the parsed token lands in `extras["photon_version"]`.
2. `pwsh -NoProfile -NonInteractive -EncodedCommand <base64-of-UTF16LE>` of
   the script `Get-HoloDeckConfig | ConvertTo-Json -Compress`. Routed
   through `pwsh_run`; the parsed JSON dict yields `version` (the
   `Version`/`HolodeckVersion` field) and `extras["pod_id"]` (the
   `PodId`/`PodID` field).

Failure modes:

- SSH connect / command fails (`OSError`, `asyncssh.Error`) → `reachable=False`
  with `extras["error"]` set to `str(exc)`. The probe never opens a
  follow-up pwsh call when the first SSH command fails.
- `pwsh` cmdlet fails (`PwshRunError`) → `reachable=False` with both
  `extras["error"]` and the Photon snapshot preserved in `extras`. The
  operator sees how far the probe got before the cmdlet broke.

`probe_method="ssh: pwsh Get-HoloDeckConfig"`.

### `probe(target)`

Four-stage health check; each stage maps to a distinct
`ProbeResult.reason`:

1. SSH connect — `OSError` → `tcp_unreachable`;
   `asyncssh.PermissionDenied`, `asyncssh.DisconnectError`, or `ValueError`
   from `_auth_config` → `ssh_auth_failed`.
2. `cat /etc/photon-release` — empty stdout or non-zero exit →
   `photon_unhealthy` (covers non-Photon targets and corrupt appliance
   images).
3. `pwsh` of
   `Get-Service | Where-Object { $_.Name -like 'Holo*' } | Select-Object
   Name,Status | ConvertTo-Json` — any service in the result with `Status`
   ≠ `Running` (string or the numeric `4`) → `holodeck_services_down`. A
   `PwshRunError` here also maps to `holodeck_services_down` (the cmdlet's
   failure is itself a Holodeck-services signal).
4. All checks pass → `ok=True`, `reason=None`.

The probe does not mutate state. `Get-Service` is read-only on Photon.

### Read ops (T2 surface + G3.18-T1 disk.usage + #2847 backups.list)

The read ops route through the dispatcher's standard
`call_operation` path. Each registers via `register_typed_operation()` with
`safety_level="safe"`, `requires_approval=False`, and an
`llm_instructions.when_to_call` text that includes the SSH-only transport
disclosure (CLAUDE.md postulate 5 + Initiative #371).

- **`holodeck.config.show`** (group `config`). Runs `Get-HoloDeckConfig |
  ConvertTo-Json -Depth 4 -Compress` via `pwsh_run`. Returns
  `{config: <parsed dict>}` or `{config: None, error: "<reason>"}` on
  cmdlet failure.
- **`holodeck.pod.list`** (group `pod`, JSONFlux-shaped). Runs
  `Get-HoloDeckPod | ConvertTo-Json -Depth 4` via `pwsh_run`. Returns
  `{rows: [...], total: N}`. JSONFlux handle creation is the reducer's
  job — not the connector's — matching the pfSense / bind9 precedent
  (`PassThroughReducer` passes the inline payload through today; a future
  JSONFlux reducer will key on the `holodeck_pod_list` HandleStore slot
  when `total` exceeds its threshold). The handler normalises
  `ConvertTo-Json`'s single-element flat-dict shape and the empty-pipeline
  `null` shape into a uniform list.
- **`holodeck.pod.info`** (group `pod`). Runs `Get-HoloDeckPod -Id '<id>' |
  ConvertTo-Json -Depth 4` via `pwsh_run`. `pod_id` parameter is required;
  the handler quotes it with `ps_single_quote`. Returns
  `{pod: <dict>}` or `{pod: None, error}` on cmdlet / missing-id failure.
- **`holodeck.service.list`** (group `service`, JSONFlux-shaped). Runs
  `Get-Service | Where-Object { $_.Name -like 'Holo*' } | Select-Object
  Name,Status,DisplayName | ConvertTo-Json -Depth 4`. Same `{rows, total}`
  envelope and the same single-dict / `null` normalisation as `pod.list`.
- **`holodeck.k8s.exec`** (group `k8s`, `safety_level=dangerous`,
  `requires_approval=True`). Forwards an operator-supplied ``kubectl``
  command to the in-appliance K8s cluster. Because the command line is
  operator-supplied, the op is approval-gated: a dispatch parks for
  human approval rather than running unattended. Its verb safelist
  confines it to read-only ``kubectl`` verbs, a positive **flag
  allowlist** confines it to read-only scoping/formatting options, and
  two complementary allowlist layers reject mutating verbs and
  shell-injection shapes
  (`;`, `&&`, `||`, `|`, `$(...)`, backticks, `>`, `<`, newline):

  1. *Schema layer.* The `command` parameter has a `pattern` regex
     anchored `\Akubectl ... \Z`. The verb alternation accepts both
     the nine single-word read verbs (`get|describe|logs|top|explain|
     api-resources|api-versions|cluster-info|version`) and a
     fixed set of multi-word inspection verbs (`config view`,
     `config get-contexts`, `config get-clusters`, `config get-users`,
     `config current-context`, `auth can-i`, `auth whoami`).
     Multi-word literals appear FIRST in the alternation so the regex
     engine commits to the longer match when both could apply;
     arguments are restricted to `[A-Za-z0-9._/=:,@-]` so shell
     metacharacters can't survive validator-layer rejection. Whitespace
     between tokens is constrained to `[ \t]` (space or tab) so a
     newline can't smuggle a second command line through the `\s`
     class. The dispatcher's `validate_params` rejects bad shapes
     before reaching the handler.
  2. *Handler layer (authoritative).* `parse_kubectl_command`
     (a) scans the raw command against `_SHELL_METACHARS_RE`
     (`[;&|<>` + backtick + `$()\\` + newline + carriage return]`) and
     refuses on hit, (b) tokenises via `shlex.split`, (c) walks past
     leading global flags (`--flag=value` and `--flag value` forms),
     **validating each against `_K8S_ALLOWED_FLAGS`**, (d) checks the
     2-token (parent, sub-verb) prefix against
     `_K8S_MULTIWORD_READ_VERBS` first, (e) falls through to the
     single-word `_K8S_READ_VERBS`, and (f) validates every remaining
     argument-position flag against the same allowlist. Any rejected
     step raises `KubectlSafetyError`; the handler returns
     `{stdout, stderr, exit_status, error}` with `error` set to the
     safety-check message. The metacharacter reject is **load-bearing**:
     `shlex.split` in POSIX mode does not treat shell separators as
     token boundaries, so without it `kubectl get pods; rm -rf /`
     would tokenise to `['kubectl', 'get', 'pods;', ...]`, the verb
     check would approve `get`, and the handler would forward the raw
     string to `asyncssh.SSHClientConnection.run`, which delegates to
     the remote login shell. The reject runs **before** any SSH
     transport is touched. The full command body is **not** echoed
     back in the error message — only the rejected verb / metachar
     category — so operator-supplied resource names don't bleed into
     structured error envelopes.

  The multi-word allowlist (added per G3.8 follow-up #1020) pins
  parent + sub-verb pairs together because the parent token
  (`config` / `auth`) is a shared namespace where both read and
  write sub-verbs live — e.g. `kubectl config view` is a read but
  `kubectl config set-context` is a mutation. The check refuses
  ``kubectl config`` / ``kubectl auth`` with no sub-verb, and
  refuses any sub-verb not in the per-parent frozenset, so
  adjacent mutating sub-verbs (`config set-context`,
  `config unset`, `auth reconcile`, ...) fail closed.

  The **flag allowlist** (`_K8S_ALLOWED_FLAGS`) closes the
  argument-level class that the verb safelist and the metacharacter
  reject leave open: a flag such as `--server`,
  `--insecure-skip-tls-verify`, `--token`, `--kubeconfig`, `--context`,
  `--raw` or `--output-directory` carries no shell metacharacter and
  sits behind a read-only verb, so without a flag check the caller
  could point the appliance's bearer credentials at an endpoint of
  their choosing, disable TLS verification, dump the raw kubeconfig, or
  write files on the appliance. `parse_kubectl_command` therefore
  validates **every** flag — in both the global position before the
  verb and the argument position after it, in both attached
  (`--flag=value`) and separated (`--flag value`) forms — against a
  positive allowlist of read-only scoping/formatting options
  (`-n`/`--namespace`, `-o`/`--output`, `-l`/`--selector`,
  `--field-selector`, `--sort-by`, logs-scoping options, ...). Endpoint,
  context, credential and TLS trust are owned by the appliance's own
  kubeconfig, never by the caller's command line. Off-allowlist flags
  (including unknown flags and streaming `--watch`) fail closed. A
  separated value that itself starts with `-` is not swallowed as the
  preceding flag's value, so `--namespace --server=…` cannot smuggle a
  rejected flag past the walk. The rejection message names the flag but
  never echoes its (possibly secret) value.

  Stderr from the appliance is truncated at 4096 chars, matching the
  `PwshRunError` convention.

- **`holodeck.logs.tail`** (group `logs`). Runs `tail -n <lines>
  /holodeck-runtime/logs/<component>*.log` over **plain SSH** (no pwsh
  indirection — the cmd is a stock POSIX pipeline). `component` is
  restricted to `[A-Za-z0-9._-]+` at both the schema and handler layers
  so shell metacharacters and directory traversal can't slip through;
  `lines` is clamped to `[1, 5000]`. Returns `{files: [{path, lines}],
  raw, lines_requested}` parsed via the GNU `tail` `==> path <==` header
  convention; single-file matches surface as `path=None`.

- **`holodeck.networking.show`** (group `networking`). Composes four
  sub-commands: `vtysh -c 'show bgp summary'`, `vtysh -c 'show ip
  route'`, `pwsh` of `Get-DnsServerZone | ... | ConvertTo-Json`, and
  `cat /var/lib/dhcp/dhcpd.leases`. Returns
  `{bgp: {summary_text, ok}, routes: {text, ok}, dns: {zones, total,
  ok}, dhcp: {leases_text, ok}}`. Each sub-section's `ok` flips false
  when its sub-command failed or produced empty output, so a single-
  component failure doesn't blank the whole response.

- **`holodeck.disk.usage`** (group `diagnostics`, G3.18-T1 #2153). Runs
  `df -B1 /` for root-fs total/used/available bytes plus `du -sb` on each
  dir in the fixed `GROWTH_DIRS` constant (`/var/backups`,
  `/holodeck-runtime`). Returns `{root_fs: {total_bytes, used_bytes,
  avail_bytes, percent_used, ok}, growth_dirs: [{path, used_bytes, ok},
  ...]}`. `percent_used` is computed from the byte counts (not `df`'s
  rounded `Use%`). Failure isolation mirrors `networking.show`: each
  sub-command runs through `_safe_run_text` (SSH/OSError → `""`) and
  `parse_disk_usage_output` flips only the empty entry's `ok`. The op
  takes **no path parameter** — the measured dirs are a code constant, so
  it can never become an arbitrary `du`. This is the complete
  pre-eviction disk signal for the 74 GB root fs (VCF-9.x backup fill).

- **`holodeck.backups.list`** (group `backups`, JSONFlux-shaped, #2847).
  Runs `find <dir> -maxdepth 1 -type f -printf '%T@ %s %p' | sort -rn`
  over **plain SSH** — the read-only half of `holodeck.backups.prune`'s
  own listing pipeline (same `find -maxdepth 1`, plus a `%s` size field,
  minus the destructive `tail | cut | xargs rm` tail). An optional `path`
  sub-directory is confined to `/var/backups/**` via `resolve_backup_dir`
  (the shared resolver both this op and `backups.prune` call, so the
  single `bound_backup_path` safety bound is never duplicated). Returns a
  `{rows, total}` envelope with one `{path, mtime, size_bytes}` row per
  file, **newest-first** — the exact ordering `backups.prune` uses to pick
  its keep window, so a caller can pass the same `keep_newest` a prune
  request proposes and locally read off which rows (index ≥ `keep_newest`)
  that prune would delete, without executing anything. A transport failure
  surfaces as `{rows: [], total: 0, error}` so an empty result from a
  broken connection is distinguishable from a genuinely empty directory
  (the backup-landing health-check signal). Closes the inspection half of
  the same root-SSH fallback the `backups.prune` write op retired: a
  reviewer approving that destructive prune is no longer asked to do so
  blind. `parse_backup_listing_output` is the pure parser. `safety_level=
  safe`, `requires_approval=False` — pure `find`, no mutation.

### Write ops (G3.18-T2 surface, approval-gated)

`ops_write.py` (module `WRITE_OPS`, appended to `HOLODECK_OPS` via
`_holodeck_ops()`) adds three **remediation** write ops. Every one registers
`safety_level="dangerous"`, `requires_approval=True`. The `requires_approval`
flag is the *only* thing the connector controls: the dispatcher's
`policy_gate` routes a USER-principal dispatch of such an op to the human
approve-queue (`awaiting_approval`) rather than executing or hard-denying it,
and floors an agent dispatch there too. The handler bodies run only on the
`_approved=True` resume path; the `DISPATCH` audit row is written by the
dispatcher on that execute path (the same row every op gets).

Each handler validates its inputs against a fixed allowlist and **rejects
before composing** the command, then `shlex.quote`s every interpolated
token — the same reject-then-compose posture as `_COMPONENT_SAFE_RE` /
`parse_kubectl_command`. `bound_backup_path` / `bound_image_tar` are the pure
path-bounding helpers (unit-tested directly); `resolve_backup_dir` wraps
`bound_backup_path` with the root-default behaviour and is the single
resolver `backups.prune` and `backups.list` (#2847) both call.

- **`holodeck.k8s.pods.gc`** (group `k8s-write`). Runs one
  `kubectl delete pods --field-selector status.phase=<phase>` per requested
  terminal phase. The only inputs are the terminal-phase set (each element
  must be `Failed` or `Succeeded`; default both) and an optional bounded
  `namespace` (RFC-1123 label). There is **no** name, label-selector, or
  arbitrary-field surface, so a live phase (`Running`/`Pending`) or a named
  pod cannot be targeted. Note: kubectl's `--field-selector` supports only
  `=`/`==`/`!=`, not the set-based `in (...)` operator (that exists only for
  the label `--selector`), so the issue table's `status.phase in
  (Failed,Succeeded)` shorthand is realised as one equality delete per
  terminal phase — same bound, valid syntax.
- **`holodeck.backups.prune`** (group `backups-write`). Removes backup
  artefacts under `/var/backups`, keeping the newest `keep_newest` (explicit
  int, required). An optional `path` sub-directory is resolved via
  `resolve_backup_dir` (shared with the `holodeck.backups.list` read op,
  #2847), which defaults to the root and routes any sub-path through
  `bound_backup_path` — rejecting any value that escapes `/var/backups/**`
  (relative/absolute traversal, or the root itself). The listing runs at
  `find -maxdepth 1` so the prune never recurses into a subtree.
- **`holodeck.images.import`** (group `images-write`). Runs
  `ctr -n k8s.io images import <tar>` to side-load an image so kubelet sees
  it. `tar_path` must match `/root/containerd-images/*.tar` (single `.tar`
  file, no nesting/traversal), validated via `bound_image_tar`.

These three retire the recovery fallback where the VCF-9.x backup-fill outage
(Initiative #2145) was fixed by hand-run local root SSH with no MEHO audit
row.

### Deploy-lifecycle ops (#2908 surface, Initiative #2907)

`ops_deploy.py` (module `DEPLOY_OPS`, appended to `HOLODECK_OPS`) adds four ops
that make the consumer's hand-run fresh-deploy procedure (`New-HoloDeckConfig`
/ `New-HoloDeckInstance` / the three per-appliance patches) dispatchable as
governed steps. Three are `dangerous` / `requires_approval=True`; one is a
`safe` read.

**Secret hygiene is by construction.** `_run_command` has no `env`/`stdin`
seam and the `_pwsh` contract forbids credential material in the (argv-visible)
script body, so **none of these ops handles secret bytes**. The vCenter
deploy-target SSO credentials and the Broadcom build token live on the
appliance — in its staged deploy environment (`$env:VC_USER`/`$env:VC_PW`) and
in the persisted config (`/holodeck-runtime/config/<id>.json`, written by
`New-HoloDeckConfig`) — and the composed PowerShell references them by variable
**name** only. Nothing credential-shaped can reach the params-echo, audit, or
broadcast surfaces.

- **`holodeck.config.apply`** (group `deploy-config`). Composes
  `New-HoloDeckConfig` **without `-Default`** (the flag triggers a buggy
  internal `Set-HoloDeckConfig`) + `$null = Import-HoloDeckConfig`. Params carry
  only non-secret config (version enum, variant, external IP, master CIDR / VLAN,
  placement refs, and a 9.x-only `depot` whose `credential_ref` is a Vault
  **path**). **Depot params are refused for a VCF 5.2.x version** (5.2 deploys
  via Cloud Builder, no offline depot): the refusal is encoded in the parameter
  schema as `if version~5.2 then not required depot`, so the framework
  `validate_params` (Draft 2020-12) rejects it **pre-park** — an operator never
  approves a config that would fail. `validate_config_apply_params` re-expresses
  the same rule as a clear string for the handler belt-and-braces and the direct
  test. It is deliberately **not** pinned credential-class, so it keeps its
  generic param-echo preview (no secret is ever a param). **Fail-closed
  (#3081):** the op reports `applied: true` **only** when the cmdlet returns a
  non-null `configID` **and** the persisted config file
  (`/holodeck-runtime/config/<id>.json`) exists on disk — a verify-after-write
  via `Test-Path` folded into the same script. A run that writes nothing (the
  CLIXML import-warning corrupting stdout, or `New-HoloDeckConfig` failing
  silently) returns `{applied: false, config_id, error}` instead of the
  self-contradictory `{applied: true, config_id: null}` that let a runbook
  proceed on a false premise.
- **`holodeck.instance.start`** (group `deploy-lifecycle`). Runs a **single-shot
  fail-fast `Connect-VIServer` precheck before launch**: a pwsh script reads the
  persisted config on the appliance, builds a `PSCredential`, and makes exactly
  one `Connect-VIServer … -ErrorAction Stop` attempt. This catches a
  stale/locked deploy-target credential *before* Holodeck's 4-retry-in-13s loop
  storm-locks the SSO account (consumer #258 lesson). On any precheck failure
  the instance is **never** launched. On a green precheck `New-HoloDeckInstance`
  is started under `tmux new-session -d` (survives the dispatch returning) and
  the op returns immediately with the instance id + a start marker.
- **`holodeck.instance.status`** (group `deploy-status`, `safe`, no approval).
  Maps `Get-HoloDeckInstance` into a flat envelope (`status`
  running/completed/failed/unknown, `current_step`, `started_at`,
  `updated_at`, `instance_id`, `notes`). The flat `status` field is the target
  a runbook `OperationCallVerify` step matches and a deploy-in-progress Sensor
  asserts on, so its shape never depends on approval state. `map_instance_status_envelope`
  re-maps the **benign VCF 5.2.x terminal quirk** — the final
  `Sync-HolodeckComponents` step throwing *"No route to host"* because VCF
  Operations is not deployed in 5.2 — to `status="completed"` plus a structured
  `notes` entry, so a verify/Sensor built on it never reads a fully-deployed
  5.2.x lab as broken.
- **`holodeck.router.patch`** (group `deploy-patch`). Idempotent
  verify-then-apply of the three per-appliance patches (C1 `SddcMgmtDeployment.psm1`
  `-eq 7`→`-ge 7`; C2 `HoloDeck.psm1` `BroadcomBuildToken` SecureString wrap; C3
  `Auth.psm1` `Connect-VIServer` PSCredential). Per patch it greps the *applied*
  marker (present at the expected count → `already_applied`), else the
  *unpatched* marker (present → timestamped backup + `sed` + re-verify →
  `applied`), else `not_applicable`. A **second run reports all
  `already_applied`** (no `sed`). On a **9.1 HoloRouter it runs verify-only**
  (patches fixed upstream) — it greps but never `sed`s. The patch strings are
  fixed code constants (C2 references `$env:brcm_build_token` by name), and the
  result reports only patch id / label / state — no file content. It **is**
  declared `credential_write` in `holodeck/op_classes.py` (broadcast defence-in-depth) per the
  `rke2.node.config.update` precedent.

### DNS name ops (#4066 surface, Initiative #2907)

A Holodeck 9.0 router runs its DNS service as dnsmasq in its own
Kubernetes. The lab's names live in ConfigMap `default/dnsmasq`, key
`hosts`, one `<address> <name>` line per server. dnsmasq answers the
reverse question from the same line. `ops_dns.py` (module `DNS_OPS`) adds
three ops in two groups, `dns` (read) and `dns-write`:

- **`holodeck.dns.host.list`** (`safe`, no approval). Runs
  `kubectl get configmap dnsmasq -n default -o json` and returns
  `{rows: [{address, names}], total}`.
- **`holodeck.dns.host.add`** (`caution`, `requires_approval=True`). Adds one
  `<address> <name>` line.
- **`holodeck.dns.host.remove`** (`dangerous`, `requires_approval=True`).
  Removes the line that is exactly `<address> <name>`.

Each write runs these steps and stops at the first failure (the answer's
`error` starts with the step name):

1. `input` — `address` must be an IP address (`ipaddress`, not unspecified,
   not multicast, no IPv6 zone id such as `%eth0`); `name` must be DNS labels joined by `.` (written in lower
   case). Nothing runs on the router before this check passes.
2. `read` — read the live ConfigMap and plan the change in Python.
   `conflict` — the add refuses a name or address that is already used with
   another partner; the remove refuses when the name or address is only in a
   line that is not exactly `<address> <name>`. When the line is already there
   (add) or not there (remove), nothing is written and nothing restarts:
   `state` is `unchanged` or `absent`. This makes both writes safe to run twice.
   Such a call still runs the `wait` step (6) and then the `check` step (7):
   a retry during a rollout must not report `verified` while an old pod
   still serves the old file.
   Size limit: the patch (step 4) carries the whole `hosts` value twice, old
   and new, inside one shell argument, and Linux allows at most 128 KiB per
   argument. MEHO builds the patch command first and refuses with a `write`
   error, before the save, when it is longer than 100,000 bytes
   (`PATCH_CMD_MAX_BYTES`). That is a `hosts` value of about 50 KB.
3. `save` — `kubectl get configmap dnsmasq -n default -o yaml >
   /root/dnsmasq-cm-pre-<op>-<time>.yaml`, returned as `backup_path`.
4. `write` — `kubectl patch configmap dnsmasq -n default --type json -p
   <patch>`. The JSON Patch has two steps: `test` that `/data/hosts` still
   holds the value MEHO read, then `replace` it. A change made by someone else
   in between makes the patch fail; nothing is overwritten. The API server
   answers a failed `test` step with a generic 422 text only, so on a failed
   patch MEHO reads the ConfigMap again. When `hosts` already holds MEHO's
   new value, the patch went through and only the answer was lost (for
   example a timeout), so MEHO goes on with `verify`, `restart` and `wait`.
   When `hosts` holds a third value, the error is
   `conflict: the hosts list changed since MEHO read it; nothing was written;
   run the op again`. Otherwise the router's text is kept. The patch is
   built with `json.dumps` and passed as one `shlex.quote`-d argument (see
   the size limit above).
5. `verify` — read the ConfigMap again: `hosts` must be the new value and
   every other key unchanged. `lines_before` / `lines_after` count the
   `hosts` lines.
6. `restart` / `wait` — `kubectl rollout restart` and `kubectl rollout status
   --timeout=180s` on `deployment/dnsmasq-dns-deployment` (the pods read the
   file only at start), then poll `kubectl get pods -n default -o json` until
   all the deployment's pods share one `pod-template-hash` and none is
   shutting down. `rollout status` alone returns while an old pod can still
   answer with the old file. The SSH call for `rollout status` gets a 200 s
   timeout, longer than its own 180 s wait (other steps keep the 30 s SSH
   default). When the router does not answer in time, `error` says so.
7. `check` — ask every running dnsmasq pod, by its pod IP, with
   `dig +noall +answer +comments @<pod ip> <name> A|AAAA` and the same with
   `-x <address>`. The `+comments` header carries the DNS status
   (`forward_status` / `reverse_status` in each check). `dig` exits with 0
   for every DNS status, so an empty `+short` answer would not tell a
   missing record from a failed query: only `NOERROR` and `NXDOMAIN` count
   as an answer, and `SERVFAIL`, `REFUSED` or a missing header make the
   check fail. `verified` is true when every pod answers as expected (the
   pair present after an add, absent after a remove). `dig` must be on the
   router; when it is missing or fails, or the SSH call for it fails,
   `verified` is false, the check's `error` says why, and the change stays.
   The answer still carries `state` and `backup_path`.

`holodeck.k8s.exec` stays read-only; these ops do not widen it.

Out of scope: a Holodeck 9.1 router, whose DNS service is Technitium (#3964).

### Delete an instance

`ops_remove.py` adds **`holodeck.instance.remove`** (group `deploy-remove`,
`destructive`, a person must approve each call). It deletes one nested lab
that `holodeck.instance.start` built: the nested ESX hosts and the VCF
Installer or Cloud Builder VM. Before, a teardown deleted each nested VM by
hand.

Parameters: `instance_id` (same pattern as `holodeck.instance.start`: 1 to 8
letters or digits), `reset_router` (default `false`), `move_state_files`
(default `true`) and an optional `config_id`.

How a call runs:

1. **Check (read-only).** One PowerShell script checks that
   `Remove-HoloDeckInstance` has `-InstanceID` (Holodeck 9.0.1 and later)
   and, when asked, `-ResetHoloRouter`; that `Get-HoloDeckInstance` exists
   (9.0.2 and later); reads `Get-HoloDeckInstance -InstanceID <id>`;
   lists the instance's files under `/holodeck-runtime/config`, `output` and
   `state` (the file name holds the id as a whole word, so `lab01-state.json`
   is not a file of `lab0`; or the file text holds `"<id>"`); and reads the
   instance's JSON files under `state`.
2. **Decide.**
   - A toolkit without the cmdlet, the parameter or the switch: `refused`,
     with a version message. MEHO does not guess.
   - An instance that `Get-HoloDeckInstance` does not list: `unchanged`.
     Nothing is deleted. (One exception: an interrupted remove, see step 5.)
   - A failing `Get-HoloDeckInstance`: `refused`, with its error text. A
     failed read is never taken as "not there".
   - A listed instance whose `Get-HoloDeckInstance` answer and state files
     name no VM: `refused`. MEHO does not delete what it cannot show in the
     preview.
   - The config to load: `config_id` when given, else the one config file
     that names the instance. None or several: `refused`, pass `config_id`.
3. **Run in the background.** Like `holodeck.instance.start`, the delete
   runs in a `tmux` session (`holodeck-remove-<id>`, output in
   `/holodeck-runtime/logs/<id>-remove.log`), and the call returns
   `started`. The script loads the config (`Import-HoloDeckConfig`), runs
   `Remove-HoloDeckInstance -InstanceID <id>` (plus `-ResetHoloRouter`),
   lists the VMs whose name still starts with `<id>-` (`Get-VM`), and, only
   when the delete did not fail, moves the listed files into
   `/holodeck-runtime/removed/<id>-<yyyyMMdd-HHmmss>/`. The vendor cmdlet
   removes only the current VMs of the instance and does not clean the
   config files, so these two steps close that gap. `Get-VM` stops on
   error: an empty search gives `leftover_vms: []`, a failed read keeps
   `leftover_vms` null and sets `leftover_error`. Each file moves on its
   own: a file that did not move goes to `move_errors` with the error text,
   and only moved files go to `moved_files`.
4. **Progress file.** The script writes
   `/holodeck-runtime/logs/<id>-remove.json` at the start, right after the
   cmdlet (so its outcome survives a reboot) and at the end (`state`
   running / done / failed, `deleted`, `error`, `leftover_vms`,
   `leftover_error`, `moved_to`, `moved_files`, `move_errors`, `resumed`).
   `deleted` says whether `Remove-HoloDeckInstance` finished: `null` until
   it returns, then `true`, or `false` with the `error`.
   `holodeck.instance.status` with `instance_id` reads it, and in the same
   command lists the `tmux` sessions: it adds `removal` and `leftover_vms`,
   and sets `status` to `removed` only when the delete finished without an
   error and `deleted` is `true`.
   A note names leftover VMs, a failed VM read and files that did not move.
   The caller deletes each leftover VM
   with `vmware.composite.vm.destroy`. The file stays after the remove, and a
   later deploy may use the same id. So an old file never hides a newer
   deploy: `status` becomes `removed` only when `Get-HoloDeckInstance` no
   longer lists the instance, or when the remove started after the listed
   deploy started. A remove that started before the deploy is kept only as
   history in `removal`. When MEHO cannot compare the two times, `status`
   stays as listed and a note says so. The status op tells "not listed"
   apart from a failed read, like the remove op does: a not-listed
   instance answers `null`, and a failed `Get-HoloDeckInstance` call keeps
   `status` as `unknown`, keeps its `error`, and adds a note. A failed read
   never sets `removed`.
5. **Interrupted run.** With `reset_router` the HoloRouter reboots at the
   end of the cmdlet and kills the script before the last two steps
   (leftover VMs, file move). The progress file then still says `running`
   while the `tmux` session `holodeck-remove-<id>` is gone. The status op
   reports `removal.state` `interrupted`. When `deleted` is `true` and
   `Get-HoloDeckInstance` no longer lists the instance (or the record is
   newer than the listed deploy), `status` is `removed` and a note says how
   to finish. When `deleted` is `null` (the script died before or inside
   the cmdlet), `status` is never `removed`: a note says that MEHO cannot
   tell whether the VMs were deleted. The second `holodeck.instance.remove`
   call with the same `instance_id` reads the record before it decides
   (`resumable` in `ops_remove.py`), also when the instance is still listed:
   - The instance is not listed: the call finishes the run. It runs only
     the two missing steps, without the cmdlet, keeps the old start time
     and `deleted` value in the progress file, and answers `started` with
     `resumed: true`. A run that was never confirmed ends with `deleted`
     `null`, so the status keeps that doubt and shows `leftover_vms`.
   - The instance is still listed, `deleted` is `true`, and the record is
     newer than the listed deploy (the state file still lists the instance
     until it moves): the call finishes the run the same way. No cmdlet, no
     second router reset.
   - The instance is still listed and the delete is not confirmed, or the
     record is older than the listed deploy, or MEHO cannot compare the two
     times: the call deletes the instance again, as a new run.
   - A record whose `tmux` session still runs, or that holds the cmdlet's
     error, is not resumed: an unlisted instance is `unchanged`, a listed
     one is deleted.

**Approval preview.** The preview builder (registered in `ops_remove.py`)
runs the same read-only check and takes the same resume decision as the
handler. Its `blast_radius` names the instance (`object`, with `found` and
`resume`), lists each VM and where it runs, the router reset and each file
that moves (`children`), and says `permanent`. An old toolkit, a failed
check, or an instance that is to be deleted but has no VM name makes the
builder raise, so the call does not wait for approval and the caller reads
why. An unknown instance waits with an empty list: the call will change
nothing. A call that finishes an interrupted remove lists only the files
that move, and its note says that no VM is deleted.

**No secrets.** No parameter is a password. The script reads the vCenter
login from the loaded config on the appliance, by variable name only. The
`Get-HoloDeckInstance` answer and the state files are never passed on:
`instance_vms` reads only known VM name fields (`Name`, `VMName`,
`HostName`, ...) whose value starts with `<id>-`, skips every field whose
name says it is a secret (password, token, credential, ...) with everything
under it, and ignores plain text outside a name field. So a password that
starts with the instance id never becomes a VM name in a result or a preview.

### `execute(target, op_id, params)`

Same dispatcher shim shape as bind9 and pfSense:

1. Look up the `EndpointDescriptor` row for `(tenant_id IS NULL, product,
   version, impl_id, op_id, is_enabled=True)`.
2. If absent → `result_unknown_op` envelope with the known-op-count for the
   triple.
3. Validate `params` against `descriptor.parameter_schema` → on failure,
   `result_invalid_params` envelope.
4. Resolve `descriptor.handler_ref` via `import_handler` (and bind to `self`
   for unbound method paths).
5. Invoke the handler; any exception → `result_connector_error` envelope.
6. Happy path → `OperationResult(status="ok", op_id, result, duration_ms)`.

### Two-phase registration

`__init__.py` runs two phases:

1. **Synchronous (import time).**
   `register_connector_v2(product="holodeck", version="9.0",
   impl_id="holodeck-ssh", cls=HolodeckConnector)` writes the v2 triple.
   No v1 dual-write — Holodeck has no chassis history.
2. **Asynchronous (lifespan startup).**
   `register_typed_op_registrar(register_holodeck_typed_operations)` queues
   the registrar; the lifespan calls it after `_eager_import_connectors`,
   and the registrar walks `HOLODECK_OPS` upserting one
   `endpoint_descriptor` row per op via `register_typed_operation`. The
   `embedding_service` kwarg is accepted and discarded — matches the
   bind9 / pfSense sibling shape; the helper uses the process-wide
   singleton fallback.

## Dependencies

Direct:

- `meho_backplane.connectors.adapters.ssh.SshConnector` — pooled-asyncssh
  base + `_auth_config`.
- `meho_backplane.connectors._shared.pwsh` — the shared PowerShell-over-SSH
  transport (hoisted from the package-local `_pwsh.py` in #3260); the
  **single** seam between the connector's Python handlers and the
  appliance's pwsh surface.
- `meho_backplane.connectors.registry.register_connector_v2` — v2 entry.
- `meho_backplane.operations.typed_register.register_typed_operation` /
  `register_typed_op_registrar` — async typed-op upsert + lifespan
  scheduling.

External (pinned in `backend/pyproject.toml`):

- `asyncssh>=2.18,<3.0` — `import_private_key`, `connect`, `SSHClientConnection.run`.
- Python stdlib `base64`, `json` — pwsh encoding + ConvertTo-Json output
  parsing. No third-party CliXml parser; the design correction in T1
  drops `pyclixml`.

## Known issues

- **Encoded payload visibility.** The base64 payload **does** appear on the
  remote process argv — that's the contract of `-EncodedCommand`. A
  privileged observer on the appliance can decode it and see the original
  PowerShell text. This matches the consumer wrapper's behaviour and is
  acceptable because Holodeck ops are deterministic; no per-call
  credentials are interpolated into the script body. Callers must not pass
  credential material in `script`.
- **`ConvertTo-Json` depth.** PowerShell's `ConvertTo-Json` defaults to
  `-Depth 2`, which silently truncates deeply nested cmdlet output. The
  helper recommends `-Depth 4` via the `PWSH_DEFAULT_DEPTH` constant and
  the #371 body's recipe; callers assemble their scripts to include the
  explicit `-Depth N` argument.
- **Holodeck simulator absence.** No published Holodeck simulator exists;
  T1 ships unit tests against a mocked `pwsh_run` seam, and T3 will layer
  recorded-fixture integration tests against captured cmdlet payloads (the
  same shape as G3.6's VCF management-plane fixtures).
- **Pod-clone deferred.** The consumer's sister wrapper
  `clone-holodeck-instance.sh` covers nested-lab provisioning end-to-end
  (multi-step orchestration). That op shape is **out of scope** for
  v0.2 — it belongs in a Runbooks Initiative once the runbook engine ships
  (Goal G11 per the v0.1 spec sequencing). The standalone wrapper stays in
  place; G3.8 provides the inspection ops a runbook would compose against.
- **`Get-HoloDeckInstance` has no published answer shape.** The remove
  preview finds VMs by walking the answer for names that start with
  `<instance-id>-`, and finds the instance's config by looking for
  `"<instance-id>"` in the config files. When that finds no config, or
  several, the call is refused and asks for `config_id`. Check both against
  a real HoloRouter answer when one is recorded.

## References

- Initiative #371 (G3.8 typed-SSH connector).
- Task #853 (T1 skeleton + the package-local `_pwsh.py`).
- Task #4066 (Initiative #2907) — the DNS name ops.
- JSON Patch `test` operation: https://www.rfc-editor.org/rfc/rfc6902#section-4.6
- Task #3260 (Initiative #3259) — hoist of the pwsh transport to the shared
  `_shared/pwsh.py`.
- Adapter: `SshConnector` (G0.2-T4 #243).
- Sibling SSH-canary precedents: bind9 (G3.4) `connectors-bind9.md`,
  pfSense (G3.7) `connectors-pfsense.md`.
- PowerShell `-EncodedCommand`:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh
- `ConvertTo-Json`:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json
- VMware Holodeck Toolkit: https://core.vmware.com/holodeck-toolkit
- Consumer wrapper replaced: `scripts/holodeck.sh` in the consumer's private repo.
- Sister wrapper deferred to Runbooks Initiative:
  `scripts/clone-holodeck-instance.sh` in the consumer's private repo.
