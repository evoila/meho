<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Consumer-onboarding guide — installing the MEHO Layer-2 starter

> Operator-facing recipe for dropping the
> [`CLAUDE.md`](./CLAUDE.md) template into a consumer repo so the
> repo's local Claude Code sessions prefer MEHO surfaces over local
> script fallbacks. The template itself is the deliverable; this
> guide is the wrapper that walks the install, verify, customise,
> and refresh paths.

## Why this template exists — the two-layer split

MEHO ships two layers of operating instructions; they meet a local
session from different angles, and the split is locked by
[decision #5](../../decisions/locked-decisions.md) of v0.2 planning:

* **Layer 1 — server-side tenant conventions.** Database-backed
  rules an admin curates per tenant (`meho conventions list/show/
  edit/history`). Auto-loaded into the MCP `initialize` response's
  `instructions` field for any agent connecting *through* MEHO.
  Bound to the **tenant**; binds every session no matter where it
  runs.
* **Layer 2 — this template.** The in-repo `CLAUDE.md` the
  operator's local Claude Code session reads when it opens a cloned
  consumer repo. Bound to the **repo on the operator's machine**;
  teaches the session "in this repo, prefer MEHO features over local
  fallbacks."

Without Layer 2, every operator with a local repo cloned has to
teach their Claude session to prefer MEHO features by hand, and the
result varies per operator. The starter template gives consumer
repos a consistent, upgradeable surface.

> **Current state (v0.2 staging).** Layer 1 is partially shipped —
> the database schema, API, CLI verbs, and seed migration land via
> [G7.1-T1..T3 + T5](https://github.com/evoila/meho/issues/229), but
> the assembler that fills the MCP `initialize` `instructions` field
> is [G7.1-T4 #316](https://github.com/evoila/meho/issues/316),
> still open. Until T4 ships, the `instructions` field is `None`
> and Layer 1 is not yet reaching agents at session start. Layer 2
> (this template) is live independently — your local session reads
> the file the moment it opens the repo, regardless of T4's state.

## Step 1 — Install the template

Copy `CLAUDE.md` from this directory into the **root** of your
consumer repo. If the repo already has a `CLAUDE.md`, merge: put
the MEHO routing rules above your existing repo-discipline rules,
and keep the existing rules below the merge marker at the bottom of
the template.

```bash
# From the root of your consumer repo, with $MEHO_REPO pointing at
# a local clone of evoila/meho:
cp "$MEHO_REPO/docs/examples/consumer-onboarding/CLAUDE.md" ./CLAUDE.md

# Or fetch a specific revision directly from GitHub:
curl -fsSL \
  "https://raw.githubusercontent.com/evoila/meho/main/docs/examples/consumer-onboarding/CLAUDE.md" \
  -o ./CLAUDE.md
```

Then fill in `$MEHO_INSTANCE` for your tenant. The template
references the variable; the cleanest pattern is to export it from
the operator's shell profile rather than hard-code it:

```bash
# In ~/.bashrc / ~/.zshrc / etc., one line per tenant you operate.
export MEHO_INSTANCE="https://meho.evba.lab"
```

If you prefer to inline the value in the file, replace every
`$MEHO_INSTANCE` reference in your local copy of `CLAUDE.md`;
diff-against-upstream stays clean as long as you keep the rest of
the template structure intact.

Commit the new (or merged) `CLAUDE.md` to your consumer repo. From
this commit onward, every Claude Code session that opens your repo
reads it on session start.

## Step 2 — Verify the local session is preferring MEHO

The verification bar is "an open Claude Code session in this repo
answers infra questions by calling `meho` verbs first, not by
reading local files." The commands below are checks the operator
can run *themselves* to confirm the tooling is present and the
template is parsed correctly; the prompt-driven verification at the
end confirms the session is actually preferring MEHO.

### 2a. Confirm the CLI is installed and authenticated

```console
$ meho version
meho/v0.2.x (commit <sha>; built <date>)

$ meho login "$MEHO_INSTANCE"   # if not already authenticated
# Follow the device-code flow in your browser.

$ meho status
Backplane: $MEHO_INSTANCE (reachable)
Operator: <your-sub>  Tenant: <your-tenant>  Role: <your-role>
```

If `meho status` returns "unreachable" or "auth_expired", fix that
first; the template's routing rules assume the CLI works.

### 2b. Confirm the template is in the repo root

```console
$ test -f CLAUDE.md && head -1 CLAUDE.md
# CLAUDE.md — MEHO-first operations
```

If the first line is something else, you merged with an existing
`CLAUDE.md` and the MEHO routing rules are lower in the file.
Confirm by grep:

```console
$ grep -c "Preferred MEHO surfaces" CLAUDE.md
1
```

A count of `0` means the merge didn't include the routing block;
re-merge.

### 2c. Confirm the CLI verbs the template references actually work

The template names concrete verbs (`meho kb search`, `meho remember`,
`meho targets list`, `meho status --watch`, etc.). Smoke-test each
surface area you care about — failures here mean either the CLI is
out of date or the connector isn't enabled for your tenant.

```console
$ meho kb search "test"            # Knowledge base reachable.
$ meho memory list                 # Memory reachable.
$ meho targets list --limit 1      # Target inventory reachable.
$ meho audit recent --limit 1      # Audit log reachable.
$ meho status --watch --op-class read  &  WATCH_PID=$!
$ sleep 2 && meho status > /dev/null    # produces one broadcast event
$ sleep 2 && kill $WATCH_PID            # close the subscriber
```

The `--watch` smoke test exercises the SSE feed end-to-end: opens
a subscriber, generates one event, sees it land. Empty output is a
broadcast-pipeline regression to file under
[#228 G6.1](https://github.com/evoila/meho/issues/228).

### 2d. Prompt-driven verification — does the session actually prefer MEHO?

Open a fresh Claude Code session in the repo and ask:

> *"How do I find recent activity against the rdc-vault target?"*

A session that has read the template answers with `meho audit
who-touched rdc-vault` (or `meho audit query --target rdc-vault`),
not with `grep -r vault scripts/` or by reading log files. Ask the
same question pre-template-install and post-install on a fresh
session to see the shift.

Other probe questions worth running:

* *"Where do I find KB entries on Vault provisioning?"* → expect
  `meho kb search "vault provisioning"`, not `grep -r kb/`.
* *"How do I list NSX tier-1 routers?"* → expect
  `meho nsx tier1 list`, not `curl https://nsx.../...` or
  `./scripts/nsx-...sh`.
* *"What's the right way to record this operator preference?"* →
  expect `meho remember "..." --scope user`, not `echo … >> notes.md`.

If the session reaches for local files instead of `meho` verbs,
re-check Step 2b (the template is in the repo root) and confirm the
session was started in the repo directory.

## Step 3 — Customise without breaking upgradeability

Tenant-specific additions go **at the bottom** of the file, below
the comment marker the template ships with:

```markdown
<!-- Add tenant-specific or repo-specific rules below this marker.
     Keep the canonical Layer-2 routing rules above untouched so
     diffs against upstream stay clean. -->

## Tenant-specific rules (rdc-internal)

- Production cluster patches require Slack #ops approval before
  `meho vmware cluster patch …`.
- Vault paths under `secret/customer/<id>/…` are operator-touch-only;
  agents must escalate before reading.
```

Patterns to follow:

* **Don't edit the top of the template in place** — the diff against
  upstream becomes noisy and re-pulls (Step 5) become merge
  conflicts. If you disagree with a canonical rule, file an issue
  upstream so the discussion lands in one place instead of
  fragmenting across tenants.
* **Tenant-wide operational rules belong in Layer 1, not Layer 2.**
  Use `meho conventions edit <slug>` (when the verb ships via
  [G7.1-T3 #315](https://github.com/evoila/meho/issues/315)) to
  publish them server-side so they reach agents connecting from
  *anywhere*, not just from this cloned repo. Reserve Layer 2 for
  things that genuinely apply only to operators working in this
  repo's checkout.

## Convention freshness — static-at-connect, reconnect-to-refresh

Tenant conventions are loaded into the session preamble **at session
connect time**, not continuously. The MCP `initialize` response on
each new connection assembles the preamble from the current
`tenant_conventions` rows and ships it as the spec-optional
`instructions` field (per
[MCP 2025-06-18 Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)).
After that, the session holds the preamble snapshot it received at
connect; subsequent edits do not retroactively reach already-
connected sessions.

What this means in practice:

* A `tenant_admin` who edits a convention via
  `meho conventions edit <slug>` while another operator's agent
  session is already running will **not** see that edit reflected in
  the running session. The edited convention reaches the running
  session only after the agent reconnects (e.g. the consumer harness
  restarts the MCP client, or the operator opens a new Claude
  session).
* New sessions opened after the edit see the new convention
  immediately, via their own `initialize` assembly.
* Audit-trail integrity is preserved either way — every edit writes
  the convention mutation + a `tenant_convention_history` row inside
  the route's DB transaction, while the audit middleware writes its
  `audit_log` row in its own session in the same response cycle. The
  two are linked by a pre-allocated `audit_id` soft-FK on the history
  row, so the diff trail joins back to the operator-attributed audit
  entry by exact uuid match — regardless of whether any live session
  sees the change mid-run.

This is the **v0.2 baseline** behaviour. A mid-session refresh path
is specified but conditional on MCP's
`capabilities.resources.subscribe` being advertised as `true`. When
that capability is enabled, edits emit
`notifications/resources/updated` for the
`meho://tenant/{tenant_id}/conventions/{slug}` resource, and
subscribing clients re-read the resource to refresh their preamble
mid-session.

**Current posture:** v0.2 ships `resources.subscribe: false` — the
`_initialize` capabilities envelope advertises `subscribe: false`,
and the helper that wraps every convention write
(`_maybe_emit_resource_updated`) is a no-op. v0.2.next flips both
halves together (capability advertisement + emit on write), kept in
sync by a single constant
(`RESOURCES_SUBSCRIBE_ENABLED` in
[`backend/src/meho_backplane/mcp/server.py`](https://github.com/evoila/meho/blob/main/backend/src/meho_backplane/mcp/server.py)).
Until then, **reconnect-to-refresh** above is the documented contract.

## Step 4 — Refresh when MEHO ships a new minor version

`meho version` reports the CLI version; `meho status` reports the
backplane version. When either bumps a minor (v0.2 → v0.3, etc.),
re-pull the template and merge.

```bash
# In your consumer repo:
git fetch --tags
LATEST=$(git ls-remote --tags https://github.com/evoila/meho.git \
  | awk -F/ '/v[0-9]/ {print $NF}' | sort -V | tail -1)

curl -fsSL \
  "https://raw.githubusercontent.com/evoila/meho/$LATEST/docs/examples/consumer-onboarding/CLAUDE.md" \
  -o CLAUDE.md.upstream

diff CLAUDE.md CLAUDE.md.upstream
# Hand-merge the diff: take upstream changes above the marker,
# keep your tenant-specific rules below it.
```

The Layer-2 marker (`<!-- Add tenant-specific … -->`) is the
diff-friendly boundary; merges should leave the canonical surface
matching upstream verbatim, with your additions strictly below.

## Step 5 — Troubleshooting

### The local session ignores routing — it reads files instead of running `meho`

* Confirm the session was started in the repo directory (not a
  parent). Claude Code reads `CLAUDE.md` from the directory it was
  launched in plus the workspace root.
* Confirm `CLAUDE.md` is at the repo root, not under `docs/` or a
  subdirectory.
* Confirm the routing block is intact — grep the file for
  "Preferred MEHO surfaces" (the template's load-bearing section
  header). Missing header = ineffective routing.

### `meho ...` verb fails with `unreachable`

The CLI can't reach `$MEHO_INSTANCE`. Walk the connectivity:

```console
$ echo "$MEHO_INSTANCE"
$ curl -fsS "$MEHO_INSTANCE/api/v1/health"
```

Empty `$MEHO_INSTANCE` → re-export it in your shell profile.
404/500 → MEHO is down or the URL is wrong; check with the
operator who provisioned the backplane.

### `meho ...` verb fails with `insufficient_role`

The operator's MEHO role doesn't satisfy the verb's requirement
(write ops require `operator`, admin ops require `tenant_admin`).
Confirm via `meho status` (renders the operator's tenant + role)
and request the role change through the realm admin if it's wrong.

### MCP `initialize` returns no preamble

The MCP server's `initialize` response carries `instructions: None`
until [G7.1-T4 #316](https://github.com/evoila/meho/issues/316)
lands the session-preamble assembler. This is **expected** in v0.2
staging — Layer 1 conventions are queryable via `meho conventions
list / show` once
[G7.1-T2/T3 #314/#315](https://github.com/evoila/meho/issues/229)
ship, but the auto-load into the MCP session preamble is gated on
T4. Layer 2 (this template) is unaffected — the local Claude
session reads `CLAUDE.md` directly from disk, independent of MCP.

### Broadcast meta-tools missing from `tools/list`

`broadcast_recent` / `broadcast_announce` / `broadcast_watch` are
referenced in the template's broadcast-discipline section as the
preferred surface for cross-operator awareness. In the current v0.2
staging build their MCP registration is not yet wired — the
underlying SSE feed and CLI subscriber ship via
[G6.1 #228](https://github.com/evoila/meho/issues/228), but the
named meta-tools are a follow-up. Until they land, the four-step
discipline maps onto:

* Step 1 (check before starting) — `meho audit who-touched <name>
  --since 30m` for "who's been here recently".
* Step 2 (announce intent) — explicit Slack/chat post.
* Step 3 (check in mid-flight) — `meho status --watch --target
  <name>` running in a background terminal.
* Step 4 (report on completion) — explicit Slack/chat post + the
  audit row id from your operation's CLI output.

When the meta-tools register, switch the discipline section in your
consumer-side `CLAUDE.md` to call them directly; the upstream
template will lead the change.

## References

* Template: [`CLAUDE.md`](./CLAUDE.md) in this directory.
* Directory README: [`README.md`](./README.md).
* Decision #5 (Layer 2 ship):
  [`docs/decisions/locked-decisions.md`](../../decisions/locked-decisions.md).
* Parent Initiative:
  [G7.1 #229](https://github.com/evoila/meho/issues/229).
* MCP client setup (the realm-side + client-side wire-up the
  template assumes is done):
  [`docs/cross-repo/mcp-client-setup.md`](../../cross-repo/mcp-client-setup.md).
* Broadcast onboarding (the SSE-feed contract this template
  references):
  [`docs/cross-repo/broadcast-onboarding.md`](../../cross-repo/broadcast-onboarding.md).
* CLI reference for every verb the template names: run
  `meho --help` and `meho <verb> --help` against the CLI version
  matching your backplane.
