<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group

This file is the **MEHO Layer-2 starter template**. Copy it into
your consumer repo's root as `CLAUDE.md` (or merge it with an
existing CLAUDE.md). It tells any local Claude Code session that
opens your repo to prefer MEHO surfaces over per-machine fallbacks.

Source of truth lives at:
  https://github.com/evoila/meho/blob/main/docs/examples/consumer-onboarding/CLAUDE.md

When MEHO ships a new minor version, re-pull this file and merge
the diff against your tenant-specific customisations. The onboarding
guide next to it (`ONBOARDING.md`) walks the install + verify path.
-->

# CLAUDE.md — MEHO-first operations

This repo uses [MEHO](https://github.com/evoila/meho) for infrastructure
operations. When you (Claude Code or another local agent) operate here,
**prefer MEHO surfaces over local fallbacks** unless explicitly told
otherwise. MEHO writes an audit row for every operation, broadcasts a
live event for every operation, and enforces tenant + role policy on
every operation — none of those guarantees hold for local script
wrappers.

## Connection

* The MEHO instance for this tenant is at `$MEHO_INSTANCE` (operator:
  fill in the value, e.g. `https://meho.evba.lab`). Set the variable
  in your shell profile or in this file; do not hard-code it in
  command examples below.
* If you don't have a token cached, run `meho login $MEHO_INSTANCE`
  to obtain one (OAuth 2.0 device-code flow against the backplane's
  Keycloak realm).
* MEHO speaks MCP 2025-06-18 at `$MEHO_INSTANCE/mcp`. Wire your
  client per
  [`docs/cross-repo/mcp-client-setup.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/mcp-client-setup.md)
  in the upstream repo.

### Server-side conventions (Layer 1)

When the local agent connects via MCP (not via raw CLI), the MEHO
backplane will auto-load tenant-curated operating rules into the
session preamble. Those Layer-1 rules cover **tenant-wide**
operational discipline (Vault canonical, naming rules, secret
handling, CLI-wrapper fallback policy). This file (Layer 2) handles
only the **"prefer MEHO" routing rules**.

> **Current state (v0.2 staging).** The MCP `initialize` response's
> `instructions` field is the carrier for the assembled Layer-1
> preamble. Until [G7.1-T4 #316](https://github.com/evoila/meho/issues/316)
> lands, that field is `None` and Layer-1 rules are not yet reaching
> agents — only Layer 2 (this file) is live. Once T4 ships, sessions
> reconnecting via MCP will receive both layers automatically.

## Preferred MEHO surfaces

### Knowledge base

* Prefer `meho kb search <query>` over `grep kb/` for finding facts.
* Prefer `meho kb add <slug>` (with `--body @-` for stdin) over
  editing files in `kb/` directly.
* Other useful verbs: `meho kb show <slug>`, `meho kb list`,
  `meho kb delete <slug>`, `meho kb ingest <directory>` for bulk
  imports.
* The repo's `kb/` directory stays live as a fallback during the v0.2
  transition (~1 month per
  [decision #2](https://github.com/evoila/meho/blob/main/docs/planning/v0.2-decisions.md));
  after that, retire local reads.

### Memory (operator notes, behavioural preferences)

* Prefer `meho remember "..." --scope user` over writing to local
  memory files for behavioural preferences that follow the operator
  across machines.
* Prefer `meho remember "..." --scope user-tenant` (the default) for
  the operator's notes scoped to one tenant.
* Prefer `meho remember "..." --scope tenant` for team-shared
  knowledge (`tenant_admin` role required).
* Other useful verbs: `meho memory list`, `meho memory recall <scope>/<slug>`,
  `meho memory forget <scope>/<slug>`, `meho memory promote <scope>/<slug>`.
* Per-user laptop-local memory files (`~/.claude/.../memory/`) work
  in parallel; the first-time migration UX (when it lands) syncs the
  persistent ones.

### Targets (inventory lookup)

* Prefer `meho targets describe <name>` over reading `targets.yaml`
  directly. The backplane is authoritative for any future target
  edits.
* Prefer `meho targets list` for inventory queries; pass
  `--product vault` / `--product vsphere` / etc. to filter.
* Other useful verbs: `meho targets probe <name>`,
  `meho targets discover <product>` (when the connector exposes one).

### Connectors (operating against vSphere, Vault, NSX, bind9, etc.)

MEHO ships per-connector verbs that pre-bake the connector_id so you
don't type it on every dispatch. Prefer them over `./scripts/<wrapper>.sh`
local fallbacks:

* vSphere / vCenter — `meho vmware vm list`,
  `meho vmware vm info <name-or-id>`,
  `meho vmware host list`, `meho vmware cluster list`, etc.
* Vault — `meho vault kv read <mount> <path>`,
  `meho vault kv list <mount> <path>`,
  `meho vault kv put <mount> <path>`, `meho vault sys health`, etc.
* NSX — `meho nsx tier0 list`, `meho nsx tier1 list`,
  `meho nsx segment list`, `meho nsx firewall policy list`,
  `meho nsx transport-zone list`, etc.
* BIND9 — `meho bind9 zone list`, `meho bind9 zone read <zone>`,
  `meho bind9 config show <file>`, etc.
* Kubernetes — `meho k8s namespace list`, `meho k8s node list`,
  `meho k8s ls <path>`, `meho k8s logs <pod>`, etc.
* Harbor — `meho harbor repository list <project>`,
  `meho harbor artifact list <project> <repo>`, etc.
* Hetzner / pfSense / gcloud / SDDC-Manager / VCF (Operations / Logs
  / Fleet / Automation) — see `meho <connector> --help` for each.

For the **generic dispatch path** when no alias verb exists yet:
`meho operation search <connector_id> "<query>"` finds the op_id,
then `meho operation call <connector_id> <op_id> --target <slug>`
invokes it. Same auth, audit, policy as the alias verbs.

* The wrappers in `scripts/` stay live as fallback during the v0.2
  transition (per CLI-wrapper-fallback-discipline in the Layer-1
  conventions). Never delete a wrapper until its MEHO equivalent has
  been in daily use for ≥ 2 weeks.

### Audit (canonical history)

* Every MEHO op writes an audit row. Don't worry about ad-hoc
  logging — the audit log is canonical and queryable.
* `meho audit recent` — last 24 h, filterable by op-id-pattern.
* `meho audit query` — full filter (target, principal, op-id,
  op-class, result-status, time window).
* `meho audit show <audit-id>` — single-row detail.
* `meho audit who-touched <target>` — every operator who ran an op
  against a given target in the recent window.
* `meho audit my-recent` — your own activity.

### Live awareness (broadcast feed)

* Other operators may be watching the live broadcast feed via
  `meho status --watch` — they'll see your work in real time. The
  feed is per-tenant, served as Server-Sent Events at
  `GET $MEHO_INSTANCE/api/v1/feed`, with the CLI subscriber as the
  default consumer.
* `meho status --watch [--op-class <class>] [--principal <sub>] [--target <name>]`
  streams structured one-line events as they arrive; reconnect-with-replay
  via SSE `Last-Event-Id` is automatic. `--op-class` accepts one of
  `read | write | credential_read | audit_query` (no op-id-pattern filter
  on the watch surface today).
* The MCP resource `meho://tenant/<tenant_id>/feed` returns the
  most recent ~50 events as a snapshot for LLM clients that poll
  rather than maintain a live socket.
* The cross-repo broadcast contract + filter shapes live in
  [`docs/cross-repo/broadcast-onboarding.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/broadcast-onboarding.md).

#### Broadcast discipline (load-bearing — added 2026-05-14)

Per the [parent Initiative #229](https://github.com/evoila/meho/issues/229)
(updated 2026-05-14), every session, no matter how short, follows
this contract:

1. **Before starting work on a target:** check whether another
   operator/agent is touching the same target. If conflicting
   activity is in flight, surface the conflict to the operator
   before proceeding.
2. **Announce intent:** publish the planned activity (e.g.
   *"investigating cluster X latency"*, *"applying NSX policy
   change to tenant Y"*) scoped to the target. Sessions that go
   quiet for >10 minutes without an announce look like crashes.
3. **Check in every few minutes during long work:** keep the
   awareness fresh. Conflicts surface mid-flight, not after the
   damage.
4. **Report on completion:** publish the result summary + audit row
   id.

> **Tooling status (as of v0.2 staging).** The named meta-tools
> `broadcast_recent` / `broadcast_announce` / `broadcast_watch`
> referenced in the planning docs ship as part of the
> [#228 G6.1 SSE feed Initiative](https://github.com/evoila/meho/issues/228).
> Their MCP registration is **not yet wired** in the current
> codebase — `tools/list` does not include them today. Until they
> land, follow the same four-step discipline using the surfaces that
> exist now: `meho status --watch` for the read side (steps 1 and
> 3), `meho audit recent --since 30m --target <name>` for "who's
> been here recently" (step 1), and an explicit Slack/chat
> announcement for intent + completion (steps 2 and 4). The G6.1
> dispatcher already auto-emits a broadcast event before and after
> every `meho` operation, so step 3's "check in" is in part
> handled implicitly — the discipline here is the *higher-level
> intent* layer that auto-emits don't cover.

Per-op broadcast (automatic before+after events emitted by the G0.6
dispatcher for every `call_operation`) is *in addition to*, not a
replacement for, the higher-level intent announcements above.

## What stays local

* **This file (the `CLAUDE.md` you're reading)** — Layer 2 routing
  rules, repo-specific.
* **Repo-discipline rules**: PR cadence, ticket+PR workflow,
  `/work-ticket` flow — these apply only to repo work, not infra ops.
* **Markdown-sidecar conventions** (e.g. `gen-spec-sidecars.sh`
  outputs) — repo-internal.
* **Per-machine credentials** — Vault is canonical for shared
  secrets; the keyring / `~/.config/meho/credentials.json` holds the
  operator's MEHO token; the broadcast feed never carries
  credentials.

## When MEHO surfaces are unavailable

If `$MEHO_INSTANCE` is unreachable, you may fall back to local
scripts (the `scripts/*.sh` wrappers). Document the fallback in the
ticket so MEHO ops in flight are aware that one operator is
operating without audit/broadcast/policy enforcement until MEHO is
back.

## Versioning

This template tracks **MEHO v0.2.x**. Newer MEHO versions may add or
refine surfaces; after upgrading the CLI (`meho version` reports the
client version, `meho status` reports the backplane version), re-pull
this template from upstream and diff against any tenant-specific
customisations you've made below this line. The
[onboarding guide](https://github.com/evoila/meho/blob/main/docs/examples/consumer-onboarding/ONBOARDING.md)
walks the refresh procedure.

<!-- Add tenant-specific or repo-specific rules below this marker.
     Keep the canonical Layer-2 routing rules above untouched so
     diffs against upstream stay clean. -->
