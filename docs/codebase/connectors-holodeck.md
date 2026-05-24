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
probe, and the `_pwsh.py` PowerShell-over-SSH helper. G3.8-T2 (#854) adds
the 7 read ops (`config.show`, `pod.list`, `pod.info`, `service.list`,
`k8s.exec`, `logs.tail`, `networking.show`) for a final total of 8 ops
registered under `connector_id="holodeck-ssh-9.0"`. G3.8-T3 (#855) ships
the CLI verbs + E2E acceptance suite + onboarding doc.

Source: `backend/src/meho_backplane/connectors/holodeck/`.

## Key types

- **`HolodeckConnector`** (`connector.py`) — `SshConnector` subclass. Class
  attributes: `product="holodeck"`, `version="9.0"`, `impl_id="holodeck-ssh"`.
  Inherits the per-target asyncssh connection pool, `_auth_config`,
  `_run_command`, and `aclose()` from the adapter. T1 shipped `fingerprint`,
  `probe`, `execute`, `about`. T2 (#854) adds the 7 read-op bound-method
  shims: `config_show`, `pod_list`, `pod_info`, `service_list`, `k8s_exec`,
  `logs_tail`, `networking_show`.

- **Read-op handlers + parsers** (`ops_read.py`) — `holodeck_config_show`,
  `holodeck_pod_list`, `holodeck_pod_info`, `holodeck_service_list`,
  `holodeck_k8s_exec`, `holodeck_logs_tail`, `holodeck_networking_show`.
  Pure parsers: `parse_kubectl_command` (verb-safelist enforcement),
  `parse_logs_tail_output` (GNU `tail` `==> path <==` header split),
  `parse_networking_payload` (four-section composer). `KubectlSafetyError`
  is the `ValueError` subclass the dispatcher's error envelope picks up
  when a mutating verb slips through.

- **`_pwsh.py` — the novel primitive.** Houses `encode_pwsh_command(script)`
  (UTF-16LE-base64 per the `-EncodedCommand` convention), `pwsh_run(connector,
  target, script)` (runs `pwsh -NoProfile -NonInteractive -EncodedCommand
  <encoded>` over the pooled SSH connection and parses `ConvertTo-Json`
  stdout via stdlib `json`), `PwshRunError` (structured failure with the
  truncated stderr fragment but no script body or auth material), and
  `PWSH_DEFAULT_DEPTH = 4` (the recommended `ConvertTo-Json -Depth` per the
  #371 body).

  **Design correction (2026-05-21).** The Initiative body originally
  specified CliXml output via `-OutputFormat Xml` + `pyclixml` parsing. T1
  supersedes that with `ConvertTo-Json` + stdlib `json`: the #371 body's
  fingerprint/probe/op examples all already use `ConvertTo-Json`, and Json
  drops the undecided `pyclixml` dependency. The wire encoding (UTF-16LE-
  base64) and the `-EncodedCommand` argv shape are unchanged from the
  original design.

- **Op metadata** (`ops.py`) — the `HolodeckOp` dataclass and the
  `HOLODECK_OPS` tuple. T1 shipped the single `holodeck.about` canary; T2
  (#854) extends the tuple via `_holodeck_ops()` which splats `READ_OPS`
  (the 7 read ops defined in `ops_read.py`) onto the canary. The pattern
  mirrors bind9's `_bind9_ops()` and pfSense's `_pfsense_ops()`.

- **`parse_photon_version`** (`connector.py`) — pure parser for
  `/etc/photon-release` output; recovers the `<major>.<minor>(.<patch>)?`
  Photon version token from the appliance's release file.

## Control flow

### Auth (inherited from `SshConnector._auth_config`)

`_auth_config` is called by the base `SshConnector._connect` method before
opening any TCP connection. It inspects `target.secret_ref`:

1. `ssh_private_key` present → parse via `asyncssh.import_private_key`,
   return `{username, client_keys=[key]}`. This is the key-preferred path
   (mirrors the wrapper's `PreferredAuthentications=publickey,password`
   header).
2. No `ssh_private_key`, `password` present → return
   `{username, password}`. This is the **default** path for the HoloRouter
   OVA, which ships with root password auth enabled.
3. Neither set → `ValueError` (the base adapter's standard message). The
   probe folds this into `ssh_auth_failed`.

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

### Read ops (T2 surface)

The seven T2 read ops route through the dispatcher's standard
`call_operation` path. Each registers via `register_typed_operation()` with
`safety_level="safe"`, `requires_approval=False`, and an
`llm_instructions.when_to_use` blob that includes the SSH-only transport
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
  the handler PowerShell-quotes single quotes via doubling. Returns
  `{pod: <dict>}` or `{pod: None, error}` on cmdlet / missing-id failure.
- **`holodeck.service.list`** (group `service`, JSONFlux-shaped). Runs
  `Get-Service | Where-Object { $_.Name -like 'Holo*' } | Select-Object
  Name,Status,DisplayName | ConvertTo-Json -Depth 4`. Same `{rows, total}`
  envelope and the same single-dict / `null` normalisation as `pod.list`.
- **`holodeck.k8s.exec`** (group `k8s`, **read-only**). Forwards a
  ``kubectl`` command to the in-appliance K8s cluster. Two layers of
  read-only enforcement:

  1. *Schema layer.* The `command` parameter has a `pattern` regex anchored
     at `^kubectl(\s+--?...)*` followed by `(get|describe|logs|top|explain|
     api-resources|api-versions|cluster-info|version)`. The dispatcher's
     `validate_params` rejects mutating verbs before reaching the handler.
  2. *Handler layer (authoritative).* `parse_kubectl_command` re-parses
     the command via `shlex.split`, walks past leading global flags
     (both `--flag=value` and `--flag value` forms), and confirms the
     verb is in `_K8S_READ_VERBS`. Mutating verbs raise
     `KubectlSafetyError`; the handler returns
     `{stdout, stderr, exit_status, error}` with `error` set to the
     safety-check message. The full command body is **not** echoed back
     in the error message — only the rejected verb — so operator-supplied
     resource names don't bleed into structured error envelopes.

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
- `meho_backplane.connectors.holodeck._pwsh` — the PowerShell-over-SSH
  helper; the **single** seam between the connector's Python handlers and
  the appliance's pwsh surface.
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

## References

- Initiative #371 (G3.8 typed-SSH connector).
- Task #853 (T1 skeleton + `_pwsh.py`).
- Adapter: `SshConnector` (G0.2-T4 #243).
- Sibling SSH-canary precedents: bind9 (G3.4) `connectors-bind9.md`,
  pfSense (G3.7) `connectors-pfsense.md`.
- PowerShell `-EncodedCommand`:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_pwsh
- `ConvertTo-Json`:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/convertto-json
- VMware Holodeck Toolkit: https://core.vmware.com/holodeck-toolkit
- Consumer wrapper replaced:
  https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/holodeck.sh
- Sister wrapper deferred to Runbooks Initiative:
  https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/clone-holodeck-instance.sh
