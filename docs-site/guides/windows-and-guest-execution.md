# Govern the Windows estate and run programs in a guest

MEHO governs the Windows half of a datacenter — Windows Server, Active
Directory, DNS, failover clustering, SQL Server, and Hyper-V — through
the same **discover → list groups → search → preview → call** ladder you
learned in [Run your first operations](first-operations.md). With
evoila's enterprise pack it also governs a harder thing: running a
program *inside* a virtual machine's guest OS, with a human approving the
risky ones. This guide covers the Windows connectors in full and the
in-guest path in short.

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
disk / iSCSI-initiator storage, and the server's NTP time source. The
reboot, shutdown, feature-removal, local-user-delete, time-source and
iSCSI-connect (`winsrv.storage.iscsi.connect`) operations wait for a
person to approve them; everything else is a read or
a recoverable write.

**Active Directory (`msad`).** Domain, forest, and DC topology reads, AD
replication metadata, plus guarded day-2 writes over users, groups,
computers, and organizational units. The SSH target *is* the domain
controller — the AD cmdlets contact the local directory. Object deletes
(`user.delete`, `group.delete`, `computer.delete`) park; a computer
"unjoin" is a recoverable disable, not a delete. For building a new domain
there are a read of the password rules and the join limit, and changes that
wait for a person to approve them: create a group, set the password rules,
set the join limit, and let a group create computers in an OU (and undo it).
For passwords there is a read of when an account's password was last set
and when it expires (`msad.user.password-expiry`), and
`msad.user.set-password`, which sets a user's password from the secret
store and enables the account. It waits for a person to approve it.

**Windows DNS (`windns`).** Read the zones on an AD-integrated DNS server,
read records, add A and CNAME records, and remove a record set. The
record removal is a permanent delete. You preview it first to get a
preview hash. The preview does not list the records. MEHO reads them
when you send the call, and the approver sees them on the approval
request before it runs. You can also read a zone's settings and the
server's forwarders, and change them. Those changes wait for a person to
approve them.

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
plan is built from. It can also create a checkpoint, start or stop a VM,
and run `Export-VM` to seed the migration (all `caution`). Reverting or
deleting a checkpoint is `dangerous` and waits for a person to approve
it.
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
when the key is present). The secret must also pin the host key: add a
`known_hosts` field with OpenSSH `known_hosts` lines for the host. You can
set `known_hosts_insecure: true` instead, but MEHO then logs a warning on
every connect. A secret with neither field fails before MEHO connects.
The connector reads the one SSH secret and constructs each command
locally.

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

A green probe reports `vendor: microsoft`, the product it observed (for
example `windows-server`, `active-directory`, or `hyper-v`) and the
version. The resolver picks the connector from the target's `product`
token (`winsrv`, `msad`, …) and the probed version.

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
runs against the **vCenter target** the VM lives on, through the vSphere
connector of the enterprise pack, and reads the guest-OS login from that
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
  forbids any secret in the script body. The SSH login stays in the
  transport. A value an operation reads from the secret store
  (`msad.user.set-password`) goes to PowerShell on standard input, never
  into the script.

That last rule has one honest consequence: `winsrv` creates a local user
without a password (set it out of band afterwards). `msad` creates a
disabled, passwordless account. Set its password with
`msad.user.set-password`: you pass `password_ref`, a reference into the
secret store, never the password itself.

Windows Server ships PowerShell 5.1, not PowerShell 7, so these connectors
target 5.1 explicitly — the cmdlet surface is stable across Windows Server
2016 through 2025.

!!! note "How preview behaves for these operations"

    `preview_operation` returns the literal request a spec-ingested
    operation would send. These connectors are typed, so an operation
    that runs without approval has no single such request, and preview
    answers `status: "unavailable"` — the expected result, not a failure.

    The governed tiers *are* previewable. Every operation that waits for
    approval (also a `caution` one such as `winsrv.time.set`) and every
    `destructive` operation returns a synthetic preview carrying the same
    redacted proposed-effect the approval park shows the approver —
    resolved with nothing sent, no park, and no audit row — so you can
    read the would-be effect of, say, `msad.user.delete` or
    `guest.file.write` before you submit it. A destructive delete such as
    `windns.record.remove` is previewable too; the exact record set it
    would remove is enumerated when it parks for approval.

    A credential operation such as `msad.user.set-password` stays
    `unavailable`. So does `guest.program.run`.
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
| `caution` | Recoverable write. Runs at once in the web interface; waits for approval when it comes with a token or from an agent. | `winsrv.service.restart`, `msad.group.add-member`, `wsfc.groups.move`, `mssql.backup.database`, `hyperv.export.vm` |
| `dangerous` + approval | Disruptive or destructive; **parks for a human** before it runs. | `winsrv.power.reboot`, `winsrv.feature.remove`, `msad.user.delete`, `wsfc.nodes.evict`, `wsfc.groups.offline`, `mssql.databases.drop`, `mssql.backup.restore`, `hyperv.checkpoints.revert` |
| `destructive` + approval | Permanent delete. You must preview it first and pass the preview hash to the call. The call then waits for approval, and the approval request shows what will be deleted. | `windns.record.remove` |

The preview of `windns.record.remove` does not show the records it will
delete. The preview never contacts the DNS server, and this list needs a
read of the server. The preview says so: it returns `plan_shown: false`
and a `plan_note`. MEHO reads the records when you send the call, and
the approver sees them on the approval request.

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
call. The full mechanics — what waits, who may approve, and how a single
operator approves their own change — are the
[approvals and break-glass guide](approvals-and-break-glass.md).

## Run a program inside a guest

Sometimes the governed operation you need is "run this program inside that
VM" — promote a domain controller, form a cluster, verify a service on a
first-boot appliance that has no network route back to MEHO. The vSphere
connector of evoila's enterprise pack governs this with a guest-operations
family on the VM's vCenter target: list guest processes, read a guest file
or environment variable, show the guest network state, write a guest file,
and run a program. It rides VMware Tools guest operations, so it needs no
new network path to the guest.

The write operations park for a human like every other `dangerous`
operation in this guide. The guest login is read from the VM target's
secret at dispatch time and never travels in a parameter, a result, an
audit row, or a log. The approval preview shows the program path and the
*names* of environment variables, never their values.

!!! warning "Do not put a bare secret in `arguments` or `env`"

    Two surfaces must keep the *full* parameters to work: the durable
    approval request re-dispatches the exact call after a human approves,
    and — when a capture policy is on — the
    [flight recorder](flight-recorder.md) records the underlying vendor
    request. So a password typed straight into `arguments` or `env` is
    retained on those surfaces. Where a program needs a secret, pass the
    *name* of an environment variable and stage the value guest-side.

The operation ids, parameters and worked example ship with the enterprise
pack's documentation. The public image answers an in-guest call with
"unknown connector".

## Honest limits

- **No freeform SQL.** `mssql` is a curated operation table, not a query
  prompt — there is deliberately no raw-T-SQL operation. A read-only,
  guarded-SELECT operation could be added later if a concrete need
  appears; it is not shipped.
- **Passwords come from the secret store, never the command.** Local and
  domain accounts are created passwordless (and, in AD, disabled). Set an
  AD user's password with `msad.user.set-password` (from the secret
  store, after approval); set a local user's password out of band. This
  follows from the "no secret in the command" rule.
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
- **In-guest execution needs the enterprise pack and VMware Tools.** The
  public image has no vSphere connector. A guest without Tools has no
  guest-operations channel at all; reaching one would be separate future
  work.
- **Live-hardware validation is still landing.** These connectors are
  built and shipped, but the end-to-end proof against real Windows and SQL
  hardware is tracked openly in the repository rather than claimed here.
  Treat them as new, governed capability — not as GA.

**Next:** [Approvals and break-glass](approvals-and-break-glass.md) — the
mechanics behind every parked write above.
