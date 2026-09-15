---
name: operations
description: >
  Prefer MEHO surfaces to inspect and operate against infrastructure in a
  MEHO-wired repo. Use when acting on any target, reading live state,
  querying inventory or topology, or reviewing operational history —
  reach for `search_operations` / `call_operation`, per-connector
  `meho <connector> …` verbs, `list_targets` / `query_topology`, and
  `meho audit …` over local wrappers, raw API calls, or local files.
---

<!--
GENERATED FILE — DO NOT EDIT.
Rendered from docs/examples/consumer-onboarding/contract/meho-first-routing.md
by scripts/ci/gen_consumer_routing.py. Edit the contract source and re-run
the generator; backend/tests/test_consumer_routing_render.py fails CI on drift.
-->

# Operations — prefer MEHO verbs

Every operation through MEHO is authenticated, policy-checked, audited, and broadcast. Prefer MEHO verbs over local script wrappers and raw API calls. See `meho:prefer-meho` for the full route-by-evidence-need table.

## Connectors — per-connector verbs

Every operation through MEHO is authenticated, policy-checked, audited, and
broadcast. MEHO ships per-connector verbs that pre-bake the connector so you
don't type it on every dispatch. Prefer them over `./scripts/<wrapper>.sh`:

- **vSphere / vCenter** — `meho vmware vm list`,
  `meho vmware vm info <name-or-id>`, `meho vmware host list`,
  `meho vmware cluster list`.
- **Vault** — `meho vault kv read <mount> <path>`,
  `meho vault kv list <mount> <path>`, `meho vault kv put <mount> <path>`,
  `meho vault sys health`.
- **NSX** — `meho nsx tier0 list`, `meho nsx tier1 list`,
  `meho nsx segment list`, `meho nsx firewall policy list`.
- **bind9** — `meho bind9 zone list`, `meho bind9 zone read <zone>`,
  `meho bind9 config show <file>`.
- **Kubernetes** — `meho k8s namespace list`, `meho k8s node list`,
  `meho k8s ls <path>`, `meho k8s logs <pod>`.
- **Harbor / Hetzner / pfSense / gcloud / SDDC-Manager / VCF** — see
  `meho <connector> --help` for each.

## Generic dispatch and set-shaped results

When no alias verb exists yet, dispatch generically — same auth, audit, and
policy as the alias verbs:

- MCP: `search_operations` finds the `op_id`, `preview_operation` shows what
  a call would do, `call_operation` runs it (its operation argument is
  `op_id`; its `target` resolves by name).
- CLI: `meho operation search <connector_id> "<query>"` then
  `meho operation call <connector_id> <op_id> --target <name>`.

Any operation that returns a list larger than a handful of rows returns a
**result handle**, not the raw payload. **Drill into it with `result_query`
— never ask for the whole set dumped inline.** Page and filter through the
handle; that is the only supported way to read set-shaped results.

## Targets and topology

- **Inventory** — prefer `list_targets` (MCP) / `meho targets describe
  <name>` / `meho targets list` (CLI, filter with `--product vault` /
  `--product vsphere`) over reading a local `targets.yaml`. The backplane is
  authoritative.
- **Dependencies / blast radius** — `query_topology` reads the dependency
  graph: what a target depends on, what depends on it, the blast radius of a
  change. It is a governed read on the default surface, distinct from the
  flat inventory `list_targets` returns — reach for it before a change whose
  reach you have not established.

## Audit (canonical history)

Every MEHO op writes an audit row, so the audit log is the canonical,
queryable history — no ad-hoc logging needed. The working path is the CLI;
the MCP `query_audit` tool is operator-gated (`mcp:admin`).

- `meho audit recent` — last 24 h, filterable by op-id pattern.
- `meho audit query` — full filter (target, principal, op-id, op-class,
  result-status, time window).
- `meho audit show <audit-id>` — single-row detail.
- `meho audit who-touched <target>` — every operator who ran an op against a
  target in the recent window.
- `meho audit my-recent` — your own activity.

A client-credential service principal has no agent session id today
([evoila/meho#3446](https://github.com/evoila/meho/issues/3446)); query its
work by `work_ref` rather than by session.

## Break-glass — preference is not permission

**Break-glass** is the word for any reach past the backplane: a local
wrapper, a raw vendor call, or a local `kb/` / memory file used because the
governed surface could not serve the need. It is never silent and never
casual — it is an **explicit, recorded** action, and for a genuine
capability gap a **filed** one.

"MEHO can't do it" must be **established, not assumed**: check
`search_operations` / `list_operation_groups` / `list_targets` against the
live deploy before declaring a gap. Then, before breaking glass:

1. **Notify** the operator in the conversation — what MEHO surface you
   tried, why it can't cover the action (the verbatim error where one
   exists), and the local invocation you are about to run instead.
2. **File / cite** the gap — a genuine MEHO capability gap is one upstream
   issue per *distinct* gap (repeat encounters cite, they don't re-file); a
   target simply not registered yet is a consumer-side registration ticket,
   not an upstream bug.
3. **Record** the deviation on the work ticket, and **fall back** for that
   one action. The next action starts again at the top of the routing rule.

A capability gap the change depends on must be linked from the change (the
signal or the registration ticket travels with the PR) — preference states
where you *should* route; it is not permission to route past the backplane
unrecorded.
