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

First increment (this doc): four **read-only** guest ops plus **one**
approval-gated write proving the governance posture.

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
   are JSON-schema-validated. (b)'s natural shape is a safelisted-but-
   still-textual command surface; the structured shape is a smaller,
   more auditable blast radius.

**Why (b) is deferred, not discarded.** Two capabilities (a) cannot
serve cleanly:

- **Running a command and capturing its output** (`ip addr`,
  `systemctl is-system-running`). vim's only exec primitive is
  `StartProgramInGuest`, which starts a detached process and returns a
  PID — output is not captured; you must redirect to a file, poll
  `ListProcessesInGuest` for the exit code, then read the file back.
  That freeform-program tier is deliberately **out of this increment**
  (it is the crux of the dangerous-by-default posture and wants its own
  design pass). Where the *reported* state suffices, this increment
  serves it structurally instead: `guest.net.show` reads Tools-reported
  guest network state directly, no program-exec.
- **Guests without VMware Tools.** No Tools → no guest operations at
  all; (b) is the only channel there.

If and when a concrete need for freeform in-guest command execution or a
Tools-less guest lands, tier (b) (a `linux-ssh` typed connector, or a
`guest.program.run` op with a stricter safety gate) is the follow-up.
This increment does **not** build it.

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
   request body carries. This is a deliberate improvement on the GOSC
   `credential-class` ops (`guest.customization_spec.create`), which
   accept the Windows admin password *in params* and rely on
   reviewer-surface suppression to hide it; here the secret is never in
   params to begin with.
2. **Structured sub-ops, not freeform shell.** Each op is a discrete
   typed verb with a JSON-schema parameter contract. There is no
   `run(cmd)`. Freeform in-guest program execution is the explicitly
   deferred tier.
3. **The write is approval-gated.** `guest.file.write` is registered
   `safety_level="dangerous"` + `requires_approval=True` and rides the
   standard approvals plane: the dispatcher parks an
   `ApprovalRequest` before the write executes, a human approves/rejects
   via console/CLI (never MCP — v0.1-spec §7), and the park stores a
   `proposed_effect` preview echoing **path + byte size + overwrite
   intent only** (never the file content). The reads are
   `safety_level="safe"` / no approval.
4. **Full audit of command + result + truncated output.** Every op
   dispatches through the synchronous append-only audit path
   (v0.1-spec §6). The audit row names the op, the VM, and the outcome;
   set-shaped and byte-shaped outputs are truncated (JSONFlux result
   handle for the process list; a byte cap on file reads) so the audit
   and the agent surface never carry an unbounded guest payload.
5. **Read/write split is explicit.** The four reads never mutate guest
   state; the single write is the only mutating op and is the only one
   that parks.

## The op family (first increment)

All five ops register under the existing `guest` operation group
(`group_key="guest"`), alongside the GOSC customization writes, and
carry `op_id`s under the `vmware.composite.vm.guest.*` namespace. Each
carries full `parameter_schema` + `response_schema` (JSON Schema
2020-12), `tags`, `safety_level`, and `llm_instructions`.

| op_id | vim method (VI-JSON) | Guest creds? | Safety | Shape |
|---|---|---|---|---|
| `vmware.composite.vm.guest.process.list` | `GuestProcessManager.ListProcessesInGuest` | yes | safe | set → JSONFlux handle |
| `vmware.composite.vm.guest.env.read` | `GuestProcessManager.ReadEnvironmentVariableInGuest` | yes | safe | set → JSONFlux handle |
| `vmware.composite.vm.guest.net.show` | `PropertyCollector.RetrievePropertiesEx` on `guest.net` + `guest.ipStack` | **no** (Tools-reported) | safe | aggregate dict |
| `vmware.composite.vm.guest.file.read` | `GuestFileManager.InitiateFileTransferFromGuest` + guest-transfer GET | yes | safe | truncated content + attrs |
| `vmware.composite.vm.guest.file.write` | `GuestFileManager.InitiateFileTransferToGuest` + guest-transfer PUT | yes | **dangerous / approval** | write report |

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
  `StartProgramInGuest` — freeform program execution, the very tier this
  increment defers, plus PID-polling and output-file gymnastics. It
  belongs to the deferred freeform-exec tier, not the clean-write proof.

So consumer case 1's *MTU set* is served by the deferred tier; this
increment proves the approval posture with the clean, symmetric
`guest.file.write`.

## Consumer evidence (issue #3100)

Two live cases in one program drove this, both observed falling back to
out-of-band `govc guest.run` (see issue #3100 for the full context;
lab-specific host/realm details are intentionally omitted from this
public repo):

1. **Guest NIC MTU + connectivity diagnosis.** Setting a deployed
   appliance's guest NIC MTU (a fabric requirement) and reading
   `ip addr` / routes while diagnosing connectivity. `guest.net.show`
   serves the read half now (Tools-reported, no creds); the MTU *set*
   is the deferred freeform-exec tier.
2. **First-boot service-state verification.** Verifying service state
   inside a first-boot appliance (`systemctl is-system-running`,
   listener checks) on a guest with no route back to MEHO.
   `guest.process.list` + `guest.file.read` cover the state-inspection
   half structurally; a `systemctl` *invocation* is again the deferred
   freeform-exec tier.

The point of the first increment is not to close both cases end-to-end —
it is to land the governed channel, the credential-via-`secret_ref`
model, the JSONFlux/audit discipline, and the approval-gated write, so
the deferred freeform-exec tier has a proven substrate to extend.

## Verification basis

No live guest is available in CI. Grounding is:

- the pinned `vcenter-9.0/vi-json.yaml` on the spec shelf (the guest-op
  method paths reconcile against it, same lane as the read composites);
- `respx`/stub unit tests with mocked `_post_vmomi_json` responses and a
  mocked guest-transfer endpoint, asserting the request bodies
  (`vm` MoRef + `NamePasswordAuthentication`), the JSONFlux wrapping, the
  approval park on the write, and that the guest credential never appears
  in any params/audit/preview surface.

Live-appliance verification is deferred (tracked with the issue).

## References

- Issue: evoila/meho#3100.
- v0.1-spec §3 (operations), §4 (JSONFlux), §6 (audit), §7 (approval).
- Substrate: `connectors/vmware_rest/connector.py` (`_post_vmomi_json`),
  `composites/_write.py` (`_write_vmomi_sub_op`, the #2254 gate),
  `composites/_read.py` (`_read_sub_op`), `vim_body.py`.
- Credential resolution: `connectors/_shared/vault_creds.py`
  (`load_basic_credentials`).
- Deferred tier (b): a `linux-ssh` typed connector / a
  `guest.program.run` freeform-exec op — not built here.
