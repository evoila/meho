# Governed guest-operations channel for arbitrary VMs (vmware-rest)

## Overview

The governed way for an agent or operator to reach *inside* an arbitrary
VM's guest OS — list running processes, read a file, inspect guest
network state, place a file — without falling back to out-of-band
`govc guest.run` / `scp` / an unmanaged SSH session. Every step is a
`vmware.composite.vm.guest.*` operation on the `vmware-rest` connector
(`vmware-rest-9.0`), dispatched through the ordinary
`call_operation(connector_id, op_id, target, params)` path so policy,
audit, broadcast, JSONFlux, and (for the one write) approvals apply per
call.

The channel rides **VMware Tools guest operations** (the vim
`GuestOperationsManager` family) over the existing VI-JSON write seam
(`VmwareRestConnector._post_vmomi_json`) — the same substrate the #2890
mutating-vmomi wave used. No `pyvmomi`, no in-guest agent of MEHO's own,
no shell. Guest OS credentials are resolved from the target's Vault
`secret_ref` and never travel in operation params.

The channel shipped in two increments: **#3100** (four read-only guest
ops plus one approval-gated `guest.file.write`, proving the governance
posture) and **#3255** (`guest.program.run` — the freeform in-guest
program-execution tier #3100 deliberately deferred, now lifted).

## The design fork: (a) vim guest-ops over (b) generic-ssh

The issue (#3100) asked for one of two shapes, or both as tiers:

- **(a)** a `vmware.composite.vm.guest.*` family riding vim
  `GuestOperationsManager` (VMware Tools guest operations) via the
  VI-JSON seam.
- **(b)** a minimal `linux-ssh` typed connector with per-target
  credentials and a safelisted verb model.

**Operator decision (2026-08-29): build (a) now; keep (b) as a possible
later tier.** Rationale:

1. **No extra network reach, no extra listener.** Guest operations flow
   vCenter → ESXi → VMware Tools inside the guest. MEHO already has a
   governed session to vCenter (the connector's authenticated
   `/api/session`), so a guest op needs no new firewall path, no SSH
   daemon reachable from MEHO, and works for guests with **no** network
   route to MEHO at all (first-boot appliances behind a management
   VLAN — exactly consumer case 2 below). The generic-ssh path (b)
   requires IP reachability MEHO frequently does not have.
2. **Cross-version, cross-guest by construction.** VMware Tools guest
   operations are a stable vim surface across ESXi/vCenter versions and
   across Linux/Windows guests. One connector implementation covers the
   fleet; (b) would need per-OS command dialects.
3. **Reuses the proven mutating-vmomi substrate.** `_post_vmomi_json`,
   `vim_moref`, `retrieve_properties_body`, `unwrap_vim_value`, the
   `_write_vmomi_sub_op` governance gate, and `poll_vim_task` already
   exist and are spec-reconciled against the pinned `vi-json.yaml`. The
   guest ops are new op-ids on that substrate, not a new transport.
4. **Structured sub-ops, not a shell.** (a) exposes discrete, typed
   verbs (`guest.process.list`, `guest.file.read`, …) whose parameters
   are JSON-schema-validated. Even the freeform-exec op `guest.program.run`
   is a typed verb — an explicit `program_path` + `arguments` + `env`
   contract with `dangerous` / `requires_approval` governance around every
   call — not an open shell surface.

### The deferred freeform-exec tier, now lifted (#3255)

#3100 deferred one capability inside branch (a): **running an arbitrary
program in the guest.** vim's only exec primitive is `StartProgramInGuest`,
which starts a detached process and returns a PID — output is *not*
captured; to capture output you redirect to a file, poll
`ListProcessesInGuest` for the exit code, then read the file back. #3100
judged that the crux of the dangerous-by-default posture and wanted a
dedicated design pass, so it shipped only the reads plus the clean
`guest.file.write`, and served the *reported*-state cases structurally
(`guest.net.show` reads Tools-reported network state without running
anything in the guest).

**#3255 lifts that deferral** as `vmware.composite.vm.guest.program.run`,
under the same governance as `guest.file.write`
(`safety_level="dangerous"`, `requires_approval=True`). It maps
`StartProgramInGuest` directly (fire-and-forget, returns the PID), and
when `wait=true` polls `ListProcessesInGuest` for the exit code and
start/end times (see "The exit-code poll" below). It deliberately does
**not** build the output-capture round-trip (redirect-to-file + read-back)
— that stays a caller-composed pattern (`program.run` writing to a file,
then `guest.file.read`), because baking it in would multiply the blast
radius and the audited surface for the common "run this and tell me the
exit code" case. `arguments` and `env` may carry secrets, so their values
are kept off the governed decision surfaces (the sub-op policy gate, the
park preview, the result, and the audit-row hash) — but note that, exactly
like `guest.file.write`'s `content`, they are **not** scrubbed on every
surface (see the safety model for the precise split and the operator
guidance on not passing bare secrets).

**Why (b) is still deferred, not discarded.** After #3255, one capability
branch (a) genuinely cannot serve remains:

- **Guests without VMware Tools.** No Tools → no guest operations at all;
  (b) — a `linux-ssh` typed connector — is the only channel there. If and
  when a Tools-less guest is a concrete need, (b) is the follow-up. This
  channel does **not** build it.

(The other historically-cited gap — "running a command and capturing its
output" — is now served by `guest.program.run` for exit code + status,
and by the `program.run` → `guest.file.read` compose for stdout.)

## Safety model

The channel is **dangerous by default**. The posture, in order of
load-bearing-ness:

1. **Secrets via `secret_ref` only — never in params.** Guest OS
   credentials are read from the target's Vault secret under the
   operator's identity (`load_basic_credentials(target, operator,
   fields=("guest_username", "guest_password"))`), exactly like the
   connector already reads the vCenter service-account
   `username`/`password` from the same secret. The guest credential
   never appears in an operation parameter, an `OperationResult`, an
   audit row, a broadcast event, or a log line — it lives only as the
   ephemeral in-memory `NamePasswordAuthentication` object the vim
   request body carries. For the guest **login** credential this is a
   deliberate improvement on the GOSC `credential-class` ops
   (`guest.customization_spec.create`), which accept the Windows admin
   password *in params*: here the login secret is never in params to begin
   with. For any secret an operator might pass *inside* `guest.program.run`'s
   `arguments` / `env` (which, like GOSC's password param, do ride in
   params), `program.run` **matches** the GOSC posture rather than
   exceeding it — both are pinned in `_CREDENTIAL_WRITE_OPS` so their
   broadcasts clamp to aggregate-only (point 4), and both rely on the
   park-preview builder to keep values off the reviewer surface. The pin is
   what makes that parity true; without it `program.run` would have been
   *weaker* than GOSC on the broadcast surface.
2. **Structured sub-ops, not freeform shell.** Each op is a discrete
   typed verb with a JSON-schema parameter contract. `guest.program.run`
   *does* run an arbitrary program, but through an explicit
   `program_path` + `arguments` + `env` contract with the full
   `dangerous` / `requires_approval` governance below around every call —
   not an open `run(cmd)` shell endpoint.
3. **The writes are approval-gated.** Both `guest.file.write` and
   `guest.program.run` are registered `safety_level="dangerous"` +
   `requires_approval=True` and ride the standard approvals plane: the
   dispatcher parks an `ApprovalRequest` before the write executes, a
   human approves/rejects via console/CLI (never MCP — v0.1-spec §7), and
   the park stores a redaction-safe `proposed_effect` preview
   (`guest.file.write`: path + byte size + overwrite intent, never the
   content; `guest.program.run`: VM + program path + working directory +
   wait flag + argument *byte size* + env-var *names*, never the argument
   string or env values). The reads are `safety_level="safe"` / no
   approval. Both writes gate *first* through the #2254
   `enforce_subop_policy` seam: a parked / denied gate resolves no guest
   credential and starts / writes nothing.
4. **`arguments` / `env` are kept off the governed decision surfaces —
   but are not scrubbed everywhere.** A command line or an environment
   variable can carry a secret (a password argument, an API token in an
   env var). For `guest.program.run` these are kept off the surfaces an
   operator's governance decision reads: they never reach the **sub-op
   policy gate** params (`_run_gate_params` passes VM + program path +
   working directory only), the **park preview** `proposed_effect` (the
   bespoke builder echoes program identity + argument *byte size* + env-var
   *names*, never the values), the **operation result** (only PID / exit
   code / times), or a **log line** (the handler logs nothing; the vim
   `_post_json` seam does not log its body). The **audit row** stores a
   `params_hash`, never the raw params.

   **Broadcast is clamped to aggregate-only.** `guest.program.run` is
   pinned in `broadcast/events.py`'s `_CREDENTIAL_WRITE_OPS` frozenset, so
   `classify_op` returns `credential_write` and the dispatch broadcast
   collapses the whole params dict to aggregate-only — it never ships the
   `arguments` string or `env` values on the SSE feed / Slack mirror /
   durable `BroadcastEvent` row. This is the exact remedy the codebase
   already applies to `k8s.job.create` (inline pod-template `env`) and the
   GOSC `guest.customization_spec.create` write, so `program.run` **matches
   the GOSC broadcast posture** rather than being weaker than it.
   `guest.file.write` is pinned in the same frozenset (#3298) for the
   identical reason — its `content` param (the file body) is not a
   secret-*named* / -shaped key, so without the pin the plain `.write`
   classification would ship the whole file body on the feed. **That
   broadcast pin is distinct from the park-preview redaction in point 3:**
   the preview already echoes only path + byte size + overwrite intent,
   but that governs the *reviewer* surface — the frozenset pin is what
   keeps `content` off the SSE feed / Slack mirror / durable
   `BroadcastEvent` row. With it, no guest-ops write rides broadcast
   unredacted.

   **What still carries the values — the same characteristic as
   `guest.file.write`'s `content`, from shared machinery, not a
   `program.run` bug:** the durable **`ApprovalRequest.params`** row stores
   the *full* params verbatim (the resume path re-dispatches the exact call
   after a human approves, so it must persist them), and **flight-recorder
   vendor-call spans** (only when a capture policy is active) record the
   `StartProgramInGuest` request body. So **operators must not put bare
   secrets in `arguments` / `env`.** Where a program needs a secret, pass
   its env-var *name* and stage the value guest-side (a file the guest user
   reads, a Vault-agent-templated env file), or use the guest's own
   credential store — the same discipline the guest OS credential itself
   follows (point 1, resolved from `secret_ref`, never a parameter).
5. **Full audit of command + result + truncated output.** Every op
   dispatches through the synchronous append-only audit path
   (v0.1-spec §6). The audit row names the op, the VM, and the outcome;
   set-shaped and byte-shaped outputs are truncated (JSONFlux result
   handle for the process list; a byte cap on file reads) so the audit
   and the agent surface never carry an unbounded guest payload.
6. **Read/write split is explicit.** The four reads never mutate guest
   state; the two writes (`guest.file.write`, `guest.program.run`) are the
   only mutating ops and the only ones that park.

## The op family

All six ops register under the `guest_ops` operation group
(`group_key="guest_ops"`) and carry `op_id`s under the
`vmware.composite.vm.guest.*` namespace (distinct from the GOSC
customization writes, which live in the sibling `guest` group). Each
carries full `parameter_schema` + `response_schema` (JSON Schema
2020-12), `tags`, `safety_level`, and `llm_instructions`.

| op_id | vim method (VI-JSON) | Guest creds? | Safety | Shape |
|---|---|---|---|---|
| `vmware.composite.vm.guest.process.list` | `GuestProcessManager.ListProcessesInGuest` | yes | safe | set → JSONFlux handle |
| `vmware.composite.vm.guest.env.read` | `GuestProcessManager.ReadEnvironmentVariableInGuest` | yes | safe | set → JSONFlux handle |
| `vmware.composite.vm.guest.net.show` | `PropertyCollector.RetrievePropertiesEx` on `guest.net` + `guest.ipStack` | **no** (Tools-reported) | safe | aggregate dict |
| `vmware.composite.vm.guest.file.read` | `GuestFileManager.InitiateFileTransferFromGuest` + guest-transfer GET | yes | safe | truncated content + attrs |
| `vmware.composite.vm.guest.file.write` | `GuestFileManager.InitiateFileTransferToGuest` + guest-transfer PUT | yes | **dangerous / approval** | write report |
| `vmware.composite.vm.guest.program.run` | `GuestProcessManager.StartProgramInGuest` (+ `ListProcessesInGuest` poll when `wait=true`) | yes | **dangerous / approval** | pid (+ exit code / times) |

Notes:

- **`guest.net.show` needs no guest credentials.** Guest NIC + IP-stack
  state is Tools-reported `GuestInfo` on the VirtualMachine, read through
  the same `RetrievePropertiesEx` seam the other composites use — no
  in-guest authentication. It directly serves consumer case 1's
  "read `ip addr`/routes during a connectivity diagnosis" without
  running anything inside the guest.
- **File transfer is two-step by vim design.** `InitiateFileTransfer*`
  returns a one-time guest-transfer URL; the file bytes flow directly
  over that URL (GET for read, PUT for write), never through the vim
  channel. The read caps returned content and the write echoes only
  path + size to the reviewer.
- **Set-shaped responses are JSONFlux-wrapped (postulate 6).** The
  process list and env list return arrays; the dispatcher wraps any
  response over the size threshold into a result handle, so the agent
  drills in via `result_query` rather than receiving an unbounded guest
  process table.

### Why `guest.file.write` is the proving write (not `guest.net.set_mtu`)

The issue offered `guest.file.write` or `guest.net.set_mtu` as the one
gated write. `guest.file.write` is chosen because it is the write the
vim API supports **most cleanly through the seam**:

- `InitiateFileTransferToGuest` is a single documented `GuestFileManager`
  method (mirror of the `InitiateFileTransferFromGuest` the file-read op
  already uses — the transfer machinery is built once, used both ways).
  It is a structured sub-op, not a shell command.
- `guest.net.set_mtu` has **no** single vim guest-op method. Setting an
  MTU inside a guest means running `ip link set … mtu …` via
  `StartProgramInGuest` — freeform program execution, which #3100 deferred.
  With `guest.program.run` shipped (#3255), that MTU set is now expressible
  (`program.run` with `program_path=/usr/sbin/ip`, `arguments="link set …
  mtu …"`), but through the governed exec op, not a bespoke `set_mtu` verb.

So #3100 proved the approval posture with the clean, symmetric
`guest.file.write`, and #3255 lifted the freeform-exec tier as
`guest.program.run`.

## The exit-code poll (`guest.program.run`, #3255)

`StartProgramInGuest` is fire-and-forget: it returns the started process's
PID and nothing else — **no stdout is captured**. So:

- **`wait=false`** (default) returns immediately with `{status: "started",
  pid, …}`. The caller polls `guest.process.list` itself if it cares.
- **`wait=true`** polls `ListProcessesInGuest` (filtered to the PID) every
  `_RUN_POLL_INTERVAL_SECONDS` (2s) until the exit code is available or
  `timeout_seconds` (default 300) elapses, returning `{status, pid,
  exit_code, start_time, end_time, …}`.

The load-bearing vim quirk, verbatim from the pinned `vi-json.yaml`
`StartProgramInGuest` description: *"When the process completes, its exit
code and end time will be available for 5 minutes after completion. If
VMware Tools is restarted, the exit code and end time will not be
available."* So a completed process stays listable **with** its `exitCode`
for ~5 minutes; the poll reliably catches it within that window (hence the
300s default timeout matches the window). The poll handles the three
terminal outcomes explicitly, and never hangs:

- **`exited`** — the process reports an `exitCode` (an int, including `0`):
  return it plus `startTime` / `endTime`.
- **`timeout`** — `timeout_seconds` elapsed while the process is still
  running (listable, no `exitCode`): return `exit_code: null`.
- **`exit_unknown`** — the process is no longer listable before an exit
  code was seen: it finished and its exit info aged out of the 5-minute
  window, or VMware Tools restarted (or the PID was never observed). Return
  `exit_code: null` rather than looping forever. This is the case the issue
  called out — a finished process that is no longer listable is handled
  explicitly, not by hanging.

Capturing stdout is deliberately **not** built in: redirect the program's
output to a file in `arguments`, then read it back with `guest.file.read`
(the caller composes the two governed ops).

## Consumer evidence

Cases observed falling back to out-of-band `govc guest.run` (see the
issues for full context; lab-specific host/realm details are intentionally
omitted from this public repo):

1. **Guest NIC MTU + connectivity diagnosis (#3100).** Setting a deployed
   appliance's guest NIC MTU (a fabric requirement) and reading
   `ip addr` / routes while diagnosing connectivity. `guest.net.show`
   serves the read half (Tools-reported, no creds); the MTU *set* is now
   expressible via `guest.program.run` (#3255).
2. **First-boot service-state verification (#3100).** Verifying service
   state inside a first-boot appliance (`systemctl is-system-running`,
   listener checks) on a guest with no route back to MEHO.
   `guest.process.list` + `guest.file.read` cover state inspection
   structurally; a `systemctl` *invocation* is now `guest.program.run`.
3. **In-guest Windows-estate bring-up (#3255).** The c1sql1 initiative
   (`claude-rdc-hetzner-dc#2789`) brings up an AD DC + WSFC + SQL Server
   FCI on Windows guests, where **every** step above the OS boundary
   (AD DS promotion, cluster formation, clustered SQL install) rode
   ungoverned `govc guest.run` as the documented-gap fallback. The lab's
   standing signal `meho-signals/vmware-guest-ops-channel-missing.yaml`
   tracks exactly this hole, and the automation windows-estate provider
   pack (`meho-automation#169`) is gated on the governed
   `vm.guest.program.run`. `guest.program.run` closes the governance gap:
   policy, approval, synchronous audit, and broadcast around in-guest
   execution.

## Verification basis

No live guest is available in CI. Grounding is:

- the pinned `vcenter-9.0/vi-json.yaml` on the spec shelf (the guest-op
  method paths — including `StartProgramInGuest` — reconcile against it,
  same lane as the read composites);
- `respx`/stub unit tests with mocked `_post_vmomi_json` responses and a
  mocked guest-transfer endpoint, asserting the request bodies
  (`vm` MoRef + `NamePasswordAuthentication` + `GuestProgramSpec`), the
  JSONFlux wrapping, the approval park + gate-first short-circuit on the
  writes, the `guest.program.run` poll outcomes (`exited` / `timeout` /
  `exit_unknown`), and that neither the guest credential **nor** the
  `arguments` / `env` values ever appear in any result / audit / preview
  surface.

Live-appliance verification is deferred (tracked with the issues). The
c1sql1 consumer-proof step — one in-guest bring-up step (e.g.
`Install-WindowsFeature`) executed via the governed op instead of
`govc guest.run`, recorded on `vmware-guest-ops-channel-missing.yaml` —
is **open** pending a lab redeploy carrying #3255 (it closes that lab
signal when done).

## References

- Issues: evoila/meho#3100 (reads + `file.write`), evoila/meho#3255
  (`program.run` — the freeform-exec tier lifted).
- v0.1-spec §3 (operations), §4 (JSONFlux), §6 (audit), §7 (approval).
- Handlers: `composites/_guest.py` (`guest_program_run_composite` +
  `_poll_for_exit`); preview: `composites/_write_preview.py`
  (`_guest_program_run_preview`); schemas: `composites/schemas.py`
  (`GUEST_PROGRAM_RUN_*`).
- Substrate: `connectors/vmware_rest/connector.py` (`_post_vmomi_json`),
  `composites/_write.py` (`_write_vmomi_sub_op`, the #2254 gate),
  `composites/_read.py` (`_read_sub_op`), `vim_body.py`.
- Credential resolution: `connectors/_shared/vault_creds.py`
  (`load_basic_credentials`).
- Redaction precedent: `guest.file.write` excludes `content` the same way
  `guest.program.run` excludes `arguments` / `env`
  (`operations/_preview.py`, `broadcast/events.py` `scrub_broadcast_params`).
- Still-deferred tier (b): a `linux-ssh` typed connector for Tools-less
  guests — not built here.
