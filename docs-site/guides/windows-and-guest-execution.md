# Govern the Windows estate and run programs in a guest

MEHO governs the Windows half of a datacenter — Windows Server, Active
Directory, DNS, failover clustering, SQL Server, and Hyper-V — through
the same **discover → list groups → search → preview → call** ladder you
learned in [Run your first operations](first-operations.md). It also
governs a harder thing: running a program *inside* a virtual machine's
guest OS, with a human approving the risky ones. This guide covers both.

These connectors are recent additions. The honest status is **shipped**:
each one is registered, has a live operation surface, and rides the full
policy, approval, and audit path. Read them as new capability, not as
long-hardened plumbing — where a connector defers something, this guide
says so plainly under [Honest limits](#honest-limits).

!!! note "Prerequisites"

    - A registered, probe-green target —
      [Register targets and secrets](targets-and-secrets.md). The Windows
      connectors add a small server-side prerequisite (an OpenSSH server
      on the host); see [Register a Windows target](#register-a-windows-target).
    - The ladder from [Run your first operations](first-operations.md).
      Every operation here is reached the same way; nothing below is a
      new tool.
    - An **operator**-role session for reads and recoverable writes.
      The destructive operations park for a human — approval is decided
      on the operator surfaces, never from an agent
      ([Approvals and break-glass](approvals-and-break-glass.md)).

## What each connector governs

Each is a typed connector: a curated, individually safety-tiered set of
operations, not a raw shell or an open SQL prompt. You address every one
by its `connector_id`, exactly as in the first-operations ladder.

| `connector_id` | Governs |
|---|---|
| `winsrv-ssh-2022.x` | Windows Server core |
| `msad-ssh-2022.x` | Active Directory |
| `windns-ssh-2016.x` | Windows AD-integrated DNS records |
| `wsfc-ssh-2022.x` | Windows Server Failover Clustering |
| `mssql-tds-2022.x` | Microsoft SQL Server |
| `hyperv-ssh-2022.x` | Hyper-V (migration-source reads) |

**Windows Server (`winsrv`).** System facts, service start/stop/restart,
Windows feature install and removal, reboot and shutdown, local users,
and disk / iSCSI-initiator storage. The reboot, shutdown, feature-removal,
and local-user-delete operations park for a human; everything else is a
read or a recoverable write.

**Active Directory (`msad`).** Domain, forest, and DC topology reads, AD
replication metadata, plus guarded day-2 writes over users, groups,
computers, and organizational units. The SSH target *is* the domain
controller — the AD cmdlets contact the local directory. Object deletes
(`user.delete`, `group.delete`, `computer.delete`) park; a computer
"unjoin" is a recoverable disable, not a delete.

**Windows DNS (`windns`).** Read the zones on an AD-integrated DNS server,
read records, add A and CNAME records, and remove a record set. The
record removal runs on the governed-delete tier: it previews the exact
set that would be removed, parks for a human, and is bound to that
preview before it executes.

**Failover Clustering (`wsfc`).** Cluster, node, clustered-role, resource,
and quorum state, plus cluster validation and guarded role moves. The
target is any single cluster node — the cluster service is cluster-wide,
so one node is a sufficient control point. A planned role *move* between
nodes is a recoverable rebalance (`caution`); taking a role offline,
forcing one online, or evicting a node parks for a human, on every role,
because MEHO tiers by operation, not by whether you call a role
"production".

**SQL Server (`mssql`).** Instance, database, availability-group / FCI,
and backup reads, plus governed backup, restore, and database create /
drop. It connects directly over TDS (port 1433), so it works against any
reachable instance. The availability-group and FCI reads are the
migration-validation surface: confirm every database is synchronized and
healthy before a cutover or planned failover.

**Hyper-V (`hyperv`).** The source-side reach for a migration onto VMware.
It reads a Hyper-V host's inventory, VM configuration (including the
firmware generation and secure-boot state a target needs), virtual-disk
format and differencing chains, and checkpoints — the inputs a migration
plan is built from — plus a guarded `Export-VM` that seeds the migration.
It manages the *source* estate so you can plan and stage the move; it does
not build new VMs on Hyper-V. Cluster-wide views of a Hyper-V cluster's
nodes are the `wsfc` connector's job on the same hosts, not duplicated
here.

## Register a Windows target

Registration follows [Register targets and secrets](targets-and-secrets.md)
unchanged — a `targets.yaml` entry with a `product` token, a green probe,
and a `secret_ref` into your credential store. The estate-specific parts
are the transport prerequisite and the credential shape.

### The PowerShell-over-SSH connectors

`winsrv`, `msad`, `windns`, `wsfc`, and `hyperv` all reach the host the
same way: over SSH, driving the built-in **Windows PowerShell 5.1**
cmdlets. The one server-side prerequisite is an OpenSSH server on the
Windows host, with PowerShell reachable as its shell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
```

The credential is an ordinary SSH secret in your store: a `username` with
either a `password` or an `ssh_private_key` field (key auth is preferred
when the key is present). Nothing else is needed — the connector reads the
one SSH secret and constructs each command locally.

```yaml
targets:
  - name: win-fs01
    product: winsrv                 # winsrv | msad | windns | wsfc | hyperv
    host: win-fs01.example.internal
    notes: File server, Windows Server 2022.
```

```bash
meho targets import targets.yaml
meho targets probe win-fs01
```

A green probe reports `vendor: microsoft` and the product it observed
(for example `windows-server`, `active-directory`, or `hyper-v`), which
is how the resolver later matches the target to its connector.

### SQL Server over TDS

`mssql` is different: it speaks the SQL Server wire protocol directly on
port 1433, so there is no SSH server to stand up. It also carries **two
credentials in one secret** — a SQL login stored as `sql_username` and
`sql_password` rather than the usual `username` / `password`:

```yaml
targets:
  - name: sql-node-a
    product: mssql
    host: sql-node-a.example.internal
    port: 1433
```

Stage `sql_username` and `sql_password` at the target's `secret_ref` the
same way [Register targets and secrets](targets-and-secrets.md#stage-the-secret)
describes for every other credential.

### In-guest execution: the VM's own target

Running a program inside a guest (below) does not need a new target. It
runs against the **VMware target** the VM lives on, through the
`vmware-rest-9.0` connector, and reads the guest-OS login from that
target's secret as `guest_username` / `guest_password`. The guest
credential is resolved at dispatch time and never travels in an operation
parameter.

## The transport: PowerShell 5.1 over SSH, secret-free

Every PowerShell-over-SSH operation is built the same disciplined way, and
the discipline is what makes it safe to hand to an agent:

- **The command is constructed locally and encoded, never assembled by a
  shell.** The script is UTF-16LE-encoded and sent as a single
  `-EncodedCommand`, so there is no shell-quoting layer to trick — only
  the PowerShell parser ever sees it.
- **Operator input rides inside single-quoted literals.** Every name,
  identity, or path an operator supplies is escaped and placed inside a
  single-quoted PowerShell string; numbers and toggles are validated to
  their type first. A value that tries to break out of the string cannot.
- **No credential material is ever in the script.** The encoded command
  is visible to a privileged observer on the remote host, so the transport
  forbids any secret in the script body. The SSH login is the only secret,
  and it stays in the transport, never in a command.

That last rule has one honest consequence: operations that would need a
secret *in* the command are deliberately absent. `winsrv` creates a local
user without a password (set it out of band afterwards); `msad` creates a
disabled, passwordless account you enable once a password is set out of
band. There is no password-reset operation on the estate.

Windows Server ships PowerShell 5.1, not PowerShell 7, so these connectors
target 5.1 explicitly — the cmdlet surface is stable across Windows Server
2016 through 2025.

!!! note "How preview behaves for these operations"

    `preview_operation` returns the literal request a spec-ingested
    operation would send. These connectors are typed, so their `safe`
    reads and `caution` writes have no single such request, and preview
    answers `status: "unavailable"` — the expected result, not a failure.

    The governed tiers *are* previewable. A `dangerous`-with-approval or
    `destructive` operation returns a synthetic preview carrying the same
    redacted proposed-effect the approval park shows the approver —
    resolved with nothing sent, no park, and no audit row — so you can
    read the would-be effect of, say, `msad.user.delete` or
    `guest.file.write` before you submit it. A destructive delete such as
    `windns.record.remove` is previewable too; the exact record set it
    would remove is enumerated when it parks for approval.

    One operation here stays `unavailable` on purpose: `guest.program.run`.
    Because its `arguments` and `env` can carry a secret, it is treated as
    a credential-class write and does not synthesize a preview — the
    approver sees its redacted summary (program path, argument size,
    env-var names) at approval time instead.

## Safety tiers of the write operations

Every operation carries a `safety_level`, set when the connector is
registered and enforced server-side. The Windows estate uses the full
ladder:

| Tier | Behavior | Estate examples |
|---|---|---|
| `safe` | Read-class; executes under default-allow. | `winsrv.service.list`, `msad.user.get`, `wsfc.cluster.get`, `mssql.databases.list`, `hyperv.vms.list` |
| `caution` | Recoverable write; subject to policy. | `winsrv.service.restart`, `msad.group.add-member`, `wsfc.groups.move`, `mssql.backup.database`, `hyperv.export.vm` |
| `dangerous` + approval | Disruptive or destructive; **parks for a human** before it runs. | `winsrv.power.reboot`, `winsrv.feature.remove`, `msad.user.delete`, `wsfc.nodes.evict`, `wsfc.groups.offline`, `mssql.databases.drop`, `mssql.backup.restore`, `hyperv.checkpoints.revert` |
| `destructive` + approval | Permanent delete; previews the exact blast radius, parks, and binds the approval to that preview. | `windns.record.remove` |

When you call a parked operation, it does **not** execute. The dispatcher
durably parks it and returns `awaiting_approval` with an
`approval_request_id`. A human resolves it on the operator surfaces:

```bash
meho approvals show <approval_request_id>
meho approvals approve <approval_request_id> --reason "verified blast radius"
meho approvals reject  <approval_request_id> --reason "wrong target"
```

The park stores a redaction-safe summary of what the operation would do —
the target and parameters for a `dangerous` operation, and for a
`destructive` one the exact record set that would be removed. Approval is
a human decision: there is no path for an agent to approve its own parked
call. The full mechanics — the four-eyes rule and the audited single-
operator break-glass — are the
[approvals and break-glass guide](approvals-and-break-glass.md).

## Run a program inside a guest

Sometimes the governed operation you need is "run this program inside that
VM" — promote a domain controller, form a cluster, verify a service on a
first-boot appliance that has no network route back to MEHO. MEHO governs
this through `vmware.composite.vm.guest.program.run` on the
`vmware-rest-9.0` connector, so it replaces the out-of-band
`govc guest.run` / SSH-into-the-guest habit with something audited.

It rides **VMware Tools guest operations** — vCenter to ESXi to the Tools
agent inside the guest — so it needs no new network path to the guest and
works even for a VM with no route back to MEHO. There is a read family
alongside it, all on the same connector:

| Operation | What it does | Tier |
|---|---|---|
| `vmware.composite.vm.guest.process.list` | List running guest processes | `safe` |
| `vmware.composite.vm.guest.file.read` | Read a guest file (content capped) | `safe` |
| `vmware.composite.vm.guest.env.read` | Read a guest environment variable | `safe` |
| `vmware.composite.vm.guest.net.show` | Guest NIC / IP state (Tools-reported, no guest login) | `safe` |
| `vmware.composite.vm.guest.file.write` | Write a guest file | `dangerous` + approval |
| `vmware.composite.vm.guest.program.run` | Run a program in the guest | `dangerous` + approval |

`program.run` runs against the VM's VMware target. It takes an explicit
`program_path`, optional `arguments` and `env`, and a `wait` flag; with
`wait: true` it polls for the exit code and start/end times instead of
returning the moment the process starts.

```bash
meho operation call vmware-rest-9.0 vmware.composite.vm.guest.program.run \
  --target lab-vcenter \
  --params '{"vm": "<vm-name-or-id>", "program_path": "C:\\Windows\\System32\\cmd.exe",
             "arguments": "/c ipconfig /all", "wait": true}'
```

### What makes it governed

- **It parks for a human.** The operation is `dangerous` +
  `requires_approval`. Calling it stores a durable approval request and
  returns `awaiting_approval`; a human approves or rejects on the console
  or CLI, never from an agent. The policy gate runs *first* — a parked or
  denied call resolves no guest credential and starts nothing.
- **The guest login never travels in a parameter.** It is read from the
  VM target's secret at dispatch time and lives only in the request to
  the Tools agent — never in a parameter, a result, an audit row, or a log.
- **The decision surfaces are kept clean.** The approval park preview
  shows the VM, the program path, the working directory, the `wait` flag,
  the *byte size* of `arguments`, and the *names* of `env` variables —
  never their values. The policy gate, the operation result, and the
  broadcast feed are the same: values are held back so a reviewer's
  decision is made on structure, not secrets.
- **The whole call is audited synchronously.** The operation does not
  return success until an append-only audit row commits, naming the
  operation, the VM, and the outcome. Raw parameters are stored as a hash,
  never in the clear.

!!! warning "Do not put a bare secret in `arguments` or `env`"

    Two surfaces must keep the *full* parameters to work: the durable
    approval request re-dispatches the exact call after a human approves,
    and — when a capture policy is on — the
    [flight recorder](flight-recorder.md) records the underlying vendor
    request. So a password typed straight into `arguments` or `env` is
    retained on those surfaces. Where a program needs a secret, pass the
    *name* of an environment variable and stage the value guest-side (a
    file the guest user reads, a templated env file), the same way the
    guest login itself is resolved from the secret store rather than a
    parameter.

Capturing a program's output is a two-step compose, by design:
`program.run` cannot capture stdout on its own, so redirect the output to
a file in `arguments`, then read it back with `guest.file.read`. Keeping
capture out of the single operation keeps its blast radius and audited
surface small.

## Honest limits

- **No freeform SQL.** `mssql` is a curated operation table, not a query
  prompt — there is deliberately no raw-T-SQL operation. A read-only,
  guarded-SELECT operation could be added later if a concrete need
  appears; it is not shipped.
- **No password provisioning over the estate transport.** Local and
  domain accounts are created passwordless (and, in AD, disabled); set the
  password out of band. There is no password-reset operation. This falls
  out of the "no secret in the command" rule and is intentional.
- **SQL encryption is default-posture today.** `mssql` connects to a
  standard instance cleanly; certificate-validated TLS for an instance
  that *forces* encryption is a named future extension, not yet built.
- **A few secret-bearing knobs are deferred** for the same reason a
  credential cannot ride the command: iSCSI CHAP on `winsrv` and the cloud
  witness on `wsfc` are out of scope for now.
- **Hyper-V is source-side and reads-first.** It reads an existing estate
  to plan and seed a migration onto VMware; it does not create or delete
  VMs on Hyper-V, and `Export-VM` is a guarded, long-running seed, not a
  routine verb.
- **In-guest execution needs VMware Tools.** A guest without Tools has no
  guest-operations channel at all; reaching one would be separate future
  work.
- **Live-hardware validation is still landing.** These connectors are
  built and shipped, but the end-to-end proof against real Windows and SQL
  hardware is tracked openly in the repository rather than claimed here.
  Treat them as new, governed capability — not as GA.

**Next:** [Approvals and break-glass](approvals-and-break-glass.md) — the
mechanics behind every parked write above.
