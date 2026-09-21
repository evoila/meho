# What's new

A plain-language summary of what landed in recent MEHO releases. It
covers releases **since v0.28.0**; older releases are in the
[CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md),
which carries the full detail — every change, and the migration recipe
for each breaking one.

MEHO is under active development. Each release below links to its full
notes.

## [v0.35.11](https://github.com/evoila/meho/releases/tag/v0.35.11) — 2026-09-21

- **A vSphere Supervisor that logs in without handing back a kubeconfig can be
  reached again.** A Kubernetes (vSphere Supervisor) target that authenticates
  through WCP-SSO gets back a session token and, on newer Supervisors, no
  embedded kubeconfig at all — and MEHO used to reject that successful login as
  malformed, so the target could never be probed. MEHO now treats the embedded
  kubeconfig as optional (just as `kubectl vsphere login` does): when it is
  absent it builds the connection from what it already knows — the login
  host/port, the target's pinned CA and certificate name, and the returned
  session token as the bearer — and the target's own TLS policy always wins over
  anything an embedded config tried to set.

## [v0.35.10](https://github.com/evoila/meho/releases/tag/v0.35.10) — 2026-09-21

- **The operator console can't be stranded on a stale stylesheet after a
  deploy.** The console references its CSS, scripts and fonts at stable URLs
  whose contents change every release, and the server sent them without a
  `Cache-Control` header — so after a roll a returning browser could keep
  serving the previous release's cached stylesheet and render the console
  unstyled until someone hard-refreshed. MEHO now serves every console asset
  `Cache-Control: no-cache`, which forces the browser to revalidate before
  reuse (a cheap `304` when nothing changed), and a build-time gate refuses to
  ship a stylesheet that is suspiciously small or missing the console's own
  classes.
- **Re-running a storage-policy create now finishes instead of dead-ending.**
  Creating a vSphere storage policy through the governed path builds a tag
  category, a tag and then the policy; if an earlier attempt failed partway it
  left the category behind, and every retry died immediately with
  `ALREADY_EXISTS`. The composite now adopts an existing category, tag or
  matching policy instead of blindly re-creating it, so a retry converges; a
  genuinely conflicting object (or a policy create that returns no id) is
  reported as a clear, retryable error rather than a false success.
- **Supervisors reached through a NAT alias can be probed again.** A Kubernetes
  (vSphere Supervisor) target that logs in via WCP-SSO verified the server's
  certificate against the address MEHO dialed, ignoring the target's configured
  certificate name — so a Supervisor fronted by a NAT alias whose address isn't
  in the certificate failed verification and couldn't be reached at all. The
  login and the Kubernetes API legs now both honour the target's
  `tls_server_name`, and a verification failure explains itself instead of
  surfacing as a bare connection error.

## [v0.35.9](https://github.com/evoila/meho/releases/tag/v0.35.9) — 2026-09-21

- **Storage-policy writes to vCenter now authenticate.** Creating or deleting a
  vSphere storage policy through the governed path reaches vCenter's Storage
  Policy (PBM) endpoint, which authenticates by a SOAP session-cookie header the
  connector was never sending — so the first live policy write faulted
  `NotAuthenticated` even though every other call on the very same credential
  succeeded. MEHO now carries the vim session cookie as the `vcSessionCookie`
  header on every PBM request (and self-heals an expired session with a single
  re-login), so `vmware.composite.storage_policy.create` / `delete` authenticate
  like the rest of the connector.
- **A composite operation that fails no longer reports success.** Some composite
  operations signal a load-bearing failure by *returning* a structured error
  envelope rather than raising, and the dispatch/audit boundary treated any
  returned value as success — so a mutation that actually failed (for example a
  content-library create that got a `500` back from vCenter) was recorded
  `ok` / 200 in the audit log and in a parked-then-approved run's result. MEHO
  now maps a composite's terminal-error envelope (one flagged with an
  `error`-severity issue) to a first-class dispatch error with an `error` audit
  row, while success-with-warnings envelopes stay `ok`.

## [v0.35.8](https://github.com/evoila/meho/releases/tag/v0.35.8) — 2026-09-20

- **Bootstrap a tenant's Keycloak role groups through the governed path.** The
  backplane derives a tenant's claims (`tenant_id` / `tenant_role`) from Keycloak
  group membership, but the Keycloak connector shipped no group operations — so
  standing up a tenant's role group and adding its users was the one part of an
  otherwise-governed identity bootstrap that had to escape to the Admin API,
  outside policy, audit and approval. MEHO now ships governed `keycloak.group.*`
  operations: list groups and members (safe reads), and create a group with
  attributes, update its attributes, and add or remove members (approval-gated
  writes) — also as `meho keycloak group ...` CLI verbs.
- **Agent permission grants now actually apply to agent tokens.** A `deny`,
  `auto-execute` or `needs-approval` grant created with `agent grant create` was
  keyed on the agent's client identity but enforced only against the token's
  service-account id, so no agent grant ever took effect. The resolver now also
  matches the token's `agent:<name>` client identity (its `azp` claim), so — for
  example — scoping an agent off a whole connector family with a `deny` grant
  finally works; the new `--principal-kind user-agent` extends the same to a
  human who signs in as an agent through a public client. Grant ceilings are
  unchanged (`deny` still beats everything; destructive stays non-grantable for
  agents).

## [v0.35.7](https://github.com/evoila/meho/releases/tag/v0.35.7) — 2026-09-19

- **Change the VLAN of a distributed portgroup that already exists.** MEHO could
  set a portgroup's VLAN only when it was created; the new
  `vmware.composite.network.portgroup.vlan.set` reconfigures the VLAN of an
  existing distributed portgroup — for example to add the native/untagged VLAN 0
  to a trunk that was created without it — on the governed, approval-gated write
  path, replacing the current VLAN config and returning `unchanged` (no write)
  when the VLAN already matches.
- **A grown NFS datastore's free space becomes visible without SSH.** ESXi caches
  a datastore's capacity and free space from mount time, so after an NFS export
  grows a deploy precheck could still fail on stale "not enough free space"
  numbers. The new `vmware.composite.datastore.refresh` asks the host to re-probe
  the volume and reads the fresh summary back — a first-class governed operation
  in place of dropping to SSH or govc.

## [v0.35.6](https://github.com/evoila/meho/releases/tag/v0.35.6) — 2026-09-18

- **Re-mounting an NFS datastore that is already there no longer fails.**
  `vmware.composite.host.datastore_mount_nfs` is now idempotent: re-running the
  mount on a host where the export is already mounted converges instead of
  faulting. Previously a second call with the same parameters surfaced the
  host's already-exists fault as a connector error, so an automation pack that
  re-ran the step — a retry after a transient failure on a later host, or an
  assisted-resume recheck — could never converge once the datastore was
  mounted. The composite now reads the host's mounted datastores before writing
  and matches on the export's identity (server and path, normalised for case and
  a trailing slash): an export that is already mounted returns `already_mounted`
  with no write, the requested name held by a different export returns a
  `name_conflict` refusal with no write, and an export mounted concurrently
  between the check and the write is recovered by re-reading. A fresh mount and
  every other fault are unchanged.

## [v0.35.5](https://github.com/evoila/meho/releases/tag/v0.35.5) — 2026-09-17

- **A standalone ESXi host that went quiet is recovered automatically.** After
  an idle period a standalone ESXi host could leave MEHO holding an expired
  session that still answered the unauthenticated bootstrap but returned every
  requested property as "missing" — so a read came back empty with no error and
  never recovered for the life of the Pod. MEHO now refreshes a host session
  before it can idle out (a bounded max-age, 20 minutes by default, tunable with
  `VMWARE_SOAP_SESSION_MAX_AGE_SECONDS`), re-reads once on a fresh session when a
  read comes back entirely missing under an auth fault, and confirms host
  reachability with an authenticated liveness check — so an unreachable host is
  reported as unreachable instead of silently empty.

## [v0.35.4](https://github.com/evoila/meho/releases/tag/v0.35.4) — 2026-09-17

- **Per-property detail on vSphere object reads.** `vmware.object.collect` now
  reports, per object, which requested properties came back missing and the
  vSphere fault behind each (for example `NoPermission`) — names and counts
  only, never the fault message or the payload — so a partial read is
  diagnosable instead of opaque.
- **Service-principal grants get CLI commands and a console.** Operators and
  tenant admins can list, create, and manage service-principal grants from the
  CLI and a unified grants console.
- **Stored large results are bounded by encoded size.** A stored result handle
  now caps its whole encoded record at a configured limit (16 MiB by default),
  keeps the longest run of whole rows that fits, and tells the caller whether the
  stored copy is complete or partial.

## [v0.35.3](https://github.com/evoila/meho/releases/tag/v0.35.3) — 2026-09-17

- **A guest-composite read never keeps an in-guest password in the trace.**
  Every vSphere guest composite that carries a login — listing guest processes,
  reading the guest environment or a guest file — is now classified so the
  flight recorder does not record its request body, closing a path where a
  safe-tier read could retain the guest login password in the 30-day trace
  store. Forward-only: traces captured before this release keep their bodies
  until they age out.
- **Reading a guest file can fetch its contents.**
  `vmware.composite.vm.guest.file.read` can now return the file itself, fetched
  server-side with a size cap and returned behind a result handle; the operation
  moves to the caution tier, so agent and service callers park for approval while
  human operators run it as before.

## [v0.35.2](https://github.com/evoila/meho/releases/tag/v0.35.2) — 2026-09-16

- **A parked secret value stays off approval screens.** When an operation that
  carries a credential parks for approval, its secret input is encrypted in a
  one-time, tenant-bound hand-off, so the value never lands on the approval row
  or any approval read surface. Operators configure the dedicated
  `approvalHandoff` Secret reference in Helm values; approval rows written
  before the upgrade are not backfilled.
- **Deploy request bodies redact OVF property values.** Captured vendor deploy
  bodies could retain OVF property values, which commonly carry appliance
  credentials; the flight recorder now strips them structurally, on request and
  response, for every connector. Forward-only.
- **Governed vSphere Supervisor namespaces.** Create (approval-gated), delete
  (approval-gated, with a blast-radius preview and a read-back), and status
  composites for vSphere Supervisor namespaces, each on the governed dispatch
  path.

## [v0.35.1](https://github.com/evoila/meho/releases/tag/v0.35.1) — 2026-09-15

- **Changing a built-in connector needs platform-admin.** Enabling, disabling,
  or editing a built-in (global) connector now requires the `platform_admin`
  claim and answers a plain tenant admin with a clear `403` instead of the
  misleading `404` an earlier path returned. Tenant-scoped connectors are
  unaffected.
- **The operator console renders correctly again.** A v0.35.0 build change left
  the console without its compiled styles; the build now fails loudly if the
  stylesheet comes out empty, and the scheduler's create-trigger form and
  timestamps are back on the current layout.

## [v0.35.0](https://github.com/evoila/meho/releases/tag/v0.35.0) — 2026-09-14

- **vSphere 8.0 and 9.0 side by side.** A `vmware-rest-8.0` catalog is registered
  next to `vmware-rest-9.0`, and MEHO picks the right one per target from its
  fingerprint — an 8.0.x vCenter resolves to the 8.0 catalog, a 9.x vCenter to
  the 9.0 one — so one estate spanning both is served correctly. An enabled 9.0
  operation dispatched against an 8.0.x or otherwise unqualified target now fails
  closed with a clear reason instead of a vendor error.
- **VMware VM writes ingest as approval-gated by default.** Ingested VMware VM
  hardware, create/delete, and power operations now carry the `dangerous` tier
  and require approval out of the box; a re-ingest never weakens a stricter
  reviewed posture.
- **Pin a host key through the backplane.** `net.ssh_keyscan` reads a host's SSH
  host keys (handshake only, no credentials offered) and returns ready-to-pin
  `known_hosts` lines, so the pin the SSH connector's fail-closed verification
  needs is minted through a governed operation instead of an out-of-band shell
  step.
- **CLI parity for broadcast, and external token issuers.** `meho broadcast
  recent` / `announce` / `watch` reach the activity feed from the CLI, and the
  declarative OAuth token-mint auth scheme can now mint a client-credentials
  token at an external issuer.

## [v0.34.3](https://github.com/evoila/meho/releases/tag/v0.34.3) — 2026-09-12

- **Pass a secret into a remote script without leaving it on the host.**
  `linux.script.run` gains an optional `secret_env` map: the referenced secret is
  resolved server-side under the caller's tenant scope and injected into the
  script's environment at run time — never written to the approval request, the
  audit parameters, the preview, or a log line. This retires the earlier
  workaround that parked a live token on the target.

## [v0.34.2](https://github.com/evoila/meho/releases/tag/v0.34.2) — 2026-09-10

- **A pfSense gateway delete recovers a transient non-persist.** A pfSense
  runtime race could leave a gateway delete uncommitted even though the command
  exited cleanly, so a gateway that would otherwise delete was left in place and
  the teardown hard-failed; the delete now re-applies once and re-verifies before
  failing closed.

## [v0.34.1](https://github.com/evoila/meho/releases/tag/v0.34.1) — 2026-09-09

A patch release that fixes a v0.34.0 regression and rolls up the governed
connector and shared-instance work that landed since.

- **Compressed vendor responses decode again.** On v0.34.0, any read against a
  vendor that gzip- or deflate-compressed its response failed with a decoding
  error, because the response body-size cap added in v0.34.0 decompressed the
  body a second time. Every HTTP-based connector — vmware-rest, vcf-fleet,
  gcloud, and the rest — was affected. This release restores the earlier
  behaviour so a compressed response decodes exactly once
  ([#3521](https://github.com/evoila/meho/issues/3521)).
- **Kubernetes Secret and ArgoCD credential values are redacted on read.** A
  safe read that surfaces a Kubernetes `Secret` or an ArgoCD repository object
  now blanks the secret values — keeping the key names and a digest — so payload
  secrets no longer reach an agent on a shared instance.
- **More governed VMware and SDDC lifecycle.** Resource-pool create/delete and a
  DRS VM-Host affinity rule, an NFS tag-based storage policy, vSphere Supervisor
  enable/disable/status, and SDDC Manager workload-domain build ops — each
  approval-gated and audited on the governed dispatch path — plus a governed read
  that stages a guest-cluster kubeconfig to a Vault reference instead of an
  agent transcript.
- **Shared-instance isolation controls.** Per-principal and per-tenant dispatch
  rate limits, a per-tenant mail-recipient allowlist, an opt-in bearer guard on
  `/metrics` and `/ready`, and a per-tenant bound on targetless network probes.

## [v0.33.3](https://github.com/evoila/meho/releases/tag/v0.33.3) — 2026-09-05

- **Probe a target's certificate, then pin it — in one step.** `net.tls_inspect`
  now returns each presented certificate as PEM (on the leaf and every chain
  entry), not just facts about it. You can probe an endpoint and feed the right
  trust anchor straight into a target's `tls_ca_pin` — the leaf for a
  self-signed appliance, or an issuer/root for a CA-signed one — which is what
  you need to register a target reachable only through a NAT alias, where
  ordinary hostname verification can never match. The PEM is public handshake
  material, never a private key.
- **The assistant no longer points at result-handle features that don't exist.**
  The guidance the agent reads, the CLI help, and the onboarding guides had
  described a result-handle query surface — aggregation, filtering, describe —
  that was never built. That text now matches what actually ships: paging a
  result handle by offset and limit, and nothing it cannot do.

## [v0.33.2](https://github.com/evoila/meho/releases/tag/v0.33.2) — 2026-09-05

- **Standalone ESXi host operations now actually reach the host.** The
  host-setup reads and composites for a not-yet-managed ESXi host — list its
  storage devices, mount an NFS datastore, mark a disk as flash — had been
  failing at sign-in, because the connector spoke only the API surface a full
  vCenter serves. It now speaks the protocol a standalone ESXi host actually
  offers, so those operations work directly against a freshly provisioned host
  early in a management-domain bring-up — the case v0.33.0 set out to support.
- **Distributed-switch portgroup VLANs are no longer silently dropped.**
  Creating a portgroup with a VLAN tag or a trunk range now applies that VLAN
  instead of quietly producing an untagged (VLAN 0) portgroup. The create had
  been reporting success while the tag was discarded.

## [v0.33.1](https://github.com/evoila/meho/releases/tag/v0.33.1) — 2026-09-04

- **Standing grants for query-string actions.** A service principal can now
  hold a standing grant for an operation whose id carries a literal `?action=`
  verb — such as a vCenter power action or an OVF deploy. Before, these were
  wrongly refused as wildcards, so an agent could never be granted them and the
  power-on and deploy-from-library flows always had to park for a human.
- **The satellite gateway guide now covers remote writes.** The guide for
  running checks and operations through a remote runner used to say a satellite
  had no way to write; it now documents the opt-in tier that lets that runner
  perform governed, approval-gated, audited writes inside networks the central
  instance cannot reach.

## [v0.33.0](https://github.com/evoila/meho/releases/tag/v0.33.0) — 2026-09-04

- **A much bigger documentation site.** New do-real-work guides for the flight
  recorder, external event ingestion, and add-ons; a guide to the Windows
  estate and governed in-guest program execution; a client-onramp section for
  Claude Code and Claude Desktop; an explainer of the MCP surface tiers — the
  default working surface versus the operator planes behind the `mcp:admin`
  scope; and this What's new page. An accuracy sweep also brought the existing
  guides back in line with what the last release actually ships.
- **Generated, always-current references.** The connector catalogue, the full
  MCP tool inventory, and every CLI command are published as reference pages
  generated straight from the code, with a CI check that fails the build if
  they drift out of date.
- **Standalone ESXi hosts, before any vCenter exists.** The host-setup
  composites — mount an NFS datastore, mark a disk as flash, control a service
  — now run directly against a freshly provisioned ESXi host that no vCenter
  manages yet, the exact state you are in early in a management-domain
  bring-up. A new read lists a host's raw storage devices so the disk it
  flash-marks is chosen from real data.
- **More governed deletes for pfSense.** Removing a static route, a gateway,
  or a single member of a shared firewall alias each became a governed,
  approval-gated delete that previews exactly what goes and refuses to strand a
  route behind a gateway or empty a shared alias.
- **Preview parity for approval-gated actions.** An agent can now preview the
  exact effect of an approval-requiring action before it parks for a human,
  matching what the approver sees — while actions that carry a secret in their
  request stay deliberately un-previewable.
- **Steadier CLI sign-in.** A stale system-keyring entry can no longer shadow
  a newer saved token, and expiry messages now say plainly when a session has
  ended and point at how to sign back in.
- **Operator console polish.** The Conventions create and edit dialogs render
  correctly again after a UI-framework upgrade.

## [v0.32.0](https://github.com/evoila/meho/releases/tag/v0.32.0) — 2026-09-02

- **The Microsoft estate gains connectors.** New connectors for Windows
  Server, Active Directory, SQL Server, failover clustering, and
  Hyper-V — the Windows and SQL half of the datacenter, behind the same
  policy and audit as every other connector.
- **Governed permanent deletes.** A destructive-delete tier previews
  exactly what would be removed, binds the human approval to that exact
  preview, and refuses to run until a person approves — so a delete of a
  VM, a DNS record, a firewall rule, or a secret always has a person who
  owns it.
- **Flight recorder.** A complete, redaction-safe record of each
  dispatch, captured at the vendor-API level with secrets stripped, and
  readable by operators and by the agent's own session.
- **Governed in-guest program execution.** Run a program inside a VM's
  guest operating system as a governed, approval-gated step that keeps
  secrets out of the preview.
- **Governed writes on satellite runners.** A remote runner can execute
  policy-checked, audited writes inside networks the central instance
  cannot reach — limited to an operator-set allowlist, and never for the
  destructive or dangerous tiers.
- **Read result handles from the CLI and REST.** `result_query` reached
  full parity: a large, set-shaped result can be paged back from the CLI
  and REST, not only from an MCP client.
- **A dedicated approve-only role.** A principal who can clear the
  approval queue without any power to run an operation — separation of
  duties for regulated teams.

## [v0.31.0](https://github.com/evoila/meho/releases/tag/v0.31.0) — 2026-08-27

- **One-command onboarding.** A Claude Code plugin (`claude plugin
  marketplace add evoila/meho`, then `/plugin install meho@meho`) and a
  one-click `.mcpb` bundle for Claude Desktop point a client at your own
  backplane in a single step. Nothing is exposed to the internet.
- **Least privilege by default.** A connecting agent now sees only the
  25-tool working surface; the operator planes list only for a session
  that explicitly requests the `mcp:admin` scope. See
  [MCP surface and scopes](../clients/mcp-surface-and-scopes.md).
- **The AI cannot approve itself.** Approving an action, rejecting it,
  and granting an agent more access have no path an MCP client can call —
  those decisions move to the operator console or the CLI.
- **Two breaking changes for MCP clients.** The default surface dropped
  to 25 tools, and the approval and grant-elevate verbs left the MCP
  surface. Each carries a migration recipe in the release notes.

## [v0.30.0](https://github.com/evoila/meho/releases/tag/v0.30.0) — 2026-08-25

- **VMware Cloud Foundation bring-up through the backplane.** Submit and
  poll a management-domain bring-up as governed, approval-gated steps.
- **Air-gapped installs.** Govern the depot configuration, TLS-trust,
  and bundle pre-download steps an air-gapped bring-up depends on.
- **Recover from a failed bring-up.** Resume a failed management-domain
  bring-up from where it stopped, under the same approval posture as the
  original run.
- **Migration-source connectors for older VMware and Aria versions.** So
  one estate spanning old and new resolves to the right implementation
  per system — useful for reading and inventorying an existing estate
  during onboarding.

## [v0.29.0](https://github.com/evoila/meho/releases/tag/v0.29.0) — 2026-08-19

- **External event ingestion.** An authenticated webhook from your
  monitoring stack can fire a subscribed agent run on match, through the
  same policy, approval, and audit path as any other operation.
- **Checks say which items breached.** A breaching aggregate assertion
  now carries a sample of the offending rows in its evidence, so a
  notification names the specific items, not just that the aggregate
  tripped.
- **PostgreSQL 12 dropped from the supported range.** The end-of-life
  release is no longer supported.
- **More curated read operations across connectors.** Kubernetes,
  Harbor, and database connectors gained typed read operations that
  dispatch on a fresh boot with no ingest step.
