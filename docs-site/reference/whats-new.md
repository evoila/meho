# What's new

A plain-language summary of what landed in recent MEHO releases. It
covers releases **since v0.28.0**; older releases are in the
[CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md),
which carries the full detail — every change, and the migration recipe
for each breaking one.

MEHO is under active development. Each release below links to its full
notes.

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
