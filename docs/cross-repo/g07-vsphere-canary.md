<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# G0.7 vSphere canary — operator runbook

**Status:** Acceptance gate for G0.7 ([#389](https://github.com/evoila/meho/issues/389)).
Drives Initiative #389 Task 8 ([#408](https://github.com/evoila/meho/issues/408))
to "done" once an operator has walked it successfully against the real
vSphere spec shelves.

This page is the upstream-side spec for the canary procedure. Operators
deploying MEHO at their own site re-run it against their own vCenter
when they want to validate a substrate change or roll a fresh
ingestion of the vendor specs. The acceptance test that mirrors this
procedure for CI lives at
[`backend/tests/acceptance/test_g07_vsphere_canary.py`](../../backend/tests/acceptance/test_g07_vsphere_canary.py).

## Contract

| Side | What it produces | What it verifies |
| --- | --- | --- |
| **Consumer** ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)) | OpenAPI shelves at `docs/vcenter-9.0/vcenter.yaml` (961 paths; modern REST) and `docs/vcenter-9.0/vi-json.yaml` (2,195 paths; JSON-over-HTTP VIM SOAP rendering). | Both files are valid OpenAPI 3.0/3.1 and reflect a real vCenter 9.0 surface. |
| **MEHO** (this repo) | The full G0.6 + G0.7 substrate (parser, register helper, LLM grouping, review state machine, meta-tools, CLI/REST/MCP surfaces). | Ingest produces ≥3,000 endpoint rows; LLM grouping yields 8-15 groups with <5% unassigned; `search_operations` finds each of 10 representative `govc` workflows in the **top-3** hits (canonical contract). The acceptance test enforces this strictly on the real-LLM opt-in path (`G07_CANARY_REAL_LLM=1`); the deterministic stub path softens to **top-15** because constant embeddings + SQLite-fallback BM25 are fuzzier than the production hybrid. Both paths assert the same metric — only the evaluation rigour differs. |

## When to run the canary

- After a substrate change that touches **any** of T1-T8 in G0.6
  ([#388](https://github.com/evoila/meho/issues/388)) or T1-T7 in G0.7
  ([#389](https://github.com/evoila/meho/issues/389)) — the canary is
  the cheapest end-to-end proof that the substrate still composes.
- When the consumer publishes a new vCenter spec version (e.g. 9.1)
  and an operator wants to verify the parser handles the new schema
  without flapping on existing top-3 hits.
- Before promoting a fresh `vmware-rest-9.0` ingest from `staged` to
  `enabled` — running the search-side benchmark catches the cases
  where the LLM-summarised `when_to_use` strings or per-op
  `llm_instructions` need operator tuning.
- As an acceptance gate before G3.1 ([#227](https://github.com/evoila/meho/issues/227))
  executes against the new substrate.

## Prerequisites

1. The consumer repo is checked out somewhere on the operator's
   machine, or the two YAML files are reachable as HTTP(S) URLs.
2. `meho` CLI built and on the operator's `PATH`. See
   [cli/README.md](../../cli/README.md) for build instructions.
3. The backplane is running locally **or** the operator has a JWT
   for a remote backplane in their environment.
4. (Optional) `vcsim` from the [govmomi](https://github.com/vmware/govmomi)
   repository for the dispatch smoke-test in step 6.

## The canary procedure

### Step 1 — Ingest both specs under one connector

```bash
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec docs:vcenter-9.0/vcenter.yaml \
  --spec docs:vcenter-9.0/vi-json.yaml \
  --json
```

Expected `IngestResponse`:

- `ingestion.inserted_count >= 3000` (961 + 2,195 minus the parser's
  ~5% non-method rejections).
- `ingestion.connector_registered == true` on the first run (the
  auto-shim fires once per fresh triple).
- `grouping.groups_created` in `[8, 15]`.
- `grouping.operations_unassigned < 0.05 * ingestion.inserted_count`.

The `docs:` URI prefix resolves against the operator's consumer-side
checkout via the chassis settings. Operators ingesting against an
HTTP(S)-served spec substitute the absolute URL.

> **Known substrate gap (T1 / Initiative #389 work item 2):** at the
> time of this writing `parse_openapi` does not yet inline
> `#/components/parameters/*` refs (only `#/components/schemas/*`).
> `vi-json.yaml` relies heavily on parameter refs, so attempting to
> ingest it raises `UnsupportedSpecError` and the multi-spec leg of
> the canary cannot complete end-to-end. The acceptance test at
> `backend/tests/acceptance/test_g07_vsphere_canary.py` catches this
> with a named `pytest.skip` rather than silently passing. Operators
> running the canary today against the live corpus will see
> `vcenter.yaml` alone produce ~1,275 endpoint rows and the
> connector ingest stop short of the 3,000 floor. The full assertion
> lights up once the parser grows parameter-ref support; track the
> follow-up under Initiative #389.

### Step 2 — Review the produced groups

```bash
meho connector review vmware-rest-9.0
```

Expected: a text table of 8-15 groups, each with a paragraph-length
`when_to_use` hint. For vCenter, the canonical groups are
`inventory`, `vm_lifecycle`, `vm_snapshot`, `cluster`, `host`,
`storage`, `networking`, `events`, `performance`, `session`.

This is the **load-bearing operator-review step** per the [CLAUDE.md
postulate 4](../../CLAUDE.md). The LLM produces draft hints; the
operator reads them and either edits weak ones or accepts them. An
agent calling `search_operations` later will land on these strings.

Operator edits the hints they find weak:

```bash
meho connector edit-group vmware-rest-9.0 vm_snapshot \
  --when-to-use "Take, revert, or remove VM snapshots. Use only when …"
```

### Step 3 — Mark destructive ops explicitly

The LLM's safety-level heuristic is `safe` for GET, `caution` for
POST/PUT/PATCH, `dangerous` for DELETE. The operator can flag known
destructive composites that fall outside the default:

```bash
meho connector edit-op vmware-rest-9.0 'DELETE:/api/vcenter/vm/{vm}' \
  --safety dangerous --requires-approval
```

`requires-approval` is read by the dispatcher; calls to that op return
a `requires_approval` extra rather than executing until an operator
signs off out-of-band.

### Step 4 — Enable the connector

```bash
meho connector enable vmware-rest-9.0 --confirm
```

The enable cascade flips `is_enabled=True` on every grouped op,
excluding rows the operator manually disabled via `edit-op`. Writes a
single `meho.connector.enable` audit row. The connector is now
visible to `list_operation_groups` / `search_operations` for the
tenant.

### Step 5 — Smoke-test the agent path

```bash
meho operation search vmware-rest-9.0 "list VMs in cluster"
```

Expected: ranked hits with `GET:/api/vcenter/vm` (or a vi-json
equivalent) in the top-3.

The complete govc-parity benchmark is the operator-facing acceptance
gate (also asserted by the canary test):

| `govc` workflow | Expected top-3 hit |
| --- | --- |
| `govc about` | `GET:/api/about` |
| `govc ls /` | `GET:/api/vcenter/datacenter` (or `cis` root) |
| `govc vm.info <name>` | `GET:/api/vcenter/vm/{vm}` |
| `govc vm.power -on` | `POST:/api/vcenter/vm/{vm}/power?action=start` |
| `govc snapshot.revert` | `vi-json:.../VirtualMachine/{moId}/RevertToSnapshot_Task` |
| `govc host.evac` | `POST:/api/vcenter/cluster/{cluster}/drs` (or vi-json equivalent) |
| `govc events` | `vi-json:.../EventManager/...` |
| `govc cluster.info` | `GET:/api/vcenter/cluster/{cluster}` |
| `govc datastore.ls` | `GET:/api/vcenter/datastore` |
| `govc network.ls` | `GET:/api/vcenter/network` |

A miss on any row means group hints or per-op `llm_instructions` need
operator tuning before the connector is fit for agent use. The
operator loops back to step 2 / 3 and edits.

### Step 6 — Smoke-test a dispatch

Requires a reachable vSphere target. `vcsim` covers the read-only
surface without needing a real vCenter:

```bash
# In a separate shell:
vcsim -l :8989

# Then:
meho operation call vmware-rest-9.0 'GET:/api/vcenter/cluster' \
  --target rdc-vcenter --json
```

Expected: a list of clusters from vcsim. The structured-error path
(`status='error'`, populated `extras.error_code`) means the
dispatcher reached the target but the target rejected the call;
that's a downstream-config issue, not a substrate regression.

### Rollback

A regression surfacing after enable rolls back via:

```bash
meho connector disable vmware-rest-9.0
```

The disable cascade flips `is_enabled=False` on every child op and
clears operator-override audit hints for the duration of the
disabled state. Re-enabling re-applies the operator's edit-op
overrides from the audit log. Disable is **non-destructive** — the
rows survive; only their dispatch surface flips. Re-ingest only
when the operator wants to drop the row set entirely.

## Acceptance gate (CI)

The canary procedure runs as an acceptance test on every PR that
touches G0.6 / G0.7 substrate:

```bash
MEHO_CONSUMER_DOCS_ROOT=/path/to/claude-rdc-hetzner-dc/docs \
  uv run pytest backend/tests/acceptance/test_g07_vsphere_canary.py -v
```

The test skips-in-sandbox when neither
`MEHO_VCENTER_OPENAPI` / `MEHO_VI_JSON_OPENAPI` nor
`MEHO_CONSUMER_DOCS_ROOT` resolves to a readable spec — that keeps
unit-test-only CI runs green while the corpus-provisioning workflow
is rolling out. The test fails loudly when the corpus is in play
and any acceptance criterion regresses.

### Opt-in extensions

Two opt-in test paths cover surfaces that the deterministic stub
cannot prove:

- `G07_CANARY_REAL_LLM=1` + `ANTHROPIC_API_KEY` opts into a real
  Claude Haiku grouping run. The operator running locally reads the
  produced `when_to_use` prose in the test's log output and judges
  quality. The strict **top-3** govc benchmark contract is asserted
  on this path; the stub-LLM canary uses a softer **top-15** window
  because constant embeddings + SQLite-fallback BM25 are fuzzier
  than the production hybrid.
- `MEHO_VCSIM_TARGET=<target-name>` opts into a live dispatch against
  a running vcsim. Seeds a `Target` row matching the name, monkey-
  patches the broadcast publisher's `publish_event`, then asserts
  `call_operation` returns `status='ok'`, exactly one
  `audit_log` row landed with `path=<op_id>` and `method='DISPATCH'`,
  and at least one `BroadcastEvent` was captured referencing the
  same `op_id`. Out of scope without explicit opt-in because vcsim
  is a separate Go binary the test cannot stand up on its own.

## Out of scope

- **Composite operations** (e.g. `vmware.composite.vm.create`) — those
  land in G3.1 vSphere
  ([#227](https://github.com/evoila/meho/issues/227)) on top of this
  ingested baseline.
- **Per-op `llm_instructions` quality polish** — vSphere ships with
  auto-generated `llm_instructions` from T3; further per-op tuning
  happens in G3.1's execution work.
- **Replacing `./scripts/govc.sh` calls with `meho vmware …`
  end-to-end** — the consumer-side dogfood loop lives in G3.1 + G4.3
  (eval & retire-checklist), not here.
- **Real vSphere dispatch** beyond the vcsim smoke — a full canary
  against a production vCenter is a deployment-side acceptance step,
  not a substrate gate.

## References

- Initiative: [#389 G0.7 spec ingestion pipeline](https://github.com/evoila/meho/issues/389).
- Goal: [#221 G0 substrate](https://github.com/evoila/meho/issues/221).
- Companion: [#388 G0.6 operation registry + dispatcher](https://github.com/evoila/meho/issues/388).
- Substrate docs: [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md).
- Architecture (TBD with #409): `docs/architecture/spec-ingestion.md`.
- Best-practices anchors:
  - [`.claude/skills/implement-issue/ai_engineering_best_practices.md`](../../.claude/skills/implement-issue/ai_engineering_best_practices.md) — evaluation discipline, corpus-based regression detection.
  - [`.claude/skills/implement-issue/devops_best_practices.md`](../../.claude/skills/implement-issue/devops_best_practices.md) — acceptance test patterns, env-gated skips.
